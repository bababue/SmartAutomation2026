import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { GeoJSONSource, LngLat, LngLatBounds, Map as MapLibre, NavigationControl, Popup, ScaleControl, setWorkerUrl, type LngLatLike, type MapLayerMouseEvent, type TransformConstrainFunction } from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import type { ExpressionSpecification } from '@maplibre/maplibre-gl-style-spec'
import 'maplibre-gl/dist/maplibre-gl.css'
import { fetchLayerStrips } from '../api'
import { tooltipContent } from '../boreholeTooltip'
import { cssColor, detailSheetRect, el } from '../dom'
import { LITHOLOGY, lithStyle, patternId } from '../lithology'
import { STATUS_LABEL } from '../review'
import type { BoreholeSummary, FeatureCollection, ReviewStatus } from '../types'
import { COLUMN_MIN_ZOOM, circlePolygon, statusColors, updateColumns, type ColumnSpread } from '../mapColumns'
import type { RoutePoint, TunnelRoute } from '../tunnelRoute'

// MapLibre resolves its worker next to its own module, which does not exist in the production bundle.
// Importing it as an asset makes Vite emit it in both dev and build.
setWorkerUrl(workerUrl)

// Public vector basemap without API key. Tile requests reveal the viewed area, nothing else.
const STYLE = { light: 'https://tiles.openfreemap.org/styles/liberty', dark: 'https://tiles.openfreemap.org/styles/dark' }

interface Props {
  data: FeatureCollection
  route: TunnelRoute | null
  routeDraft: RoutePoint[] | null
  onRoutePoint: (point: RoutePoint) => void
  selectedId: string | null
  focus: { lon: number; lat: number; accuracy_m: number | null; approximate: boolean; hasDepth: boolean } | null
  onSelect: (id: string) => void
  /** Width of the detail sheet over the map; the selected hole stays centred beside it. */
  sheetWidth: number | null
  dark: boolean
  /** Off: flat points at every zoom instead of the depth columns. */
  columns3d: boolean
}

const CLUSTER_MAX_ZOOM = 14
const LAYER_PREFETCH_ZOOM = 14
const PITCH = 45

function sectionLine(coords: [number, number][]) {
  return {
    type: 'FeatureCollection' as const,
    features: coords.length > 1
      ? [{ type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: coords } }]
      : [],
  }
}

function routeVertices(coords: RoutePoint[]) {
  return { type: 'FeatureCollection' as const, features: coords.map((coordinates) => ({
    type: 'Feature' as const, properties: {}, geometry: { type: 'Point' as const, coordinates },
  })) }
}

function updateRouteCorridorWidth(m: MapLibre, route: TunnelRoute | null) {
  if (!m.getLayer('tunnel-corridor')) return
  if (!route) return
  const latitude = route.coordinates[Math.floor(route.coordinates.length / 2)][1]
  const metresPerPixel = 40_075_016.686 * Math.cos(latitude * Math.PI / 180) / (512 * 2 ** m.getZoom())
  m.setPaintProperty('tunnel-corridor', 'line-width', Math.min(300, 2 * route.corridorM / metresPerPixel))
}

function updateRouteOverlay(m: MapLibre, state: { route: TunnelRoute | null; routeDraft: RoutePoint[] | null }) {
  ;(m.getSource('tunnel-route') as GeoJSONSource).setData(sectionLine(state.route?.coordinates ?? []))
  ;(m.getSource('tunnel-draft') as GeoJSONSource).setData(sectionLine(state.routeDraft ?? []))
  ;(m.getSource('tunnel-vertices') as GeoJSONSource).setData(routeVertices(state.routeDraft ?? []))
  updateRouteCorridorWidth(m, state.route)
}

function fitRoute(m: MapLibre, coordinates: RoutePoint[]) {
  if (coordinates.length < 2) return
  const bounds = coordinates.reduce((box, point) => box.extend(point), new LngLatBounds(coordinates[0], coordinates[0]))
  m.fitBounds(bounds, { padding: 60, maxZoom: 15, duration: 450 })
}

function pickerList(items: BoreholeSummary[], onPick: (id: string) => void) {
  const root = el('div', 'picker')
  root.append(el('p', 'picker-head', `${items.length} boreholes at this position`))
  const list = el('ul', 'picker-list')
  items.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })).forEach((p) => {
    const li = el('li', '')
    const button = el('button', 'picker-item', p.name)
    button.append(el('span', 'picker-meta', ` ${STATUS_LABEL[p.review_status as ReviewStatus]}`))
    button.addEventListener('click', () => onPick(p.id))
    li.append(button)
    list.append(li)
  })
  root.append(list)
  return root
}

