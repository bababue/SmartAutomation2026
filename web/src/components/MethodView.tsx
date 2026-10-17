import type { ReactNode } from 'react'
import { fmtCount as n } from '../format'
import type { Quality, StatsDetail } from '../types'

/** Name, who does the work, what it does. */
const STEPS: [string, 'llm' | 'code', string][] = [
  ['Render pages', 'code', 'Each document becomes page images, locally'],
  ['Classify', 'llm', 'Log, lab sheet, coordinate table or other page'],
  ['Extract', 'llm', 'Layers, groundwater, tests and coordinates, as printed'],
  ['Assemble', 'code', 'One record per hole from all its sheets; layer sequence checked'],
  ['Georeference', 'code', 'Printed coordinates read in every plausible system, checked against the named place; the LLM re-reads the digits'],
  ['Score & decide', 'code', 'Release score from evidence, findings'],
]

const Arrow = () => (
  <svg className="flow-arrow" viewBox="0 0 22 12" aria-hidden="true"><path d="M1 6H19M14 1.5L19.5 6L14 10.5" /></svg>
)

const Person = () => (
  <svg className="flow-person" viewBox="0 0 16 16" aria-hidden="true">
    <circle cx="8" cy="4.5" r="3" /><path d="M2 15c0-3.6 2.7-5.5 6-5.5s6 1.9 6 5.5" />
  </svg>
)

/** How a page becomes a record, how the release score is computed, and who decides. */
export function MethodView({ stats, quality }: { stats: StatsDetail | null; quality: Quality | null }) {
  const t = stats ? Math.round(stats.threshold * 100) : '…'
  const calls = quality?.usage.reduce((sum, r) => sum + r.calls, 0)
  const since = quality?.usage.map((r) => r.first).sort()[0]?.slice(0, 10)
  const audit = quality?.audit
  return (
    <div className="stats-grid stats-grid-method">
      <section className="stats-card stats-wide" aria-labelledby="method-flow">
        <h3 id="method-flow">From page to record</h3>
        <ol className="flow">
          {STEPS.map(([name, kind, text], i) => (
            <li key={name} className="flow-item">
              <div className={`flow-step flow-${kind}`}>
                <span className="flow-kind">{kind === 'llm' ? 'LLM' : 'Code'}</span>
                <b className="flow-name">{name}</b>
                <span className="flow-text">{text}</span>
              </div>
              {i < STEPS.length - 1 ? <Arrow /> : (
                <svg className="flow-fork" viewBox="0 0 24 100" preserveAspectRatio="none" aria-hidden="true">
                  <path d="M0 50H10V25H24M10 50V75H24" vectorEffect="non-scaling-stroke" />
                </svg>
              )}
            </li>
          ))}
          <li className="flow-outcomes">
            <div className="flow-step flow-released">
              <b className="flow-name">Released</b>
              <span className="flow-text">Score ≥ {t} and no blocking finding</span>
            </div>
            <div className="flow-step flow-review">
              <b className="flow-name">Review queue <span aria-hidden="true">→</span> <span className="flow-who"><Person />a person</span></b>
              <span className="flow-text">Everything else: approve, correct or reject</span>
            </div>
          </li>
        </ol>
        <p className="flow-legend">
          <span><i className="flow-swatch flow-llm" />LLM: reads the page</span>
          <span><i className="flow-swatch flow-code" />Code: positions, checks, score and decision</span>
        </p>
      </section>

      <section className="stats-card stats-wide" aria-labelledby="method-score">
        <h3 id="method-score">The release score</h3>
        <p className="method-formula"><code>score = min(extraction, position)</code></p>
        <p className="stats-lede">Computed from evidence, never the model’s own estimate. A record is only as trustworthy
          as its weaker part.</p>
        <div className="method-parts">
          <div>
            <h4>Extraction</h4>
            <dl className="method-rules">
              <Rule figure="100">Start</Rule>
              <Rule figure="−10" detail="each, at most −40">Plausibility finding in the layer sequence: gap, overlap,
                inferred boundary, layers that start below the surface or end away from the stated final depth</Rule>
              <Rule figure="−5" detail="each, at most −20">Item the model reported as unreadable in the log or the position</Rule>
              <Rule figure="−0.6" detail="per point below 80 %">Layer depths that do not appear in the PDF text layer</Rule>
              <Rule figure="max 85">Nothing confirms the depths independently: neither the text layer nor, on a scan, a
                complete log from 0 m to a stated final depth. Also when a finding needs a look.</Rule>
            </dl>
          </div>
          <div>
            <h4>Position</h4>
            <dl className="method-rules">
              <Rule figure="100">Set by a reviewer</Rule>
              <Rule figure="95">Printed coordinates in a stated coordinate system, at the named place</Rule>
              <Rule figure="90">Coordinate system stated elsewhere in the document, or learned from reviewers</Rule>
              <Rule figure="80">Coordinate system inferred, not stated</Rule>
              <Rule figure="70–15">Printed coordinates farther from the named place</Rule>
              <Rule figure="≤ 50">Several readings fit, or no place to check them against</Rule>
              <Rule figure="30">Position from the place name only</Rule>
            </dl>
            <p className="method-note">Where a second reading ran, the model reads the printed coordinates again, enlarged.
              If that reading differs, the record is blocked.</p>
          </div>
        </div>
        <p className="method-decision">
          Released automatically only at a score ≥ <b>{t}</b> and without a blocking finding.
          A blocking finding always goes to a person, whatever the score.
        </p>
      </section>

      <Principle title="Score system" lead="The weaker part decides.">
        Both parts come from checks code can compute. Every deduction is shown on the record as a reason.
      </Principle>
      <Principle title="Blocking findings" lead="What could hide wrong data goes to a person."
        figure={stats && `${n(stats.waiting_blocking)} records held for this now`}>
        Gaps and overlaps, an ambiguous coordinate system, depths in feet, missing sheets, a second reading that
        differs, text that addresses the model.
      </Principle>
      <Principle title="Human in the loop" lead="Reviewers decide, and the system learns."
        figure={audit?.reviewed ? `${n(audit.wrong)} of ${n(audit.reviewed)} audited releases wrong` : null}>
        A decision holds while the record stays unchanged. Confirmed coordinate systems and local grids carry over to
        other holes. A random sample of automatic releases is checked by a person.
      </Principle>
      <Principle title="Governance" lead="Every model call is logged, never its content."
        figure={calls != null && since && `${n(calls)} model calls logged since ${since}`}>
        Stage, model, tokens, cost and outcome per call. Claude runs through Anthropic; OpenRouter routes only to
        zero-data-retention providers. Pages are rendered locally and places looked up in an offline gazetteer.
      </Principle>
    </div>
  )
}

function Rule({ figure, detail, children }: { figure: string; detail?: string; children: ReactNode }) {
  return (
    <div>
      <dt>{figure}{detail && <span>{detail}</span>}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function Principle({ title, lead, figure, children }: { title: string; lead: string; figure?: string | false | null; children: ReactNode }) {
  return (
    <section className="stats-card principle">
      <h3>{title}</h3>
      <p className="principle-lead">{lead}</p>
      <p className="principle-text">{children}</p>
      {figure && <p className="principle-figure">{figure}</p>}
    </section>
  )
}
