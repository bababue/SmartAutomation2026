import { encodeRange } from './url'
import type { BoreholeSummary, CorrectionKind, CrsGroup, Detail, DetailUpdate, FeatureCollection, Filters, QueueItem, ReviewStatus, SimilarGround, Stats, UploadStatus } from './types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    let detail = body.slice(0, 200)
    try {
      const parsed = JSON.parse(body)
      if (typeof parsed.detail === 'string') detail = parsed.detail // FastAPI's HTTPException message
    } catch { /* not JSON: keep the raw text */ }
    throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ''}`)
  }
  return res.json() as Promise<T>
}

function filterParams(f: Filters): URLSearchParams {
  const params = new URLSearchParams()
  if (f.q.trim()) params.set('q', f.q.trim())
  f.lithology.forEach((l) => params.append('lithology', encodeRange(l)))
  f.lab.forEach((l) => params.append('lab', encodeRange(l)))
  if (f.minPosition != null) params.set('min_position', String(f.minPosition))
  if (f.status !== 'all') params.set('status', f.status)
  return params
}

/** One page of summaries for the list, with the total and lithology counts of all matches. */
export function fetchBoreholes(f: Filters, offset = 0, limit = 200, signal?: AbortSignal): Promise<FeatureCollection> {
  const params = filterParams(f)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  return request(`/api/boreholes?${params}`, { signal })
}

/** Every match as a slim point for the map (no strips or ground models). */
export function fetchMapPoints(f: Filters, signal?: AbortSignal): Promise<FeatureCollection> {
  const params = filterParams(f)
  params.set('fields', 'map')
  return request(`/api/boreholes?${params}`, { signal })
}

/** Summaries of the given boreholes regardless of the filters, e.g. a section restored from the URL. */
export function fetchBoreholesById(ids: string[], signal?: AbortSignal): Promise<FeatureCollection> {
  const params = new URLSearchParams()
  ids.forEach((id) => params.append('id', id))
  params.set('limit', String(ids.length))
  return request(`/api/boreholes?${params}`, { signal })
}

export function fetchLayerStrips(bbox: string, signal?: AbortSignal): Promise<{ items: { id: string; strip: BoreholeSummary['strip'] }[] }> {
  const params = new URLSearchParams({ fields: 'layers', bbox })
  return request(`/api/boreholes?${params}`, { signal })
}

const enc = (id: string) => encodeURIComponent(id)

export const fetchDetail = (id: string, signal?: AbortSignal) => request<Detail>(`/api/boreholes/${enc(id)}/detail`, { signal })
export const fetchSimilar = (id: string, signal?: AbortSignal) =>
  request<SimilarGround>(`/api/boreholes/${enc(id)}/similar`, { signal })
export const fetchStats = () => request<Stats>('/api/stats')
export async function fetchQueue(blocking: boolean, signal?: AbortSignal, limit = 200): Promise<{ items: QueueItem[]; total: number }> {
  const res = await fetch(`/api/review-queue?limit=${limit}${blocking ? '&blocking=true' : ''}`, { signal })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  const items = (await res.json()) as QueueItem[]
  return { items, total: Number(res.headers.get('X-Total-Count') ?? items.length) }
}
export const fetchCrsGroups = (signal?: AbortSignal) => request<CrsGroup[]>('/api/review-queue/crs-groups', { signal })

export function postCorrection(id: string, kind: CorrectionKind, payload: Record<string, unknown>, reviewer: string,
  scope: 'borehole' | 'document' = 'borehole') {
  return request<DetailUpdate & { updated: string[] }>(`/api/boreholes/${enc(id)}/corrections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, scope, payload, reviewer, comment: null }),
  })
}

export function postReview(id: string, decision: ReviewStatus, reviewer: string, comment: string | null,
  contentHash: string | null = null, reviewRevision: number | null = null) {
  return request<DetailUpdate>(`/api/boreholes/${enc(id)}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, reviewer, comment, content_hash: contentHash, review_revision: reviewRevision }),
  })
}

export function postUpload(file: File) {
  const body = new FormData()
  body.append('file', file)
  return request<{ job_id: string; pages: number }>('/api/upload', { method: 'POST', body })
}

export const fetchUploadStatus = (jobId: string) => request<UploadStatus>(`/api/upload/${enc(jobId)}`)
