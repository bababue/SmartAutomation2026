"""Georeferencing tests. Inputs are public landmarks written in the notations found in the
challenge data (no confidential coordinates in the repo)."""

import pytest
from pyproj import Transformer

from pipeline.georef import georeference, haversine_km, parse_tokens, resolve_with


def fwd(epsg: int, lat: float, lon: float) -> tuple[float, float]:
    e, n = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
    return e, n


def dm(v: float) -> str:
    """29.3897 → '29,23,382' (degrees, minutes, thousandths of minutes)."""
    d = int(abs(v))
    m = (abs(v) - d) * 60
    return f"{d},{int(m):02d},{round((m - int(m)) * 1000):03d}"


def packed_dms(v: float) -> str:
    d = int(v)
    m = int((v - d) * 60)
    s = ((v - d) * 60 - m) * 60
    return f"{d}{m:02d}{s:05.2f}"


def br(v: float) -> str:
    """Brazilian notation as printed: dot thousands, comma decimal (7.796.396,981)."""
    whole, frac = f"{v:.3f}".split(".")
    return f"{int(whole):,}".replace(",", ".") + "," + frac


CASES = []

# Kuwait: degrees + decimal minutes written with commas
lat, lon = 29.3897, 47.9983
CASES.append(("kuwait_dm", f"N: {dm(lat)} E: {dm(lon)}", None, "KW", (lat, lon)))

# Auckland: Mt Eden 2000 with mN/mE suffixes
lat, lon = -36.8485, 174.7622
e, n = fwd(2105, lat, lon)
CASES.append(("nz_mt_eden", f"{n:.2f}mN {e:.2f}mE", "Mt Eden 2000", "NZ", (lat, lon)))

# Wellington: NZTM with swapped N/E labels
lat, lon = -41.2865, 174.7762
e, n = fwd(2193, lat, lon)
CASES.append(("nz_swapped_labels", f"N{e:.1f} E{n:.1f}", None, "NZ", (lat, lon)))

# Sydney: MGA94 zone 56 named in the CRS text
lat, lon = -33.8568, 151.2153
e, n = fwd(28356, lat, lon)
CASES.append(("au_mga94", f"E: {e:.3f}, N: {n:.3f}", "56H MGA94", "AU", (lat, lon)))

# London: OSGB36 national grid
lat, lon = 51.5007, -0.1246
e, n = fwd(27700, lat, lon)
CASES.append(("uk_osgb", f"Easting {e:.3f} Northing {n:.3f}", "OSGB36", "GB", (lat, lon)))

# Busan: Korean East Belt, X = northing, thousands commas
lat, lon = 35.1151, 129.0422
e, n = fwd(5187, lat, lon)
CASES.append(("kr_x_is_north", f"X:{n:,.3f} Y:{e:,.3f}", None, "KR", (lat, lon)))

# São Paulo: UTM 23S without zone, Brazilian number format
lat, lon = -23.5505, -46.6333
e, n = fwd(32723, lat, lon)
CASES.append(("br_utm_no_zone", f"N= {br(n)} E= {br(e)}", None, "BR", (lat, lon)))

# Tripoli: packed DDMMSS.ss
lat, lon = 32.8872, 13.1913
CASES.append(("ly_packed_dms", f"E: {packed_dms(lon)} N: {packed_dms(lat)}", None, "LY", (lat, lon)))

# Baku: UTM 39N, X = easting, no zone printed
lat, lon = 40.4093, 49.8671
e, n = fwd(32639, lat, lon)
CASES.append(("az_utm_xy", f"X {e:.2f} Y {n:.2f}", None, "AZ", (lat, lon)))

# Cape Town: decimal degrees in X-COORD (= latitude) / Y-COORD fields
lat, lon = -33.9249, 18.4241
CASES.append(("za_decimal_xy", f"X-COORD : {lat} Y-COORD : {lon}", None, "ZA", (lat, lon)))


@pytest.mark.parametrize("name,coords,crs,country,truth", CASES, ids=[c[0] for c in CASES])
def test_resolves_to_truth(name, coords, crs, country, truth):
    # Place estimate deliberately off by a few km, as an LLM estimate would be.
    place = (truth[0] + 0.03, truth[1] - 0.03)
    result = georeference(coords, crs, country, place[0], place[1], 5.0)
    loc = result.location
    assert loc.lat is not None, result.issues
    assert haversine_km(loc.lat, loc.lon, *truth) < 0.05, (loc, result.issues)
    assert loc.confidence >= 0.5


def test_korea_flags_ambiguity_without_hint():
    lat, lon = 35.1151, 129.0422
    e, n = fwd(5187, lat, lon)
    # Place far from the truth but between candidate belts → must not claim high confidence
    result = georeference(f"X:{n:.3f} Y:{e:.3f}", None, "KR", 35.1, 128.0, 150.0)
    assert result.location.confidence <= 0.7


