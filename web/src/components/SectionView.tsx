import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { estimationTooltip, tooltipContent } from '../boreholeTooltip'
import { el } from '../dom'
import { fmtDistance, fmtNum } from '../format'
import { lithStyle, patternId } from '../lithology'
import { MAX_JOIN_GAP_M, correlateBoundaries, correlateLayers } from '../sectionLayers'
import type { SectionBand } from '../sectionLayers'
import type { BoreholeSummary, Feature } from '../types'
import { projectToRoute, routeLength, type TunnelRoute } from '../tunnelRoute'

interface Props {
  features: Feature[]
  route: TunnelRoute
  selectedId: string | null
  hoveredId: string | null
  onHover: (id: string | null) => void
  onSelect: (id: string) => void
  onRemove: (id: string) => void
}

/** Half-width of the band around a joined rock head, as a share of the distance to the nearest hole. */
const UNCERTAINTY = 0.1
const SAMPLES = 12
const HEIGHT = 290
const MARGIN = { left: 58, right: 24, top: 36, bottom: 56 }
const PLOT_H = HEIGHT - MARGIN.top - MARGIN.bottom
/** Width of a borehole column in px. */
const COL = 6
const PLOT_LEFT = MARGIN.left + COL / 2

/** Final depth, else the bottom of the deepest logged layer. */
const holeDepth = (p: BoreholeSummary) => p.depth ?? Math.max(0, ...(p.strip ?? []).map((layer) => layer[1]))
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value))
const tickStep = (range: number) => {
  const base = 10 ** Math.floor(Math.log10(Math.max(range, 0.001) / 5))
  const scaled = range / 5 / base
  return base * (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10)
}

interface ScreenPoint { clientX: number; clientY: number }

/** Where a keyboard-focused element shows its tooltip: its centre. */
function centre(element: Element): ScreenPoint {
  const r = element.getBoundingClientRect()
  return { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 }
}

interface Viewport { key: string; from: number; to: number; low: number; high: number }
interface ZoomSelection { x0: number; y0: number; x1: number; y1: number }

/** Dragging across the plot zooms to the box; a click without a drag picks the hole under the pointer. */
function useBoxZoom({ plotWidth, svgWidth, onZoom, onPick, onDragStart }: {
  plotWidth: number
  svgWidth: number
  onZoom: (box: ZoomSelection) => void
  onPick: (holeId: string) => void
  onDragStart: () => void
}) {
  const drag = useRef<{ pointerId: number; x: number; y: number; holeId: string | null; zooming: boolean } | null>(null)
  const suppressClick = useRef(false)
  const [selection, setSelection] = useState<ZoomSelection | null>(null)
  const pointerPosition = (event: ReactPointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: clamp(event.clientX - rect.left, PLOT_LEFT, PLOT_LEFT + plotWidth),
      y: clamp(event.clientY - rect.top, MARGIN.top, MARGIN.top + PLOT_H),
    }
  }
  const startSelection = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) return
    suppressClick.current = false
    const { x, y } = pointerPosition(event)
    const rect = event.currentTarget.getBoundingClientRect()
    if (event.clientX - rect.left < MARGIN.left || event.clientX - rect.left > svgWidth - MARGIN.right ||
        event.clientY - rect.top < MARGIN.top || event.clientY - rect.top > MARGIN.top + PLOT_H) return
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
    if (!start.zooming) { start.zooming = true; onDragStart() }
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
      onZoom({ x0: Math.min(start.x, x), x1: Math.max(start.x, x), y0: Math.min(start.y, y), y1: Math.max(start.y, y) })
    } else if (!start.zooming && start.holeId) {
      onPick(start.holeId)
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

  return {
    selection,
    zooming: () => drag.current?.zooming ?? false,
    handlers: {
      onPointerDown: startSelection, onPointerMove: moveSelection, onPointerUp: finishSelection, onPointerCancel: cancelSelection,
      onClickCapture: (event: ReactMouseEvent) => {
        if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false }
      },
    },
  }
}

