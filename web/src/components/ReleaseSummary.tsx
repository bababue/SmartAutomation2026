import type { Stats } from '../types'

/** Released and total records in the header, with a way into the records that have no position yet. */
export function ReleaseSummary({ stats, onShowUnplaced }: { stats: Stats; onShowUnplaced: () => void }) {
  return (
    <p className="stats">
      <strong>{(stats.status.auto_approved + stats.status.approved).toLocaleString('en')}/{stats.boreholes.toLocaleString('en')}</strong>{' '}
      (Released/Total) ·{' '}
      {stats.boreholes > stats.located ? <button className="link-button muted" onClick={onShowUnplaced}>
        {(stats.boreholes - stats.located).toLocaleString('en')} without position</button>
        : <span>0 without position</span>}
    </p>
  )
}
