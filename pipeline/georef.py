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

from pyproj import CRS, Transformer

from schema.borehole import GeorefMethod, Location

from . import gazetteer

# A CRS is an EPSG code, or 'AUTH:code' for a local grid that has none.
Crs = int | str

# Oslo kommune's local grid (NGO 1948, axis III, shifted origin); X is the northing, as in all Norwegian surveys.
OSLO_LOCAL = "ESRI:102138"
X_NORTHING_GRIDS = {OSLO_LOCAL}

# Hints in printed CRS text → CRS candidates (checked case-insensitively, in order).
CRS_HINTS: list[tuple[str, list[Crs]]] = [
    (r"mt\.? ?eden", [2105]),
    (r"nztm|nzgd ?2000 ?/ ?new zealand", [2193]),
    (r"osgb|ordnance survey|british national grid", [27700]),
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
    (r"oslo ?(?:lokal|kommune)", [OSLO_LOCAL]),
]

# Common national grids per country (ISO alpha-2). UTM zones are added from the place.
COUNTRY_CRS: dict[str, list[Crs]] = {
    "GB": [27700],
    "NZ": [2193, 2105],
    "AU": [28348, 28349, 28350, 28351, 28352, 28353, 28354, 28355, 28356],
    "KR": [5185, 5186, 5187, 5188, 5174, 5176, 2097],
    "DK": [25832, 25833, 10250, 10160, 10254, 23032, 23033],
    "DE": [25832, 25833, 31466, 31467, 31468, 31469],
    "NO": [25832, 25833, 23032, 23033],
    "ES": [25829, 25830, 25831, 23029, 23030, 23031],
    "PT": [23029],
    "BR": [31981, 31982, 31983, 31984, 31985],
    "AR": [5343, 5344, 5345, 5346, 5347, 5348, 5349],
    "CH": [2056],
    "AT": [31254, 31255, 31256],
    "FR": [2154],
    "NL": [28992],
    "IT": [6707, 6708, 23032, 23033],
}

# Datums named beside a UTM zone: (pattern, EPSG base, zones that have a code).
UTM_DATUMS: list[tuple[str, int, range]] = [
    (r"etrs ?-?89|euref ?-?89|(?<=\d)e89\b", 25800, range(28, 39)),
    (r"\bed ?-?50\b|european datum 1950", 23000, range(28, 39)),
    (r"nad ?-?83", 26900, range(1, 24)),
    (r"nad ?-?27", 26700, range(3, 23)),
    (r"wgs ?-?84", 32600, range(1, 61)),
]
# Any other datum, as in 'PSAD56 / UTM 18S' or 'Pulkovo 1942 UTM 37': the zone alone then states no datum we know.
# GRS80 is the ellipsoid of ETRS89 and WGS84 alike, so 'UTM 32 GRS80' names no other datum.
OTHER_DATUM = re.compile(
    r"\b(?!utm|zone|sone|zona|huso|fuso|grs)[a-z]{2,6} ?[4-9]\d\b|\b[a-z]{3,} ?1[89]\d\d\b|pulkovo|clarke|bessel|hayford"
)

GEOGRAPHIC = 4326

# East-oriented reconstructions of the Danish System 34/45 grids, whose original y axis points west.
WESTING_GRIDS = {10250, 10160, 10254}


@lru_cache(maxsize=None)
def _to_wgs84(crs: Crs) -> Transformer:
    return Transformer.from_crs(crs, GEOGRAPHIC, always_xy=True)


@lru_cache(maxsize=None)
def _is_utm(crs: Crs) -> bool:
    try:
        return CRS.from_user_input(crs).utm_zone is not None
    except Exception:
        return False


