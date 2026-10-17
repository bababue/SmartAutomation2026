import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { estimationTooltip, tooltipContent } from '../boreholeTooltip'
import { lithStyle, patternId } from '../lithology'
import { MAX_JOIN_GAP_M, correlateBoundaries, correlateLayers } from '../sectionLayers'
import type { SectionBand } from '../sectionLayers'
import type { BoreholeSummary, Feature } from '../types'
import { projectToRoute, routeLength, type TunnelRoute } from '../tunnelRoute'

interface Props {
  features: Feature[]
  route: TunnelRoute
  selectedId: string | null
  onSelect: (id: string) => void
  onRemove: (id: string) => void
  onClear: () => void
}

const R = 6_371_000
function distance([lon1, lat1]: [number, number], [lon2, lat2]: [number, number]) {
  const p1 = (lat1 * Math.PI) / 180, p2 = (lat2 * Math.PI) / 180
  const dp = p2 - p1, dl = ((lon2 - lon1) * Math.PI) / 180
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(a))
}

/** Half-width of the band around a joined rock head, as a share of the distance to the nearest hole. */
const UNCERTAINTY = 0.1
const SAMPLES = 12

const fmtDist = (m: number) => (m >= 1000 ? `${(m / 1000).toLocaleString('en', { maximumFractionDigits: 1 })} km` : `${m > 0 && m < 1 ? m.toFixed(2) : m < 10 && m > 0 ? m.toFixed(1) : Math.round(m)} m`)
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value))
const tickStep = (range: number) => {
  const base = 10 ** Math.floor(Math.log10(Math.max(range, 0.001) / 5))
  const scaled = range / 5 / base
  return base * (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10)
}

interface Viewport { key: string; from: number; to: number; low: number; high: number }
interface ZoomSelection { x0: number; y0: number; x1: number; y1: number }

