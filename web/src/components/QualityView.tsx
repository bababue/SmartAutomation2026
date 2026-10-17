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

// The model the app extracts with: a decision, not something the figures below compute.
const SELECTED = 'Haiku 5.5'

// Within this share of the best value is a tie: run-to-run noise, not a better model.
const TIE = 0.05

/** One tested model: measured against the reference records, screened on the benchmark pages, or both. */
interface Tested { name: string; route: string | null; measured: ModelRun | null; screened: Benchmarked | null; reference: boolean }

const mid = ([low, high]: [number, number]) => (low + high) / 2

/** Column label, group, value for comparing (null: none), cell; `lower`: the smaller value is the better one. */
const COLUMNS: { label: string; group: 'measured' | 'screened' | 'page'; value: (t: Tested) => number | null; show: (t: Tested) => ReactNode; lower?: boolean }[] = [
  ...(([['boundaries', 'Boundaries'], ['precision', 'Precision'], ['lab', 'Lab values']]) as const).map(([key, label]) => ({
    label, group: 'measured' as const,
    value: (t: Tested) => (t.measured ? ratio(t.measured.metrics[key]) : null),
    show: (t: Tested) => (t.measured ? percent(t.measured.metrics[key]) : '–'),
  })),
  ...(([['boundaries', 'Boundaries'], ['lithology', 'Lithology']]) as const).map(([key, label]) => ({
    label, group: 'screened' as const,
    value: (t: Tested) => (t.screened ? mid(t.screened[key]) : null),
    show: (t: Tested) => (t.reference ? <span className="muted">reference</span> : t.screened ? wholePercent(t.screened[key]) : '–'),
  })),
  {
    label: 'Cost', group: 'page', lower: true,
    value: (t) => t.measured?.usd_per_page ?? t.screened?.usd_per_page ?? null,
    show: (t) => {
      if (t.measured?.usd_per_page != null) return `${t.measured.usd_measured ? '' : '≈ '}${usd(t.measured.usd_per_page)}`
      return t.screened ? usd(t.screened.usd_per_page) : '–'
    },
  },
  {
    label: 'Seconds', group: 'page', lower: true,
    value: (t) => t.measured?.seconds_per_page ?? (t.screened ? mid(t.screened.seconds_per_page) : null),
    show: (t) => t.measured?.seconds_per_page ?? (t.screened ? range(t.screened.seconds_per_page, String) : '–'),
  },
]

const points = (a: Share, b: Share) => Math.round(100 * ((ratio(a) ?? 0) - (ratio(b) ?? 0)))

/** Every tested model once: the reference runs first, then the benchmark models; a model in both keeps both. */
function testedModels(ev: ModelEvaluation): Tested[] {
  const against = modelName(ev.benchmark.against)
  const tested: Tested[] = (ev.models?.runs ?? []).map((r) => {
    const name = modelName(r.model)
    return { name, route: null, measured: r, screened: null, reference: name === against }
  })
  for (const b of ev.benchmark.rows) {
    const known = tested.find((t) => t.name === b.model)
    if (known) Object.assign(known, { screened: b, route: b.route })
    else tested.push({ name: b.model, route: b.route, measured: null, screened: b, reference: false })
  }
  return tested
}

