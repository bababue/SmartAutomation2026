import { useEffect, useRef, useState } from 'react'
import { GeoJSONSource, LngLat, LngLatBounds, Map as MapLibre, NavigationControl, Popup, ScaleControl, setWorkerUrl, type LngLatLike, type MapLayerMouseEvent, type TransformConstrainFunction } from 'maplibre-gl'
import type { Feature as GeoJSONFeature, Polygon } from 'geojson'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import type { ExpressionSpecification } from '@maplibre/maplibre-gl-style-spec'
import 'maplibre-gl/dist/maplibre-gl.css'
import { fetchLayerStrips } from '../api'
import { tooltipContent } from '../boreholeTooltip'
import { LITHOLOGY, lithStyle, patternId } from '../lithology'
import { STATUS_COLOR as STATUS_COLORS, STATUS_LABEL } from '../review'
import type { BoreholeSummary, FeatureCollection, ReviewStatus } from '../types'

// MapLibre resolves its worker next to its own module, which does not exist in the production bundle.
// Importing it as an asset makes Vite emit it in both dev and build.
setWorkerUrl(workerUrl)

// Public vector basemap without API key. Tile requests reveal the viewed area, nothing else.
const STYLE = { light: 'https://tiles.openfreemap.org/styles/liberty', dark: 'https://tiles.openfreemap.org/styles/dark' }

const ACCENT = '#0E6F8E'
// Point colour = release status (see review.ts); a ring instead of a dot = approximate position.
const STATUS_COLOR: ExpressionSpecification = [
  'match', ['get', 'review_status'],
  ['auto_approved', 'approved'], '#2F7D4F',
  'rejected', '#B33A3A',
  '#B7791F',
]

interface Props {
  data: FeatureCollection
  section: [number, number][]  // line through the boreholes of the section view, in order
  selectedId: string | null
  focus: { lon: number; lat: number; accuracy_m: number | null; approximate: boolean; hasDepth: boolean } | null
  onSelect: (id: string) => void
  /** Width of the detail sheet over the map; the selected hole stays centred beside it. */
  sheetWidth: number | null
  dark: boolean
}

const CLUSTER_MAX_ZOOM = 14
const COLUMN_MIN_ZOOM = 15
const LAYER_PREFETCH_ZOOM = 14
const COLUMN_RADIUS_M = 5
const PITCH = 45

