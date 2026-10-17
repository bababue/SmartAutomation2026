import { useEffect, useMemo, useRef, useState } from 'react'
import { lithStyle, patternId } from '../lithology'
import type { Feature } from '../types'

interface Props {
  features: Feature[]
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

/** Order boreholes along the line from the first one to the one farthest from it. */
export function orderAlongLine(features: Feature[]): Feature[] {
  if (features.length < 3) return features
  const start = features[0].geometry.coordinates
  const far = features.reduce((a, b) => (distance(start, b.geometry.coordinates) > distance(start, a.geometry.coordinates) ? b : a))
  const [x0, y0] = start, [x1, y1] = far.geometry.coordinates
  const dx = x1 - x0, dy = y1 - y0
  const t = (f: Feature) => (f.geometry.coordinates[0] - x0) * dx + (f.geometry.coordinates[1] - y0) * dy
  return [...features].sort((a, b) => t(a) - t(b))
}

/** Neighbouring holes farther apart than this are not joined: the ground between them is unknown. */
const MAX_JOIN_GAP_M = 150
/** Half-width of the band around a joined rock head, as a share of the distance to the nearest hole. */
const UNCERTAINTY = 0.1
const SAMPLES = 12

const fmtDist = (m: number) => (m >= 1000 ? `${(m / 1000).toLocaleString('en', { maximumFractionDigits: 1 })} km` : `${Math.round(m)} m`)

/** Boreholes side by side along a line, at their ground level: the view a tunnel route is planned from.
 * Rock head and groundwater are joined between close neighbours as a linear estimate with a widening
 * uncertainty band. Layers are not correlated – that is a geological interpretation left to the reader. */
export function SectionView({ features, selectedId, onSelect, onRemove, onClear }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(900)
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, entry.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const ordered = useMemo(() => orderAlongLine(features), [features])
  const byElevation = ordered.every((f) => f.properties.elevation != null)
  const height = 250
  const m = { left: 58, right: 24, top: 36, bottom: 40 }
  const col = 26

  const layout = useMemo(() => {
    const gaps = ordered.slice(1).map((f, i) => distance(ordered[i].geometry.coordinates, f.geometry.coordinates))
    const total = gaps.reduce((a, b) => a + b, 0)
    const inner = width - m.left - m.right - col
    let xs = [0]
    gaps.forEach((g) => xs.push(xs[xs.length - 1] + (total ? (g / total) * inner : inner / Math.max(1, gaps.length))))
    // Keep columns from overlapping; then the horizontal axis is no longer to scale.
    const minGap = col + 34
    let adjusted = false
    for (let i = 1; i < xs.length; i++) {
      if (xs[i] - xs[i - 1] < minGap) { xs[i] = xs[i - 1] + minGap; adjusted = true }
    }
    const span = xs[xs.length - 1]
    if (span > inner && span > 0) xs = xs.map((x) => (x / span) * inner)
    const tops = ordered.map((f) => (byElevation ? f.properties.elevation! : 0))
    const bottoms = ordered.map((f, i) => {
      const depth = f.properties.depth ?? Math.max(0, ...(f.properties.strip ?? []).map((s) => s[1]))
      return byElevation ? tops[i] - depth : depth
    })
    const vMax = byElevation ? Math.max(...tops) : 0
    const vMin = byElevation ? Math.min(...bottoms) : Math.max(...bottoms)
    return { gaps, xs: xs.map((x) => x + m.left), tops, vMax, vMin, adjusted }
  }, [ordered, width, byElevation, m.left, m.right])

  const plotH = height - m.top - m.bottom
  const y = (v: number) =>
    byElevation
      ? m.top + ((layout.vMax - v) / Math.max(0.1, layout.vMax - layout.vMin)) * plotH
      : m.top + (v / Math.max(0.1, layout.vMin)) * plotH
  const range = Math.abs(layout.vMax - layout.vMin)
  const step = range > 200 ? 50 : range > 80 ? 20 : range > 30 ? 10 : range > 12 ? 5 : 2
  /** Plot value of a depth below ground in hole i: a level, or the depth itself. */
  const at = (i: number, depth: number) => (byElevation ? layout.tops[i] - depth : depth)

  /** Joins a ground value between neighbours: [pairIndex, line points, band polygon points or null]. */
  const joins = (pick: (g: NonNullable<Feature['properties']['ground']>) => number | null, band: boolean) =>
    ordered.slice(1).flatMap((f, j) => {
      const va = ordered[j].properties.ground, vb = f.properties.ground
      const da = va ? pick(va) : null, db = vb ? pick(vb) : null
      if (da == null || db == null || layout.gaps[j] > MAX_JOIN_GAP_M) return []
      const xa = layout.xs[j] + col, xb = layout.xs[j + 1]
      const pts = Array.from({ length: SAMPLES + 1 }, (_, k) => {
        const t = k / SAMPLES
        const v = at(j, da) + (at(j + 1, db) - at(j, da)) * t
        return { x: xa + (xb - xa) * t, v, half: UNCERTAINTY * layout.gaps[j] * Math.min(t, 1 - t) }
      })
      const line = pts.map((q) => `${q.x},${y(q.v)}`).join(' ')
      const poly = band
        ? [...pts.map((q) => `${q.x},${y(q.v + q.half)}`), ...[...pts].reverse().map((q) => `${q.x},${y(q.v - q.half)}`)].join(' ')
        : null
      return [[j, line, poly] as const]
    })
  const rockJoins = joins((g) => g.rock_head_m, true)
  const waterJoins = joins((g) => g.groundwater_m, false)

  const ticks: number[] = []
  if (byElevation) for (let v = Math.ceil(layout.vMin / step) * step; v <= layout.vMax; v += step) ticks.push(v)
  else for (let v = 0; v <= layout.vMin; v += step) ticks.push(v)

  return (
    <section className="section" aria-label="Section view">
      <header className="section-head">
        <h2>Section along {ordered.length} borehole{ordered.length === 1 ? '' : 's'}</h2>
        <p className="muted">
          {byElevation ? 'Levels in m above datum as printed on each log.' : 'Depth below ground: not every log states its ground level.'}
          {layout.adjusted && ' Horizontal spacing widened where holes are close together, so it is not to scale.'}
          {' '}Rock head (solid) and groundwater (blue) are joined between holes up to {MAX_JOIN_GAP_M} m apart,
          with a band of ±{UNCERTAINTY * 100} % of the distance to the nearest hole
          {byElevation ? '' : ', assuming level ground'}. A linear estimate for orientation; layers are not correlated.
        </p>
        <button className="btn" onClick={onClear}>Clear section</button>
      </header>
      <div ref={box} className="section-plot">
        <svg width={width} height={height} role="img" aria-label={`Section with ${ordered.length} boreholes`}>
          {ticks.map((v) => (
            <g key={v}>
              <line x1={m.left - 4} x2={width - m.right} y1={y(v)} y2={y(v)} className="section-grid" />
              <text x={m.left - 8} y={y(v) + 4} textAnchor="end" className="section-tick">{v}</text>
            </g>
          ))}
          <text x={14} y={m.top + plotH / 2} className="section-tick" transform={`rotate(-90 14 ${m.top + plotH / 2})`} textAnchor="middle">
            {byElevation ? 'Level (m)' : 'Depth (m)'}
          </text>
          {byElevation && ordered.length > 1 && (
            <polyline
              className="section-ground"
              points={ordered.map((_, i) => `${layout.xs[i] + col / 2},${y(layout.tops[i])}`).join(' ')}
            />
          )}
          {rockJoins.map(([j, line, poly]) => (
            <g key={`rock-${j}`}>
              <polygon points={poly!} className="section-band" />
              <polyline points={line} className="section-rock-join" />
            </g>
          ))}
          {waterJoins.map(([j, line]) => <polyline key={`water-${j}`} points={line} className="section-water-join" />)}
          {ordered.map((f, i) => {
            const p = f.properties
            const g = p.ground
            const x = layout.xs[i]
            const base = byElevation ? layout.tops[i] : 0
            return (
              <g key={p.id} className={`section-hole${p.id === selectedId ? ' section-hole-on' : ''}`}>
                <text x={x + col / 2} y={m.top - 18} textAnchor="middle" className="section-name" onClick={() => onSelect(p.id)}>
                  {p.name}
                </text>
                <text x={x + col / 2} y={m.top - 6} textAnchor="middle" className="section-x" onClick={() => onRemove(p.id)}>
                  remove
                </text>
                {(p.strip ?? []).map(([top, bottom, lith], j) => {
                  const y1 = byElevation ? y(base - top) : y(top)
                  const y2 = byElevation ? y(base - bottom) : y(bottom)
                  return (
                    <rect key={j} x={x} y={y1} width={col} height={Math.max(1, y2 - y1)} fill={`url(#${patternId(lith)})`} className="section-cell" onClick={() => onSelect(p.id)}>
                      <title>{`${p.name}: ${top}–${bottom} m, ${lithStyle(lith).label}`}</title>
                    </rect>
                  )
                })}
                {g?.weathered_rock_top_m != null && (
                  <line x1={x - 4} x2={x + col + 4} y1={y(at(i, g.weathered_rock_top_m))} y2={y(at(i, g.weathered_rock_top_m))} className="section-weathered">
                    <title>{`${p.name}: decomposed rock from ${g.weathered_rock_top_m} m`}</title>
                  </line>
                )}
                {g?.rock_head_m != null && (
                  <line x1={x - 4} x2={x + col + 4} y1={y(at(i, g.rock_head_m))} y2={y(at(i, g.rock_head_m))} className="section-rock">
                    <title>{`${p.name}: rock head at ${g.rock_head_m} m below ground`}</title>
                  </line>
                )}
                {g && g.rock_head_m == null && g.final_depth_m != null && (
                  <text x={x + col / 2} y={y(at(i, g.final_depth_m)) + 11} textAnchor="middle" className="section-tick">
                    no rock<title>{`${p.name}: rock not reached within ${g.final_depth_m} m`}</title>
                  </text>
                )}
                {g?.groundwater_m != null && (
                  <path d={`M${x + col + 2},${y(at(i, g.groundwater_m)) - 6} h10 l-5,6 z`} className="section-gw">
                    <title>{`${p.name}: groundwater at ${g.groundwater_m} m below ground`}</title>
                  </path>
                )}
                {i > 0 && (
                  <text x={(layout.xs[i - 1] + x + col) / 2} y={height - 6} textAnchor="middle" className="section-tick">
                    {fmtDist(layout.gaps[i - 1])}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      </div>
    </section>
  )
}