def _utm_epsgs(lat: float, lon: float, country: str | None) -> list[int]:
    zone = int((lon + 180) // 6) + 1
    zones = [z for z in (zone, zone - 1, zone + 1) if 1 <= z <= 60]
    base = 32600 if lat >= 0 else 32700
    epsgs = [base + z for z in zones]
    if country in ("US", "CA") and lat >= 0:
        epsgs += [26900 + z for z in zones if z <= 23]  # NAD83 / UTM
    return epsgs


def _utm_hint(text: str) -> tuple[list[int], bool]:
    """UTM codes the lower-case CRS text names, and whether it names their datum too."""
    named = [(base, zones) for pattern, base, zones in UTM_DATUMS if re.search(pattern, text)]
    # Datum names carry digits of their own ('WGS 84'): the zone is read without them.
    m = re.search(r"utm\D{0,12}(\d{1,2}) ?([c-x])?", re.sub("|".join(p for p, _, _ in UTM_DATUMS), "", text))
    if not m:
        return [], False
    zone = int(m.group(1))
    south = m.group(2) is not None and m.group(2) < "n" and "south" not in text
    if "south" in text or re.search(r"\b\d{1,2} ?s\b", text):
        south = True
    if not named:
        if OTHER_DATUM.search(text) or not 1 <= zone <= 60:
            return [], False
        return [(32700 if south else 32600) + zone], False
    # Only WGS84 has southern zones here; beside another datum an 'S' is the latitude band (Spain lies in band S).
    return [(32700 if south and base == 32600 else base) + zone for base, zones in named if zone in zones], True


def _crs_hints(crs_raw: str | None) -> tuple[list[Crs], list[Crs]]:
    """CRS candidates the printed text names, and those of them whose datum it names (not only a UTM zone)."""
    if not crs_raw:
        return [], []
    text = crs_raw.lower()
    out: list[Crs] = []
    m = re.search(r"(4[89]|5[0-6]) ?[a-z]? ?mga ?94|mga ?94\D*(4[89]|5[0-6])", text)
    if m:
        out.append(28300 + int(m.group(1) or m.group(2)))
    for code in re.findall(r"epsg\s*:?\s*(\d{4,5})", text):
        out.append(int(code))
    m = re.search(r"mga ?2020\D*(4[6-9]|5[0-9])", text)
    if m:
        out.append(7800 + int(m.group(1)))  # GDA2020 / MGA zone 56 = EPSG:7856
    utm, datum_named = _utm_hint(text)
    out += utm
    for pattern, epsgs in CRS_HINTS:
        if re.search(pattern, text):
            out.extend(epsgs)
    out = list(dict.fromkeys(out))
    return out, [c for c in out if datum_named or c not in utm]


def crs_from_hint(crs_raw: str | None) -> list[Crs]:
    return _crs_hints(crs_raw)[0]


# 'ØV-koordinat' and 'NS-koordinat' are Norwegian for the east-west and north-south coordinate.
LABEL = (
    r"(?P<label>latitude|longitude|lat|lon|long|northing|easting|nord|norte|este|øst|ost|"
    r"x-coord|y-coord|øv|ns|n|e|x|y)(?:-?koordinat(?:e|en)?)?"
)
# A UTM zone printed beside the values ('UTM Sone: 32') is no coordinate; the zone is read from the text itself.
ZONE = re.compile(r"\b(?:utm\s*)?(?:zone|sone|zona)\s*[:=]?\s*\d{1,2}\s*[c-x]?(?![\w.,])", re.IGNORECASE)
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
    if label in ("n", "ns", "northing", "nord", "norte", "lat", "latitude", "y-coord"):
        return "n" if label not in ("y-coord",) else "y"
    if label in ("e", "øv", "easting", "este", "øst", "ost", "lon", "long", "longitude"):
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
    # A label is a word of its own, not the last letter of one ('Sone 32' has no easting 32).
    pattern = re.compile(rf"(?:(?<![^\W\d_]){LABEL}\s*[:=]?\s*)?{TOKEN}", re.IGNORECASE)
    for m in pattern.finditer(ZONE.sub(" ", text)):
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
    r"(?:(?<![A-Za-z])(?P<pre>[NSEW])\s*)?"  # not the last letter of a datum name ('WGS84', 'ETRS89')
    r"(?<![\d.,])(?P<sign>-)?(?P<deg>\d{1,3}(?:[.,]\d+)?)(?!\d|[.,]\d)\s*(?P<degsym>°|º|˚|\bdeg\b)?\s*"
    r"(?:(?P<min>\d{1,2}(?:[.,]\d+)?)\s*(?:['′’]|\bmin\b)\s*)?"
    r"(?:(?P<sec>\d{1,2}(?:[.,]\d+)?)\s*(?:[\"″”]|''|′′|\bsec\b)\s*)?"
    r"(?(pre)|(?P<post>[NSEW](?![A-Za-z]))?)",  # 'S 33.9 E 151.2': the E belongs to the next value
    re.IGNORECASE,
)
# Degrees followed by bare minutes (and seconds), as in 'N 48 01 12' or 'Lat 47 59.876'. Only after a hemisphere
# letter, lat/lon label or degree sign, or before a hemisphere letter: '48.1 7.9' stays two values.
_NUM = r"\d+(?:[.,]\d+)?(?![.,]?\d)"
SPACED_DMS = re.compile(
    r"(?P<intro>(?<![A-Za-z])(?:(?:latitude|longitude|lat|long|lon)\.?\s*[:=]?|[NSEW])\s*)?"
    rf"(?<![\d.,])(?P<deg>-?{_NUM})\s*(?P<degsym>°|º|˚)?(?P<rest>(?:\s+{_NUM})+)"
    r"(?!\s*(?:\d|['′’\"″”°º˚]|min\b|sec\b))"
    r"(?(intro)|(?(degsym)|\s*[NSEW](?![A-Za-z])))",
    re.IGNORECASE,
)