/** Boreholes along a route, with lithology inferred between adjacent logs. */
export function SectionView({ features, route, selectedId, onSelect, onRemove, onClear }: Props) {
  const section = useRef<HTMLElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const tooltip = useRef<HTMLDivElement>(null)
  const drag = useRef<{ pointerId: number; x: number; y: number; holeId: string | null; zooming: boolean } | null>(null)
  const suppressClick = useRef(false)
  const [width, setWidth] = useState(900)
  const [viewport, setViewport] = useState<Viewport | null>(null)
  const [selection, setSelection] = useState<ZoomSelection | null>(null)
  const [correlate, setCorrelate] = useState(true)
  const [helpOpen, setHelpOpen] = useState(false)  // phones only: the text hides behind '?'
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, entry.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // The map's bottom controls move up above the tray (.map-wrap-section in index.css).
  useEffect(() => {
    const el = section.current, host = el?.parentElement
    if (!el || !host) return
    const ro = new ResizeObserver(() => host.style.setProperty('--tray', `${el.offsetHeight}px`))
    ro.observe(el)
    return () => { ro.disconnect(); host.style.removeProperty('--tray') }
  }, [])

  const routePositions = useMemo(() => new Map(features.map((feature) => [feature.properties.id,
    projectToRoute(feature.geometry.coordinates, route.coordinates)] as const)), [features, route])
  const ordered = useMemo(() => [...features].sort((a, b) =>
    (routePositions.get(a.properties.id)?.chainageM ?? 0) - (routePositions.get(b.properties.id)?.chainageM ?? 0)
    || a.properties.id.localeCompare(b.properties.id)), [features, routePositions])
  const sectionKey = `${route.coordinates.map((point) => point.join(',')).join(';')}|${ordered.map((f) => f.properties.id).join('|')}`
  const layerBands = useMemo(() => ordered.slice(1).map((f, j) =>
    correlateLayers(ordered[j].properties.strip ?? [], f.properties.strip ?? []),
  ), [ordered])
  const boundaries = useMemo(() => ordered.slice(1).map((f, j) =>
    correlateBoundaries(ordered[j].properties.strip ?? [], f.properties.strip ?? [],
      distance(ordered[j].geometry.coordinates, f.geometry.coordinates)),
  ), [ordered])
  const byElevation = ordered.every((f) => f.properties.elevation != null)
  const height = 290
  const m = { left: 58, right: 24, top: 36, bottom: 56 }
  const col = 6

  const layout = useMemo(() => {
    const positions = ordered.map((feature) => routePositions.get(feature.properties.id)?.chainageM ?? 0)
    const gaps = positions.slice(1).map((position, i) => position - positions[i])
    const total = routeLength(route.coordinates)
    const plotWidth = Math.max(1, width - m.left - m.right - col)
    const tops = ordered.map((f) => (byElevation ? f.properties.elevation! : 0))
    const bottoms = ordered.map((f, i) => {
      const depth = f.properties.depth ?? Math.max(0, ...(f.properties.strip ?? []).map((s) => s[1]))
      return byElevation ? tops[i] - depth : depth
    })
    const diameter = route.diameterM != null && route.diameterM > 0 ? route.diameterM : 0
    let tunnel: [number, number][] | null = null
    if (route.tunnelMode === 'depth' && route.depthM != null && route.depthM >= 0) {
      tunnel = byElevation
        ? positions.map((station, i) => [station, tops[i] - route.depthM!])
        : [[0, route.depthM], [total, route.depthM]]
    } else if (route.tunnelMode === 'elevation' && byElevation &&
      route.startElevationM != null && route.endElevationM != null) {
      tunnel = [[0, route.startElevationM], [total, route.endElevationM]]
    }
    const tunnelValues = tunnel?.map(([, value]) => value) ?? []
    const vMax = byElevation ? Math.max(...tops, ...tunnelValues.map((value) => value + diameter / 2)) : 0
    const vMin = byElevation ? Math.min(...bottoms, ...tunnelValues.map((value) => value - diameter / 2))
      : Math.max(...bottoms, ...tunnelValues.map((value) => value + diameter / 2))
    return { gaps, total, positions, plotWidth, svgWidth: m.left + plotWidth + col + m.right, tops, vMax, vMin, tunnel, diameter }
  }, [ordered, route, routePositions, width, byElevation, m.left, m.right])

  const plotH = height - m.top - m.bottom
  const active = viewport?.key === sectionKey ? viewport : null
  const from = active?.from ?? 0, to = active?.to ?? Math.max(1, layout.total)
  const low = active?.low ?? (byElevation ? layout.vMin : 0)
  const high = Math.max(low + 0.1, active?.high ?? (byElevation ? layout.vMax : layout.vMin))
  const xs = layout.positions.map((metres) => m.left + col / 2 + ((metres - from) / (to - from)) * layout.plotWidth)
  const y = (v: number) =>
    byElevation
      ? m.top + ((high - v) / Math.max(0.1, high - low)) * plotH
      : m.top + ((v - low) / Math.max(0.1, high - low)) * plotH
  const step = tickStep(high - low)
  /** Plot value of a depth below ground in hole i: a level, or the depth itself. */
  const at = (i: number, depth: number) => (byElevation ? layout.tops[i] - depth : depth)

  /** Line and uncertainty band from depth da in hole j to depth db in hole j + 1. */
  const joinPath = (j: number, da: number, db: number) => {
    const xa = xs[j], xb = xs[j + 1]
    const pts = Array.from({ length: SAMPLES + 1 }, (_, k) => {
      const t = k / SAMPLES
      const v = at(j, da) + (at(j + 1, db) - at(j, da)) * t
      return { x: xa + (xb - xa) * t, v, half: UNCERTAINTY * layout.gaps[j] * Math.min(t, 1 - t) }
    })
    return {
      line: pts.map((q) => `${q.x},${y(q.v)}`).join(' '),
      band: [...pts.map((q) => `${q.x},${y(q.v + q.half)}`), ...[...pts].reverse().map((q) => `${q.x},${y(q.v - q.half)}`)].join(' '),
    }
  }
  /** Joins a ground value between neighbours: [pairIndex, line points, band polygon points]. */
  const joins = (pick: (g: NonNullable<Feature['properties']['ground']>) => number | null) =>
    ordered.slice(1).flatMap((f, j) => {
      const va = ordered[j].properties.ground, vb = f.properties.ground
      const da = va ? pick(va) : null, db = vb ? pick(vb) : null
      if (da == null || db == null || layout.gaps[j] > MAX_JOIN_GAP_M ||
        ((routePositions.get(ordered[j].properties.id)?.offsetM ?? Infinity) > route.corridorM ||
          (routePositions.get(f.properties.id)?.offsetM ?? Infinity) > route.corridorM)) return []
      const { line, band } = joinPath(j, da, db)
      return [[j, line, band] as const]
    })
  const rockJoins = joins((g) => g.rock_head_m)
  const waterJoins = joins((g) => g.groundwater_m)
  const boundaryJoins = correlate ? boundaries.flatMap((list, j) => list
    // The rock head is drawn already where both holes reach rock at this boundary.
    .filter((b) => !(b.below === 'rock' && ordered[j].properties.ground?.rock_head_m === b.leftDepth
      && ordered[j + 1].properties.ground?.rock_head_m === b.rightDepth))
    .map((b, k) => ({ key: `${j}-${k}`, j, b, ...joinPath(j, b.leftDepth, b.rightDepth) }))) : []

  const ticks: number[] = []
  for (let v = Math.ceil(low / step) * step; v <= high; v += step) ticks.push(v)
  const horizontalStep = tickStep(to - from)
  const horizontalTicks: number[] = []
  for (let v = Math.ceil(from / horizontalStep) * horizontalStep; v <= to; v += horizontalStep) horizontalTicks.push(v)
  const labels = ordered.map((f, i) => ({
    index: i,
    x: xs[i],
    width: Math.max(42, f.properties.name.length * 7 + 10),
  }))
  const visibleLabels = new Set<number>()
  for (const label of [...labels].sort((a, b) => Number(ordered[b.index].properties.id === selectedId) - Number(ordered[a.index].properties.id === selectedId) || a.index - b.index)) {
    if (label.x < m.left || label.x > layout.svgWidth - m.right) continue
    if ([...visibleLabels].every((index) => {
      const other = labels[index]
      return Math.abs(label.x - other.x) >= (label.width + other.width) / 2 + 6
    })) visibleLabels.add(label.index)
  }
  const placeTooltip = (event: ReactMouseEvent<SVGElement>, content: HTMLElement) => {
    if (drag.current?.zooming) return
    const host = section.current, tip = tooltip.current
    if (!host || !tip) return
    tip.firstElementChild?.replaceChildren(content)
    tip.hidden = false
    const bounds = host.getBoundingClientRect()
    const cursorX = event.clientX - bounds.left, cursorY = event.clientY - bounds.top
    const left = cursorX + 12 + tip.offsetWidth > bounds.width - 8 ? cursorX - tip.offsetWidth - 12 : cursorX + 12
    const top = cursorY + 12 + tip.offsetHeight > bounds.height - 8 ? cursorY - tip.offsetHeight - 12 : cursorY + 12
    tip.style.left = `${Math.max(8, left)}px`
    tip.style.top = `${Math.max(8, top)}px`
  }
  const showTooltip = (event: ReactMouseEvent<SVGElement>, p: BoreholeSummary, layer?: NonNullable<BoreholeSummary['strip']>[number]) => {
    const content = tooltipContent(layer ? { ...p, layer_top: layer[0], layer_bottom: layer[1], layer_lithology: layer[2] } : p)
    const position = routePositions.get(p.id)
    if (route && position) {
      const line = document.createElement('div')
      line.className = 'tip-line'
      line.textContent = `Station ${fmtDist(position.chainageM)} · ${Math.round(position.offsetM)} m from route`
      content.append(line)
    }
    placeTooltip(event, content)
  }
  const showHoleTooltip = (event: ReactMouseEvent<SVGGElement>, p: BoreholeSummary, i: number) => {
    const svg = event.currentTarget.ownerSVGElement
    if (!svg) return
    const fraction = (event.clientY - svg.getBoundingClientRect().top - m.top) / plotH
    const level = byElevation ? high - fraction * (high - low) : low + fraction * (high - low)
    const depth = byElevation ? layout.tops[i] - level : level
    const layer = p.strip?.find(([top, bottom]) => depth >= top && depth <= bottom)
    showTooltip(event, p, layer)
  }
  const showEstimation = (event: ReactMouseEvent<SVGPolygonElement>, band: SectionBand, j: number) => {
    const svg = event.currentTarget.ownerSVGElement
    if (!svg) return
    const x1 = xs[j], x2 = xs[j + 1]
    const fraction = x2 === x1 ? 0.5 : Math.max(0, Math.min(1, (event.clientX - svg.getBoundingClientRect().left - x1) / (x2 - x1)))
    const top = band.leftTop + (band.rightTop - band.leftTop) * fraction
    const bottom = band.leftBottom + (band.rightBottom - band.leftBottom) * fraction
    const content = estimationTooltip(band.lithology, top, bottom, ordered[j].properties.name, ordered[j + 1].properties.name)
    if (layout.gaps[j] > MAX_JOIN_GAP_M) {
      const warning = document.createElement('span')
      warning.className = 'tip-line'
      warning.textContent = `${fmtDist(layout.gaps[j])} between boreholes · lower confidence`
      content.append(warning)
    }
    placeTooltip(event, content)
  }
  const hideTooltip = () => { if (tooltip.current) tooltip.current.hidden = true }
  const pointerPosition = (event: ReactPointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: clamp(event.clientX - rect.left, m.left + col / 2, m.left + col / 2 + layout.plotWidth),
      y: clamp(event.clientY - rect.top, m.top, m.top + plotH),
    }
  }
  const startSelection = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) return
    suppressClick.current = false
    const { x, y } = pointerPosition(event)
    const rect = event.currentTarget.getBoundingClientRect()
    if (event.clientX - rect.left < m.left || event.clientX - rect.left > layout.svgWidth - m.right ||
        event.clientY - rect.top < m.top || event.clientY - rect.top > m.top + plotH) return
    const target = event.target as Element
    const holeId = target.closest<SVGGElement>('.section-hole')?.dataset.holeId ?? null
    drag.current = { pointerId: event.pointerId, x, y, holeId, zooming: false }
    event.currentTarget.setPointerCapture(event.pointerId)
    event.preventDefault()
  }
  const moveSelection = (event: ReactPointerEvent<SVGSVGElement>) => {
    const start = drag.current
    if (!start || start.pointerId !== event.pointerId) return
    const { x, y } = pointerPosition(event)
    if (!start.zooming && Math.hypot(x - start.x, y - start.y) < 4) return
    if (!start.zooming) { start.zooming = true; hideTooltip() }
    event.preventDefault()
    setSelection({ x0: start.x, y0: start.y, x1: x, y1: y })
  }
  const finishSelection = (event: ReactPointerEvent<SVGSVGElement>) => {
    const start = drag.current
    if (!start || start.pointerId !== event.pointerId) return
    const { x, y } = pointerPosition(event)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    suppressClick.current = true
    if (start.zooming && Math.abs(x - start.x) >= 16 && Math.abs(y - start.y) >= 16) {
      const x0 = Math.min(start.x, x), x1 = Math.max(start.x, x)
      const y0 = Math.min(start.y, y), y1 = Math.max(start.y, y)
      const left = m.left + col / 2
      const nextFrom = layout.total ? from + (x0 - left) / layout.plotWidth * (to - from) : 0
      const nextTo = layout.total ? from + (x1 - left) / layout.plotWidth * (to - from) : 1
      const nextLow = byElevation ? high - (y1 - m.top) / plotH * (high - low) : low + (y0 - m.top) / plotH * (high - low)
      const nextHigh = byElevation ? high - (y0 - m.top) / plotH * (high - low) : low + (y1 - m.top) / plotH * (high - low)
      setViewport({ key: sectionKey, from: nextFrom, to: nextTo, low: nextLow, high: nextHigh })
    } else if (!start.zooming && start.holeId) {
      onSelect(start.holeId)
    }
    drag.current = null
    setSelection(null)
  }
  const cancelSelection = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (drag.current?.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    drag.current = null
    setSelection(null)
  }

  return (
    <section ref={section} className="section" aria-label="Section view">
      <header className="section-head">
        <h2>{`${route.name || 'Tunnel route'} · ${ordered.length} borehole${ordered.length === 1 ? '' : 's'}`}</h2>
        <button className="btn btn-small section-help-toggle" onClick={() => setHelpOpen(!helpOpen)}
          aria-expanded={helpOpen} aria-controls="section-help" aria-label="How to read the section">?</button>
        <p id="section-help" className={`muted section-help${helpOpen ? ' section-help-open' : ''}`}>
          {byElevation ? 'Levels in m above datum as printed on each log.' : 'Depth below ground: not every log states its ground level.'}
          {` Stations follow the tunnel route (${fmtDist(layout.total)}); borehole offsets appear on hover.`}
          {' '}Rock head (dashed) and groundwater (blue) are joined between holes up to {MAX_JOIN_GAP_M} m apart,
          with a band of ±{UNCERTAINTY * 100} % of the distance to the nearest hole
          {byElevation ? '' : ', assuming level ground'}. Coloured layers connect adjacent logs along the route;
          longer spans are shown with a lighter pattern because their geology is less certain.
          {route.tunnelMode === 'elevation' && !byElevation && route.startElevationM != null &&
            ' Tunnel elevations need a ground level on every selected log.'}
          {correlate && ' Dotted lines join matching layer boundaries; ambiguous units stay open.'}
          {' '}Drag across an area to zoom both axes.
        </p>
        <label className="section-correlate">
          <input type="checkbox" checked={correlate} onChange={(e) => setCorrelate(e.target.checked)} /> Correlate layers
        </label>
        <button className="btn" onClick={() => setViewport(null)} disabled={!active}>Reset zoom</button>
        <button className="btn" onClick={onClear}>Clear boreholes</button>
      </header>
      <div ref={box} className="section-plot">
        <svg width={layout.svgWidth} height={height} role="img" aria-label={`Section with ${ordered.length} boreholes over ${fmtDist(layout.total)}`}
          onPointerDown={startSelection} onPointerMove={moveSelection} onPointerUp={finishSelection}
          onPointerCancel={cancelSelection}
          onClickCapture={(event) => { if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false } }}>
          <defs><clipPath id="section-plot-clip"><rect x={m.left} y={m.top} width={layout.svgWidth - m.left - m.right} height={plotH} /></clipPath></defs>
          <g clipPath="url(#section-plot-clip)">
          {layerBands.flatMap((bands, j) => bands.map((band, k) => {
            const x1 = xs[j], x2 = xs[j + 1]
            const points = [
              `${x1},${y(at(j, band.leftTop))}`,
              `${x2},${y(at(j + 1, band.rightTop))}`,
              `${x2},${y(at(j + 1, band.rightBottom))}`,
              `${x1},${y(at(j, band.leftBottom))}`,
            ].join(' ')
            return <polygon key={`${j}-${k}`} points={points} fill={`url(#${patternId(band.lithology)})`}
              className={`section-layer-band${layout.gaps[j] > MAX_JOIN_GAP_M ? ' section-layer-band-distant' : ''}`}
              role="img" tabIndex={0} aria-label={`Estimation: ${lithStyle(band.lithology).label} between ${ordered[j].properties.name} and ${ordered[j + 1].properties.name}`}
              onMouseEnter={(event) => showEstimation(event, band, j)} onMouseMove={(event) => showEstimation(event, band, j)}
              onMouseLeave={hideTooltip} />
          }))}
          </g>
          {ticks.map((v) => (
            <g key={v}>
              <line x1={m.left - 4} x2={layout.svgWidth - m.right} y1={y(v)} y2={y(v)} className="section-grid" />
              <text x={m.left - 8} y={y(v) + 4} textAnchor="end" className="section-tick">{v.toLocaleString('en', { maximumFractionDigits: 3 })}</text>
            </g>
          ))}
          <text x={14} y={m.top + plotH / 2} className="section-tick" transform={`rotate(-90 14 ${m.top + plotH / 2})`} textAnchor="middle">
            {byElevation ? 'Level (m)' : 'Depth (m)'}
          </text>
          <line x1={m.left + col / 2} x2={m.left + col / 2 + layout.plotWidth} y1={height - 33} y2={height - 33} className="section-axis" />
          {horizontalTicks.map((v) => {
            const x = m.left + col / 2 + (v - from) / (to - from) * layout.plotWidth
            return <g key={v}>
              <line x1={x} x2={x} y1={height - 33} y2={height - 27} className="section-axis" />
              <text x={x} y={height - 14} textAnchor="middle" className="section-tick">{fmtDist(v)}</text>
            </g>
          })}
          <text x={layout.svgWidth - m.right} y={height - 2} textAnchor="end" className="section-tick">
            Station along tunnel route
          </text>
          <g clipPath="url(#section-plot-clip)">
          {byElevation && ordered.length > 1 && (
            <polyline
              className="section-ground"
              points={ordered.map((_, i) => `${xs[i]},${y(layout.tops[i])}`).join(' ')}
            />
          )}
          {rockJoins.map(([j, line, poly]) => (
            <g key={`rock-${j}`}>
              <polygon points={poly!} className="section-band" />
              <polyline points={line} className="section-rock-join" />
            </g>
          ))}
          {boundaryJoins.map(({ key, j, b, line, band }) => (
            <g key={`boundary-${key}`}>
              <polygon points={band} className="section-band" />
              <polyline points={line} className="section-boundary-join">
                <title>{`Estimated boundary ${b.above} over ${b.below} between ${ordered[j].properties.name} (${b.leftDepth} m) and ${ordered[j + 1].properties.name} (${b.rightDepth} m), ±${UNCERTAINTY * 100} % of the distance to the nearest hole`}</title>
              </polyline>
            </g>
          ))}
          {waterJoins.map(([j, line]) => <polyline key={`water-${j}`} points={line} className="section-water-join" />)}
          {layout.tunnel && layout.tunnel.length > 1 && <>
            {layout.diameter > 0 && <polygon className="section-tunnel-band" points={[
              ...layout.tunnel.map(([station, value]) => `${m.left + col / 2 + (station - from) / (to - from) * layout.plotWidth},${y(value + layout.diameter / 2)}`),
              ...[...layout.tunnel].reverse().map(([station, value]) => `${m.left + col / 2 + (station - from) / (to - from) * layout.plotWidth},${y(value - layout.diameter / 2)}`),
            ].join(' ')} />}
            <polyline className="section-tunnel-axis" points={layout.tunnel.map(([station, value]) =>
              `${m.left + col / 2 + (station - from) / (to - from) * layout.plotWidth},${y(value)}`).join(' ')}>
              <title>Planned tunnel axis{layout.diameter > 0 ? ` · ${layout.diameter} m diameter` : ''}</title>
            </polyline>
          </>}
          {layout.tunnel?.length === 1 && <circle className="section-tunnel-point"
            cx={m.left + col / 2 + (layout.tunnel[0][0] - from) / (to - from) * layout.plotWidth}
            cy={y(layout.tunnel[0][1])} r={4}><title>Planned tunnel axis at this borehole</title></circle>}
          {ordered.map((f, i) => {
            const p = f.properties
            const g = p.ground
            const x = xs[i]
            if (x < m.left - col || x > layout.svgWidth - m.right + col) return null
            const base = byElevation ? layout.tops[i] : 0
            const depth = p.depth ?? Math.max(0, ...(p.strip ?? []).map((layer) => layer[1]))
            const nearest = Math.min(i > 0 ? x - xs[i - 1] : Infinity,
              i < ordered.length - 1 ? xs[i + 1] - x : Infinity)
            const lineWidth = Math.max(1, Math.min(col, nearest * 0.7))
            const hitWidth = Math.max(lineWidth, Math.min(24, nearest * 0.9))
            return (
              <g key={p.id} data-hole-id={p.id} className={`section-hole${p.id === selectedId ? ' section-hole-on' : ''}`}
                role="button" tabIndex={0} aria-label={`Select ${p.name}${p.id === selectedId ? ', press Delete to remove from section' : ''}`}
                onMouseEnter={(event) => showHoleTooltip(event, p, i)} onMouseMove={(event) => showHoleTooltip(event, p, i)} onMouseLeave={hideTooltip}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(p.id) }
                  if (p.id === selectedId && (event.key === 'Delete' || event.key === 'Backspace')) { event.preventDefault(); onRemove(p.id) }
                }}>
                <line x1={x} x2={x} y1={y(base)} y2={y(at(i, depth))} className="section-hole-spine" />
                <rect x={x - hitWidth / 2} y={y(base)} width={hitWidth} height={Math.max(2, y(at(i, depth)) - y(base))} fill="transparent" />
                {(p.strip ?? []).map(([top, bottom, lith], j) => {
                  const y1 = byElevation ? y(base - top) : y(top)
                  const y2 = byElevation ? y(base - bottom) : y(bottom)
                  return (
                    <rect key={j} x={x - lineWidth / 2} y={y1} width={lineWidth} height={Math.max(1, y2 - y1)} fill={lithStyle(lith).fill} className="section-cell" />
                  )
                })}
                {g?.weathered_rock_top_m != null && (
                  <line x1={x - 3} x2={x + 3} y1={y(at(i, g.weathered_rock_top_m))} y2={y(at(i, g.weathered_rock_top_m))} className="section-weathered">
                    <title>{`${p.name}: decomposed rock from ${g.weathered_rock_top_m} m`}</title>
                  </line>
                )}
                {g?.rock_head_m != null && (
                  <line x1={x - 3} x2={x + 3} y1={y(at(i, g.rock_head_m))} y2={y(at(i, g.rock_head_m))} className="section-rock">
                    <title>{`${p.name}: rock head at ${g.rock_head_m} m below ground`}</title>
                  </line>
                )}
                {g?.groundwater_m != null && (
                  <path d={`M${x + 4},${y(at(i, g.groundwater_m)) - 5} h6 l-3,5 z`} className="section-gw">
                    <title>{`${p.name}: groundwater at ${g.groundwater_m} m below ground`}</title>
                  </path>
                )}
              </g>
            )
          })}
          </g>
          {ordered.map((f, i) => <g key={`label-${f.properties.id}`}>
            {visibleLabels.has(i) && <text x={xs[i]} y={m.top - 18} textAnchor="middle" className="section-name"
              onClick={() => onSelect(f.properties.id)}>{f.properties.name}</text>}
            {f.properties.id === selectedId && xs[i] >= m.left && xs[i] <= layout.svgWidth - m.right &&
              <text x={xs[i] + 9} y={m.top - 5} className="section-x" onClick={() => onRemove(f.properties.id)}>×<title>Remove from section</title></text>}
          </g>)}
          {selection && <rect className="section-zoom-selection" x={Math.min(selection.x0, selection.x1)} y={Math.min(selection.y0, selection.y1)}
            width={Math.abs(selection.x1 - selection.x0)} height={Math.abs(selection.y1 - selection.y0)} />}
        </svg>
      </div>
      <div ref={tooltip} className="section-tooltip map-tooltip" hidden><div className="maplibregl-popup-content" /></div>
    </section>
  )
}
