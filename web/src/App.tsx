import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { fetchQueue, fetchStats } from './api'
import { useBoreholeDetail, useBoreholes } from './boreholes'
import { useStoredFlag, useTheme } from './hooks'
import { useEscapeToClose, useListArrowKeys } from './keyboard'
import { LithologyPatterns } from './lithology'
import { useReviewQueue } from './review'
import { useTunnels } from './tunnels'
import { tunnelProject } from './tunnelRoute'
import type { DetailUpdate, ReviewFilters, SimilarGround, Stats, StatusFilter } from './types'
import { useUpload } from './upload'
import { DEFAULT_FILTERS, DEFAULT_REVIEW_FILTERS, useUrlState, type UrlState } from './url'
import { detailSheetRect } from './dom'
import { AppMenu } from './components/AppMenu'
import { BrandLogo } from './components/BrandLogo'
import { DetailPanel } from './components/DetailPanel'
import { FilterBar } from './components/FilterBar'
import { GroupingToggle } from './components/GroupingToggle'
import { MapView } from './components/MapView'
import { ReleaseSummary } from './components/ReleaseSummary'
import { ResultList } from './components/ResultList'
import { ReviewList } from './components/ReviewList'
import { RoutePanel } from './components/RoutePanel'
import { EmptySection, SectionView } from './components/SectionView'
import { SheetResizer } from './components/SheetResizer'
import { StatsView } from './components/StatsView'
import { TunnelList } from './components/TunnelList'
import { UploadQueue } from './components/UploadQueue'

const always = () => true
const QUEUE_NOUN: Record<StatusFilter, string> = { pending: 'waiting', released: 'released', rejected: 'rejected', all: 'listed' }

