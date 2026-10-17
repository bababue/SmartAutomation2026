import { useRef, useState } from 'react'
import { routeFromGeoJson, routeLength, type RoutePoint, type TunnelRoute } from '../tunnelRoute'

export interface RouteCandidate {
  id: string
  name: string
  chainageM: number
  offsetM: number
}

interface Props {
  route: TunnelRoute | null
  draft: RoutePoint[] | null
  candidates: RouteCandidate[]
  selectedIds: string[]
  loading: boolean
  error: string | null
  onChange: (route: TunnelRoute) => void
  onImport: (route: TunnelRoute) => void
  onStartDrawing: () => void
  onUndo: () => void
  onFinishDrawing: () => void
  onCancelDrawing: () => void
  onSelectNearby: () => void
  onToggleHole: (id: string) => void
  onDelete: () => void
  onClose: () => void
}

const formatMetres = (value: number) => value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${Math.round(value)} m`
const numberOrNull = (value: string) => value === '' ? null : Number(value)

export function RoutePanel({ route, draft, candidates, selectedIds, loading, error, onChange, onImport,
  onStartDrawing, onUndo, onFinishDrawing, onCancelDrawing, onSelectNearby, onToggleHole, onDelete, onClose }: Props) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [importError, setImportError] = useState<string | null>(null)

  async function importFile(file: File) {
    try {
      const imported = routeFromGeoJson(await file.text(), file.name.replace(/\.(geojson|json)$/i, ''))
      setImportError(null)
      onImport(imported)
    } catch (cause) {
      setImportError(cause instanceof Error ? cause.message : 'Could not read this GeoJSON file.')
    }
  }

  const set = (change: Partial<TunnelRoute>) => { if (route) onChange({ ...route, ...change }) }

  return (
    <aside className="route-panel" aria-label="Tunnel route">
      <header className="route-panel-head">
        <div><h2>Tunnel route</h2><p className="muted">Longitudinal section along a planned alignment</p></div>
        <button className="btn btn-icon" onClick={onClose} aria-label="Close tunnel route" title="Close route view">×</button>
      </header>

      <div className="route-panel-scroll">
        {route && <label className="route-field">Route name
          <input value={route.name} onChange={(event) => set({ name: event.target.value })} aria-label="Route name" />
        </label>}
        <div className="route-actions">
          {draft ? <>
            <button className="btn" onClick={onUndo} disabled={!draft.length}>Undo point</button>
            <button className="btn btn-primary" onClick={onFinishDrawing} disabled={draft.length < 2}>Finish route</button>
            <button className="btn" onClick={onCancelDrawing}>Cancel</button>
          </> : <>
            <button className="btn btn-primary" onClick={onStartDrawing}>{route ? 'Redraw' : 'Draw on map'}</button>
            <button className="btn" onClick={() => fileInput.current?.click()}>Import GeoJSON</button>
            <input ref={fileInput} className="visually-hidden" type="file" accept=".geojson,.json,application/geo+json,application/json"
              onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFile(file) }}
              aria-label="Import tunnel route GeoJSON" />
          </>}
        </div>
        {draft && <p className="route-hint" role="status">Click the map to place vertices. {draft.length} point{draft.length === 1 ? '' : 's'} placed.</p>}
        {importError && <p className="error" role="alert">{importError}</p>}
        {!route && !draft && <p className="route-hint">Draw a line on the map or import a WGS84 GeoJSON LineString.</p>}

        {route && <>
          <p className="route-summary">{formatMetres(routeLength(route.coordinates))} · {route.coordinates.length} vertices · saved in this browser</p>
          <div className="route-divider" />
          <h3>Boreholes</h3>
          <label className="route-field">Distance from route, each side (m)
            <input type="number" min="1" max="5000" step="1" value={route.corridorM}
              onChange={(event) => { const value = Number(event.target.value); if (Number.isFinite(value) && value > 0) set({ corridorM: Math.min(5000, value) }) }} />
          </label>
          <div className="route-actions">
            <button className="btn" onClick={onSelectNearby} disabled={loading || !candidates.length}>Use {candidates.length} nearby boreholes</button>
          </div>
          {loading && <p className="muted" role="status">Finding boreholes…</p>}
          {error && <p className="error" role="alert">{error}</p>}
          {!loading && !error && candidates.length === 0 && <p className="route-hint">No boreholes in this corridor. Increase the distance or add one from the map or list.</p>}
          {candidates.length > 0 && <ul className="route-candidates" aria-label="Boreholes near route">
            {candidates.map((candidate) => <li key={candidate.id}>
              <label><input type="checkbox" checked={selectedIds.includes(candidate.id)} onChange={() => onToggleHole(candidate.id)} />
                <span className="route-candidate-name">{candidate.name}</span>
                <span className="muted">{formatMetres(candidate.chainageM)} · {Math.round(candidate.offsetM)} m away</span>
              </label>
            </li>)}
          </ul>}

          <div className="route-divider" />
          <div className="route-profile">
            <h3>Planned tunnel profile</h3>
            <p className="route-hint">Optional. Enter design levels to place the tunnel in the section.</p>
            <label className="route-field">Vertical reference
              <select value={route.tunnelMode} onChange={(event) => set({ tunnelMode: event.target.value as TunnelRoute['tunnelMode'] })}>
                <option value="depth">Depth below ground</option>
                <option value="elevation">Elevation above datum</option>
              </select>
            </label>
            {route.tunnelMode === 'depth' ? <label className="route-field">Axis depth (m)
              <input type="number" min="0" step="0.1" value={route.depthM ?? ''} placeholder="e.g. 15"
                onChange={(event) => set({ depthM: numberOrNull(event.target.value) })} />
            </label> : <>
              <label className="route-field">Axis elevation at route start (m)
                <input type="number" step="0.1" value={route.startElevationM ?? ''}
                  onChange={(event) => set({ startElevationM: numberOrNull(event.target.value) })} />
              </label>
              <label className="route-field">Axis elevation at route end (m)
                <input type="number" step="0.1" value={route.endElevationM ?? ''}
                  onChange={(event) => set({ endElevationM: numberOrNull(event.target.value) })} />
              </label>
            </>}
            <label className="route-field">Tunnel diameter (m, optional)
              <input type="number" min="0" step="0.1" value={route.diameterM ?? ''}
                onChange={(event) => set({ diameterM: numberOrNull(event.target.value) })} />
            </label>
          </div>
          <button className="route-delete" onClick={onDelete}>Delete saved route</button>
        </>}
      </div>
    </aside>
  )
}
