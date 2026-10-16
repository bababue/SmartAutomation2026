"""Standardised borehole data model.

Target format follows Herrenknecht's ``BoreholeDataFormat.xlsx`` (metadata, log data,
optional lab results) with DIN 4023 lithology codes for rendering. On top of that every
borehole carries provenance (source file/page) and confidence so the UI can show *why*
a value is what it is — that is the "reliable" part of the challenge metric.

Conventions:
- All lengths in metres, depths measured from ground surface along the hole, positive down.
  Feet are converted during extraction; the original string is kept in ``raw`` fields.
- Coordinates are stored as written (x/y + CRS as written) AND resolved to WGS84 lat/lon.
- Anything the source does not state stays ``None`` — never guess silently.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import math
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


# ---------------------------------------------------------------------------
# Provenance


class SourceRef(BaseModel):
    file: str = Field(description="File name relative to the dataset root")
    page: int | None = Field(None, description="1-based page number (None for single images)")
    bbox: tuple[float, float, float, float] | None = Field(
        None, description="Region on the page image (x0, y0, x1, y1) as fractions of width/height (0-1, origin "
        "top left), if known. Approximate: read by the vision model"
    )


# ---------------------------------------------------------------------------
# Location / georeferencing


class GeorefMethod(str, Enum):
    stated_coordinates = "stated_coordinates"  # coordinates printed on the log
    linked_table = "linked_table"  # coordinates from a separate summary table in the same report
    geocoded_place = "geocoded_place"  # only a place/project name → geocoder
    manual = "manual"  # set by a human reviewer


class Location(BaseModel):
    coords_raw: str | None = Field(
        None,
        description="Coordinate text exactly as written incl. labels, e.g. 'N: 31,12,345 E: 45,03,210'. "
        "Labels are ambiguous across countries (X is northing in Korea, easting elsewhere), so keep them.",
    )
    crs_raw: str | None = Field(None, description="CRS as written, e.g. '55H MGA94', 'OSGB36', 'Mt Eden 2000'")
    easting: float | None = Field(None, description="Parsed easting (or longitude for geographic CRS) in source CRS")
    northing: float | None = Field(None, description="Parsed northing (or latitude for geographic CRS) in source CRS")
    crs_epsg: int | None = Field(None, description="Resolved EPSG code of the source CRS")
    crs_proj: str | None = Field(None, description="PROJ string for local grids without an EPSG code")
    crs_candidates: list[str] = Field(
        [], description="All CRS tried (EPSG codes / PROJ strings) when the choice was ambiguous"
    )
    is_planned: bool | None = Field(None, description="True if only proposed/planned coordinates are available")
    lat: float | None = Field(None, description="WGS84 latitude")
    lon: float | None = Field(None, description="WGS84 longitude")
    method: GeorefMethod | None = None
    accuracy_m: float | None = Field(None, description="Estimated horizontal uncertainty in metres")
    place_name: str | None = Field(None, description="Place used for geocoding / plausibility check")
    country: str | None = Field(None, description="ISO 3166-1 alpha-2")
    confidence: Confidence | None = None
    source: SourceRef | None = None


# ---------------------------------------------------------------------------
# Lithology: one key per DIN 4023 hatch pattern. Keys are English; the German DIN
# terms are kept as data in DIN4023_LABEL_DE (prompting for German logs, pattern lookup).


class Lithology(str, Enum):
    # Soils
    gravel = "gravel"
    coarse_gravel = "coarse_gravel"
    medium_gravel = "medium_gravel"
    fine_gravel = "fine_gravel"
    sand = "sand"
    coarse_sand = "coarse_sand"
    medium_sand = "medium_sand"
    fine_sand = "fine_sand"
    silt = "silt"
    clay = "clay"
    peat_humus = "peat_humus"
    cobbles = "cobbles"
    boulders = "boulders"
    volcanic_ash = "volcanic_ash"
    topsoil = "topsoil"
    fill = "fill"
    # Geological soil names
    slope_loam = "slope_loam"
    scree = "scree"
    boulder_clay = "boulder_clay"
    till_marl = "till_marl"
    loess = "loess"
    loess_loam = "loess_loam"
    marsh_clay = "marsh_clay"
    meadow_lime = "meadow_lime"
    varved_clay = "varved_clay"
    gyttja = "gyttja"
    # Rocks
    lignite = "lignite"
    conglomerate = "conglomerate"
    breccia = "breccia"
    sandstone = "sandstone"
    siltstone = "siltstone"
    claystone = "claystone"
    marlstone = "marlstone"
    limestone = "limestone"
    dolomite = "dolomite"
    anhydrite = "anhydrite"
    gypsum = "gypsum"
    salt_rock = "salt_rock"
    tuff = "tuff"
    hard_coal = "hard_coal"
    quartzite = "quartzite"
    volcanic_rock = "volcanic_rock"
    plutonic_rock = "plutonic_rock"  # e.g. granite
    massive_metamorphic = "massive_metamorphic"
    foliated_metamorphic = "foliated_metamorphic"
    chalk = "chalk"
    calc_tufa = "calc_tufa"
    # Not geology (core loss, unreadable)
    no_recovery = "no_recovery"
    unknown = "unknown"


DIN4023_LABEL_DE: dict[Lithology, str] = {
    Lithology.gravel: "Kies",
    Lithology.coarse_gravel: "Grobkies",
    Lithology.medium_gravel: "Mittelkies",
    Lithology.fine_gravel: "Feinkies",
    Lithology.sand: "Sand",
    Lithology.coarse_sand: "Grobsand",
    Lithology.medium_sand: "Mittelsand",
    Lithology.fine_sand: "Feinsand",
    Lithology.silt: "Schluff",
    Lithology.clay: "Ton",
    Lithology.peat_humus: "Torf/Humus",
    Lithology.cobbles: "Steine",
    Lithology.boulders: "Blöcke",
    Lithology.volcanic_ash: "Vulkanische Aschen",
    Lithology.topsoil: "Mutterboden",
    Lithology.fill: "Auffüllung",
    Lithology.slope_loam: "Hanglehm",
    Lithology.scree: "Hangschutt",
    Lithology.boulder_clay: "Geschiebelehm",
    Lithology.till_marl: "Geschiebemergel",
    Lithology.loess: "Löss",
    Lithology.loess_loam: "Lösslehm",
    Lithology.marsh_clay: "Klei, Schlick",
    Lithology.meadow_lime: "Wiesenkalk",
    Lithology.varved_clay: "Bänderton",
    Lithology.gyttja: "Mudde",
    Lithology.lignite: "Braunkohle",
    Lithology.conglomerate: "Konglomerat",
    Lithology.breccia: "Brekzie",
    Lithology.sandstone: "Sandstein",
    Lithology.siltstone: "Schluffstein",
    Lithology.claystone: "Tonstein",
    Lithology.marlstone: "Mergelstein",
    Lithology.limestone: "Kalkstein",
    Lithology.dolomite: "Dolomitstein",
    Lithology.anhydrite: "Anhydrit",
    Lithology.gypsum: "Gips",
    Lithology.salt_rock: "Salzgestein",
    Lithology.tuff: "Tuffstein",
    Lithology.hard_coal: "Steinkohle",
    Lithology.quartzite: "Quarzit",
    Lithology.volcanic_rock: "Vulkanite",
    Lithology.plutonic_rock: "Plutonite",
    Lithology.massive_metamorphic: "massige Metamorphite",
    Lithology.foliated_metamorphic: "blättrige Metamorphite",
    Lithology.chalk: "Kreidestein",
    Lithology.calc_tufa: "Kalktuff",
    Lithology.no_recovery: "Kernverlust",
}


class Qualifier(str, Enum):
    """Proportion of a secondary component, following DIN EN ISO 14688 terminology."""

    trace = "trace"  # "trace of"
    slightly = "slightly"  # "slightly sandy", "some sand"
    moderately = "moderately"  # "sandy"
    very = "very"  # "very sandy"


class SecondaryComponent(BaseModel):
    lithology: Lithology
    qualifier: Qualifier | None = None


# ---------------------------------------------------------------------------
# Log data


class Layer(BaseModel):
    top_m: float
    bottom_m: float
    description_original: str = Field(description="Description verbatim, original language")
    language: str | None = Field(None, description="ISO 639-1 code of the original description")
    description_en: str | None = Field(None, description="English translation of the description")
    primary: Lithology = Field(description="Main constituent, drives the DIN 4023 pattern")
    secondary: list[SecondaryComponent] = []
    uscs: str | None = Field(None, description="Soil classification symbol as stated (USCS/AS/BS), e.g. 'CL', 'SM'")
    stratigraphic_name: str | None = Field(None, description="Formation / unit name, e.g. 'Ortenau Formation'")
    stratigraphic_code: str | None = Field(None, description="Formation abbreviation as on the log, e.g. 'qORT', 'qhy'")
    stratigraphic_color: str | None = Field(None, description="Colour used for the unit on the log, hex if determinable")
    engineering_unit: str | None = Field(None, description="EngineeringSoilUnit / geotechnical unit")
    geologic_age: str | None = Field(None, description="Age as written, e.g. 'Quaternary', 'Lower Miocene'")
    color: str | None = Field(None, description="Material colour as described, e.g. 'greyish brown'")
    pattern_raw: str | None = Field(None, description="Graphic log symbol as shown on the source, if not DIN 4023")
    consistency: str | None = Field(None, description="Cohesive soils, e.g. 'stiff', 'weich'")
    relative_density: str | None = Field(None, description="Granular soils, e.g. 'medium dense'")
    weathering: str | None = Field(None, description="Rock weathering grade, e.g. 'slightly weathered', 'W2'")
    rock_strength: str | None = None
    moisture: str | None = None
    confidence: Confidence | None = None
    source: SourceRef | None = None


class CoreRun(BaseModel):
    top_m: float
    bottom_m: float
    core: bool | None = None
    total_core_recovery_pct: float | None = Field(None, description="TCR [%]")
    solid_core_recovery_pct: float | None = Field(None, description="SCR [%]")
    rqd_pct: float | None = Field(None, description="RQD [%]")
    fracture_frequency_per_m: float | None = None
    source: SourceRef | None = None


class SampleType(str, Enum):
    disturbed = "disturbed"  # D, G, bag
    undisturbed = "undisturbed"  # U, UD, Shelby, piston
    split_spoon = "split_spoon"  # SS / SPT sampler
    core = "core"
    water = "water"
    environmental = "environmental"  # ES
    other = "other"


class Sample(BaseModel):
    name: str | None = Field(None, description="SampleName as written, e.g. 'S1', 'UD4'")
    top_m: float = Field(description="SampleDepth (top)")
    bottom_m: float | None = None
    type: SampleType | None = None
    type_raw: str | None = None
    source: SourceRef | None = None


class SPT(BaseModel):
    depth_m: float = Field(description="Test depth (top of seating drive)")
    blows: list[int] = Field([], description="Blow counts per increment as written, e.g. [4, 5, 5]")
    increment_mm: int | None = Field(None, description="Length of one increment, usually 150 (sometimes 75)")
    n_value: int | None = Field(None, description="N as stated; else blows over the last 300 mm")
    refusal: bool = Field(False, description="True for 'R', '50/10', 'N=R' etc.")
    raw: str | None = Field(None, description="Exactly as written, e.g. '50/10', 'N=R', '6,8,10 = 18'")
    source: SourceRef | None = None


class GroundwaterObservation(BaseModel):
    depth_m: float | None = Field(None, description="Groundwaterlevel below ground surface")
    kind: Literal["struck", "standing", "stable", "unknown"] = "unknown"
    observed_on: date | None = None
    dry: bool | None = Field(None, description="True if the log states 'no water' / 'NO'")
    reference: Literal["ground", "casing", "other"] | None = Field(
        None, description="Reference point of depth_m, if stated (ground surface, top of casing, other)"
    )
    raw: str | None = None
    source: SourceRef | None = None


class LabParameter(str, Enum):
    """Test results on samples — from BoreholeDataFormat.xlsx, target unit in the comment."""

    water_content = "water_content"  # %
    consistency = "consistency"  # %
    unit_weight = "unit_weight"  # kN/m³
    dry_unit_weight = "dry_unit_weight"  # kN/m³
    ucs = "ucs"  # MPa
    tensile_strength = "tensile_strength"  # MPa (ITS / Brazilian)
    point_load_index = "point_load_index"  # MPa (Is50)
    cai = "cai"  # Cerchar abrasivity index [-]
    rmr = "rmr"  # [-]
    q = "q"  # [-]
    gsi = "gsi"  # [-]
    friction_angle = "friction_angle"  # °
    cohesion = "cohesion"  # kPa
    undrained_shear_strength = "undrained_shear_strength"  # kPa
    fines = "fines"  # % passing 0.063/0.075 mm
    sand = "sand"  # %
    gravel = "gravel"  # %
    liquid_limit = "liquid_limit"  # %
    plastic_limit = "plastic_limit"  # %
    plasticity_index = "plasticity_index"  # [-]
    consistency_index = "consistency_index"  # [-]
    hydraulic_conductivity = "hydraulic_conductivity"  # m/s
    e_modulus = "e_modulus"  # MPa
    poissons_ratio = "poissons_ratio"  # [-]
    other = "other"


LAB_UNITS: dict[LabParameter, str] = {
    LabParameter.water_content: "%",
    LabParameter.consistency: "%",
    LabParameter.unit_weight: "kN/m³",
    LabParameter.dry_unit_weight: "kN/m³",
    LabParameter.ucs: "MPa",
    LabParameter.tensile_strength: "MPa",
    LabParameter.point_load_index: "MPa",
    LabParameter.cai: "-",
    LabParameter.rmr: "-",
    LabParameter.q: "-",
    LabParameter.gsi: "-",
    LabParameter.friction_angle: "°",
    LabParameter.cohesion: "kPa",
    LabParameter.undrained_shear_strength: "kPa",
    LabParameter.fines: "%",
    LabParameter.sand: "%",
    LabParameter.gravel: "%",
    LabParameter.liquid_limit: "%",
    LabParameter.plastic_limit: "%",
    LabParameter.plasticity_index: "-",
    LabParameter.consistency_index: "-",
    LabParameter.hydraulic_conductivity: "m/s",
    LabParameter.e_modulus: "MPa",
    LabParameter.poissons_ratio: "-",
}


class LabResult(BaseModel):
    parameter: LabParameter
    parameter_raw: str | None = Field(None, description="Column header as written, e.g. 'Cu (kg/cm2)'")
    value: float = Field(description="Converted to the target unit in LAB_UNITS")
    value_raw: str | None = Field(None, description="Value + unit as written, e.g. '0,35 kg/cm2'")
    top_m: float | None = None
    bottom_m: float | None = None
    sample_name: str | None = None
    source: SourceRef | None = None


# ---------------------------------------------------------------------------
# Borehole


class ReviewStatus(str, Enum):
    auto_approved = "auto_approved"  # score above the threshold, no blocking findings
    pending_review = "pending_review"  # waits for a human decision
    approved = "approved"  # released by a human
    rejected = "rejected"  # rejected by a human


class InvestigationType(str, Enum):
    borehole = "borehole"
    test_pit = "test_pit"  # trial pits are logged in the same reports
    other = "other"


class Borehole(BaseModel):
    # --- Metadata (BoreholeDataFormat.xlsx) ---
    borehole_id: str = Field(description="Stable internal ID, e.g. '<file-stem>:<borehole_name>'")
    borehole_name: str = Field(description="Name as written on the log, e.g. 'BH-1', 'BH-07'")
    investigation_type: InvestigationType | None = None
    location_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    client: str | None = None
    contractor: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    location: Location = Location()
    surface_elevation_m: float | None = Field(
        None, description="Ground level [m above datum], positive up; may be negative (e.g. Caspian coast)"
    )
    surface_elevation_raw: str | None = Field(
        None, description="As written; some reports use inverted signs ('negative = above datum')"
    )
    elevation_datum: str | None = Field(None, description="As written, e.g. 'mASL', 'AHD', 'OD', 'DVR90', 'RL'")
    depth_m: float | None = Field(None, description="BoreholeDepth: final vertical depth")
    length_m: float | None = Field(None, description="BoreholeLength: length along the hole")
    inclination_deg: float | None = Field(None, description="From horizontal; 90 = vertical")
    drilling_method: str | None = None
    notes: str | None = None

    # --- Log data ---
    layers: list[Layer] = []
    core_runs: list[CoreRun] = []
    samples: list[Sample] = []
    spt: list[SPT] = []
    groundwater: list[GroundwaterObservation] = []
    lab_results: list[LabResult] = []

    # --- Provenance & quality ---
    sources: list[SourceRef] = Field([], description="All pages this borehole was assembled from")
    confidence: Confidence | None = Field(None, description="Extraction confidence (layers, values)")
    issues: list[str] = Field([], description="Validation findings, filled by validate_log()")
    score: Confidence | None = Field(None, description="Release score: the weaker of extraction and position")
    score_reasons: list[str] = Field([], description="Why the score is what it is / why review is needed")
    review_status: ReviewStatus = ReviewStatus.pending_review
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None

    def validate_log(self, tol: float = 0.02) -> list[str]:
        """Plausibility checks; returns findings instead of raising so data is still stored.

        Run explicitly after normalisation and georeferencing. Layers are checked in depth
        order without reordering the extracted list (source order stays visible).
        """
        issues: list[str] = []
        layers = sorted(self.layers, key=lambda layer: layer.top_m)
        for layer in layers:
            if not (math.isfinite(layer.top_m) and math.isfinite(layer.bottom_m)):
                issues.append(f"layer {layer.top_m}–{layer.bottom_m}: non-finite depth")
            elif layer.top_m < 0 or layer.bottom_m <= layer.top_m:
                issues.append(f"layer {layer.top_m}–{layer.bottom_m}: invalid interval")
        if not layers:
            issues.append("no layers extracted")
        else:
            covered = layers[0].bottom_m
            for layer in layers[1:]:
                if layer.top_m - covered > tol:
                    issues.append(f"gap {covered}–{layer.top_m} m")
                elif covered - layer.top_m > tol:
                    issues.append(f"overlap {layer.top_m}–{min(covered, layer.bottom_m)} m")
                covered = max(covered, layer.bottom_m)
            if layers[0].top_m > tol:
                issues.append(f"log starts at {layers[0].top_m} m, not at surface")
            # Layer depths are measured along the hole, so compare with the length if known.
            final = self.length_m if self.length_m is not None else self.depth_m
            if final is not None and abs(covered - final) > 0.1:
                issues.append(f"layers end at {covered} m, stated final depth/length {final} m")
        for run in self.core_runs:
            interval = f"core run {run.top_m:g}–{run.bottom_m:g} m"
            values = {"TCR": run.total_core_recovery_pct, "SCR": run.solid_core_recovery_pct, "RQD": run.rqd_pct}
            for name, value in values.items():
                if value is not None and not 0 <= value <= 100:
                    issues.append(f"{interval}: {name} {value:g}% outside 0–100%")
            tcr = run.total_core_recovery_pct
            for name in ("SCR", "RQD"):
                if tcr is not None and values[name] is not None and values[name] > tcr + 0.5:
                    issues.append(f"{interval}: {name} {values[name]:g}% above TCR {tcr:g}%")
        # Tests and samples cannot lie below the end of the hole.
        end = self.length_m if self.length_m is not None else self.depth_m
        if end is None and layers:
            end = max(layer.bottom_m for layer in layers)
        if end is not None:
            deepest = [(f"sample {s.name or ''}".rstrip(), s.bottom_m if s.bottom_m is not None else s.top_m)
                       for s in self.samples] + [("SPT", t.depth_m) for t in self.spt]
            for what, depth in deepest:
                if depth - end > 0.1:
                    issues.append(f"{what} at {depth:g} m below final depth {end:g} m")
        for gw in self.groundwater:
            if gw.dry and gw.depth_m is not None:
                issues.append(f"groundwater marked dry but has level {gw.depth_m} m")
        loc = self.location
        if loc.lat is None or loc.lon is None:
            issues.append("not georeferenced")
        elif not (-90 <= loc.lat <= 90 and -180 <= loc.lon <= 180):
            issues.append(f"invalid WGS84 position {loc.lat}, {loc.lon}")
        elif len(loc.crs_candidates) > 1 and loc.crs_epsg is None and loc.crs_proj is None:
            issues.append("CRS ambiguous")
        self.issues = issues
        return issues


class SourceDocument(BaseModel):
    """One input file and what was found in it."""

    file: str
    kind: Literal["pdf_text", "pdf_scan", "pdf_vector", "image", "docx"]
    page_count: int | None = None
    log_pages: list[int] = Field([], description="Pages classified as borehole logs")
    coordinate_table_pages: list[int] = Field([], description="Pages with borehole coordinate summaries")
    boreholes: list[str] = Field([], description="borehole_id values extracted from this file")
