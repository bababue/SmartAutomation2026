"""Release score and human-in-the-loop routing.

score = min(extraction confidence, position confidence), so a record is only as trustworthy as
its weakest part. Records at or above the threshold without blocking findings are released
automatically; everything else waits for a human in the review queue.

The extraction confidence is not the model's self-assessment. It is derived from checks we can
compute: plausibility of the log, what the model reported as unreadable, and – for PDFs with a
text layer – how many extracted layer depths actually appear in that text.
"""

from __future__ import annotations

import re

from schema.borehole import Borehole, GeorefMethod, ReviewStatus

from . import config

# Findings that always need a human, whatever the score: anything that suggests a misread.
BLOCKING = ("no layers extracted", "log not extracted", "CRS ambiguous", "invalid interval", "non-finite depth",
            "invalid WGS84", "gap", "overlap", "layers end at", "stale correction", "log starts at", "depths printed in feet",
            "missing sheets", "sheet header differs", "same printed coordinates as", "same hole assumed",
            "sources disagree on")

# Findings that name files, holes and header texts of other documents: their own severity holds, whatever
# those names contain ('Singapore' contains 'gap').
QUOTING = ("combined with the record from", "possibly the same hole as", "sources differ on")


def is_blocking(issue: str) -> bool:
    return not issue.startswith(QUOTING) and any(b in issue for b in BLOCKING)


# Header values compared between the documents of one hole: (finding, name, unit). A disagreement on the depths
# or the ground level blocks the release; one on the dates or the inclination needs a look.
SOURCE_CONFLICTS = {"depth_m": ("sources disagree on", "final depth", " m"),
                    "length_m": ("sources disagree on", "hole length", " m"),
                    "surface_elevation_m": ("sources disagree on", "ground level", " m"),
                    "start_date": ("sources differ on", "drilling start date", ""),
                    "end_date": ("sources differ on", "drilling end date", ""),
                    "inclination_deg": ("sources differ on", "inclination", "°")}

# Georeferencing doubts that explain a position confidence below the release threshold.
GEOREF_DOUBTS = ("coordinate system inferred", "coordinate axes inferred", "coordinate notation or datum ambiguous",
                 "coordinate system from reviewer knowledge", "position from a local grid")

STRUCTURAL = ("gap", "overlap", "invalid interval", "layers end at", "log starts at", "groundwater marked dry",
              "boundary inferred")


def _variants(v: float) -> set[str]:
    out = {f"{v:.2f}", f"{v:.1f}", f"{v:g}"}
    out |= {x.replace(".", ",") for x in out}
    return out


# Core logs describe pieces by offsets within the run ('0 - 21 cm, FLINT ...'); their absolute depths are
# computed, never printed, so the text layer cannot confirm them.
RELATIVE_PIECE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*[-–]\s*\d+(?:[.,]\d+)?\s*cm\b", re.I)


def text_check(bh: Borehole, page_texts: dict[tuple[str, int | None], str]) -> tuple[int, int, int, int]:
    """(boundaries found, boundaries checked, layers checked, layers checkable) against the text layer."""
    found = total = checked = checkable = 0
    for layer in bh.layers:
        if RELATIVE_PIECE.match(layer.description_original):
            continue
        checkable += 1
        src = layer.source
        text = page_texts.get((src.file, src.page)) if src else None
        if not text or len(text) < 200:
            continue
        checked += 1
        numbers = set(re.findall(r"\d+(?:[.,]\d+)?", text))
        for boundary in (layer.top_m, layer.bottom_m):
            if boundary == 0:
                continue
            total += 1
            found += bool(_variants(boundary) & numbers)
    return found, total, checked, checkable


def text_agreement(bh: Borehole, page_texts: dict[tuple[str, int | None], str]) -> float | None:
    """Share of extracted layer boundaries that occur literally in the source page's text layer.
    None when the source has no usable text layer (scans, images, outlined vector PDFs)."""
    found, total, _, _ = text_check(bh, page_texts)
    return found / total if total >= 3 else None


# The model names the field of each unreadable item ('header: ...', 'tests: ...'). Header fields and test values
# do not decide the release; an item without a field name counts, and so does anything about the position or the
# ground level, whatever field it is filed under.
MINOR_UNREADABLE = re.compile(r"^(?:unreadable:\s*)?(?:header|tests)\s*:(?!.*\b(?:coordinat\w*|crs|datum|grid|zone|"
                              r"easting|northing|latitude|longitude|elevation|level|epsg|utm)\b)", re.I)


