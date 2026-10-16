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
BLOCKING = ("no layers extracted", "CRS ambiguous", "invalid interval", "non-finite depth", "invalid WGS84",
            "gap", "overlap", "layers end at", "stale correction", "log starts at", "depths printed in feet")

# Georeferencing doubts that explain a position confidence below the release threshold.
GEOREF_DOUBTS = ("coordinate system inferred", "coordinate axes inferred", "coordinate notation or datum ambiguous")

STRUCTURAL = ("gap", "overlap", "invalid interval", "layers end at", "log starts at", "groundwater marked dry")


def _variants(v: float) -> set[str]:
    out = {f"{v:.2f}", f"{v:.1f}", f"{v:g}"}
    out |= {x.replace(".", ",") for x in out}
    return out


def text_agreement(bh: Borehole, page_texts: dict[tuple[str, int | None], str]) -> float | None:
    """Share of extracted layer boundaries that occur literally in the source page's text layer.
    None when the source has no usable text layer (scans, images, outlined vector PDFs)."""
    found = total = 0
    for layer in bh.layers:
        src = layer.source
        text = page_texts.get((src.file, src.page)) if src else None
        if not text or len(text) < 200:
            continue
        numbers = set(re.findall(r"\d+(?:[.,]\d+)?", text))
        for boundary in (layer.top_m, layer.bottom_m):
            if boundary == 0:
                continue
            total += 1
            found += bool(_variants(boundary) & numbers)
    return found / total if total >= 3 else None


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
    agreement = text_agreement(bh, page_texts)
    if agreement is not None:
        if agreement < 0.8:
            extraction -= (0.8 - agreement) * 0.6
        reasons.append(f"{round(agreement * 100)} % of layer depths confirmed by the PDF text layer")
    # Plausible is not the same as correct: release automatically only with positive evidence.
    if bh.layers and not (agreement is not None and agreement >= 0.8) and not covers_full_depth(bh):
        if extraction > UNCONFIRMED_CAP:
            extraction = UNCONFIRMED_CAP
            reasons.append("Nothing confirms the layer depths independently (no matching PDF text layer, and the log "
                           "does not run from 0 m to a stated final depth)")
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
    blocking = [i for i in bh.issues if any(b in i for b in BLOCKING)]
    released = bh.score >= config.AUTO_APPROVE_THRESHOLD and not blocking
    bh.review_status = ReviewStatus.auto_approved if released else ReviewStatus.pending_review
    if not released and not reasons:
        reasons.append(f"Score below {round(config.AUTO_APPROVE_THRESHOLD * 100)}")
    bh.score_reasons = reasons
