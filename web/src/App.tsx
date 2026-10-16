import { useEffect, useMemo, useState } from 'react'
import { fetchBoreholes, fetchDetail, fetchStats, setReviewed } from './api'
import { LithologyPatterns, lithStyle, patternId } from './lithology'
import { DetailPanel } from './components/DetailPanel'
import { MapView } from './components/MapView'
import { ResultList } from './components/ResultList'
import type { Detail, FeatureCollection, Filters, Stats } from './types'

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }

export default function App() {
  const [filters, setFilters] = useState<Filters>({ q: '', lithology: [], minConfidence: null, issuesOnly: false })
  const [query, setQuery] = useState('')
  const [data, setData] = useState<FeatureCollection>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: query })), 250)
    return () => clearTimeout(t)
  }, [query])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchBoreholes(filters)
      .then((fc) => { if (!cancelled) { setData(fc); setError(null) } })
      .catch((e) => !cancelled && setError(`Could not load boreholes (${e.message}). Is the API running on port 8000?`))
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [filters])

  useEffect(() => { fetchStats().then(setStats).catch(() => undefined) }, [])

  useEffect(() => {
    if (!selectedId) { setDetail(null); return }
    fetchDetail(selectedId).then(setDetail).catch((e) => setError(`Could not load the borehole (${e.message}).`))
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

  const onReviewed = async (reviewed: boolean) => {
    if (!detail) return
    await setReviewed(detail.borehole.borehole_id, reviewed)
    setDetail({ ...detail, borehole: { ...detail.borehole, reviewed } })
    setData((d) => ({ ...d, features: d.features.map((f) => f.properties.id === detail.borehole.borehole_id ? { ...f, properties: { ...f.properties, reviewed: reviewed ? 1 : 0 } } : f) }))
  }

  return (
    <div className="app">
      <svg width={0} height={0} style={{ position: 'absolute' }} aria-hidden="true"><LithologyPatterns /></svg>

      <header className="topbar">
        <h1 className="brand">Borehole Atlas</h1>
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
            <strong>{stats.boreholes.toLocaleString('en')}</strong> boreholes from <strong>{stats.documents}</strong> documents in{' '}
            <strong>{stats.countries.length}</strong> countries
          </p>
        )}
      </header>

      <aside className="sidebar">
        <div className="filters">
          <div className="filter-row">
            <label>
              Position quality
              <select
                value={filters.minConfidence ?? ''}
                onChange={(e) => setFilters((f) => ({ ...f, minConfidence: e.target.value ? Number(e.target.value) : null }))}
              >
                <option value="">Any</option>
                <option value="0.4">Medium or better</option>
                <option value="0.7">High only</option>
              </select>
            </label>
            <label className="check">
              <input type="checkbox" checked={filters.issuesOnly} onChange={(e) => setFilters((f) => ({ ...f, issuesOnly: e.target.checked }))} />
              Needs checking
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
          <a href="/api/export.csv" className="link">Export CSV</a>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <ResultList features={data.features} selectedId={selectedId} onSelect={setSelectedId} loading={loading} />
      </aside>

      <main className="map-wrap">
        <MapView data={data} selectedId={selectedId} focus={focus} onSelect={setSelectedId} />
        <div className="legend" aria-label="Map legend">
          <span><i className="dot dot-high" /> position high</span>
          <span><i className="dot dot-medium" /> medium</span>
          <span><i className="dot dot-low" /> low</span>
          <span><i className="dot dot-ring" /> from place name</span>
        </div>
      </main>

      {detail && <DetailPanel detail={detail} onClose={() => setSelectedId(null)} onReviewed={onReviewed} />}
    </div>
  )
}
