import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { fetchQuality, fetchStatsDetail } from '../api'
import { fmtCount as n, fmtError, percentOf as pct, words } from '../format'
import { lithStyle, patternId } from '../lithology'
import { SEVERITY_LABEL } from '../review'
import { DEFAULT_FILTERS, DEFAULT_REVIEW_FILTERS, hasFilters, type StatsTab } from '../url'
import type { CrsDetermination, Filters, Quality, ReviewFilters, StatsDetail } from '../types'
import { MethodView } from './MethodView'
import { ModelSummary, QualityDetails } from './QualityView'
import { Bar, Shares } from './StatsParts'

interface Props {
  filters: Filters
  tab: StatsTab
  onTab: (tab: StatsTab) => void
  /** Narrow the map to a document group (the list's group filter). */
  onShowGroup: (group: string) => void
  /** Open the review queue on the release audit sample. */
  onReviewAudit: () => void
  /** Open the review queue with these filters. */
  onReview: (filters: ReviewFilters) => void
}

/** The cards a headline figure leads to. */
type Target = 'coordinates' | 'extraction-quality' | 'release-audit'

const TABS: [StatsTab, string][] = [['overview', 'Overview'], ['method', 'How it works'], ['models', 'Models'], ['details', 'Details']]
const LITH_ROWS = 10
const CRS_ROWS = 6

/** How the coordinate system was settled: label and explanation. */
const DETERMINATION: [CrsDetermination, string, string][] = [
  ['stated', 'On the sheet', 'the log names the system, or labels latitude and longitude'],
  ['document', 'Elsewhere in the document', 'on another page or in the coordinate table'],
  ['reviewer', 'Set by a reviewer', 'a reviewer’s coordinate system correction'],
  ['knowledge', 'Learned from reviewers', 'confirmed on other holes of the document, or a local grid fitted to reviewer positions'],
  ['inferred', 'Inferred', 'not stated: the system that lands nearest the named place; marked for a look'],
  ['ambiguous', 'Ambiguous', 'several systems fit: held for a person to decide'],
]

/** Where the evaluation figures come from, when not this installation's own data. */
function Snapshot({ quality }: { quality: Quality | null }) {
  if (quality?.source !== 'snapshot' || !quality.evaluation) return null
  return <> (evaluation snapshot of <span className="num">{quality.evaluation.generated}</span>)</>
}

function reveal(target: Target): boolean {
  const el = document.getElementById(target)
  if (!el) return false
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  el.focus({ preventScroll: true })
  return true
}

export function StatsView({ filters, tab, onTab, onShowGroup, onReviewAudit, onReview }: Props) {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [whole, setWhole] = useState<StatsDetail | null>(null)
  const [narrowed, setNarrowed] = useState<StatsDetail | null>(null)
  const [dataError, setDataError] = useState<string | null>(null)
  const [quality, setQuality] = useState<Quality | null>(null)
  const [qualityError, setQualityError] = useState<string | null>(null)
  const pending = useRef<Target | null>(null)
  const filtered = hasFilters(filters)

  useEffect(() => {
    const ctrl = new AbortController()
    const failed = (set: (e: string) => void, what: string) => (e: Error) => {
      if (!ctrl.signal.aborted) set(`Could not load the ${what} (${e.message}).`)
    }
    fetchStatsDetail(DEFAULT_FILTERS, ctrl.signal).then(setWhole).catch(failed(setDataError, 'statistics'))
    fetchQuality(ctrl.signal).then(setQuality).catch(failed(setQualityError, 'quality figures'))
    return () => ctrl.abort()
  }, [])

  useEffect(() => {
    if (!filtered) return
    const ctrl = new AbortController()
    fetchStatsDetail(filters, ctrl.signal)
      .then((s) => { setNarrowed(s); setDataError(null) })
      .catch((e) => { if (!ctrl.signal.aborted) setDataError(`Could not load the statistics (${e.message}).`) })
    return () => ctrl.abort()
  }, [filters, filtered])

  // The target card renders once its tab is open and its data has arrived.
  useEffect(() => {
    if (pending.current && reveal(pending.current)) pending.current = null
  }, [tab, whole, narrowed, quality])

  const open = (to: StatsTab, target: Target) => {
    if (to === tab && reveal(target)) return
    pending.current = target
    onTab(to)
  }

  const onKey = (e: KeyboardEvent) => {
    const at = TABS.findIndex(([key]) => key === tab)
    const next = e.key === 'ArrowRight' ? (at + 1) % TABS.length : e.key === 'ArrowLeft' ? (at - 1 + TABS.length) % TABS.length
      : e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : null
    if (next == null) return
    e.preventDefault()
    onTab(TABS[next][0])
    tabRefs.current[next]?.focus()
  }

  return (
    <div className="stats-view">
      <header className="stats-head">
        <h2>Statistics</h2>
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
      <div className="stats-panel" role="tabpanel" id={`stats-panel-${tab}`} aria-labelledby={`stats-tab-${tab}`}>
        {tab === 'overview' && (
          <>
            <Claims whole={whole} quality={quality} onOpen={open} />
            <Overview stats={filtered ? narrowed : whole} filtered={filtered} error={dataError} quality={quality}
              onShowGroup={onShowGroup} onReview={onReview} />
          </>
        )}
        {tab === 'method' && <MethodView stats={whole} quality={quality} />}
        {tab === 'models' && <ModelSummary quality={quality} error={qualityError} onDetails={() => onTab('details')} />}
        {tab === 'details' && <QualityDetails quality={quality} error={qualityError} onReviewAudit={onReviewAudit} />}
      </div>
    </div>
  )
}

