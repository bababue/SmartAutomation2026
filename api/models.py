"""Response bodies of the HTTP API; web/src/types.ts mirrors them."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

from pipeline.ground import GroundSummary
from schema.borehole import Borehole, GeorefMethod, ReviewStatus

P = TypeVar("P")
Strip = list[tuple[float, float, str]]  # (top m, bottom m, lithology) per layer


class Point(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]  # lon, lat


class Feature(BaseModel, Generic[P]):
    type: Literal["Feature"] = "Feature"
    geometry: Point
    properties: P


class MapPoint(BaseModel):
    id: str
    name: str
    depth: float | None
    review_status: ReviewStatus
    loc_method: GeorefMethod | None


class Summary(MapPoint):
    project: str | None
    grp: str  # the hole's document group
    location_name: str | None
    country: str | None
    elevation: float | None
    score: float | None
    strip: Strip | None
    ground: GroundSummary | None


class GroupFacet(BaseModel):
    group: str
    project: str | None  # the project name its holes give most often
    count: int
    released: int
    bbox: tuple[float, float, float, float]  # west, south, east, north


class Facets(BaseModel):
    lithologies: list[tuple[str, int]]  # holes per lithology, most first
    groups: list[GroupFacet]


class ListPage(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    total: int
    offset: int
    limit: int
    facets: Facets
    features: list[Feature[Summary]]


class MapPoints(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    total: int
    features: list[Feature[MapPoint]]


class LayerStrip(BaseModel):
    id: str
    strip: Strip | None


class LayerStrips(BaseModel):
    items: list[LayerStrip]


class QueueItem(BaseModel):
    id: str
    name: str
    project: str | None
    location_name: str | None
    country: str | None
    score: float | None
    n_layers: int
    source_file: str | None
    reasons: list[str]


class QueuePage(BaseModel):
    items: list[QueueItem]
    total: int


class SourcePage(BaseModel):
    file: str
    page: int
    url: str


class ReviewEvent(BaseModel):
    decision: ReviewStatus
    reviewer: str
    comment: str | None
    at: str


class CorrectionRecord(BaseModel):
    id: int
    scope: Literal["borehole", "document"]
    target: str
    kind: str
    payload: dict
    reviewer: str
    comment: str | None
    at: str


class ReviewResult(BaseModel):
    borehole: Borehole
    review_log: list[ReviewEvent]
    content_hash: str  # the version a next decision is bound to
    review_revision: int


class CorrectionResult(ReviewResult):
    corrections: list[CorrectionRecord]
    blocking: list[str]
    updated: list[str]  # the holes the correction changed (several for a document-wide one)


class Detail(ReviewResult):
    source_pages: list[SourcePage]
    corrections: list[CorrectionRecord]
    blocking: list[str]  # findings that keep the record in the queue whatever its score
    threshold: float


class Stats(BaseModel):
    boreholes: int
    located: int
    status: dict[ReviewStatus, int]
    threshold: float
    demo: bool  # synthetic fixtures, no source pages


class PositionCounts(BaseModel):
    exact: int
    approximate: int
    none: int


class Funnel(BaseModel):
    records: int
    without_blocking: int
    released_automatically: int
    released: int


class FindingCount(BaseModel):
    code: str
    severity: str
    records: int
    count: int


class TopFinding(BaseModel):
    code: str
    records: int


class GroupStats(BaseModel):
    group: str
    count: int
    released: int
    top_finding: TopFinding | None


class Audit(BaseModel):
    sample: int
    reviewed: int
    wrong: int
    rate: float | None
    low: float
    high: float


class StatsDetail(BaseModel):
    documents: int
    pages: int
    boreholes: int
    layers: int
    countries: int
    status: dict[ReviewStatus, int]
    position: PositionCounts
    funnel: Funnel
    threshold: float
    findings: list[FindingCount]
    lithology_m: list[tuple[str, float]]
    depth_bin_m: int
    depths: list[int]
    groups: list[GroupStats]
    crs: list[tuple[int, int]]
    audit: Audit | None


class UploadJob(BaseModel):
    job_id: str
    pages: int
    files: list[str]
    skipped: list[str]