export default function App() {
  const { view, routeActive, tunnelId, selectedId, listOpen, filters, reviewFilters, auditOnly, statsTab,
    query, setQuery, show, select, setFilters } = useUrlState()
  const inTunnel = routeActive && view === 'map'
  // Statistics take the whole width: no list, no record sheet.
  const inStats = view === 'stats'
  const [refresh, setRefresh] = useState(0)  // bump to reload map, list, queue and stats
  const reload = useCallback(() => setRefresh((n) => n + 1), [])

  const [grouped, setGrouped] = useStoredFlag('borehole-atlas.group-results', 'on', 'off', always)
  const [dark, setDark] = useTheme()
  const [view3d, setView3d] = useStoredFlag('borehole-atlas.view3d', 'on', 'off', always)

  const tunnels = useTunnels(tunnelId, routeActive, inTunnel, refresh)
  const wantedIds = useMemo(() => [...tunnels.saved.flatMap((tunnel) => tunnel.holeIds), ...(selectedId ? [selectedId] : [])],
    [tunnels.saved, selectedId])
  const boreholes = useBoreholes(filters, refresh, wantedIds)
  const { data, loading, known } = boreholes
  const queue = useReviewQueue(reviewFilters, auditOnly, refresh)
  const [stats, setStats] = useState<Stats | null>(null)
  useEffect(() => { fetchStats().then(setStats).catch(() => undefined) }, [refresh])

  const onDetailError = useCallback((notFound: boolean) =>
    show(notFound ? { listOpen: true, selectedId: null } : { listOpen: true }), [show])
  const { detail, error: detailError, applyUpdate, focus } = useBoreholeDetail(selectedId, refresh, onDetailError)
  const notice = detailError ?? boreholes.error

  const closeRecord = useCallback(() => select(null), [select])
  useEscapeToClose(closeRecord)
  const sidebar = useRef<HTMLElement>(null)
  useListArrowKeys(sidebar)

  const { batches, usage, cancel, dismiss, active: uploading, dragging, fileInput, onFileChosen, dropTarget } =
    useUpload(reload, select)

  const [sheetWidth, setSheetWidth] = useState<number | null>(null)  // null: default split; a reload resets it
  const beforeSource = useRef<{ listOpen: boolean; sheetWidth: number | null }>({ listOpen: true, sheetWidth: null })
  // The source page needs the room: opening it hides the list and widens the sheet so the record
  // keeps a readable width; closing it restores both.
  const onSourceToggle = useCallback((open: boolean) => {
    if (open) {
      beforeSource.current = { listOpen, sheetWidth }
      const current = detailSheetRect()?.width ?? 0
      show({ listOpen: false })
      setSheetWidth(Math.round(Math.max(current, Math.min(window.innerWidth * 0.75, window.innerWidth - 200))))
    } else {
      if (beforeSource.current.listOpen) show({ listOpen: true })
      setSheetWidth(beforeSource.current.sheetWidth)
    }
  }, [listOpen, sheetWidth, show])

  // Holes opened from "Similar ground elsewhere", one step per jump; the button shows only on the hole jumped to.
  const [trail, setTrail] = useState<{ from: { id: string; name: string }; to: string }[]>([])
  const back = trail.at(-1)?.to === selectedId ? trail.at(-1) : undefined

  const [hoveredHoleId, setHoveredHoleId] = useState<string | null>(null)
  const routeFeatures = useMemo(
    () => tunnels.holeIds.map((id) => known.get(id)).filter((f): f is NonNullable<typeof f> => !!f),
    [tunnels.holeIds, known],
  )
  const hoveredHole = hoveredHoleId ? routeFeatures.find((feature) => feature.properties.id === hoveredHoleId) : null
  const listFeatures = useMemo(() => {
    const selected = selectedId ? known.get(selectedId) : null
    return selected && !data.features.some((feature) => feature.properties.id === selectedId)
      ? [selected, ...data.features] : data.features
  }, [data.features, known, selectedId])

  const toggleSelected = (id: string) => show((s) => ({ selectedId: s.selectedId === id ? null : id }))
  const showGroup = (group: string | null, patch: Partial<UrlState> = {}) =>
    show((s) => ({ selectedId: null, filters: { ...s.filters, group }, ...patch }))

  // The start page: map view, default filters, nothing open; the reload fits the map to all matches.
  const goHome = () => {
    show({ view: 'map', routeActive: false, selectedId: null, reviewFilters: DEFAULT_REVIEW_FILTERS, auditOnly: false,
      listOpen: true, filters: DEFAULT_FILTERS })
    setQuery('')
    tunnels.cancelDrawing()
    reload()
  }
  const openReview = (patch: Partial<UrlState> = {}) =>
    show({ view: 'review', routeActive: false, selectedId: null, listOpen: true, auditOnly: false, ...patch })
  const toggleReview = () => {
    if (view === 'review') return show({ view: 'map', selectedId: null, auditOnly: false, listOpen: true })
    tunnels.cancelDrawing()
    openReview()
  }
  const showUnplaced = () => {
    const unplaced: ReviewFilters = { ...DEFAULT_REVIEW_FILTERS, position: 'none' }
    openReview({ reviewFilters: unplaced })
    fetchQueue(unplaced, undefined, 1).then(({ items }) => { if (items[0]) select(items[0].id) }).catch(() => undefined)
  }
  const enterTunnels = (id: string | null) =>
    show({ view: 'map', routeActive: true, tunnelId: id, selectedId: null, listOpen: true })
  const toggleTunnels = () => {
    tunnels.cancelDrawing()
    if (inTunnel) show({ routeActive: false })
    else enterTunnels(null)
  }
  const selectTunnel = (id: string | null) => {
    tunnels.cancelDrawing()
    show((s) => ({ tunnelId: s.tunnelId === id ? null : id, selectedId: null }))
  }

  const onReviewChange = useCallback((update: DetailUpdate) => {
    applyUpdate(update)
    reload()
  }, [applyUpdate, reload])
  // After a decision in review mode, the next record of the list opens; a decided waiting record leaves it.
  const openNext = useCallback(() => {
    const i = queue.items.findIndex((q) => q.id === selectedId)
    select(i < 0 ? null : (queue.items[i + 1] ?? queue.items[i - 1])?.id ?? null)
  }, [queue.items, selectedId, select])
  const findLike = (like: NonNullable<SimilarGround['like']>) =>
    show((s) => ({ selectedId: null, filters: { ...s.filters, group: null, rockMax: like.rock_head_max,
      waterMax: like.groundwater_max, material: like.material } }))

  const shownTunnel = inTunnel ? tunnels.active : null
  return (
    <div
      className={`app${listOpen && !inStats ? '' : ' list-closed'}${shownTunnel && !selectedId ? ' tunnel-detail-open' : ''}`}
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
            className="visually-hidden" tabIndex={-1} onChange={onFileChosen} aria-label="Upload borehole logs" />
          <UploadQueue batches={batches} usage={usage} onCancel={cancel} onDismiss={dismiss} />
          {stats && <ReleaseSummary stats={stats} onShowUnplaced={showUnplaced} />}
          <AppMenu inStats={inStats} uploading={uploading} dark={dark} view3d={view3d}
            onToggleStats={() => show({ view: inStats ? 'map' : 'stats' })}
            onUpload={() => fileInput.current?.click()} onToggleDark={() => setDark(!dark)} onToggleView3d={() => setView3d(!view3d)} />
        </div>
      </header>

      {!inStats && <button className="edge-tab list-toggle" onClick={() => show({ listOpen: !listOpen })} aria-expanded={listOpen} aria-controls="sidebar"
        aria-label={listOpen ? 'Hide filters and results' : 'Show filters and results'} title={listOpen ? 'Hide list' : 'Show list'}>
        {listOpen ? '‹' : '›'}
      </button>}
      <aside className="sidebar" id="sidebar" ref={sidebar} inert={!listOpen || inStats}>
        <div className="filters view-tabs">
          <div className="filter-bar" role="group" aria-label="Views">
            <button className={`pill${inTunnel ? ' pill-on pill-mode' : ''}`} onClick={toggleTunnels}
              aria-label={inTunnel ? 'Close tunnels and show boreholes' : 'Show tunnels'}>
              Tunnels {inTunnel && <span aria-hidden="true">×</span>}
            </button>
            <button className={`pill${view === 'review' ? ' pill-on pill-mode' : ''}`} onClick={toggleReview}
              aria-label={view === 'review' ? 'Close review and show boreholes' : 'Show review'}>
              Review {view === 'review' && <span aria-hidden="true">×</span>}
            </button>
          </div>
        </div>
        {inTunnel ? <TunnelList tunnels={tunnels.saved} boreholes={known} selectedId={tunnelId}
          grouped={grouped} onGroupedChange={setGrouped}
          onCreate={() => enterTunnels(tunnels.create())}
          onSelect={selectTunnel} /> : view === 'review' ? <>
        {auditOnly ? (
          <div className="result-head">
            <span>Release audit: automatic releases drawn for a person to check</span>
            <button className="link-button" onClick={() => show({ auditOnly: false })}>Waiting records</button>
          </div>
        ) : (
          <FilterBar filters={filters} setFilters={setFilters} lithologies={boreholes.lithologies}
            review={{ filters: reviewFilters, onChange: (f) => show({ reviewFilters: f }), findings: queue.findings }} />
        )}
        {notice && <p className="error" role="alert">{notice}</p>}
        <ReviewList items={queue.items} groups={queue.groups} total={queue.total} loading={queue.loading} audit={auditOnly}
          noun={auditOnly ? 'sampled' : QUEUE_NOUN[reviewFilters.status]}
          grouped={grouped} onGroupedChange={setGrouped}
          selectedId={selectedId} onSelect={toggleSelected}
          crsGroups={!auditOnly && reviewFilters.status === 'pending' && reviewFilters.position === 'crs_inferred' ? queue.crsGroups : []}
          onChanged={reload} />
        </> : <>
        <FilterBar filters={filters} setFilters={setFilters} lithologies={boreholes.lithologies}
          groupName={data.facets?.groups.find((group) => group.group === filters.group)?.project ?? filters.group ?? undefined}
          onClearGroup={() => showGroup(null)} />
        <div className="result-head">
          <span>{loading ? 'Loading…' : `${(data.total ?? data.features.length).toLocaleString('en')} found`}</span>
          <GroupingToggle grouped={grouped} onChange={setGrouped} />
        </div>
        {notice && <p className="error" role="alert">{notice}</p>}
        <ResultList features={listFeatures} groups={data.facets?.groups ?? []} activeGroup={filters.group} grouped={grouped}
          onShowGroup={(group) => showGroup(group)}
          selectedId={selectedId} onSelect={toggleSelected} loading={loading} />
        {!loading && data.total != null && data.features.length < data.total && (
          <button className="btn load-more" onClick={boreholes.loadMore} disabled={boreholes.loadingMore}>
            {boreholes.loadingMore ? 'Loading…' : 'Show more'}
          </button>
        )}
        </>}
      </aside>

      <main className={`map-wrap${inTunnel ? ' map-wrap-section' : ''}`}>
        <MapView data={boreholes.points} route={tunnels.shownRoute} routeFocusKey={inTunnel ? tunnelId : null}
          inactiveRoutes={tunnels.inactiveRoutes} routeDraft={tunnels.draft}
          hoveredHole={hoveredHole?.geometry.coordinates ?? null} onHoverHole={setHoveredHoleId}
          onRoutePoint={tunnels.addPoint}
          selectedId={selectedId} focus={focus} onSelect={(id) => show({ selectedId: id, listOpen: true })}
          sheetWidth={detail ? sheetWidth : null} dark={dark} view3d={view3d} />
        <div className={`legend${detail ? ' legend-beside-sheet' : ''}`} aria-label="Map legend">
          <span><i className="dot dot-high" /> released</span>
          <span><i className="dot dot-medium" /> waiting for review</span>
          <span><i className="dot dot-low" /> rejected</span>
          <span><i className="dot dot-ring" /> approximate position</span>
        </div>
        {tunnels.shownRoute ? (
          <SectionView
            features={routeFeatures}
            route={tunnels.shownRoute}
            selectedId={selectedId}
            hoveredId={hoveredHoleId}
            onHover={setHoveredHoleId}
            onSelect={select}
            onRemove={tunnels.toggleHole}
          />
        ) : shownTunnel && <EmptySection title={shownTunnel.name || 'Tunnel profile'} />}
        {inStats && (
          <StatsView tab={statsTab} onTab={(tab) => show({ statsTab: tab })}
            onReviewAudit={() => openReview({ auditOnly: true })}
            onReview={(f) => openReview({ reviewFilters: f })}
            onShowGroup={(group) => showGroup(group, { view: 'map' })} />
        )}
      </main>

      {shownTunnel && !selectedId && <RoutePanel
        name={shownTunnel.name} project={tunnelProject(shownTunnel, known)} route={tunnels.route} draft={tunnels.draft}
        candidates={tunnels.candidates} selectedIds={tunnels.holeIds} loading={tunnels.loading} error={tunnels.error}
        onChange={tunnels.setRoute}
        onRename={tunnels.rename}
        onImport={tunnels.importRoute}
        onStartDrawing={tunnels.startDrawing}
        onUndo={tunnels.undoPoint} onFinishDrawing={tunnels.finishDrawing}
        onCancelDrawing={tunnels.cancelDrawing} onSelectNearby={tunnels.selectNearby}
        onClearBoreholes={tunnels.clearHoles}
        onToggleHole={tunnels.toggleHole}
        onDelete={() => { tunnels.removeActive(); show({ tunnelId: null }) }}
        onClose={() => { tunnels.cancelDrawing(); show({ tunnelId: null }) }} />}

      {detail && !inStats && <SheetResizer onResize={setSheetWidth} />}
      {detail && !inStats && (
        <DetailPanel
          key={detail.borehole.borehole_id}
          detail={detail}
          onClose={closeRecord}
          onReviewChange={onReviewChange}
          onDecided={view === 'review' ? openNext : undefined}
          onCrsConfirmed={view === 'review' ? reload : undefined}
          onSourceToggle={onSourceToggle}
          onSelect={(id) => {
            const here = detail.borehole
            setTrail((t) => [...(t.at(-1)?.to === here.borehole_id ? t : []), { from: { id: here.borehole_id, name: here.borehole_name }, to: id }])
            select(id)
          }}
          backTo={back?.from.name}
          onBack={() => { setTrail((t) => t.slice(0, -1)); select(back!.from.id) }}
          onFindLike={findLike}
        />
      )}
    </div>
  )
}
