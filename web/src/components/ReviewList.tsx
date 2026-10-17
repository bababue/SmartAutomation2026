import { useState } from 'react'
import { STATUS_LABEL, scoreText } from '../review'
import type { CrsGroup, GroupFacet, QueueItem } from '../types'
import { ConfirmCrs } from './ConfirmCrs'

interface Props {
  items: QueueItem[]
  groups: Pick<GroupFacet, 'group' | 'project' | 'count' | 'released'>[]
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
export function ReviewList({ items, groups, total, loading, audit, selectedId, onSelect, crsGroups, onChanged }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const byGroup = new Map<string, QueueItem[]>()
  for (const item of items) byGroup.set(item.grp, [...(byGroup.get(item.grp) ?? []), item])
  function toggle(group: string) {
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(group)) next.add(group)
      return next
    })
  }

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
      {!loading && !items.length && groups.length > 0 && <div className="result-head">No {audit ? 'sampled' : 'waiting'} records match these filters.</div>}
      {!loading && !items.length && !groups.length ? (
        <div className="empty"><p>{audit
          ? 'No audit sample drawn yet: run python -m pipeline eval audit.'
          : 'No waiting record matches these filters.'}</p></div>
      ) : (
        <ul className="results review-list" aria-busy={loading}>
          {groups.map((group) => {
            const entries = byGroup.get(group.group) ?? []
            const share = group.count ? Math.round(100 * group.released / group.count) : 0
            const open = expanded.has(group.group)
            return <li key={group.group} className="result-group">
              <div className="group-head">
                <button className="group-toggle" onClick={() => toggle(group.group)} aria-expanded={open}>
                  <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 3.5L5 7l3.5-3.5" /></svg>
                  <span className="group-name">{group.project ?? group.group}</span>
                  <span className="group-meta">{group.project ? `${group.group} · ` : ''}{group.count.toLocaleString('en')} {group.count === 1 ? 'hole' : 'holes'} · {share} % released</span>
                  <span className="group-share" aria-hidden="true"><span style={{ width: `${share}%` }} /></span>
                </button>
              </div>
              {open && <ul className="group-items">
                {!entries.length && <li className="group-empty">No {audit ? 'sampled' : 'waiting'} boreholes in this group.</li>}
                {entries.map((it) => <li key={it.id}>
                  <button className={`review-item${it.id === selectedId ? ' result-selected' : ''}`} onClick={() => onSelect(it.id)}
                    aria-current={it.id === selectedId}>
                    <span className="queue-score" style={{ ['--s' as string]: it.score ?? 0 }}>{scoreText(it.score)}</span>
                    <span className="review-item-text">
                      <span className="result-name">{it.name}</span>
                      <span className="result-place">{it.location_name ?? it.project ?? 'Unnamed site'}{it.country ? `, ${it.country}` : ''}</span>
                      <span className="result-flags">
                        <span className={`flag flag-${it.review_status}`}>{STATUS_LABEL[it.review_status]}</span>
                        {it.loc_method === 'geocoded_place' && <span className="flag flag-warn" title="Position estimated from the place name">approx. position</span>}
                      </span>
                    </span>
                  </button>
                </li>)}
              </ul>}
            </li>
          })}
        </ul>
      )}
    </>
  )
}
