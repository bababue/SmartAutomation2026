import { useState } from 'react'
import { postCorrection } from '../api'
import { LITHOLOGY, lithStyle } from '../lithology'
import { fmtDateTime, loadReviewer } from '../review'
import type { Borehole, CorrectionKind, CorrectionRecord, DetailUpdate, Layer } from '../types'

/** Sends corrections under the reviewer name from the release box; keeps a readable error. */
function useCorrection(boreholeId: string, onChange: (update: DetailUpdate) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** Sends one or more corrections in order; stops at the first that fails. */
  async function send(items: { kind: CorrectionKind; payload: Record<string, unknown>; scope?: 'borehole' | 'document' }[]) {
    const reviewer = loadReviewer().trim()
    if (!reviewer) {
      setError('Enter your name in the release box first, so the correction can be traced.')
      return false
    }
    setBusy(true)
    setError(null)
    try {
      for (const it of items) onChange(await postCorrection(boreholeId, it.kind, it.payload, reviewer, it.scope ?? 'borehole'))
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
const str = (v: number | string | null | undefined) => (v == null ? '' : String(v))

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
      ? await send([{ kind: 'crs', payload: { epsg: Math.trunc(num(epsg)) }, scope: wholeDoc ? 'document' : 'borehole' }])
      : await send([{ kind: 'position', payload: { lat: num(lat), lon: num(lon) } }])
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

type FieldKey = 'borehole_name' | 'project_name' | 'location_name' | 'surface_elevation_m' | 'elevation_datum' | 'depth_m' | 'start_date' | 'end_date'
const FIELDS: { key: FieldKey; label: string; type: 'text' | 'number' | 'date'; hint?: string }[] = [
  { key: 'borehole_name', label: 'Hole name', type: 'text' },
  { key: 'project_name', label: 'Project', type: 'text' },
  { key: 'location_name', label: 'Location', type: 'text' },
  { key: 'surface_elevation_m', label: 'Ground level (m)', type: 'number', hint: 'positive up' },
  { key: 'elevation_datum', label: 'Level datum', type: 'text', hint: 'e.g. AHD, mOD, MSL' },
  { key: 'depth_m', label: 'Final depth (m)', type: 'number' },
  { key: 'start_date', label: 'Started', type: 'date' },
  { key: 'end_date', label: 'Finished', type: 'date' },
]

/** Header values: fill in what the extraction missed or correct what it misread. */
export function RecordEditor({ borehole, onChange, onDone }: {
  borehole: Borehole
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const initial = Object.fromEntries(FIELDS.map((f) => [f.key, str(borehole[f.key])])) as Record<FieldKey, string>
  const [values, setValues] = useState(initial)
  const [groundwater, setGroundwater] = useState('')
  const { send, busy, error } = useCorrection(borehole.borehole_id, onChange)

  const changed = FIELDS.filter((f) => values[f.key].trim() !== initial[f.key])
  const invalid = changed.some((f) => f.type === 'number' && values[f.key].trim() !== '' && !Number.isFinite(num(values[f.key])))
    || (groundwater.trim() !== '' && !(num(groundwater) >= 0))

  async function save() {
    const items: { kind: CorrectionKind; payload: Record<string, unknown> }[] = changed.map((f) => ({
      kind: 'field',
      payload: { field: f.key, value: values[f.key].trim() === '' ? null : f.type === 'number' ? num(values[f.key]) : values[f.key].trim() },
    }))
    if (groundwater.trim() !== '') items.push({ kind: 'groundwater', payload: { depth_m: num(groundwater) } })
    if (!items.length) return onDone()
    if (await send(items)) onDone()
  }

  return (
    <div className="correction-form correction-form-wide">
      <div className="correction-grid">
        {FIELDS.map((f) => (
          <label key={f.key}>
            {f.label}{f.hint && <span className="muted"> ({f.hint})</span>}
            <input type={f.type === 'date' ? 'date' : 'text'} inputMode={f.type === 'number' ? 'decimal' : undefined}
              value={values[f.key]} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          </label>
        ))}
        <label>
          Add groundwater level (m)
          <input inputMode="decimal" value={groundwater} onChange={(e) => setGroundwater(e.target.value)} placeholder="below ground" />
        </label>
      </div>
      <div className="correction-buttons">
        <button className="btn btn-approve" disabled={busy || invalid} onClick={save}>
          {changed.length || groundwater ? `Save ${changed.length + (groundwater ? 1 : 0)} change(s)` : 'Close'}
        </button>
        <button className="btn" disabled={busy} onClick={onDone}>Cancel</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

const LITH_OPTIONS = Object.entries(LITHOLOGY).map(([key, s]) => ({ key, label: s.label }))

/** Inline editor for one layer row: depths, lithology, description and stratigraphy, or delete. */
export function LayerEditor({ boreholeId, layer, onChange, onDone }: {
  boreholeId: string
  layer: Layer
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const [top, setTop] = useState(String(layer.top_m))
  const [bottom, setBottom] = useState(String(layer.bottom_m))
  const [primary, setPrimary] = useState(layer.primary)
  const [description, setDescription] = useState(layer.description_original)
  const [code, setCode] = useState(str(layer.stratigraphic_code))
  const [stratName, setStratName] = useState(str(layer.stratigraphic_name))
  const [age, setAge] = useState(str(layer.geologic_age))
  const { send, busy, error } = useCorrection(boreholeId, onChange)
  const orig = { orig_top: layer.top_m, orig_bottom: layer.bottom_m }

  async function save() {
    const payload: Record<string, unknown> = { ...orig }
    if (num(top) !== layer.top_m || num(bottom) !== layer.bottom_m) Object.assign(payload, { top: num(top), bottom: num(bottom) })
    if (primary !== layer.primary) payload.primary = primary
    if (description.trim() !== layer.description_original) payload.description_original = description.trim()
    if (code.trim() !== str(layer.stratigraphic_code)) payload.stratigraphic_code = code.trim()
    if (stratName.trim() !== str(layer.stratigraphic_name)) payload.stratigraphic_name = stratName.trim()
    if (age.trim() !== str(layer.geologic_age)) payload.geologic_age = age.trim()
    if (Object.keys(payload).length === 2) return onDone()  // nothing changed
    if (await send([{ kind: 'layer', payload }])) onDone()
  }
  async function remove() {
    if (await send([{ kind: 'layer', payload: { ...orig, delete: true } }])) onDone()
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
      <td><textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} aria-label="Description as printed" /></td>
      <td className="strat-inputs">
        <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="code" aria-label="Stratigraphic code" />
        <input value={stratName} onChange={(e) => setStratName(e.target.value)} placeholder="formation" aria-label="Stratigraphic name" />
        <input value={age} onChange={(e) => setAge(e.target.value)} placeholder="age" aria-label="Geologic age" />
      </td>
      <td>
        <div className="correction-buttons correction-buttons-stack">
          <button className="btn btn-small btn-approve" disabled={busy || !valid} onClick={save}>Save</button>
          <button className="btn btn-small btn-reject" disabled={busy} onClick={remove}>Delete</button>
          <button className="btn btn-small" disabled={busy} onClick={onDone}>Cancel</button>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
      </td>
    </tr>
  )
}

/** Row at the end of the layer table: add a layer the extraction missed. */
export function LayerAdder({ boreholeId, suggestTop, onChange, onDone }: {
  boreholeId: string
  suggestTop: number
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const [top, setTop] = useState(String(suggestTop))
  const [bottom, setBottom] = useState('')
  const [primary, setPrimary] = useState('unknown')
  const [description, setDescription] = useState('')
  const { send, busy, error } = useCorrection(boreholeId, onChange)
  const valid = Number.isFinite(num(top)) && Number.isFinite(num(bottom)) && num(top) >= 0 && num(top) < num(bottom)

  async function save() {
    const payload = { top: num(top), bottom: num(bottom), primary, description_original: description.trim() || null }
    if (await send([{ kind: 'layer_add', payload }])) onDone()
  }
  return (
    <tr className="row-editing">
      <td><input className="num-input" inputMode="decimal" value={top} onChange={(e) => setTop(e.target.value)} aria-label="From (m)" /></td>
      <td><input className="num-input" inputMode="decimal" value={bottom} onChange={(e) => setBottom(e.target.value)} aria-label="To (m)" autoFocus /></td>
      <td>
        <select value={primary} onChange={(e) => setPrimary(e.target.value)} aria-label="Lithology">
          {LITH_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>
      </td>
      <td colSpan={2}><textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} placeholder="Description as printed" aria-label="Description as printed" /></td>
      <td>
        <div className="correction-buttons correction-buttons-stack">
          <button className="btn btn-small btn-approve" disabled={busy || !valid} onClick={save}>Add</button>
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
  if (c.kind === 'field') return `${String(p.field).replaceAll('_', ' ')} set to ${p.value ?? '–'}`
  if (c.kind === 'groundwater') return p.dry ? 'Groundwater: dry' : `Groundwater level ${p.depth_m} m added`
  if (c.kind === 'layer_add') return `Layer ${p.top}–${p.bottom} m added (${lithStyle(String(p.primary)).label})`
  const span = `Layer ${p.orig_top}–${p.orig_bottom} m`
  if (p.delete) return `${span} deleted`
  const texts = ['description_original', 'stratigraphic_code', 'stratigraphic_name', 'geologic_age'].filter((k) => k in p)
  return `${span}: ${[p.top != null ? `${p.top}–${p.bottom} m` : null, p.primary ? lithStyle(String(p.primary)).label : null,
    texts.length ? `${texts.length} text field(s)` : null].filter(Boolean).join(', ')}`
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
