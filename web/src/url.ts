import type { Filters, StatusFilter } from './types'

export type View = 'map' | 'review'

export interface UrlState {
  view: View
  selectedId: string | null
  sectionIds: string[]
  filters: Filters
}

const STATUSES: StatusFilter[] = ['all', 'released', 'pending', 'rejected']

function num(v: string | null): number | null {
  if (v == null || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function readUrl(search: string): UrlState {
  const p = new URLSearchParams(search)
  const status = p.get('status') as StatusFilter | null
  return {
    view: p.get('view') === 'review' ? 'review' : 'map',
    selectedId: p.get('borehole'),
    sectionIds: p.getAll('section'),
    filters: {
      q: p.get('q') ?? '',
      lithology: p.getAll('lith'),
      depthFrom: num(p.get('from')),
      depthTo: num(p.get('to')),
      lab: p.get('lab'),
      labMin: num(p.get('lab_min')),
      minPosition: num(p.get('position')),
      status: status && STATUSES.includes(status) ? status : 'all',
    },
  }
}

export function writeUrl(s: UrlState): string {
  const p = new URLSearchParams()
  const f = s.filters
  if (s.view !== 'map') p.set('view', s.view)
  if (s.selectedId) p.set('borehole', s.selectedId)
  s.sectionIds.forEach((id) => p.append('section', id))
  if (f.q.trim()) p.set('q', f.q.trim())
  f.lithology.forEach((l) => p.append('lith', l))
  if (f.depthFrom != null) p.set('from', String(f.depthFrom))
  if (f.depthTo != null) p.set('to', String(f.depthTo))
  if (f.lab) p.set('lab', f.lab)
  if (f.labMin != null) p.set('lab_min', String(f.labMin))
  if (f.minPosition != null) p.set('position', String(f.minPosition))
  if (f.status !== 'all') p.set('status', f.status)
  const qs = p.toString()
  return qs ? `?${qs}` : window.location.pathname
}
