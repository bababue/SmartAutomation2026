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

from pydantic import BaseModel, ConfigDict, Field

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

# Layer depths closer than this are continuous. A gap or overlap up to MINOR_DISCONTINUITY_M is core loss
# between runs or rounding of piece depths in core logs: shown, but not treated as a misread.
CONTINUITY_TOLERANCE_M = 0.02
MINOR_DISCONTINUITY_M = 0.10


class _Model(BaseModel):
    # A non-finite number serialises to JSON null, which a required field then refuses on every read.
    model_config = ConfigDict(allow_inf_nan=False)


class SourceRef(_Model):
    file: str = Field(description="File name relative to the dataset root")
    page: int | None = Field(None, description="1-based page number (None for single images)")
    bbox: tuple[float, float, float, float] | None = Field(
        None, description="Region on the page image (x0, y0, x1, y1) as fractions of width/height (0-1, origin "
        "top left), if known. Approximate: read by the vision model"
    )


class GeorefMethod(str, Enum):
    stated_coordinates = "stated_coordinates"  # coordinates printed on the log
    linked_table = "linked_table"  # coordinates from a separate summary table in the same report
    geocoded_place = "geocoded_place"  # only a place/project name → geocoder
    manual = "manual"  # set by a human reviewer


class Location(_Model):
    coords_raw: str | None = Field(
        None,
        description="Coordinate text exactly as written incl. labels, e.g. 'N: 31,12,345 E: 45,03,210'. "
        "Labels are ambiguous across countries (X is northing in Korea, easting elsewhere), so keep them.",
    )
    crs_raw: str | None = Field(None, description="CRS as written, e.g. '55H MGA94', 'OSGB36', 'Mt Eden 2000'")
    easting: float | None = Field(None, description="Parsed easting (or longitude for geographic CRS) in source CRS")
    northing: float | None = Field(None, description="Parsed northing (or latitude for geographic CRS) in source CRS")
    crs_epsg: int | None = Field(None, description="Resolved EPSG code of the source CRS")
    crs_proj: str | None = Field(
        None, description="CRS without an EPSG code: another authority's code (e.g. 'ESRI:102138') or a "
        "description of a local grid fitted to reviewed positions"
    )
    crs_candidates: list[str] = Field(
        [], description="All CRS tried (authority codes such as 'EPSG:32632', or a notation name) when the choice "
        "was ambiguous"
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


class SecondaryComponent(_Model):
    lithology: Lithology
    qualifier: Qualifier | None = None


class Layer(_Model):
    top_m: float
    bottom_m: float
    description_original: str = Field(description="Description verbatim, original language")
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
    consistency: str | None = Field(None, description="Cohesive soils, e.g. 'stiff', 'weich'")
    relative_density: str | None = Field(None, description="Granular soils, e.g. 'medium dense'")
    weathering: str | None = Field(None, description="Rock weathering grade, e.g. 'slightly weathered', 'W2'")
    rock_strength: str | None = None
    moisture: str | None = None
    source: SourceRef | None = None


class CoreRun(_Model):
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


class Sample(_Model):
    name: str | None = Field(None, description="SampleName as written, e.g. 'S1', 'UD4'")
    top_m: float = Field(description="SampleDepth (top)")
    bottom_m: float | None = None
    type: SampleType | None = None
    type_raw: str | None = None
    source: SourceRef | None = None


class SPT(_Model):
    depth_m: float = Field(description="Test depth (top of seating drive)")
    blows: list[int] = Field([], description="Blow counts per increment as written, e.g. [4, 5, 5]")
    increment_mm: int | None = Field(None, description="Length of one increment, usually 150 (sometimes 75)")
    n_value: int | None = Field(None, description="N as stated; else blows over the last 300 mm")
    refusal: bool = Field(False, description="True for 'R', '50/10', 'N=R' etc.")
    raw: str | None = Field(None, description="Exactly as written, e.g. '50/10', 'N=R', '6,8,10 = 18'")
    source: SourceRef | None = None


class GroundwaterObservation(_Model):
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


class LabResult(_Model):
    parameter: LabParameter
    parameter_raw: str | None = Field(None, description="Column header as written, e.g. 'Cu (kg/cm2)'")
    value: float = Field(description="Converted to the target unit in LAB_UNITS")
    value_upper: float | None = Field(None, description="Upper end of a range, in the same target unit as value")
    value_raw: str | None = Field(None, description="Value + unit as written, e.g. '0,35 kg/cm2'")
    qualifier: str | None = Field(None, description="'<', '>', '≤', '≥' or 'range' when the printed value is not exact; "
                                  "value is then the bound or the lower end")
    top_m: float | None = None
    bottom_m: float | None = None
    sample_name: str | None = None
    source: SourceRef | None = None


class ReviewStatus(str, Enum):
    auto_approved = "auto_approved"  # score above the threshold, no blocking findings
    pending_review = "pending_review"  # waits for a human decision
    approved = "approved"  # released by a human
    rejected = "rejected"  # rejected by a human


class ScoreFactor(_Model):
    """One influence on the release score: which part it concerns and whether it raised or lowered trust."""
    part: Literal["extraction", "position", "review"]
    effect: Literal["positive", "negative", "neutral"]
    text: str
    todo: str | None = Field(None, description="What a reviewer can do about a negative influence")


class InvestigationType(str, Enum):
    borehole = "borehole"
    test_pit = "test_pit"  # trial pits are logged in the same reports
    other = "other"


class Borehole(_Model):
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
    layers: list[Layer] = []
    core_runs: list[CoreRun] = []
    samples: list[Sample] = []
    spt: list[SPT] = []
    groundwater: list[GroundwaterObservation] = []
    lab_results: list[LabResult] = []
    sources: list[SourceRef] = Field([], description="All pages this borehole was assembled from")
    field_sources: dict[str, SourceRef] = Field(
        {}, description="Header fields filled from another document of the same hole than the layer log, with "
        "the first page of that document's record"
    )
    confidence: Confidence | None = Field(None, description="Extraction confidence (layers, values)")
    issues: list[str] = Field([], description="Findings of validate_log() and of the pipeline steps")
    score: Confidence | None = Field(None, description="Release score: the weaker of extraction and position")
    score_factors: list[ScoreFactor] = Field([], description="What raised and lowered the score, and why review is needed")
    review_status: ReviewStatus = ReviewStatus.pending_review
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None

    def validate_log(self) -> list[str]:
        """Plausibility findings, returned instead of raised so the data is still stored. Layers are checked in
        depth order without reordering the extracted list (source order stays visible)."""
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
                if layer.top_m - covered > CONTINUITY_TOLERANCE_M:
                    kind = "minor discontinuity" if layer.top_m - covered <= MINOR_DISCONTINUITY_M else "gap"
                    issues.append(f"{kind} {covered}–{layer.top_m} m" + (" (not covered)" if kind != "gap" else ""))
                elif covered - layer.top_m > CONTINUITY_TOLERANCE_M:
                    end = min(covered, layer.bottom_m)
                    kind = "minor discontinuity" if end - layer.top_m <= MINOR_DISCONTINUITY_M else "overlap"
                    issues.append(f"{kind} {layer.top_m}–{end} m" + (" (covered twice)" if kind != "overlap" else ""))
                covered = max(covered, layer.bottom_m)
            if layers[0].top_m > CONTINUITY_TOLERANCE_M:
                issues.append(f"log starts at {layers[0].top_m} m, not at surface")
            # Layer depths are measured along the hole, so compare with the length if known.
            final = self.length_m if self.length_m is not None else self.depth_m
            if final is not None and abs(covered - final) > 0.1:
                issues.append(f"layers end at {covered} m, stated final depth/length {final} m")
        for run in self.core_runs:
            interval = f"core run {run.top_m:g}–{run.bottom_m:g} m"
            values = {"TCR": run.total_core_recovery_pct, "SCR": run.solid_core_recovery_pct, "RQD": run.rqd_pct}
            for name, value in values.items():
                # TCR above 100 % is core gain (swelling, depth errors of the run); plausible up to ~120 %.
                upper = 120 if name == "TCR" else 100
                if value is not None and not 0 <= value <= upper:
                    issues.append(f"{interval}: {name} {value:g}% outside 0–{upper}%")
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
        return issues