async function lithologyImage(key: string): Promise<ImageData> {
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 32
  const context = canvas.getContext('2d')!
  context.fillStyle = lithStyle(key).fill
  context.fillRect(0, 0, 32, 32)
  const pattern = document.getElementById(patternId(key))
  if (pattern) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 16 16">${pattern.innerHTML}</svg>`
    const image = new Image()
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
    try {
      await image.decode()
      context.drawImage(image, 0, 0)
    } catch {
      console.warn(`Could not render lithology pattern: ${key}`)
    }
  }
  return context.getImageData(0, 0, 32, 32)
}

function wrapLongitude(lon: number) {
  return ((lon + 180) % 360 + 360) % 360 - 180
}

export function MapView({ data, route, routeDraft, onRoutePoint, selectedId, focus, onSelect, sheetWidth, dark, columns3d }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibre | null>(null)
  const ready = useRef(false)
  const latest = useRef({ data, onSelect, selectedId, focus, route, routeDraft, onRoutePoint, dark, columns3d })
  const layerStrips = useRef(new Map<string, BoreholeSummary['strip']>())
  const [loaded, setLoaded] = useState(false)
  const routeCoordinates = route?.coordinates
  const shownDark = useRef(dark)  // the basemap currently on the map
  const spread = useRef<ColumnSpread>({ columns: [], drawn: new Map(), start: 0, frame: 0, show: columns3d })
  // Map event handlers read the newest props; layout effects run before the effects below that use them too.
  useLayoutEffect(() => {
    latest.current = { data, onSelect, selectedId, focus, route, routeDraft, onRoutePoint, dark, columns3d }
  })

  useEffect(() => {
    const columnSpread = spread.current
    let created: MapLibre | undefined  // the constraint already runs inside the constructor
    const m = new MapLibre({
      container: container.current!,
      style: STYLE[latest.current.dark ? 'dark' : 'light'],
      center: [10, 25],
      zoom: 1.4,
      pitch: PITCH,
      transformConstrain: keepWorldInView(container.current!, () => created),
      canvasContextAttributes: { antialias: true },
      attributionControl: { compact: true },
    })
    created = m
    map.current = m
    const tooltip = new Popup({ closeButton: false, closeOnClick: false, offset: 10, className: 'map-tooltip' })
    const picker = new Popup({ closeButton: true, offset: 14, className: 'map-picker', maxWidth: '280px' })
    let stripRequest: AbortController | null = null
    let pendingStrips = false
    let requestedBbox = ''
    let refreshTimer: ReturnType<typeof setTimeout> | null = null
    let disposed = false
    const loadVisibleStrips = () => {
      if (m.getZoom() < LAYER_PREFETCH_ZOOM) return
      const bounds = m.getBounds()
      const bbox = [wrapLongitude(bounds.getWest()), bounds.getSouth(),
        wrapLongitude(bounds.getEast()), bounds.getNorth()].join(',')
      if (bbox === requestedBbox) return
      if (stripRequest) { pendingStrips = true; return }
      requestedBbox = bbox
      const controller = new AbortController()
      stripRequest = controller
      fetchLayerStrips(bbox, controller.signal).then(({ items }) => {
        if (disposed) return
        for (const { id, strip } of items) layerStrips.current.set(id, strip)
        updateColumns(m, latest.current.data, layerStrips.current, spread.current)
      }).catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        requestedBbox = ''
        console.error('Could not load borehole layers', error)
      }).finally(() => {
        if (stripRequest !== controller) return
        stripRequest = null
        if (pendingStrips && !disposed) {
          pendingStrips = false
          loadVisibleStrips()
        }
      })
    }
    const refreshColumns = () => {
      updateColumns(m, latest.current.data, layerStrips.current, spread.current)
      loadVisibleStrips()
    }
    const scheduleRefresh = () => {
      if (refreshTimer != null) return
      refreshTimer = setTimeout(() => {
        refreshTimer = null
        refreshColumns()
      }, 120)
    }
    const finishRefresh = () => {
      if (refreshTimer != null) clearTimeout(refreshTimer)
      refreshTimer = null
      refreshColumns()
    }
    m.addControl(new NavigationControl(), 'bottom-right')
    m.addControl(new ScaleControl({ maxWidth: 120 }), 'bottom-left')

    m.on('load', async () => {
      const images = await loadPatterns()
      if (disposed) return
      addOverlay(m, images, latest.current)

      m.on('click', (event) => {
        if (latest.current.routeDraft !== null) latest.current.onRoutePoint([event.lngLat.lng, event.lngLat.lat])
      })

      m.on('click', 'clusters', async (e: MapLayerMouseEvent) => {
        if (latest.current.routeDraft !== null) return
        const feature = e.features?.[0]
        if (!feature || feature.geometry.type !== 'Point') return
        const source = m.getSource('boreholes') as GeoJSONSource
        const center = feature.geometry.coordinates as [number, number]
        const zoom = await source.getClusterExpansionZoom(feature.properties.cluster_id)
        if (zoom <= CLUSTER_MAX_ZOOM && m.getZoom() < CLUSTER_MAX_ZOOM) {
          m.easeTo({ center: center as LngLatLike, zoom })
          return
        }
        // Holes at (almost) the same position never split up: offer a list instead.
        const leaves = await source.getClusterLeaves(feature.properties.cluster_id, 50, 0)
        picker.remove()
        picker.setLngLat(center).setDOMContent(pickerList(leaves.map((l) => l.properties as BoreholeSummary), (id) => {
          picker.remove()
          latest.current.onSelect(id)
        })).addTo(m)
      })
      for (const layer of ['points', 'points-no-depth', 'points-approx', 'depth-columns', 'depth-column-patterns']) {
        m.on('click', layer, (e: MapLayerMouseEvent) => {
          if (latest.current.routeDraft !== null) return
          const id = e.features?.[0]?.properties?.id
          if (id) latest.current.onSelect(id)
        })
        m.on('mouseenter', layer, (e: MapLayerMouseEvent) => {
          if (latest.current.routeDraft !== null) return
          m.getCanvas().style.cursor = 'pointer'
          const f = e.features?.[0]
          if (!f) return
          tooltip.setLngLat(f.geometry.type === 'Point' ? f.geometry.coordinates as [number, number] : e.lngLat)
            .setDOMContent(tooltipContent(f.properties as BoreholeSummary)).addTo(m)
        })
        m.on('mouseleave', layer, () => {
          m.getCanvas().style.cursor = latest.current.routeDraft !== null ? 'crosshair' : ''
          tooltip.remove()
        })
      }
      for (const layer of ['depth-columns', 'depth-column-patterns']) {
        m.on('mousemove', layer, (e: MapLayerMouseEvent) => {
          const f = e.features?.[0]
          if (f) tooltip.setLngLat(e.lngLat).setDOMContent(tooltipContent(f.properties as BoreholeSummary)).addTo(m)
        })
      }
      m.on('mouseenter', 'clusters', () => (m.getCanvas().style.cursor = latest.current.routeDraft !== null ? 'crosshair' : 'pointer'))
      m.on('mouseleave', 'clusters', () => (m.getCanvas().style.cursor = latest.current.routeDraft !== null ? 'crosshair' : ''))
      // Collapsing the list resizes the map; keep the selected hole in view.
      m.on('resize', () => recenter(m, latest.current.focus))
      // Tilting does not run the constraint; re-apply it once the tilt ends.
      m.on('pitchend', () => m.jumpTo({ center: m.getCenter(), zoom: m.getZoom() }))
      m.on('move', scheduleRefresh)
      m.on('zoom', () => updateRouteCorridorWidth(m, latest.current.route))
      m.on('moveend', finishRefresh)
      ready.current = true
      setLoaded(true)  // a theme switch made before this point applies now
      if (latest.current.route) fitRoute(m, latest.current.route.coordinates)
      else if (!latest.current.selectedId) fit(m, latest.current.data)
      applySelection(m, latest.current.selectedId, latest.current.focus)  // selection made before load
      refreshColumns()
    })
    return () => {
      disposed = true
      if (refreshTimer != null) clearTimeout(refreshTimer)
      stripRequest?.abort()
      cancelAnimationFrame(columnSpread.frame)
      m.remove()
    }
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    ;(m.getSource('boreholes') as GeoJSONSource).setData(data)
    updateColumns(m, data, layerStrips.current, spread.current)
    // An open borehole (e.g. after a reload) keeps its view instead of zooming out to all matches.
    if (!latest.current.selectedId && !latest.current.route) fit(m, data)
  }, [data])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    applySelection(m, selectedId, focus)
  }, [selectedId, focus])

  useEffect(() => {
    const m = map.current
    if (m && ready.current) recenter(m, latest.current.focus)
  }, [sheetWidth])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    updateRouteOverlay(m, latest.current)
    if (routeCoordinates) fitRoute(m, routeCoordinates)
  }, [routeCoordinates])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    updateRouteOverlay(m, latest.current)
  }, [route?.corridorM, routeDraft])

  useEffect(() => {
    const m = map.current
    if (!m) return
    if (routeDraft !== null) {
      m.doubleClickZoom.disable()
      m.getCanvas().style.cursor = 'crosshair'
    } else {
      m.doubleClickZoom.enable()
      m.getCanvas().style.cursor = ''
    }
  }, [routeDraft])

  useEffect(() => {
    const m = map.current
    if (!m || !loaded || shownDark.current === dark) return
    shownDark.current = dark
    ready.current = false
    m.setStyle(STYLE[dark ? 'dark' : 'light'], { diff: false })
    m.once('style.load', async () => {
      addOverlay(m, await loadPatterns(), latest.current)
      updateColumns(m, latest.current.data, layerStrips.current, spread.current)
      markSelection(m, latest.current.selectedId, latest.current.focus)
      updateRouteOverlay(m, latest.current)
      ready.current = true
    })
  }, [dark, loaded])

  useEffect(() => {
    spread.current.show = columns3d
    const m = map.current
    if (!m || !ready.current) return
    showColumns(m, columns3d)
    updateColumns(m, latest.current.data, layerStrips.current, spread.current)
  }, [columns3d])

  return <div ref={container} className="map" />
}

