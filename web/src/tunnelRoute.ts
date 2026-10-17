import type { Feature } from './types'

export type RoutePoint = [number, number]

export interface TunnelRoute {
  name: string
  coordinates: RoutePoint[]
  corridorM: number
  tunnelMode: 'depth' | 'elevation'
  depthM: number | null
  startElevationM: number | null
  endElevationM: number | null
  diameterM: number | null
}

export interface SavedTunnel {
  id: string
  name: string
  route: TunnelRoute | null
  holeIds: string[]
}

export function tunnelProject(tunnel: SavedTunnel, features: Map<string, Feature>): string | null {
  const counts = new Map<string, number>()
  for (const id of new Set(tunnel.holeIds)) {
    const project = features.get(id)?.properties.project?.trim()
    if (project) counts.set(project, (counts.get(project) ?? 0) + 1)
  }
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], undefined, { numeric: true }))[0]?.[0] ?? null
}

interface RoutePosition {
  chainageM: number
  offsetM: number
}

export interface RouteCandidate extends RoutePosition {
  id: string
  name: string
}

const STORAGE_KEY = 'borehole-atlas.tunnel-route'
const TUNNELS_KEY = 'borehole-atlas.tunnels'
const RAD = Math.PI / 180
/** Metres per degree, for the flat approximation used over the short distances of a site or a route. */
export const M_PER_DEG_LAT = 111_132
export const M_PER_DEG_LON = 111_320  // at the equator; times cos(latitude) elsewhere

/** The point `eastM` and `northM` metres away from [lon, lat]. */
export function offsetPoint([lon, lat]: RoutePoint, eastM: number, northM: number): RoutePoint {
  return [lon + eastM / (M_PER_DEG_LON * Math.max(0.01, Math.cos(lat * RAD))), lat + northM / M_PER_DEG_LAT]
}

function longitudeDifference(a: number, b: number) {
  return ((b - a + 540) % 360) - 180
}

function segmentVector(a: RoutePoint, b: RoutePoint) {
  const latitude = ((a[1] + b[1]) / 2) * RAD
  return {
    x: longitudeDifference(a[0], b[0]) * M_PER_DEG_LON * Math.cos(latitude),
    y: (b[1] - a[1]) * M_PER_DEG_LAT,
  }
}

export function routeLength(coordinates: RoutePoint[]): number {
  return coordinates.slice(1).reduce((sum, point, i) => {
    const v = segmentVector(coordinates[i], point)
    return sum + Math.hypot(v.x, v.y)
  }, 0)
}

export function projectToRoute(point: RoutePoint, coordinates: RoutePoint[]): RoutePosition {
  let passed = 0
  let nearest: RoutePosition = { chainageM: 0, offsetM: Infinity }
  for (let i = 1; i < coordinates.length; i++) {
    const a = coordinates[i - 1], b = coordinates[i]
    const segment = segmentVector(a, b)
    const length = Math.hypot(segment.x, segment.y)
    if (!length) continue
    const latitude = ((a[1] + b[1]) / 2) * RAD
    const px = longitudeDifference(a[0], point[0]) * M_PER_DEG_LON * Math.cos(latitude)
    const py = (point[1] - a[1]) * M_PER_DEG_LAT
    const fraction = Math.max(0, Math.min(1, (px * segment.x + py * segment.y) / (length * length)))
    const offsetM = Math.hypot(px - fraction * segment.x, py - fraction * segment.y)
    if (offsetM < nearest.offsetM) nearest = { chainageM: passed + fraction * length, offsetM }
    passed += length
  }
  return nearest
}

export function nearbyBoreholes(features: Feature[], route: TunnelRoute): RouteCandidate[] {
  return features.flatMap((feature) => {
    const position = projectToRoute(feature.geometry.coordinates, route.coordinates)
    return position.offsetM <= route.corridorM
      ? [{ id: feature.properties.id, name: feature.properties.name, ...position }]
      : []
  }).sort((a, b) => a.chainageM - b.chainageM || a.id.localeCompare(b.id))
}

