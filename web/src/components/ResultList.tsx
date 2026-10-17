import { useEffect, useRef, useState } from 'react'
import type { Feature, GroupFacet } from '../types'
import { fmtMetres } from '../format'
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

export function ResultList({ features, groups, activeGroup, onShowGroup, selectedId, onSelect, loading }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const selectedRef = useRef<HTMLButtonElement>(null)
  const selectedGroup = features.find((feature) => feature.properties.id === selectedId)?.properties.grp
  useEffect(() => {
    if (loading || !selectedGroup) return
    const button = selectedRef.current
    button?.focus({ preventScroll: true })
    button?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [selectedId, selectedGroup, loading])
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
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(group)) next.add(group)
      return next
    })
  }

  return (
    <ul className="results" aria-busy={loading}>
      {ordered.map((group) => {
        const items = byGroup.get(group)!.sort((a, b) =>
          a.properties.name.localeCompare(b.properties.name, undefined, { numeric: true }))
        const project = facet.get(group)?.project
        const open = group === selectedGroup || group === activeGroup || expanded.has(group)
        return (
          <li key={group} className="result-group">
            <div className="group-head">
              <button className="group-toggle" onClick={() => toggle(group)} aria-expanded={open}>
                <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 3.5L5 7l3.5-3.5" /></svg>
                <span className="group-name">{project ?? group}</span>
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
                      ref={p.id === selectedId ? selectedRef : undefined}
                      className={`result${p.id === selectedId ? ' result-selected' : ''}`}
                      onClick={() => onSelect(p.id)}
                      aria-current={p.id === selectedId}
                    >
                      <span className="result-name">{p.name}</span>
                      <span className="result-depth">{fmtMetres(p.depth)}</span>
                      <span className="result-place">{p.location_name ?? p.project ?? 'Unnamed site'}{p.country ? `, ${p.country}` : ''}</span>
                      <CoreStrip strip={p.strip} depth={p.depth} />
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
