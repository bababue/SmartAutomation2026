import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { postReview } from '../api'
import { isTyping } from '../dom'
import { SEVERITY_LABEL, STATUS_LABEL, fmtDateTime, loadReviewer, saveReviewer, scoreText } from '../review'
import type { Detail, DetailUpdate, ReviewStatus } from '../types'

interface Props {
  detail: Detail
  onChange: (update: DetailUpdate) => void
  /** Review mode: called after an approval or rejection, to open the next record. */
  onDecided?: () => void
}

const PARTS = [['extraction', 'Layers'], ['position', 'Position']] as const
const EFFECT_MARK = { positive: '✓', negative: '−', neutral: '·' }

function Level({ value }: { value: number | null }) {
  const [filled, label] = value == null ? [0, 'unknown'] : value >= 0.7 ? [3, 'high'] : value >= 0.4 ? [2, 'medium'] : [1, 'low']
  return <span className="level" role="img" aria-label={label} title={label}>{'●'.repeat(filled)}{'○'.repeat(3 - filled)}</span>
}

/** Release status, what a reviewer has to check, why the score is what it is, and the decision controls. */
export function ReviewBox({ detail, onChange, onDecided }: Props) {
  const bh = detail.borehole
  const [reviewer, setReviewer] = useState(loadReviewer)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  // Two key presses can land before a re-render shows `busy`.
  const submitting = useRef(false)
  const [error, setError] = useState<string | null>(null)
  // In review mode the decision controls are what the record is opened for.
  const [open, setOpen] = useState(!!onDecided)
  const status = bh.review_status
  const decided = status === 'approved' || status === 'rejected'
  const severity = detail.severity ?? {}
  // What keeps the record from release, then what would raise its score; the rest of the findings into the details.
  const todo = [
    ...bh.score_factors.filter((f) => f.part === 'review' && f.effect === 'negative').map((f) => f.text),
    ...bh.issues.filter((i) => severity[i] === 'blocking' || severity[i] === 'needs_look'),
    ...new Set(bh.score_factors.flatMap((f) => f.todo ?? [])),
  ]
  const unreadable = bh.issues.filter((i) => i.startsWith('unreadable:'))
  const explained = new Set(bh.score_factors.map((f) => f.text.toLowerCase()))
  const others = bh.issues.filter((i) => (severity[i] === 'warning' || severity[i] === 'info') && !explained.has(i.toLowerCase())
    && !i.startsWith('unreadable:') && !i.startsWith('corrected by'))  // corrections are listed in the panel

  async function decide(decision: ReviewStatus) {
    if (submitting.current || decision === status) return
    if (!reviewer.trim()) {
      setOpen(true)
      setError('Enter your name so the decision can be traced.')
      return
    }
    submitting.current = true
    setBusy(true)
    setError(null)
    let advanced = false
    try {
      saveReviewer(reviewer.trim())
      onChange(await postReview(bh.borehole_id, decision, reviewer.trim(), comment.trim() || null, detail.content_hash, detail.review_revision))
      setComment('')
      if (decision !== 'pending_review' && onDecided) {
        // The decided record stays on screen until the next one has loaded; it takes no second decision.
        advanced = true
        onDecided()
      }
    } catch (e) {
      const msg = (e as Error).message
      setError(msg.startsWith('409')
        ? 'Not saved: this record changed since you opened it (a re-run or another reviewer). Reopen it and check again.'
        : `Decision not saved (${msg}).`)
    } finally {
      if (!advanced) {
        submitting.current = false
        setBusy(false)
      }
    }
  }

  // Keyboard in review mode: A approves, R rejects (not while typing in a field).
  const onShortcut = useEffectEvent((decision: ReviewStatus) => decide(decision))
  const reviewMode = !!onDecided
  useEffect(() => {
    if (!reviewMode) return
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat || isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
      const key = e.key.toLowerCase()
      if (key === 'a') { e.preventDefault(); onShortcut('approved') }
      if (key === 'r') { e.preventDefault(); onShortcut('rejected') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [reviewMode])

  const decisions = (<>
    {decided && (
      <button className="btn" disabled={busy} onClick={() => decide('pending_review')}>Reopen</button>
    )}
    {status !== 'rejected' && (
      <button className="btn btn-decide btn-reject" disabled={busy} onClick={() => decide('rejected')}
        aria-label={onDecided ? 'Reject and next' : 'Reject'} title={onDecided ? 'Reject and next (R)' : 'Reject'}>
        <svg width={16} height={16} viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
      </button>
    )}
    {status !== 'approved' && (
      <button className="btn btn-decide btn-approve" disabled={busy} onClick={() => decide('approved')}
        aria-label={onDecided ? 'Approve and next' : 'Approve'} title={onDecided ? 'Approve and next (A)' : 'Approve'}>
        <svg width={16} height={16} viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 4.8" /></svg>
      </button>
    )}
  </>)

  return (
    <section className={`review review-${status}`} aria-label="Release status">
      {/* The whole head toggles; the button keeps it reachable by keyboard and its click bubbles here. */}
      <div className="review-head" onClick={() => setOpen(!open)}>
        <div className="review-score" title={`Release threshold ${scoreText(detail.threshold)}`}>
          <span className="review-score-n">{scoreText(bh.score)}</span>
          <span className="review-score-of">/100</span>
        </div>
        <span className="review-status" title={decided
          ? `by ${bh.reviewed_by ?? 'unknown'}, ${fmtDateTime(bh.reviewed_at)}`
          : status === 'auto_approved'
            ? `Score at or above ${scoreText(detail.threshold)} and no findings that need a person`
            : `Below ${scoreText(detail.threshold)} or a finding that needs a person`}>
          {STATUS_LABEL[status]}
        </span>
        <div className="review-buttons">
          <button className={`btn btn-decide review-toggle${open ? ' review-toggle-open' : ''}`} aria-expanded={open}
            aria-controls="review-summary" aria-label={open ? 'Hide the review summary' : 'Show the review summary'}
            title={open ? 'Hide the review summary' : 'Show the review summary'}>
            <svg width={16} height={16} viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
          </button>
        </div>
      </div>
      {!open && error && <p className="error" role="alert">{error}</p>}

      {open && <div id="review-summary" className="review-summary">
      <h4>To do</h4>
      {todo.length ? (
        <ul className="review-todo">
          {todo.map((t) => (
            <li key={t}><label>
              <input type="checkbox" />
              <span>{t[0].toUpperCase() + t.slice(1)}</span>
            </label></li>
          ))}
        </ul>
      ) : <p className="muted">Nothing to do.</p>}

      <details className="review-details">
        <summary>Details</summary>
        <h4>Why this score</h4>
        <dl className="review-factors">
          {PARTS.map(([part, label]) => (
            <div key={part}>
              <dt>{label} <Level value={part === 'extraction' ? bh.confidence : bh.location.confidence} /></dt>
              <dd><ul>
                {bh.score_factors.filter((f) => f.part === part).map((f) => (
                  <li key={f.text} className={`factor factor-${f.effect}`}>
                    <span className="factor-mark" aria-label={f.effect}>{EFFECT_MARK[f.effect]}</span>{f.text}
                  </li>
                ))}
              </ul></dd>
            </div>
          ))}
        </dl>
        {others.length > 0 && (<>
          <h4>Other findings</h4>
          <ul className="review-findings">{others.map((i) => (
            <li key={i}><span className={`sev sev-${severity[i]}`}>{SEVERITY_LABEL[severity[i]]}</span> {i[0].toUpperCase() + i.slice(1)}</li>
          ))}</ul>
        </>)}
        {unreadable.length > 0 && (<>
          <h4>What the model could not read</h4>
          <ul>{unreadable.map((i) => <li key={i}>{i.replace(/^unreadable:\s*/, '')}</li>)}</ul>
        </>)}
        {detail.review_log.length > 0 && (<>
          <h4>Decision history</h4>
          <ul>
            {detail.review_log.map((e, i) => (
              <li key={i}>
                {fmtDateTime(e.at)}: {STATUS_LABEL[e.decision]} by {e.reviewer}
                {e.comment ? `, “${e.comment}”` : ''}
              </li>
            ))}
          </ul>
        </>)}
      </details>

      <div className="review-form">
        <label>
          Your name
          <input value={reviewer} onChange={(e) => { setReviewer(e.target.value); saveReviewer(e.target.value.trim()) }} autoComplete="name" />
        </label>
        <label className="review-form-comment">
          Comment (optional)
          <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="e.g. depths checked against page 2" />
        </label>
        {decided && <span className="muted review-meta">by {bh.reviewed_by ?? 'unknown'}, {fmtDateTime(bh.reviewed_at)}</span>}
        {bh.review_comment && <span className="review-comment">“{bh.review_comment}”</span>}
        <div className="review-buttons">{decisions}</div>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      </div>}
    </section>
  )
}