def counts_against(unreadable: str) -> bool:
    return not MINOR_UNREADABLE.match(unreadable)


# Findings that need a look before release, without blocking: they cap the extraction confidence.
NEEDS_LOOK = ("boundary inferred", "core loss inferred", "lithology of layer", "sources differ on",
              "possibly the same hole as")
NEEDS_LOOK_RE = re.compile(r"^core run .*: (?:(?:SCR|RQD) [\d.]+% above TCR|\w+ [\d.]+% outside)")


# Below this share of matching boundaries the text layer simply does not print them.
NOT_PRINTED = 0.3

# Without positive evidence the extraction stays below the release threshold.
UNCONFIRMED_CAP = 0.85


def covers_full_depth(bh: Borehole, tol: float = 0.05) -> bool:
    """The layers run from the surface to a stated final depth (or length) without a gap or overlap."""
    final = bh.depth_m or bh.length_m
    if not bh.layers or final is None:
        return False
    layers = sorted(bh.layers, key=lambda layer: layer.top_m)
    if layers[0].top_m > tol or abs(max(layer.bottom_m for layer in layers) - final) > tol:
        return False
    return all(abs(a.bottom_m - b.top_m) <= tol for a, b in zip(layers, layers[1:]))


INTERVAL = re.compile(r"\d+(?:[.,]\d+)?\s*(?:m\s*)?[-–~]\s*\d+(?:[.,]\d+)?\s*m?\b")


def prints_depths(bh: Borehole, page_texts: dict[tuple[str, int | None], str]) -> bool:
    """The text layer holds about as many depth-like decimals (within the hole) as there are boundaries:
    then a mismatch is a contradiction, not a log that only draws its boundaries."""
    pages = {(layer.source.file, layer.source.page) for layer in bh.layers if layer.source}
    deepest = max((layer.bottom_m for layer in bh.layers), default=0) + 1
    # Intervals ('1.15-1.31m: joint', 'UCS 2.10-2.22m') describe defects and tests, not layer boundaries.
    texts = [INTERVAL.sub(" ", page_texts.get(key, "")) for key in pages]
    numbers = [float(n.replace(",", ".")) for text in texts
               for n in re.findall(r"(?<![\d.,])\d+[.,]\d{1,2}(?![\d.,])", text)]
    depth_like = sum(1 for v in numbers if 0 < v <= deepest)
    return depth_like >= 0.5 * max(1, len(bh.layers))


def score(bh: Borehole, page_texts: dict[tuple[str, int | None], str], unreadable: int) -> None:
    reasons: list[str] = []
    extraction = 1.0
    if not bh.layers:
        extraction = 0.0
        reasons.append("No layers could be extracted")
    structural = [i for i in bh.issues if i.startswith(STRUCTURAL)]
    if structural:
        extraction -= min(0.4, 0.1 * len(structural))
        reasons.append(f"{len(structural)} plausibility finding(s) in the layer sequence")
    if unreadable:
        extraction -= min(0.2, 0.05 * unreadable)
        reasons.append(f"{unreadable} item(s) reported as unreadable")
    found, total, checked, checkable = text_check(bh, page_texts)
    agreement = found / total if total >= 3 else None
    has_text = checked > 0
    if agreement is not None and agreement < NOT_PRINTED and not prints_depths(bh, page_texts):
        # Almost nothing matches: the log draws boundaries against a depth scale instead of printing them.
        # No penalty, but no confirmation either.
        reasons.append("Layer depths are not printed as numbers in the PDF text layer (read from the depth scale)")
        agreement = None
    elif agreement is not None:
        if agreement < 0.8:
            extraction -= (0.8 - agreement) * 0.6
        reasons.append(f"{round(agreement * 100)} % of layer depths confirmed by the PDF text layer")
    # Plausible is not the same as correct: release automatically only with positive evidence. With a text
    # layer only the text can confirm, and it has to cover most layers (scanned extra sheets don't ride along);
    # without one, a log from 0 m to a stated final depth counts.
    # Few boundaries (a one-layer pit): all of them must be in the text.
    confirmed = ((agreement is not None and agreement >= 0.8) or (0 < total < 3 and found == total)) \
        and checked >= 0.8 * max(1, checkable)
    if bh.layers and not confirmed and (has_text or not covers_full_depth(bh)):
        if extraction > UNCONFIRMED_CAP:
            extraction = UNCONFIRMED_CAP
            reasons.append("Nothing confirms the layer depths independently (PDF text layer missing, partial or not "
                           "matching, and no complete log from 0 m to a stated final depth on a scan)")
    look = [i for i in bh.issues if finding_severity(i) == "needs_look"]
    if look and extraction > UNCONFIRMED_CAP:
        extraction = UNCONFIRMED_CAP
        reasons.append(f"{len(look)} finding(s) need a look before release (inferred values or inconsistent core runs)")
    bh.confidence = round(max(0.0, min(1.0, extraction)), 2)

    loc = bh.location
    position = loc.confidence or 0.0
    if loc.method == GeorefMethod.geocoded_place:
        reasons.append("Position estimated from the place name only")
    elif loc.lat is None:
        reasons.append("Not georeferenced")
    elif position < 0.9:
        doubts = [i for i in bh.issues if i.startswith(GEOREF_DOUBTS)]
        reasons += [i[0].upper() + i[1:] for i in doubts]
        if position < 0.8 or not doubts:
            reasons.append("Printed coordinates only partly match the named place")
    if len(loc.crs_candidates) > 1:
        reasons.append("Coordinate system ambiguous: " + ", ".join(loc.crs_candidates[:3]))

    bh.score = round(min(bh.confidence, position), 2)
    blocking = [i for i in bh.issues if finding_severity(i) == "blocking"]
    released = bh.score >= config.AUTO_APPROVE_THRESHOLD and not blocking
    bh.review_status = ReviewStatus.auto_approved if released else ReviewStatus.pending_review
    if not released and not reasons:
        reasons.append(f"Score below {round(config.AUTO_APPROVE_THRESHOLD * 100)}")
    bh.score_reasons = reasons