let patterns: Promise<(readonly [string, Awaited<ReturnType<typeof lithologyImage>>])[]> | null = null
function loadPatterns() {
  patterns ??= Promise.all(Object.keys(LITHOLOGY).map(async (key) => [key, await lithologyImage(key)] as const))
  return patterns
}

/** The borehole sources and layers on top of the basemap; a style switch drops them, so they are added again. */
function addOverlay(m: MapLibre, images: Awaited<ReturnType<typeof loadPatterns>>,
  state: { data: FeatureCollection; route: TunnelRoute | null; routeDraft: RoutePoint[] | null; columns3d: boolean }) {
  if (m.getSource('boreholes')) return
  const accent = cssColor('--water'), ink = cssColor('--ink'), panel = cssColor('--panel')
  const status = statusColors()
  // Point colour = release status; a ring instead of a dot = approximate position.
  const statusColor: ExpressionSpecification = ['match', ['get', 'review_status'],
    ['auto_approved', 'approved'], status.approved, 'rejected', status.rejected, status.pending_review]
  for (const [key, image] of images) m.addImage(patternId(key), image, { pixelRatio: 2 })
  m.addSource('boreholes', { type: 'geojson', data: state.data, cluster: true, clusterRadius: 42, clusterMaxZoom: CLUSTER_MAX_ZOOM })
  m.addSource('depth-markers', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  m.addSource('column-leaders', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  m.addSource('uncertainty', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  m.addSource('tunnel-route', { type: 'geojson', data: sectionLine(state.route?.coordinates ?? []) })
  m.addSource('tunnel-draft', { type: 'geojson', data: sectionLine(state.routeDraft ?? []) })
  m.addSource('tunnel-vertices', { type: 'geojson', data: routeVertices(state.routeDraft ?? []) })
  m.addLayer({ id: 'tunnel-corridor', type: 'line', source: 'tunnel-route', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': accent, 'line-opacity': 0.18, 'line-width': 1 } })
  m.addLayer({ id: 'tunnel-line', type: 'line', source: 'tunnel-route', paint: { 'line-color': accent, 'line-width': 3 } })
  m.addLayer({ id: 'tunnel-draft-line', type: 'line', source: 'tunnel-draft', paint: { 'line-color': accent, 'line-width': 3, 'line-dasharray': [2, 2] } })
  m.addLayer({ id: 'tunnel-draft-vertices', type: 'circle', source: 'tunnel-vertices',
    paint: { 'circle-color': accent, 'circle-radius': 5, 'circle-stroke-color': panel, 'circle-stroke-width': 2 } })
  updateRouteOverlay(m, state)
  m.addLayer({ id: 'uncertainty-fill', type: 'fill', source: 'uncertainty', paint: { 'fill-color': accent, 'fill-opacity': 0.08 } })
  m.addLayer({ id: 'uncertainty-line', type: 'line', source: 'uncertainty', paint: { 'line-color': accent, 'line-width': 1.2, 'line-dasharray': [3, 2] } })
  m.addLayer({
    id: 'column-leaders', type: 'line', source: 'column-leaders', minzoom: COLUMN_MIN_ZOOM, filter: ['==', ['geometry-type'], 'LineString'],
    layout: { 'line-cap': 'round' },
    paint: { 'line-color': ink, 'line-width': 2.5, 'line-opacity': 0.9, 'line-dasharray': [2, 1.6] },
  })
  // The true spot of the fanned-out holes: a target, so it never reads as a borehole point.
  m.addLayer({
    id: 'column-spots', type: 'circle', source: 'column-leaders', minzoom: COLUMN_MIN_ZOOM, filter: ['==', ['geometry-type'], 'Point'],
    paint: { 'circle-color': panel, 'circle-radius': 8, 'circle-stroke-color': ink, 'circle-stroke-width': 2.5, 'circle-pitch-alignment': 'map' },
  })
  m.addLayer({
    id: 'column-spot-dots', type: 'circle', source: 'column-leaders', minzoom: COLUMN_MIN_ZOOM, filter: ['==', ['geometry-type'], 'Point'],
    paint: { 'circle-color': ink, 'circle-radius': 2.5, 'circle-pitch-alignment': 'map' },
  })
  m.addLayer({
    id: 'depth-columns', type: 'fill-extrusion', source: 'depth-markers', minzoom: COLUMN_MIN_ZOOM,
    filter: ['!', ['has', 'pattern']],
    paint: { 'fill-extrusion-color': ['get', 'color'], 'fill-extrusion-base': ['get', 'base_m'],
      'fill-extrusion-height': ['get', 'height_m'], 'fill-extrusion-opacity': 0.95 },
  })
  m.addLayer({
    id: 'depth-column-patterns', type: 'fill-extrusion', source: 'depth-markers', minzoom: COLUMN_MIN_ZOOM,
    filter: ['has', 'pattern'],
    paint: { 'fill-extrusion-pattern': ['get', 'pattern'], 'fill-extrusion-base': ['get', 'base_m'],
      'fill-extrusion-height': ['get', 'height_m'], 'fill-extrusion-opacity': 0.95 },
  })
  m.addLayer({
    id: 'clusters', type: 'circle', source: 'boreholes', filter: ['has', 'point_count'],
    paint: {
      'circle-color': accent,
      'circle-opacity': 0.88,
      'circle-radius': ['step', ['get', 'point_count'], 13, 10, 17, 50, 22, 200, 28],
      'circle-stroke-color': cssColor('--paper'),
      'circle-stroke-width': 2,
    },
  })
  m.addLayer({
    id: 'cluster-count', type: 'symbol', source: 'boreholes', filter: ['has', 'point_count'],
    layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['Noto Sans Regular'], 'text-size': 12 },
    paint: { 'text-color': cssColor('--on-accent') },
  })
  m.addLayer({
    id: 'points', type: 'circle', source: 'boreholes', maxzoom: COLUMN_MIN_ZOOM,
    filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'loc_method'], 'geocoded_place']],
    paint: { 'circle-color': statusColor, 'circle-radius': 6, 'circle-stroke-color': panel, 'circle-stroke-width': 1.5 },
  })
  m.addLayer({
    id: 'points-no-depth', type: 'circle', source: 'boreholes', minzoom: COLUMN_MIN_ZOOM,
    filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'loc_method'], 'geocoded_place'],
      ['!', ['>', ['coalesce', ['get', 'depth'], 0], 0]]],
    paint: { 'circle-color': statusColor, 'circle-radius': 6, 'circle-stroke-color': panel, 'circle-stroke-width': 1.5 },
  })
  m.addLayer({
    id: 'points-approx', type: 'circle', source: 'boreholes',
    filter: ['all', ['!', ['has', 'point_count']], ['==', ['get', 'loc_method'], 'geocoded_place']],
    paint: { 'circle-color': panel, 'circle-stroke-color': statusColor, 'circle-stroke-width': 2.5 },
  })
  m.addLayer({
    // Below column zoom only: at column zoom the centred 3D column marks the selection.
    id: 'selected', type: 'circle', source: 'boreholes', maxzoom: COLUMN_MIN_ZOOM, filter: ['==', ['get', 'id'], ''],
    paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-radius': 11, 'circle-stroke-color': accent, 'circle-stroke-width': 3 },
  })
  showColumns(m, state.columns3d)
}

