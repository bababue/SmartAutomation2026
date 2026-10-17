"""Ground model values per borehole, derived from the layers in code: rock head and groundwater level.

These are what a tunnel route is planned from (soft ground vs. rock, water). They are derived, not
extracted, so they live outside the Borehole record and do not change its content hash.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from schema.borehole import Borehole, Lithology

L = Lithology
SOIL = {
    L.gravel, L.coarse_gravel, L.medium_gravel, L.fine_gravel, L.sand, L.coarse_sand, L.medium_sand, L.fine_sand,
    L.silt, L.clay, L.peat_humus, L.cobbles, L.boulders, L.volcanic_ash, L.topsoil, L.fill, L.slope_loam, L.scree,
    L.boulder_clay, L.till_marl, L.loess, L.loess_loam, L.marsh_clay, L.meadow_lime, L.varved_clay, L.gyttja,
    L.lignite,  # soft, excavated like soil
}
NO_DATA = {L.no_recovery, L.unknown}

# Rock weathered to soil-like material (grades IV-VI): not yet the rock head.
_DECOMPOSED = re.compile(
    r"\b(complete|completely|highly|extremely|totally)\s+weathered\b|\bresidual\b|\bdecomposed\b|"
    r"\bW\s?[4-6]\b|(?-i:\b(IV|V|VI)\b)|\bvollständig\s+verwittert\b|\bstark\s+verwittert\b|\bzersetzt\b",
    re.IGNORECASE,
)


class GroundSummary(BaseModel):
    final_depth_m: float | None
    rock_head_m: float | None  # top of the rock that continues to the bottom of the hole; None: not reached
    weathered_rock_top_m: float | None  # top of decomposed rock above the rock head, if any
    groundwater_m: float | None  # shallowest water level below ground surface


def decomposed(weathering: str | None) -> bool:
    return bool(weathering and _DECOMPOSED.search(weathering))


def summarize(bh: Borehole) -> GroundSummary:
    layers = sorted(bh.layers, key=lambda layer: layer.top_m)
    final = bh.depth_m or (max(layer.bottom_m for layer in layers) if layers else None)
    # From the bottom up: the run of rock that reaches the end of the hole. Core loss and unreadable
    # layers inside the run do not break it; any soil layer does.
    rock_head = weathered_top = None
    for layer in reversed(layers):
        if layer.primary in NO_DATA:
            continue
        if layer.primary in SOIL:
            break
        if decomposed(layer.weathering):
            weathered_top = layer.top_m
        else:
            rock_head, weathered_top = layer.top_m, None
    if rock_head is None:
        weathered_top = None  # decomposed rock only: rock not reached
    levels = [g.depth_m for g in bh.groundwater
              if g.depth_m is not None and not g.dry and g.reference in (None, "ground")]
    return GroundSummary(final_depth_m=final, rock_head_m=rock_head, weathered_rock_top_m=weathered_top,
                         groundwater_m=min(levels) if levels else None)
