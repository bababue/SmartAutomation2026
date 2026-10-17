import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { fetchBoreholes, fetchBoreholesById, fetchDetail, fetchMapPoints, fetchQueue, fetchStats } from './api'
import { useStoredFlag } from './hooks'
import { LithologyPatterns } from './lithology'
import { BrandLogo } from './components/BrandLogo'
import { DetailPanel } from './components/DetailPanel'
import { MapView } from './components/MapView'
import { ResultList } from './components/ResultList'
import { RoutePanel } from './components/RoutePanel'
import { TunnelList } from './components/TunnelList'
import { ReviewQueue } from './components/ReviewQueue'
import { StatsView } from './components/StatsView'
import { SectionView } from './components/SectionView'
import type { QueueNav } from './components/ReviewBox'
import type { Detail, DetailUpdate, Feature, FeatureCollection, Filters, QueueItem, Stats } from './types'
import { DEFAULT_FILTERS, readUrl, writeUrl, type View } from './url'
import { detailSheetRect, isTyping } from './dom'
import { SheetResizer } from './components/SheetResizer'
import { FilterBar } from './components/FilterBar'
import { AppMenu } from './components/AppMenu'
import { UploadQueue } from './components/UploadQueue'
import { useUpload } from './upload'
import { loadTunnels, nearbyBoreholes, projectToRoute, saveTunnels, tunnelProject, validateRouteCoordinates,
  type RoutePoint, type SavedTunnel, type TunnelRoute } from './tunnelRoute'

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }
const EMPTY_IDS: string[] = []
const prefersDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches
const always = () => true

