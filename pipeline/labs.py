"""Lab values: printed values and units converted to the stored units in code, never by the model, checked
against plausible ranges, and kept once per test."""

from __future__ import annotations

import re

from schema.borehole import Borehole, LabParameter, LabResult, SourceRef

from .extraction_models import XLab, XLabRow


def _num(raw: str | None) -> float | None:
    """First numeric literal incl. exponent ('1.2E-6', '3,5x10-7'); decimal comma accepted."""
    if not raw:
        return None
    text = raw.replace(" ", "").replace("×", "x")
    m = re.search(r"(-?\d+(?:[.,]\d+)?)(?:[eE]([-+]?\d+)|x10\^?([-+−]?\d+))?", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    exponent = m.group(2) or (m.group(3) or "").replace("−", "-")
    return value * 10 ** int(exponent) if exponent else value


_STRENGTH = {LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}
_SHEAR = {LabParameter.cohesion, LabParameter.undrained_shear_strength}
_WEIGHT = {LabParameter.unit_weight, LabParameter.dry_unit_weight}
# Units each parameter is stored in, as printed without conversion (see LAB_UNITS); a value with any
# other unit that has no factor below is dropped rather than published in the wrong unit.
_LAB_NATIVE: list[tuple[set[LabParameter], str]] = [
    (_STRENGTH, r"^(mpa|n/mm2)$"),
    (_SHEAR, r"^(kpa|kn/m2)$"),
    (_WEIGHT, r"^kn/m3$"),
    ({LabParameter.water_content, LabParameter.consistency, LabParameter.fines, LabParameter.sand, LabParameter.gravel,
      LabParameter.liquid_limit, LabParameter.plastic_limit}, r"^(%|percent)$"),
    ({LabParameter.friction_angle}, r"^(°|deg|degrees?)$"),
    ({LabParameter.hydraulic_conductivity}, r"^m/s(ec)?$"),
]
_DIMENSIONLESS = {LabParameter.cai, LabParameter.rmr, LabParameter.q, LabParameter.gsi, LabParameter.plasticity_index,
                  LabParameter.consistency_index, LabParameter.poissons_ratio, LabParameter.other}

# (parameters, unit pattern, factor to the target unit); the first match converts.
_LAB_FACTORS: list[tuple[set[LabParameter], str, float]] = [
    (_STRENGTH, r"^psi$", 0.00689476),
    (_STRENGTH, r"^ksi$", 6.89476),
    (_STRENGTH, r"^kn/m2$", 0.001),
    (_SHEAR, r"^psi$", 6.89476),
    (_SHEAR, r"^t/?m2$", 9.80665),
    (_WEIGHT, r"^pcf$|^lb/ft3$", 0.157087),
    (_STRENGTH, r"^kpa$", 0.001),
    (_STRENGTH, r"^kg/?cm2|kgf/cm", 0.0980665),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.e_modulus}, r"^gpa$", 1000.0),
    (_SHEAR, r"^kg/?cm2|kgf/cm", 98.0665),
    (_SHEAR, r"^mpa$", 1000.0),
    (_WEIGHT, r"^t/?m3|tn/?m3|g/?cm3|mg/m3", 9.80665),
    ({LabParameter.hydraulic_conductivity}, r"^cm/s", 0.01),
    ({LabParameter.hydraulic_conductivity}, r"^m/d(ay)?$", 1 / 86400),
]

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?(?:\s*(?:[eE]|[x×*]\s*10\^?)\s*[-+−]?\d+)?"


def lab_qualifier(value_raw: str) -> str | None:
    """'<0.01' → '<', '5 - 10' → 'range': the stored number is then not an exact value."""
    m = re.match(r"\s*([<>≤≥])", value_raw or "")
    if m:
        return m.group(1)
    return "range" if re.fullmatch(rf"\s*{_NUMBER}\s*[-–~]\s*{_NUMBER}\s*", value_raw or "") else None


# Digits grouped by commas ('12,500', '1,250,000'), with no exponent after them.
_GROUPED = re.compile(r"[<>≤≥~]?(\d{1,3}(?:,\d{3})+)(?![\d.,eEx×])")


def lab_value(parameter: LabParameter, value_raw: str, unit_raw: str | None) -> float | None:
    grouped = _GROUPED.match((value_raw or "").replace(" ", ""))
    if not grouped:
        return _in_unit(parameter, _num(value_raw), unit_raw)
    digits = grouped.group(1)
    readings = [float(digits.replace(",", ""))]
    if digits.count(",") == 1:  # '12,500' is 12.5 with a decimal comma or 12500 with a thousands separator
        readings.append(float(digits.replace(",", ".")))
    values = [v for r in readings if (v := _in_unit(parameter, r, unit_raw)) is not None]
    fits = [v for v in values if plausible(parameter, v)]
    if len(fits) == 1:
        return fits[0]
    # Both readings plausible: not published, like a value without a number. Neither: dropped as implausible.
    return None if fits else next(iter(values), None)


