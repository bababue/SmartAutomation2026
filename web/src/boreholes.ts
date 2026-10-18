import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchBoreholes, fetchBoreholesById, fetchDetail, fetchMapPoints } from './api'
import type { Detail, DetailUpdate, Feature, FeatureCollection, Filters, MapPoint } from './types'

export const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }
const PAGE = 500
const BY_ID_BATCH = 100

/** The records matching the filters: list pages of summaries and every match on the map, slim. Every summary seen
 * stays known, and `wantedIds` outside the loaded pages are fetched by id, since tunnels and the open record need them. */
export function useBoreholes(filters: Filters, refresh: number, wantedIds: string[]) {
  const [data, setData] = useState<FeatureCollection>(EMPTY)
  const [points, setPoints] = useState<FeatureCollection<MapPoint>>(EMPTY)
  const [loadingMore, setLoadingMore] = useState(false)
  const [known, setKnown] = useState<Map<string, Feature>>(new Map())
  const [error, setError] = useState<string | null>(null)
  const requestedIds = useRef(new Set<string>())

  const remember = useCallback((features: Feature[]) => setKnown((k) => {
    const next = new Map(k)
    features.forEach((f) => next.set(f.properties.id, f))
    return next
  }), [])

  useEffect(() => {
    const requested = requestedIds.current
    const missing = [...new Set(wantedIds)].filter((id) => !known.has(id) && !requested.has(id))
    if (!missing.length) return
    const controller = new AbortController()
    missing.forEach((id) => requested.add(id))
    const batches = Array.from({ length: Math.ceil(missing.length / BY_ID_BATCH) }, (_, index) =>
      missing.slice(index * BY_ID_BATCH, (index + 1) * BY_ID_BATCH))
    let settled = false
    Promise.all(batches.map((ids) => fetchBoreholesById(ids, controller.signal)))
      .then((pages) => { settled = true; remember(pages.flatMap((page) => page.features)) })
      .catch((cause) => { if (cause.name !== 'AbortError') { settled = true; setError(`Could not load route boreholes (${cause.message}).`) } })
    return () => {
      controller.abort()
      if (!settled) missing.forEach((id) => requested.delete(id))
    }
  }, [wantedIds, known, remember])

  // The list is loading until a response for the current filters and refresh has landed.
  const [listFor, setListFor] = useState<{ filters: Filters; refresh: number } | null>(null)
  const moreRequest = useRef<AbortController | null>(null)
  useEffect(() => {
    moreRequest.current?.abort()
    const ctrl = new AbortController()
    const request = { filters, refresh }
    Promise.all([fetchBoreholes(filters, 0, PAGE, ctrl.signal), fetchMapPoints(filters, ctrl.signal)])
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
    fetchBoreholes(filters, data.features.length, PAGE, ctrl.signal)
      .then((page) => { setData((d) => ({ ...page, features: [...d.features, ...page.features] })); remember(page.features) })
      .catch((e) => e.name !== 'AbortError' && setError(`Could not load more boreholes (${e.message}).`))
      .finally(() => setLoadingMore(false))
  }, [filters, data.features.length, remember])

  // Counts over all matches, from the server (the list may be one page).
  const lithologies = useMemo(() => {
    const counts = new Map<string, number>(data.facets?.lithologies ?? [])
    filters.lithology.forEach(({ key }) => counts.set(key, counts.get(key) ?? 0))
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [data, filters.lithology])

  return { data, points, loading, loadingMore, loadMore, known, error, lithologies }
}

/** The full record of the selected borehole. `onError` hears of a failed load; `notFound` when the id is unknown. */
export function useBoreholeDetail(selectedId: string | null, refresh: number, onError: (notFound: boolean) => void) {
  const [detail, setDetail] = useState<Detail | null>(null)
  // Kept apart from the list's error, which every list load clears, so a record that failed to open stays reported.
  const [error, setError] = useState<string | null>(null)
  // Closing drops the record, so a later selection never shows it while its own loads; switching keeps it until then.
  if (!selectedId && detail) setDetail(null)

  // Only the response for the currently selected borehole may land in state.
  useEffect(() => {
    if (!selectedId) return
    const ctrl = new AbortController()
    fetchDetail(selectedId, ctrl.signal)
      .then((d) => { setDetail(d); setError(null) })
      .catch((e) => {
        if (e.name === 'AbortError') return
        // The sheet must not keep showing the previous record; the error shows in the list.
        setDetail(null)
        const notFound = e.message.startsWith('404')
        if (notFound) {
          // An outdated link: the id leaves the address in place, so Back does not lead to it again.
          const url = new URL(window.location.href)
          url.searchParams.delete('borehole')
          window.history.replaceState(null, '', url)
          setError(`No borehole with the id “${selectedId}”: the link may be out of date.`)
        } else setError(`Could not load the borehole (${e.message}).`)
        onError(notFound)
      })
    return () => ctrl.abort()
  }, [selectedId, refresh, onError])

  const applyUpdate = useCallback((update: DetailUpdate) =>
    setDetail((d) => (d && d.borehole.borehole_id === update.borehole.borehole_id ? { ...d, ...update } : d)), [])

  // Only the open borehole's own position: while the next one loads, the previous detail must not move the map.
  const focus = useMemo(() => {
    const borehole = detail?.borehole.borehole_id === selectedId ? detail?.borehole : undefined
    const loc = borehole?.location
    if (!loc || loc.lat == null || loc.lon == null) return null
    const depth = Math.max(borehole.depth_m ?? 0, ...borehole.layers.map((layer) => layer.bottom_m))
    return { lat: loc.lat, lon: loc.lon, accuracy_m: loc.accuracy_m,
      approximate: loc.method === 'geocoded_place', hasDepth: depth > 0 }
  }, [detail, selectedId])

  return { detail, error, applyUpdate, focus }
}