/** The headline figures over the whole database, each leading to its evidence. */
function Claims({ whole, quality, onOpen }: {
  whole: StatsDetail | null
  quality: Quality | null
  onOpen: (tab: StatsTab, target: Target) => void
}) {
  const loading = '…'
  const located = whole ? whole.position.printed + whole.position.linked_table : 0
  const ev = quality?.evaluation
  const found = ev?.current.boundaries
  const audit = quality?.audit
  return (
    <ul className="claims" aria-label="Headline figures, whole database">
      <Claim figure={whole ? `${pct(located, whole.boreholes)} %` : loading} label="positioned from printed coordinates"
        note={whole && `${n(located)} of ${n(whole.boreholes)} holes`}
        onClick={() => onOpen('overview', 'coordinates')} />
      <Claim figure={!quality ? loading : found?.of ? `${((100 * found.hit) / found.of).toFixed(1)} %` : 'n/a'}
        label="of layer boundaries found"
        note={quality && (ev && found?.of ? <>in {n(ev.reference.records)} human-checked records<Snapshot quality={quality} /></>
          : 'no checked records yet')}
        onClick={() => onOpen('details', 'extraction-quality')} />
      <Claim figure={!quality ? loading : audit?.reviewed ? <>{n(audit.wrong)}<small> / {n(audit.reviewed)}</small></> : 'n/a'}
        label="audited automatic releases wrong"
        note={quality && (!audit ? 'no audit yet' : !audit.reviewed ? `${n(audit.sample)} drawn, none checked`
          : `random sample, 95\u00a0% interval ${Math.round(100 * audit.low)}–\u2060${Math.round(100 * audit.high)}\u00a0%`)}
        onClick={() => onOpen('details', 'release-audit')} />
    </ul>
  )
}

function Claim({ figure, label, note, onClick }: { figure: ReactNode; label: string; note: ReactNode; onClick: () => void }) {
  return (
    <li>
      <button className="claim" onClick={onClick}>
        <span className="claim-figure">{figure}</span>
        <span className="claim-label">{label}</span>
        {note && <span className="claim-note">{note}</span>}
      </button>
    </li>
  )
}

function Overview({ stats: s, filtered, error, quality, onShowGroup, onReview }: {
  stats: StatsDetail | null
  filtered: boolean
  error: string | null
  quality: Quality | null
  onShowGroup: Props['onShowGroup']
  onReview: Props['onReview']
}) {
  const lith = s?.lithology_m ?? []
  const lithTotal = lith.reduce((sum, [, m]) => sum + m, 0)
  const lithRows = lith.length > LITH_ROWS + 1
    ? [...lith.slice(0, LITH_ROWS), ['other', lith.slice(LITH_ROWS).reduce((sum, [, m]) => sum + m, 0)] as [string, number]]
    : lith
  const lithMax = Math.max(0, ...lithRows.map(([, m]) => m))  // the summed rest can outweigh the top class
  const depthMax = Math.max(1, ...(s?.depths ?? []))
  // Notes (unreadable items, corrections) hold nothing back; they would only dwarf the findings that do.
  const findings = s?.findings.filter((f) => f.severity !== 'info') ?? []
  const findingMax = Math.max(1, ...findings.map((f) => f.records))

  return (
    <>
      {error && <p className="error" role="alert">{error}</p>}
      {!s ? (!error && <p className="muted">Loading…</p>) : (
        <>
          {filtered && <p className="stats-scope">Below: only the records the list filters select.</p>}
          <Coordinates s={s} quality={quality} onReview={onReview} />
          <Funnel s={s} />

          <details className="stats-more">
            <summary>More figures <span className="muted">depth, lithology, findings, documents</span></summary>
            <div className="stats-grid stats-grid-more">
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
                <p className="muted">Share of {n(Math.round(lithTotal))} logged metres per DIN 4023 class.</p>
                <ol className="stats-rows">
                  {lithRows.map(([key, m]) => {
                    const label = key === 'other' ? 'Other classes' : lithStyle(key).label
                    return (
                      <li key={key}>
                        <span className="stats-label">{label}</span>
                        <Bar value={m} max={lithMax} title={`${label}: ${n(Math.round(m))} m`}
                          {...(key === 'other' ? { fill: 'var(--graphite)' } : { pattern: patternId(key) })} />
                        <span className="stats-value">{pct(m, lithTotal)} %</span>
                      </li>
                    )
                  })}
                </ol>
              </section>

              <section className="stats-card stats-wide" aria-labelledby="stats-findings">
                <h3 id="stats-findings">Findings</h3>
                <p className="muted">Records per finding, blocking first.</p>
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
                <p className="muted">Choose one to see it on the map.</p>
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
          </details>
        </>
      )}
    </>
  )
}

