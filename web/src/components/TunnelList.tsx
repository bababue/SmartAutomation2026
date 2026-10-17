import { useState } from 'react'
import { fmtDistance } from '../format'
import { routeLength, tunnelProject, type SavedTunnel } from '../tunnelRoute'
import type { Feature } from '../types'
import { GroupingToggle } from './GroupingToggle'

interface Props {
  tunnels: SavedTunnel[]
  boreholes: Map<string, Feature>
  selectedId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  grouped: boolean
  onGroupedChange: (grouped: boolean) => void
}

export function TunnelList({ tunnels, boreholes, selectedId, onSelect, onCreate, grouped, onGroupedChange }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const unassigned: SavedTunnel[] = []
  const byProject = new Map<string, SavedTunnel[]>()
  for (const tunnel of tunnels) {
    const project = tunnelProject(tunnel, boreholes)
    if (project) byProject.set(project, [...(byProject.get(project) ?? []), tunnel])
    else unassigned.push(tunnel)
  }
  const groups = [
    ...(unassigned.length ? [{ key: '', name: 'No project', tunnels: unassigned }] : []),
    ...[...byProject].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
      .map(([key, items]) => ({ key, name: key, tunnels: items })),
  ]
  function toggle(group: string) {
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(group)) next.add(group)
      return next
    })
  }

  function renderItem(tunnel: SavedTunnel) {
    return <li key={tunnel.id}>
      <button className={`tunnel-result${selectedId === tunnel.id ? ' tunnel-result-selected' : ''}`}
        aria-current={selectedId === tunnel.id} onClick={() => onSelect(tunnel.id)} data-list-item>
        <strong>{tunnel.name || 'Tunnel'}</strong>
        <span>{tunnel.route ? fmtDistance(routeLength(tunnel.route.coordinates)) : 'Route not drawn'}
          {' · '}{tunnel.holeIds.length} borehole{tunnel.holeIds.length === 1 ? '' : 's'}</span>
      </button>
    </li>
  }

  return <>
    <div className="result-head grouping-head">
      <button className="pill mode-add" onClick={onCreate}>+ New tunnel</button>
      <GroupingToggle grouped={grouped} onChange={onGroupedChange} />
    </div>
    {tunnels.length ? <ul className="tunnel-results">
      {!grouped && tunnels.map(renderItem)}
      {grouped && groups.map((group) => {
        const open = expanded.has(group.key)
        return <li key={group.key} className="result-group">
          <div className="group-head">
            <button className="group-toggle" onClick={() => toggle(group.key)} aria-expanded={open}>
              <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 3.5L5 7l3.5-3.5" /></svg>
              <span className="group-name">{group.name}</span>
            </button>
          </div>
          {open && <ul className="group-items">
            {group.tunnels.map(renderItem)}
          </ul>}
        </li>
      })}
    </ul> : <div className="empty"><p>No tunnels yet.</p><p className="muted">Use the plus button above to draw an alignment.</p></div>}
  </>
}
