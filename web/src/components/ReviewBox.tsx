import { useEffect, useEffectEvent, useState, type ReactNode } from 'react'
import { postReview } from '../api'
import { isTyping } from '../dom'
import { STATUS_LABEL, fmtDateTime, loadReviewer, saveReviewer, scoreText } from '../review'
import type { Detail, DetailUpdate, ReviewStatus } from '../types'

interface Props {
  detail: Detail
  onChange: (update: DetailUpdate) => void
  /** Review mode: called after an approval or rejection, to open the next record. */
  onDecided?: () => void
  /** Shown next to the summary heading. */
  children?: ReactNode
  /** Findings shown in the summary below the score reasons. */
  findings?: ReactNode
}

/** Release status, why it needs a human, and the decision controls. */
export function ReviewBox({ detail, onChange, onDecided, children, findings }: Props) {
  const bh = detail.borehole
  const [reviewer, setReviewer] = useState(loadReviewer)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const status = bh.review_status
  const decided = status === 'approved' || status === 'rejected'

  async function decide(decision: ReviewStatus) {
    if (busy || decision === status) return
    if (!reviewer.trim()) {
      setOpen(true)
      setError('Enter your name so the decision can be traced.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      saveReviewer(reviewer.trim())
      onChange(await postReview(bh.borehole_id, decision, reviewer.trim(), comment.trim() || null, detail.content_hash, detail.review_revision))
      setComment('')
      if (decision !== 'pending_review') onDecided?.()
    } catch (e) {
      const msg = (e as Error).message
      setError(msg.startsWith('409')
        ? 'Not saved: this record changed since you opened it (a re-run or another reviewer). Reopen it and check again.'
        : `Decision not saved (${msg}).`)
    } finally {
      setBusy(false)
    }
  }

  // Keyboard: A approves, R rejects (not while typing in a field).
  const onShortcut = useEffectEvent((decision: ReviewStatus) => decide(decision))
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return
      const key = e.key.toLowerCase()
      if (key === 'a') { e.preventDefault(); onShortcut('approved') }
      if (key === 'r') { e.preventDefault(); onShortcut('rejected') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <section className={`review review-${status}`} aria-label="Release status">
      <div className="review-head">
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
          <button className="btn review-toggle" aria-expanded={open} aria-controls="review-summary" onClick={() => setOpen(!open)}>
            Review summary <span aria-hidden="true">{open ? '▴' : '▾'}</span>
          </button>
          {decided && (
            <button className="btn" disabled={busy} onClick={() => decide('pending_review')}>Reopen</button>
          )}
          {status !== 'rejected' && (
            <button className="btn btn-decide btn-reject" disabled={busy} onClick={() => decide('rejected')}
              aria-label={onDecided ? 'Reject and next' : 'Reject'} title={`${onDecided ? 'Reject and next' : 'Reject'} (R)`}>
              <svg width={16} height={16} viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
            </button>
          )}
          {status !== 'approved' && (
            <button className="btn btn-decide btn-approve" disabled={busy} onClick={() => decide('approved')}
              aria-label={onDecided ? 'Approve and next' : 'Approve'} title={`${onDecided ? 'Approve and next' : 'Approve'} (A)`}>
              <svg width={16} height={16} viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 4.8" /></svg>
            </button>
          )}
        </div>
      </div>
      {error && <p className="error" role="alert">{error}</p>}

      {open && <div id="review-summary" className="review-summary">
      <div className="checks checks-score">
        <h3>Summary <span className="checks-levels">{children}</span></h3>
        <ul>
          {bh.score_reasons.map((r) => <li key={r}>{r}</li>)}
        </ul>
        {findings}
      </div>
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
      </div>

      {detail.review_log.length > 0 && (
        <details className="review-log">
          <summary>Decision history ({detail.review_log.length})</summary>
          <ul>
            {detail.review_log.map((e, i) => (
              <li key={i}>
                {fmtDateTime(e.at)}: {STATUS_LABEL[e.decision]} by {e.reviewer}
                {e.comment ? `, “${e.comment}”` : ''}
              </li>
            ))}
          </ul>
        </details>
      )}
      </div>}
    </section>
  )
}
