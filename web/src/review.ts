import type { ReviewStatus } from './types'

export const STATUS_LABEL: Record<ReviewStatus, string> = {
  auto_approved: 'Released automatically',
  pending_review: 'Waiting for review',
  approved: 'Approved',
  rejected: 'Rejected',
}

export const STATUS_COLOR: Record<ReviewStatus, string> = {
  auto_approved: '#2F7D4F',
  approved: '#2F7D4F',
  pending_review: '#B7791F',
  rejected: '#B33A3A',
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