def test_no_coordinates_falls_back_to_place():
    result = georeference(None, None, "MY", 3.1, 101.8, 10.0)
    assert result.location.method.value == "geocoded_place"
    assert result.location.confidence <= 0.3
    assert "position from place name only" in result.issues


def test_negative_label_prefix():
    tokens = parse_tokens("-E: 12345 (m) N: 67890 (m)")
    east = next(t for t in tokens if t.label == "e")
    assert east.values[0] == -12345


def test_inferred_crs_is_not_released_automatically():
    lat, lon = 40.4093, 49.8671  # Baku, UTM 39N without a printed zone
    e, n = fwd(32639, lat, lon)
    result = georeference(f"X {e:.2f} Y {n:.2f}", None, "AZ", lat, lon, 5.0)
    assert result.location.confidence < 0.9
    assert any(i.startswith("coordinate system inferred") for i in result.issues)


def test_stated_crs_keeps_high_confidence():
    lat, lon = -33.8568, 151.2153  # Sydney, MGA94 zone 56 printed
    e, n = fwd(28356, lat, lon)
    result = georeference(f"E: {e:.3f}, N: {n:.3f}", "56H MGA94", "AU", lat, lon, 5.0)
    assert result.location.confidence >= 0.95
    assert not any("inferred" in i or "ambiguous" in i for i in result.issues)


def test_decimal_minutes_vs_seconds_is_flagged():
    # '29,23,382' reads as 29°23.382' (chosen) or 29°23'38.2" – a few hundred metres apart.
    lat, lon = 29.3897, 47.9983
    result = georeference(f"N: {dm(lat)} E: {dm(lon)}", None, "KW", lat, lon, 5.0)
    assert haversine_km(result.location.lat, result.location.lon, lat, lon) < 0.05
    assert result.location.confidence < 0.9
    assert any(i.startswith("coordinate notation or datum ambiguous") for i in result.issues)


# Hemisphere letters, degree signs and minutes/seconds (public landmarks)
GEO_TEXT = [
    ("33.8568 S, 151.2153 E", (-33.8568, 151.2153)),  # Sydney Opera House
    ("S 33.8568 E 151.2153", (-33.8568, 151.2153)),
    ("33,8568 S; 151,2153 E", (-33.8568, 151.2153)),  # decimal comma
    ("48° 51′ 30″ N, 2° 17′ 40″ E", (48.858333, 2.294444)),  # Eiffel Tower
    ("48°51'30\"N 2°17'40\"E", (48.858333, 2.294444)),
    ("N 48°51.5' E 2°17.6667'", (48.858333, 2.294444)),  # degrees and decimal minutes
    ("40°41′21″N 74°02′40″W", (40.689167, -74.044444)),  # Statue of Liberty
    ("W 74.0444 N 40.6892", (40.6892, -74.0444)),
    ("Lat: 48 Lon: 7", (48.0, 7.0)),
    ("Location: 48.8584 N, 2.2945 E (WGS84)", (48.8584, 2.2945)),  # Eiffel Tower; 'S84' is no latitude
    ("48.8584 N 2.2945 E ETRS89", (48.8584, 2.2945)),
]


@pytest.mark.parametrize("text,truth", GEO_TEXT)
def test_hemispheres_and_degree_signs(text, truth):
    r = georeference(text, None, None, None, None, None)
    assert r.location.crs_epsg == 4326
    assert r.location.lat == pytest.approx(truth[0], abs=1e-5)
    assert r.location.lon == pytest.approx(truth[1], abs=1e-5)


def test_degrees_need_a_marker_and_metres_are_not_degrees():
    from pipeline.georef import geographic_reading

    assert geographic_reading("48.1 7.9") is None  # plain numbers keep the existing search
    assert geographic_reading("E 312456.78 N 6251234.50") is None
    assert geographic_reading("N 29,23,382 E 47,59,898") is None  # degrees + decimal minutes in commas
    assert geographic_reading("48° 51′ 30″, 2° 17′ 40″") == pytest.approx((48.858333, 2.294444, False), abs=1e-5)


def test_place_decides_axis_order_without_hemispheres():
    # Sydney written longitude first, only degree signs: the named place settles it.
    r = georeference("151.2153°, -33.8568°", None, "AU", -33.87, 151.21, 5)
    assert (r.location.lat, r.location.lon) == pytest.approx((-33.8568, 151.2153))


