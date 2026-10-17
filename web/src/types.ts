// Mirrors schema/borehole.py (only the fields the UI uses).

export type Lithology = string

export interface SourceRef {
  file: string
  page: number | null
  /** Approximate region on the page image as fractions (x0, y0, x1, y1), read by the vision model. */
  bbox: [number, number, number, number] | null
}

export interface Layer {
  top_m: number
  bottom_m: number
  description_original: string
  description_en: string | null
  primary: Lithology
  stratigraphic_name: string | null
  stratigraphic_code: string | null
  stratigraphic_color: string | null
  geologic_age: string | null
  source: SourceRef | null
}

export interface Location {
  coords_raw: string | null
  crs_raw: string | null
  crs_epsg: number | null
  crs_candidates: string[]
  lat: number | null
  lon: number | null
  method: 'stated_coordinates' | 'linked_table' | 'geocoded_place' | 'manual' | null
  accuracy_m: number | null
  confidence: number | null
  source: SourceRef | null
}

export interface SPT {
  depth_m: number
  n_value: number | null
  refusal: boolean
  raw: string | null
}

export interface Groundwater {
  depth_m: number | null
  kind: string
  dry: boolean | null
  reference: 'ground' | 'casing' | 'other' | null
  raw: string | null
}

export interface CoreRun {
  top_m: number
  bottom_m: number
  total_core_recovery_pct: number | null
  rqd_pct: number | null
}

export interface LabResult {
  parameter: string
  value: number
  value_raw: string | null
  top_m: number | null
}

export interface Borehole {
  borehole_id: string
  borehole_name: string
  location_name: string | null
  project_name: string | null
  client: string | null
  contractor: string | null
  start_date: string | null
  end_date: string | null
  location: Location
  surface_elevation_m: number | null
  surface_elevation_raw: string | null
  elevation_datum: string | null
  depth_m: number | null
  inclination_deg: number | null
  drilling_method: string | null
  notes: string | null
  layers: Layer[]
  core_runs: CoreRun[]
  spt: SPT[]
  groundwater: Groundwater[]
  lab_results: LabResult[]
  confidence: number | null
  issues: string[]
  score: number | null
  score_reasons: string[]
  review_status: ReviewStatus
  reviewed_by: string | null
  reviewed_at: string | null
  review_comment: string | null
}

export type ReviewStatus = 'auto_approved' | 'pending_review' | 'approved' | 'rejected'

export interface ReviewEvent {
  decision: ReviewStatus
  reviewer: string
  comment: string | null
  at: string
}

export interface QueueItem {
  id: string
  name: string
  project: string | null
  location_name: string | null
  country: string | null
  score: number | null
  n_layers: number
  source_file: string | null
  reasons: string[]
}

/** One page of the review queue, with the number of all records in it. */
export interface QueuePage {
  items: QueueItem[]
  total: number
}

export interface CrsGroup {
  group: string
  epsg: number
  name: string
  count: number
  borehole_id: string
  source_file: string | null
}

/** Ground model values derived from the layers (pipeline/ground.py), depths below ground in m. */
export interface Ground {
  /** Top of the rock that continues to the bottom of the hole; null: rock not reached. */
  rock_head_m: number | null
  /** Top of decomposed rock (grades IV–VI) above the rock head. */
  weathered_rock_top_m: number | null
  groundwater_m: number | null
}

export interface BoreholeSummary {
  id: string
  name: string
  project: string | null
  /** The hole's group in the list: its document group (the document, or a folder of single images). */
  grp: string
  location_name: string | null
  country: string | null
  depth: number | null
  elevation: number | null
  loc_method: string | null
  n_layers: number
  strip: [number, number, Lithology][] | null
  ground: Ground | null
  score: number | null
  review_status: ReviewStatus
}

/** A borehole on the map: the slim properties of every match. */
export type MapPoint = Pick<BoreholeSummary, 'id' | 'name' | 'depth' | 'review_status' | 'loc_method'>

/** Layer strips of the holes in a map box, for the 3D columns. */
export interface LayerStrips {
  items: { id: string; strip: BoreholeSummary['strip'] }[]
}

export interface Feature<P = BoreholeSummary> {
  type: 'Feature'
  geometry: { type: 'Point'; coordinates: [number, number] }
  properties: P
}

export interface FeatureCollection<P = BoreholeSummary> {
  type: 'FeatureCollection'
  features: Feature<P>[]
  /** All matches (the features may be one page of them). */
  total?: number
  facets?: { lithologies: [string, number][]; groups: GroupFacet[] }
}

