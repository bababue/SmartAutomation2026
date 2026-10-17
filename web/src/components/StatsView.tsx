import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { fetchStatsDetail } from '../api'
import { fmtCount as n, percentOf as pct, words } from '../format'
import { lithStyle, patternId } from '../lithology'
import { SEVERITY_LABEL } from '../review'
import { hasFilters, type StatsTab } from '../url'
import type { Filters, StatsDetail } from '../types'
import { QualityView } from './QualityView'
import { Bar, Shares } from './StatsParts'

interface Props {
  filters: Filters
  tab: StatsTab
  onTab: (tab: StatsTab) => void
  /** Narrow the map to a document group (the list's group filter). */
  onShowGroup: (group: string) => void
  /** Open the review queue on the release audit sample. */
  onReviewAudit: () => void
  onClose: () => void
}

const TABS: [StatsTab, string][] = [['data', 'Data'], ['quality', 'Quality & models']]
const LITH_ROWS = 10

export function StatsView({ filters, tab, onTab, onShowGroup, onReviewAudit, onClose }: Props) {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])

  const onKey = (e: KeyboardEvent) => {
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    if (!step) return
    e.preventDefault()
    const next = (TABS.findIndex(([key]) => key === tab) + step + TABS.length) % TABS.length
    onTab(TABS[next][0])
    tabRefs.current[next]?.focus()
  }

  return (
    <div className="stats-view">
      <header className="stats-head">
        <div className="stats-title">
          <h2>Statistics</h2>
          <button className="btn" onClick={onClose}>Back to map</button>
        </div>
        <div className="stats-tabs" role="tablist" aria-label="Statistics" onKeyDown={onKey}>
          {TABS.map(([key, label], i) => (
            <button key={key} ref={(el) => { tabRefs.current[i] = el }} role="tab" id={`stats-tab-${key}`}
              aria-selected={tab === key} aria-controls={`stats-panel-${key}`} tabIndex={tab === key ? 0 : -1}
              className={`stats-tab${tab === key ? ' stats-tab-on' : ''}`} onClick={() => onTab(key)}>
              {label}
            </button>
          ))}
        </div>
      </header>
      <div role="tabpanel" id={`stats-panel-${tab}`} aria-labelledby={`stats-tab-${tab}`}>
        {tab === 'data'
          ? <DataTab filters={filters} onShowGroup={onShowGroup} />
          : <QualityView onReviewAudit={onReviewAudit} />}
      </div>
    </div>
  )
}

function DataTab({ filters, onShowGroup }: Pick<Props, 'filters' | 'onShowGroup'>) {
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
    <>
      <p className="stats-intro muted">
        {hasFilters(filters) ? 'For the records the current filters select' : 'All records in the database'}, holes without a
        position included. Change the filters in the list to narrow it.
      </p>
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

          <section className="stats-card stats-wide" aria-labelledby="stats-findings">
            <h3 id="stats-findings">Findings</h3>
            <p className="muted">Records per finding, blocking ones first; notes are left out.</p>
            <table className="stats-table">
              <thead><tr><th>Finding</th><th>Severity</th><th className="num" colSpan={2}>Records</th></tr></thead>
              <tbody>
                {findings.map((f) => (
                  <tr key={`${f.code}-${f.severity}`}>
                    <td>{words(f.code)}</td>
                    <td><span className={`sev sev-${f.severity}`}>{SEVERITY_LABEL[f.severity]}</span></td>
                    <td className="stats-bar-cell">
                      <Bar value={f.records} max={findingMax} title={`${words(f.code)}: ${n(f.records)} records, ${n(f.count)} findings`} />
                    </td>
                    <td className="num">{n(f.records)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
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
        </div>
      )}
    </>
  )
}
