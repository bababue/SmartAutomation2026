import { useEffect, useRef } from 'react'
import { GeoJSONSource, LngLatBounds, Map as MapLibre, NavigationControl, ScaleControl, setWorkerUrl, type LngLatLike, type MapLayerMouseEvent } from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import type { ExpressionSpecification } from '@maplibre/maplibre-gl-style-spec'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { FeatureCollection } from '../types'

// MapLibre resolves its worker next to its own module, which does not exist in the production bundle.
// Importing it as an asset makes Vite emit it in both dev and build.
setWorkerUrl(workerUrl)

// Public vector basemap without API key. Tile requests reveal the viewed area, nothing else.
const STYLE = 'https://tiles.openfreemap.org/styles/positron'

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
  selectedId: string | null
  focus: { lon: number; lat: number; accuracy_m: number | null; approximate: boolean } | null
  onSelect: (id: string) => void
}

function circlePolygon(lon: number, lat: number, radiusM: number) {
  const coords: [number, number][] = []
  for (let i = 0; i <= 64; i++) {
    const a = (i / 64) * 2 * Math.PI
    const dLat = (radiusM / 111_320) * Math.sin(a)
    const dLon = (radiusM / (111_320 * Math.cos((lat * Math.PI) / 180))) * Math.cos(a)
    coords.push([lon + dLon, lat + dLat])
  }
  return { type: 'Feature' as const, properties: {}, geometry: { type: 'Polygon' as const, coordinates: [coords] } }
}

export function MapView({ data, selectedId, focus, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibre | null>(null)
  const ready = useRef(false)
  const latest = useRef({ data, onSelect, selectedId, focus })
  latest.current = { data, onSelect, selectedId, focus }

  useEffect(() => {
    const m = new MapLibre({
      container: container.current!,
      style: STYLE,
      center: [10, 25],
      zoom: 1.4,
      attributionControl: { compact: true },
    })
    map.current = m
    m.addControl(new NavigationControl({ showCompass: false }), 'bottom-right')
    m.addControl(new ScaleControl({ maxWidth: 120 }), 'bottom-left')

    m.on('load', () => {
      m.addSource('boreholes', { type: 'geojson', data: latest.current.data, cluster: true, clusterRadius: 42, clusterMaxZoom: 13 })
      m.addSource('uncertainty', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
      m.addLayer({ id: 'uncertainty-fill', type: 'fill', source: 'uncertainty', paint: { 'fill-color': ACCENT, 'fill-opacity': 0.08 } })
      m.addLayer({ id: 'uncertainty-line', type: 'line', source: 'uncertainty', paint: { 'line-color': ACCENT, 'line-width': 1.2, 'line-dasharray': [3, 2] } })
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
        id: 'points', type: 'circle', source: 'boreholes',
        filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'loc_method'], 'geocoded_place']],
        paint: { 'circle-color': STATUS_COLOR, 'circle-radius': 6, 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 1.5 },
      })
      m.addLayer({
        id: 'points-approx', type: 'circle', source: 'boreholes',
        filter: ['all', ['!', ['has', 'point_count']], ['==', ['get', 'loc_method'], 'geocoded_place']],
        paint: { 'circle-color': '#FFFFFF', 'circle-opacity': 0.6, 'circle-radius': 6, 'circle-stroke-color': STATUS_COLOR, 'circle-stroke-width': 2.5 },
      })
      m.addLayer({
        id: 'selected', type: 'circle', source: 'boreholes', filter: ['==', ['get', 'id'], ''],
        paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-radius': 11, 'circle-stroke-color': ACCENT, 'circle-stroke-width': 3 },
      })

      m.on('click', 'clusters', async (e: MapLayerMouseEvent) => {
        const feature = e.features?.[0]
        if (!feature) return
        const source = m.getSource('boreholes') as GeoJSONSource
        const zoom = await source.getClusterExpansionZoom(feature.properties.cluster_id)
        m.easeTo({ center: (feature.geometry as { coordinates: [number, number] }).coordinates as LngLatLike, zoom })
      })
      for (const layer of ['points', 'points-approx']) {
        m.on('click', layer, (e: MapLayerMouseEvent) => {
          const id = e.features?.[0]?.properties?.id
          if (id) latest.current.onSelect(id)
        })
        m.on('mouseenter', layer, () => (m.getCanvas().style.cursor = 'pointer'))
        m.on('mouseleave', layer, () => (m.getCanvas().style.cursor = ''))
      }
      m.on('mouseenter', 'clusters', () => (m.getCanvas().style.cursor = 'pointer'))
      m.on('mouseleave', 'clusters', () => (m.getCanvas().style.cursor = ''))
      ready.current = true
      fit(m, latest.current.data)
      applySelection(m, latest.current.selectedId, latest.current.focus)  // selection made before load
    })
    return () => m.remove()
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    ;(m.getSource('boreholes') as GeoJSONSource).setData(data)
    fit(m, data)
  }, [data])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    applySelection(m, selectedId, focus)
  }, [selectedId, focus])

  return <div ref={container} className="map" />
}

function applySelection(m: MapLibre, selectedId: string | null, focus: Props['focus']) {
  m.setFilter('selected', ['==', ['get', 'id'], selectedId ?? ''])
  const source = m.getSource('uncertainty') as GeoJSONSource
  if (!focus) {
    source.setData({ type: 'FeatureCollection', features: [] })
    return
  }
  const radius = focus.approximate ? Math.max(focus.accuracy_m ?? 0, 500) : 0
  source.setData({ type: 'FeatureCollection', features: radius ? [circlePolygon(focus.lon, focus.lat, radius)] : [] })
  const zoom = radius ? Math.max(6, Math.min(13, 16 - Math.log2(radius / 100))) : 15
  m.flyTo({ center: [focus.lon, focus.lat], zoom, padding: { left: 0, right: Math.min(window.innerWidth * 0.55, 760), top: 0, bottom: 0 } })
}

function fit(m: MapLibre, data: FeatureCollection) {
  if (!data.features.length) return
  const bounds = new LngLatBounds()
  data.features.forEach((f) => bounds.extend(f.geometry.coordinates))
  m.fitBounds(bounds, { padding: 80, maxZoom: 12, duration: 600 })
}
