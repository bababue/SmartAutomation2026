import { useState } from 'react'
import { postCorrection } from '../api'
import { loadReviewer, saveReviewer } from '../review'
import type { CrsGroup } from '../types'

/** Confirms the coordinate system the pipeline inferred for a document, for all its waiting holes. */
export function ConfirmCrs({ crs, onChanged }: { crs: CrsGroup; onChanged: () => void }) {
  const [reviewer, setReviewer] = useState(loadReviewer)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirm() {
    if (!reviewer.trim()) { setAsking(true); return }
    setBusy(true)
    setError(null)
    try {
      saveReviewer(reviewer.trim())
      await postCorrection(crs.borehole_id, 'crs', { epsg: crs.epsg }, reviewer.trim(), 'document')
      onChanged()
    } catch (e) {
      setError(`Not saved: ${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="crs-confirm" title="The document states no coordinate system; this one matches the named place. Check one hole on the map, then confirm.">
      <span>Coordinate system inferred: {crs.name}</span>
      {asking && (
        <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Your name" aria-label="Your name"
          autoComplete="name" autoFocus onKeyDown={(e) => e.key === 'Enter' && confirm()} />
      )}
      <button className="btn btn-small btn-approve" disabled={busy || (asking && !reviewer.trim())} onClick={confirm}>
        {busy ? 'Saving…' : `Confirm for all ${crs.count}`}
      </button>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}
