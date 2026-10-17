import type { Filters, RangeFilter, StatusFilter } from './types'

export type View = 'map' | 'review' | 'stats'

export interface UrlState {
  view: View
  routeActive: boolean
  selectedId: string | null
  routeHoleIds: string[]
  listOpen: boolean
  filters: Filters
  /** Review queue: only records with a blocking finding. */
  blockingOnly: boolean
  /** Review queue: the release audit sample instead (automatic releases drawn for a person to check). */
  auditOnly: boolean
}

const STATUSES: StatusFilter[] = ['all', 'released', 'pending', 'rejected']
const VIEWS: View[] = ['map', 'review', 'stats']

function num(v: string | null): number | null {
  if (v == null || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
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
    selectedId: p.get('borehole'),
    routeHoleIds: p.getAll('routeHole'),
    blockingOnly: p.get('blocking') === '1',
    auditOnly: p.get('audit') === '1',
    listOpen: p.get('list') !== 'closed',
    filters: {
      q: p.get('q') ?? '',
      lithology: p.getAll('lith').filter(Boolean).map(decodeRange),
      lab: p.getAll('lab').filter(Boolean).map(decodeRange),
      minPosition: num(p.get('position')),
      status: status && STATUSES.includes(status) ? status : 'all',
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
  if (s.selectedId) p.set('borehole', s.selectedId)
  s.routeHoleIds.forEach((id) => p.append('routeHole', id))
  if (s.blockingOnly) p.set('blocking', '1')
  if (s.auditOnly) p.set('audit', '1')
  if (!s.listOpen) p.set('list', 'closed')
  if (f.q.trim()) p.set('q', f.q.trim())
  f.lithology.forEach((l) => p.append('lith', encodeRange(l)))
  f.lab.forEach((l) => p.append('lab', encodeRange(l)))
  if (f.minPosition != null) p.set('position', String(f.minPosition))
  if (f.status !== 'all') p.set('status', f.status)
  if (f.group) p.set('group', f.group)
  if (f.rockMax != null) p.set('rock', String(f.rockMax))
  if (f.waterMax != null) p.set('water', String(f.waterMax))
  f.material.forEach((m) => p.append('mat', m))
  const qs = p.toString()
  return qs ? `?${qs}` : window.location.pathname
}
