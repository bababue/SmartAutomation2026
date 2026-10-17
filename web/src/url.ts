import { parseDecimal } from './format'
import type { Filters, QueuePosition, RangeFilter, ReviewFilters, StatusFilter } from './types'

export type View = 'map' | 'review' | 'stats'
export type StatsTab = 'data' | 'quality'

export interface UrlState {
  view: View
  routeActive: boolean
  tunnelId: string | null
  selectedId: string | null
  listOpen: boolean
  filters: Filters
  reviewFilters: ReviewFilters
  /** Review mode: the release audit sample instead (automatic releases drawn for a person to check). */
  auditOnly: boolean
  statsTab: StatsTab
}

const STATUSES: StatusFilter[] = ['all', 'released', 'pending', 'rejected']
const VIEWS: View[] = ['map', 'review', 'stats']
const POSITIONS: QueuePosition[] = ['printed', 'estimated', 'crs_inferred', 'none']

const num = (v: string | null) => (v == null ? null : parseDecimal(v))

export const DEFAULT_FILTERS: Filters = {
  q: '', lithology: [], lab: [], status: 'all', scoreMin: null, scoreMax: null, group: null, rockMax: null, waterMax: null,
  material: [],
}
export const DEFAULT_REVIEW_FILTERS: ReviewFilters = { finding: null, position: null }

/** Whether the filters narrow the records at all. */
export function hasFilters(f: Filters): boolean {
  return Boolean(f.q.trim() || f.lithology.length || f.lab.length || f.status !== 'all' || f.group ||
    f.rockMax != null || f.waterMax != null || f.material.length || f.scoreMin != null || f.scoreMax != null)
}

/** 'key', or 'key:min:max' with either bound left empty; the format the API reads too. */
export function encodeRange(r: RangeFilter): string {
  return r.min == null && r.max == null ? r.key : `${r.key}:${r.min ?? ''}:${r.max ?? ''}`
}

function decodeRange(value: string): RangeFilter {
  const [key, min = null, max = null] = value.split(':')
  return { key, min: num(min), max: num(max) }
}

export function readUrl(search: string): UrlState {
  const p = new URLSearchParams(search)
  const status = p.get('status') as StatusFilter | null
  return {
    view: VIEWS.includes(p.get('view') as View) ? (p.get('view') as View) : 'map',
    routeActive: p.get('route') === '1',
    tunnelId: p.get('tunnel'),
    selectedId: p.get('borehole'),
    auditOnly: p.get('audit') === '1',
    statsTab: p.get('tab') === 'quality' ? 'quality' : 'data',
    listOpen: p.get('list') !== 'closed',
    reviewFilters: {
      finding: p.get('finding'),
      position: POSITIONS.find((x) => x === p.get('position')) ?? null,
    },
    filters: {
      q: p.get('q') ?? '',
      lithology: p.getAll('lith').filter(Boolean).map(decodeRange),
      lab: p.getAll('lab').filter(Boolean).map(decodeRange),
      status: status && STATUSES.includes(status) ? status : 'all',
      scoreMin: num(p.get('smin')),
      scoreMax: num(p.get('smax')),
      group: p.get('group'),
      rockMax: num(p.get('rock')),
      waterMax: num(p.get('water')),
      material: p.getAll('mat').filter(Boolean),
    },
  }
}

export function writeUrl(s: UrlState): string {
  const p = new URLSearchParams()
  const f = s.filters
  if (s.view !== 'map') p.set('view', s.view)
  if (s.routeActive) p.set('route', '1')
  if (s.routeActive && s.tunnelId) p.set('tunnel', s.tunnelId)
  if (s.selectedId) p.set('borehole', s.selectedId)
  if (s.auditOnly) p.set('audit', '1')
  if (s.view === 'stats' && s.statsTab !== 'data') p.set('tab', s.statsTab)
  if (!s.listOpen) p.set('list', 'closed')
  if (f.q.trim()) p.set('q', f.q.trim())
  f.lithology.forEach((l) => p.append('lith', encodeRange(l)))
  f.lab.forEach((l) => p.append('lab', encodeRange(l)))
  if (f.status !== 'all') p.set('status', f.status)
  if (s.reviewFilters.finding) p.set('finding', s.reviewFilters.finding)
  if (s.reviewFilters.position) p.set('position', s.reviewFilters.position)
  if (f.scoreMin != null) p.set('smin', String(f.scoreMin))
  if (f.scoreMax != null) p.set('smax', String(f.scoreMax))
  if (f.group) p.set('group', f.group)
  if (f.rockMax != null) p.set('rock', String(f.rockMax))
  if (f.waterMax != null) p.set('water', String(f.waterMax))
  f.material.forEach((m) => p.append('mat', m))
  const qs = p.toString()
  return qs ? `?${qs}` : window.location.pathname
}
