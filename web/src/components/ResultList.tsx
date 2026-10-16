import type { Feature } from '../types'
import { STATUS_LABEL, scoreText } from '../review'
import { SHOW_SOURCE_FILE } from '../dev'
import { CoreStrip } from './Profile'

interface Props {
  features: Feature[]
  selectedId: string | null
  onSelect: (id: string) => void
  loading: boolean
  sectionIds: string[]
  onToggleSection: (id: string) => void
}

const fmtDepth = (d: number | null) => (d == null ? '–' : `${d.toLocaleString('en', { maximumFractionDigits: 1 })} m`)

export function ResultList({ features, selectedId, onSelect, loading, sectionIds, onToggleSection }: Props) {
  if (!loading && !features.length) {
    return (
      <div className="empty">
        <p>No boreholes match these filters.</p>
        <p className="muted">Clear the search, remove lithology filters or show all release states.</p>
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
        <li key={p.id} className="result-item">
          <button
            className={`result${p.id === selectedId ? ' result-selected' : ''}`}
            onClick={() => onSelect(p.id)}
            aria-current={p.id === selectedId}
          >
            <span className="result-name">{p.name}</span>
            <span className="result-depth">{fmtDepth(p.depth)}</span>
            <span className="result-place">{p.location_name ?? p.project ?? 'Unnamed site'}{p.country ? `, ${p.country}` : ''}</span>
            {SHOW_SOURCE_FILE && p.source_file && <span className="result-place result-file">{p.source_file}</span>}
            <CoreStrip strip={p.strip} depth={p.depth} />
            <span className="result-flags">
              <span className={`flag flag-${p.review_status}`} title={`Release score ${scoreText(p.score)}`}>
                {STATUS_LABEL[p.review_status]} <span className="flag-score">{scoreText(p.score)}</span>
              </span>
              {p.loc_method === 'geocoded_place' && <span className="flag flag-warn" title="Position estimated from the place name">approx. position</span>}
            </span>
          </button>
          <button
            className={`section-toggle${sectionIds.includes(p.id) ? ' section-toggle-on' : ''}`}
            onClick={() => onToggleSection(p.id)}
            aria-pressed={sectionIds.includes(p.id)}
            aria-label={`${sectionIds.includes(p.id) ? 'Remove' : 'Add'} ${p.name} ${sectionIds.includes(p.id) ? 'from' : 'to'} the section`}
            title={sectionIds.includes(p.id) ? 'In the section view' : 'Add to the section view'}
          >
            {sectionIds.includes(p.id) ? 'In section' : '+ Section'}
          </button>
        </li>
      ))}
    </ul>
  )
}
