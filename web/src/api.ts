import type { CorrectionKind, CrsGroup, Detail, DetailUpdate, FeatureCollection, Filters, KnowledgeItem, KnowledgeList, QueueItem, ReviewStatus, Stats } from './types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${body ? `: ${body.slice(0, 200)}` : ''}`)
  }
  return res.json() as Promise<T>
}

export function fetchBoreholes(f: Filters, signal?: AbortSignal): Promise<FeatureCollection> {
  const params = new URLSearchParams()
  if (f.q.trim()) params.set('q', f.q.trim())
  f.lithology.forEach((l) => params.append('lithology', l))
  if (f.minPosition != null) params.set('min_position', String(f.minPosition))
  if (f.status !== 'all') params.set('status', f.status)
  return request(`/api/boreholes?${params}`, { signal })
}

const enc = (id: string) => encodeURIComponent(id)

export const fetchDetail = (id: string, signal?: AbortSignal) => request<Detail>(`/api/boreholes/${enc(id)}/detail`, { signal })
export const fetchStats = () => request<Stats>('/api/stats')
export const fetchQueue = (signal?: AbortSignal) => request<QueueItem[]>('/api/review-queue', { signal })
export const fetchCrsGroups = (signal?: AbortSignal) => request<CrsGroup[]>('/api/review-queue/crs-groups', { signal })

export const fetchKnowledge = (boreholeId?: string) =>
  request<KnowledgeList>(`/api/knowledge${boreholeId ? `?borehole_id=${enc(boreholeId)}` : ''}`)

export function postKnowledge(body: {
  borehole_id: string; scope_type: KnowledgeItem['scope_type']; kind: KnowledgeItem['kind']
  payload: Record<string, unknown>; source: 'review' | 'external'; note: string | null; reviewer: string
}) {
  return request<{ item: KnowledgeItem; rebuilding: number }>('/api/knowledge', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
}

export const deleteKnowledge = (id: number) => request<{ rebuilding: number }>(`/api/knowledge/${id}`, { method: 'DELETE' })

export function postCorrection(id: string, kind: CorrectionKind, payload: Record<string, unknown>, reviewer: string,
  scope: 'borehole' | 'document' = 'borehole') {
  return request<DetailUpdate & { updated: string[] }>(`/api/boreholes/${enc(id)}/corrections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, scope, payload, reviewer, comment: null }),
  })
}

export function postReview(id: string, decision: ReviewStatus, reviewer: string, comment: string | null) {
  return request<Pick<Detail, 'borehole' | 'review_log'>>(`/api/boreholes/${enc(id)}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, reviewer, comment }),
  })
}
