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

/** Neighbouring holes farther apart than this are not joined: the ground between them is unknown. */
export const MAX_JOIN_GAP_M = 150

/** Coarse material groups, so that 'fine sand' in one log and 'medium sand' in the next count as one unit. */
const GROUP: Record<string, string> = Object.fromEntries([
  ...['topsoil', 'fill'].map((c) => [c, 'made ground']),
  ...['gravel', 'coarse_gravel', 'medium_gravel', 'fine_gravel', 'cobbles', 'boulders', 'scree'].map((c) => [c, 'gravel']),
  ...['sand', 'coarse_sand', 'medium_sand', 'fine_sand', 'volcanic_ash'].map((c) => [c, 'sand']),
  ...['silt', 'loess', 'loess_loam', 'slope_loam', 'meadow_lime'].map((c) => [c, 'silt']),
  ...['clay', 'marsh_clay', 'varved_clay', 'boulder_clay', 'till_marl'].map((c) => [c, 'clay']),
  ...['peat_humus', 'gyttja', 'lignite'].map((c) => [c, 'organic']),
])

/** Material group of a lithology; null for core loss and unreadable layers, which match nothing. */
const lithologyGroup = (code: string) =>
  code === 'unknown' || code === 'no_recovery' ? null : GROUP[code] ?? 'rock'

interface Unit { top: number; bottom: number; group: string | null }

function units(strip: Strip): Unit[] {
  const result: Unit[] = []
  for (const { top, bottom, lithology } of intervals(strip)) {
    const group = lithologyGroup(lithology), previous = result[result.length - 1]
    if (previous && group !== null && previous.group === group) previous.bottom = bottom
    else result.push({ top, bottom, group })
  }
  return result
}

interface BoundaryJoin {
  leftDepth: number // metres below ground in the left hole
  rightDepth: number
  above: string // material group above the boundary
  below: string
}

/**
 * Layer boundaries to join between adjacent holes: the units of both logs are matched by material group in
 * top-down order (longest common sequence, so matches never cross), and only matches that every best
 * sequence shares count. A boundary is joined where the units on both sides of it are matched to adjacent
 * units in the other hole; everywhere else the units stay open.
 */
export function correlateBoundaries(leftStrip: Strip, rightStrip: Strip, gapM: number): BoundaryJoin[] {
  if (gapM > MAX_JOIN_GAP_M) return []
  const a = units(leftStrip), b = units(rightStrip)
  const n = a.length, m = b.length
  const same = (i: number, j: number) => a[i].group !== null && a[i].group === b[j].group
  const table = (skip: [number, number] | null) => {
    const t = Array.from({ length: n + 1 }, () => Array<number>(m + 1).fill(0))
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        const match = same(i, j) && !(skip && skip[0] === i && skip[1] === j) ? 1 + t[i + 1][j + 1] : 0
        t[i][j] = Math.max(match, t[i + 1][j], t[i][j + 1])
      }
    }
    return t
  }
  const best = table(null)
  const length = best[0]?.[0] ?? 0
  const pairs = new Map<number, number>()
  for (let i = 0, j = 0; i < n && j < m;) {
    if (same(i, j) && best[i][j] === 1 + best[i + 1][j + 1]) {
      // Kept only if no equally long sequence leaves this match out: otherwise the logs allow another reading.
      if (table([i, j])[0][0] < length) pairs.set(i, j)
      i++; j++
    } else if (best[i + 1][j] >= best[i][j + 1]) i++
    else j++
  }
  const joins: BoundaryJoin[] = []
  for (const [i, j] of pairs) {
    if (pairs.get(i + 1) === j + 1) {
      joins.push({ leftDepth: a[i].bottom, rightDepth: b[j].bottom, above: a[i].group!, below: a[i + 1].group! })
    }
  }
  return joins
}
