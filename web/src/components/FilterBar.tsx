import { useEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import { parseDecimal, words } from '../format'
import { lithStyle, patternId } from '../lithology'
import { SEVERITY_LABEL } from '../review'
import { DEFAULT_FILTERS, hasFilters } from '../url'
import type { FindingFacet, Filters, QueuePosition, RangeFilter, ReviewFilters, StatusFilter } from '../types'

const STATUS: [StatusFilter, string][] = [
  ['all', 'All'], ['released', 'Released'], ['pending', 'Waiting for review'], ['rejected', 'Rejected'],
]
const POSITION: [QueuePosition | null, string][] = [
  [null, 'Any'], ['printed', 'From printed coordinates'], ['estimated', 'Estimated from the place name'],
  ['crs_inferred', 'Coordinate system inferred'], ['none', 'No position'],
]
const SEVERITIES = ['blocking', 'needs_look', 'warning'] as const
// Findings the position filter already covers.
const POSITION_FINDINGS = new Set(['crs_inferred', 'position_from_place', 'not_georeferenced'])
const LAB: [string, string, string][] = [
  ['ucs', 'UCS', 'MPa'], ['cai', 'CAI', ''], ['tensile_strength', 'Tensile strength', 'MPa'],
  ['point_load_index', 'Point load Is50', 'MPa'], ['water_content', 'Water content', '%'],
]
const labName = (key: string) => LAB.find(([k]) => k === key)?.[1] ?? words(key)
const labUnit = (key: string) => LAB.find(([k]) => k === key)?.[2] ?? ''

type Menu = 'status' | 'position' | 'findings' | 'lithology' | 'lab' | 'ground'

/** Material groups of the ground search (api/similar.py). */
const MATERIALS = ['made ground', 'gravel', 'sand', 'silt', 'clay', 'organic', 'rock']
const GROUND_PRESETS: [string, Partial<Filters>][] = [
  ['Shallow rock (rock head ≤ 10 m)', { rockMax: 10, waterMax: null, material: [] }],
  ['Clay over rock', { rockMax: 30, waterMax: null, material: ['clay:0:10:3'] }],
  ['Sand in the top 20 m (≥ 5 m)', { rockMax: null, waterMax: null, material: ['sand:0:20:5'] }],
  ['High groundwater (≤ 3 m)', { rockMax: null, waterMax: 3, material: [] }],
]

function parseMaterial(m: string) {
  const [group, from, to, metres] = m.split(':')
  return { group, from: Number(from), to: Number(to), metres: Number(metres) }
}
const materialText = (m: string) => {
  const { group, from, to, metres } = parseMaterial(m)
  return `${group[0].toUpperCase()}${group.slice(1)} ≥ ${metres} m in ${from}–${to} m`
}

/** Decimal input that keeps what is typed ("2.", "1,5") while passing the parsed number on. */
function NumberField({ value, onChange, placeholder, label }: {
  value: number | null; onChange: (v: number | null) => void; placeholder: string; label: string
}) {
  const [text, setText] = useState(value == null ? '' : String(value))
  const [seen, setSeen] = useState(value)
  if (value !== seen) {  // changed from outside, e.g. Reset or browser Back
    setSeen(value)
    if (parseDecimal(text) !== value) setText(value == null ? '' : String(value))
  }
  return (
    <input inputMode="decimal" value={text} placeholder={placeholder} aria-label={label}
      onChange={(e) => { setText(e.target.value); setSeen(parseDecimal(e.target.value)); onChange(parseDecimal(e.target.value)) }} />
  )
}

const range = (r: RangeFilter) =>
  r.min != null && r.max != null ? ` ${r.min}–${r.max}` : r.min != null ? ` ≥ ${r.min}` : r.max != null ? ` ≤ ${r.max}` : ''

/** Pill text for a list of conditions: the first one, then "+n" for the rest. */
function summary(items: RangeFilter[], name: (key: string) => string, withRange: boolean) {
  const [first] = items
  return `${name(first.key)}${withRange ? range(first) : ''}${items.length > 1 ? ` +${items.length - 1}` : ''}`
}

const toggle = (items: RangeFilter[], key: string) =>
  items.some((i) => i.key === key) ? items.filter((i) => i.key !== key) : [...items, { key, min: null, max: null }]

/** One min/max row per chosen condition. */
function RangeRows({ items, label, unit, onChange }: {
  items: RangeFilter[]; label: (key: string) => ReactNode; unit: (key: string) => string; onChange: (items: RangeFilter[]) => void
}) {
  if (!items.length) return null
  const update = (key: string, patch: Partial<RangeFilter>) => onChange(items.map((i) => (i.key === key ? { ...i, ...patch } : i)))
  return (
    <table className="range-rows">
      <thead><tr><th /><th>Min</th><th>Max</th><th /></tr></thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.key}>
            <th scope="row">{label(i.key)}{unit(i.key) && <span className="muted"> ({unit(i.key)})</span>}</th>
            <td><NumberField placeholder="any" value={i.min} label={`Minimum for ${i.key}`} onChange={(min) => update(i.key, { min })} /></td>
            <td><NumberField placeholder="any" value={i.max} label={`Maximum for ${i.key}`} onChange={(max) => update(i.key, { max })} /></td>
            <td><button className="btn btn-small btn-icon" aria-label={`Remove ${i.key}`} title="Remove"
              onClick={() => onChange(items.filter((x) => x.key !== i.key))}>✕</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

interface Props {
  filters: Filters
  setFilters: Dispatch<SetStateAction<Filters>>
  /** Lithology keys with match counts, most frequent first. */
  lithologies: [string, number][]
  /** Review mode: only the filters of the waiting records, by position source and finding. */
  review?: { filters: ReviewFilters; onChange: (filters: ReviewFilters) => void; findings: FindingFacet[] }
}

/** Filter pills; each opens a dropdown below the row. */
export function FilterBar({ filters, setFilters, lithologies, review }: Props) {
  const [open, setOpen] = useState<Menu | null>(null)
  const [top, setTop] = useState(0)
  const root = useRef<HTMLDivElement>(null)
  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(null) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(null) } }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey, true)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey, true) }
  }, [open])

  const { lithology: liths, lab } = filters
  const lithName = (key: string) => lithStyle(key).label
  const ground = filters.rockMax != null || filters.waterMax != null || filters.material.length > 0
  const active = hasFilters({ ...filters, q: '' })
  const scored = filters.scoreMin != null || filters.scoreMax != null
  const statusLabel = filters.status === 'all' ? (scored ? 'Score' : 'Release state') : STATUS.find(([s]) => s === filters.status)![1]
  const scoreLabel = scored ? range({ key: '', min: filters.scoreMin, max: filters.scoreMax }) : ''
  const rf = review?.filters
  const setReview = (patch: Partial<ReviewFilters>) => review?.onChange({ ...review.filters, ...patch })
  const findings = review?.findings.filter((f) => !POSITION_FINDINGS.has(f.code)) ?? []
  const findingLabel = (f: string) => (f === 'blocking' || f === 'needs_look' ? `Any ${SEVERITY_LABEL[f].toLowerCase()}` : words(f))
  const [draft, setDraft] = useState({ group: 'sand', from: 0, to: 20, metres: 5 })

  function pill(menu: Menu, label: string, on: boolean) {
    return (
      <button className={`pill${on ? ' pill-on' : ''}${open === menu ? ' pill-open' : ''}`} aria-expanded={open === menu}
        aria-controls="filter-menu" onClick={(e) => {
          const btn = e.currentTarget
          setTop(btn.offsetTop + btn.offsetHeight + 6)
          setOpen(open === menu ? null : menu)
        }}>
        {label}
        <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 3.5L5 7l3.5-3.5" /></svg>
      </button>
    )
  }

  function options<T>(items: [T, string][], value: T, onPick: (v: T) => void): ReactNode {
    return (
      <ul className="menu-options" role="listbox">
        {items.map(([v, label]) => (
          <li key={label}>
            <button role="option" aria-selected={v === value} className={`menu-option${v === value ? ' menu-option-on' : ''}`}
              onClick={() => { onPick(v); setOpen(null) }}>
              {label}
            </button>
          </li>
        ))}
      </ul>
    )
  }

  const swatch = (key: string) => (
    <svg width={12} height={12} aria-hidden="true"><rect width={12} height={12} fill={`url(#${patternId(key)})`} /></svg>
  )

  return (
    <div className="filters" ref={root}>
      {rf ? (
      <div className="filter-bar" role="group" aria-label="Review filters">
        {pill('position', rf.position == null ? 'Position' : POSITION.find(([p]) => p === rf.position)![1], rf.position != null)}
        {pill('findings', rf.finding ? findingLabel(rf.finding) : 'Findings', rf.finding != null)}
        {(rf.position != null || rf.finding != null) && (
          <button className="pill-reset" onClick={() => { setReview({ position: null, finding: null }); setOpen(null) }}>Reset</button>
        )}
      </div>
      ) : (<>
      <div className="filter-bar" role="group" aria-label="Filters">
        {pill('status', `${statusLabel}${scoreLabel}`, filters.status !== 'all' || scored)}
        {lithologies.length > 0 && pill('lithology', liths.length ? summary(liths, lithName, false) : 'Lithology', liths.length > 0)}
        {pill('lab', lab.length ? summary(lab, labName, true) : 'Lab value', lab.length > 0)}
        {pill('ground', 'Ground', ground)}
        {filters.rockMax != null && (
          <button className="pill pill-on" onClick={() => set({ rockMax: null })} aria-label={`Remove rock head at most ${filters.rockMax} m`}>
            Rock head ≤ {filters.rockMax} m <span aria-hidden="true">×</span>
          </button>
        )}
        {filters.waterMax != null && (
          <button className="pill pill-on" onClick={() => set({ waterMax: null })} aria-label={`Remove groundwater at most ${filters.waterMax} m`}>
            Groundwater ≤ {filters.waterMax} m <span aria-hidden="true">×</span>
          </button>
        )}
        {filters.material.map((m) => (
          <button key={m} className="pill pill-on" onClick={() => set({ material: filters.material.filter((x) => x !== m) })}
            aria-label={`Remove ${materialText(m)}`}>
            {materialText(m)} <span aria-hidden="true">×</span>
          </button>
        ))}
        {active && (
          <button className="pill-reset" onClick={() => {
            setFilters((f) => ({ ...DEFAULT_FILTERS, q: f.q }))
            setOpen(null)
          }}>Reset</button>
        )}
      </div>
      </>)}

      {open && (
        <div className="filter-menu" id="filter-menu" style={{ top }}>
          {open === 'status' && (
            <>
              {options(STATUS, filters.status, (status) => set({ status }))}
              <label className="ground-field">Release score
                <NumberField placeholder="0" value={filters.scoreMin} label="Lowest release score" onChange={(v) => set({ scoreMin: v })} />–
                <NumberField placeholder="100" value={filters.scoreMax} label="Highest release score" onChange={(v) => set({ scoreMax: v })} />
              </label>
              <p className="menu-note">Score 0–100; records without a score fail any bound.</p>
            </>
          )}
          {open === 'position' && rf && options(POSITION, rf.position, (position) => setReview({ position }))}
          {open === 'findings' && rf && (
            <ul className="menu-options menu-scroll" role="listbox" aria-label="Findings">
              {([[null, 'Any'], ['blocking', 'Any blocking finding'], ['needs_look', 'Any finding that needs a look']] as const).map(([v, label]) => (
                <li key={label}>
                  <button role="option" aria-selected={rf.finding === v} className={`menu-option${rf.finding === v ? ' menu-option-on' : ''}`}
                    onClick={() => { setReview({ finding: v }); setOpen(null) }}>{label}</button>
                </li>
              ))}
              {SEVERITIES.map((sev) => findings.filter((f) => f.severity === sev).map((f, i) => (
                <li key={f.code}>
                  {i === 0 && <p className="menu-note menu-heading">{SEVERITY_LABEL[sev]}</p>}
                  <button role="option" aria-selected={rf.finding === f.code}
                    className={`menu-option${rf.finding === f.code ? ' menu-option-on' : ''}`}
                    onClick={() => { setReview({ finding: f.code }); setOpen(null) }}>
                    {words(f.code)} <span className="chip-n">{f.records.toLocaleString('en')}</span>
                  </button>
                </li>
              )))}
            </ul>
          )}
          {open === 'lithology' && (
            <>
              <div className="chips" role="group" aria-label="Lithologies">
                {lithologies.map(([key, n]) => {
                  const on = liths.some((l) => l.key === key)
                  return (
                    <button key={key} className={`chip${on ? ' chip-on' : ''}`} aria-pressed={on} onClick={() => set({ lithology: toggle(liths, key) })}>
                      {swatch(key)}{lithName(key)}<span className="chip-n">{n}</span>
                    </button>
                  )
                })}
              </div>
              {liths.length > 0 && <p className="menu-note">Depth range per lithology, in m. All chosen lithologies must occur.</p>}
              <RangeRows items={liths} label={(key) => <>{swatch(key)} {lithName(key)}</>} unit={() => ''} onChange={(lithology) => set({ lithology })} />
            </>
          )}
          {open === 'lab' && (
            <>
              <ul className="menu-options" aria-label="Lab parameters">
                {LAB.map(([key, label, unit]) => {
                  const on = lab.some((l) => l.key === key)
                  return (
                    <li key={key}>
                      <button aria-pressed={on} className={`menu-option${on ? ' menu-option-on' : ''}`} onClick={() => set({ lab: toggle(lab, key) })}>
                        {label}{unit && <span className="muted"> ({unit})</span>}
                      </button>
                    </li>
                  )
                })}
              </ul>
              {lab.length > 0 && <p className="menu-note">All chosen values must be measured within their range.</p>}
              <RangeRows items={lab} label={labName} unit={labUnit} onChange={(next) => set({ lab: next })} />
            </>
          )}
          {open === 'ground' && (
            <div className="ground-menu">
              <ul className="menu-options" aria-label="Ground presets">
                {GROUND_PRESETS.map(([label, patch]) => (
                  <li key={label}><button className="menu-option" onClick={() => { set(patch); setOpen(null) }}>{label}</button></li>
                ))}
              </ul>
              <label className="ground-field">Rock head at most
                <NumberField placeholder="any" value={filters.rockMax} label="Rock head at most (m)" onChange={(rockMax) => set({ rockMax })} /> m
              </label>
              <label className="ground-field">Groundwater at most
                <NumberField placeholder="any" value={filters.waterMax} label="Groundwater at most (m)" onChange={(waterMax) => set({ waterMax })} /> m
              </label>
              <div className="ground-field" role="group" aria-label="Material condition">
                At least
                <NumberField placeholder="m" value={draft.metres} label="Metres at least" onChange={(v) => setDraft({ ...draft, metres: v ?? 0 })} /> m
                <select value={draft.group} aria-label="Material" onChange={(e) => setDraft({ ...draft, group: e.target.value })}>
                  {MATERIALS.map((g) => <option key={g} value={g}>{g}</option>)}
                </select>
                in
                <NumberField placeholder="from" value={draft.from} label="From depth (m)" onChange={(v) => setDraft({ ...draft, from: v ?? 0 })} />–
                <NumberField placeholder="to" value={draft.to} label="To depth (m)" onChange={(v) => setDraft({ ...draft, to: v ?? 0 })} /> m
                <button className="btn btn-small" disabled={!(draft.metres > 0 && draft.to > draft.from && draft.from >= 0)}
                  onClick={() => {
                    const m = `${draft.group}:${draft.from}:${draft.to}:${draft.metres}`
                    if (!filters.material.includes(m)) set({ material: [...filters.material, m] })
                  }}>Add</button>
              </div>
              <p className="menu-note">Holes without a rock head or water level do not match a bound on it; materials count only where the log describes them.</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
