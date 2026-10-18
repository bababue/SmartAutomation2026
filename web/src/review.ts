import { useEffect, useState } from 'react'
import { fetchCrsGroups, fetchQueue, fetchQueueFindings } from './api'
import type { CrsGroup, FindingFacet, GroupFacet, QueueItem, ReviewFilters, ReviewStatus, Severity } from './types'

export const STATUS_LABEL: Record<ReviewStatus, string> = {
  auto_approved: 'Released automatically',
  pending_review: 'Waiting for review',
  approved: 'Approved',
  rejected: 'Rejected',
}

export const SEVERITY_LABEL: Record<Severity, string> = {
  blocking: 'Blocking', needs_look: 'Needs a look', warning: 'Warning', info: 'Note',
}

export const scoreText = (score: number | null) => (score == null ? '–' : String(Math.round(score * 100)))

const REVIEWER_KEY = 'borehole-atlas-reviewer'

export function loadReviewer(): string {
  try {
    return localStorage.getItem(REVIEWER_KEY) ?? ''
  } catch {
    return ''
  }
}

export function saveReviewer(name: string) {
  try {
    localStorage.setItem(REVIEWER_KEY, name)
  } catch {
    /* storage unavailable: the name is just not remembered */
  }
}

export const fmtDateTime = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : ''

/** `text` with `suffix` appended, unless the printed text already carries it ('… (WGS84)', '… m a.s.l.'). */
export const withSuffix = (text: string, suffix: string | null, wrap = (x: string) => ` ${x}`) =>
  suffix && !text.toLowerCase().includes(suffix.toLowerCase()) ? `${text}${wrap(suffix)}` : text

/** The review queue for the filters (or the release audit sample), with its finding counts and the CRS groups to confirm. */
export function useReviewQueue(filters: ReviewFilters, auditOnly: boolean, refresh: number) {
  const [items, setItems] = useState<QueueItem[]>([])
  const [groups, setGroups] = useState<Pick<GroupFacet, 'group' | 'project' | 'count' | 'released'>[]>([])
  const [total, setTotal] = useState(0)
  const [findings, setFindings] = useState<FindingFacet[]>([])
  const [crsGroups, setCrsGroups] = useState<CrsGroup[]>([])

  // The queue is loading until a response for the current request has landed.
  const key = `${refresh}|${JSON.stringify(filters)}|${auditOnly}`
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    fetchQueue(filters, ctrl.signal, 2000, auditOnly)
      .then((page) => { setItems(page.items); setTotal(page.total); setGroups(page.groups); setLoadedKey(key) })
      .catch((e) => { if (e.name !== 'AbortError') setLoadedKey(key) })
    fetchQueueFindings(filters, ctrl.signal).then(setFindings).catch(() => undefined)
    fetchCrsGroups(ctrl.signal).then(setCrsGroups).catch(() => undefined)
    return () => ctrl.abort()
  }, [key, filters, auditOnly])

  return { items, groups, total, findings, crsGroups, loading: loadedKey !== key }
}
