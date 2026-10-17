import { postKnowledge } from './api'
import { loadReviewer } from './review'
import type { KnowledgeScope } from './types'

/** After a lithology correction: offer to remember the term for similar descriptions. */
export async function rememberTerm(boreholeId: string, term: string, primary: string, scope: KnowledgeScope) {
  const reviewer = loadReviewer().trim()
  if (!reviewer || term.trim().length < 2) return 0
  const r = await postKnowledge({ borehole_id: boreholeId, scope_type: scope, kind: 'term', payload: { term: term.trim(), primary },
    source: 'review', note: 'from a layer correction', reviewer })
  return r.rebuilding
}
