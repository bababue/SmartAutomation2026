import { useEffect, useState } from 'react'
import { fetchCrsGroups, postCorrection } from '../api'
import { SHOW_SOURCE_FILE } from '../dev'
import { loadReviewer, saveReviewer, scoreText } from '../review'
import type { CrsGroup, QueueItem } from '../types'

interface Props {
  items: QueueItem[]
  /** All waiting records (the items are the weakest of them). */
  total?: number
  loading: boolean
  selectedId: string | null
  threshold: number
  blockingOnly: boolean
  onBlockingOnly: (on: boolean) => void
  onSelect: (id: string) => void
  onChanged: () => void
}

/** One card per document whose waiting holes share an inferred coordinate system. */
function CrsConfirmations({ refreshKey, onChanged }: { refreshKey: unknown; onChanged: () => void }) {
  const [groups, setGroups] = useState<CrsGroup[]>([])
  const [reviewer, setReviewer] = useState(loadReviewer)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    fetchCrsGroups(ctrl.signal).then(setGroups).catch(() => { /* optional helper: the queue works without it */ })
    return () => ctrl.abort()
  }, [refreshKey])

  if (!groups.length) return null
  async function confirm(g: CrsGroup) {
    if (!reviewer.trim()) { setError('Enter your name, so the confirmation can be traced.'); return }
    setBusy(g.group)
    setError(null)
    try {
      saveReviewer(reviewer.trim())
      await postCorrection(g.borehole_id, 'crs', { epsg: g.epsg }, reviewer.trim(), 'document')
      onChanged()
    } catch (e) {
      setError(`Not saved: ${(e as Error).message}`)
    } finally {
      setBusy(null)
    }
  }
  return (
    <section className="crs-groups" aria-label="Coordinate systems to confirm">
      <h3>Confirm coordinate systems</h3>
      <p className="muted">
        These documents do not state their coordinate system; the pipeline inferred one that matches the named place.
        Check one hole on the map or against the report, then confirm it for all holes of the document.
      </p>
      <label className="crs-groups-name">Your name <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} autoComplete="name" /></label>
      <ul>
        {groups.map((g) => (
          <li key={g.group}>
            <span>
              <strong>{g.group}</strong> · {g.count} holes · {g.name}
              {SHOW_SOURCE_FILE && g.source_file && <span className="original result-file">{g.source_file}</span>}
            </span>
            <button className="btn btn-small btn-approve" disabled={busy != null} onClick={() => confirm(g)}>
              {busy === g.group ? 'Saving…' : `Confirm EPSG:${g.epsg} for all ${g.count}`}
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  )
}

/** Everything that was not released automatically, weakest first. */
export function ReviewQueue({ items, total, loading, selectedId, threshold, blockingOnly, onBlockingOnly, onSelect, onChanged }: Props) {
  return (
    <div className="queue">
      <header className="queue-head">
        <h2>Review queue</h2>
        <p className="muted">
          Records below a score of {scoreText(threshold)}, or with findings that need a person, wait here. Open one,
          compare it with the source page, then approve or reject it.
        </p>
        <label className="check queue-filter">
          <input type="checkbox" checked={blockingOnly} onChange={(e) => onBlockingOnly(e.target.checked)} />
          Only records with blocking findings{blockingOnly && total != null ? ` (${total.toLocaleString('en')})` : ''}
        </label>
      </header>
      <CrsConfirmations refreshKey={items} onChanged={onChanged} />
      {total != null && total > items.length && (
        <p className="muted">Showing the {items.length} weakest of {total.toLocaleString('en')} waiting records; approving them brings up the next.</p>
      )}
      {!loading && !items.length ? (
        <p className="empty">{blockingOnly
          ? 'No waiting record has a blocking finding.'
          : 'Nothing waits for review. New extractions appear here when they are not released automatically.'}</p>
      ) : (
        <table className="queue-table" aria-busy={loading}>
          <thead>
            <tr><th className="num">Score</th><th>Borehole</th><th>Site</th><th>Why it needs a person</th></tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr
                key={it.id}
                className={it.id === selectedId ? 'row-active' : undefined}
                onClick={() => onSelect(it.id)}
                tabIndex={0}
                onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onSelect(it.id)}
              >
                <td className="num"><span className="queue-score" style={{ ['--s' as string]: it.score ?? 0 }}>{scoreText(it.score)}</span></td>
                <td><strong>{it.name}</strong><span className="original">{it.n_layers} layers</span>
                  {SHOW_SOURCE_FILE && it.source_file && <span className="original result-file">{it.source_file}</span>}</td>
                <td>{it.location_name ?? it.project ?? '–'}{it.country ? `, ${it.country}` : ''}</td>
                <td><ul className="queue-reasons">{it.reasons.map((r) => <li key={r}>{r}</li>)}</ul></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
