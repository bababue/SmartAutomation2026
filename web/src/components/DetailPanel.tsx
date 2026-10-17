import { useMemo, useRef, useState } from 'react'
import type { Detail, DetailUpdate, Layer, SimilarGround, SourceRef } from '../types'
import { lithStyle, patternId } from '../lithology'
import { CardHead, CorrectionList, LayersEditor, NotesEditor, RecordEditor, TestsEditor, type TestRow } from './Corrections'
import { Profile } from './Profile'
import { SimilarHoles } from './SimilarHoles'
import { ReviewBox } from './ReviewBox'
import { ConfirmCrs } from './ConfirmCrs'
import { withSuffix } from '../review'
import { fmtNum, words } from '../format'

interface Props {
  detail: Detail
  onClose: () => void
  onReviewChange: (update: DetailUpdate) => void
  /** Review mode: opens the next record after a decision. */
  onDecided?: () => void
  /** Review mode: reloads after a document-wide coordinate system confirmation. */
  onCrsConfirmed?: () => void
  onSourceToggle: (open: boolean) => void
  onSelect: (id: string) => void
  onFindLike: (like: NonNullable<SimilarGround['like']>) => void
}

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

export function DetailPanel({ detail, onClose, onReviewChange, onDecided, onCrsConfirmed, onSourceToggle, onSelect, onFindLike }: Props) {
  const bh = detail.borehole
  const loc = bh.location
  const [hovered, setHovered] = useState<number | null>(null)
  const layerRows = useRef<HTMLTableSectionElement>(null)
  const [coordsHovered, setCoordsHovered] = useState(false)
  const [editing, setEditing] = useState<'record' | 'layers' | 'tests' | 'notes' | null>(null)
  const done = () => setEditing(null)
  const [sourceOpen, setSourceOpen] = useState(false)

  // Findings in the order a reviewer needs them: what blocks the release, other checks, then the model's notes.
  const blocking = new Set(detail.blocking ?? [])
  const notes = bh.issues.filter((i) => i.startsWith('unreadable:'))
  const others = bh.issues.filter((i) => !blocking.has(i) && !i.startsWith('unreadable:') && !i.startsWith('corrected by'))  // corrections are listed below
  const [pageIndex, setPageIndex] = useState(0)
  const pages = detail.source_pages
  const page = pages[Math.min(pageIndex, pages.length - 1)]  // the panel is keyed by id; clamp anyway

  // What is hovered, and where it was read: follow it to its page and outline it there.
  const focus: SourceRef | null = coordsHovered ? loc.source : hovered != null ? bh.layers[hovered]?.source ?? null : null
  const followSource = (ref: SourceRef | null | undefined) => {
    const i = ref ? pages.findIndex((p) => p.file === ref.file && p.page === ref.page) : -1
    if (i >= 0) setPageIndex(i)
  }
  const hoverLayer = (i: number | null) => {
    setHovered(i)
    if (i != null) followSource(bh.layers[i]?.source)
  }
  // Hovering the profile brings the matching table row into view; hovering the table itself must not scroll it.
  const hoverProfile = (i: number | null) => {
    hoverLayer(i)
    if (i != null) layerRows.current?.children[i]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }
  const highlight = focus?.bbox && page && focus.file === page.file && focus.page === page.page ? focus.bbox : null

  const tests = useMemo(() => {
    const rows: TestRow[] = []
    bh.spt.forEach((s) => rows.push({ test: 'spt', depth: s.depth_m, value: s.n_value, label: 'SPT N', text: `SPT ${s.refusal ? 'refusal' : `N = ${s.n_value ?? '–'}`}${s.raw ? ` (${s.raw})` : ''}` }))
    bh.core_runs.forEach((c) => rows.push({ test: 'core', depth: c.top_m, value: c.total_core_recovery_pct, rqd: c.rqd_pct, label: 'Core TCR %', text: `Core ${fmtNum(c.top_m)}–${fmtNum(c.bottom_m)} m: TCR ${fmtNum(c.total_core_recovery_pct, 0)} %, RQD ${fmtNum(c.rqd_pct, 0)} %` }))
    bh.lab_results.forEach((l) => rows.push({ test: 'lab', depth: l.top_m ?? 0, parameter: l.parameter, value: l.value, label: words(l.parameter), text: `${words(l.parameter)}: ${fmtNum(l.value, 3)}${l.value_raw ? ` (printed ${l.value_raw})` : ''}` }))
    return rows.sort((a, b) => a.depth - b.depth)
  }, [bh])

  return (
    <aside className="detail" aria-label={`Borehole ${bh.borehole_name}`}>
      <header className="detail-head">
        <div>
          <h2>{bh.borehole_name}</h2>
          <p className="detail-sub">{[bh.project_name, bh.location_name].filter(Boolean).join(', ') || 'No project name on the log'}</p>
        </div>
        <div className="detail-actions">
          <button className="btn btn-icon" onClick={onClose} aria-label="Close details" title="Close (Esc)">✕</button>
        </div>
      </header>
      <ReviewBox detail={detail} onChange={onReviewChange} onDecided={onDecided} findings={<>
        {blocking.size > 0 && (<><h4>Needs a person</h4><ul>{[...blocking].map((i) => <li key={i}>{i}</li>)}</ul></>)}
        {others.length > 0 && (<><h4>Other findings</h4><ul>{others.map((i) => <li key={i}>{i}</li>)}</ul></>)}
        {notes.length > 0 && (
          <details className="model-notes">
            <summary>What the model could not read ({notes.length})</summary>
            <ul>{notes.map((i) => <li key={i}>{i.replace(/^unreadable:\s*/, '')}</li>)}</ul>
          </details>
        )}
      </>}>
        <Level value={bh.confidence} label="Extraction" />
        <span title={`Position ${METHOD_TEXT[loc.method ?? ''] ?? 'unknown'}`}><Level value={loc.confidence} label="Position" /></span>
      </ReviewBox>
      {onCrsConfirmed && detail.crs_group && <ConfirmCrs crs={detail.crs_group} onChanged={onCrsConfirmed} />}

      <div className={`detail-body${sourceOpen ? '' : ' source-closed'}`}>
        <section className="card detail-profile">
          <h3 className="card-head">Profile <span className="muted card-unit">m</span></h3>
          {bh.layers.length ? <Profile borehole={bh} hovered={hovered} onHover={hoverProfile} /> : <p className="muted">No layers extracted.</p>}
        </section>

        <section className="detail-data">
          <CorrectionList items={detail.corrections ?? []} />

          {editing === 'record' ? <RecordEditor borehole={bh} onChange={onReviewChange} onDone={done} /> : (
          <div className="card">
          <CardHead id="detail-record" title="Record" onEdit={editing ? undefined : () => setEditing('record')} />
          <dl className="record">
            <dt>Coordinates as printed</dt>
            <dd onMouseEnter={() => { setCoordsHovered(true); followSource(loc.source) }} onMouseLeave={() => setCoordsHovered(false)}>
              {withSuffix(loc.coords_raw ?? '–', loc.crs_raw, (x) => ` (${x})`)}
            </dd>
            <dt>Position (WGS84)</dt>
            <dd>
              {loc.lat != null ? `${fmtNum(loc.lat, 6)}, ${fmtNum(loc.lon, 6)}` : '–'}
              {loc.crs_epsg ? <span className="muted"> via EPSG:{loc.crs_epsg}</span> : null}
              {loc.crs_candidates.length > 1 && <span className="muted"> (also possible: {loc.crs_candidates.slice(1).join(', ')})</span>}
            </dd>
            <dt>Ground level</dt><dd>{withSuffix(bh.surface_elevation_raw ?? fmtNum(bh.surface_elevation_m), bh.elevation_datum)}</dd>
            <dt>Final depth</dt><dd>{fmtNum(bh.depth_m)} m{bh.inclination_deg != null && bh.inclination_deg !== 90 && Math.abs(bh.inclination_deg) !== 90 ? `, inclined ${bh.inclination_deg}°` : ''}</dd>
            <dt>Drilled</dt><dd>{bh.start_date ?? '–'}{bh.end_date && bh.end_date !== bh.start_date ? ` to ${bh.end_date}` : ''}</dd>
            <dt>Method</dt><dd>{bh.drilling_method ?? '–'}</dd>
            <dt>Client</dt><dd>{bh.client ?? '–'}</dd>
            <dt>Contractor</dt><dd>{bh.contractor ?? '–'}</dd>
            {bh.groundwater.length > 0 && (<><dt>Groundwater</dt><dd>{bh.groundwater.map((g) => g.dry ? 'dry' : `${fmtNum(g.depth_m)} m ${g.reference === 'casing' ? 'below casing ' : ''}(${g.kind})`).join('; ')}</dd></>)}
          </dl>
          </div>
          )}

          {editing === 'layers' ? <LayersEditor borehole={bh} onChange={onReviewChange} onDone={done} /> : (
          <div className="card card-flush">
          <CardHead id="detail-layers" title="Layers" onEdit={editing ? undefined : () => setEditing('layers')} />
          <div className="table-scroll">
          <table className="layers">
            <thead>
              <tr><th>Depth</th><th>Lithology</th><th>Description</th><th>Stratigraphy</th></tr>
            </thead>
            <tbody ref={layerRows}>
              {bh.layers.map((l: Layer, i) => (
                <tr key={i} className={hovered === i ? 'row-active' : undefined} onMouseEnter={() => hoverLayer(i)} onMouseLeave={() => setHovered(null)}>
                  <td className="num depth">{fmtNum(l.top_m)}–{fmtNum(l.bottom_m)}</td>
                  <td>
                    <svg width={14} height={14} className="swatch" aria-hidden="true"><rect width={14} height={14} fill={`url(#${patternId(l.primary)})`} /></svg>
                    {lithStyle(l.primary).label}
                  </td>
                  <td>
                    {l.description_en ?? l.description_original}
                    {l.description_en && <span className="original">{l.description_original}</span>}
                  </td>
                  <td>{[l.stratigraphic_code, l.stratigraphic_name, l.geologic_age].filter(Boolean).join(', ') || '–'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          {!bh.layers.length && <p className="muted card-empty">No layers extracted. Use “Edit” to enter them from the source page.</p>}
          </div>
          )}

          {tests.length > 0 && (editing === 'tests' ? <TestsEditor boreholeId={bh.borehole_id} rows={tests} onChange={onReviewChange} onDone={done} /> : (
            <div className="card">
              <CardHead title="Tests" onEdit={editing ? undefined : () => setEditing('tests')} />
              <ul className="tests">{tests.map((t, i) => <li key={i}><span className="num">{fmtNum(t.depth)} m</span> {t.text}</li>)}</ul>
            </div>
          ))}
          {editing === 'notes' ? <NotesEditor borehole={bh} onChange={onReviewChange} onDone={done} /> : (
            <div className="card">
              <CardHead title="Notes" onEdit={editing ? undefined : () => setEditing('notes')} />
              <p className="notes">{bh.notes ?? <span className="muted">No notes.</span>}</p>
            </div>
          )}
          <SimilarHoles boreholeId={bh.borehole_id} onSelect={onSelect} onFindLike={onFindLike} />
        </section>

        {sourceOpen && (
        <section className="card detail-source" id="detail-source">
          <div className="card-head">
            <h3 className="source-title">Source</h3>
            {pages.length > 0 && (
              <div className="page-tabs" role="tablist">
                {pages.map((p, i) => (
                  <button key={`${p.file}-${p.page}`} role="tab" aria-selected={i === pageIndex} className={`page-tab${i === pageIndex ? ' page-tab-on' : ''}`} onClick={() => setPageIndex(i)}>
                    p. {p.page}
                  </button>
                ))}
              </div>
            )}
          </div>
          {pages.length ? (
            <>
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
              <p className="muted source-hint">
                {hovered != null && bh.layers[hovered]
                  ? <>Selected: <strong>{lithStyle(bh.layers[hovered].primary).label}</strong> · {fmtNum(bh.layers[hovered].top_m)}–{fmtNum(bh.layers[hovered].bottom_m)} m{!highlight && ' · not located on this page'}</>
                  : 'Hover a layer or the coordinates to outline where they were read.'}
              </p>
            </>
          ) : (
            <p className="muted">No source page stored for this record.</p>
          )}
        </section>
        )}
      </div>
      <button className="edge-tab source-toggle" onClick={() => { setSourceOpen(!sourceOpen); onSourceToggle(!sourceOpen) }} aria-expanded={sourceOpen} aria-controls="detail-source"
        title={sourceOpen ? 'Hide the source page' : 'Show the source page'}>
        {sourceOpen ? '›' : '‹'} <span>Source</span>
      </button>
    </aside>
  )
}