def test_nad_datums_in_utm_hints():
    from pipeline.georef import crs_from_hint

    assert crs_from_hint("NAD83 / UTM zone 17N") == [26917]
    assert crs_from_hint("NAD 27 UTM 15") == [26715]
    assert crs_from_hint("WGS84 UTM 17N") == [32617]
    assert crs_from_hint("UTM 56 South") == [32756]
    # Toronto CN Tower in NAD83 / UTM 17N
    e, n = fwd(26917, 43.6426, -79.3871)
    r = georeference(f"E {e:.2f} N {n:.2f}", "NAD83 / UTM zone 17N", "CA", 43.65, -79.38, 5)
    assert r.location.crs_epsg == 26917
    assert haversine_km(r.location.lat, r.location.lon, 43.6426, -79.3871) < 0.01


def test_coarse_place_does_not_reject_printed_coordinates():
    # Only the country is known (centroid ~300 km away, precision 600 km): keep the printed position.
    lat, lon = -33.8568, 151.2153
    e, n = fwd(28356, lat, lon)
    result = georeference(f"E: {e:.3f}, N: {n:.3f}", "56H MGA94", "AU", lat + 2.5, lon - 1.5, 600.0)
    assert result.location.method.value == "stated_coordinates"
    assert haversine_km(result.location.lat, result.location.lon, lat, lon) < 0.05


def test_danish_system_34():
    # Copenhagen landmark in S34S (EPSG:10250), printed as on Danish logs: '-E: ... (m) N: ... (m) S34S'
    lat, lon = 55.6761, 12.5683
    e, n = fwd(10250, lat, lon)
    result = georeference(f"-E: {-e:.0f} (m) N: {n:.0f} (m) S34S", None, "DK", lat + 0.02, lon - 0.03, 5.0)
    assert result.location.crs_epsg == 10250, result.issues
    assert haversine_km(result.location.lat, result.location.lon, lat, lon) < 0.05


def test_danish_system_34_printed_as_positive_westing():
    lat, lon = 55.6761, 12.5683
    e, n = fwd(10250, lat, lon)  # e < 0 in the east-oriented reconstruction
    result = georeference(f"X : {-e:.0f} (m) Y : {n:.0f} (m)", None, "DK", lat + 0.01, lon - 0.02, 2.0)
    assert result.location.crs_epsg == 10250, result.issues
    assert haversine_km(result.location.lat, result.location.lon, lat, lon) < 0.05
    assert not any(i.startswith("CRS ambiguous") for i in result.issues)


def test_north_america_prefers_nad83_utm():
    lat, lon = 49.2488, -122.9805  # Burnaby, BC
    e, n = fwd(26910, lat, lon)
    for offset in (0.01, -0.01, 0.03):  # whatever the place estimate's noise
        result = georeference(f"N: {n:.1f} E: {e:.1f}", None, "CA", lat + offset, lon - offset, 5.0)
        assert result.location.crs_epsg == 26910


def test_oslo_local_grid_reads_x_as_northing():
    lat, lon = 59.9075, 10.7531  # Oslo Opera House
    east, north = Transformer.from_crs(4326, "ESRI:102138", always_xy=True).transform(lon, lat)
    r = georeference(f"X {north:.1f} Y {east:.1f}", "Koordinater og høyder Oslo lokal", "NO", 59.91, 10.75, 2.0)
    loc = r.location
    assert loc.crs_epsg is None and loc.crs_proj == "ESRI:102138"
    assert haversine_km(loc.lat, loc.lon, lat, lon) < 0.01
    assert loc.confidence >= 0.9
    assert not any("ambiguous" in i or "inferred" in i for i in r.issues)


def test_a_utm_zone_beside_norwegian_labels_is_no_coordinate():
    # Oslo City Hall in UTM 32N, written as Norwegian logs print it.
    e, n = Transformer.from_crs(4326, 32632, always_xy=True).transform(10.7337, 59.9119)
    raw = f"UTM Sone: 32 ØV-koordinat: {e:.0f} NS-koordinat: {n:.0f}"
    assert [(t.label, t.raw) for t in parse_tokens(raw)] == [("e", f"{e:.0f}"), ("n", f"{n:.0f}")]
    r = georeference(raw, "Kartdatum WGS 84, UTM Sone 32", "NO", 59.91, 10.75, 5.0)
    assert haversine_km(r.location.lat, r.location.lon, 59.9119, 10.7337) < 0.01
    assert r.location.confidence >= 0.9 and not r.issues


def test_a_utm_easting_split_in_transcription_resolves_to_no_point():
    # Brandenburg Gate in ETRS89 / UTM 33N, the easting split by a space as a model may copy it
    raw = "N= 5.819.670,000 E= 389.9 40,000"
    assert resolve_with(raw, 25833) is None  # neither 389.9 m nor the swapped axes
    assert resolve_with("N= 5.819.670,000 E= 389.940,000", 25833) is not None
    r = georeference(raw, "ETRS89 / UTM 33N", "DE", None, None, None)
    assert r.location.lat is None