function sectionLine(coords: [number, number][]) {
  return {
    type: 'FeatureCollection' as const,
    features: coords.length > 1
      ? [{ type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: coords } }]
      : [],
  }
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string) {
  const node = document.createElement(tag)
  node.className = className
  if (text != null) node.textContent = text  // textContent: names come from documents, never as HTML
  return node
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

function circlePolygon(lon: number, lat: number, radiusM: number, segments = 64) {
  const coords: [number, number][] = []
  for (let i = 0; i <= segments; i++) {
    const a = (i / segments) * 2 * Math.PI
    const dLat = (radiusM / 111_320) * Math.sin(a)
    const dLon = (radiusM / (111_320 * Math.max(0.01, Math.cos((lat * Math.PI) / 180)))) * Math.cos(a)
    coords.push([lon + dLon, lat + dLat])
  }
  return { type: 'Feature' as const, properties: {}, geometry: { type: 'Polygon' as const, coordinates: [coords] } }
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

function updateColumns(m: MapLibre, data: FeatureCollection, strips: Map<string, BoreholeSummary['strip']>) {
  const source = m.getSource('depth-markers') as GeoJSONSource
  const features: GeoJSONFeature<Polygon>[] = []
  if (m.getZoom() >= COLUMN_MIN_ZOOM) {
    const bounds = m.getBounds()
    for (const feature of data.features) {
      const depth = feature.properties.depth
      if (depth == null || !Number.isFinite(depth) || depth <= 0) continue
      const [lon, lat] = feature.geometry.coordinates
      if (![lon - 360, lon, lon + 360].some((wrappedLon) => bounds.contains([wrappedLon, lat]))) continue
      const geometry = circlePolygon(lon, lat, COLUMN_RADIUS_M, 24).geometry
      const addSection = (top: number, bottom: number, color: string, lithology?: string) => {
        if (bottom <= top) return
        features.push({ type: 'Feature', geometry, properties: {
          ...feature.properties, base_m: depth - bottom,
          height_m: depth - top,
          color, ...(lithology ? { layer_top: top, layer_bottom: bottom, layer_lithology: lithology,
            pattern: patternId(lithology in LITHOLOGY ? lithology : 'unknown') } : {}),
        } })
      }
      const strip = strips.get(feature.properties.id)
      if (!strip?.length) {
        addSection(0, depth, STATUS_COLORS[feature.properties.review_status])
        continue
      }
      let covered = 0
      for (const [rawTop, rawBottom, lithology] of [...strip].sort((a, b) => a[0] - b[0])) {
        if (!Number.isFinite(rawTop) || !Number.isFinite(rawBottom)) continue
        const top = Math.max(covered, Math.min(depth, rawTop))
        const bottom = Math.min(depth, rawBottom)
        if (top > covered) addSection(covered, top, lithStyle('unknown').fill, 'unknown')
        if (bottom > top) addSection(top, bottom, lithStyle(lithology).fill, lithology)
        covered = Math.max(covered, bottom)
      }
      if (covered < depth) addSection(covered, depth, lithStyle('unknown').fill, 'unknown')
      features.push({ type: 'Feature', geometry, properties: {
        ...feature.properties, base_m: depth,
        height_m: depth + 0.5, color: STATUS_COLORS[feature.properties.review_status],
      } })
    }
  }
  source.setData({ type: 'FeatureCollection', features })
}

function wrapLongitude(lon: number) {
  return ((lon + 180) % 360 + 360) % 360 - 180
}

export function MapView({ data, section, selectedId, focus, onSelect, sheetWidth, dark }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibre | null>(null)
  const ready = useRef(false)
  const latest = useRef({ data, onSelect, selectedId, focus, section, dark })
  const layerStrips = useRef(new Map<string, BoreholeSummary['strip']>())
  latest.current = { data, onSelect, selectedId, focus, section, dark }
  const [loaded, setLoaded] = useState(false)
  const shownDark = useRef(dark)  // the basemap currently on the map

  useEffect(() => {
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
        updateColumns(m, latest.current.data, layerStrips.current)
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
      updateColumns(m, latest.current.data, layerStrips.current)
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

      m.on('click', 'clusters', async (e: MapLayerMouseEvent) => {
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
          const id = e.features?.[0]?.properties?.id
          if (id) latest.current.onSelect(id)
        })
        m.on('mouseenter', layer, (e: MapLayerMouseEvent) => {
          m.getCanvas().style.cursor = 'pointer'
          const f = e.features?.[0]
          if (!f) return
          tooltip.setLngLat(f.geometry.type === 'Point' ? f.geometry.coordinates as [number, number] : e.lngLat)
            .setDOMContent(tooltipContent(f.properties as BoreholeSummary)).addTo(m)
        })
        m.on('mouseleave', layer, () => {
          m.getCanvas().style.cursor = ''
          tooltip.remove()
        })
      }
      for (const layer of ['depth-columns', 'depth-column-patterns']) {
        m.on('mousemove', layer, (e: MapLayerMouseEvent) => {
          const f = e.features?.[0]
          if (f) tooltip.setLngLat(e.lngLat).setDOMContent(tooltipContent(f.properties as BoreholeSummary)).addTo(m)
        })
      }
      m.on('mouseenter', 'clusters', () => (m.getCanvas().style.cursor = 'pointer'))
      m.on('mouseleave', 'clusters', () => (m.getCanvas().style.cursor = ''))
      // Collapsing the list resizes the map; keep the selected hole in view.
      m.on('resize', () => recenter(m, latest.current.focus))
      // Tilting does not run the constraint; re-apply it once the tilt ends.
      m.on('pitchend', () => m.jumpTo({ center: m.getCenter(), zoom: m.getZoom() }))
      m.on('move', scheduleRefresh)
      m.on('moveend', finishRefresh)
      ready.current = true
      setLoaded(true)  // a theme switch made before this point applies now
      if (!latest.current.selectedId) fit(m, latest.current.data)
      applySelection(m, latest.current.selectedId, latest.current.focus)  // selection made before load
      refreshColumns()
    })
    return () => {
      disposed = true
      if (refreshTimer != null) clearTimeout(refreshTimer)
      stripRequest?.abort()
      m.remove()
    }
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    ;(m.getSource('boreholes') as GeoJSONSource).setData(data)
    updateColumns(m, data, layerStrips.current)
    // An open borehole (e.g. after a reload) keeps its view instead of zooming out to all matches.
    if (!latest.current.selectedId) fit(m, data)
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
    ;(m.getSource('section') as GeoJSONSource).setData(sectionLine(section))
  }, [section])

  useEffect(() => {
    const m = map.current
    if (!m || !loaded || shownDark.current === dark) return
    shownDark.current = dark
    ready.current = false
    m.setStyle(STYLE[dark ? 'dark' : 'light'], { diff: false })
    m.once('style.load', async () => {
      addOverlay(m, await loadPatterns(), latest.current)
      updateColumns(m, latest.current.data, layerStrips.current)
      markSelection(m, latest.current.selectedId, latest.current.focus)
      ready.current = true
    })
  }, [dark, loaded])

  return <div ref={container} className="map" />
}