def _mark_spaced_dms(text: str) -> str | None:
    """'N 48 01 12' → 'N 48° 01′ 12″'. None when bare numbers after a degree value cannot be its minutes and
    seconds: reading only the degrees would drop digits."""
    out, end = [], 0
    for m in SPACED_DMS.finditer(text):
        groups = m.group("rest").split()
        if (len(groups) > 2 or not re.fullmatch(r"-?\d{1,3}(?:[.,]\d+)?", m.group("deg"))
                or not all(re.fullmatch(r"\d{1,2}(?:[.,]\d+)?", g) for g in groups)):
            return None
        out += [text[end:m.start("deg")], f"{m.group('deg')}° " + " ".join(g + s for g, s in zip(groups, "′″"))]
        end = m.end("rest")
    return "".join(out) + text[end:]


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
    text = _mark_spaced_dms(coords_raw.replace("−", "-").replace("–", "-"))
    if text is None:
        return None
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


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


@dataclass
class Candidate:
    lat: float
    lon: float
    crs: str  # 'EPSG:xxxx', 'AUTH:code' of a local grid, or a notation name
    epsg: int | None
    easting: float | None
    northing: float | None
    note: str
    hinted: bool  # matches printed CRS text
    datum_stated: bool = False  # the printed text names this CRS's datum, not only a UTM zone
    distance_km: float | None = None


