import { useCallback, useEffect, useState } from 'react'
import { deleteKnowledge, fetchDetail, fetchKnowledge, postKnowledge } from '../api'
import { LITHOLOGY, lithStyle } from '../lithology'
import { fmtDateTime, loadReviewer } from '../review'
import type { DetailUpdate, KnowledgeItem, KnowledgeList, KnowledgeScope } from '../types'

const SCOPE_LABEL: Record<KnowledgeScope, string> = {
  document: 'this document', project: 'this project', region: 'this country', global: 'everywhere',
}

function scopeText(k: KnowledgeItem): string {
  if (k.scope_type === 'global') return 'everywhere'
  if (k.scope_type === 'region') return `country ${k.scope_value}`
  return `${k.scope_type} ${k.scope_value}`
}

function what(k: KnowledgeItem): string {
  if (k.kind === 'crs') return `Coordinate system EPSG:${k.payload.epsg}`
  if (k.kind === 'term') return `“${k.payload.term}” → ${lithStyle(String(k.payload.primary)).label}`
  return k.payload.text ?? ''
}

function origin(k: KnowledgeItem): string {
  if (k.source === 'derived') return `learned from ${k.confirmations} confirmation${k.confirmations === 1 ? '' : 's'}`
  if (k.source === 'external') return `told by someone${k.note ? `: ${k.note}` : ''}`
  return `from the documents${k.note ? `: ${k.note}` : ''}`
}

/** Waits for the background re-assembly, then reloads the open record so learned changes show. */
async function waitForRebuild(boreholeId: string, onChange: (update: DetailUpdate) => void) {
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    const status = await fetchKnowledge(boreholeId).catch(() => null)
    if (status && !status.rebuild.running) break
  }
  onChange(await fetchDetail(boreholeId))
}

/** What is known for this record beyond its own data: learned, from the documents, or told by someone. */
export function KnowledgePanel({ boreholeId, onChange }: { boreholeId: string; onChange: (update: DetailUpdate) => void }) {
  const [data, setData] = useState<KnowledgeList | null>(null)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [kind, setKind] = useState<KnowledgeItem['kind']>('note')
  const [scope, setScope] = useState<KnowledgeScope>('document')
  const [source, setSource] = useState<'review' | 'external'>('external')
  const [text, setText] = useState('')
  const [term, setTerm] = useState('')
  const [primary, setPrimary] = useState('unknown')
  const [epsg, setEpsg] = useState('')
  const [note, setNote] = useState('')

  const load = useCallback(() => fetchKnowledge(boreholeId).then(setData).catch(() => setData(null)), [boreholeId])
  useEffect(() => { load() }, [load])

  async function after(rebuilding: number) {
    await load()
    if (rebuilding) {
      setBusy(`Applying to ${rebuilding} document${rebuilding === 1 ? '' : 's'}…`)
      await waitForRebuild(boreholeId, onChange)
      await load()
    }
    setBusy(null)
  }

  async function save() {
    const reviewer = loadReviewer().trim()
    if (!reviewer) { setError('Enter your name in the release box first.'); return }
    const payload = kind === 'crs' ? { epsg: Number(epsg) } : kind === 'term' ? { term: term.trim(), primary } : { text: text.trim() }
    setError(null)
    setBusy('Saving…')
    try {
      const r = await postKnowledge({ borehole_id: boreholeId, scope_type: scope, kind, payload, source, note: note.trim() || null, reviewer })
      setAdding(false); setText(''); setTerm(''); setEpsg(''); setNote('')
      await after(r.rebuilding)
    } catch (e) {
      const msg = (e as Error).message
      setError(`Not saved: ${msg.match(/"msg":\s*"([^"]+)"/)?.[1] ?? msg}`)
      setBusy(null)
    }
  }

  async function remove(k: KnowledgeItem) {
    if (!window.confirm(`Remove “${what(k)}” for ${scopeText(k)}? It no longer applies on the next assembly.`)) return
    setBusy('Removing…')
    try { await after((await deleteKnowledge(k.id)).rebuilding) } catch (e) { setError((e as Error).message); setBusy(null) }
  }

  const items = data?.items ?? []
  const valid = kind === 'crs' ? /^\d{4,6}$/.test(epsg.trim()) : kind === 'term' ? term.trim().length >= 2 : text.trim().length > 0
  return (
    <section className="knowledge" aria-label="Knowledge">
      <h3 className="section-head">
        What we know here
        {!adding && <button className="btn btn-small" onClick={() => setAdding(true)}>Add knowledge</button>}
      </h3>
      {items.length === 0 && !adding && (
        <p className="muted">Nothing yet. Add what you know about this document, project or country, also from a phone call or the site; it is applied to all matching holes and given to the model for new pages.</p>
      )}
      {items.length > 0 && (
        <ul className="knowledge-list">
          {items.map((k) => (
            <li key={k.id}>
              <span>
                <strong>{what(k)}</strong> <span className="muted">· {scopeText(k)} · {origin(k)} · {k.created_by}, {fmtDateTime(k.created_at)}</span>
              </span>
              <button className="btn btn-small" onClick={() => remove(k)} disabled={busy != null} aria-label={`Remove ${what(k)}`}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <div className="correction-form correction-form-wide">
          <div className="correction-row">
            <label>Kind
              <select value={kind} onChange={(e) => setKind(e.target.value as KnowledgeItem['kind'])}>
                <option value="note">Note</option>
                <option value="term">Local term → lithology</option>
                <option value="crs">Coordinate system</option>
              </select>
            </label>
            <label>Applies to
              <select value={scope} onChange={(e) => setScope(e.target.value as KnowledgeScope)}>
                {(Object.keys(SCOPE_LABEL) as KnowledgeScope[]).map((s) => (
                  <option key={s} value={s} disabled={s !== 'global' && !data?.scopes?.[s]}>
                    {SCOPE_LABEL[s]}{s !== 'global' && data?.scopes?.[s] ? ` (${data.scopes[s]})` : ''}
                  </option>
                ))}
              </select>
            </label>
            <label>Source
              <select value={source} onChange={(e) => setSource(e.target.value as 'review' | 'external')}>
                <option value="external">Told by someone (phone, e-mail, site)</option>
                <option value="review">From the documents</option>
              </select>
            </label>
          </div>
          {kind === 'note' && <label>Note <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. levels refer to the site datum 12.40 m above MSL" /></label>}
          {kind === 'term' && (
            <div className="correction-row">
              <label>Term as printed <input value={term} onChange={(e) => setTerm(e.target.value)} placeholder="e.g. caliche" /></label>
              <label>Means
                <select value={primary} onChange={(e) => setPrimary(e.target.value)}>
                  {Object.entries(LITHOLOGY).map(([key, s]) => <option key={key} value={key}>{s.label}</option>)}
                </select>
              </label>
            </div>
          )}
          {kind === 'crs' && <label>EPSG code <input inputMode="numeric" value={epsg} onChange={(e) => setEpsg(e.target.value)} placeholder="e.g. 32639" /></label>}
          <label>Where from (optional) <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. phone call with the client's surveyor, 17 Oct" /></label>
          <div className="correction-buttons">
            <button className="btn btn-approve" disabled={!valid || busy != null} onClick={save}>Save knowledge</button>
            <button className="btn" disabled={busy != null} onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}
      {busy && <p className="muted" role="status">{busy}</p>}
      {data?.rebuild.error && <p className="error">Last re-assembly failed: {data.rebuild.error}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  )
}
