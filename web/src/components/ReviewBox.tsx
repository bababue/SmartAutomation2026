import { useState } from 'react'
import { postReview } from '../api'
import { STATUS_LABEL, fmtDateTime, loadReviewer, saveReviewer, scoreText } from '../review'
import type { Detail, ReviewStatus } from '../types'

interface Props {
  detail: Detail
  onChange: (update: Pick<Detail, 'borehole' | 'review_log'>) => void
}

/** Release status, why it needs a human, and the decision controls. */
export function ReviewBox({ detail, onChange }: Props) {
  const bh = detail.borehole
  const [reviewer, setReviewer] = useState(loadReviewer)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const status = bh.review_status
  const decided = status === 'approved' || status === 'rejected'

  async function decide(decision: ReviewStatus) {
    if (!reviewer.trim()) {
      setError('Enter your name so the decision can be traced.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      saveReviewer(reviewer.trim())
      onChange(await postReview(bh.borehole_id, decision, reviewer.trim(), comment.trim() || null))
      setComment('')
    } catch (e) {
      setError(`Decision not saved (${(e as Error).message}).`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className={`review review-${status}`} aria-label="Release status">
      <div className="review-head">
        <div className="review-score" title={`Release threshold ${scoreText(detail.threshold)}`}>
          <span className="review-score-n">{scoreText(bh.score)}</span>
          <span className="review-score-of">/ 100</span>
        </div>
        <div>
          <p className="review-status">{STATUS_LABEL[status]}</p>
          <p className="muted review-meta">
            {decided
              ? `by ${bh.reviewed_by ?? 'unknown'}, ${fmtDateTime(bh.reviewed_at)}`
              : status === 'auto_approved'
                ? `Score at or above ${scoreText(detail.threshold)} and no findings that need a person`
                : `Below ${scoreText(detail.threshold)} or a finding that needs a person`}
          </p>
        </div>
      </div>

      {bh.score_reasons.length > 0 && (
        <ul className="review-reasons">{bh.score_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      )}
      {bh.review_comment && <p className="review-comment">“{bh.review_comment}”</p>}

      <div className="review-form">
        <label>
          Your name
          <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} autoComplete="name" />
        </label>
        <label className="review-form-comment">
          Comment (optional)
          <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="e.g. depths checked against page 2" />
        </label>
        <div className="review-buttons">
          {status !== 'approved' && (
            <button className="btn btn-approve" disabled={busy} onClick={() => decide('approved')}>Approve</button>
          )}
          {status !== 'rejected' && (
            <button className="btn btn-reject" disabled={busy} onClick={() => decide('rejected')}>Reject</button>
          )}
          {decided && (
            <button className="btn" disabled={busy} onClick={() => decide('pending_review')}>Reopen</button>
          )}
        </div>
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