def pairs(tokens: list[Token]) -> list[tuple[Token, Token, bool]]:
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
    (hinted, stated), (hinted_in_coords, stated_in_coords) = _crs_hints(crs_raw), _crs_hints(coords_raw)
    hinted, stated = hinted + hinted_in_coords, stated + stated_in_coords
    epsgs = list(hinted)
    if country and country.upper() in COUNTRY_CRS:
        epsgs += COUNTRY_CRS[country.upper()]
    if place:
        epsgs += _utm_epsgs(place[0], place[1], country)
    epsgs = list(dict.fromkeys(epsgs))

    out: list[Candidate] = []
    for north_t, east_t, labelled in pairs(tokens):
        swap_note = "" if labelled else " (axes as guessed)"
        lats = [(v, False) for v in north_t.values if abs(v) <= 90] + _sexagesimal(north_t)
        lons = [(v, False) for v in east_t.values if abs(v) <= 180] + _sexagesimal(east_t)
        for lat, lat_alt in lats:
            for lon, lon_alt in lons:
                if abs(lat) <= 90 and abs(lon) <= 180 and (abs(lat) > 1 or abs(lon) > 1):
                    note = "geographic" + (" (alternative notation)" if lat_alt or lon_alt else "") + swap_note
                    out.append(Candidate(lat, lon, "EPSG:4326", GEOGRAPHIC, lon, lat, note, False))
        for n in north_t.values:
            for e in east_t.values:
                if abs(n) < 1000 and abs(e) < 1000:
                    continue
                for crs in epsgs:
                    if crs in X_NORTHING_GRIDS and labelled and north_t.label != "x":
                        continue
                    # A UTM easting lies within some 1,000 km of 500,000 m. One far outside is a number split
                    # in transcription ('389.9 40,000') or a swapped axis, which would still resolve to a point.
                    if not 1000 <= abs(e) < 2_000_000 and _is_utm(crs):
                        continue
                    # Danish System 34/45 count y towards the west; logs print it with or without the sign.
                    for east in ([e, -e] if crs in WESTING_GRIDS and e > 0 else [e]):
                        try:
                            lon, lat = _to_wgs84(crs).transform(east, n)
                        except Exception:
                            continue
                        # UTM is defined from 80°S to 84°N: beyond that, a northing lost its leading digits.
                        if math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180 \
                                and not (_is_utm(crs) and not -80.0 <= lat <= 84.0):
                            note = "projected" + (" (westing)" if east != e else "") + swap_note
                            name, epsg = (f"EPSG:{crs}", crs) if isinstance(crs, int) else (crs, None)
                            out.append(Candidate(lat, lon, name, epsg, east, n, note, crs in hinted, crs in stated))
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
    """The location text is looked up in the gazetteer. Its place replaces the model's estimate where it is the
    only one (or the one of several namesakes the estimate lies at) and agrees with the estimate, or where the
    printed coordinates fit it better; otherwise the estimate stays, with a finding. A name that fits several
    places, or none in the stated country, keeps the estimate and says so."""
    found = gazetteer.lookup(place_name, country)
    estimate = (place_lat, place_lon) if place_lat is not None and place_lon is not None else None
    if found.ambiguous and estimate:
        near = [p for p in found.ambiguous if haversine_km(*estimate, p.lat, p.lon) <= 50.0]
        if len(near) == 1:
            found = gazetteer.Lookup(place=near[0])
    if found.ambiguous:
        result = _georeference(coords_raw, crs_raw, country, place_lat, place_lon, place_precision_km, place_name)
        result.issues.insert(0, "place name fits several places: "
                             + ", ".join(f"{p.name} ({p.country})" for p in found.ambiguous))
        return result
    if found.elsewhere and (not estimate or any(haversine_km(*estimate, e.lat, e.lon) <= 50.0 for e in found.elsewhere)):
        # Small places are missing from the gazetteer: only an estimate at a namesake abroad is a finding.
        result = _georeference(coords_raw, crs_raw, country, place_lat, place_lon, place_precision_km, place_name)
        result.issues.insert(0, f"place name not in {country}, only in: "
                             + ", ".join(f"{p.name} ({p.country})" for p in found.elsewhere))
        return result
    g = found.place
    if g is None:
        return _georeference(coords_raw, crs_raw, country, place_lat, place_lon, place_precision_km, place_name)
    by_gazetteer = _georeference(coords_raw, crs_raw, country, g.lat, g.lon, g.precision_km, place_name)
    if not estimate:
        return by_gazetteer
    # With an estimate, the gazetteer place only checks printed coordinates: a position from the place name
    # alone stays where it was (moving it a few km would drop a reviewer's approval for nothing).
    by_estimate = _georeference(coords_raw, crs_raw, country, place_lat, place_lon, place_precision_km, place_name)
    stated = GeorefMethod.stated_coordinates
    distance = haversine_km(*estimate, g.lat, g.lon)
    if distance <= max(50.0, 2 * (place_precision_km or 10.0), 2 * g.precision_km):
        return by_gazetteer if by_gazetteer.location.method == stated else by_estimate
    # Far apart: a street or region can be named after another town. The printed coordinates decide.
    if by_gazetteer.location.method == stated and by_estimate.location.method != stated:
        by_gazetteer.issues.insert(0, f"place estimate {distance:.0f} km from {g.name}, {g.country} (gazetteer), "
                                      "where the printed coordinates fit")
        by_gazetteer.location.confidence = min(by_gazetteer.location.confidence or 0.0, 0.5)
        return by_gazetteer
    if by_estimate.location.method != stated:  # coordinates that fit the estimate settle it
        by_estimate.issues.insert(0, f"place estimate {distance:.0f} km from {g.name}, {g.country} (gazetteer); "
                                     "estimate kept")
    return by_estimate


