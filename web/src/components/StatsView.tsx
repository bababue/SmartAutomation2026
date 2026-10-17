import { useEffect, useState } from 'react'
import { fetchStatsDetail } from '../api'
import { lithStyle, patternId } from '../lithology'
import type { Filters, StatsDetail } from '../types'

interface Props {
  filters: Filters
  /** Narrow the map to a document group (the list's group filter). */
  onShowGroup: (group: string) => void
  onClose: () => void
}

const SEVERITY: Record<StatsDetail['findings'][number]['severity'], string> = {
  blocking: 'Blocking', needs_look: 'Needs a look', warning: 'Warning', info: 'Note',
}
const LITH_ROWS = 10
const n = (v: number) => v.toLocaleString('en')
const pct = (part: number, whole: number) => (whole ? Math.round((100 * part) / whole) : 0)
const words = (code: string) => code.replace(/_/g, ' ')

function filtered(f: Filters) {
  return Boolean(f.q.trim() || f.lithology.length || f.lab.length || f.minPosition != null || f.status !== 'all' || f.group)
}

/** One stacked bar of shares, each part labelled with its count beside the bar (colour is never the only cue). */
function Shares({ parts, label }: { parts: { key: string; label: string; value: number; color: string }[]; label: string }) {
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
function Bar({ value, max, fill, pattern, title }: { value: number; max: number; fill?: string; pattern?: string; title: string }) {
  const width = `${max ? (100 * value) / max : 0}%`
  return (
    <span className="stats-bar" title={title}>
      {pattern
        ? <svg width="100%" height="100%" aria-hidden="true"><rect width={width} height="100%" rx={2} fill={`url(#${pattern})`} /></svg>
        : <span style={{ width, ...(fill ? { background: fill } : {}) }} />}
    </span>
  )
}

export function StatsView({ filters, onShowGroup, onClose }: Props) {
  const [stats, setStats] = useState<StatsDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    fetchStatsDetail(filters, ctrl.signal)
      .then((s) => { setStats(s); setError(null) })
      .catch((e) => { if (!ctrl.signal.aborted) setError(`Could not load the statistics (${e.message}).`) })
    return () => ctrl.abort()
  }, [filters])

  const s = stats
  const lith = s?.lithology_m ?? []
  const lithTotal = lith.reduce((sum, [, m]) => sum + m, 0)
  const lithRows = lith.length > LITH_ROWS + 1
    ? [...lith.slice(0, LITH_ROWS), ['other', lith.slice(LITH_ROWS).reduce((sum, [, m]) => sum + m, 0)] as [string, number]]
    : lith
  const depthMax = Math.max(1, ...(s?.depths ?? []))
  // Notes (unreadable items, corrections) hold nothing back; they would only dwarf the findings that do.
  const findings = s?.findings.filter((f) => f.severity !== 'info') ?? []
  const findingMax = Math.max(1, ...findings.map((f) => f.records))

  return (
    <div className="stats-view">
      <header className="stats-head">
        <div className="queue-title">
          <h2>Statistics</h2>
          <button className="btn" onClick={onClose}>Back to map</button>
        </div>
        <p className="muted">
          {filtered(filters) ? 'For the records the current filters select' : 'All records in the database'}, holes without a
          position included. Change the filters in the list to narrow it.
        </p>
      </header>
      {error && <p className="error" role="alert">{error}</p>}
      {!s ? (!error && <p className="muted">Loading…</p>) : (
        <div className="stats-grid">
          <section className="stats-card stats-wide" aria-labelledby="stats-figures">
            <h3 id="stats-figures">Key figures</h3>
            <dl className="stats-tiles">
              {([['Documents', s.documents], ['Pages', s.pages], ['Boreholes', s.boreholes], ['Layers', s.layers],
                ['Countries', s.countries]] as [string, number][]).map(([label, value]) => (
                <div key={label}><dt>{label}</dt><dd>{n(value)}</dd></div>
              ))}
            </dl>
            <h4>Release state</h4>
            <Shares label="Release state" parts={[
              { key: 'released', label: 'Released', value: s.status.auto_approved + s.status.approved, color: 'var(--high)' },
              { key: 'pending', label: 'Waiting for review', value: s.status.pending_review, color: 'var(--medium)' },
              { key: 'rejected', label: 'Rejected', value: s.status.rejected, color: 'var(--low)' },
            ]} />
            <h4>Position</h4>
            <Shares label="Position" parts={[
              { key: 'exact', label: 'From printed coordinates', value: s.position.exact, color: 'var(--water)' },
              { key: 'approx', label: 'Approximate (place name)', value: s.position.approximate, color: 'var(--water-mid)' },
              { key: 'none', label: 'None', value: s.position.none, color: 'var(--grid)' },
            ]} />
          </section>

          <section className="stats-card" aria-labelledby="stats-funnel">
            <h3 id="stats-funnel">Release funnel</h3>
            <p className="muted">A record is released automatically only with a score of at least {Math.round(s.threshold * 100)} and
              no blocking finding; everything else goes to a person.</p>
            <ol className="stats-rows">
              {([['Records extracted', s.funnel.records], ['Without a blocking finding', s.funnel.without_blocking],
                ['Released automatically', s.funnel.released_automatically]] as [string, number][]).map(([label, value]) => (
                <li key={label}>
                  <span className="stats-label">{label}</span>
                  <Bar value={value} max={s.funnel.records} title={`${label}: ${n(value)}`} />
                  <span className="stats-value">{n(value)} <span className="muted">{pct(value, s.funnel.records)} %</span></span>
                </li>
              ))}
            </ol>
            <p className="stats-note">Released after a person checked them: <b>{n(s.funnel.released - s.funnel.released_automatically)}</b>,
              released in all: <b>{n(s.funnel.released)}</b></p>
          </section>

          <section className="stats-card" aria-labelledby="stats-findings">
            <h3 id="stats-findings">Findings</h3>
            <p className="muted">Records per finding, blocking ones first; notes are left out.</p>
            <table className="stats-table">
              <thead><tr><th>Finding</th><th>Severity</th><th className="num" colSpan={2}>Records</th></tr></thead>
              <tbody>
                {findings.map((f) => (
                  <tr key={`${f.code}-${f.severity}`}>
                    <td>{words(f.code)}</td>
                    <td><span className={`sev sev-${f.severity}`}>{SEVERITY[f.severity]}</span></td>
                    <td className="stats-bar-cell">
                      <Bar value={f.records} max={findingMax} title={`${words(f.code)}: ${n(f.records)} records, ${n(f.count)} findings`} />
                    </td>
                    <td className="num">{n(f.records)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="stats-card" aria-labelledby="stats-lith">
            <h3 id="stats-lith">Lithology</h3>
            <p className="muted">Share of the logged metres ({n(Math.round(lithTotal))} m) per DIN 4023 class.</p>
            <ol className="stats-rows">
              {lithRows.map(([key, m]) => {
                const label = key === 'other' ? 'Other classes' : lithStyle(key).label
                return (
                  <li key={key}>
                    <span className="stats-label">{label}</span>
                    <Bar value={m} max={lithRows[0][1]} title={`${label}: ${n(Math.round(m))} m`}
                      {...(key === 'other' ? { fill: 'var(--graphite)' } : { pattern: patternId(key) })} />
                    <span className="stats-value">{pct(m, lithTotal)} %</span>
                  </li>
                )
              })}
            </ol>
          </section>

          <section className="stats-card" aria-labelledby="stats-depth">
            <h3 id="stats-depth">Final depth</h3>
            <p className="muted">Holes per {s.depth_bin_m} m of final depth.</p>
            <div className="stats-columns" role="img"
              aria-label={s.depths.map((v, i) => `${i * s.depth_bin_m} m: ${v}`).join(', ')}>
              {s.depths.map((v, i) => {
                const range = i === s.depths.length - 1 ? `${i * s.depth_bin_m}+` : `${i * s.depth_bin_m}–${(i + 1) * s.depth_bin_m}`
                return (
                  <div key={i} className="stats-column" title={`${range} m: ${n(v)} holes`}>
                    <span className="stats-column-value">{v || ''}</span>
                    <span className="stats-column-bar" style={{ height: `${(100 * v) / depthMax}%` }} />
                    <span className="stats-column-label">{range}</span>
                  </div>
                )
              })}
            </div>
            <p className="stats-axis muted">metres</p>
          </section>

          <section className="stats-card stats-wide" aria-labelledby="stats-groups">
            <h3 id="stats-groups">Documents</h3>
            <p className="muted">Per document group. Choose one to see its holes on the map.</p>
            <table className="stats-table">
              <thead><tr><th>Document</th><th className="num">Holes</th><th colSpan={2}>Released</th><th>Most frequent finding</th></tr></thead>
              <tbody>
                {s.groups.map((g) => (
                  <tr key={g.group}>
                    <td><button className="link-button" onClick={() => onShowGroup(g.group)}>{g.group}</button></td>
                    <td className="num">{n(g.count)}</td>
                    <td className="stats-bar-cell">
                      <Bar value={g.released} max={g.count} fill="var(--high)" title={`${n(g.released)} of ${n(g.count)} released`} />
                    </td>
                    <td className="num">{pct(g.released, g.count)} %</td>
                    <td>{g.top_finding ? `${words(g.top_finding.code)} (${n(g.top_finding.records)})` : <span className="muted">none</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {s.crs.length > 0 && (
            <section className="stats-card" aria-labelledby="stats-crs">
              <h3 id="stats-crs">Coordinate systems</h3>
              <p className="muted">Holes per coordinate system their position was read in.</p>
              <ol className="stats-rows">
                {s.crs.map(([epsg, count]) => (
                  <li key={epsg}>
                    <span className="stats-label">EPSG:{epsg}</span>
                    <Bar value={count} max={s.crs[0][1]} title={`EPSG:${epsg}: ${n(count)} holes`} />
                    <span className="stats-value">{n(count)}</span>
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>
      )}
    </div>
  )
}
