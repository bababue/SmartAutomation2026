import { fmtCount as n, percentOf as pct } from '../format'

/** One stacked bar of shares, each part labelled with its count beside the bar (colour is never the only cue). */
export function Shares({ parts, label }: { parts: { key: string; label: string; value: number; color: string }[]; label: string }) {
  const total = parts.reduce((s, p) => s + p.value, 0)
  return (
    <figure className="stats-shares">
      <div className="stats-stack" role="img" aria-label={`${label}: ${parts.map((p) => `${p.label} ${p.value}`).join(', ')}`}>
        {parts.filter((p) => p.value > 0).map((p) => (
          <span key={p.key} style={{ flexGrow: p.value, background: p.color }} title={`${p.label}: ${n(p.value)} (${pct(p.value, total)} %)`} />
        ))}
      </div>
      <figcaption className="stats-legend">
        {parts.map((p) => (
          <span key={p.key}><i style={{ background: p.color }} />{p.label} <b>{n(p.value)}</b> <span className="muted">{pct(p.value, total)} %</span></span>
        ))}
      </figcaption>
    </figure>
  )
}

/** A horizontal bar on a shared scale. `pattern`: a lithology pattern, drawn in SVG (CSS cannot paint with it). */
export function Bar({ value, max, fill, pattern, title }: { value: number; max: number; fill?: string; pattern?: string; title: string }) {
  const width = `${max ? (100 * value) / max : 0}%`
  return (
    <span className="stats-bar" title={title}>
      {pattern
        ? <svg width="100%" height="100%" aria-hidden="true"><rect width={width} height="100%" rx={2} fill={`url(#${pattern})`} /></svg>
        : <span style={{ width, ...(fill ? { background: fill } : {}) }} />}
    </span>
  )
}
