/** At most `digits` decimals; '–' for a missing value. */
export const fmtNum = (v: number | null | undefined, digits = 2) =>
  v == null ? '–' : v.toLocaleString('en', { maximumFractionDigits: digits })

export const fmtMetres = (v: number | null | undefined, digits = 1) => (v == null ? '–' : `${fmtNum(v, digits)} m`)

/** A count with thousands separators. */
export const fmtCount = (v: number) => v.toLocaleString('en')

/** A share as a whole percentage; 0 when there is nothing to share. */
export const percentOf = (part: number, whole: number) => (whole ? Math.round((100 * part) / whole) : 0)

/** A distance along a route: km from 1 km on, finer steps below 10 m for zoomed-in axes. */
export const fmtDistance = (m: number) =>
  m >= 1000 ? `${fmtNum(m / 1000, 2)} km` : `${fmtNum(m, m < 1 ? 2 : m < 10 ? 1 : 0)} m`

/** A code as words: 'point_load_index' → 'point load index'. */
export const words = (code: string) => code.replaceAll('_', ' ')

/** A typed decimal, with a comma accepted as the decimal mark; null for an empty or unreadable entry. */
export function parseDecimal(text: string): number | null {
  if (text.trim() === '') return null
  const n = Number(text.replace(',', '.'))
  return Number.isFinite(n) ? n : null
}
