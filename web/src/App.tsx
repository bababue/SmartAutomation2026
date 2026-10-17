import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchBoreholes, fetchBoreholesById, fetchDetail, fetchMapPoints, fetchQueue, fetchStats, fetchUploadStatus, postUpload } from './api'
import { LithologyPatterns, lithStyle, patternId } from './lithology'
import { DetailPanel } from './components/DetailPanel'
import { MapView } from './components/MapView'
import { ResultList } from './components/ResultList'
import { ReviewQueue } from './components/ReviewQueue'
import { SectionView, orderAlongLine } from './components/SectionView'
import type { QueueNav } from './components/ReviewBox'
import type { Detail, DetailUpdate, Feature, FeatureCollection, Filters, QueueItem, Stats, StatusFilter } from './types'
import { readUrl, writeUrl, type View } from './url'

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }

export default function App() {
  const [initial] = useState(() => readUrl(window.location.search))
  const [view, setView] = useState<View>(initial.view)
  const [filters, setFilters] = useState<Filters>(initial.filters)
  const [query, setQuery] = useState(initial.filters.q)
  const [data, setData] = useState<FeatureCollection>(EMPTY)  // list: loaded pages of summaries
  const [points, setPoints] = useState<FeatureCollection>(EMPTY)  // map: every match, slim
  const [loadingMore, setLoadingMore] = useState(false)
  const [known, setKnown] = useState<Map<string, Feature>>(new Map())  // full features seen, for the section
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [queueTotal, setQueueTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(initial.selectedId)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [refresh, setRefresh] = useState(0) // bump after a review decision
  const [sectionIds, setSectionIds] = useState<string[]>(initial.sectionIds)
  const [uploadJobId, setUploadJobId] = useState<string | null>(null)
  const [uploadNote, setUploadNote] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: query })), 250)
    return () => clearTimeout(t)
  }, [query])

  // Full features of every loaded page, so the section view keeps holes from earlier pages.
  const remember = useCallback((features: Feature[]) => setKnown((k) => {
    const next = new Map(k)
    features.forEach((f) => next.set(f.properties.id, f))
    return next
  }), [])

  // Section holes from a URL need not be in the loaded list pages.
  const restoreSection = useCallback((ids: string[]) => {
    if (ids.length) fetchBoreholesById(ids).then((page) => remember(page.features)).catch(() => undefined)
  }, [remember])

  useEffect(() => { restoreSection(initial.sectionIds) }, [initial, restoreSection])

  // Page and selection changes get a history entry, so Back closes a borehole or leaves the queue.
  useEffect(() => {
    const url = writeUrl({ view, selectedId, sectionIds, filters })
    const shown = readUrl(window.location.search)
    if (url === (window.location.search || window.location.pathname)) return
    if (shown.view !== view || shown.selectedId !== selectedId) window.history.pushState(null, '', url)
    else window.history.replaceState(null, '', url)
  }, [view, selectedId, sectionIds, filters])

  useEffect(() => {
    const onPop = () => {
      const s = readUrl(window.location.search)
      setView(s.view)
      setSelectedId(s.selectedId)
      setSectionIds(s.sectionIds)
      setFilters(s.filters)
      setQuery(s.filters.q)
      restoreSection(s.sectionIds)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [restoreSection])

  useEffect(() => {
    const ctrl = new AbortController()
    setLoading(true)
    Promise.all([fetchBoreholes(filters, 0, 200, ctrl.signal), fetchMapPoints(filters, ctrl.signal)])
      .then(([page, map]) => { setData(page); remember(page.features); setPoints(map); setError(null) })
      .catch((e) => e.name !== 'AbortError' && setError(`Could not load boreholes (${e.message}). Is the API running on port 8000?`))
      .finally(() => !ctrl.signal.aborted && setLoading(false))
    return () => ctrl.abort()
  }, [filters, refresh, remember])

  const loadMore = useCallback(() => {
    setLoadingMore(true)
    fetchBoreholes(filters, data.features.length, 200)
      .then((page) => { setData((d) => ({ ...page, features: [...d.features, ...page.features] })); remember(page.features) })
      .catch((e) => setError(`Could not load more boreholes (${e.message}).`))
      .finally(() => setLoadingMore(false))
  }, [filters, data.features.length, remember])

  useEffect(() => {
    const ctrl = new AbortController()
    fetchQueue(ctrl.signal).then(({ items, total }) => { setQueue(items); setQueueTotal(total) }).catch(() => undefined)
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

  // Counts over all matches, from the server (the list may be one page).
  const lithologies = useMemo(() => {
    const counts = new Map<string, number>(data.facets?.lithologies ?? [])
    filters.lithology.forEach((l) => counts.set(l, counts.get(l) ?? 0))
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [data, filters.lithology])

  const focus = useMemo(() => {
    const borehole = detail?.borehole
    const loc = borehole?.location
    if (!loc || loc.lat == null || loc.lon == null) return null
    const depth = Math.max(borehole.depth_m ?? 0, ...borehole.layers.map((layer) => layer.bottom_m))
    return { lat: loc.lat, lon: loc.lon, accuracy_m: loc.accuracy_m,
      approximate: loc.method === 'geocoded_place', hasDepth: depth > 0 }
  }, [detail])

  const toggleLith = (key: string) =>
    setFilters((f) => ({ ...f, lithology: f.lithology.includes(key) ? f.lithology.filter((l) => l !== key) : [...f.lithology, key] }))

  const onReviewChange = useCallback((update: DetailUpdate) => {
    setDetail((d) => (d && d.borehole.borehole_id === update.borehole.borehole_id ? { ...d, ...update } : d))
    setRefresh((n) => n + 1)
  }, [])

  const pending = stats?.status.pending_review ?? queue.length

  // Position in the review queue, for "approve and next" and arrow-key navigation.
  const nav: QueueNav | null = useMemo(() => {
    const i = queue.findIndex((q) => q.id === selectedId)
    if (i < 0) return null
    const prevId = queue[i - 1]?.id, nextId = queue[i + 1]?.id
    return {
      position: i + 1,
      total: queue.length,
      prev: prevId ? () => setSelectedId(prevId) : null,
      next: nextId ? () => setSelectedId(nextId) : null,
      // The decided record leaves the queue; continue with its neighbour.
      advance: () => setSelectedId(nextId ?? prevId ?? null),
    }
  }, [queue, selectedId])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (e.key === 'Escape') setSelectedId(null)
      if (e.key === 'ArrowRight' && nav?.next) { e.preventDefault(); nav.next() }
      if (e.key === 'ArrowLeft' && nav?.prev) { e.preventDefault(); nav.prev() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nav])

  const toggleSection = useCallback(
    (id: string) => setSectionIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id])),
    [],
  )
  const sectionFeatures = useMemo(
    () => sectionIds.map((id) => known.get(id)).filter((f): f is NonNullable<typeof f> => !!f),
    [sectionIds, known],
  )
  const sectionLine = useMemo(
    () => orderAlongLine(sectionFeatures).map((f) => f.geometry.coordinates),
    [sectionFeatures],
  )

  const onFileChosen = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-choosing the same file later
    if (!file) return
    setUploadNote(`Uploading ${file.name}…`)
    postUpload(file)
      .then((res) => { setUploadJobId(res.job_id); setUploadNote(`Processing ${res.pages} page${res.pages === 1 ? '' : 's'} of ${file.name}…`) })
      .catch((err) => setUploadNote(`Could not upload ${file.name}: ${err.message}`))
  }, [])

  // Poll until the document is classified, extracted and stored (or fails).
  useEffect(() => {
    if (!uploadJobId) return
    const t = setInterval(() => {
      fetchUploadStatus(uploadJobId).then((status) => {
        if (status.status === 'processing') return
        clearInterval(t)
        setUploadJobId(null)
        if (status.status === 'error') {
          setUploadNote(`Upload failed: ${status.error}`)
        } else if (status.boreholes.length === 0) {
          setUploadNote('Processed, but no borehole was recognised in the document.')
        } else {
          const [first] = status.boreholes
          setUploadNote(`Added ${status.boreholes.map((b) => b.name).join(', ')}`)
          setSelectedId(first.id)
          setRefresh((n) => n + 1) // reload the map, list and queue
        }
      }).catch(() => { clearInterval(t); setUploadJobId(null); setUploadNote('Lost track of the upload.') })
    }, 1500)
    return () => clearInterval(t)
  }, [uploadJobId])

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
        <button className="btn" onClick={() => fileInput.current?.click()} disabled={!!uploadJobId}>
          {uploadJobId ? 'Processing…' : 'Upload document'}
        </button>
        <input ref={fileInput} type="file" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.docx" className="visually-hidden"
          onChange={onFileChosen} aria-label="Upload a borehole log" />
        {uploadNote && <p className="upload-note" role="status">{uploadNote}</p>}
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
          {filters.lithology.length > 0 && (
            <div className="filter-row filter-depth">
              <label>
                Between (m)
                <input inputMode="decimal" placeholder="0" value={filters.depthFrom ?? ''}
                  onChange={(e) => setFilters((f) => ({ ...f, depthFrom: e.target.value === '' ? null : Number(e.target.value.replace(',', '.')) }))} />
              </label>
              <label>
                and (m)
                <input inputMode="decimal" placeholder="any" value={filters.depthTo ?? ''}
                  onChange={(e) => setFilters((f) => ({ ...f, depthTo: e.target.value === '' ? null : Number(e.target.value.replace(',', '.')) }))} />
              </label>
            </div>
          )}
          <div className="filter-row filter-lab">
            <label>
              Lab value
              <select value={filters.lab ?? ''} onChange={(e) => setFilters((f) => ({ ...f, lab: e.target.value || null }))}>
                <option value="">Any</option>
                <option value="ucs">UCS (MPa)</option>
                <option value="cai">CAI</option>
                <option value="tensile_strength">Tensile strength (MPa)</option>
                <option value="point_load_index">Point load Is50 (MPa)</option>
                <option value="water_content">Water content (%)</option>
              </select>
            </label>
            {filters.lab && (
              <label>
                at least
                <input inputMode="decimal" placeholder="e.g. 80" value={filters.labMin ?? ''}
                  onChange={(e) => setFilters((f) => ({ ...f, labMin: e.target.value === '' ? null : Number(e.target.value.replace(',', '.')) }))} />
              </label>
            )}
          </div>
        </div>
        <div className="result-head">
          <span>{loading ? 'Loading…' : `${(data.total ?? data.features.length).toLocaleString('en')} found`}</span>
          <span className="export-links">
            Export released:{' '}
            <a href="/api/export.xlsx" className="link" title="Released records in Herrenknecht's BoreholeDataFormat">Excel</a>
            {' · '}
            <a href="/api/export.csv" className="link" title="Released records, one row per layer">CSV</a>
          </span>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <ResultList features={data.features} selectedId={selectedId} onSelect={setSelectedId} loading={loading} sectionIds={sectionIds} onToggleSection={toggleSection} />
        {!loading && data.total != null && data.features.length < data.total && (
          <button className="btn load-more" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? 'Loading…' : `Show ${Math.min(200, data.total - data.features.length)} more of ${(data.total - data.features.length).toLocaleString('en')}`}
          </button>
        )}
      </aside>

      <main className={`map-wrap${sectionFeatures.length ? ' map-wrap-section' : ''}`}>
        <MapView data={points} section={sectionLine} selectedId={selectedId} focus={focus} onSelect={setSelectedId} />
        <div className="legend" aria-label="Map legend">
          <span><i className="dot dot-high" /> released</span>
          <span><i className="dot dot-medium" /> waiting for review</span>
          <span><i className="dot dot-low" /> rejected</span>
          <span><i className="dot dot-ring" /> approximate position</span>
          <span>3D columns: layer heights at 1:1 scale; patterns show lithology, top cap shows release state</span>
        </div>
        {sectionFeatures.length > 0 && (
          <SectionView
            features={sectionFeatures}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onRemove={toggleSection}
            onClear={() => setSectionIds([])}
          />
        )}
        {view === 'review' && (
          <ReviewQueue items={queue} total={queueTotal} loading={loading} selectedId={selectedId} threshold={stats?.threshold ?? 0.9} onSelect={setSelectedId}
            onChanged={() => setRefresh((n) => n + 1)} />
        )}
      </main>

      {detail && (
        <DetailPanel
          key={detail.borehole.borehole_id}
          detail={detail}
          onClose={() => setSelectedId(null)}
          onReviewChange={onReviewChange}
          nav={nav}
          inSection={sectionIds.includes(detail.borehole.borehole_id)}
          onToggleSection={() => toggleSection(detail.borehole.borehole_id)}
        />
      )}
    </div>
  )
}
