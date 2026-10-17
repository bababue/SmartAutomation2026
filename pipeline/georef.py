"""Georeferencing: turn printed coordinate text into WGS84 with an honest confidence.

Printed coordinates are ambiguous in several independent ways, all of which occur in the
challenge data:
- number formats: '7812.345,678' (dot thousands, comma decimal), '312,456.789', '523456,78'
- notations: decimal degrees, degrees + decimal minutes as '31,12,345', packed DDMMSS.ss
  as '153012.40', projected metres
- axis labels: X is northing in Korea, easting elsewhere; some reports swap N/E labels
- CRS: often only implied (UTM zone missing, national grid not named)

So we enumerate interpretations, transform each to WGS84 and score them by distance to the
place the document names (estimated by the extraction model). The best one wins; close
competitors are kept as `crs_candidates` and flagged.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from pyproj import Transformer

from schema.borehole import GeorefMethod, Location

# --- CRS knowledge -----------------------------------------------------------------

# Hints in printed CRS text → EPSG candidates (checked case-insensitively, in order).
CRS_HINTS: list[tuple[str, list[int]]] = [
    (r"mt\.? ?eden", [2105]),
    (r"nztm|nzgd ?2000 ?/ ?new zealand", [2193]),
    (r"osgb|ordnance survey|british national grid", [27700]),
    (r"mga ?94.*?(4[89]|5[0-6])|(4[89]|5[0-6]) ?[a-z]? ?mga ?94", []),  # zone handled below
    (r"mga ?2020", []),
    (r"etrs ?89.*utm ?32|utm32e89|euref89.*32", [25832]),
    (r"etrs ?89.*utm ?33|utm33e89", [25833]),
    (r"gauss|krüger|kruger|\bgk\b", [31466, 31467, 31468, 31469]),
    (r"korea|kgd ?2002|east belt", [5187, 5186, 5185, 5188]),
    # Korean origin names (GRS80 belt and the older Bessel "modified" belt are both in use)
    (r"동부\s?원점", [5187, 5176]),
    (r"중부\s?원점", [5186, 5174]),
    (r"서부\s?원점", [5185, 5173]),
    (r"동해\s?원점", [5188, 5177]),
    (r"posgar", [5343, 5344, 5345, 5346, 5347, 5348, 5349]),
    (r"sirgas", [31981, 31982, 31983, 31984, 31985]),
    (r"lv95|ch1903\+", [2056]),
    # Danish System 34 / 45 (local grids; the 'reconstruction east-orientated' CRSs have E/N axes)
    (r"\bs34s\b|system ?34 ?sj(?:ae|æ)lland", [10250]),
    (r"\bs34j\b|system ?34 ?jylland", [10160]),
    (r"\bs45b\b|system ?45 ?bornholm", [10254]),
    (r"rd ?new|amersfoort", [28992]),
    (r"lambert.?93", [2154]),
]

# Common national grids per country (ISO alpha-2). UTM zones are added from the place.
COUNTRY_CRS: dict[str, list[int]] = {
    "GB": [27700],
    "NZ": [2193, 2105],
    "AU": [28348, 28349, 28350, 28351, 28352, 28353, 28354, 28355, 28356],
    "KR": [5185, 5186, 5187, 5188, 5174, 5176, 2097],
    "DK": [25832, 25833, 10250, 10160, 10254],
    "DE": [25832, 25833, 31466, 31467, 31468, 31469],
    "NO": [25832, 25833],
    "ES": [25829, 25830, 25831],
    "BR": [31981, 31982, 31983, 31984, 31985],
    "AR": [5343, 5344, 5345, 5346, 5347, 5348, 5349],
    "CH": [2056],
    "AT": [31254, 31255, 31256],
    "FR": [2154],
    "NL": [28992],
    "IT": [6707, 6708],
}

GEOGRAPHIC = 4326

# East-oriented reconstructions of the Danish System 34/45 grids, whose original y axis points west.
WESTING_GRIDS = {10250, 10160, 10254}


@lru_cache(maxsize=None)
def _to_wgs84(epsg: int) -> Transformer:
    return Transformer.from_crs(epsg, GEOGRAPHIC, always_xy=True)


def _utm_epsgs(lat: float, lon: float, country: str | None) -> list[int]:
    zone = int((lon + 180) // 6) + 1
    zones = [z for z in (zone, zone - 1, zone + 1) if 1 <= z <= 60]
    base = 32600 if lat >= 0 else 32700
    epsgs = [base + z for z in zones]
    if country in ("US", "CA") and lat >= 0:
        epsgs += [26900 + z for z in zones if z <= 23]  # NAD83 / UTM
    return epsgs


def crs_from_hint(crs_raw: str | None) -> list[int]:
    if not crs_raw:
        return []
    text = crs_raw.lower()
    out: list[int] = []
    m = re.search(r"(4[89]|5[0-6]) ?[a-z]? ?mga ?94|mga ?94\D*(4[89]|5[0-6])", text)
    if m:
        out.append(28300 + int(m.group(1) or m.group(2)))
    for code in re.findall(r"epsg\s*:?\s*(\d{4,5})", text):
        out.append(int(code))
    m = re.search(r"mga ?2020\D*(4[6-9]|5[0-9])", text)
    if m:
        out.append(7800 + int(m.group(1)))  # GDA2020 / MGA zone 56 = EPSG:7856
    m = re.search(r"utm\D{0,8}(\d{1,2}) ?([c-x])?", text)
    if m:
        zone = int(m.group(1))
        band = (m.group(2) or "n").lower()
        south = band < "n" and band != "n" and m.group(2) is not None and "south" not in text
        if "south" in text or re.search(r"\b\d{1,2} ?s\b", text):
            south = True
        if 1 <= zone <= 60:
            if not south and re.search(r"nad ?83", text) and zone <= 23:
                out.append(26900 + zone)  # NAD83 / UTM zone N
            elif not south and re.search(r"nad ?27", text) and 3 <= zone <= 22:
                out.append(26700 + zone)  # NAD27 / UTM zone N
            else:
                out.append((32700 if south else 32600) + zone)
    for pattern, epsgs in CRS_HINTS:
        if epsgs and re.search(pattern, text):
            out.extend(epsgs)
    return list(dict.fromkeys(out))


# --- number parsing -----------------------------------------------------------------

LABEL = (
    r"(?P<label>latitude|longitude|lat|lon|long|northing|easting|nord|norte|este|øst|ost|"
    r"x-coord|y-coord|n|e|x|y)"
)
TOKEN = r"(?P<num>-?\d[\d.,']*\d|-?\d)"  # no spaces: '48.1 7.9' are two values


@dataclass
class Token:
    label: str | None  # normalised: 'n', 'e', 'x', 'y' or None
    raw: str
    values: list[float] = field(default_factory=list)  # alternative readings as plain numbers
    groups: list[str] = field(default_factory=list)  # digit groups if comma/dot-grouped like 31,12,345


def _norm_label(label: str | None) -> str | None:
    if not label:
        return None
    label = label.lower()
    if label in ("n", "northing", "nord", "norte", "lat", "latitude", "y-coord"):
        return "n" if label not in ("y-coord",) else "y"
    if label in ("e", "easting", "este", "øst", "ost", "lon", "long", "longitude"):
        return "e"
    if label in ("x", "x-coord"):
        return "x"
    if label == "y":
        return "y"
    return None


def _readings(raw: str) -> tuple[list[float], list[str]]:
    s = raw.replace(" ", "").replace("'", "")
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("-")
    seps = [c for c in s if c in ".,"]
    groups = re.split(r"[.,]", s)
    readings: list[float] = []
    if not s or not re.fullmatch(r"\d+(?:[.,]\d+)*", s):
        return [], []  # malformed, e.g. '1..2'

    if not seps:
        readings.append(float(s))
    elif len(set(seps)) == 2:
        # Both separators: the last one is the decimal separator.
        dec = s[max(s.rfind("."), s.rfind(","))]
        thou = "," if dec == "." else "."
        readings.append(float(s.replace(thou, "").replace(dec, ".")))
    else:
        sep = seps[0]
        if len(seps) == 1:
            after = len(groups[1])
            readings.append(float(s.replace(sep, ".")))  # decimal separator
            if after == 3:
                readings.append(float(s.replace(sep, "")))  # thousands separator
        else:
            readings.append(float(s.replace(sep, "")))  # thousands-grouped
            if all(len(g) == 3 for g in groups[1:-1]) and len(groups[-1]) != 3:
                readings.append(float("".join(groups[:-1]) + "." + groups[-1]))
    return [sign * r for r in dict.fromkeys(readings)], (groups if len(groups) >= 3 else [])


def _safe_readings(raw: str) -> tuple[list[float], list[str]]:
    try:
        return _readings(raw)
    except ValueError:  # malformed number: no reading rather than a crash
        return [], []


def parse_tokens(coords_raw: str) -> list[Token]:
    tokens: list[Token] = []
    text = coords_raw.replace("−", "-").replace("–", "-")
    # Suffix labels like '812345.67mN' or '402345.67mE'
    for m in re.finditer(r"(-?\d[\d.,]*\d)\s*m\s*([NE])\b", text):
        values, groups = _safe_readings(m.group(1))
        tokens.append(Token(m.group(2).lower(), m.group(1), values, groups))
    if tokens:
        return tokens
    pattern = re.compile(rf"(?:{LABEL}\s*[:=]?\s*)?{TOKEN}", re.IGNORECASE)
    for m in pattern.finditer(text):
        num = m.group("num").strip().rstrip(".,")
        if len(re.sub(r"\D", "", num)) < 2:
            continue
        values, groups = _safe_readings(num)
        label = _norm_label(m.group("label"))
        # '-E: 12345' style: a minus before the label belongs to the value
        start = m.start()
        if label and start > 0 and text[start - 1] == "-":
            values = [-v for v in values]
        tokens.append(Token(label, num, values, groups))
    return tokens


# One geographic coordinate as printed: 'S 33.9249', '33.9249 S', '48° 30′ 00″ N', 'Lat: -33,92'. Degrees take
# at most three digits and stand alone, so projected metres ('E 312456.78') never match.
GEO_COMPONENT = re.compile(
    r"(?:(?P<label>latitude|longitude|lat|lon|long)\.?\s*[:=]?\s*)?"
    r"(?:(?P<pre>[NSEW])\s*)?"
    r"(?<![\d.,])(?P<sign>-)?(?P<deg>\d{1,3}(?:[.,]\d+)?)(?!\d|[.,]\d)\s*(?P<degsym>°|º|˚|\bdeg\b)?\s*"
    r"(?:(?P<min>\d{1,2}(?:[.,]\d+)?)\s*(?:['′’]|\bmin\b)\s*)?"
    r"(?:(?P<sec>\d{1,2}(?:[.,]\d+)?)\s*(?:[\"″”]|''|′′|\bsec\b)\s*)?"
    r"(?(pre)|(?P<post>[NSEW](?![A-Za-z]))?)",  # 'S 33.9 E 151.2': the E belongs to the next value
    re.IGNORECASE,
)


def _geo_value(m: re.Match) -> tuple[str | None, float] | None:
    """(axis 'lat'/'lon'/None, signed degrees) of a matched component, None if it is not clearly geographic."""
    hemis = {h.upper() for h in (m.group("pre"), m.group("post")) if h}
    label = (m.group("label") or "").lower()
    if not (hemis or label or m.group("degsym") or m.group("min")) or len(hemis) > 1:
        return None
    deg, minutes, seconds = (float(g.replace(",", ".")) if g else 0.0 for g in m.group("deg", "min", "sec"))
    if (m.group("min") or m.group("sec")) and (not deg.is_integer() or minutes >= 60 or seconds >= 60):
        return None
    if m.group("sec") and not minutes.is_integer():
        return None
    value = deg + minutes / 60 + seconds / 3600
    axis = "lat" if hemis & {"N", "S"} or label.startswith("lat") else "lon" if hemis or label else None
    if m.group("sign") or hemis & {"S", "W"}:
        value = -value
    return axis, value


def geographic_reading(coords_raw: str) -> tuple[float, float, bool] | None:
    """(lat, lon, labelled) when the text is one latitude and one longitude written as degrees with
    hemisphere letters, degree signs or lat/lon labels. labelled=False: order assumed latitude first."""
    text = coords_raw.replace("−", "-").replace("–", "-")
    parts = [v for m in GEO_COMPONENT.finditer(text) if m.group("deg") and (v := _geo_value(m))]
    if len(parts) != 2:
        return None
    (axis_a, a), (axis_b, b) = parts
    if {axis_a, axis_b} == {"lat", "lon"}:
        lat, lon = (a, b) if axis_a == "lat" else (b, a)
        labelled = True
    elif axis_a is None and axis_b is None:
        lat, lon, labelled = a, b, False
    else:
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return lat, lon, labelled


def _sexagesimal(token: Token) -> list[tuple[float, bool]]:
    """Degree interpretations of a token as (value, alternative): '31,12,345' → 31°12.345' and, as an
    alternative reading, 31°12'34.5"; 153012.40 → 15°30'12.40"."""
    out: list[tuple[float, bool]] = []
    g = token.groups
    sign = -1.0 if token.raw.strip().startswith("-") else 1.0
    if len(g) == 3 and g[0].isdigit() and g[1].isdigit() and g[2].isdigit():
        deg, minutes = int(g[0]), float(f"{g[1]}.{g[2]}")
        if minutes < 60:
            out.append((sign * (deg + minutes / 60), False))
        if int(g[1]) < 60 and len(g[2]) <= 2 and int(g[2]) < 60:
            out.append((sign * (deg + int(g[1]) / 60 + int(g[2]) / 3600), False))
        elif int(g[1]) < 60 and len(g[2]) == 3 and int(g[2][:2]) < 60:
            # Seconds with tenths: rarer than decimal minutes, kept only to flag the ambiguity.
            out.append((sign * (deg + int(g[1]) / 60 + int(g[2]) / 36000), True))
    for v in token.values:
        whole = abs(v)
        digits = len(str(int(whole)))
        if digits in (6, 7):  # DDMMSS(.ss) or DDDMMSS(.ss)
            deg = int(whole // 10000)
            minutes = int((whole // 100) % 100)
            seconds = whole - deg * 10000 - minutes * 100
            if minutes < 60 and seconds < 60:
                out.append((math.copysign(deg + minutes / 60 + seconds / 3600, v), False))
    return out


# --- scoring ------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


@dataclass
class Candidate:
    lat: float
    lon: float
    crs: str  # 'EPSG:xxxx' or a notation name
    epsg: int | None
    easting: float | None
    northing: float | None
    note: str
    hinted: bool  # matches printed CRS text
    distance_km: float | None = None


def _pairs(tokens: list[Token]) -> list[tuple[Token, Token, bool]]:
    """(northing-ish, easting-ish, labelled) pairs to try; both orders when labels are unclear."""
    if len(tokens) < 2:
        return []
    labels = {t.label: t for t in tokens if t.label}
    if "n" in labels and "e" in labels:
        n, e = labels["n"], labels["e"]
        return [(n, e, True), (e, n, False)]  # some reports swap the labels
    if "x" in labels and "y" in labels:
        x, y = labels["x"], labels["y"]
        return [(y, x, True), (x, y, True)]  # X=easting (most) or X=northing (Korea, Switzerland old)
    a, b = tokens[0], tokens[1]
    return [(a, b, False), (b, a, False)]


def candidates(coords_raw: str, crs_raw: str | None, country: str | None, place: tuple[float, float] | None) -> list[Candidate]:
    if geo := geographic_reading(coords_raw):
        # Written as degrees with hemispheres or degree signs: no projected or swapped readings to try.
        lat, lon, labelled = geo
        readings = [(lat, lon)] if labelled else [(lat, lon)] + ([(lon, lat)] if abs(lon) <= 90 else [])
        note = "geographic" if labelled else "geographic (axes as guessed)"
        out = [Candidate(a, b, "EPSG:4326", GEOGRAPHIC, b, a, note, False) for a, b in readings]
        if place:
            for c in out:
                c.distance_km = haversine_km(c.lat, c.lon, place[0], place[1])
        return out
    tokens = parse_tokens(coords_raw)
    hinted = crs_from_hint(crs_raw) + crs_from_hint(coords_raw)
    epsgs = list(hinted)
    if country and country.upper() in COUNTRY_CRS:
        epsgs += COUNTRY_CRS[country.upper()]
    if place:
        epsgs += _utm_epsgs(place[0], place[1], country)
    epsgs = list(dict.fromkeys(epsgs))

    out: list[Candidate] = []
    for north_t, east_t, labelled in _pairs(tokens):
        swap_note = "" if labelled else " (axes as guessed)"
        # Geographic: decimal degrees and sexagesimal notations
        lats = [(v, False) for v in north_t.values if abs(v) <= 90] + _sexagesimal(north_t)
        lons = [(v, False) for v in east_t.values if abs(v) <= 180] + _sexagesimal(east_t)
        for lat, lat_alt in lats:
            for lon, lon_alt in lons:
                if abs(lat) <= 90 and abs(lon) <= 180 and (abs(lat) > 1 or abs(lon) > 1):
                    note = "geographic" + (" (alternative notation)" if lat_alt or lon_alt else "") + swap_note
                    out.append(Candidate(lat, lon, "EPSG:4326", GEOGRAPHIC, lon, lat, note, False))
        # Projected metres
        for n in north_t.values:
            for e in east_t.values:
                if abs(n) < 1000 and abs(e) < 1000:
                    continue
                for epsg in epsgs:
                    # Danish System 34/45 count y towards the west; logs print it with or without the sign.
                    for east in ([e, -e] if epsg in WESTING_GRIDS and e > 0 else [e]):
                        try:
                            lon, lat = _to_wgs84(epsg).transform(east, n)
                        except Exception:
                            continue
                        if math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180:
                            note = "projected" + (" (westing)" if east != e else "") + swap_note
                            out.append(Candidate(lat, lon, f"EPSG:{epsg}", epsg, east, n, note, epsg in hinted))
    if place:
        for c in out:
            c.distance_km = haversine_km(c.lat, c.lon, place[0], place[1])
    return out


@dataclass
class GeorefResult:
    location: Location
    issues: list[str]


def _confidence(distance_km: float, precision_km: float, explicit: bool) -> float:
    """explicit: the CRS/notation is stated or unambiguous (not inferred, e.g. a guessed UTM zone).
    An inferred CRS stays below the default release threshold: a person confirms it once."""
    tolerance = max(5.0, 2 * precision_km)
    if distance_km <= tolerance:
        return 0.95 if explicit else 0.8
    if distance_km <= 4 * tolerance:
        return 0.7
    if distance_km <= 100:
        return 0.4
    return 0.15


def georeference(
    coords_raw: str | None,
    crs_raw: str | None,
    country: str | None,
    place_lat: float | None,
    place_lon: float | None,
    place_precision_km: float | None,
    place_name: str | None = None,
) -> GeorefResult:
    issues: list[str] = []
    place = (place_lat, place_lon) if place_lat is not None and place_lon is not None else None
    precision = place_precision_km or 10.0
    loc = Location(coords_raw=coords_raw, crs_raw=crs_raw, country=country, place_name=place_name)

    cands = candidates(coords_raw, crs_raw, country, place) if coords_raw else []
    if cands and place:
        # Prefer hinted CRS and labelled axes slightly; then nearest to the named place.
        def rank(c: Candidate) -> float:
            penalty = 0.0 if c.hinted else 0.5
            penalty += 0.0 if "guessed" not in c.note else 1.0
            penalty += 0.0 if "alternative" not in c.note else 2.0  # flags only, never outranks
            # North America: NAD83 / UTM is the usual grid; WGS84 / UTM lands within metres, so only a
            # tie-break, otherwise the choice would follow noise in the place estimate.
            if (country or "").upper() in ("US", "CA") and c.epsg and 32601 <= c.epsg <= 32660:
                penalty += 0.05
            return (c.distance_km or 0) + penalty

        cands.sort(key=rank)
        best = cands[0]
        # A coarse place (e.g. only the country) cannot reject printed coordinates 300 km from its centre.
        if best.distance_km is not None and best.distance_km <= max(200.0, 2 * precision):
            _apply(loc, best, GeorefMethod.stated_coordinates)
            explicit = best.hinted or (best.epsg == GEOGRAPHIC and "guessed" not in best.note)
            loc.confidence = _confidence(best.distance_km, precision, explicit)
            loc.accuracy_m = 10.0 if loc.confidence >= 0.7 else best.distance_km * 1000
            if not explicit:
                issues.append(f"coordinate system inferred, not stated: {best.crs}" if best.epsg != GEOGRAPHIC
                              else "coordinate axes inferred, not labelled")
            rivals = [
                c for c in cands[1:]
                if c.distance_km is not None
                and c.distance_km <= max(25.0, best.distance_km * 3)
                and haversine_km(c.lat, c.lon, best.lat, best.lon) > 1.0
            ]
            if rivals:
                loc.crs_candidates = list(dict.fromkeys([best.crs] + [c.crs for c in rivals]))
                loc.confidence = min(loc.confidence, 0.5)
                issues.append("CRS ambiguous: " + ", ".join(loc.crs_candidates[:4]))
            else:
                # Readings too close to tell apart via the place (e.g. decimal minutes vs. seconds,
                # or another datum), but far enough apart to matter on site.
                spread = max((d for c in cands[1:] if 0.05 < (d := haversine_km(c.lat, c.lon, best.lat, best.lon)) <= 1.0),
                             default=None)
                if spread is not None:
                    loc.confidence = min(loc.confidence, 0.8)
                    loc.accuracy_m = max(loc.accuracy_m or 0.0, spread * 1000)
                    issues.append(f"coordinate notation or datum ambiguous: readings differ by up to {spread * 1000:.0f} m")
            if best.distance_km > 4 * max(5.0, 2 * precision):
                issues.append(f"coordinates {best.distance_km:.0f} km from the named place")
            return GeorefResult(loc, issues)
        issues.append("printed coordinates could not be matched to the named place")
    elif cands:
        hinted = [c for c in cands if c.hinted] or [c for c in cands if c.epsg == GEOGRAPHIC and "guessed" not in c.note]
        if hinted:
            best = hinted[0]
            _apply(loc, best, GeorefMethod.stated_coordinates)
            distinct = [c for c in hinted[1:] if haversine_km(c.lat, c.lon, best.lat, best.lon) > 1.0]
            if distinct:
                # Several readings of the printed text are possible and nothing tells them apart.
                loc.crs_candidates = list(dict.fromkeys([best.crs] + [c.crs for c in distinct]))
                loc.confidence = 0.2
                loc.accuracy_m = max(haversine_km(c.lat, c.lon, best.lat, best.lon) for c in distinct) * 1000
                issues.append("CRS ambiguous and no place to decide: " + ", ".join(loc.crs_candidates[:4]))
            else:
                loc.confidence = 0.5
                loc.accuracy_m = 10.0
                issues.append("no place to cross-check coordinates")
            return GeorefResult(loc, issues)
        issues.append("coordinates without CRS or place – not resolvable")

    if place:
        loc.lat, loc.lon = place
        loc.method = GeorefMethod.geocoded_place
        loc.accuracy_m = precision * 1000
        loc.confidence = 0.3
        issues.append("position from place name only")
    else:
        issues.append("not georeferenced")
    return GeorefResult(loc, issues)


def resolve_with(coords_raw: str, epsg: int, near: tuple[float, float] | None = None) -> Candidate | None:
    """Reading of printed coordinates in a CRS chosen by a reviewer: labelled axes and the main
    notation first, then the reading nearest the current position."""
    cands = [c for c in candidates(coords_raw, f"EPSG:{epsg}", None, near) if c.epsg == epsg]
    if not cands:
        return None
    return min(cands, key=lambda c: ("guessed" in c.note, "alternative" in c.note, c.distance_km or 0.0))


def _apply(loc: Location, c: Candidate, method: GeorefMethod) -> None:
    loc.lat, loc.lon = round(c.lat, 7), round(c.lon, 7)
    loc.easting, loc.northing = c.easting, c.northing
    loc.crs_epsg = c.epsg
    loc.method = method