let patterns: Promise<(readonly [string, Awaited<ReturnType<typeof lithologyImage>>])[]> | null = null
function loadPatterns() {
  patterns ??= Promise.all(Object.keys(LITHOLOGY).map(async (key) => [key, await lithologyImage(key)] as const))
  return patterns
}

/** The borehole sources and layers on top of the basemap; a style switch drops them, so they are added again. */
function addOverlay(m: MapLibre, images: Awaited<ReturnType<typeof loadPatterns>>,
  state: { data: FeatureCollection; section: [number, number][]; dark: boolean }) {
  if (m.getSource('boreholes')) return
  for (const [key, image] of images) m.addImage(patternId(key), image, { pixelRatio: 2 })
  m.addSource('boreholes', { type: 'geojson', data: state.data, cluster: true, clusterRadius: 42, clusterMaxZoom: CLUSTER_MAX_ZOOM })
  m.addSource('depth-markers', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  m.addSource('uncertainty', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  m.addSource('section', { type: 'geojson', data: sectionLine(state.section) })
  m.addLayer({ id: 'section-line', type: 'line', source: 'section', paint: { 'line-color': state.dark ? '#E2E7E5' : '#1D2528', 'line-width': 2, 'line-dasharray': [2, 1.5] } })
  m.addLayer({ id: 'uncertainty-fill', type: 'fill', source: 'uncertainty', paint: { 'fill-color': ACCENT, 'fill-opacity': 0.08 } })
  m.addLayer({ id: 'uncertainty-line', type: 'line', source: 'uncertainty', paint: { 'line-color': ACCENT, 'line-width': 1.2, 'line-dasharray': [3, 2] } })
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
      'circle-color': ACCENT,
      'circle-opacity': 0.88,
      'circle-radius': ['step', ['get', 'point_count'], 13, 10, 17, 50, 22, 200, 28],
      'circle-stroke-color': '#F6F7F4',
      'circle-stroke-width': 2,
    },
  })
  m.addLayer({
    id: 'cluster-count', type: 'symbol', source: 'boreholes', filter: ['has', 'point_count'],
    layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['Noto Sans Regular'], 'text-size': 12 },
    paint: { 'text-color': '#FFFFFF' },
  })
  m.addLayer({
    id: 'points', type: 'circle', source: 'boreholes', maxzoom: COLUMN_MIN_ZOOM,
    filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'loc_method'], 'geocoded_place']],
    paint: { 'circle-color': STATUS_COLOR, 'circle-radius': 6, 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 1.5 },
  })
  m.addLayer({
    id: 'points-no-depth', type: 'circle', source: 'boreholes', minzoom: COLUMN_MIN_ZOOM,
    filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'loc_method'], 'geocoded_place'],
      ['!', ['>', ['coalesce', ['get', 'depth'], 0], 0]]],
    paint: { 'circle-color': STATUS_COLOR, 'circle-radius': 6, 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 1.5 },
  })
  m.addLayer({
    id: 'points-approx', type: 'circle', source: 'boreholes',
    filter: ['all', ['!', ['has', 'point_count']], ['==', ['get', 'loc_method'], 'geocoded_place']],
    paint: { 'circle-color': '#FFFFFF', 'circle-opacity': ['step', ['zoom'], 0.6, COLUMN_MIN_ZOOM, 0],
      'circle-radius': ['step', ['zoom'], 6, COLUMN_MIN_ZOOM, 10],
      'circle-stroke-color': STATUS_COLOR, 'circle-stroke-width': 2.5 },
  })
  m.addLayer({
    // Below column zoom only: at column zoom the centred 3D column marks the selection.
    id: 'selected', type: 'circle', source: 'boreholes', maxzoom: COLUMN_MIN_ZOOM, filter: ['==', ['get', 'id'], ''],
    paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-radius': 11, 'circle-stroke-color': ACCENT, 'circle-stroke-width': 3 },
  })
}

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
  const sheet = document.querySelector('.detail')?.getBoundingClientRect()
  const right = sheet && sheet.left > map.left ? Math.max(0, map.right - sheet.left) : 0
  return { left: 0, right, top: 0, bottom: 0 }
}

function fit(m: MapLibre, data: FeatureCollection) {
  if (!data.features.length) return
  const bounds = new LngLatBounds()
  data.features.forEach((f) => bounds.extend(f.geometry.coordinates))
  m.fitBounds(bounds, { padding: 80, maxZoom: 12, duration: 600 })
}
