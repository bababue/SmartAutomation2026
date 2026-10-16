import type { Feature } from '../types'
import { CoreStrip } from './Profile'

interface Props {
  features: Feature[]
  selectedId: string | null
  onSelect: (id: string) => void
  loading: boolean
}

const fmtDepth = (d: number | null) => (d == null ? '–' : `${d.toLocaleString('en', { maximumFractionDigits: 1 })} m`)

export function ResultList({ features, selectedId, onSelect, loading }: Props) {
  if (!loading && !features.length) {
    return (
      <div className="empty">
        <p>No boreholes match these filters.</p>
        <p className="muted">Clear the search or lower the minimum confidence.</p>
      </div>
    )
  }
  const sorted = [...features].sort(
    (a, b) => (a.properties.project ?? '').localeCompare(b.properties.project ?? '') ||
      a.properties.name.localeCompare(b.properties.name, undefined, { numeric: true }),
  )
  return (
    <ul className="results" aria-busy={loading}>
      {sorted.map(({ properties: p }) => (
        <li key={p.id}>
          <button
            className={`result${p.id === selectedId ? ' result-selected' : ''}`}
            onClick={() => onSelect(p.id)}
            aria-current={p.id === selectedId}
          >
            <span className="result-name">{p.name}</span>
            <span className="result-depth">{fmtDepth(p.depth)}</span>
            <span className="result-place">{p.location_name ?? p.project ?? 'Unnamed site'}{p.country ? `, ${p.country}` : ''}</span>
            <CoreStrip strip={p.strip} depth={p.depth} />
            <span className="result-flags">
              {p.loc_method === 'geocoded_place' && <span className="flag flag-warn" title="Position estimated from the place name">approx. position</span>}
              {p.n_issues > 0 && <span className="flag" title="Plausibility checks found something to look at">{p.n_issues} to check</span>}
              {p.reviewed ? <span className="flag flag-ok">checked</span> : null}
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}