/** The models tab: every tested model in one table, the selected one outlined, the best value per column in bold. */
export function ModelSummary({ quality, error, onDetails }: {
  quality: Quality | null
  error: string | null
  onDetails: () => void
}) {
  if (error) return <p className="error" role="alert">{error}</p>
  if (!quality) return <p className="muted">Loading…</p>
  const ev = quality.evaluation
  const tested = ev ? testedModels(ev) : []
  if (!ev || !tested.length) {
    return <p className="stats-intro muted">No model comparison yet: run the same prompt as experiment arms with different
      models, then <code>python -m pipeline eval snapshot-models</code>.</p>
  }
  const best = COLUMNS.map((c) => {
    const values = tested.map(c.value).filter((v): v is number => v != null)
    return values.length > 1 ? (c.lower ? Math.min(...values) : Math.max(...values)) : null
  })
  const top = (i: number, t: Tested) => {
    const v = COLUMNS[i].value(t)
    const b = best[i]
    return v != null && b != null && Math.abs(v - b) <= TIE * Math.abs(b)
  }
  const selected = tested.find((t) => t.name === SELECTED)
  const accurate = tested.filter((t) => t.measured).reduce<Tested | null>((a, t) =>
    !a || (ratio(t.measured!.metrics.boundaries) ?? -1) > (ratio(a.measured!.metrics.boundaries) ?? -1) ? t : a, null)
  const versus = selected?.measured && accurate && accurate !== selected ? { s: selected.measured, a: accurate.measured! } : null
  const factor = versus?.s.usd_per_page && versus.a.usd_per_page ? Math.round(versus.a.usd_per_page / versus.s.usd_per_page) : null
  const measured = COLUMNS.filter((c) => c.group === 'measured').length
  const screened = COLUMNS.filter((c) => c.group === 'screened').length
  return (
    <div className="stats-grid stats-grid-models">
      <section className="stats-card stats-wide" aria-labelledby="models-head">
        <h3 id="models-head">Models we tested</h3>
        {selected && (
          <p className="stats-lede">
            <b>{selected.name}</b> is selected{versus && <>: {factor && factor > 1 ? `about ${factor}× cheaper` : 'cheaper'} per
              page than {accurate!.name}, {points(versus.a.metrics.boundaries, versus.s.metrics.boundaries)} points lower on
              layer boundaries and {points(versus.a.metrics.lab, versus.s.metrics.lab)} on lab values</>}.
          </p>
        )}
        <div className="stats-scroll">
          <table className="models-table">
            <thead>
              <tr>
                <th rowSpan={2} scope="col">Model</th>
                <th colSpan={measured} scope="colgroup" className="models-group">Against {n(ev.models?.records ?? 0)} checked records</th>
                <th colSpan={screened} scope="colgroup" className="models-group">Agreement with {modelName(ev.benchmark.against)}</th>
                <th colSpan={COLUMNS.length - measured - screened} scope="colgroup" className="models-group">Per page</th>
              </tr>
              <tr>{COLUMNS.map((c) => <th key={`${c.group}-${c.label}`} scope="col" className="num">{c.label}</th>)}</tr>
            </thead>
            <tbody>
              {tested.map((t) => (
                <tr key={t.name} className={t === selected ? 'models-selected' : undefined}>
                  <th scope="row">
                    {t.name}{t === selected && <span className="models-tag">Selected</span>}
                    {t.route && <span className="models-hint">{t.route}</span>}
                  </th>
                  {COLUMNS.map((c, i) => (
                    <td key={`${c.group}-${c.label}`} className={`num${top(i, t) ? ' models-best' : ''}`}>{c.show(t)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="stats-note muted">
          Best value per column in bold. Against checked records: the human-checked records every measured model
          extracted itself. Agreement: before there were reviewed records, the models read the same {n(ev.benchmark.pages)} log
          pages from {n(ev.benchmark.documents)} documents; agreement with {modelName(ev.benchmark.against)}, not accuracy.
          Cost: billed, or ≈ an estimate at API list prices; a run on the Claude subscription is not billed per page.
        </p>
        <button className="link-button stats-more-link" onClick={onDetails}>All figures and the prompt versions in Details</button>
      </section>
    </div>
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
        How well the model reads the logs, measured against records reviewers checked, and what each model costs.
        Over the whole database; the list filters do not apply.{' '}
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
        {ev?.models && <Models ev={ev} models={ev.models} />}
        <div className="stats-stack-col">
          <Audit audit={quality.audit} onReviewAudit={onReviewAudit} />
          {ev && <Benchmark benchmark={ev.benchmark} />}
        </div>
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
        The model output in this database, before reviewer corrections, against <b>{n(ev.reference.records)}</b> records
        that reviewers approved ({n(ev.reference.documents)} documents{ev.reference.created ? `, reference of ${ev.reference.created}` : ''}).
        Those records were approved from earlier Opus output, so the reference favours Opus, and where nobody corrected a
        record it matches by construction. {n(c.corrected)} of them carry reviewer corrections.
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
  return (
    <section className="stats-card" id="release-audit" tabIndex={-1} aria-labelledby="stats-audit">
      <h3 id="stats-audit">Release audit</h3>
      <p className="muted">How often an automatic release is wrong: a random sample of released records, checked by a
        person. A correction or rejection counts as wrong.</p>
      {!audit ? (
        <p className="stats-note">No sample drawn yet: run <code>python -m pipeline eval audit</code>.</p>
      ) : (
        <>
          {audit.reviewed > 0 ? (
            <p className="stats-figure">
              <b>{Math.round(100 * (audit.rate ?? 0))} %</b> wrong
              <span className="muted"> ({n(audit.wrong)} of {n(audit.reviewed)}; 95 % interval
                {' '}{Math.round(100 * audit.low)}–{Math.round(100 * audit.high)} %)</span>
            </p>
          ) : <p className="stats-note">None of the sample checked yet.</p>}
          <Shares label="Audit progress" parts={[
            { key: 'checked', label: 'Checked', value: audit.reviewed, color: 'var(--water)' },
            { key: 'open', label: 'Not checked yet', value: audit.sample - audit.reviewed, color: 'var(--grid)' },
          ]} />
          {audit.reviewed < audit.sample && (
            <button className="btn stats-action" onClick={onReviewAudit}>Check the sample in the review queue</button>
          )}
        </>
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
    <section className="stats-card" aria-labelledby="quality-models">
      <h3 id="quality-models">Model comparison</h3>
      <p className="muted">
        The same prompt with different models, on the <b>{n(models.records)}</b> reference records every run extracted
        itself. Best value in bold.
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
        Estimates are at API list prices; a run on the Claude subscription is not billed per page. Time is one page after
        the other; the pipeline runs several at once. Two plain runs of the same model also differ by a point or two.
      </p>
    </section>
  )
}

function Benchmark({ benchmark }: { benchmark: ModelEvaluation['benchmark'] }) {
  return (
    <section className="stats-card" aria-labelledby="quality-bench">
      <h3 id="quality-bench">Wider benchmark</h3>
      <p className="muted">
        <b>Agreement with {modelName(benchmark.against)}, not accuracy.</b> Before there were reviewed records, these
        models read the same {benchmark.pages} log pages from {benchmark.documents} documents with the same prompt: enough
        to see coarse differences only.
      </p>
      <div className="stats-scroll">
        <table className="stats-table">
          <thead>
            <tr><th>Model</th><th className="num">s / page</th><th className="num">$ / page</th>
              <th className="num" title="Layer boundaries as Opus found them">Bound&shy;aries</th>
              <th className="num" title="Lithology by depth as Opus read it">Lith&shy;ology</th></tr>
          </thead>
          <tbody>
            {benchmark.rows.map((r) => (
              <tr key={r.model}>
                <td>{r.model}<span className="quality-sub muted">{r.route}</span></td>
                <td className="num">{range(r.seconds_per_page, String)}</td>
                <td className="num">{usd(r.usd_per_page)}</td>
                <td className="num">{wholePercent(r.boundaries)}</td>
                <td className="num">{wholePercent(r.lithology)}</td>
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
  return (
    <section className="stats-card stats-wide" aria-labelledby="quality-arms">
      <h3 id="quality-arms">Prompt versions</h3>
      <p className="muted">
        Each experiment arm re-extracts the reference documents with its own prompt. All arms are scored on the
        same <b>{n(arms.records)}</b> reference records ({n(arms.documents)} documents) that every arm extracted itself;
        “covered” is how many reference records an arm extracted. The main-cache arm holds the output the reference was
        approved from, so it is favoured. Best value per column in bold.
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
              <tr key={r.name}>
                <td><span className="quality-arm">{r.name}</span><span className="quality-sub muted">{r.date}</span></td>
                <td>{modelName(r.model)}</td>
                <td className="num">{r.pages ? n(r.pages) : <span className="muted">all</span>}</td>
                <td className="num">{n(r.covered)}</td>
                {METRICS.map(([key]) => {
                  const top = ratio(r.metrics[key]) === best(runs, key)
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
      <p className="muted">Every call pipeline runs and uploads made against this database, per model, backend and step.
        Cached pages make no call.</p>
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
