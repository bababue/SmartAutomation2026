import type { Detail, FeatureCollection, Filters, Stats } from './types'

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export function fetchBoreholes(f: Filters): Promise<FeatureCollection> {
  const params = new URLSearchParams()
  if (f.q.trim()) params.set('q', f.q.trim())
  f.lithology.forEach((l) => params.append('lithology', l))
  if (f.minConfidence != null) params.set('min_confidence', String(f.minConfidence))
  if (f.issuesOnly) params.set('has_issues', 'true')
  return get(`/api/boreholes?${params}`)
}

export const fetchDetail = (id: string) => get<Detail>(`/api/boreholes/${encodeURIComponent(id)}/detail`)
export const fetchStats = () => get<Stats>('/api/stats')

export async function setReviewed(id: string, reviewed: boolean): Promise<void> {
  await fetch(`/api/boreholes/${encodeURIComponent(id)}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed }),
  })
}
