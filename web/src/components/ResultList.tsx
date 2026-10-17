import { useState } from 'react'
import type { Feature, GroupFacet } from '../types'
import { STATUS_LABEL, scoreText } from '../review'
import { CoreStrip } from './Profile'

interface Props {
  features: Feature[]
  /** Counts of all matches per document group; the features may be one page of them. */
  groups: GroupFacet[]
  /** The document group the list and map are narrowed to, if any. */
  activeGroup: string | null
  onShowGroup: (group: string) => void
  selectedId: string | null
  onSelect: (id: string) => void
  loading: boolean
}

const fmtDepth = (d: number | null) => (d == null ? '–' : `${d.toLocaleString('en', { maximumFractionDigits: 1 })} m`)

export function ResultList({ features, groups, activeGroup, onShowGroup, selectedId, onSelect, loading }: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  if (!loading && !features.length) {
    return (
      <div className="empty">
        <p>No boreholes match these filters.</p>
        <p className="muted">Clear the search, remove lithology filters or show all release states.</p>
      </div>
    )
  }
  const byGroup = new Map<string, Feature[]>()
  for (const f of features) byGroup.set(f.properties.grp, [...(byGroup.get(f.properties.grp) ?? []), f])
  const facet = new Map(groups.map((g) => [g.group, g]))
  const ordered = [...byGroup.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))

  function toggle(group: string) {
    setCollapsed((c) => {
      const next = new Set(c)
      if (!next.delete(group)) next.add(group)
      return next
    })
  }

  return (
    <ul className="results" aria-busy={loading}>
      {ordered.map((group) => {
        const items = byGroup.get(group)!.sort((a, b) =>
          a.properties.name.localeCompare(b.properties.name, undefined, { numeric: true }))
        const count = facet.get(group)?.count ?? items.length
        const released = facet.get(group)?.released ?? items.filter((f) => f.properties.review_status.endsWith('approved')).length
        const share = Math.round((100 * released) / count)
        const project = facet.get(group)?.project
        const open = !collapsed.has(group)
        return (
          <li key={group} className="result-group">
            <div className="group-head">
              <button className="group-toggle" onClick={() => toggle(group)} aria-expanded={open}>
                <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 3.5L5 7l3.5-3.5" /></svg>
                <span className="group-name">{project ?? group}</span>
                <span className="group-meta">
                  {project ? `${group} · ` : ''}{count.toLocaleString('en')} {count === 1 ? 'hole' : 'holes'} · {share} % released
                </span>
                <span className="group-share" aria-hidden="true"><span style={{ width: `${share}%` }} /></span>
              </button>
              {activeGroup !== group && (
                <button className="group-show" onClick={() => onShowGroup(group)} title="Show only this project, on the map too">
                  Show on map
                </button>
              )}
            </div>
            {open && (
              <ul className="group-items">
                {items.map(({ properties: p }) => (
                  <li key={p.id} className="result-item">
                    <button
                      className={`result${p.id === selectedId ? ' result-selected' : ''}`}
                      onClick={() => onSelect(p.id)}
                      aria-current={p.id === selectedId}
                    >
                      <span className="result-name">{p.name}</span>
                      <span className="result-depth">{fmtDepth(p.depth)}</span>
                      <span className="result-place">{p.location_name ?? p.project ?? 'Unnamed site'}{p.country ? `, ${p.country}` : ''}</span>
                      <CoreStrip strip={p.strip} depth={p.depth} />
                      <span className="result-flags">
                        <span className={`flag flag-${p.review_status}`} title={`Release score ${scoreText(p.score)}`}>
                          {STATUS_LABEL[p.review_status]}
                        </span>
                        {p.loc_method === 'geocoded_place' && <span className="flag flag-warn" title="Position estimated from the place name">approx. position</span>}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        )
      })}
    </ul>
  )
}
