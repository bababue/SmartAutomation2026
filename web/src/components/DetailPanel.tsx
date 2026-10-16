import { useEffect, useMemo, useState } from 'react'
import type { Detail, DetailUpdate, Layer, SourceRef } from '../types'
import { SHOW_SOURCE_FILE } from '../dev'
import { lithStyle, patternId } from '../lithology'
import { CorrectionList, LayerEditor, PositionCorrection } from './Corrections'
import { Profile } from './Profile'
import { ReviewBox, type QueueNav } from './ReviewBox'

interface Props {
  detail: Detail
  onClose: () => void
  onReviewChange: (update: DetailUpdate) => void
  nav?: QueueNav | null
  inSection?: boolean
  onToggleSection?: () => void
}

const fmt = (v: number | null | undefined, digits = 2) =>
  v == null ? '–' : v.toLocaleString('en', { maximumFractionDigits: digits })

const METHOD_TEXT: Record<string, string> = {
  stated_coordinates: 'from coordinates printed on the log',
  linked_table: 'from the coordinate table in the same report',
  geocoded_place: 'approximate, estimated from the place name',
  manual: 'set manually',
}

function Level({ value, label }: { value: number | null; label: string }) {
  const level = value == null ? 'none' : value >= 0.7 ? 'high' : value >= 0.4 ? 'medium' : 'low'
  return (
    <span className={`level level-${level}`} title={value == null ? undefined : `${Math.round(value * 100)} %`}>
      <span className="level-dot" />
      {label}: {level === 'none' ? 'unknown' : level}
    </span>
  )
}

