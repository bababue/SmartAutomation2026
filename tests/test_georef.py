"""Georeferencing tests. Inputs are public landmarks written in the notations found in the
challenge data (no confidential coordinates in the repo)."""

import pytest
from pyproj import Transformer

from pipeline.georef import georeference, haversine_km, parse_tokens


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