/** With the 3D columns off, the flat points stay at column zoom too and stand in for the columns. */
function showColumns(m: MapLibre, on: boolean) {
  const top = on ? COLUMN_MIN_ZOOM : 24
  m.setLayerZoomRange('points', 0, top)
  m.setLayerZoomRange('selected', 0, top)
  m.setLayoutProperty('points-no-depth', 'visibility', on ? 'visible' : 'none')
  m.setPaintProperty('points-approx', 'circle-opacity', on ? ['step', ['zoom'], 0.6, COLUMN_MIN_ZOOM, 0] : 0.6)
  m.setPaintProperty('points-approx', 'circle-radius', on ? ['step', ['zoom'], 6, COLUMN_MIN_ZOOM, 10] : 6)
  m.setPaintProperty('points-approx', 'circle-stroke-opacity', on ? APPROX_STROKE_OPACITY : 1)
}

// At column zoom the spot marker of the fanned-out columns stands in for the approximate-position ring.
const APPROX_STROKE_OPACITY: ExpressionSpecification =
  ['step', ['zoom'], 1, COLUMN_MIN_ZOOM, ['case', ['>', ['coalesce', ['get', 'depth'], 0], 0], 0, 1]]

function applySelection(m: MapLibre, selectedId: string | null, focus: Props['focus']) {
  const radius = markSelection(m, selectedId, focus)
  if (!focus) return
  const zoom = radius && !focus.hasDepth ? Math.max(6, Math.min(13, 16 - Math.log2(radius / 100))) : 16
  m.flyTo({ center: [focus.lon, focus.lat], zoom, duration: 500, padding: uncovered(m) })
}