export function validateRouteCoordinates(value: unknown): RoutePoint[] {
  if (!Array.isArray(value) || value.length < 2 || value.length > 5000) throw new Error('A route needs 2–5000 vertices.')
  const points = value.map((item): RoutePoint => {
    if (!Array.isArray(item) || item.length < 2 || !Number.isFinite(item[0]) || !Number.isFinite(item[1]) ||
        Math.abs(item[0]) > 180 || Math.abs(item[1]) > 85) throw new Error('Route coordinates must be valid longitude/latitude pairs (WGS84).')
    return [item[0], item[1]]
  })
  if (routeLength(points) < 1) throw new Error('The route must cover at least one metre.')
  return points
}

export function routeFromGeoJson(text: string, fallbackName: string): TunnelRoute {
  const input: unknown = JSON.parse(text)
  if (!input || typeof input !== 'object') throw new Error('Expected a GeoJSON LineString.')
  const object = input as Record<string, unknown>
  const feature = object.type === 'FeatureCollection' && Array.isArray(object.features)
    ? object.features.find((item) => item && typeof item === 'object' && (item as { geometry?: { type?: string } }).geometry?.type === 'LineString')
    : object
  const record = feature && typeof feature === 'object' ? feature as Record<string, unknown> : {}
  const geometry = record.type === 'Feature' ? record.geometry : record
  if (!geometry || typeof geometry !== 'object' || (geometry as { type?: string }).type !== 'LineString') {
    throw new Error('Choose a GeoJSON LineString or a FeatureCollection containing one.')
  }
  const properties = record.properties && typeof record.properties === 'object' ? record.properties as Record<string, unknown> : {}
  return {
    name: typeof properties.name === 'string' && properties.name.trim() ? properties.name.trim() : fallbackName,
    coordinates: validateRouteCoordinates((geometry as { coordinates?: unknown }).coordinates),
    corridorM: 50,
    tunnelMode: 'depth',
    depthM: null,
    startElevationM: null,
    endElevationM: null,
    diameterM: null,
  }
}

function validRoute(value: unknown): TunnelRoute | null {
  if (!value || typeof value !== 'object') return null
  const route = value as TunnelRoute
  const coordinates = validateRouteCoordinates(route.coordinates)
  if (typeof route.name !== 'string' || !Number.isFinite(route.corridorM) ||
      !['depth', 'elevation'].includes(route.tunnelMode)) return null
  return { ...route, coordinates, corridorM: Math.max(1, Math.min(5000, route.corridorM)) }
}

export function loadTunnels(): SavedTunnel[] {
  try {
    const saved = localStorage.getItem(TUNNELS_KEY)
    if (saved) {
      const entries: unknown = JSON.parse(saved)
      if (!Array.isArray(entries)) return []
      return entries.flatMap((entry): SavedTunnel[] => {
        try {
          if (!entry || typeof entry.id !== 'string' || typeof entry.name !== 'string') return []
          const route = entry.route == null ? null : validRoute(entry.route)
          if (entry.route != null && !route) return []
          return [{ id: entry.id, name: entry.name, route,
            holeIds: Array.isArray(entry.holeIds) ? entry.holeIds.filter((id: unknown): id is string => typeof id === 'string') : [] }]
        } catch { return [] }
      })
    }
    const previous = localStorage.getItem(STORAGE_KEY)
    if (!previous) return []
    const route = validRoute(JSON.parse(previous))
    return route ? [{ id: crypto.randomUUID(), name: route.name, route, holeIds: [] }] : []
  } catch { return [] }
}

export function saveTunnels(tunnels: SavedTunnel[]) {
  try {
    localStorage.setItem(TUNNELS_KEY, JSON.stringify(tunnels))
    localStorage.removeItem(STORAGE_KEY)
  } catch { /* storage can be unavailable in private browsing */ }
}
