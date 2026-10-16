import { scoreText } from '../review'
import type { QueueItem } from '../types'

interface Props {
  items: QueueItem[]
  loading: boolean
  selectedId: string | null
  threshold: number
  onSelect: (id: string) => void
}

/** Everything that was not released automatically, weakest first. */
export function ReviewQueue({ items, loading, selectedId, threshold, onSelect }: Props) {
  return (
    <div className="queue">
      <header className="queue-head">
        <h2>Review queue</h2>
        <p className="muted">
          Records below a score of {scoreText(threshold)}, or with findings that need a person, wait here. Open one,
          compare it with the source page, then approve or reject it.
        </p>
      </header>
      {!loading && !items.length ? (
        <p className="empty">Nothing waits for review. New extractions appear here when they are not released automatically.</p>
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
                <td><strong>{it.name}</strong><span className="original">{it.n_layers} layers</span></td>
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
