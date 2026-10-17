import { useEffect, useState } from 'react'
import { fetchSimilar } from '../api'
import { STATUS_LABEL, scoreText } from '../review'
import type { SimilarGround } from '../types'

/** Holes of other projects with comparable ground: lithology per depth band and water level, from the stored layers. */
export function SimilarHoles({ boreholeId, onSelect, onFindLike }: {
  boreholeId: string; onSelect: (id: string) => void; onFindLike: (like: NonNullable<SimilarGround['like']>) => void
}) {
  const [result, setResult] = useState<SimilarGround | 'error' | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    fetchSimilar(boreholeId, ctrl.signal).then(setResult).catch(() => { if (!ctrl.signal.aborted) setResult('error') })
    return () => ctrl.abort()
  }, [boreholeId])

  return (
    <div className="card">
      <h3 className="card-head">Similar ground elsewhere</h3>
      {result === null ? <p className="muted card-empty">Comparing…</p>
        : result === 'error' ? <p className="muted card-empty">Could not compare this hole.</p>
        : !result.ground ? <p className="muted card-empty">Too few layers to compare.</p>
        : (
          <>
            <p className="similar-here">
              This hole: {result.ground}
              {result.like && (
                <button className="btn btn-small btn-ghost similar-find" onClick={() => onFindLike(result.like!)}
                  title="Set the ground filters from this hole and show every match">Find more like this</button>
              )}
            </p>
            {result.similar.length === 0 ? <p className="muted card-empty">No comparable hole in other documents.</p> : (
              <ul className="similar">
                {result.similar.map((s) => (
                  <li key={s.id}>
                    <button className="similar-item" onClick={() => onSelect(s.id)}>
                      <span className="similar-name">{s.name}</span>
                      <span className="similar-match num" title="Agreement of the lithology mix per depth band and of the water level">
                        {Math.round(s.similarity * 100)} % alike
                      </span>
                      <span className="similar-place muted">{s.project ?? 'Unnamed project'}{s.country ? `, ${s.country}` : ''}</span>
                      <span className="similar-reason">{s.reason}</span>
                      <span className={`flag flag-${s.review_status}`} title={`Release score ${scoreText(s.score)}`}>
                        {STATUS_LABEL[s.review_status]}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
    </div>
  )
}