def _georeference(
    coords_raw: str | None,
    crs_raw: str | None,
    country: str | None,
    place_lat: float | None,
    place_lon: float | None,
    place_precision_km: float | None,
    place_name: str | None,
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
            # ED50 lies some 100-250 m from the current datums, which the place cannot resolve: unstated, an ED50
            # reading only flags the datum as ambiguous.
            if not c.hinted and c.epsg and 23028 <= c.epsg <= 23038:
                penalty += 0.5
            return (c.distance_km or 0) + penalty

        cands.sort(key=rank)
        best = cands[0]
        # A coarse place (e.g. only the country) cannot reject printed coordinates 300 km from its centre.
        if best.distance_km is not None and best.distance_km <= max(200.0, 2 * precision):
            apply_reading(loc, best)
            explicit = best.hinted or (best.epsg == GEOGRAPHIC and "guessed" not in best.note)
            loc.confidence = _confidence(best.distance_km, precision, explicit)
            loc.accuracy_m = 10.0 if loc.confidence >= 0.7 else best.distance_km * 1000
            if not explicit:
                issues.append(f"coordinate system inferred, not stated: {best.crs}" if best.epsg != GEOGRAPHIC
                              else "coordinate axes inferred, not labelled")
            # A rival is another reading the place estimate cannot rule out (within its own uncertainty).
            # Readings that ignore printed axis labels are no rivals of a labelled reading.
            labelled = "guessed" not in best.note
            rivals = [
                c for c in cands[1:]
                if c.distance_km is not None
                and c.distance_km <= max(25.0, best.distance_km * 3, min(2 * precision, 300.0))
                and haversine_km(c.lat, c.lon, best.lat, best.lon) > 1.0
                and not (labelled and "guessed" in c.note)
            ]
            if rivals:
                loc.crs_candidates = list(dict.fromkeys([best.crs] + [c.crs for c in rivals]))
                loc.confidence = min(loc.confidence, 0.5)
                issues.append("CRS ambiguous: " + ", ".join(loc.crs_candidates[:4]))
            else:
                # Readings too close to tell apart via the place (e.g. decimal minutes vs. seconds,
                # or another datum), but far enough apart to matter on site. A stated datum rules out the others.
                others = [c for c in cands[1:] if c.hinted or c.crs == best.crs or not best.datum_stated]
                spread = max((d for c in others if 0.05 < (d := haversine_km(c.lat, c.lon, best.lat, best.lon)) <= 1.0),
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
            apply_reading(loc, best)
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


def apply_reading(loc: Location, c: Candidate) -> None:
    """Position `loc` at the reading `c` of its printed coordinates; coordinates from a summary table stay
    marked as such."""
    loc.lat, loc.lon = round(c.lat, 7), round(c.lon, 7)
    loc.easting, loc.northing = c.easting, c.northing
    loc.crs_epsg = c.epsg
    loc.crs_proj = c.crs if c.epsg is None else None
    if loc.method != GeorefMethod.linked_table:
        loc.method = GeorefMethod.stated_coordinates


def is_precise(loc: Location) -> bool:
    """A position read from printed coordinates, on the log or in a summary table."""
    return loc.lat is not None and loc.method in (GeorefMethod.stated_coordinates, GeorefMethod.linked_table)