function markSelection(m: MapLibre, selectedId: string | null, focus: Props['focus']) {
  m.setFilter('selected', ['==', ['get', 'id'], selectedId ?? ''])
  const source = m.getSource('uncertainty') as GeoJSONSource
  const radius = focus?.approximate ? Math.max(focus.accuracy_m ?? 0, 500) : 0
  source.setData({ type: 'FeatureCollection', features: focus && radius ? [circlePolygon(focus.lon, focus.lat, radius)] : [] })
  return radius
}

/**
 * Keeps the map covered by the world: Web Mercator ends near ±85°, and with the camera tilted the top
 * edge of the view reaches far beyond the centre, so the centre and zoom are limited by the ground
 * distance from the centre to the top and bottom edges.
 */
function keepWorldInView(el: HTMLElement, getMap: () => MapLibre | undefined): TransformConstrainFunction {
  return (lngLat, zoom) => {
    const m = getMap()
    const h = el.clientHeight
    const half = ((m?.getVerticalFieldOfView() ?? 36.87) * Math.PI) / 360
    const pitch = ((m?.getPitch() ?? PITCH) * Math.PI) / 180
    const dist = h / 2 / Math.tan(half)
    const height = dist * Math.cos(pitch), across = dist * Math.sin(pitch)
    const top = height * Math.tan(pitch + half) - across
    const bottom = across - height * Math.tan(pitch - half)
    const z = Math.max(zoom, Math.log2((top + bottom) / 512))
    const world = 512 * 2 ** z
    const rad = (lngLat.lat * Math.PI) / 180
    const y = ((1 - Math.log(Math.tan(Math.PI / 4 + rad / 2)) / Math.PI) / 2) * world
    const clamped = Math.min(Math.max(y, top), world - bottom)
    const lat = (2 * Math.atan(Math.exp(Math.PI * (1 - (2 * clamped) / world))) - Math.PI / 2) * 180 / Math.PI
    return { center: new LngLat(lngLat.lng, lat), zoom: z }
  }
}

function recenter(m: MapLibre, focus: Props['focus']) {
  if (focus) m.easeTo({ center: [focus.lon, focus.lat], padding: uncovered(m), duration: 0 })
}

/** Padding that keeps the centre in the part of the map the detail sheet leaves visible. */
function uncovered(m: MapLibre) {
  const map = m.getContainer().getBoundingClientRect()
  const sheet = detailSheetRect()
  const right = sheet && sheet.left > map.left ? Math.max(0, map.right - sheet.left) : 0
  return { left: 0, right, top: 0, bottom: 0 }
}

function fit(m: MapLibre, data: FeatureCollection) {
  if (!data.features.length) return
  const bounds = new LngLatBounds()
  data.features.forEach((f) => bounds.extend(f.geometry.coordinates))
  m.fitBounds(bounds, { padding: 80, maxZoom: 12, duration: 600 })
}
