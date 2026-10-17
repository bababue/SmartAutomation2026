import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { fetchBoreholes, fetchBoreholesById, fetchDetail, fetchMapPoints, fetchQueue, fetchStats, fetchUploadStatus, postUpload } from './api'
import { LithologyPatterns } from './lithology'
import { BrandLogo } from './components/BrandLogo'
import { DetailPanel } from './components/DetailPanel'
import { MapView } from './components/MapView'
import { ResultList } from './components/ResultList'
import { ReviewQueue } from './components/ReviewQueue'
import { SectionView, orderAlongLine } from './components/SectionView'
import { STATUS_LABEL } from './review'
import type { QueueNav } from './components/ReviewBox'
import type { Detail, DetailUpdate, Feature, FeatureCollection, Filters, QueueItem, Stats } from './types'
import { readUrl, writeUrl, type View } from './url'
import { SheetResizer } from './components/SheetResizer'
import { FilterBar } from './components/FilterBar'
import { AppMenu } from './components/AppMenu'

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }
const THEME_KEY = 'borehole-atlas.theme'

function loadDark(): boolean {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    if (saved) return saved === 'dark'
  } catch { /* storage blocked */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

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
  const [blockingOnly, setBlockingOnly] = useState(initial.blockingOnly)
  const [uploadJob, setUploadJob] = useState<{ id: string; label: string } | null>(null)
  const [uploading, setUploading] = useState(false) // from the first byte sent until the job is done or failed
  const [listOpen, setListOpen] = useState(initial.listOpen)
  const [sheetWidth, setSheetWidth] = useState<number | null>(null)  // null: default split; a reload resets it
  const beforeSource = useRef<{ listOpen: boolean; sheetWidth: number | null }>({ listOpen: true, sheetWidth: null })
  // The source page needs the room: opening it hides the list and widens the sheet so the record
  // keeps a readable width; closing it restores both.
  const onSourceToggle = useCallback((open: boolean) => {
    if (open) {
      beforeSource.current = { listOpen, sheetWidth }
      const current = document.querySelector('.detail')?.getBoundingClientRect().width ?? 0
      setListOpen(false)
      setSheetWidth(Math.round(Math.max(current, Math.min(window.innerWidth * 0.75, window.innerWidth - 200))))
    } else {
      if (beforeSource.current.listOpen) setListOpen(true)
      setSheetWidth(beforeSource.current.sheetWidth)
    }
  }, [listOpen, sheetWidth])
  const [uploadNote, setUploadNote] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const [dark, setDark] = useState(loadDark)

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    try { localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light') } catch { /* private mode: the choice lasts this visit */ }
  }, [dark])

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
    const url = writeUrl({ view, selectedId, sectionIds, listOpen, filters, blockingOnly })
    const shown = readUrl(window.location.search)
    if (url === (window.location.search || window.location.pathname)) return
    if (shown.view !== view || shown.selectedId !== selectedId) window.history.pushState(null, '', url)
    else window.history.replaceState(null, '', url)
  }, [view, selectedId, sectionIds, listOpen, filters, blockingOnly])

  useEffect(() => {
    const onPop = () => {
      const s = readUrl(window.location.search)
      setView(s.view)
      setSelectedId(s.selectedId)
      setSectionIds(s.sectionIds)
      setBlockingOnly(s.blockingOnly)
      setListOpen(s.listOpen)
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
    fetchQueue(blockingOnly, ctrl.signal).then(({ items, total }) => { setQueue(items); setQueueTotal(total) }).catch(() => undefined)
    fetchStats().then(setStats).catch(() => undefined)
    return () => ctrl.abort()
  }, [refresh, blockingOnly])

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
    filters.lithology.forEach(({ key }) => counts.set(key, counts.get(key) ?? 0))
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

  const onReviewChange = useCallback((update: DetailUpdate) => {
    setDetail((d) => (d && d.borehole.borehole_id === update.borehole.borehole_id ? { ...d, ...update } : d))
    setRefresh((n) => n + 1)
  }, [])

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

  // The start page: map view, default filters, nothing open; the reload fits the map to all matches.
  const goHome = useCallback(() => {
    const start = readUrl('')
    setView(start.view)
    setSelectedId(null)
    setSectionIds([])
    setBlockingOnly(false)
    setListOpen(true)
    setFilters(start.filters)
    setQuery('')
    setRefresh((n) => n + 1)
  }, [])

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

  const startUpload = useCallback((file: File) => {
    setUploading(true)
    setUploadNote(`Uploading ${file.name}…`)
    postUpload(file)
      .then((res) => {
        const label = `${res.pages} page${res.pages === 1 ? '' : 's'} of ${file.name}`
        setUploadJob({ id: res.job_id, label })
        setUploadNote(`Processing ${label}…`)
      })
      .catch((err) => { setUploading(false); setUploadNote(`Could not upload ${file.name}: ${err.message}`) })
  }, [])

  const onFileChosen = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-choosing the same file later
    if (file) startUpload(file)
  }, [startUpload])

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file && !uploading) startUpload(file)
  }, [startUpload, uploading])

  // Poll until the document is classified, extracted and stored (or fails).
  useEffect(() => {
    if (!uploadJob) return
    const started = Date.now()
    const finish = (note: string) => { clearInterval(t); setUploadJob(null); setUploading(false); setUploadNote(note) }
    const t = setInterval(() => {
      fetchUploadStatus(uploadJob.id).then((status) => {
        if (status.status === 'processing') {
          setUploadNote(`Processing ${uploadJob.label}… ${Math.round((Date.now() - started) / 1000)} s`)
        } else if (status.status === 'error') {
          finish(`Upload failed: ${status.error}`)
        } else if (status.boreholes.length === 0) {
          finish('Processed, but no borehole was recognised in the document.')
        } else {
          const [first] = status.boreholes
          finish(`Added ${status.boreholes.map((b) => `${b.name} (${STATUS_LABEL[b.review_status].toLowerCase()})`).join(', ')}`)
          setSelectedId(first.id)
          setRefresh((n) => n + 1) // reload the map, list and queue
        }
      }).catch(() => finish('Lost track of the upload.'))
    }, 1500)
    return () => clearInterval(t)
  }, [uploadJob])

  return (
    <div
      className={`app${listOpen ? '' : ' list-closed'}`}
      style={sheetWidth ? { '--sheet-w': `${sheetWidth}px` } as CSSProperties : undefined}
      onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } }}
      onDragLeave={(e) => { if (!e.relatedTarget) setDragging(false) }}
      onDrop={onDrop}
    >
      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          {uploading ? 'An upload is still being processed' : 'Drop a borehole log to add it (PDF, image or DOCX)'}
        </div>
      )}
      <svg width={0} height={0} style={{ position: 'absolute' }} aria-hidden="true"><LithologyPatterns /></svg>

      <header className="topbar">
        <div className="topbar-side">
          <BrandLogo onHome={goHome} />
          {stats?.demo && <span className="demo-flag" title="The API serves data/demo.sqlite: synthetic records without source pages">Synthetic demo data</span>}
        </div>
        <label className="search">
          <span className="visually-hidden">Search boreholes</span>
          <input
            type="search"
            placeholder="Search names, projects, places or layer descriptions, e.g. granite"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="topbar-side topbar-end">
          <input ref={fileInput} type="file" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.docx" className="visually-hidden"
            onChange={onFileChosen} aria-label="Upload a borehole log" />
          {uploadNote && <p className="upload-note" role="status" title={uploadNote}>{uploadNote}</p>}
          {stats && (
            <p className="stats">
              <strong>{stats.boreholes.toLocaleString('en')}</strong> boreholes,{' '}
              <strong>{(stats.status.auto_approved + stats.status.approved).toLocaleString('en')}</strong> released
            </p>
          )}
          <AppMenu inReview={view === 'review'} uploading={uploading} dark={dark}
            onToggleReview={() => setView(view === 'review' ? 'map' : 'review')}
            onUpload={() => fileInput.current?.click()} onToggleDark={() => setDark(!dark)} />
        </div>
      </header>

      <button className="edge-tab list-toggle" onClick={() => setListOpen(!listOpen)} aria-expanded={listOpen} aria-controls="sidebar"
        aria-label={listOpen ? 'Hide filters and results' : 'Show filters and results'} title={listOpen ? 'Hide list' : 'Show list'}>
        {listOpen ? '‹' : '›'}
      </button>
      <aside className="sidebar" id="sidebar" inert={!listOpen}>
        <FilterBar filters={filters} setFilters={setFilters} lithologies={lithologies} />
        <div className="result-head">
          <span>{loading ? 'Loading…' : `${(data.total ?? data.features.length).toLocaleString('en')} found`}</span>
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
        <MapView data={points} section={sectionLine} selectedId={selectedId} focus={focus} onSelect={setSelectedId} sheetWidth={detail ? sheetWidth : null} dark={dark} />
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
          <ReviewQueue items={queue} total={queueTotal} loading={loading} selectedId={selectedId} threshold={stats?.threshold ?? 0.9}
            blockingOnly={blockingOnly} onBlockingOnly={setBlockingOnly} onSelect={setSelectedId}
            onChanged={() => setRefresh((n) => n + 1)} onClose={() => setView('map')} />
        )}
      </main>

      {detail && <SheetResizer onResize={setSheetWidth} />}
      {detail && (
        <DetailPanel
          key={detail.borehole.borehole_id}
          detail={detail}
          onClose={() => setSelectedId(null)}
          onReviewChange={onReviewChange}
          nav={nav}
          inSection={sectionIds.includes(detail.borehole.borehole_id)}
          onToggleSection={() => toggleSection(detail.borehole.borehole_id)}
          onSourceToggle={onSourceToggle}
          onSelect={setSelectedId}
        />
      )}
    </div>
  )
}