/** A document group among all matches: its project, how many holes, how many released, where. */
export interface GroupFacet {
  group: string
  /** The project name its holes give most often; null: none gives one. */
  project: string | null
  count: number
  released: number
}

export type CorrectionKind = 'crs' | 'position' | 'layer' | 'layer_add' | 'field' | 'groundwater' | 'test'

/** One hole, or every hole of its document (e.g. a coordinate system). */
export type CorrectionScope = 'borehole' | 'document'

export interface CorrectionRecord {
  id: number
  scope: CorrectionScope
  target: string
  kind: CorrectionKind
  payload: Record<string, number | string | boolean>
  reviewer: string
  comment: string | null
  at: string
}

export interface Detail {
  borehole: Borehole
  source_pages: { file: string; page: number; url: string }[]
  review_log: ReviewEvent[]
  corrections: CorrectionRecord[]
  /** Findings that keep the record in the queue whatever its score. */
  blocking: string[]
  /** Version of the content shown; a decision on an outdated version is refused. */
  content_hash: string
  /** Review state the reviewer saw; a decision on an outdated state is refused. */
  review_revision: number
  threshold: number
}

/** What a review decision or a correction changes in the open record. */
export type DetailUpdate = Pick<Detail, 'borehole' | 'review_log' | 'content_hash' | 'review_revision'>
  & Partial<Pick<Detail, 'corrections' | 'blocking'>>

export interface Stats {
  boreholes: number
  located: number
  status: Record<ReviewStatus, number>
  threshold: number
  demo: boolean
}

/** Aggregates over the records the list filters select (holes without a position included). */
export interface StatsDetail {
  documents: number
  pages: number
  boreholes: number
  layers: number
  countries: number
  status: Record<ReviewStatus, number>
  position: { exact: number; approximate: number; none: number }
  funnel: { records: number; without_blocking: number; released_automatically: number; released: number }
  threshold: number
  /** Blocking first, then needs a look, warnings and notes; most records first within each. */
  findings: { code: string; severity: 'blocking' | 'needs_look' | 'warning' | 'info'; records: number; count: number }[]
  /** Logged metres per lithology, most first. */
  lithology_m: [string, number][]
  depth_bin_m: number
  /** Holes per final-depth bin of depth_bin_m metres; the last bin holds everything deeper. */
  depths: number[]
  groups: { group: string; count: number; released: number; top_finding: { code: string; records: number } | null }[]
  crs: [number, number][]
  /** Release audit over the whole database; null: no sample drawn yet (`pipeline eval audit`). */
  audit: { sample: number; reviewed: number; wrong: number; rate: number | null; low: number; high: number } | null
}

/** An accepted upload batch: its job, and which files are read or skipped. */
export interface UploadJob {
  job_id: string
  pages: number
  files: string[]
  skipped: string[]
}

export interface UploadStatus {
  status: 'queued' | 'processing' | 'done' | 'error'
  boreholes: { id: string; name: string; review_status: ReviewStatus }[]
  error: string | null
  /** Uploads to finish before this one starts; set only while queued. */
  ahead: number | null
  /** The running stage; set only while processing. */
  progress: { stage: 'classify' | 'extract' | 'lab results' | 'coordinates' | 'store'; done: number; total: number } | null
}

export type StatusFilter = 'all' | 'released' | 'pending' | 'rejected'

/** A key with an optional range, e.g. clay between 10 and 20 m, or UCS of at least 80. */
export interface RangeFilter {
  key: string
  min: number | null
  max: number | null
}

export interface Filters {
  q: string
  /** Lithologies that must all occur, each within its depth range (m). */
  lithology: RangeFilter[]
  /** Lab parameters that must all be measured, each within its value range. */
  lab: RangeFilter[]
  minPosition: number | null
  status: StatusFilter
  /** Only the holes of this document group. */
  group: string | null
  /** Ground search: rock head and groundwater at most this deep (m). */
  rockMax: number | null
  waterMax: number | null
  /** At least so many metres of a material group in a depth range, as 'group:from:to:metres'. */
  material: string[]
}

export interface SimilarHole {
  id: string
  name: string
  project: string | null
  country: string | null
  review_status: ReviewStatus
  score: number | null
  similarity: number
  reason: string
}

export interface SimilarGround {
  ground: string | null // this hole's ground in a few words; null: too few layers to compare
  /** Ground filters that find holes like this one. */
  like: { rock_head_max: number | null; groundwater_max: number | null; material: string[] } | null
  similar: SimilarHole[]
}