def _in_unit(parameter: LabParameter, value: float | None, unit_raw: str | None) -> float | None:
    if value is None:
        return None
    unit = (unit_raw or "").lower().replace(" ", "").replace("³", "3").replace("²", "2").strip("()[]")
    for params, pattern, factor in _LAB_FACTORS:
        if parameter in params and re.search(pattern, unit):
            return value * factor
    if not unit or unit == "-" or parameter in _DIMENSIONLESS:
        return value
    if any(parameter in params and re.search(pattern, unit) for params, pattern in _LAB_NATIVE):
        return value
    return None  # unknown unit: do not publish a number in the wrong unit


def lab_upper(parameter: LabParameter, value_raw: str, unit_raw: str | None) -> float | None:
    match = re.fullmatch(rf"\s*{_NUMBER}\s*[-–~]\s*({_NUMBER})\s*", value_raw)
    return lab_value(parameter, match.group(1), unit_raw) if match else None


def lab_result(row: XLab | XLabRow, value: float, top_m: float | None, bottom_m: float | None,
                source: SourceRef) -> LabResult:
    return LabResult(parameter=row.parameter, parameter_raw=row.parameter_raw, value=value,
                     value_raw=f"{row.value_raw} {row.unit_raw or ''}".strip(), top_m=top_m, bottom_m=bottom_m,
                     sample_name=row.sample_name, source=source, qualifier=lab_qualifier(row.value_raw),
                     value_upper=lab_upper(row.parameter, row.value_raw, row.unit_raw))


def _same_value(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 + 5e-4 * max(abs(a), abs(b))


# Physically plausible ranges in the stored units: a value outside is a misread or a unit error
# (e.g. UCS '9100' without unit is kPa, not MPa) and is not published.
LAB_RANGES: dict[LabParameter, tuple[float, float]] = {
    LabParameter.ucs: (0.05, 500), LabParameter.tensile_strength: (0.01, 100), LabParameter.point_load_index: (0.01, 25),
    LabParameter.e_modulus: (0.1, 200_000), LabParameter.cai: (0, 8), LabParameter.water_content: (0, 500),
    LabParameter.unit_weight: (8, 35), LabParameter.dry_unit_weight: (5, 32), LabParameter.fines: (0, 100),
    LabParameter.sand: (0, 100), LabParameter.gravel: (0, 100), LabParameter.liquid_limit: (0, 500),
    LabParameter.plastic_limit: (0, 300), LabParameter.plasticity_index: (0, 300), LabParameter.friction_angle: (0, 60),
    LabParameter.cohesion: (0, 5000), LabParameter.undrained_shear_strength: (0, 5000),
    LabParameter.hydraulic_conductivity: (1e-14, 1), LabParameter.poissons_ratio: (0, 0.5), LabParameter.rmr: (0, 100),
    LabParameter.gsi: (0, 100), LabParameter.q: (1e-4, 1000),
}


def plausible(parameter: LabParameter, value: float) -> bool:
    lo, hi = LAB_RANGES.get(parameter, (float("-inf"), float("inf")))
    return lo <= value <= hi


_LEADING_VALUE = re.compile(rf"^\s*[<>≤≥~]?\s*{_NUMBER}(?:\s*[-–~]\s*{_NUMBER})?\s*")


def lab_name(result: LabResult) -> tuple[str, str]:
    """(name, unit) without separators. The unit is what follows the number in value_raw ('< 1e-3 g/m3');
    some sheets repeat it in the row name ('Benzene g/m3'), so it is cut off the name."""
    key = lambda s: re.sub(r"\W+", "", s.lower().replace("³", "3").replace("²", "2"))
    unit = key(_LEADING_VALUE.sub("", result.value_raw or "", count=1))
    name = key(result.parameter_raw or "")
    return (name.removesuffix(unit) if unit else name), unit


def add_lab_result(bh: Borehole, result: LabResult) -> bool:
    """The same test is often on the log and on the lab sheet (only the sheet has the sample id): it is kept
    once. Catch-all parameters (chemistry) are not converted and are told apart by printed name and unit,
    since detection limits such as '<1' repeat down a column. Returns whether the result was added."""
    same = next((x for x in bh.lab_results if x.parameter == result.parameter and _same_value(x.value, result.value)
                 and x.top_m == result.top_m and x.bottom_m == result.bottom_m and x.qualifier == result.qualifier
                 and x.value_upper == result.value_upper
                 and (not x.sample_name or not result.sample_name or x.sample_name == result.sample_name)
                 and (x.parameter != LabParameter.other or lab_name(x) == lab_name(result))), None)
    if same is None:
        bh.lab_results.append(result)
    elif same.sample_name is None:
        same.sample_name = result.sample_name
    return same is None
