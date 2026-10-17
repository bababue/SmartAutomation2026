"""Schemas the LLM fills. Deliberately slimmer than the canonical `schema.borehole` model:
the model reports what it *sees* (raw strings, parsed numbers, its reading of the page);
IDs, CRS resolution, WGS84 positions, confidence and review state are set by our code.

Every field is required-but-nullable, so the model decides explicitly between a value and null. Classification
runs as strict structured output; extraction is too large for that and is validated here after the answer.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from schema.borehole import LabParameter, Lithology, Qualifier, SampleType


class PageKind(str, Enum):
    borehole_log = "borehole_log"
    test_pit_log = "test_pit_log"
    probe_log = "probe_log"  # dynamic probe / Mackintosh / CPT records
    coordinate_table = "coordinate_table"
    lab_results = "lab_results"
    legend = "legend"  # key sheets explaining symbols and abbreviations
    site_plan = "site_plan"
    text = "text"  # report prose, cover pages, tables of contents
    photo = "photo"
    other = "other"


class LogRef(BaseModel):
    name: str = Field(description="Borehole / pit name exactly as printed, e.g. 'BH-1', 'BH-07', 'S-1'")
    sheet: str | None = Field(description="Sheet indicator as printed, e.g. '2 of 6', 'Hoja 1 de 1'")


class PageClassification(BaseModel):
    kind: PageKind
    logs: list[LogRef] = Field(description="Boreholes/pits whose log or coordinates appear on this page")
    language: str | None = Field(description="ISO 639-1 code of the main language of the page")
    has_coordinates: bool = Field(description="True if coordinates or a coordinate table are visible")
    crs_text: str | None = Field(
        description="Statement of the coordinate system, grid, datum or zone exactly as printed on this page "
        "(e.g. 'Coordinates: MGA94 Zone 55'); null if none"
    )


class XSecondary(BaseModel):
    lithology: Lithology
    qualifier: Qualifier | None


class XLayer(BaseModel):
    top_m: float | None = Field(description="Top depth below ground in metres (convert feet)")
    bottom_m: float | None = Field(description="Bottom depth below ground in metres (convert feet)")
    description_original: str = Field(description="Full layer description verbatim, original language")
    description_en: str | None = Field(description="English translation; null if already English")
    primary: Lithology = Field(description="Main constituent mapped to the closest DIN 4023 class")
    secondary: list[XSecondary] = Field(description="Secondary components, e.g. 'slightly sandy' → sand/slightly")
    uscs: str | None = Field(description="Classification symbol as printed (USCS/AS/BS/other), e.g. 'CL'")
    stratigraphic_name: str | None = Field(description="Formation/unit name as printed")
    stratigraphic_code: str | None = Field(description="Formation abbreviation as printed, e.g. 'qORT'")
    stratigraphic_color: str | None = Field(description="Colour of the stratigraphy column on the log, as hex")
    geologic_age: str | None = Field(description="Age as printed, or obvious from the stratigraphy, e.g. 'Quaternary'")
    color: str | None = Field(description="Material colour as described, in English")
    consistency: str | None
    relative_density: str | None
    weathering: str | None
    rock_strength: str | None
    moisture: str | None
    bbox: list[float] | None = Field(
        description="Where this is printed on the page image: [x0, y0, x1, y1] as fractions of image width and "
        "height (0-1, origin top left). Approximate is fine; null if unsure"
    )


class XSPT(BaseModel):
    depth_m: float | None
    blows: list[int] = Field(description="Blow counts per increment as printed")
    increment_mm: int | None = Field(description="Increment length if stated (usually 150, sometimes 75)")
    n_value: int | None = Field(description="N as printed")
    refusal: bool | None = Field(description="True for refusal notations like 'R', '50/10', 'N=R'")
    raw: str = Field(description="Exactly as printed")


class XCoreRun(BaseModel):
    top_m: float | None
    bottom_m: float | None
    tcr_pct: float | None
    scr_pct: float | None
    rqd_pct: float | None
    fracture_frequency_per_m: float | None


class XSample(BaseModel):
    name: str | None
    top_m: float | None
    bottom_m: float | None
    type: SampleType | None
    type_raw: str | None


class XGroundwater(BaseModel):
    depth_m: float | None = Field(description="Water level below ground in metres")
    kind: Literal["struck", "standing", "stable", "unknown"]
    reference: Literal["ground", "casing", "other"] | None = Field(
        description="What the depth is measured from, if stated (below ground, below top of casing, ...)"
    )
    date: str | None = Field(description="ISO date if readable")
    dry: bool | None = Field(description="True if the log explicitly states no water / dry")
    raw: str


class XLab(BaseModel):
    parameter: LabParameter
    parameter_raw: str = Field(description="Column header or label as printed")
    value_raw: str = Field(description="Value as printed incl. qualifiers like '<'")
    unit_raw: str | None = Field(description="Unit as printed, e.g. 'kg/cm2', 'kPa', '%'")
    top_m: float | None
    bottom_m: float | None
    sample_name: str | None


class XLog(BaseModel):
    """One borehole / test pit as visible on this page (possibly only one sheet of it)."""

    name: str = Field(description="Borehole / pit name exactly as printed")
    sheet: str | None = Field(description="Sheet indicator, e.g. '2 of 6'")
    investigation_type: Literal["borehole", "test_pit", "probe", "other"] | None
    project_name: str | None
    project_id: str | None
    client: str | None
    contractor: str | None
    location_text: str | None = Field(description="Site / location as printed (street, park, town)")
    country: str | None = Field(description="ISO 3166-1 alpha-2, from the page or obvious context; null if unclear")
    place_lat: float | None = Field(
        description="Your best estimate of the latitude of the named site/town (not the printed coordinates)"
    )
    place_lon: float | None = Field(description="Your best estimate of the longitude of the named site/town")
    place_precision_km: float | None = Field(description="How precise your place estimate is, in km")
    start_date: str | None = Field(description="ISO date (YYYY-MM-DD) if unambiguous")
    end_date: str | None
    date_raw: str | None
    coords_raw: str | None = Field(
        description="Coordinate text exactly as printed incl. labels, e.g. 'N: 31,12,345 E: 45,03,210'"
    )
    crs_raw: str | None = Field(description="Coordinate system / datum text as printed, e.g. '55H MGA94'")
    coords_bbox: list[float] | None = Field(
        description="Where this is printed on the page image: [x0, y0, x1, y1] as fractions of image width and "
        "height (0-1, origin top left). Approximate is fine; null if unsure"
    )
    elevation_raw: str | None = Field(description="Ground level / collar elevation exactly as printed")
    elevation_m: float | None
    elevation_datum: str | None
    final_depth_m: float | None = Field(description="Final depth of the hole in metres")
    length_m: float | None = Field(description="Length along the hole if stated separately (inclined holes)")
    inclination_deg: float | None = Field(description="From horizontal, 90 = vertical")
    drilling_method: str | None
    depth_unit_raw: str | None = Field(description="Depth unit as printed, e.g. 'm', 'ft'")
    layers: list[XLayer]
    spt: list[XSPT]
    core_runs: list[XCoreRun]
    samples: list[XSample]
    groundwater: list[XGroundwater]
    lab_results: list[XLab]
    notes: str | None = Field(description="Remarks relevant for interpretation (e.g. 'continued', datum notes)")
    unreadable: list[str] = Field(description="What you could not read reliably on this page")


class XCoordinateRow(BaseModel):
    name: str
    coords_raw: str = Field(description="Coordinate values incl. column headers, e.g. 'Easting 412345.000 Northing 398765.000'")
    crs_raw: str | None
    elevation_raw: str | None
    is_planned: bool | None = Field(description="True if the row lists proposed rather than actual coordinates")


class PageExtraction(BaseModel):
    logs: list[XLog]
    coordinate_rows: list[XCoordinateRow] = Field(description="Rows of coordinate summary tables on this page")


class XLabRow(BaseModel):
    """One test result on a lab sheet (several parameters of one sample are several rows)."""

    hole_name: str | None = Field(description="Borehole / pit name as printed for this row or in the sheet header")
    sample_name: str | None = Field(description="Sample id as printed, e.g. 'U3', 'S-12', 'C4'")
    top_m: float | None = Field(description="Sample top depth in metres (convert feet)")
    bottom_m: float | None = Field(description="Sample bottom depth in metres; null if only one depth is given")
    parameter: LabParameter
    parameter_raw: str = Field(description="Column header or test name as printed, incl. the unit")
    value_raw: str = Field(description="Value exactly as printed incl. qualifiers like '<' or ranges")
    unit_raw: str | None = Field(description="Unit as printed, e.g. 'MPa', 'kg/cm2', '%', 'cm/s'")


class PageLabResults(BaseModel):
    rows: list[XLabRow]
    unreadable: list[str] = Field(description="What you could not read reliably on this page")


class CoordinateReading(BaseModel):
    coords_raw: str | None = Field(description="Coordinate text exactly as printed, incl. axis labels; null if none")
    crs_raw: str | None = Field(description="Coordinate system / grid / datum / zone text as printed; null if none")
