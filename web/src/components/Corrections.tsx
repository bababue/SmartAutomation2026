import { useState, type ReactNode } from 'react'
import { postCorrection } from '../api'
import { fmtNum, parseDecimal, words } from '../format'
import { LITHOLOGY, lithStyle } from '../lithology'
import { fmtDateTime, loadReviewer, withSuffix } from '../review'
import type { Borehole, CorrectionKind, CorrectionRecord, CorrectionScope, DetailUpdate, Layer } from '../types'

type Item = { kind: CorrectionKind; payload: Record<string, unknown>; scope?: CorrectionScope }

/** Sends corrections under the reviewer name from the release bar; keeps a readable error. */
function useCorrection(boreholeId: string, onChange: (update: DetailUpdate) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Some corrections were saved and others not: the form no longer matches the record, and a retry would
  // send the saved ones again.
  const [stale, setStale] = useState(false)

  /** Sends the corrections in order; stops at the first that fails. */
  async function send(items: Item[]) {
    if (!items.length) return true
    const reviewer = loadReviewer().trim()
    if (!reviewer) {
      setError('Enter your name in the release bar first, so the correction can be traced.')
      return false
    }
    setBusy(true)
    setError(null)
    let saved = 0
    try {
      for (const it of items) {
        onChange(await postCorrection(boreholeId, it.kind, it.payload, reviewer, it.scope ?? 'borehole'))
        saved += 1
      }
      return true
    } catch (e) {
      const message = (e as Error).message
      if (saved) {
        setStale(true)
        setError(`Saved ${saved} of ${items.length} corrections; the rest failed (${message}). Cancel and edit again.`)
      } else setError(`Correction not saved: ${message}`)
      return false
    } finally {
      setBusy(false)
    }
  }
  return { send, busy, error, stale }
}

// NaN for an empty or unreadable entry, so that every comparison with it fails.
const num = (v: string) => parseDecimal(v) ?? NaN
const str = (v: number | string | null | undefined) => (v == null ? '' : String(v))
const bad = (v: string) => v.trim() !== '' && !Number.isFinite(num(v))

interface EditState {
  busy: boolean
  canSave: boolean
  onSave: () => void
  onCancel: () => void
}

