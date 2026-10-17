import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchMapPoints } from './api'
import { loadTunnelRoute, nearbyBoreholes, projectToRoute, saveTunnelRoute, validateRouteCoordinates,
  type RouteCandidate, type RoutePoint, type TunnelRoute } from './tunnelRoute'
import type { Feature } from './types'
import { DEFAULT_FILTERS } from './url'

/** A switch remembered in localStorage as `on` or `off`; without a stored choice `fallback` decides. */
export function useStoredFlag(key: string, on: string, off: string, fallback: () => boolean) {
  const [value, setValue] = useState(() => {
    try {
      const saved = localStorage.getItem(key)
      if (saved === on || saved === off) return saved === on
    } catch { /* storage blocked */ }
    return fallback()
  })
  useEffect(() => {
    try { localStorage.setItem(key, value ? on : off) } catch { /* private mode: the choice lasts this visit */ }
  }, [key, on, off, value])
  return [value, setValue] as const
}

/**
 * The tunnel route saved in this browser, the line being drawn, and the boreholes near the route among all
 * records (not only the filtered ones). `selectedIds` outside the corridor stay listed as candidates.
 */
export function useTunnelRoute(active: boolean, refresh: number, selectedIds: string[]) {
  const [route, setRoute] = useState<TunnelRoute | null>(loadTunnelRoute)
  const [draft, setDraft] = useState<RoutePoint[] | null>(null)
  const [points, setPoints] = useState<Feature[]>([])
  const [settled, setSettled] = useState<number | null>(null)  // the refresh the points or the error belong to
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { saveTunnelRoute(route) }, [route])

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    fetchMapPoints(DEFAULT_FILTERS, controller.signal)
      .then((collection) => { setPoints(collection.features); setError(null); setSettled(refresh) })
      .catch((cause) => {
        if (cause.name === 'AbortError') return
        setError(`Could not load nearby boreholes (${cause.message}).`)
        setSettled(refresh)
      })
    return () => controller.abort()
  }, [active, refresh])

  const activeRoute = active ? route : null
  const nearby = useMemo(() => (activeRoute ? nearbyBoreholes(points, activeRoute) : []), [points, activeRoute])
  const candidates = useMemo((): RouteCandidate[] => {
    if (!activeRoute) return nearby
    const ids = new Set(nearby.map((candidate) => candidate.id))
    const otherSelected = points.flatMap((feature) => {
      if (!selectedIds.includes(feature.properties.id) || ids.has(feature.properties.id)) return []
      return [{ id: feature.properties.id, name: feature.properties.name,
        ...projectToRoute(feature.geometry.coordinates, activeRoute.coordinates) }]
    })
    return [...nearby, ...otherSelected].sort((a, b) => a.chainageM - b.chainageM)
  }, [activeRoute, nearby, points, selectedIds])

  /** Saves the drawn line as the route, keeping the settings of the previous one; false if it is not a valid route. */
  const finishDrawing = useCallback(() => {
    try {
      const coordinates = validateRouteCoordinates(draft)
      setRoute({ name: route?.name || 'Tunnel route', coordinates, corridorM: route?.corridorM ?? 50,
        tunnelMode: route?.tunnelMode ?? 'depth', depthM: route?.depthM ?? null,
        startElevationM: route?.startElevationM ?? null, endElevationM: route?.endElevationM ?? null,
        diameterM: route?.diameterM ?? null })
      setDraft(null)
      setError(null)
      return true
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save the route.')
      return false
    }
  }, [draft, route])

  return { route, setRoute, activeRoute, draft, setDraft, loading: active && settled !== refresh, error, setError,
    nearby, candidates, finishDrawing }
}
