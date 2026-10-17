import { scoreText } from '../review'
import type { CrsGroup, QueueItem } from '../types'
import { ConfirmCrs } from './ConfirmCrs'

interface Props {
  items: QueueItem[]
  /** All waiting records that match (the items are the weakest of them; deciding them brings up the next). */
  total: number
  loading: boolean
  /** The release audit sample instead of the waiting records. */
  audit: boolean
  selectedId: string | null
  onSelect: (id: string) => void
  /** Documents whose coordinate system to confirm; shown above the records. */
  crsGroups: CrsGroup[]
  onChanged: () => void
}

/** The waiting records, or the audit sample, weakest first. */
export function ReviewList({ items, total, loading, audit, selectedId, onSelect, crsGroups, onChanged }: Props) {
  return (
    <>
      <div className="result-head">
        <span>{loading ? 'Loading…' : `${total.toLocaleString('en')} ${audit ? 'sampled' : 'waiting'}`}</span>
      </div>
      {crsGroups.map((g) => (
        <div key={g.group} className="review-crs">
          <strong>{g.group}</strong>
          <ConfirmCrs crs={g} onChanged={onChanged} />
        </div>
      ))}
      {!loading && !items.length ? (
        <div className="empty"><p>{audit
          ? 'No audit sample drawn yet: run python -m pipeline eval audit.'
          : 'No waiting record matches these filters.'}</p></div>
      ) : (
        <ul className="results review-list" aria-busy={loading}>
          {items.map((it) => (
            <li key={it.id}>
              <button className={`review-item${it.id === selectedId ? ' result-selected' : ''}`} onClick={() => onSelect(it.id)}
                aria-current={it.id === selectedId}>
                <span className="queue-score" style={{ ['--s' as string]: it.score ?? 0 }}>{scoreText(it.score)}</span>
                <span className="review-item-text">
                  <span className="result-name">{it.name}</span>
                  <span className="result-place">{it.location_name ?? it.project ?? 'Unnamed site'}{it.country ? `, ${it.country}` : ''}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