/** From extracted records to those the release rule lets through, and how many are released in all. */
function Funnel({ s }: { s: StatsDetail }) {
  const f = s.funnel
  const t = Math.round(s.threshold * 100)
  const steps: [string, number, string][] = [
    ['extracted', f.records, ''],
    ['nothing blocking', f.without_blocking, ''],
    ['passed the rule', f.passed_rule, `score ≥ ${t} too`],
    ['released', f.released, `${n(s.status.approved)} by a person`],
  ]
  return (
    <section className="stats-section" aria-labelledby="stats-funnel">
      <h3 id="stats-funnel">Release funnel</h3>
      <p className="stats-sub">Automatic release only at score ≥ {t} with nothing blocking.</p>
      <ol className="funnel">
        {steps.map(([label, value, sub]) => (
          <li key={label}>
            <span className="funnel-figure">{n(value)}</span>
            <span className="funnel-label">{label}{sub && <span className="muted"> · {sub}</span>}</span>
            <Bar value={value} max={f.records} title={`${label}: ${n(value)} of ${n(f.records)} (${pct(value, f.records)} %)`} />
          </li>
        ))}
      </ol>
    </section>
  )
}

function Coordinates({ s, quality, onReview }: { s: StatsDetail; quality: Quality | null; onReview: Props['onReview'] }) {
  const p = s.position
  const fromCoordinates = p.printed + p.linked_table
  const howMax = Math.max(1, ...Object.values(s.crs_determination))
  const ev = quality?.evaluation
  return (
    <section className="stats-section" id="coordinates" tabIndex={-1} aria-labelledby="stats-coordinates">
      <h3 id="stats-coordinates">Coordinates</h3>
      <p className="stats-sub">Only evidence places a hole; the rest goes to a person.</p>
      <Shares label="Position" parts={[
        { key: 'printed', label: 'Printed coordinates', value: p.printed, color: 'var(--bar)' },
        { key: 'table', label: 'Coordinate table', value: p.linked_table, color: 'var(--water)' },
        { key: 'manual', label: 'Set by a reviewer', value: p.manual, color: 'var(--graphite)' },
        { key: 'place', label: 'Place name only', value: p.place_name, color: 'var(--water-mid)' },
        { key: 'none', label: 'None', value: p.none, color: 'var(--grid)' },
      ]} />
      <div className="coord-cols">
        <div>
          <h4>Coordinate system <span className="muted">· {n(fromCoordinates)} holes</span></h4>
          <ol className="stats-rows">
            {DETERMINATION.map(([key, label, hint]) => {
              const value = s.crs_determination[key]
              return (
                <li key={key}>
                  <span className="stats-label" title={hint}>{label}</span>
                  <Bar value={value} max={howMax} title={`${label}: ${n(value)} holes (${hint})`}
                    {...(key === 'ambiguous' ? { fill: 'var(--medium)' } : {})} />
                  <span className="stats-value">{pct(value, fromCoordinates)} %</span>
                </li>
              )
            })}
          </ol>
        </div>
        <div>
          <h4>EPSG codes <span className="muted">· {n(s.crs.length)}{s.crs_without_epsg > 0 && `, plus ${n(s.crs_without_epsg)} holes in local grids`}</span></h4>
          {s.crs.length > 0 && <CrsRows rows={s.crs.slice(0, CRS_ROWS)} max={s.crs[0][1]} />}
          {s.crs.length > CRS_ROWS && (
            <details className="coord-more">
              <summary>All {n(s.crs.length)}</summary>
              <CrsRows rows={s.crs.slice(CRS_ROWS)} max={s.crs[0][1]} />
            </details>
          )}
        </div>
      </div>
      <p className="stats-note">
        {ev && <>Position error on checked records<Snapshot quality={quality} />: median <b>{fmtError(ev.current.position_median_m)}</b>, largest{' '}
          <b>{fmtError(ev.current.position_max_m)}</b>. </>}
        <b>{n(s.positions_withheld)}</b> {s.positions_withheld === 1 ? 'hole' : 'holes'} held back: they would share one place-name point.{' '}
        {s.positions_withheld > 0 && (
          <button className="link-button" onClick={() => onReview({ ...DEFAULT_REVIEW_FILTERS, position: 'none' })}>
            Place them
          </button>
        )}
      </p>
    </section>
  )
}

function CrsRows({ rows, max }: { rows: [number, number][]; max: number }) {
  return (
    <ol className="stats-rows">
      {rows.map(([epsg, count]) => (
        <li key={epsg}>
          <span className="stats-label">EPSG:{epsg}</span>
          <Bar value={count} max={max} title={`EPSG:${epsg}: ${n(count)} holes`} />
          <span className="stats-value">{n(count)}</span>
        </li>
      ))}
    </ol>
  )
}