export default function App() {
  const [initial] = useState(() => readUrl(window.location.search))
  const [view, setView] = useState<View>(initial.view)
  const [routeActive, setRouteActive] = useState(initial.routeActive)
  const [tunnels, setTunnels] = useState<SavedTunnel[]>(loadTunnels)
  const [activeTunnelId, setActiveTunnelId] = useState<string | null>(initial.tunnelId ?? (initial.routeActive ? tunnels[0]?.id ?? null : null))
  const activeTunnel = tunnels.find((tunnel) => tunnel.id === activeTunnelId) ?? null
  const route = activeTunnel?.route ?? null
  const routeHoleIds = activeTunnel?.holeIds ?? EMPTY_IDS
  const [routeDraft, setRouteDraft] = useState<RoutePoint[] | null>(null)
  const [routePoints, setRoutePoints] = useState<FeatureCollection>(EMPTY)
  const [routeLoadedFor, setRouteLoadedFor] = useState<number | null>(null)
  const [routeError, setRouteError] = useState<string | null>(null)
  const [filters, setFilters] = useState<Filters>(initial.filters)
  const [query, setQuery] = useState(initial.filters.q)
  const [data, setData] = useState<FeatureCollection>(EMPTY)  // list: loaded pages of summaries
  const [points, setPoints] = useState<FeatureCollection>(EMPTY)  // map: every match, slim
  const [loadingMore, setLoadingMore] = useState(false)
  const [known, setKnown] = useState<Map<string, Feature>>(new Map())  // full features seen, for the route profile
  const requestedHoleIds = useRef(new Set<string>())
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [queueTotal, setQueueTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(initial.selectedId)
  const [detail, setDetail] = useState<Detail | null>(null)
  // Closing drops the record, so a later selection never shows it while its own loads; switching keeps it until then.
  if (!selectedId && detail) setDetail(null)
  const [refresh, setRefresh] = useState(0)  // bump to reload map, list, queue and stats
  const [blockingOnly, setBlockingOnly] = useState(initial.blockingOnly)
  const [auditOnly, setAuditOnly] = useState(initial.auditOnly)
  const [listOpen, setListOpen] = useState(initial.routeActive ? true : initial.listOpen)
  const [sheetWidth, setSheetWidth] = useState<number | null>(null)  // null: default split; a reload resets it
  const beforeSource = useRef<{ listOpen: boolean; sheetWidth: number | null }>({ listOpen: true, sheetWidth: null })
  // The source page needs the room: opening it hides the list and widens the sheet so the record
  // keeps a readable width; closing it restores both.
  const onSourceToggle = useCallback((open: boolean) => {
    if (open) {
      beforeSource.current = { listOpen, sheetWidth }
      const current = detailSheetRect()?.width ?? 0
      setListOpen(false)
      setSheetWidth(Math.round(Math.max(current, Math.min(window.innerWidth * 0.75, window.innerWidth - 200))))
    } else {
      if (beforeSource.current.listOpen) setListOpen(true)
      setSheetWidth(beforeSource.current.sheetWidth)
    }
  }, [listOpen, sheetWidth])
  const [dark, setDark] = useStoredFlag('borehole-atlas.theme', 'dark', 'light', prefersDark)
  const [columns3d, setColumns3d] = useStoredFlag('borehole-atlas.columns3d', 'on', 'off', always)

  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light' }, [dark])

  useEffect(() => { saveTunnels(tunnels) }, [tunnels])

  const updateActiveTunnel = useCallback((change: Partial<SavedTunnel>) => {
    setTunnels((items) => items.map((item) => item.id === activeTunnelId ? { ...item, ...change } : item))
  }, [activeTunnelId])
  const setRouteHoleIds = useCallback((value: string[] | ((ids: string[]) => string[])) => {
    setTunnels((items) => items.map((item) => item.id === activeTunnelId
      ? { ...item, holeIds: typeof value === 'function' ? value(item.holeIds) : value } : item))
  }, [activeTunnelId])
  const setRoute = useCallback((next: TunnelRoute) => {
    updateActiveTunnel({ name: next.name, route: next })
  }, [updateActiveTunnel])

  useEffect(() => {
    if (!routeActive) return
    const controller = new AbortController()
    fetchMapPoints(DEFAULT_FILTERS, controller.signal)
      .then((collection) => { setRoutePoints(collection); setRouteError(null); setRouteLoadedFor(refresh) })
      .catch((cause) => {
        if (cause.name === 'AbortError') return
        setRouteError(`Could not load nearby boreholes (${cause.message}).`)
        setRouteLoadedFor(refresh)
      })
    return () => controller.abort()
  }, [routeActive, refresh])
  const routeLoading = routeActive && routeLoadedFor !== refresh

  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: query })), 250)
    return () => clearTimeout(t)
  }, [query])

  // Keep previously loaded boreholes available for the route profile.
  const remember = useCallback((features: Feature[]) => setKnown((k) => {
    const next = new Map(k)
    features.forEach((f) => next.set(f.properties.id, f))
    return next
  }), [])

  // Tunnel projects need summaries for every saved tunnel, including those outside the current list page.
  useEffect(() => {
    const requested = requestedHoleIds.current
    const missing = [...new Set([...tunnels.flatMap((tunnel) => tunnel.holeIds), ...(selectedId ? [selectedId] : [])])]
      .filter((id) => !known.has(id) && !requested.has(id))
    if (!missing.length) return
    const controller = new AbortController()
    missing.forEach((id) => requested.add(id))
    const batches = Array.from({ length: Math.ceil(missing.length / 100) }, (_, index) => missing.slice(index * 100, (index + 1) * 100))
    let settled = false
    Promise.all(batches.map((ids) => fetchBoreholesById(ids, controller.signal)))
      .then((pages) => { settled = true; remember(pages.flatMap((page) => page.features)) })
      .catch((cause) => { if (cause.name !== 'AbortError') { settled = true; setError(`Could not load route boreholes (${cause.message}).`) } })
    return () => {
      controller.abort()
      if (!settled) missing.forEach((id) => requested.delete(id))
    }
  }, [tunnels, selectedId, known, remember])

  // Page and selection changes get a history entry, so Back closes a borehole or leaves the queue.
  useEffect(() => {
    const url = writeUrl({ view, routeActive, tunnelId: activeTunnelId, selectedId, listOpen, filters, blockingOnly, auditOnly })
    const shown = readUrl(window.location.search)
    if (url === (window.location.search || window.location.pathname)) return
    if (shown.view !== view || shown.routeActive !== routeActive ||
      (routeActive && shown.tunnelId !== activeTunnelId) || shown.selectedId !== selectedId) window.history.pushState(null, '', url)
    else window.history.replaceState(null, '', url)
  }, [view, routeActive, activeTunnelId, selectedId, listOpen, filters, blockingOnly, auditOnly])

  useEffect(() => {
    const onPop = () => {
      const s = readUrl(window.location.search)
      setView(s.view)
      setRouteActive(s.routeActive)
      setActiveTunnelId(s.tunnelId)
      setSelectedId(s.selectedId)
      setBlockingOnly(s.blockingOnly)
      setAuditOnly(s.auditOnly)
      setListOpen(s.routeActive ? true : s.listOpen)
      setFilters(s.filters)
      setQuery(s.filters.q)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  // The list is loading until a response for the current filters and refresh has landed.
  const [listFor, setListFor] = useState<{ filters: Filters; refresh: number } | null>(null)
  const moreRequest = useRef<AbortController | null>(null)
  useEffect(() => {
    moreRequest.current?.abort()
    const ctrl = new AbortController()
    const request = { filters, refresh }
    Promise.all([fetchBoreholes(filters, 0, 200, ctrl.signal), fetchMapPoints(filters, ctrl.signal)])
      .then(([page, map]) => { setData(page); remember(page.features); setPoints(map); setError(null); setListFor(request) })
      .catch((e) => {
        if (e.name === 'AbortError') return
        setError(`Could not load boreholes (${e.message}). Is the API running on port 8000?`)
        setListFor(request)
      })
    return () => ctrl.abort()
  }, [filters, refresh, remember])
  const loading = listFor?.filters !== filters || listFor.refresh !== refresh

  const loadMore = useCallback(() => {
    const ctrl = new AbortController()
    moreRequest.current = ctrl
    setLoadingMore(true)
    fetchBoreholes(filters, data.features.length, 200, ctrl.signal)
      .then((page) => { setData((d) => ({ ...page, features: [...d.features, ...page.features] })); remember(page.features) })
      .catch((e) => e.name !== 'AbortError' && setError(`Could not load more boreholes (${e.message}).`))
      .finally(() => setLoadingMore(false))
  }, [filters, data.features.length, remember])

  // The queue is loading until a response for the current request has landed.
  const queueKey = `${refresh}|${blockingOnly}|${auditOnly}`
  const [queueLoadedKey, setQueueLoadedKey] = useState<string | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    fetchQueue(blockingOnly, ctrl.signal, 200, auditOnly)
      .then(({ items, total }) => { setQueue(items); setQueueTotal(total); setQueueLoadedKey(queueKey) })
      .catch((e) => { if (e.name !== 'AbortError') setQueueLoadedKey(queueKey) })
    fetchStats().then(setStats).catch(() => undefined)
    return () => ctrl.abort()
  }, [queueKey, blockingOnly, auditOnly])

  // Only the response for the currently selected borehole may land in state.
  useEffect(() => {
    if (!selectedId) return
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
      if (isTyping(e.target)) return
      if (e.key === 'Escape') setSelectedId(null)
      if (e.key === 'ArrowRight' && nav?.next) { e.preventDefault(); nav.next() }
      if (e.key === 'ArrowLeft' && nav?.prev) { e.preventDefault(); nav.prev() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nav])

  // The start page: map view, default filters, nothing open; the reload fits the map to all matches.
  const goHome = useCallback(() => {
    setView('map')
    setRouteActive(false)
    setRouteDraft(null)
    setSelectedId(null)
    setBlockingOnly(false)
    setAuditOnly(false)
    setListOpen(true)
    setFilters(DEFAULT_FILTERS)
    setQuery('')
    setRefresh((n) => n + 1)
  }, [])

  const toggleRouteHole = useCallback(
    (id: string) => setRouteHoleIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id])),
    [setRouteHoleIds],
  )
  const routeFeatures = useMemo(
    () => routeHoleIds.map((id) => known.get(id)).filter((f): f is NonNullable<typeof f> => !!f),
    [routeHoleIds, known],
  )
  const listFeatures = useMemo(() => {
    const selected = selectedId ? known.get(selectedId) : null
    return selected && !data.features.some((feature) => feature.properties.id === selectedId)
      ? [selected, ...data.features] : data.features
  }, [data.features, known, selectedId])
  const inTunnel = routeActive && view === 'map'
  const activeRoute = inTunnel ? route : null
  const nearby = useMemo(() => activeRoute ? nearbyBoreholes(routePoints.features, activeRoute) : [], [routePoints, activeRoute])
  const routeCandidates = useMemo(() => {
    if (!activeRoute) return nearby
    const ids = new Set(nearby.map((candidate) => candidate.id))
    const otherSelected = routePoints.features.flatMap((feature) => {
      if (!routeHoleIds.includes(feature.properties.id) || ids.has(feature.properties.id)) return []
      return [{ id: feature.properties.id, name: feature.properties.name,
        ...projectToRoute(feature.geometry.coordinates, activeRoute.coordinates) }]
    })
    return [...nearby, ...otherSelected].sort((a, b) => a.chainageM - b.chainageM)
  }, [activeRoute, nearby, routePoints, routeHoleIds])

  const finishRoute = useCallback(() => {
    try {
      const coordinates = validateRouteCoordinates(routeDraft)
      setRoute({ name: activeTunnel?.name || 'Tunnel route', coordinates, corridorM: route?.corridorM ?? 50,
        tunnelMode: route?.tunnelMode ?? 'depth', depthM: route?.depthM ?? null,
        startElevationM: route?.startElevationM ?? null, endElevationM: route?.endElevationM ?? null,
        diameterM: route?.diameterM ?? null })
      setRouteDraft(null)
      setRouteHoleIds([])
      setRouteError(null)
    } catch (cause) { setRouteError(cause instanceof Error ? cause.message : 'Could not save the route.') }
  }, [routeDraft, route, activeTunnel, setRoute, setRouteHoleIds])

  const openTunnel = () => {
    if (!activeTunnelId && tunnels.length === 1) setActiveTunnelId(tunnels[0].id)
    setRouteActive(true)
    setView('map')
    setListOpen(true)
    setSelectedId(null)
  }
  const closeTunnel = () => {
    setRouteActive(false)
    setRouteDraft(null)
  }
  const createTunnel = () => {
    const id = crypto.randomUUID()
    setTunnels((items) => [...items, { id, name: `Tunnel ${items.length + 1}`, route: null, holeIds: [] }])
    setActiveTunnelId(id)
    setRouteDraft(null)
    setSelectedId(null)
    setRouteActive(true)
    setView('map')
    setListOpen(true)
  }

  const onUploaded = useCallback(() => setRefresh((n) => n + 1), [])
  const { batches, usage, cancel, dismiss, active: uploading, dragging, fileInput, onFileChosen, dropTarget } =
    useUpload(onUploaded, setSelectedId)

  return (
    <div
      className={`app${listOpen ? '' : ' list-closed'}${inTunnel && activeTunnel && !selectedId ? ' tunnel-detail-open' : ''}`}
      style={sheetWidth ? { '--sheet-w': `${sheetWidth}px` } as CSSProperties : undefined}
      {...dropTarget}
    >
      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          Drop borehole logs to add them (PDF, image, DOCX, TXT, XLSX or ZIP). Files dropped together are read as one batch.
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
            placeholder="Search names, projects, places or layer descriptions"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="topbar-side topbar-end">
          <input ref={fileInput} type="file" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.docx,.txt,.xlsx,.xlsm,.zip" multiple
            className="visually-hidden" onChange={onFileChosen} aria-label="Upload borehole logs" />
          <UploadQueue batches={batches} usage={usage} onCancel={cancel} onDismiss={dismiss} />
          {stats && (
            <p className="stats">
              <strong>{(stats.status.auto_approved + stats.status.approved).toLocaleString('en')}</strong> of{' '}
              <strong>{stats.boreholes.toLocaleString('en')}</strong> boreholes released
              {stats.boreholes > stats.located && (
                <span className="muted" title="Not on the map or in the list until a reviewer sets the position">
                  {' '}({(stats.boreholes - stats.located).toLocaleString('en')} without position)
                </span>
              )}
            </p>
          )}
          <AppMenu inReview={view === 'review'} inStats={view === 'stats'} uploading={uploading} dark={dark} columns3d={columns3d}
            onToggleReview={() => { setAuditOnly(false); setView(view === 'review' ? 'map' : 'review') }}
            onToggleStats={() => setView(view === 'stats' ? 'map' : 'stats')}
            onCreateTunnel={createTunnel}
            onUpload={() => fileInput.current?.click()} onToggleDark={() => setDark(!dark)} onToggleColumns={() => setColumns3d(!columns3d)} />
        </div>
      </header>

      {!inTunnel && <button className="edge-tab list-toggle" onClick={() => setListOpen(!listOpen)} aria-expanded={listOpen} aria-controls="sidebar"
        aria-label={listOpen ? 'Hide filters and results' : 'Show filters and results'} title={listOpen ? 'Hide list' : 'Show list'}>
        {listOpen ? '‹' : '›'}
      </button>}
      <aside className="sidebar" id="sidebar" inert={!listOpen}>
        {inTunnel ? <TunnelList tunnels={tunnels} boreholes={known} selectedId={activeTunnelId}
          onSelect={(id) => { setActiveTunnelId(id); setRouteDraft(null); setSelectedId(null) }}
          onShowBoreholes={closeTunnel} /> : <>
        <FilterBar filters={filters} setFilters={setFilters} lithologies={lithologies}
          groupLabel={filters.group && (data.facets?.groups.find((g) => g.group === filters.group)?.project ?? filters.group)}
          onShowTunnels={openTunnel} />
        <div className="result-head">
          <span>{loading ? 'Loading…' : `${(data.total ?? data.features.length).toLocaleString('en')} found`}</span>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <ResultList features={listFeatures} groups={data.facets?.groups ?? []} activeGroup={filters.group}
          onShowGroup={(group) => { setSelectedId(null); setFilters((f) => ({ ...f, group })) }}
          selectedId={selectedId} onSelect={setSelectedId} loading={loading} />
        {!loading && data.total != null && data.features.length < data.total && (
          <button className="btn load-more" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? 'Loading…' : 'Show more'}
          </button>
        )}
        </>}
      </aside>

      <main className={`map-wrap${inTunnel ? ' map-wrap-section' : ''}`}>
        <MapView data={points} route={activeRoute} routeDraft={routeDraft}
          onRoutePoint={(point) => setRouteDraft((draft) => draft ? [...draft, point] : draft)}
          selectedId={selectedId} focus={focus} onSelect={(id) => { setSelectedId(id); setListOpen(true) }}
          sheetWidth={detail ? sheetWidth : null} dark={dark} columns3d={columns3d} />
        <div className={`legend${detail ? ' legend-beside-sheet' : ''}`} aria-label="Map legend">
          <span><i className="dot dot-high" /> released</span>
          <span><i className="dot dot-medium" /> waiting for review</span>
          <span><i className="dot dot-low" /> rejected</span>
          <span><i className="dot dot-ring" /> approximate position</span>
        </div>
        {activeRoute ? (
          <SectionView
            features={routeFeatures}
            route={activeRoute}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onRemove={toggleRouteHole}
            onClear={() => setRouteHoleIds([])}
          />
        ) : inTunnel && (
          <section className="section" aria-label="Tunnel profile">
            <header className="section-head"><h2>{activeTunnel?.name || 'Tunnel profile'}</h2></header>
            <div className="section-plot section-empty">
              <svg width="100%" height="290" aria-hidden="true" preserveAspectRatio="none">
                <line x1="58" x2="100%" y1="257" y2="257" className="section-axis" />
                <line x1="58" x2="58" y1="36" y2="257" className="section-axis" />
              </svg>
              <p>Draw a tunnel route to see its profile here.</p>
            </div>
          </section>
        )}
        {view === 'review' && (
          <ReviewQueue items={queue} total={queueTotal} loading={queueLoadedKey !== queueKey} selectedId={selectedId}
            threshold={stats?.threshold ?? 0.9} auditOnly={auditOnly} blockingOnly={blockingOnly} onBlockingOnly={setBlockingOnly} onSelect={setSelectedId}
            onChanged={() => setRefresh((n) => n + 1)} onClose={() => setView('map')} />
        )}
        {view === 'stats' && (
          <StatsView filters={filters} onClose={() => setView('map')}
            onReviewAudit={() => { setAuditOnly(true); setView('review') }}
            onShowGroup={(group) => { setSelectedId(null); setFilters((f) => ({ ...f, group })); setView('map') }} />
        )}
      </main>

      {inTunnel && activeTunnel && !selectedId && <RoutePanel
        name={activeTunnel.name} project={tunnelProject(activeTunnel, known)} route={route} draft={routeDraft} candidates={routeCandidates}
        selectedIds={routeHoleIds} loading={routeLoading} error={routeError}
        onChange={setRoute}
        onRename={(name) => updateActiveTunnel({ name, route: route ? { ...route, name } : null })}
        onImport={(imported) => { setRoute(imported); setRouteDraft(null); setRouteHoleIds([]); setRouteError(null) }}
        onStartDrawing={() => { setRouteDraft([]); setRouteError(null) }}
        onUndo={() => setRouteDraft((draft) => draft ? draft.slice(0, -1) : null)} onFinishDrawing={finishRoute}
        onCancelDrawing={() => setRouteDraft(null)} onSelectNearby={() => setRouteHoleIds(nearby.map((candidate) => candidate.id))}
        onToggleHole={toggleRouteHole}
        onDelete={() => { setTunnels((items) => items.filter((item) => item.id !== activeTunnelId)); setActiveTunnelId(null); setRouteDraft(null) }}
        onClose={() => { setActiveTunnelId(null); setRouteDraft(null) }} />}

      {detail && <SheetResizer onResize={setSheetWidth} />}
      {detail && (
        <DetailPanel
          key={detail.borehole.borehole_id}
          detail={detail}
          onClose={() => setSelectedId(null)}
          onReviewChange={onReviewChange}
          nav={nav}
          onSourceToggle={onSourceToggle}
          onSelect={setSelectedId}
          onFindLike={(like) => {
            setFilters((f) => ({ ...f, group: null, rockMax: like.rock_head_max, waterMax: like.groundwater_max, material: like.material }))
            setSelectedId(null)
          }}
        />
      )}
    </div>
  )
}
