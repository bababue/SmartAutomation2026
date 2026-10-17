import type { GeoJSONSource, Map as MapLibre } from 'maplibre-gl'
import type { Feature as GeoJSONFeature, Polygon } from 'geojson'
import { cssColor } from './dom'
import { LITHOLOGY, lithStyle, patternId } from './lithology'
import { M_PER_DEG_LAT, M_PER_DEG_LON, offsetPoint } from './tunnelRoute'
import type { BoreholeSummary, FeatureCollection, MapPoint, ReviewStatus } from './types'

export const COLUMN_MIN_ZOOM = 15
const COLUMN_RADIUS_M = 5

/** Release status colours of the current theme, for map layers that MapLibre paints on its canvas. */
export function statusColors(): Record<ReviewStatus, string> {
  const released = cssColor('--high')
  return { auto_approved: released, approved: released, pending_review: cssColor('--medium'), rejected: cssColor('--low') }
}

export function circlePolygon(lon: number, lat: number, radiusM: number, segments = 64) {
  const coords: [number, number][] = []
  for (let i = 0; i <= segments; i++) {
    const a = (i / segments) * 2 * Math.PI
    coords.push(offsetPoint([lon, lat], radiusM * Math.cos(a), radiusM * Math.sin(a)))
  }
  return { type: 'Feature' as const, properties: {}, geometry: { type: 'Polygon' as const, coordinates: [coords] } }
}

interface Column {
  lon: number
  lat: number
  to: Offset    // where the column belongs once fanned out
  from: Offset  // where its current animation started
  sections: GeoJSONFeature<Polygon>['properties'][]
  props: MapPoint
}

type Offset = readonly [east_m: number, north_m: number]

/** What the fan-out animation needs between redraws, one per map. */
export interface ColumnSpread {
  columns: Column[]
  drawn: Map<string, Offset>  // offset each visible column was last drawn at
  start: number
  frame: number
  show: boolean  // the 3D columns are switched on
}

const SPREAD_GAP_M = 3
const SPREAD_SPACING_M = 2 * COLUMN_RADIUS_M + SPREAD_GAP_M
const SPREAD_MS = 1000
const SPOT_CLEARANCE_M = 3  // free ground between the spot marker and the ring of columns around it
const NO_OFFSET: Offset = [0, 0]

export function updateColumns(m: MapLibre, data: FeatureCollection<MapPoint>, strips: Map<string, BoreholeSummary['strip']>, spread: ColumnSpread) {
  const columns: Column[] = []
  if (spread.show && m.getZoom() >= COLUMN_MIN_ZOOM) {
    const statusColor = statusColors()
    const bounds = m.getBounds()
    const offsets = columnOffsets(data)
    for (const feature of data.features) {
      const depth = feature.properties.depth
      if (depth == null || !Number.isFinite(depth) || depth <= 0) continue
      const [lon, lat] = feature.geometry.coordinates
      if (![lon - 360, lon, lon + 360].some((wrappedLon) => bounds.contains([wrappedLon, lat]))) continue
      const sections: Column['sections'] = []
      const addSection = (top: number, bottom: number, color: string, lithology?: string) => {
        if (bottom <= top) return
        sections.push({
          ...feature.properties, base_m: depth - bottom,
          height_m: depth - top,
          color, ...(lithology ? { layer_top: top, layer_bottom: bottom, layer_lithology: lithology,
            pattern: patternId(lithology in LITHOLOGY ? lithology : 'unknown') } : {}),
        })
      }
      const strip = strips.get(feature.properties.id)
      if (!strip?.length) {
        addSection(0, depth, statusColor[feature.properties.review_status])
      } else {
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
        sections.push({
          ...feature.properties, base_m: depth,
          height_m: depth + 0.5, color: statusColor[feature.properties.review_status],
        })
      }
      const to = offsets.get(feature.properties.id) ?? NO_OFFSET
      columns.push({ lon, lat, to, from: to, sections, props: feature.properties })
    }
  }
  // Columns slide out when they first appear and when their layout changes. A column panned into view
  // keeps the position it would have had anyway, and panning never restarts a running slide.
  const now = performance.now()
  const appearing = spread.drawn.size === 0
  const previous = new Map(spread.columns.map((c) => [c.props.id, c]))
  const changed = columns.some((c) => {
    const before = previous.get(c.props.id)
    return appearing ? c.to !== NO_OFFSET : !!before && !sameOffset(before.to, c.to)
  })
  if (changed) {
    for (const c of columns) c.from = appearing ? NO_OFFSET : spread.drawn.get(c.props.id) ?? c.to
    spread.start = now
  } else {
    for (const c of columns) c.from = previous.get(c.props.id)?.from ?? c.to
  }
  spread.columns = columns
  cancelAnimationFrame(spread.frame)
  const step = () => {
    if (!m.getSource('depth-markers')) return  // a style switch is under way
    const t = REDUCED_MOTION.matches ? 1 : Math.min(1, (performance.now() - spread.start) / SPREAD_MS)
    spread.drawn = drawColumns(m, spread.columns, 1 - (1 - t) ** 3)
    if (t < 1) spread.frame = requestAnimationFrame(step)
  }
  step()
}

const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)')

function sameOffset(a: Offset, b: Offset) {
  return Math.abs(a[0] - b[0]) < 0.01 && Math.abs(a[1] - b[1]) < 0.01
}

const layouts = new WeakMap<FeatureCollection<MapPoint>, Map<string, Offset>>()