# Findings as data: a stable code and a severity per finding text, for queries and the review queue.
FINDING_CODES: list[tuple[str, str]] = [
    ("no layers extracted", "no_layers"), ("log not extracted", "log_not_extracted"), ("layer ", "invalid_interval"), ("gap", "gap"), ("overlap", "overlap"),
    ("minor discontinuity", "minor_discontinuity"), ("layers end at", "depth_mismatch"),
    ("log starts at", "log_starts_below_surface"), ("missing sheets", "missing_sheets"),
    ("sheet header differs", "sheet_header_differs"), ("same printed coordinates as", "coords_shared"),
    ("CRS ambiguous", "crs_ambiguous"), ("coordinate system inferred", "crs_inferred"),
    ("coordinate axes inferred", "axes_inferred"), ("coordinate notation or datum ambiguous", "notation_ambiguous"),
    ("coordinate system taken from", "crs_from_document"), ("coordinate system from reviewer knowledge", "crs_from_knowledge"),
    ("position from a local grid", "position_from_grid"), ("position from place name only", "position_from_place"),
    ("printed coordinates could not be matched", "coords_unmatched"), ("coordinates ", "coords_far_from_place"),
    ("position ", "position_outlier"), ("no place to cross-check", "no_place_check"),
    ("not georeferenced", "not_georeferenced"), ("invalid WGS84", "invalid_position"), ("unreadable:", "unreadable"),
    ("boundary inferred", "boundary_inferred"), ("core loss inferred", "core_loss_inferred"),
    ("lithology of layer", "lithology_inferred"), ("reached at the final depth", "end_of_hole_material"),
    ("corrected by", "correction"), ("stale correction", "stale_correction"), ("depths printed in feet", "feet"),
    ("core run", "core_run_inconsistent"), ("sample", "test_below_final_depth"), ("SPT at", "test_below_final_depth"),
    ("groundwater marked dry", "groundwater_inconsistent"), ("combined with the record from", "combined_records"),
    ("same hole assumed", "same_hole_assumed"), ("sources disagree on", "sources_disagree"),
    ("sources differ on", "sources_differ"), ("possibly the same hole as", "possibly_same_hole"),
]
EPSG_IN_FINDING = re.compile(r"EPSG:(\d{4,6})")


def finding_code(issue: str) -> str:
    # 'layer X–Y m restated / repeats / ends at' are sheet-merge notes, not invalid intervals
    if issue.startswith("layer ") and re.search(r"restated|repeats|ends at", issue):
        return "sheet_merge"
    return next((code for prefix, code in FINDING_CODES if issue.startswith(prefix)), "other")


def finding_severity(issue: str) -> str:
    if is_blocking(issue):
        return "blocking"
    if issue.startswith(NEEDS_LOOK) or NEEDS_LOOK_RE.match(issue):
        return "needs_look"
    if issue.startswith(("unreadable:", "corrected by", "combined with the record from")):
        return "info"
    return "warning"
