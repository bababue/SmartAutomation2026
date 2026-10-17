import { useEffect, useRef, useState, type ReactNode } from 'react'
import { postReview } from '../api'
import { STATUS_LABEL, fmtDateTime, loadReviewer, saveReviewer, scoreText } from '../review'
import type { Detail, DetailUpdate, ReviewStatus } from '../types'

export interface QueueNav {
  position: number // 1-based position in the review queue
  total: number
  prev: (() => void) | null
  next: (() => void) | null
  /** Called after a decision; moves on to the next waiting record. */
  advance: () => void
}

interface Props {
  detail: Detail
  onChange: (update: DetailUpdate) => void
  nav?: QueueNav | null
  /** Extra chips shown before the score reasons. */
  children?: ReactNode
}

const typing = (el: EventTarget | null) =>
  el instanceof HTMLElement && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)

/** Release status, why it needs a human, and the decision controls. */
export function ReviewBox({ detail, onChange, nav, children }: Props) {
  const bh = detail.borehole
  const [reviewer, setReviewer] = useState(loadReviewer)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const status = bh.review_status
  const decided = status === 'approved' || status === 'rejected'

  async function decide(decision: ReviewStatus) {
    if (busy || decision === status) return
    if (!reviewer.trim()) {
      setError('Enter your name so the decision can be traced.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      saveReviewer(reviewer.trim())
      onChange(await postReview(bh.borehole_id, decision, reviewer.trim(), comment.trim() || null, detail.content_hash ?? null, detail.review_revision ?? null))
      setComment('')
      if (nav && decision !== 'pending_review') nav.advance()
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
  const decideRef = useRef(decide)
  decideRef.current = decide
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return
      const key = e.key.toLowerCase()
      if (key === 'a') { e.preventDefault(); decideRef.current('approved') }
      if (key === 'r') { e.preventDefault(); decideRef.current('rejected') }
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
        <ul className="review-chips">
          {children}
          {bh.score_reasons.map((r) => <li key={r} className="chip-pill">{r}</li>)}
        </ul>
        <div className="review-buttons">
          {decided && (
            <button className="btn" disabled={busy} onClick={() => decide('pending_review')}>Reopen</button>
          )}
          {status !== 'rejected' && (
            <button className="btn btn-reject" disabled={busy} onClick={() => decide('rejected')} title="Shortcut: R">
              {nav ? 'Reject and next' : 'Reject'} <kbd>R</kbd>
            </button>
          )}
          {status !== 'approved' && (
            <button className="btn btn-approve" disabled={busy} onClick={() => decide('approved')} title="Shortcut: A">
              {nav ? 'Approve and next' : 'Approve'} <kbd>A</kbd>
            </button>
          )}
        </div>
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
      {error && <p className="error" role="alert">{error}</p>}

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
    </section>
  )
}
