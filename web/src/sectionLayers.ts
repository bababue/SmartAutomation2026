import type { BoreholeSummary } from './types'

type Strip = NonNullable<BoreholeSummary['strip']>

interface Interval {
  top: number
  bottom: number
  lithology: string
}

export interface SectionBand {
  leftTop: number
  leftBottom: number
  rightTop: number
  rightBottom: number
  lithology: string
}

function intervals(strip: Strip): Interval[] {
  const result: Interval[] = []
  let depth = 0
  for (const [rawTop, bottom, lithology] of [...strip].sort((a, b) => a[0] - b[0])) {
    if (!Number.isFinite(rawTop) || !Number.isFinite(bottom) || bottom <= depth) continue
    const top = Math.max(depth, rawTop)
    if (top > depth + 0.001) result.push({ top: depth, bottom: top, lithology: 'unknown' })
    const previous = result[result.length - 1]
    if (previous?.lithology === lithology && Math.abs(previous.bottom - top) < 0.001) previous.bottom = bottom
    else result.push({ top, bottom, lithology })
    depth = bottom
  }
  return result
}

/** Align adjacent logs without crossing layer boundaries. Unmatched layers pinch out at the other hole. */
export function correlateLayers(leftStrip: Strip, rightStrip: Strip): SectionBand[] {
  const left = intervals(leftStrip), right = intervals(rightStrip)
  if (!left.length || !right.length) return []
  const n = left.length, m = right.length
  const cost = Array.from({ length: n + 1 }, () => Array<number>(m + 1).fill(0))
  for (let i = n; i >= 0; i--) {
    for (let j = m; j >= 0; j--) {
      if (i === n && j === m) continue
      if (i === n) cost[i][j] = 1 + cost[i][j + 1]
      else if (j === m) cost[i][j] = 1 + cost[i + 1][j]
      else {
        const same = left[i].lithology === right[j].lithology && !['unknown', 'no_recovery'].includes(left[i].lithology)
        const match = (same ? 0 : 3) + cost[i + 1][j + 1]
        cost[i][j] = Math.min(match, 1 + cost[i + 1][j], 1 + cost[i][j + 1])
      }
    }
  }

  const bands: SectionBand[] = []
  let i = 0, j = 0, leftDepth = 0, rightDepth = 0
  while (i < n || j < m) {
    const same = i < n && j < m && left[i].lithology === right[j].lithology && !['unknown', 'no_recovery'].includes(left[i].lithology)
    if (same && cost[i][j] === cost[i + 1][j + 1]) {
      bands.push({ leftTop: left[i].top, leftBottom: left[i].bottom,
        rightTop: right[j].top, rightBottom: right[j].bottom, lithology: left[i].lithology })
      leftDepth = left[i++].bottom
      rightDepth = right[j++].bottom
    } else if (i < n && (j === m || cost[i][j] === 1 + cost[i + 1][j])) {
      bands.push({ leftTop: left[i].top, leftBottom: left[i].bottom,
        rightTop: rightDepth, rightBottom: rightDepth, lithology: left[i].lithology })
      leftDepth = left[i++].bottom
    } else {
      bands.push({ leftTop: leftDepth, leftBottom: leftDepth,
        rightTop: right[j].top, rightBottom: right[j].bottom, lithology: right[j].lithology })
      rightDepth = right[j++].bottom
    }
  }
  return bands.filter((band) => !['unknown', 'no_recovery'].includes(band.lithology))
}
