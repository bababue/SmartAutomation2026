import type { ReactNode } from 'react'
import type { ModelEvaluation, Quality, QualityMetrics, Share, UsageRow } from '../types'
import { fmtCount as n, fmtError } from '../format'
import { Bar, Shares } from './StatsParts'

type MetricKey = 'boundaries' | 'precision' | 'lithology_depth' | 'final_depth' | 'groundwater' | 'lab'
type ModelRun = NonNullable<ModelEvaluation['models']>['runs'][number]

/** Key, label, explanation, short column label. */
const METRICS: [MetricKey, string, string, string][] = [
  ['boundaries', 'Layer boundaries found', 'reference boundaries matched within 0.1 m', 'Boundaries'],
  ['precision', 'Boundaries in the reference', 'precision: the model’s boundaries that the reference has too', 'Precision'],
  ['lithology_depth', 'Lithology by depth', 'share of the logged metres with the same class at the same depth', 'Lithology'],
  ['final_depth', 'Final depth', 'within 0.1 m', 'Final depth'],
  ['groundwater', 'Groundwater levels', 'levels found within 0.1 m', 'Groundwater'],
  ['lab', 'Lab values on the log', 'same parameter, value, qualifier and sample depth', 'Lab values'],
]

const ratio = (s: Share) => (s.of ? s.hit / s.of : null)
const percent = (s: Share) => {
  const r = ratio(s)
  return r == null ? '–' : `${(100 * r).toFixed(1)} %`
}
const range = ([low, high]: [number, number], f: (v: number) => string) => (low === high ? f(low) : `${f(low)}–${f(high)}`)
const wholePercent = (r: [number, number]) => `${range(r, (v) => String(Math.round(100 * v)))} %`
const usd = (v: number) => (v === 0 ? '$0' : v >= 10 ? `$${n(Math.round(v))}` : v >= 0.1 ? `$${v.toFixed(2)}` : `$${v.toPrecision(2)}`)
const hours = (s: number) => (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`)
const tokens = (v: number | null) => (v == null ? '–' : v >= 1e6 ? `${(v / 1e6).toFixed(1)} M` : `${(v / 1000).toFixed(1)} k`)

/** 'claude-opus-5-5' and 'anthropic/claude-haiku-5.5' read as 'Opus 5.5' and 'Haiku 5.5'; other ids stay as they are. */
function modelName(id: string | null): string {
  if (!id) return 'main cache'
  const m = (id.split('/').pop() ?? id).replace(/^claude-/, '').match(/^([a-z]+)-(\d+)[-.](\d+)$/)
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : id
}

type Benchmarked = ModelEvaluation['benchmark']['rows'][number]

// The model new uploads are extracted with: a decision, not something the figures below compute.
const SELECTED = 'Haiku 5.5'

// Within this share of the best value is a tie: run-to-run noise, not a better model.
const TIE = 0.05

interface Cell { value: number | null; text: ReactNode }
interface Row { name: string; route: string | null; cells: Cell[] }

/** Label; `lower`: the smaller value is the better one. */
const COLUMNS: { label: string; lower?: boolean }[] = [
  { label: 'Boundaries' }, { label: 'Precision' }, { label: 'Lithology' }, { label: 'Final depth' },
  { label: 'Groundwater' }, { label: 'Lab values' }, { label: 'Cost / page', lower: true }, { label: 's / page', lower: true },
]

const mid = ([low, high]: [number, number]) => (low + high) / 2
const shareCell = (s: Share): Cell => ({ value: ratio(s), text: percent(s) })
const rangeCell = (r: [number, number]): Cell => ({ value: mid(r), text: wholePercent(r) })

function measuredRow(r: ModelRun, route: string | null): Row {
  const m = r.metrics
  return {
    name: modelName(r.model), route,
    cells: [shareCell(m.boundaries), shareCell(m.precision), shareCell(m.lithology_depth), shareCell(m.final_depth),
      shareCell(m.groundwater), shareCell(m.lab),
      { value: r.usd_per_page, text: r.usd_per_page == null ? '–' : `${r.usd_measured ? '' : '≈ '}${usd(r.usd_per_page)}` },
      { value: r.seconds_per_page, text: r.seconds_per_page ?? '–' }],
  }
}

function screenedRow(b: Benchmarked): Row {
  return {
    name: b.model, route: b.route,
    cells: [rangeCell(b.boundaries), rangeCell(b.precision), rangeCell(b.lithology), rangeCell(b.final_depth),
      rangeCell(b.groundwater), rangeCell(b.lab), { value: b.usd_per_page, text: usd(b.usd_per_page) },
      { value: mid(b.seconds_per_page), text: range(b.seconds_per_page, String) }],
  }
}

/** Per column, whether each row holds the best value of its section (within TIE of it). */
function bestOf(rows: Row[]): boolean[][] {
  const best = COLUMNS.map((c, i) => {
    const values = rows.map((r) => r.cells[i].value).filter((v): v is number => v != null)
    return values.length > 1 ? (c.lower ? Math.min(...values) : Math.max(...values)) : null
  })
  return rows.map((r) => r.cells.map((cell, i) => {
    const b = best[i]
    return cell.value != null && b != null && Math.abs(cell.value - b) <= TIE * Math.abs(b)
  }))
}

function Section({ title, rows }: { title: ReactNode; rows: Row[] }) {
  const top = bestOf(rows)
  return (
    <tbody>
      <tr className="models-section"><th colSpan={COLUMNS.length + 1} scope="rowgroup">{title}</th></tr>
      {rows.map((r, i) => (
        <tr key={r.name} className={r.name === SELECTED ? 'models-selected' : undefined}>
          <th scope="row">
            {r.name}{r.name === SELECTED && <span className="models-tag">selected</span>}
            {r.route && <span className="models-hint">{r.route}</span>}
          </th>
          {r.cells.map((cell, j) => (
            <td key={COLUMNS[j].label} className={`num${top[i][j] ? ' models-best' : ''}`}>{cell.text}</td>
          ))}
        </tr>
      ))}
    </tbody>
  )
}

/** The models tab: every tested model, measured against the checked records or screened against the reference
 * model, the selected one outlined, the best value per column and section in bold. */
export function ModelSummary({ quality, error, onDetails }: {
  quality: Quality | null
  error: string | null
  onDetails: () => void
}) {
  if (error) return <p className="error" role="alert">{error}</p>
  if (!quality) return <p className="muted">Loading…</p>
  const ev = quality.evaluation
  if (!ev) {
    return <p className="stats-intro muted">No model comparison yet: run the same prompt as experiment arms with different
      models, then <code>python -m pipeline eval snapshot-models</code>.</p>
  }
  const bench = ev.benchmark
  const runs = ev.models?.runs ?? []
  const routeOf = (name: string) => bench.rows.find((b) => b.model === name)?.route ?? null
  const measured = runs.map((r) => measuredRow(r, routeOf(modelName(r.model))))
  const screened = bench.rows.map(screenedRow)
  const against = modelName(bench.against)
  const selected = runs.find((r) => modelName(r.model) === SELECTED)
  const accurate = runs.reduce<ModelRun | null>((a, r) =>
    !a || (ratio(r.metrics.boundaries) ?? -1) > (ratio(a.metrics.boundaries) ?? -1) ? r : a, null)
  const versus = selected && accurate && accurate !== selected
  const factor = versus && selected.usd_per_page && accurate.usd_per_page ? Math.round(accurate.usd_per_page / selected.usd_per_page) : null
  const c = bench.compared
  return (
    <section className="stats-section" aria-labelledby="models-head">
      <h3 id="models-head">Models we tested</h3>
      <p className="stats-sub">
        <b>{SELECTED}</b> is selected for new uploads{versus && factor && factor > 1 && <>: about {factor}× cheaper than{' '}
          {modelName(accurate.model)}, which read the corpus</>}.
      </p>
      <div className="stats-scroll">
        <table className="models-table">
          <thead>
            <tr><th scope="col">Model</th>{COLUMNS.map((col) => <th key={col.label} scope="col" className="num">{col.label}</th>)}</tr>
          </thead>
          {measured.length > 0 && (
            <Section rows={measured} title={<>Measured on {n(ev.models!.records)} checked records <span>accuracy</span></>} />
          )}
          {screened.length > 0 && (
            <Section rows={screened} title={<>Screened on {n(bench.pages)} pages <span>agreement with {against}</span></>} />
          )}
        </table>
      </div>
      <p className="stats-note">
        Best per section in bold. Screening base: {n(c.boundaries)} boundaries, {n(c.final_depth)} final depths,{' '}
        {n(c.groundwater)} groundwater levels, {n(c.lab)} lab values. ≈ estimate at API list prices.{' '}
        <button className="link-button" onClick={onDetails}>All figures</button>
      </p>
    </section>
  )
}

/** The details tab: extraction quality, the model comparison in full, the release audit, benchmark, prompt versions and calls. */
export function QualityDetails({ quality, error, onReviewAudit }: {
  quality: Quality | null
  error: string | null
  onReviewAudit: () => void
}) {
  if (error) return <p className="error" role="alert">{error}</p>
  if (!quality) return <p className="muted">Loading…</p>
  const ev = quality.evaluation
  return (
    <>
      <p className="stats-intro muted">
        Measured against records reviewers checked, over the whole database.{' '}
        {quality.source === 'live' && <span className="stats-source">Live from the local evaluation data</span>}
        {quality.source === 'snapshot' && ev && (
          <span className="stats-source">Snapshot of {ev.generated}: this installation has no evaluation data</span>
        )}
      </p>
      <div className="stats-grid stats-grid-quality">
        {ev ? <Current ev={ev} /> : (
          <section className="stats-card stats-wide" id="extraction-quality" tabIndex={-1}>
            <h3>Extraction quality</h3>
            <p className="stats-note">No evaluation data and no snapshot: approve records in the review queue, then
              run <code>python -m pipeline eval snapshot</code>.</p>
          </section>
        )}
        <Audit audit={quality.audit} onReviewAudit={onReviewAudit} />
        {ev?.models && <Models ev={ev} models={ev.models} />}
        {ev && <Benchmark benchmark={ev.benchmark} />}
        {ev && ev.arms.runs.length > 0 && <Arms arms={ev.arms} />}
        <Usage rows={quality.usage} />
      </div>
    </>
  )
}

function Current({ ev }: { ev: ModelEvaluation }) {
  const c = ev.current
  return (
    <section className="stats-card stats-wide" id="extraction-quality" tabIndex={-1} aria-labelledby="quality-current">
      <h3 id="quality-current">Extraction quality</h3>
      <p className="muted">
        Model output before corrections, against <b>{n(ev.reference.records)}</b> approved records
        ({n(ev.reference.documents)} documents). They were approved from this Opus output, so uncorrected records match by
        construction; {n(c.corrected)} carry corrections.
      </p>
      <dl className="quality-tiles">
        {METRICS.map(([key, label, hint]) => (
          <div key={key} title={hint}>
            <dt>{label}</dt>
            <dd>{percent(c[key])}</dd>
            <Bar value={c[key].hit} max={c[key].of} title={`${label}: ${n(c[key].hit)} of ${n(c[key].of)}`} />
            <span className="quality-of muted">{n(Math.round(c[key].hit))} of {n(Math.round(c[key].of))}{key === 'lithology_depth' ? ' m' : ''}</span>
          </div>
        ))}
      </dl>
      <p className="stats-note">
        Position: median error <b>{fmtError(c.position_median_m)}</b>, largest <b>{fmtError(c.position_max_m)}</b>;
        {' '}{n(c.without_position)} without a position, coordinate system differs on {n(c.crs_changed)}.
        Reference records not found: <b>{n(c.missing)}</b>.
      </p>
    </section>
  )
}

function Audit({ audit, onReviewAudit }: { audit: Quality['audit']; onReviewAudit: () => void }) {
  const pctOf = (v: number) => Math.round(100 * v)
  return (
    <section className="stats-card stats-wide" id="release-audit" tabIndex={-1} aria-labelledby="stats-audit">
      <h3 id="stats-audit">Release audit</h3>
      {!audit ? (
        <p className="stats-note">No sample drawn yet: run <code>python -m pipeline eval audit</code>.</p>
      ) : (
        <div className="audit">
          <p className="audit-figure">
            {audit.reviewed ? <>{n(audit.wrong)}<small> / {n(audit.reviewed)}</small></> : '–'}
            <span>automatic releases wrong</span>
          </p>
          <div>
            <p>
              A random sample of automatic releases, checked by a person; a correction or rejection counts as wrong.
              {audit.reviewed > 0 && <> With 95 % confidence the true error rate lies between <b>{pctOf(audit.low)} %</b> and{' '}
                <b>{pctOf(audit.high)} %</b>.</>}
            </p>
            <Shares label="Audit progress" parts={[
              { key: 'checked', label: 'Checked', value: audit.reviewed, color: 'var(--bar)' },
              { key: 'open', label: 'Not checked yet', value: audit.sample - audit.reviewed, color: 'var(--grid)' },
            ]} />
            {audit.reviewed < audit.sample && (
              <button className="btn stats-action" onClick={onReviewAudit}>Check the sample in the review queue</button>
            )}
          </div>
        </div>
      )}
    </section>
  )
}

/** The highest share per metric across runs, to mark the best value of each row or column. */
function best(runs: { metrics: QualityMetrics }[], key: MetricKey): number {
  return Math.max(...runs.map((r) => ratio(r.metrics[key]) ?? -1))
}

function Models({ ev, models }: { ev: ModelEvaluation; models: NonNullable<ModelEvaluation['models']> }) {
  const runs = models.runs
  const cell = (key: MetricKey, run: string, m: QualityMetrics) => {
    const top = runs.length > 1 && ratio(m[key]) === best(runs, key)
    return <td key={run} className={`num${top ? ' quality-best' : ''}`}>{percent(m[key])}</td>
  }
  return (
    <section className="stats-card stats-wide quality-models" aria-labelledby="quality-models">
      <h3 id="quality-models">Model comparison</h3>
      <p className="muted">
        Same prompt, <b>{n(models.records)}</b> reference records every run extracted itself. Best in bold.
      </p>
      <div className="stats-scroll">
        <table className="stats-table quality-table">
          <thead>
            <tr><th />{runs.map((r) => <th key={r.arm} className="num">{modelName(r.model)}</th>)}</tr>
          </thead>
          <tbody>
            {METRICS.map(([key, label, hint]) => (
              <tr key={key}><th scope="row" title={hint}>{label}</th>{runs.map((r) => cell(key, r.arm, r.metrics))}</tr>
            ))}
            <tr><th scope="row">Records not found</th>{runs.map((r) => <td key={r.arm} className="num">{n(r.metrics.missing)}</td>)}</tr>
            <tr><th scope="row">Largest position error</th>{runs.map((r) => <td key={r.arm} className="num">{fmtError(r.metrics.position_max_m)}</td>)}</tr>
            <tr className="quality-group"><th scope="row">Seconds per page (median)</th>{runs.map((r) => <td key={r.arm} className="num">{r.seconds_per_page ?? '–'}</td>)}</tr>
            <tr>
              <th scope="row">Tokens per page in / out</th>
              {runs.map((r) => (
                <td key={r.arm} className="num">{r.input_tokens == null ? <span className="muted">not reported</span> : `${tokens(r.input_tokens)} / ${tokens(r.output_tokens)}`}</td>
              ))}
            </tr>
            <tr>
              <th scope="row">Cost per page</th>
              {runs.map((r) => (
                <td key={r.arm} className="num">
                  {r.usd_per_page == null ? '–' : <>{r.usd_measured ? '' : '≈ '}{usd(r.usd_per_page)} <span className="muted">{r.usd_measured ? 'billed' : 'estimate'}</span></>}
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">All {n(ev.log_pages)} log pages</th>
              {runs.map((r) => (
                <td key={r.arm} className="num">
                  {r.usd_per_page == null ? '–' : `${r.usd_measured ? '' : '≈ '}${usd(r.usd_per_page * ev.log_pages)}`}
                  {r.seconds_per_page != null && <span className="muted">, {hours(r.seconds_per_page * ev.log_pages)}</span>}
                </td>
              ))}
            </tr>
            <tr><th scope="row">Pages extracted</th>{runs.map((r) => <td key={r.arm} className="num">{n(r.pages)}</td>)}</tr>
          </tbody>
        </table>
      </div>
      <p className="stats-note muted">
        Estimates at API list prices; the subscription is not billed per page. Time is one page at a time. Two runs of one
        model differ by a point or two.
      </p>
    </section>
  )
}

/** Agreement columns of the wider benchmark: key, header, explanation. */
const AGREEMENT: ['boundaries' | 'precision' | 'lithology' | 'final_depth' | 'groundwater' | 'lab', string, string][] = [
  ['boundaries', 'Bound\u00adaries', 'layer boundaries as the reference model found them'],
  ['precision', 'Precision', 'boundaries the reference model has too'],
  ['lithology', 'Lith\u00adology', 'lithology by depth as the reference model read it'],
  ['final_depth', 'Final depth', 'final depth as the reference model read it'],
  ['groundwater', 'Ground\u00adwater', 'groundwater levels as the reference model found them'],
  ['lab', 'Lab values', 'lab values as the reference model found them'],
]

function Benchmark({ benchmark }: { benchmark: ModelEvaluation['benchmark'] }) {
  const c = benchmark.compared
  return (
    <section className="stats-card stats-wide" aria-labelledby="quality-bench">
      <h3 id="quality-bench">Wider benchmark</h3>
      <p className="muted">
        <b>Agreement with {modelName(benchmark.against)}, not accuracy.</b> Before there were reviewed records, these
        models read the same {benchmark.pages} log pages from {benchmark.documents} documents with the same prompt.
        The reference model had {n(c.boundaries)} boundaries, {n(c.final_depth)} final depths, {n(c.groundwater)} groundwater
        levels and {n(c.lab)} lab values there, so one value moves the last three columns a lot.
      </p>
      <div className="stats-scroll">
        <table className="stats-table">
          <thead>
            <tr><th>Model</th><th className="num">s / page</th><th className="num">$ / page</th>
              {AGREEMENT.map(([key, label, hint]) => <th key={key} className="num" title={hint}>{label}</th>)}</tr>
          </thead>
          <tbody>
            {benchmark.rows.map((r) => (
              <tr key={r.model}>
                <td>{r.model}<span className="quality-sub muted">{r.route}</span></td>
                <td className="num">{range(r.seconds_per_page, String)}</td>
                <td className="num">{usd(r.usd_per_page)}</td>
                {AGREEMENT.map(([key]) => <td key={key} className="num">{wholePercent(r[key])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Arms({ arms }: { arms: ModelEvaluation['arms'] }) {
  const runs = arms.runs
  // The arm without a prompt of its own is the main cache, the output the reference was approved from.
  const compared = runs.filter((r) => r.prompt)
  return (
    <section className="stats-card stats-wide" aria-labelledby="quality-arms">
      <h3 id="quality-arms">Prompt versions</h3>
      <p className="muted">
        Each arm re-extracts the reference documents with its own prompt, scored on the <b>{n(arms.records)}</b> records
        ({n(arms.documents)} documents) all arms extracted. Best per column in bold; the main cache is what reviewers
        approved, so it is not compared.
      </p>
      <div className="stats-scroll">
        <table className="stats-table quality-table">
          <thead>
            <tr>
              <th>Arm</th><th>Model</th><th className="num">Pages</th><th className="num">Covered</th>
              {METRICS.map(([key, label, hint, short]) => <th key={key} className="num" title={`${label}: ${hint}`}>{short}</th>)}
              <th className="num">Not found</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.name} className={r.prompt ? undefined : 'quality-source'}>
                <td>
                  <span className="quality-arm">{r.name}</span>
                  <span className="quality-sub muted">{r.prompt ? r.date : 'reference source, not compared'}</span>
                </td>
                <td>{modelName(r.model)}</td>
                <td className="num">{r.pages ? n(r.pages) : <span className="muted">all</span>}</td>
                <td className="num">{n(r.covered)}</td>
                {METRICS.map(([key]) => {
                  const top = Boolean(r.prompt) && ratio(r.metrics[key]) === best(compared, key)
                  return <td key={key} className={`num${top ? ' quality-best' : ''}`}>{percent(r.metrics[key])}</td>
                })}
                <td className="num">{n(r.metrics.missing)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Usage({ rows }: { rows: UsageRow[] }) {
  const total = rows.reduce((s, r) => s + r.calls, 0)
  return (
    <section className="stats-card stats-wide" aria-labelledby="quality-usage">
      <h3 id="quality-usage">Model calls</h3>
      <p className="muted">Every call against this database, per model, backend and step. Cached pages make no call.</p>
      {!rows.length ? <p className="stats-note">No model calls recorded in this database yet.</p> : (
        <div className="stats-scroll">
          <table className="stats-table">
            <thead>
              <tr><th>Model</th><th>Backend</th><th>Step</th><th className="num">Calls</th><th className="num">Failed</th>
                <th className="num">Pages</th><th className="num">Tokens in</th><th className="num">Tokens out</th>
                <th className="num">Cost</th><th className="num">Time</th><th className="num">s / call</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.model}|${r.endpoint}|${r.stage}`}>
                  <td>{modelName(r.model)}</td>
                  <td>{r.endpoint ?? '–'}</td>
                  <td>{r.stage ?? '–'}</td>
                  <td className="num">{n(r.calls)}</td>
                  <td className="num">{r.errors ? n(r.errors) : <span className="muted">0</span>}</td>
                  <td className="num">{n(r.pages)}</td>
                  <td className="num">{r.input_tokens ? tokens(r.input_tokens) : <span className="muted">–</span>}</td>
                  <td className="num">{r.output_tokens ? tokens(r.output_tokens) : <span className="muted">–</span>}</td>
                  <td className="num" title={`${n(r.priced)} of ${n(r.calls)} calls with a known cost`}>
                    {r.cost_usd == null ? <span className="muted">not billed</span> : usd(r.cost_usd)}
                  </td>
                  <td className="num">{hours(r.seconds)}</td>
                  <td className="num">{r.calls ? (r.seconds / r.calls).toFixed(1) : '–'}</td>
                </tr>
              ))}
            </tbody>
            {rows.length > 1 && (
              <tfoot><tr><th colSpan={3}>All</th><td className="num">{n(total)}</td><td colSpan={7} /></tr></tfoot>
            )}
          </table>
        </div>
      )}
    </section>
  )
}
