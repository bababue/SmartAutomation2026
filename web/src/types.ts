// Mirrors schema/borehole.py (only the fields the UI uses).

export type Lithology = string

export interface SourceRef {
  file: string
  page: number | null
}

export interface Layer {
  top_m: number
  bottom_m: number
  description_original: string
  description_en: string | null
  language: string | null
  primary: Lithology
  secondary: { lithology: Lithology; qualifier: string | null }[]
  uscs: string | null
  stratigraphic_name: string | null
  stratigraphic_code: string | null
  stratigraphic_color: string | null
  geologic_age: string | null
  color: string | null
  consistency: string | null
  relative_density: string | null
  weathering: string | null
  source: SourceRef | null
}

export interface Location {
  coords_raw: string | null
  crs_raw: string | null
  easting: number | null
  northing: number | null
  crs_epsg: number | null
  crs_candidates: string[]
  lat: number | null
  lon: number | null
  method: 'stated_coordinates' | 'linked_table' | 'geocoded_place' | 'manual' | null
  accuracy_m: number | null
  place_name: string | null
  country: string | null
  confidence: number | null
}

export interface SPT {
  depth_m: number
  blows: number[]
  n_value: number | null
  refusal: boolean
  raw: string | null
}

export interface Groundwater {
  depth_m: number | null
  kind: string
  dry: boolean | null
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
  investigation_type: string | null
  location_name: string | null
  project_id: string | null
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
  sources: SourceRef[]
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
  confidence: number | null
  loc_confidence: number | null
  loc_method: string | null
  n_layers: number
  reasons: string[]
}

export interface BoreholeSummary {
  id: string
  name: string
  project: string | null
  location_name: string | null
  country: string | null
  depth: number | null
  elevation: number | null
  investigation_type: string | null
  confidence: number | null
  loc_confidence: number | null
  loc_method: string | null
  n_layers: number
  n_issues: number
  lithologies: string | null
  strip: [number, number, Lithology][] | null
  score: number | null
  review_status: ReviewStatus
}

export interface Feature {
  type: 'Feature'
  geometry: { type: 'Point'; coordinates: [number, number] }
  properties: BoreholeSummary
}

export interface FeatureCollection {
  type: 'FeatureCollection'
  features: Feature[]
}

export interface Detail {
  borehole: Borehole
  source_pages: { file: string; page: number; url: string }[]
  review_log: ReviewEvent[]
  threshold: number
}

export interface Stats {
  boreholes: number
  located: number
  layers: number
  documents: number
  pages: number
  countries: { country: string; n: number }[]
  status: Record<ReviewStatus, number>
  threshold: number
}

export type StatusFilter = 'all' | 'released' | 'pending' | 'rejected'

export interface Filters {
  q: string
  lithology: Lithology[]
  minPosition: number | null
  status: StatusFilter
}