export function DetailPanel({ detail, onClose, onReviewChange, nav, inSection, onToggleSection }: Props) {
  const bh = detail.borehole
  const loc = bh.location
  const [hovered, setHovered] = useState<number | null>(null)
  const [coordsHovered, setCoordsHovered] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [pageIndex, setPageIndex] = useState(0)
  const pages = detail.source_pages
  const page = pages[Math.min(pageIndex, pages.length - 1)]  // the panel is keyed by id; clamp anyway

  // What is hovered, and where it was read: follow it to its page and outline it there.
  const focus: SourceRef | null = coordsHovered ? loc.source : hovered != null ? bh.layers[hovered]?.source ?? null : null
  useEffect(() => {
    if (!focus) return
    const i = pages.findIndex((p) => p.file === focus.file && p.page === focus.page)
    if (i >= 0) setPageIndex(i)
  }, [focus, pages])
  const highlight = focus?.bbox && page && focus.file === page.file && focus.page === page.page ? focus.bbox : null

  // 'report.pdf p. 1, 2' per source file
  const sourceFiles = useMemo(() => {
    const byFile = new Map<string, number[]>()
    bh.sources.forEach((s) => byFile.set(s.file, [...(byFile.get(s.file) ?? []), ...(s.page != null ? [s.page] : [])]))
    return [...byFile].map(([file, ps]) => (ps.length ? `${file} p. ${[...new Set(ps)].sort((a, b) => a - b).join(', ')}` : file)).join('; ')
  }, [bh.sources])

  const tests = useMemo(() => {
    const rows: { depth: number; text: string }[] = []
    bh.spt.forEach((s) => rows.push({ depth: s.depth_m, text: `SPT ${s.refusal ? 'refusal' : `N = ${s.n_value ?? '–'}`}${s.raw ? ` (${s.raw})` : ''}` }))
    bh.core_runs.forEach((c) => rows.push({ depth: c.top_m, text: `Core ${fmt(c.top_m)}–${fmt(c.bottom_m)} m: TCR ${fmt(c.total_core_recovery_pct, 0)} %, RQD ${fmt(c.rqd_pct, 0)} %` }))
    bh.lab_results.forEach((l) => rows.push({ depth: l.top_m ?? 0, text: `${l.parameter.replaceAll('_', ' ')}: ${fmt(l.value, 3)}${l.value_raw ? ` (printed ${l.value_raw})` : ''}` }))
    return rows.sort((a, b) => a.depth - b.depth)
  }, [bh])

  return (
    <aside className="detail" aria-label={`Borehole ${bh.borehole_name}`}>
      <header className="detail-head">
        <div>
          <h2>{bh.borehole_name}</h2>
          <p className="detail-sub">{[bh.project_name, bh.location_name].filter(Boolean).join(', ') || 'No project name on the log'}</p>
          {SHOW_SOURCE_FILE && sourceFiles && <p className="detail-sub result-file">{sourceFiles}</p>}
        </div>
        <div className="detail-actions">
          {nav && (
            <div className="queue-nav" aria-label="Review queue position">
              <button className="btn btn-icon" onClick={nav.prev ?? undefined} disabled={!nav.prev} aria-label="Previous in queue" title="Previous (←)">‹</button>
              <span className="muted">{nav.position} of {nav.total} waiting</span>
              <button className="btn btn-icon" onClick={nav.next ?? undefined} disabled={!nav.next} aria-label="Next in queue" title="Next (→)">›</button>
            </div>
          )}
          {onToggleSection && (
            <button className={`btn${inSection ? ' btn-on' : ''}`} onClick={onToggleSection} aria-pressed={inSection}>
              {inSection ? 'In section' : 'Add to section'}
            </button>
          )}
          <button className="btn btn-icon" onClick={onClose} aria-label="Close details" title="Close (Esc)">✕</button>
        </div>
      </header>
      <div className="detail-levels">
        <Level value={bh.confidence} label="Extraction" />
        <Level value={loc.confidence} label="Position" />
        <span className="muted">Position {METHOD_TEXT[loc.method ?? ''] ?? 'unknown'}</span>
      </div>

      <div className="detail-body">
        <section className="detail-profile">
          {bh.layers.length ? <Profile borehole={bh} hovered={hovered} onHover={setHovered} /> : <p className="muted">No layers extracted.</p>}
        </section>

        <section className="detail-data">
          <ReviewBox detail={detail} onChange={onReviewChange} nav={nav} />
          {bh.issues.length > 0 && (
            <div className="checks">
              <h3>Plausibility findings</h3>
              <ul>{bh.issues.map((i) => <li key={i}>{i}</li>)}</ul>
            </div>
          )}
          <CorrectionList items={detail.corrections ?? []} />

          <h3>Record</h3>
          <dl className="record">
            <dt>Coordinates as printed</dt>
            <dd onMouseEnter={() => setCoordsHovered(true)} onMouseLeave={() => setCoordsHovered(false)}>
              {loc.coords_raw ?? '–'}{loc.crs_raw ? ` (${loc.crs_raw})` : ''}
            </dd>
            <dt>Position (WGS84)</dt>
            <dd>
              {loc.lat != null ? `${fmt(loc.lat, 6)}, ${fmt(loc.lon, 6)}` : '–'}
              {loc.crs_epsg ? <span className="muted"> via EPSG:{loc.crs_epsg}</span> : null}
              {loc.crs_candidates.length > 1 && <span className="muted"> (also possible: {loc.crs_candidates.slice(1).join(', ')})</span>}
              <PositionCorrection boreholeId={bh.borehole_id} hasCoords={!!loc.coords_raw} currentEpsg={loc.crs_epsg} onChange={onReviewChange} />
            </dd>
            <dt>Ground level</dt><dd>{bh.surface_elevation_raw ?? fmt(bh.surface_elevation_m)}{bh.elevation_datum ? ` ${bh.elevation_datum}` : ''}</dd>
            <dt>Final depth</dt><dd>{fmt(bh.depth_m)} m{bh.inclination_deg != null && bh.inclination_deg !== 90 && Math.abs(bh.inclination_deg) !== 90 ? `, inclined ${bh.inclination_deg}°` : ''}</dd>
            <dt>Drilled</dt><dd>{bh.start_date ?? '–'}{bh.end_date && bh.end_date !== bh.start_date ? ` to ${bh.end_date}` : ''}</dd>
            <dt>Method</dt><dd>{bh.drilling_method ?? '–'}</dd>
            <dt>Client</dt><dd>{bh.client ?? '–'}</dd>
            <dt>Contractor</dt><dd>{bh.contractor ?? '–'}</dd>
            {bh.groundwater.length > 0 && (<><dt>Groundwater</dt><dd>{bh.groundwater.map((g) => g.dry ? 'dry' : `${fmt(g.depth_m)} m ${g.reference === 'casing' ? 'below casing ' : ''}(${g.kind})`).join('; ')}</dd></>)}
          </dl>

          <h3>Layers</h3>
          <table className="layers">
            <thead>
              <tr><th>From</th><th>To</th><th>Lithology</th><th>Description</th><th>Stratigraphy</th><th><span className="sr-only">Edit</span></th></tr>
            </thead>
            <tbody>
              {bh.layers.map((l: Layer, i) => editing === i ? (
                <LayerEditor key={`edit-${i}`} boreholeId={bh.borehole_id} layer={l} onChange={onReviewChange} onDone={() => setEditing(null)} />
              ) : (
                <tr key={i} className={hovered === i ? 'row-active' : undefined} onMouseEnter={() => setHovered(i)} onMouseLeave={() => setHovered(null)}>
                  <td className="num">{fmt(l.top_m)}</td>
                  <td className="num">{fmt(l.bottom_m)}</td>
                  <td>
                    <svg width={14} height={14} className="swatch" aria-hidden="true"><rect width={14} height={14} fill={`url(#${patternId(l.primary)})`} /></svg>
                    {lithStyle(l.primary).label}
                  </td>
                  <td>
                    {l.description_en ?? l.description_original}
                    {l.description_en && <span className="original">{l.description_original}</span>}
                  </td>
                  <td>{[l.stratigraphic_code, l.stratigraphic_name, l.geologic_age].filter(Boolean).join(', ') || '–'}</td>
                  <td>
                    <button className="btn btn-small btn-edit" onClick={() => setEditing(i)} disabled={editing != null}
                      aria-label={`Correct layer ${fmt(l.top_m)}–${fmt(l.bottom_m)} m`} title="Correct this layer">Edit</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {tests.length > 0 && (
            <>
              <h3>Tests</h3>
              <ul className="tests">{tests.map((t, i) => <li key={i}><span className="num">{fmt(t.depth)} m</span> {t.text}</li>)}</ul>
            </>
          )}
          {bh.notes && (<><h3>Notes</h3><p className="notes">{bh.notes}</p></>)}
        </section>

        <section className="detail-source">
          <h3>Source</h3>
          {pages.length ? (
            <>
              <div className="page-tabs" role="tablist">
                {pages.map((p, i) => (
                  <button key={`${p.file}-${p.page}`} role="tab" aria-selected={i === pageIndex} className={`page-tab${i === pageIndex ? ' page-tab-on' : ''}`} onClick={() => setPageIndex(i)}>
                    p. {p.page}
                  </button>
                ))}
              </div>
              <p className="muted source-file">{page.file}</p>
              <a href={page.url} target="_blank" rel="noreferrer" className="source-link">
                <img src={page.url} alt={`Original page ${page.page} of ${page.file}`} />
                {highlight && (
                  <span
                    className="source-highlight"
                    title="Approximate position read by the model"
                    style={{
                      left: `${highlight[0] * 100}%`, top: `${highlight[1] * 100}%`,
                      width: `${(highlight[2] - highlight[0]) * 100}%`, height: `${(highlight[3] - highlight[1]) * 100}%`,
                    }}
                  />
                )}
              </a>
              <p className="muted source-hint">Hover a layer or the coordinates to outline where they were read.</p>
            </>
          ) : (
            <p className="muted">No source page stored for this record.</p>
          )}
        </section>
      </div>
    </aside>
  )
}
