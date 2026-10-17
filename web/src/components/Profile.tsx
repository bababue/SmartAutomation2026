import type { Borehole } from '../types'
import { fmtNum } from '../format'
import { lithStyle, patternId, stratColor } from '../lithology'

interface Props {
  borehole: Borehole
  hovered: number | null
  onHover: (index: number | null) => void
}

/** Two-column borehole profile: stratigraphy (code + colour) left, DIN 4023 lithology right. */
export function Profile({ borehole, hovered, onHover }: Props) {
  const layers = borehole.layers
  const depth = Math.max(borehole.depth_m ?? 0, ...layers.map((l) => l.bottom_m), 1)
  const pxPerM = Math.min(48, Math.max(9, 620 / depth))
  const top = 34
  const height = depth * pxPerM
  const x0 = 54 // depth axis
  const strat = { x: 64, w: 54 }
  const lith = { x: 126, w: 74 }
  const width = 236
  const y = (m: number) => top + m * pxPerM
  const tickStep = depth > 60 ? 10 : depth > 25 ? 5 : depth > 10 ? 2 : 1
  const ticks = Array.from({ length: Math.floor(depth / tickStep) + 1 }, (_, i) => i * tickStep)
  const elev = borehole.surface_elevation_m

  return (
    <svg
      className="profile"
      width={width}
      height={top + height + 24}
      role="img"
      aria-label={`Profile of ${borehole.borehole_name}, ${fmtNum(depth)} m deep, ${layers.length} layers`}
    >
      <text x={strat.x + strat.w / 2} y={18} className="profile-head" textAnchor="middle">Stratigraphy</text>
      <text x={lith.x + lith.w / 2} y={18} className="profile-head" textAnchor="middle">Lithology</text>

      {/* depth axis */}
      <line x1={x0} x2={x0} y1={top} y2={top + height} className="axis" />
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x0 - 4} x2={x0} y1={y(t)} y2={y(t)} className="axis" />
          <text x={x0 - 7} y={y(t) + 3.5} textAnchor="end" className="tick">
            {elev != null ? fmtNum(elev - t) : fmtNum(t)}
          </text>
        </g>
      ))}
      <text x={10} y={top + height / 2} className="tick" transform={`rotate(-90 10 ${top + height / 2})`} textAnchor="middle">
        {elev != null ? `Level (m ${borehole.elevation_datum ?? 'above datum'})` : 'Depth (m)'}
      </text>

      {layers.map((l, i) => {
        const y1 = y(l.top_m)
        const h = Math.max(1, (l.bottom_m - l.top_m) * pxPerM)
        const active = hovered === i
        const style = lithStyle(l.primary)
        return (
          <g key={i} onMouseEnter={() => onHover(i)} onMouseLeave={() => onHover(null)} className="profile-layer">
            <title>{`${fmtNum(l.top_m)}–${fmtNum(l.bottom_m)} m · ${style.label}\n${l.description_en ?? l.description_original}`}</title>
            <rect x={strat.x} y={y1} width={strat.w} height={h} fill={stratColor(l)} className="cell" />
            {l.stratigraphic_code && h >= 11 && (
              <text x={strat.x + strat.w / 2} y={y1 + h / 2 + 4} textAnchor="middle" className="strat-code">
                {l.stratigraphic_code}
              </text>
            )}
            <rect x={lith.x} y={y1} width={lith.w} height={h} fill={`url(#${patternId(l.primary)})`} className="cell" />
            {active && <rect x={strat.x - 3} y={y1} width={lith.x + lith.w - strat.x + 6} height={h} className="cell-active" />}
            <line x1={lith.x + lith.w} x2={lith.x + lith.w + 6} y1={y1 + h} y2={y1 + h} className="axis" />
            {h >= 9 || i === layers.length - 1 ? (
              <text x={lith.x + lith.w + 9} y={y1 + h + 3.5} className="tick">{fmtNum(l.bottom_m)}</text>
            ) : null}
          </g>
        )
      })}

      {borehole.groundwater
        .filter((g) => g.depth_m != null && !g.dry)
        .map((g, i) => (
          <g key={`gw${i}`} className="groundwater">
            <title>{`Groundwater ${fmtNum(g.depth_m!)} m (${g.kind})`}</title>
            <path d={`M${strat.x - 13} ${y(g.depth_m!) - 7} h10 l-5 7 z`} />
            <line x1={strat.x - 13} x2={strat.x - 3} y1={y(g.depth_m!) + 2} y2={y(g.depth_m!) + 2} />
          </g>
        ))}
    </svg>
  )
}

/** Horizontal miniature of the profile, like a core box tray. Surface on the left. */
export function CoreStrip({ strip, depth, width = 168 }: { strip: [number, number, string][] | null; depth: number | null; width?: number }) {
  if (!strip?.length) return <div className="core-strip core-strip-empty" style={{ width }} />
  const total = Math.max(depth ?? 0, ...strip.map((s) => s[1]), 0.1)
  const x = (m: number) => (m / total) * width
  return (
    <svg className="core-strip" width={width} height={14} aria-hidden="true">
      {strip.map(([t, b, lith], i) => (
        <rect key={i} x={x(t)} y={0} width={Math.max(1, x(b) - x(t))} height={14} fill={`url(#${patternId(lith)})`} />
      ))}
    </svg>
  )
}