/** Boreholes along a route, with lithology inferred between adjacent logs. */
export function SectionView({ features, route, selectedId, hoveredId, onHover, onSelect, onRemove }: Props) {
  const section = useRef<HTMLElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const tooltip = useRef<HTMLDivElement>(null)
  const helpDialog = useRef<HTMLDialogElement>(null)
  const [width, setWidth] = useState(900)
  const [viewport, setViewport] = useState<Viewport | null>(null)
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
      routeLength([ordered[j].geometry.coordinates, f.geometry.coordinates])),
  ), [ordered])
  const byElevation = ordered.length > 0 && ordered.every((f) => f.properties.elevation != null)

  const layout = useMemo(() => {
    const positions = ordered.map((feature) => routePositions.get(feature.properties.id)?.chainageM ?? 0)
    const gaps = positions.slice(1).map((position, i) => position - positions[i])
    const total = routeLength(route.coordinates)
    const plotWidth = Math.max(1, width - MARGIN.left - MARGIN.right - COL)
    const tops = ordered.map((f) => (byElevation ? f.properties.elevation! : 0))
    const bottoms = ordered.map((f, i) => {
      const depth = holeDepth(f.properties)
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
      : Math.max(ordered.length ? 0 : 20, ...bottoms, ...tunnelValues.map((value) => value + diameter / 2))
    return { gaps, total, positions, plotWidth, svgWidth: MARGIN.left + plotWidth + COL + MARGIN.right, tops, vMax, vMin, tunnel, diameter }
  }, [ordered, route, routePositions, width, byElevation])

  const active = viewport?.key === sectionKey ? viewport : null
  const from = active?.from ?? 0, to = active?.to ?? Math.max(1, layout.total)
  const low = active?.low ?? (byElevation ? layout.vMin : 0)
  const high = Math.max(low + 0.1, active?.high ?? (byElevation ? layout.vMax : layout.vMin))
  const xAt = (station: number) => PLOT_LEFT + ((station - from) / (to - from)) * layout.plotWidth
  const xs = layout.positions.map(xAt)
  const y = (v: number) =>
    byElevation
      ? MARGIN.top + ((high - v) / Math.max(0.1, high - low)) * PLOT_H
      : MARGIN.top + ((v - low) / Math.max(0.1, high - low)) * PLOT_H
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
  const boundaryJoins = boundaries.flatMap((list, j) => list
    // The rock head is drawn already where both holes reach rock at this boundary.
    .filter((b) => !(b.below === 'rock' && ordered[j].properties.ground?.rock_head_m === b.leftDepth
      && ordered[j + 1].properties.ground?.rock_head_m === b.rightDepth))
    .map((b, k) => ({ key: `${j}-${k}`, j, b, ...joinPath(j, b.leftDepth, b.rightDepth) })))

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
    if (label.x < MARGIN.left || label.x > layout.svgWidth - MARGIN.right) continue
    if ([...visibleLabels].every((index) => {
      const other = labels[index]
      return Math.abs(label.x - other.x) >= (label.width + other.width) / 2 + 6
    })) visibleLabels.add(label.index)
  }
  const placeTooltip = (event: ScreenPoint, content: HTMLElement) => {
    if (boxZoom.zooming()) return
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
  const showTooltip = (event: ScreenPoint, p: BoreholeSummary, layer?: NonNullable<BoreholeSummary['strip']>[number]) => {
    const content = tooltipContent(layer ? { ...p, layer_top: layer[0], layer_bottom: layer[1], layer_lithology: layer[2] } : p)
    const elevation = p.elevation
    if (layer && elevation != null) {
      content.append(el('div', 'tip-line', `Level ${fmtNum(elevation - layer[0], 1)} to ${fmtNum(elevation - layer[1], 1)} m`))
    }
    const position = routePositions.get(p.id)
    if (route && position) {
      content.append(el('div', 'tip-line', `Station ${fmtDistance(position.chainageM)} · ${Math.round(position.offsetM)} m from route`))
    }
    placeTooltip(event, content)
  }
  const showHoleTooltip = (event: ReactMouseEvent<SVGGElement>, p: BoreholeSummary, i: number) => {
    const svg = event.currentTarget.ownerSVGElement
    if (!svg) return
    const fraction = (event.clientY - svg.getBoundingClientRect().top - MARGIN.top) / PLOT_H
    const level = byElevation ? high - fraction * (high - low) : low + fraction * (high - low)
    const depth = byElevation ? layout.tops[i] - level : level
    const layer = p.strip?.find(([top, bottom]) => depth >= top && depth <= bottom)
    showTooltip(event, p, layer)
  }
  const showEstimation = (target: SVGPolygonElement, event: ScreenPoint, band: SectionBand, j: number) => {
    const svg = target.ownerSVGElement
    if (!svg) return
    const x1 = xs[j], x2 = xs[j + 1]
    const fraction = x2 === x1 ? 0.5 : Math.max(0, Math.min(1, (event.clientX - svg.getBoundingClientRect().left - x1) / (x2 - x1)))
    const top = band.leftTop + (band.rightTop - band.leftTop) * fraction
    const bottom = band.leftBottom + (band.rightBottom - band.leftBottom) * fraction
    const content = estimationTooltip(band.lithology, top, bottom, ordered[j].properties.name, ordered[j + 1].properties.name)
    if (layout.gaps[j] > MAX_JOIN_GAP_M) {
      content.append(el('span', 'tip-line', `${fmtDistance(layout.gaps[j])} between boreholes · lower confidence`))
    }
    placeTooltip(event, content)
  }
  const hideTooltip = () => { if (tooltip.current) tooltip.current.hidden = true }
  const zoomTo = ({ x0, x1, y0, y1 }: ZoomSelection) => {
    const nextFrom = layout.total ? from + (x0 - PLOT_LEFT) / layout.plotWidth * (to - from) : 0
    const nextTo = layout.total ? from + (x1 - PLOT_LEFT) / layout.plotWidth * (to - from) : 1
    const nextLow = byElevation ? high - (y1 - MARGIN.top) / PLOT_H * (high - low) : low + (y0 - MARGIN.top) / PLOT_H * (high - low)
    const nextHigh = byElevation ? high - (y0 - MARGIN.top) / PLOT_H * (high - low) : low + (y1 - MARGIN.top) / PLOT_H * (high - low)
    setViewport({ key: sectionKey, from: nextFrom, to: nextTo, low: nextLow, high: nextHigh })
  }
  const boxZoom = useBoxZoom({ plotWidth: layout.plotWidth, svgWidth: layout.svgWidth, onZoom: zoomTo, onPick: onSelect, onDragStart: hideTooltip })

  return (
    <section ref={section} className="section" aria-label="Section view">
      <header className="section-head">
        <button className="btn btn-small" onClick={() => helpDialog.current?.showModal()}>Usage guide</button>
        <button className="btn section-reset" onClick={() => setViewport(null)} disabled={!active}>Reset zoom</button>
      </header>
      <dialog ref={helpDialog} className="section-help-dialog" aria-labelledby="section-help-title">
        <div className="section-help-head">
          <h2 id="section-help-title">How to read the tunnel profile</h2>
          <button className="btn btn-icon" onClick={() => helpDialog.current?.close()} aria-label="Close information">×</button>
        </div>
        <p>Boreholes are placed at their stations along the route ({fmtDistance(layout.total)}). Hover a borehole to see its distance from the route and the measured layers.</p>
        <p>{byElevation ? 'The vertical axis shows levels above the datum printed on the borehole logs.'
          : ordered.length ? 'The vertical axis shows depth below ground because at least one borehole has no ground level.'
            : 'The vertical axis shows depth below ground until boreholes with ground levels are selected.'}</p>
        {route.tunnelMode === 'elevation' && <p>For Elevation below datum, enter a positive distance to place the tunnel below the datum. Negative values place it above the datum.</p>}
        <p>Patterned columns are borehole measurements. The coloured areas between them are estimates; lighter areas span more than {MAX_JOIN_GAP_M} m and are less certain.</p>
        <p>Dashed lines show estimated rock head, blue markers show groundwater, and the solid blue line shows the planned tunnel axis. The shaded band shows its diameter when set.</p>
        <p>Dotted lines connect matching layer boundaries in adjacent boreholes up to {MAX_JOIN_GAP_M} m apart. The match uses broad material groups in depth order and omits ambiguous boundaries. These lines are estimates.</p>
        <p>Drag a rectangle over the profile to zoom. Use Reset zoom to return to the full view. Edit the route, borehole selection, and tunnel profile in the tunnel details on the right.</p>
        {route.tunnelMode === 'elevation' && !byElevation && route.startElevationM != null &&
          <p className="section-help-warning">A tunnel axis specified by elevation needs a ground level on every selected borehole.</p>}
      </dialog>
      <div ref={box} className="section-plot">
        <svg width={layout.svgWidth} height={HEIGHT} role="img" aria-label={`Section with ${ordered.length} boreholes over ${fmtDistance(layout.total)}`}
          {...boxZoom.handlers}>
          <defs><clipPath id="section-plot-clip"><rect x={MARGIN.left} y={MARGIN.top} width={layout.svgWidth - MARGIN.left - MARGIN.right} height={PLOT_H} /></clipPath></defs>
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
              onMouseEnter={(event) => showEstimation(event.currentTarget, event, band, j)}
              onMouseMove={(event) => showEstimation(event.currentTarget, event, band, j)} onMouseLeave={hideTooltip}
              onFocus={(event) => showEstimation(event.currentTarget, centre(event.currentTarget), band, j)} onBlur={hideTooltip} />
          }))}
          </g>
          {ticks.map((v) => (
            <g key={v}>
              <line x1={MARGIN.left - 4} x2={layout.svgWidth - MARGIN.right} y1={y(v)} y2={y(v)} className="section-grid" />
              <text x={MARGIN.left - 8} y={y(v) + 4} textAnchor="end" className="section-tick">{fmtNum(v, 3)}</text>
            </g>
          ))}
          <text x={14} y={MARGIN.top + PLOT_H / 2} className="section-tick" transform={`rotate(-90 14 ${MARGIN.top + PLOT_H / 2})`} textAnchor="middle">
            {byElevation ? 'Level (m)' : 'Depth (m)'}
          </text>
          <line x1={PLOT_LEFT} x2={PLOT_LEFT + layout.plotWidth} y1={HEIGHT - 33} y2={HEIGHT - 33} className="section-axis" />
          {horizontalTicks.map((v) => {
            const x = xAt(v)
            return <g key={v}>
              <line x1={x} x2={x} y1={HEIGHT - 33} y2={HEIGHT - 27} className="section-axis" />
              <text x={x} y={HEIGHT - 14} textAnchor="middle" className="section-tick">{fmtDistance(v)}</text>
            </g>
          })}
          <text x={layout.svgWidth - MARGIN.right} y={HEIGHT - 2} textAnchor="end" className="section-tick">
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
              ...layout.tunnel.map(([station, value]) => `${xAt(station)},${y(value + layout.diameter / 2)}`),
              ...[...layout.tunnel].reverse().map(([station, value]) => `${xAt(station)},${y(value - layout.diameter / 2)}`),
            ].join(' ')} />}
            <polyline className="section-tunnel-axis" points={layout.tunnel.map(([station, value]) =>
              `${xAt(station)},${y(value)}`).join(' ')}>
              <title>Planned tunnel axis{layout.diameter > 0 ? ` · ${layout.diameter} m diameter` : ''}</title>
            </polyline>
          </>}
          {layout.tunnel?.length === 1 && <circle className="section-tunnel-point"
            cx={xAt(layout.tunnel[0][0])}
            cy={y(layout.tunnel[0][1])} r={4}><title>Planned tunnel axis at this borehole</title></circle>}
          {ordered.map((f, i) => {
            const p = f.properties
            const g = p.ground
            const x = xs[i]
            if (x < MARGIN.left - COL || x > layout.svgWidth - MARGIN.right + COL) return null
            const base = byElevation ? layout.tops[i] : 0
            const depth = holeDepth(p)
            const nearest = Math.min(i > 0 ? x - xs[i - 1] : Infinity,
              i < ordered.length - 1 ? xs[i + 1] - x : Infinity)
            const lineWidth = Math.max(1, Math.min(COL, nearest * 0.7))
            const hitWidth = Math.max(lineWidth, Math.min(24, nearest * 0.9))
            return (
              <g key={p.id} data-hole-id={p.id} className={`section-hole${p.id === selectedId ? ' section-hole-on' : ''}${p.id === hoveredId ? ' section-hole-hover' : ''}`}
                role="button" tabIndex={0} aria-label={`Select ${p.name}${p.id === selectedId ? ', press Delete to remove from section' : ''}`}
                onMouseEnter={(event) => { onHover(p.id); showHoleTooltip(event, p, i) }} onMouseMove={(event) => showHoleTooltip(event, p, i)}
                onMouseLeave={() => { onHover(null); hideTooltip() }}
                onFocus={(event) => { onHover(p.id); showTooltip(centre(event.currentTarget), p) }} onBlur={() => { onHover(null); hideTooltip() }}
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
            {visibleLabels.has(i) && <text x={xs[i]} y={MARGIN.top - 18} textAnchor="middle" className="section-name"
              onClick={() => onSelect(f.properties.id)}>{f.properties.name}</text>}
            {f.properties.id === selectedId && xs[i] >= MARGIN.left && xs[i] <= layout.svgWidth - MARGIN.right &&
              <text x={xs[i] + 9} y={MARGIN.top - 5} className="section-x" onClick={() => onRemove(f.properties.id)}>×<title>Remove from section</title></text>}
          </g>)}
          {boxZoom.selection && <rect className="section-zoom-selection" x={Math.min(boxZoom.selection.x0, boxZoom.selection.x1)}
            y={Math.min(boxZoom.selection.y0, boxZoom.selection.y1)} width={Math.abs(boxZoom.selection.x1 - boxZoom.selection.x0)}
            height={Math.abs(boxZoom.selection.y1 - boxZoom.selection.y0)} />}
        </svg>
      </div>
      <div ref={tooltip} className="section-tooltip map-tooltip" hidden><div className="maplibregl-popup-content" /></div>
    </section>
  )
}

/** The profile's place, held for a tunnel that has no route yet. */
export function EmptySection({ title }: { title: string }) {
  return (
    <section className="section" aria-label="Tunnel profile">
      <header className="section-head"><h2>{title}</h2></header>
      <div className="section-plot section-empty">
        <svg width="100%" height="290" aria-hidden="true" preserveAspectRatio="none">
          <line x1="58" x2="100%" y1="257" y2="257" className="section-axis" />
          <line x1="58" x2="58" y1="36" y2="257" className="section-axis" />
        </svg>
        <p>Draw a tunnel route to see its profile here.</p>
      </div>
    </section>
  )
}
