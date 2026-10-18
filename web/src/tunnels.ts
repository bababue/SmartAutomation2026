import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchMapPoints } from './api'
import { EMPTY } from './boreholes'
import type { FeatureCollection, MapPoint } from './types'
import { DEFAULT_FILTERS } from './url'
import { loadTunnels, nearbyBoreholes, projectToRoute, saveTunnels, validateRouteCoordinates,
  type RoutePoint, type SavedTunnel, type TunnelRoute } from './tunnelRoute'

const EMPTY_IDS: string[] = []

/** The saved tunnels, kept in localStorage, and the work on the active one: drawing its route and choosing the
 * boreholes along it. `shown`: the tunnel view is on screen, so the active route is drawn and profiled. */
export function useTunnels(activeId: string | null, routeActive: boolean, shown: boolean, refresh: number) {
  const [tunnels, setTunnels] = useState<SavedTunnel[]>(loadTunnels)
  const active = tunnels.find((tunnel) => tunnel.id === activeId) ?? null
  const route = active?.route ?? null
  const holeIds = active?.holeIds ?? EMPTY_IDS
  const [draft, setDraft] = useState<RoutePoint[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { saveTunnels(tunnels) }, [tunnels])

  const updateActive = useCallback((change: Partial<SavedTunnel>) => {
    setTunnels((items) => items.map((item) => item.id === activeId ? { ...item, ...change } : item))
  }, [activeId])
  const setHoleIds = useCallback((value: string[] | ((ids: string[]) => string[])) => {
    setTunnels((items) => items.map((item) => item.id === activeId
      ? { ...item, holeIds: typeof value === 'function' ? value(item.holeIds) : value } : item))
  }, [activeId])
  const setRoute = useCallback((next: TunnelRoute) => {
    updateActive({ name: next.name, route: next })
  }, [updateActive])
  const toggleHole = useCallback(
    (id: string) => setHoleIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id])),
    [setHoleIds],
  )

  // Every located borehole, unfiltered, as candidates along any route.
  const [points, setPoints] = useState<FeatureCollection<MapPoint>>(EMPTY)
  const [loadedFor, setLoadedFor] = useState<number | null>(null)
  useEffect(() => {
    if (!routeActive) return
    const controller = new AbortController()
    fetchMapPoints(DEFAULT_FILTERS, controller.signal)
      .then((collection) => { setPoints(collection); setError(null); setLoadedFor(refresh) })
      .catch((cause) => {
        if (cause.name === 'AbortError') return
        setError(`Could not load nearby boreholes (${cause.message}).`)
        setLoadedFor(refresh)
      })
    return () => controller.abort()
  }, [routeActive, refresh])
  const loading = routeActive && loadedFor !== refresh

  const shownRoute = shown ? route : null
  const inactiveRoutes = useMemo(() => shown ? tunnels.filter((tunnel) => tunnel.id !== activeId)
    .map((tunnel) => tunnel.route).filter((item): item is TunnelRoute => item !== null) : [], [shown, tunnels, activeId])
  const nearby = useMemo(() => shownRoute ? nearbyBoreholes(points.features, shownRoute) : [], [points, shownRoute])
  const candidates = useMemo(() => {
    if (!shownRoute) return nearby
    const ids = new Set(nearby.map((candidate) => candidate.id))
    const otherSelected = points.features.flatMap((feature) => {
      if (!holeIds.includes(feature.properties.id) || ids.has(feature.properties.id)) return []
      return [{ id: feature.properties.id, name: feature.properties.name,
        ...projectToRoute(feature.geometry.coordinates, shownRoute.coordinates) }]
    })
    return [...nearby, ...otherSelected].sort((a, b) => a.chainageM - b.chainageM)
  }, [shownRoute, nearby, points, holeIds])

  const finishDrawing = useCallback(() => {
    try {
      const coordinates = validateRouteCoordinates(draft)
      setRoute({ name: active?.name || 'Tunnel route', coordinates, corridorM: route?.corridorM ?? 50,
        tunnelMode: route?.tunnelMode ?? 'depth', depthM: route?.depthM ?? null,
        startElevationM: route?.startElevationM ?? null, endElevationM: route?.endElevationM ?? null,
        diameterM: route?.diameterM ?? null })
      setDraft(null)
      setHoleIds([])
      setError(null)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not save the route.') }
  }, [draft, route, active, setRoute, setHoleIds])

  /** Adds a tunnel without a route and returns its id. */
  const create = () => {
    const id = crypto.randomUUID()
    setTunnels((items) => [...items, { id, name: `Tunnel ${items.length + 1}`, route: null, holeIds: [] }])
    setDraft(null)
    return id
  }

  return {
    saved: tunnels, active, route, shownRoute, inactiveRoutes, holeIds, candidates, draft, loading, error,
    create,
    removeActive: () => { setTunnels((items) => items.filter((item) => item.id !== activeId)); setDraft(null) },
    rename: (name: string) => updateActive({ name, route: route ? { ...route, name } : null }),
    setRoute,
    importRoute: (imported: TunnelRoute) => { setRoute(imported); setDraft(null); setHoleIds([]); setError(null) },
    startDrawing: () => { setDraft([]); setError(null) },
    addPoint: (point: RoutePoint) => setDraft((d) => d ? [...d, point] : d),
    undoPoint: () => setDraft((d) => d ? d.slice(0, -1) : null),
    finishDrawing,
    cancelDrawing: () => setDraft(null),
    toggleHole,
    selectNearby: () => setHoleIds(nearby.map((candidate) => candidate.id)),
    clearHoles: () => setHoleIds([]),
  }
}
