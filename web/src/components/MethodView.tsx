import type { ReactNode } from 'react'
import { fmtCount as n } from '../format'
import type { Quality, StatsDetail } from '../types'

/** Name, who does the work, what it does. */
const STEPS: [string, 'llm' | 'code', string][] = [
  ['Render', 'code', 'Page images, made locally'],
  ['Classify', 'llm', 'Which kind of page'],
  ['Extract', 'llm', 'Layers, water, tests, coordinates, as printed'],
  ['Assemble', 'code', 'One record per hole'],
  ['Georeference', 'code', 'Coordinates checked against the named place; the LLM re-reads the digits'],
  ['Score', 'code', 'From evidence'],
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
    <>
      <section className="stats-section" aria-labelledby="method-flow">
        <h3 id="method-flow">From page to record</h3>
        <ol className="flow">
          {STEPS.map(([name, kind, text], i) => (
            <li key={name} className="flow-item">
              <div className={`flow-step flow-${kind}`}>
                <span className="flow-kind">{kind === 'llm' ? 'LLM' : 'code'}</span>
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
              <span className="flow-text">Score ≥ {t}, nothing blocking</span>
            </div>
            <div className="flow-step flow-review">
              <b className="flow-name"><span className="flow-who"><Person />A person</span></b>
              <span className="flow-text">Everything else</span>
            </div>
          </li>
        </ol>
        <p className="flow-legend">The LLM reads the pages; code computes positions, checks and the decision.</p>
      </section>

      <section className="stats-section" aria-labelledby="method-score">
        <h3 id="method-score">Release score</h3>
        <p className="method-formula"><code>score = min(extraction, position)</code></p>
        <p className="stats-sub">From evidence, never the model’s own estimate.</p>
        <div className="method-parts">
          <div>
            <h4>Extraction</h4>
            <dl className="method-rules">
              <Rule figure="100">Start</Rule>
              <Rule figure="−10" detail="each, max −40">Gap, overlap or other plausibility finding</Rule>
              <Rule figure="−5" detail="each, max −20">Unreadable item in the log or the position</Rule>
              <Rule figure="−0.6" detail="per point below 80\u00a0%">Depths missing from the PDF text layer</Rule>
              <Rule figure="max 85">Depths not confirmed independently, or a finding needs a look</Rule>
            </dl>
          </div>
          <div>
            <h4>Position</h4>
            <dl className="method-rules">
              <Rule figure="100">Set by a reviewer</Rule>
              <Rule figure="95">Stated coordinate system, at the named place</Rule>
              <Rule figure="90">System from elsewhere in the document, or learned</Rule>
              <Rule figure="80">System inferred</Rule>
              <Rule figure="70–15">Farther from the named place</Rule>
              <Rule figure="≤ 50">Ambiguous, or nothing to check against</Rule>
              <Rule figure="30">Place name only</Rule>
            </dl>
          </div>
        </div>
        <p className="method-decision">Automatic release at score ≥ {t} with nothing blocking. Anything blocking goes to a
          person, whatever the score.</p>
      </section>

      <section className="stats-section principles" aria-label="Principles">
        <Principle title="Weakest part decides">Every deduction is shown on the record.</Principle>
        <Principle title="Blocking findings" figure={stats && `${n(stats.waiting_blocking)} records held now`}>
          Anything that could hide wrong data goes to a person, including a second coordinate reading that differs.
        </Principle>
        <Principle title="Human in the loop"
          figure={audit?.reviewed ? `${n(audit.wrong)} of ${n(audit.reviewed)} audited releases wrong` : null}>
          Decisions hold while the record is unchanged. Confirmed coordinate systems carry over.
        </Principle>
        <Principle title="Governance" figure={calls != null && since && `${n(calls)} calls logged since ${since}`}>
          Every model call logged with tokens and cost, never content. OpenRouter: zero data retention only.
        </Principle>
      </section>
    </>
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

function Principle({ title, figure, children }: { title: string; figure?: string | false | null; children: ReactNode }) {
  return (
    <div className="principle">
      <h3>{title}</h3>
      <p>{children}</p>
      {figure && <p className="principle-figure">{figure}</p>}
    </div>
  )
}