/** The detail header edited in place: the hole name, project and location become fields in the same type. */
export function HeaderEditor({ borehole, onChange, onDone, children }: {
  borehole: Borehole
  onChange: (update: DetailUpdate) => void
  onDone: () => void
  /** Further header actions, after Cancel and Save. */
  children?: ReactNode
}) {
  const initial = { borehole_name: str(borehole.borehole_name), project_name: str(borehole.project_name),
    location_name: str(borehole.location_name) }
  type Key = keyof typeof initial
  const [v, setV] = useState(initial)
  const { send, busy, error, stale } = useCorrection(borehole.borehole_id, onChange)
  const canSave = !busy && !stale && v.borehole_name.trim() !== ''

  async function save() {
    if (!canSave) return
    const items: Item[] = (Object.keys(initial) as Key[]).filter((k) => v[k].trim() !== initial[k]).map((k) => ({
      kind: 'field', payload: { field: k, value: v[k].trim() || null },
    }))
    if (await send(items)) onDone()
  }
  // A hidden copy of the text sizes each field, so it is exactly as wide as what is typed (or its placeholder).
  const field = (key: Key, label: string, className: string) => (
    <span className={`head-field-wrap ${className}`} data-value={v[key] || label}>
      <input className="head-field" value={v[key]} placeholder={label} aria-label={label} size={1}
        autoFocus={key === 'borehole_name'} onChange={(e) => setV({ ...v, [key]: e.target.value })}
        onKeyDown={(e) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') { e.stopPropagation(); onDone() } }} />
    </span>
  )

  return (
    <>
      <div className="head-edit">
        {field('borehole_name', 'Hole name', 'head-field-name')}
        <p className="detail-sub head-edit-sub">
          {field('project_name', 'Project', 'head-field-sub')}
          {v.project_name.trim() && v.location_name.trim() && <span aria-hidden="true">,</span>}
          {field('location_name', 'Location', 'head-field-sub')}
        </p>
        {error && <p className="error" role="alert">{error}</p>}
      </div>
      <div className="detail-actions">
        <button className="btn btn-small btn-ghost head-edit-btn" onClick={onDone} disabled={busy}>Cancel</button>
        <button className="btn btn-small btn-primary head-edit-btn" onClick={save} disabled={!canSave}>{busy ? 'Saving…' : 'Save'}</button>
        {children}
      </div>
    </>
  )
}

/** Card title with either the Edit button or, while editing, Cancel and Save. */
export function CardHead({ title, id, onEdit, edit }: { title: ReactNode; id?: string; onEdit?: () => void; edit?: EditState }) {
  return (
    <h3 id={id} className="card-head">
      {title}
      {edit ? (
        <span className="card-actions">
          <button className="btn btn-small btn-ghost" onClick={edit.onCancel} disabled={edit.busy}>Cancel</button>
          <button className="btn btn-small btn-primary" onClick={edit.onSave} disabled={edit.busy || !edit.canSave}>
            {edit.busy ? 'Saving…' : 'Save'}
          </button>
        </span>
      ) : onEdit && (
        <button className="btn btn-small btn-ghost" onClick={onEdit}><span aria-hidden="true">✎</span> Edit</button>
      )}
    </h3>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (<><dt>{label}</dt><dd className="record-inputs">{children}</dd></>)
}

/** Header values and position, edited in place. Each changed value becomes one correction. */
export function RecordEditor({ borehole, onChange, onDone }: {
  borehole: Borehole
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const loc = borehole.location
  const initial = {
    borehole_name: str(borehole.borehole_name), project_name: str(borehole.project_name), location_name: str(borehole.location_name),
    surface_elevation_m: str(borehole.surface_elevation_m), elevation_datum: str(borehole.elevation_datum), depth_m: str(borehole.depth_m),
    start_date: str(borehole.start_date), end_date: str(borehole.end_date),
  }
  type Key = keyof typeof initial
  const numeric: Key[] = ['surface_elevation_m', 'depth_m']
  const [v, setV] = useState(initial)
  const [epsg, setEpsg] = useState(str(loc.crs_epsg))
  const [wholeDoc, setWholeDoc] = useState(true)
  const [lat, setLat] = useState(str(loc.lat))
  const [lon, setLon] = useState(str(loc.lon))
  const [groundwater, setGroundwater] = useState('')
  const { send, busy, error, stale } = useCorrection(borehole.borehole_id, onChange)

  const field = (key: Key, label: string, extra: { type?: string; inputMode?: 'decimal'; placeholder?: string } = {}) => (
    <input className={`field${numeric.includes(key) && bad(v[key]) ? ' field-invalid' : ''}`} type={extra.type ?? 'text'}
      inputMode={extra.inputMode} placeholder={extra.placeholder} aria-label={label}
      value={v[key]} onChange={(e) => setV({ ...v, [key]: e.target.value })} />
  )
  const positionChanged = lat.trim() !== str(loc.lat) || lon.trim() !== str(loc.lon)
  const epsgChanged = epsg.trim() !== str(loc.crs_epsg)
  const positionInvalid = positionChanged && !(Math.abs(num(lat)) <= 90 && Math.abs(num(lon)) <= 180)
  const epsgInvalid = epsgChanged && !Number.isInteger(num(epsg))
  const invalid = numeric.some((k) => bad(v[k])) || positionInvalid || epsgInvalid || (groundwater.trim() !== '' && !(num(groundwater) >= 0))

  async function save() {
    const items: Item[] = (Object.keys(initial) as Key[]).filter((k) => v[k].trim() !== initial[k]).map((k) => ({
      kind: 'field', payload: { field: k, value: v[k].trim() === '' ? null : numeric.includes(k) ? num(v[k]) : v[k].trim() },
    }))
    // A position typed by hand wins over reading the printed coordinates in another system.
    if (positionChanged) items.push({ kind: 'position', payload: { lat: num(lat), lon: num(lon) } })
    else if (epsgChanged) items.push({ kind: 'crs', payload: { epsg: Math.trunc(num(epsg)) }, scope: wholeDoc ? 'document' : 'borehole' })
    if (groundwater.trim() !== '') items.push({ kind: 'groundwater', payload: { depth_m: num(groundwater) } })
    if (await send(items)) onDone()
  }

  return (
    <div className="card card-editing">
      <CardHead id="detail-record" title="Record" edit={{ busy, canSave: !stale && !invalid, onSave: save, onCancel: onDone }} />
      <dl className="record record-edit">
        <Row label="Hole name">{field('borehole_name', 'Hole name')}</Row>
        <Row label="Project">{field('project_name', 'Project')}</Row>
        <Row label="Location">{field('location_name', 'Location')}</Row>
        <dt>Coordinates as printed</dt>
        <dd>{withSuffix(loc.coords_raw ?? '–', loc.crs_raw, (x) => ` (${x})`)}</dd>
        {loc.coords_raw && (
          <Row label="Coordinate system">
            <span className="field-prefix">EPSG</span>
            <input className={`field${epsgInvalid ? ' field-invalid' : ''}`} inputMode="numeric" value={epsg} onChange={(e) => setEpsg(e.target.value)}
              placeholder="e.g. 25832" aria-label="EPSG code of the printed coordinates" disabled={positionChanged} />
            <label className="field-check">
              <input type="checkbox" checked={wholeDoc} onChange={(e) => setWholeDoc(e.target.checked)} disabled={positionChanged} /> whole document
            </label>
          </Row>
        )}
        <Row label="Latitude">
          <input className={`field${positionInvalid || bad(lat) ? ' field-invalid' : ''}`} inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} placeholder="e.g. 48.0" aria-label="Latitude (WGS84)" />
        </Row>
        <Row label="Longitude">
          <input className={`field${positionInvalid || bad(lon) ? ' field-invalid' : ''}`} inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} placeholder="e.g. 7.85" aria-label="Longitude (WGS84)" />
        </Row>
        <Row label="Ground level">
          {field('surface_elevation_m', 'Ground level (m)', { inputMode: 'decimal', placeholder: 'm' })}
          {field('elevation_datum', 'Level datum', { placeholder: 'datum' })}
        </Row>
        <Row label="Final depth">{field('depth_m', 'Final depth (m)', { inputMode: 'decimal', placeholder: 'm' })}</Row>
        <Row label="Started">{field('start_date', 'Started', { type: 'date' })}</Row>
        <Row label="Finished">{field('end_date', 'Finished', { type: 'date' })}</Row>
        <Row label="Add groundwater">
          <input className={`field${bad(groundwater) ? ' field-invalid' : ''}`} inputMode="decimal" value={groundwater}
            onChange={(e) => setGroundwater(e.target.value)} placeholder="m below ground" aria-label="Add groundwater level (m)" />
        </Row>
      </dl>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

const LITH_OPTIONS = Object.entries(LITHOLOGY).map(([key, s]) => ({ key, label: s.label }))

interface LayerDraft {
  key: number
  orig: Layer | null
  top: string
  bottom: string
  primary: string
  description: string
  code: string
  name: string
  age: string
}

const draftOf = (l: Layer, key: number): LayerDraft => ({
  key, orig: l, top: str(l.top_m), bottom: str(l.bottom_m), primary: l.primary, description: l.description_original,
  code: str(l.stratigraphic_code), name: str(l.stratigraphic_name), age: str(l.geologic_age),
})
const layerValid = (d: LayerDraft) => num(d.top) >= 0 && num(d.top) < num(d.bottom)

/** The layer table with every cell editable; rows can be removed and added. */
export function LayersEditor({ borehole, onChange, onDone }: {
  borehole: Borehole
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const [drafts, setDrafts] = useState(() => borehole.layers.map(draftOf))
  const [nextKey, setNextKey] = useState(borehole.layers.length)
  const { send, busy, error, stale } = useCorrection(borehole.borehole_id, onChange)
  const set = (key: number, patch: Partial<LayerDraft>) => setDrafts(drafts.map((d) => (d.key === key ? { ...d, ...patch } : d)))

  function add() {
    const top = Math.max(0, ...drafts.map((d) => num(d.bottom)).filter(Number.isFinite))
    setDrafts([...drafts, { key: nextKey, orig: null, top: str(top), bottom: '', primary: 'unknown', description: '', code: '', name: '', age: '' }])
    setNextKey(nextKey + 1)
  }

  async function save() {
    const kept = new Set(drafts.map((d) => d.orig))
    const orig = (l: Layer) => ({ orig_top: l.top_m, orig_bottom: l.bottom_m })
    const items: Item[] = borehole.layers.filter((l) => !kept.has(l)).map((l) => ({ kind: 'layer', payload: { ...orig(l), delete: true } }))
    for (const d of drafts) {
      if (!d.orig) {
        items.push({ kind: 'layer_add', payload: { top: num(d.top), bottom: num(d.bottom), primary: d.primary, description_original: d.description.trim() || null } })
        continue
      }
      const l = d.orig
      const p: Record<string, unknown> = {}
      if (num(d.top) !== l.top_m || num(d.bottom) !== l.bottom_m) Object.assign(p, { top: num(d.top), bottom: num(d.bottom) })
      if (d.primary !== l.primary) p.primary = d.primary
      if (d.description.trim() !== l.description_original) p.description_original = d.description.trim()
      if (d.code.trim() !== str(l.stratigraphic_code)) p.stratigraphic_code = d.code.trim()
      if (d.name.trim() !== str(l.stratigraphic_name)) p.stratigraphic_name = d.name.trim()
      if (d.age.trim() !== str(l.geologic_age)) p.geologic_age = d.age.trim()
      if (Object.keys(p).length) items.push({ kind: 'layer', payload: { ...orig(l), ...p } })
    }
    if (await send(items)) onDone()
  }

  return (
    <div className="card card-flush card-editing">
      <CardHead id="detail-layers" title="Layers" edit={{ busy, canSave: !stale && drafts.every(layerValid), onSave: save, onCancel: onDone }} />
      <div className="table-scroll">
        <table className="layers layers-edit">
          <thead>
            <tr><th>Depth (m)</th><th>Lithology</th><th>Description as printed</th><th>Stratigraphy</th><th><span className="sr-only">Remove</span></th></tr>
          </thead>
          <tbody>
            {drafts.map((d) => (
              <tr key={d.key}>
                <td>
                  <span className="depth-inputs">
                    <input className={`field field-num${layerValid(d) ? '' : ' field-invalid'}`} inputMode="decimal" value={d.top}
                      onChange={(e) => set(d.key, { top: e.target.value })} aria-label="From (m)" />
                    <span aria-hidden="true">–</span>
                    <input className={`field field-num${layerValid(d) ? '' : ' field-invalid'}`} inputMode="decimal" value={d.bottom}
                      onChange={(e) => set(d.key, { bottom: e.target.value })} aria-label="To (m)" autoFocus={!d.orig} />
                  </span>
                </td>
                <td>
                  <select className="field" value={d.primary} onChange={(e) => set(d.key, { primary: e.target.value })} aria-label="Lithology">
                    {LITH_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
                  </select>
                </td>
                <td><textarea className="field" value={d.description} onChange={(e) => set(d.key, { description: e.target.value })} rows={2} aria-label="Description as printed" /></td>
                <td className="strat-inputs">
                  <input className="field" value={d.code} onChange={(e) => set(d.key, { code: e.target.value })} placeholder="code" aria-label="Stratigraphic code" />
                  <input className="field" value={d.name} onChange={(e) => set(d.key, { name: e.target.value })} placeholder="formation" aria-label="Stratigraphic name" />
                  <input className="field" value={d.age} onChange={(e) => set(d.key, { age: e.target.value })} placeholder="age" aria-label="Geologic age" />
                </td>
                <td>
                  <button className="btn-remove" onClick={() => setDrafts(drafts.filter((x) => x.key !== d.key))}
                    aria-label={`Remove layer ${d.top}–${d.bottom} m`} title="Remove layer">✕</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="btn-add" onClick={add}>+ Add layer</button>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

export interface TestRow {
  test: 'spt' | 'core' | 'lab'
  depth: number
  text: string
  label: string
  parameter?: string
  value: number | null
  rqd?: number | null
}

/** Test results with their values editable in place; rows can be removed. */
export function TestsEditor({ boreholeId, rows, onChange, onDone }: {
  boreholeId: string
  rows: TestRow[]
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const [drafts, setDrafts] = useState(() => rows.map((row, key) => ({ key, row, value: str(row.value), rqd: str(row.rqd) })))
  const { send, busy, error, stale } = useCorrection(boreholeId, onChange)
  type Draft = (typeof drafts)[number]
  const set = (d: Draft, patch: Partial<Draft>) => setDrafts(drafts.map((x) => (x === d ? { ...x, ...patch } : x)))
  const ref = (r: TestRow) => ({ test: r.test, orig_depth: r.depth, ...(r.parameter ? { parameter: r.parameter } : {}) })
  const changed = (d: Draft) => d.value.trim() !== str(d.row.value) || (d.row.test === 'core' && d.rqd.trim() !== str(d.row.rqd))
  const valid = drafts.every((d) => !changed(d) || (Number.isFinite(num(d.value)) && !bad(d.rqd)))

  async function save() {
    const kept = new Set(drafts.map((d) => d.row))
    const items: Item[] = rows.filter((r) => !kept.has(r)).map((r) => ({ kind: 'test', payload: { ...ref(r), delete: true } }))
    for (const d of drafts.filter(changed)) {
      items.push({ kind: 'test', payload: { ...ref(d.row), value: num(d.value), ...(d.row.test === 'core' && d.rqd.trim() !== '' ? { rqd: num(d.rqd) } : {}) } })
    }
    if (await send(items)) onDone()
  }

  return (
    <div className="card card-editing">
      <CardHead title="Tests" edit={{ busy, canSave: !stale && valid, onSave: save, onCancel: onDone }} />
      <ul className="tests tests-edit">
        {drafts.map((d) => (
          <li key={d.key}>
            <span className="num">{fmtNum(d.row.depth)} m</span>
            <span className="tests-label">{d.row.label}</span>
            <input className={`field field-num${bad(d.value) ? ' field-invalid' : ''}`} inputMode="decimal" value={d.value}
              onChange={(e) => set(d, { value: e.target.value })} aria-label={`${d.row.label} at ${d.row.depth} m`} />
            {d.row.test === 'core' && (
              <>
                <span className="tests-label">RQD %</span>
                <input className={`field field-num${bad(d.rqd) ? ' field-invalid' : ''}`} inputMode="decimal" value={d.rqd}
                  onChange={(e) => set(d, { rqd: e.target.value })} aria-label={`RQD % at ${d.row.depth} m`} />
              </>
            )}
            <button className="btn-remove" onClick={() => setDrafts(drafts.filter((x) => x !== d))}
              aria-label={`Remove ${d.row.label} at ${d.row.depth} m`} title="Remove">✕</button>
          </li>
        ))}
      </ul>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

/** Free-text notes of the record. */
export function NotesEditor({ borehole, onChange, onDone }: {
  borehole: Borehole
  onChange: (update: DetailUpdate) => void
  onDone: () => void
}) {
  const [notes, setNotes] = useState(str(borehole.notes))
  const { send, busy, error, stale } = useCorrection(borehole.borehole_id, onChange)
  async function save() {
    const items: Item[] = notes.trim() === str(borehole.notes).trim() ? [] : [{ kind: 'field', payload: { field: 'notes', value: notes.trim() || null } }]
    if (await send(items)) onDone()
  }
  return (
    <div className="card card-editing">
      <CardHead title="Notes" edit={{ busy, canSave: !stale, onSave: save, onCancel: onDone }} />
      <textarea className="field notes-input" value={notes} onChange={(e) => setNotes(e.target.value)} rows={5} aria-label="Notes" autoFocus />
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

function describe(c: CorrectionRecord): string {
  const p = c.payload
  if (c.kind === 'crs') return `Coordinate system set to EPSG:${p.epsg}${c.scope === 'document' ? ' for the whole document' : ''}`
  if (c.kind === 'position') return `Position set to ${p.lat}, ${p.lon}`
  if (c.kind === 'field') return `${words(String(p.field))} set to ${p.value ?? '–'}`
  if (c.kind === 'groundwater') return p.dry ? 'Groundwater: dry' : `Groundwater level ${p.depth_m} m added`
  if (c.kind === 'test') {
    const what = `${words(String(p.parameter ?? p.test))} at ${p.orig_depth} m`
    return p.delete ? `${what} deleted` : `${what} set to ${p.value}${p.rqd != null ? `, RQD ${p.rqd} %` : ''}`
  }
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
