import { useState } from 'react'
import { routeLength, tunnelProject, type SavedTunnel } from '../tunnelRoute'
import type { Feature } from '../types'

interface Props {
  tunnels: SavedTunnel[]
  boreholes: Map<string, Feature>
  selectedId: string | null
  onSelect: (id: string) => void
  onShowBoreholes: () => void
}

const formatLength = (metres: number) => metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${Math.round(metres)} m`

export function TunnelList({ tunnels, boreholes, selectedId, onSelect, onShowBoreholes }: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
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
    setCollapsed((current) => {
      const next = new Set(current)
      if (!next.delete(group)) next.add(group)
      return next
    })
  }

  return <>
    <div className="filters tunnel-tabs">
      <div className="filter-bar" role="group" aria-label="Map content">
        <button className="pill pill-on" onClick={onShowBoreholes} aria-label="Close tunnels and show boreholes">
          Tunnels <span aria-hidden="true">×</span>
        </button>
      </div>
    </div>
    {tunnels.length ? <ul className="tunnel-results">
      {groups.map((group) => {
        const open = group.tunnels.some((tunnel) => tunnel.id === selectedId) || !collapsed.has(group.key)
        return <li key={group.key} className="result-group">
          <div className="group-head">
            <button className="group-toggle" onClick={() => toggle(group.key)} aria-expanded={open}>
              <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 3.5L5 7l3.5-3.5" /></svg>
              <span className="group-name">{group.name}</span>
            </button>
          </div>
          {open && <ul className="group-items">
            {group.tunnels.map((tunnel) => <li key={tunnel.id}>
              <button className={`tunnel-result${selectedId === tunnel.id ? ' tunnel-result-selected' : ''}`}
                aria-current={selectedId === tunnel.id} onClick={() => onSelect(tunnel.id)}>
                <strong>{tunnel.name || 'Tunnel'}</strong>
                <span>{tunnel.route ? formatLength(routeLength(tunnel.route.coordinates)) : 'Route not drawn'}
                  {' · '}{tunnel.holeIds.length} borehole{tunnel.holeIds.length === 1 ? '' : 's'}</span>
              </button>
            </li>)}
          </ul>}
        </li>
      })}
    </ul> : <div className="empty"><p>No tunnels yet.</p><p className="muted">Choose New tunnel from the menu to draw an alignment.</p></div>}
  </>
}
