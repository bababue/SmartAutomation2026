import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchBoreholes, fetchDetail, fetchQueue, fetchStats } from './api'
import { LithologyPatterns, lithStyle, patternId } from './lithology'
import { DetailPanel } from './components/DetailPanel'
import { MapView } from './components/MapView'
import { ResultList } from './components/ResultList'
import { ReviewQueue } from './components/ReviewQueue'
import type { Detail, FeatureCollection, Filters, QueueItem, Stats, StatusFilter } from './types'

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }
type View = 'map' | 'review'

export default function App() {
  const [view, setView] = useState<View>('map')
  const [filters, setFilters] = useState<Filters>({ q: '', lithology: [], minPosition: null, status: 'all' })
  const [query, setQuery] = useState('')
  const [data, setData] = useState<FeatureCollection>(EMPTY)
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [refresh, setRefresh] = useState(0) // bump after a review decision

  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: query })), 250)
    return () => clearTimeout(t)
  }, [query])

  useEffect(() => {
    const ctrl = new AbortController()
    setLoading(true)
    fetchBoreholes(filters, ctrl.signal)
      .then((fc) => { setData(fc); setError(null) })
      .catch((e) => e.name !== 'AbortError' && setError(`Could not load boreholes (${e.message}). Is the API running on port 8000?`))
      .finally(() => !ctrl.signal.aborted && setLoading(false))
    return () => ctrl.abort()
  }, [filters, refresh])

  useEffect(() => {
    const ctrl = new AbortController()
    fetchQueue(ctrl.signal).then(setQueue).catch(() => undefined)
    fetchStats().then(setStats).catch(() => undefined)
    return () => ctrl.abort()
  }, [refresh])

  // Only the response for the currently selected borehole may land in state.
  useEffect(() => {
    if (!selectedId) { setDetail(null); return }
    const ctrl = new AbortController()
    fetchDetail(selectedId, ctrl.signal)
      .then(setDetail)
      .catch((e) => e.name !== 'AbortError' && setError(`Could not load the borehole (${e.message}).`))
    return () => ctrl.abort()
  }, [selectedId])

  const lithologies = useMemo(() => {
    const counts = new Map<string, number>()
    data.features.forEach((f) => (f.properties.lithologies ?? '').split(',').filter(Boolean).forEach((l) => counts.set(l, (counts.get(l) ?? 0) + 1)))
    filters.lithology.forEach((l) => counts.set(l, counts.get(l) ?? 0))
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [data, filters.lithology])

  const focus = useMemo(() => {
    const loc = detail?.borehole.location
    if (!loc || loc.lat == null || loc.lon == null) return null
    return { lat: loc.lat, lon: loc.lon, accuracy_m: loc.accuracy_m, approximate: loc.method === 'geocoded_place' }
  }, [detail])

  const toggleLith = (key: string) =>
    setFilters((f) => ({ ...f, lithology: f.lithology.includes(key) ? f.lithology.filter((l) => l !== key) : [...f.lithology, key] }))

  const onReviewChange = useCallback((update: Pick<Detail, 'borehole' | 'review_log'>) => {
    setDetail((d) => (d && d.borehole.borehole_id === update.borehole.borehole_id ? { ...d, ...update } : d))
    setRefresh((n) => n + 1)
  }, [])

  const pending = stats?.status.pending_review ?? queue.length

  return (
    <div className="app">
      <svg width={0} height={0} style={{ position: 'absolute' }} aria-hidden="true"><LithologyPatterns /></svg>

      <header className="topbar">
        <h1 className="brand">Borehole Atlas</h1>
        {stats?.demo && <span className="demo-flag" title="The API serves data/demo.sqlite: synthetic records without source pages">Synthetic demo data</span>}
        <nav className="tabs" aria-label="Views">
          <button className={`tab${view === 'map' ? ' tab-on' : ''}`} aria-current={view === 'map'} onClick={() => setView('map')}>Map</button>
          <button className={`tab${view === 'review' ? ' tab-on' : ''}`} aria-current={view === 'review'} onClick={() => setView('review')}>
            Review queue {pending > 0 && <span className="tab-count">{pending}</span>}
          </button>
        </nav>
        <label className="search">
          <span className="visually-hidden">Search boreholes</span>
          <input
            type="search"
            placeholder="Search names, projects, places or layer descriptions, e.g. granite"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        {stats && (
          <p className="stats">
            <strong>{stats.boreholes.toLocaleString('en')}</strong> boreholes,{' '}
            <strong>{(stats.status.auto_approved + stats.status.approved).toLocaleString('en')}</strong> released
          </p>
        )}
      </header>

      <aside className="sidebar">
        <div className="filters">
          <div className="filter-row">
            <label>
              Release state
              <select value={filters.status} onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value as StatusFilter }))}>
                <option value="all">All</option>
                <option value="released">Released</option>
                <option value="pending">Waiting for review</option>
                <option value="rejected">Rejected</option>
              </select>
            </label>
            <label>
              Position quality
              <select
                value={filters.minPosition ?? ''}
                onChange={(e) => setFilters((f) => ({ ...f, minPosition: e.target.value ? Number(e.target.value) : null }))}
              >
                <option value="">Any</option>
                <option value="0.4">Medium or better</option>
                <option value="0.7">High only</option>
              </select>
            </label>
          </div>
          {lithologies.length > 0 && (
            <div className="chips" role="group" aria-label="Filter by lithology">
              {lithologies.map(([key, n]) => (
                <button key={key} className={`chip${filters.lithology.includes(key) ? ' chip-on' : ''}`} onClick={() => toggleLith(key)} aria-pressed={filters.lithology.includes(key)}>
                  <svg width={12} height={12} aria-hidden="true"><rect width={12} height={12} fill={`url(#${patternId(key)})`} /></svg>
                  {lithStyle(key).label}
                  <span className="chip-n">{n}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="result-head">
          <span>{loading ? 'Loading…' : `${data.features.length.toLocaleString('en')} shown`}</span>
          <a href="/api/export.csv" className="link" title="Released records only">Export released (CSV)</a>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <ResultList features={data.features} selectedId={selectedId} onSelect={setSelectedId} loading={loading} />
      </aside>

      <main className="map-wrap">
        <MapView data={data} selectedId={selectedId} focus={focus} onSelect={setSelectedId} />
        <div className="legend" aria-label="Map legend">
          <span><i className="dot dot-high" /> released</span>
          <span><i className="dot dot-medium" /> waiting for review</span>
          <span><i className="dot dot-low" /> rejected</span>
          <span><i className="dot dot-ring" /> approximate position</span>
        </div>
        {view === 'review' && (
          <ReviewQueue items={queue} loading={loading} selectedId={selectedId} threshold={stats?.threshold ?? 0.9} onSelect={setSelectedId} />
        )}
      </main>

      {detail && (
        <DetailPanel key={detail.borehole.borehole_id} detail={detail} onClose={() => setSelectedId(null)} onReviewChange={onReviewChange} />
      )}
    </div>
  )
}