/**
 * Display offsets for holes whose columns would merge, by data set, so that panning never reshuffles them.
 * Holes at one spot (often a place centre standing in for unknown coordinates) stand on a ring around
 * it, leaving the spot itself free for its marker. Columns that still overlap are pushed apart only as
 * far as needed, which keeps every column next to its hole and lets a row of close holes stay a row.
 */
function columnOffsets(data: FeatureCollection<MapPoint>) {
  const cached = layouts.get(data)
  if (cached) return cached
  const holes = data.features
    .filter((f) => f.properties.depth != null && Number.isFinite(f.properties.depth) && f.properties.depth > 0)
    .map((f) => {
      const [lon, lat] = f.geometry.coordinates
      return { id: f.properties.id, x: lon * M_PER_DEG_LON * Math.cos((lat * Math.PI) / 180), y: lat * M_PER_DEG_LAT }
    })
  const spots = new Map<string, number[]>()
  holes.forEach((h, i) => {
    const key = `${Math.round(h.x)},${Math.round(h.y)}`
    spots.set(key, [...(spots.get(key) ?? []), i])
  })
  const sx = holes.map((h) => h.x), sy = holes.map((h) => h.y)
  for (const spot of spots.values()) {
    if (spot.length < 2) continue
    const ring = Math.max(SPREAD_SPACING_M / (2 * Math.sin(Math.PI / spot.length)), COLUMN_RADIUS_M + SPOT_CLEARANCE_M)
    spot.sort((a, b) => holes[a].id.localeCompare(holes[b].id)).forEach((i, k) => {
      const angle = Math.PI / 2 + (2 * Math.PI * k) / spot.length
      sx[i] = holes[i].x + Math.cos(angle) * ring
      sy[i] = holes[i].y + Math.sin(angle) * ring
    })
  }
  const parent = holes.map((_, i) => i)
  const root = (i: number): number => (parent[i] === i ? i : (parent[i] = root(parent[i])))
  const cells = new Map<string, number[]>()
  const cell = (x: number, y: number) => `${Math.floor(x / SPREAD_SPACING_M)},${Math.floor(y / SPREAD_SPACING_M)}`
  holes.forEach((_, i) => {
    const cx = Math.floor(sx[i] / SPREAD_SPACING_M), cy = Math.floor(sy[i] / SPREAD_SPACING_M)
    for (let ox = -1; ox <= 1; ox++) for (let oy = -1; oy <= 1; oy++) {
      for (const j of cells.get(`${cx + ox},${cy + oy}`) ?? []) {
        if (Math.hypot(sx[j] - sx[i], sy[j] - sy[i]) < SPREAD_SPACING_M) parent[root(i)] = root(j)
      }
    }
    const key = cell(sx[i], sy[i])
    cells.set(key, [...(cells.get(key) ?? []), i])
  })
  const groups = new Map<number, number[]>()
  holes.forEach((_, i) => groups.set(root(i), [...(groups.get(root(i)) ?? []), i]))
  const offsets = new Map<string, Offset>()
  holes.forEach((h, i) => { if (sx[i] !== h.x || sy[i] !== h.y) offsets.set(h.id, [sx[i] - h.x, sy[i] - h.y]) })
  for (const group of groups.values()) {
    if (group.length < 2) continue
    const x = group.map((i) => sx[i]), y = group.map((i) => sy[i])
    for (let round = 0; round < 120; round++) {
      let overlap = false
      for (let a = 0; a < group.length; a++) {
        for (let b = a + 1; b < group.length; b++) {
          let dx = x[b] - x[a], dy = y[b] - y[a]
          const d = Math.hypot(dx, dy)
          if (d >= SPREAD_SPACING_M - 0.01) continue
          overlap = true
          if (d < 0.01) {
            dx = 1; dy = 0
          } else {
            dx /= d; dy /= d
          }
          const push = (SPREAD_SPACING_M - d) / 2
          x[a] -= dx * push; y[a] -= dy * push
          x[b] += dx * push; y[b] += dy * push
        }
      }
      if (!overlap) break
    }
    group.forEach((i, k) => offsets.set(holes[i].id, [x[k] - holes[i].x, y[k] - holes[i].y]))
  }
  layouts.set(data, offsets)
  return offsets
}

/** Draws each column `t` (0 to 1) of the way from its start to its target, with a leader line back to the hole. */
function drawColumns(m: MapLibre, columns: Column[], t: number) {
  const features: GeoJSONFeature<Polygon>[] = []
  const leaders: GeoJSONFeature[] = []
  const spots = new Map<string, GeoJSONFeature>()
  const drawn = new Map<string, Offset>()
  for (const c of columns) {
    const east = c.from[0] + (c.to[0] - c.from[0]) * t
    const north = c.from[1] + (c.to[1] - c.from[1]) * t
    drawn.set(c.props.id, [east, north])
    const [lon, lat] = offsetPoint([c.lon, c.lat], east, north)
    const geometry = circlePolygon(lon, lat, COLUMN_RADIUS_M, 24).geometry
    for (const properties of c.sections) features.push({ type: 'Feature', geometry, properties })
    if (c.to !== NO_OFFSET) {
      leaders.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [[c.lon, c.lat], [lon, lat]] } })
      spots.set(`${c.lon},${c.lat}`, { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [c.lon, c.lat] } })
    }
  }
  leaders.push(...spots.values())
  ;(m.getSource('depth-markers') as GeoJSONSource).setData({ type: 'FeatureCollection', features })
  ;(m.getSource('column-leaders') as GeoJSONSource).setData({ type: 'FeatureCollection', features: leaders })
  return drawn
}
