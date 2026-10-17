import { useEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import { lithStyle, patternId } from '../lithology'
import type { Filters, RangeFilter, StatusFilter } from '../types'

const STATUS: [StatusFilter, string][] = [
  ['all', 'All'], ['released', 'Released'], ['pending', 'Waiting for review'], ['rejected', 'Rejected'],
]
const POSITION: [number | null, string][] = [[null, 'Any'], [0.4, 'Medium or better'], [0.7, 'High only']]
const LAB: [string, string, string][] = [
  ['ucs', 'UCS', 'MPa'], ['cai', 'CAI', ''], ['tensile_strength', 'Tensile strength', 'MPa'],
  ['point_load_index', 'Point load Is50', 'MPa'], ['water_content', 'Water content', '%'],
]
const labName = (key: string) => LAB.find(([k]) => k === key)?.[1] ?? key.replaceAll('_', ' ')
const labUnit = (key: string) => LAB.find(([k]) => k === key)?.[2] ?? ''

type Menu = 'status' | 'position' | 'lithology' | 'lab'

const toNumber = (v: string) => {
  const n = Number(v.replace(',', '.'))
  return v.trim() === '' || !Number.isFinite(n) ? null : n
}

/** Decimal input that keeps what is typed ("2.", "1,5") while passing the parsed number on. */
function NumberField({ value, onChange, placeholder, label }: {
  value: number | null; onChange: (v: number | null) => void; placeholder: string; label: string
}) {
  const [text, setText] = useState(value == null ? '' : String(value))
  const [seen, setSeen] = useState(value)
  if (value !== seen) {  // changed from outside, e.g. Reset or browser Back
    setSeen(value)
    if (toNumber(text) !== value) setText(value == null ? '' : String(value))
  }
  return (
    <input inputMode="decimal" value={text} placeholder={placeholder} aria-label={label}
      onChange={(e) => { setText(e.target.value); setSeen(toNumber(e.target.value)); onChange(toNumber(e.target.value)) }} />
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
  /** Name of the document group the filter narrows to: its project, else the group itself. */
  groupLabel: string | null
}

/** Filter pills; each opens a dropdown below the row. */
export function FilterBar({ filters, setFilters, lithologies, groupLabel }: Props) {
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
  const active = filters.status !== 'all' || filters.minPosition != null || liths.length > 0 || lab.length > 0 ||
    filters.group != null

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
      <div className="filter-bar" role="group" aria-label="Filters">
        {pill('status', filters.status === 'all' ? 'Release state' : STATUS.find(([s]) => s === filters.status)![1], filters.status !== 'all')}
        {pill('position', filters.minPosition == null ? 'Position' : `Position: ${POSITION.find(([p]) => p === filters.minPosition)?.[1] ?? filters.minPosition}`, filters.minPosition != null)}
        {lithologies.length > 0 && pill('lithology', liths.length ? summary(liths, lithName, false) : 'Lithology', liths.length > 0)}
        {pill('lab', lab.length ? summary(lab, labName, true) : 'Lab value', lab.length > 0)}
        {filters.group != null && (
          <button className="pill pill-on" onClick={() => set({ group: null })} aria-label={`Show all projects, not only ${groupLabel}`}
            title="Show all projects">
            Project: {groupLabel} <span aria-hidden="true">×</span>
          </button>
        )}
        {active && (
          <button className="pill-reset" onClick={() => {
            set({ status: 'all', minPosition: null, lithology: [], lab: [], group: null })
            setOpen(null)
          }}>Reset</button>
        )}
      </div>

      {open && (
        <div className="filter-menu" id="filter-menu" style={{ top }}>
          {open === 'status' && options(STATUS, filters.status, (status) => set({ status }))}
          {open === 'position' && options(POSITION, filters.minPosition, (minPosition) => set({ minPosition }))}
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
        </div>
      )}
    </div>
  )
}
