import { useState } from 'react'
import { postCorrection } from '../api'
import { LITHOLOGY, lithStyle } from '../lithology'
import { fmtDateTime, loadReviewer } from '../review'
import type { CorrectionKind, CorrectionRecord, DetailUpdate, Layer } from '../types'

/** Sends a correction under the reviewer name from the release box; throws a readable message. */
function useCorrection(boreholeId: string, onChange: (update: DetailUpdate) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send(kind: CorrectionKind, payload: Record<string, unknown>, scope: 'borehole' | 'document' = 'borehole') {
    const reviewer = loadReviewer().trim()
    if (!reviewer) {
      setError('Enter your name in the release box first, so the correction can be traced.')
      return false
    }
    setBusy(true)
    setError(null)
    try {
      onChange(await postCorrection(boreholeId, kind, payload, reviewer, scope))
      return true
    } catch (e) {
      const text = (e as Error).message
      const detail = text.match(/"detail":\s*"([^"]+)"/)?.[1]
      setError(`Correction not saved: ${detail ?? text}`)
      return false
    } finally {
      setBusy(false)
    }
  }
  return { send, busy, error }
}

const num = (v: string) => (v.trim() === '' ? NaN : Number(v.replace(',', '.')))

/** Position correction: read the printed coordinates in another CRS, or set the position by hand. */
export function PositionCorrection({ boreholeId, hasCoords, currentEpsg, onChange }: {
  boreholeId: string
  hasCoords: boolean
  currentEpsg: number | null
  onChange: (update: DetailUpdate) => void
}) {
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'crs' | 'position'>(hasCoords ? 'crs' : 'position')
  const [epsg, setEpsg] = useState(currentEpsg ? String(currentEpsg) : '')
  const [wholeDoc, setWholeDoc] = useState(true)
  const [lat, setLat] = useState('')
  const [lon, setLon] = useState('')
  const { send, busy, error } = useCorrection(boreholeId, onChange)

  if (!open) {
    return <button className="btn btn-small" onClick={() => setOpen(true)}>Correct position</button>
  }
  async function save() {
    const ok = mode === 'crs'
      ? await send('crs', { epsg: Math.trunc(num(epsg)) }, wholeDoc ? 'document' : 'borehole')
      : await send('position', { lat: num(lat), lon: num(lon) })
    if (ok) setOpen(false)
  }
  const valid = mode === 'crs' ? Number.isInteger(num(epsg)) : Number.isFinite(num(lat)) && Number.isFinite(num(lon))
  return (
    <div className="correction-form">
      <div className="correction-modes" role="radiogroup" aria-label="Correction type">
        <label><input type="radio" checked={mode === 'crs'} disabled={!hasCoords} onChange={() => setMode('crs')} /> Coordinate system</label>
        <label><input type="radio" checked={mode === 'position'} onChange={() => setMode('position')} /> Position by hand</label>
      </div>
      {mode === 'crs' ? (
        <>
          <label>EPSG code <input inputMode="numeric" value={epsg} onChange={(e) => setEpsg(e.target.value)} placeholder="e.g. 28356" /></label>
          <label className="correction-check">
            <input type="checkbox" checked={wholeDoc} onChange={(e) => setWholeDoc(e.target.checked)} /> For all holes of this document
          </label>
          <p className="muted">The printed coordinates are read again in this system; the position is marked as confirmed.</p>
        </>
      ) : (
        <div className="correction-row">
          <label>Latitude <input inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} placeholder="-33.8568" /></label>
          <label>Longitude <input inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} placeholder="151.2153" /></label>
        </div>
      )}
      <div className="correction-buttons">
        <button className="btn btn-approve" disabled={busy || !valid} onClick={save}>Save correction</button>
        <button className="btn" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

const LITH_OPTIONS = Object.entries(LITHOLOGY).map(([key, s]) => ({ key, label: s.label }))

/** Inline editor for one layer row: depths, lithology, or delete. */
export function LayerEditor({ boreholeId, layer, onChange, onDone }: {
  boreholeId: string
  layer: Layer
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const [top, setTop] = useState(String(layer.top_m))
  const [bottom, setBottom] = useState(String(layer.bottom_m))
  const [primary, setPrimary] = useState(layer.primary)
  const { send, busy, error } = useCorrection(boreholeId, onChange)
  const orig = { orig_top: layer.top_m, orig_bottom: layer.bottom_m }

  async function save() {
    const payload: Record<string, unknown> = { ...orig }
    if (num(top) !== layer.top_m || num(bottom) !== layer.bottom_m) Object.assign(payload, { top: num(top), bottom: num(bottom) })
    if (primary !== layer.primary) payload.primary = primary
    if (Object.keys(payload).length === 2) return onDone()  // nothing changed
    if (await send('layer', payload)) onDone()
  }
  async function remove() {
    if (await send('layer', { ...orig, delete: true })) onDone()
  }
  const valid = Number.isFinite(num(top)) && Number.isFinite(num(bottom)) && num(top) >= 0 && num(top) < num(bottom)
  return (
    <tr className="row-editing">
      <td><input className="num-input" inputMode="decimal" value={top} onChange={(e) => setTop(e.target.value)} aria-label="From (m)" /></td>
      <td><input className="num-input" inputMode="decimal" value={bottom} onChange={(e) => setBottom(e.target.value)} aria-label="To (m)" /></td>
      <td>
        <select value={primary} onChange={(e) => setPrimary(e.target.value)} aria-label="Lithology">
          {LITH_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>
      </td>
      <td colSpan={3}>
        <div className="correction-buttons">
          <button className="btn btn-small btn-approve" disabled={busy || !valid} onClick={save}>Save</button>
          <button className="btn btn-small btn-reject" disabled={busy} onClick={remove}>Delete layer</button>
          <button className="btn btn-small" disabled={busy} onClick={onDone}>Cancel</button>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
      </td>
    </tr>
  )
}

function describe(c: CorrectionRecord): string {
  const p = c.payload
  if (c.kind === 'crs') return `Coordinate system set to EPSG:${p.epsg}${c.scope === 'document' ? ' for the whole document' : ''}`
  if (c.kind === 'position') return `Position set to ${p.lat}, ${p.lon}`
  const span = `Layer ${p.orig_top}–${p.orig_bottom} m`
  if (p.delete) return `${span} deleted`
  return `${span}: ${[p.top != null ? `${p.top}–${p.bottom} m` : null, p.primary ? lithStyle(String(p.primary)).label : null].filter(Boolean).join(', ')}`
}

/** Corrections that apply to this record, newest first; they are re-applied after every run. */
export function CorrectionList({ items }: { items: CorrectionRecord[] }) {
  if (!items.length) return null
  return (
    <details className="review-log" open>
      <summary>Corrections ({items.length}), kept across re-runs</summary>
      <ul>
        {[...items].reverse().map((c) => (
          <li key={c.id}>{fmtDateTime(c.at)}: {describe(c)} by {c.reviewer}{c.comment ? `, “${c.comment}”` : ''}</li>
        ))}
      </ul>
    </details>
  )
}
