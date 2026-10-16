// Lithology colours and hatch patterns, drawn after the DIN 4023 symbol chart (2006 edition).
// Colours are an approximation for screen use, not a normative palette.

import type { JSX } from 'react'

interface Style {
  label: string
  fill: string
  ink?: string
  pattern: (ink: string) => JSX.Element
}

const S = 16 // pattern tile size in px

const dots = (r: number, pts: [number, number][]) => (ink: string) => (
  <>{pts.map(([x, y], i) => <circle key={i} cx={x} cy={y} r={r} fill={ink} />)}</>
)
const rings = (r: number, pts: [number, number][]) => (ink: string) => (
  <>{pts.map(([x, y], i) => <circle key={i} cx={x} cy={y} r={r} fill="none" stroke={ink} strokeWidth={0.9} />)}</>
)
const dashes = (rows: number[], len = 6) => (ink: string) => (
  <>
    {rows.map((y, i) => (
      <line key={i} x1={i % 2 ? 9 : 1} x2={(i % 2 ? 9 : 1) + len} y1={y} y2={y} stroke={ink} strokeWidth={1} />
    ))}
  </>
)
const bricks = (slant = 0) => (ink: string) => (
  <>
    <line x1={0} y1={0.5} x2={S} y2={0.5} stroke={ink} strokeWidth={0.8} />
    <line x1={0} y1={8.5} x2={S} y2={8.5} stroke={ink} strokeWidth={0.8} />
    <line x1={4} y1={0} x2={4 + slant} y2={8} stroke={ink} strokeWidth={0.8} />
    <line x1={12} y1={8} x2={12 + slant} y2={16} stroke={ink} strokeWidth={0.8} />
  </>
)
const glyph = (ch: string, size = 7) => (ink: string) => (
  <>
    <text x={2} y={7} fontSize={size} fill={ink} fontFamily="sans-serif">{ch}</text>
    <text x={10} y={15} fontSize={size} fill={ink} fontFamily="sans-serif">{ch}</text>
  </>
)
const combine = (...fns: ((ink: string) => JSX.Element)[]) => (ink: string) => <>{fns.map((f, i) => <g key={i}>{f(ink)}</g>)}</>
const hlines = (ink: string) => (
  <>
    <line x1={0} y1={4} x2={S} y2={4} stroke={ink} strokeWidth={0.6} />
    <line x1={0} y1={12} x2={S} y2={12} stroke={ink} strokeWidth={0.6} />
  </>
)
const diag = (ink: string) => <path d={`M0 ${S} L${S} 0 M-4 4 L4 -4 M${S - 4} ${S + 4} L${S + 4} ${S - 4}`} stroke={ink} strokeWidth={0.8} />

const silt = (ink: string) => (
  <>
    <path d="M2 6 q1.5 -2 3 0 M4 6 q1.5 -2 3 0" fill="none" stroke={ink} strokeWidth={0.8} />
    <path d="M9 14 q1.5 -2 3 0 M11 14 q1.5 -2 3 0" fill="none" stroke={ink} strokeWidth={0.8} />
  </>
)
const triangles = (ink: string) => (
  <path d="M2 7 l3 -5 l3 5 z M10 15 l3 -5 l3 5 z" fill="none" stroke={ink} strokeWidth={0.8} />
)
const waves = (ink: string) => (
  <path d="M0 5 q4 -4 8 0 t8 0 M0 13 q4 -4 8 0 t8 0" fill="none" stroke={ink} strokeWidth={0.8} />
)

export const LITHOLOGY: Record<string, Style> = {
  gravel: { label: 'Gravel', fill: '#F2B33D', pattern: rings(2.2, [[4, 4], [12, 11]]) },
  coarse_gravel: { label: 'Coarse gravel', fill: '#EFA531', pattern: rings(3, [[5, 5], [12, 12]]) },
  medium_gravel: { label: 'Medium gravel', fill: '#F2B33D', pattern: rings(2.2, [[4, 4], [12, 11]]) },
  fine_gravel: { label: 'Fine gravel', fill: '#F5C463', pattern: rings(1.4, [[4, 4], [12, 11], [12, 3]]) },
  sand: { label: 'Sand', fill: '#F7E7A1', pattern: dots(0.9, [[3, 3], [11, 6], [6, 11], [14, 14]]) },
  coarse_sand: { label: 'Coarse sand', fill: '#F4DF8B', pattern: dots(1.3, [[3, 3], [11, 6], [6, 12]]) },
  medium_sand: { label: 'Medium sand', fill: '#F7E7A1', pattern: dots(0.9, [[3, 3], [11, 6], [6, 11], [14, 14]]) },
  fine_sand: { label: 'Fine sand', fill: '#FAEFC0', pattern: dots(0.6, [[3, 3], [11, 6], [6, 11], [14, 14], [9, 2]]) },
  silt: { label: 'Silt', fill: '#B5D58A', pattern: silt },
  clay: { label: 'Clay', fill: '#B9A6CC', pattern: dashes([4, 8, 12]) },
  peat_humus: { label: 'Peat / humus', fill: '#7A6545', ink: '#F1E9D8', pattern: combine(dashes([5, 13], 5), waves) },
  cobbles: { label: 'Cobbles', fill: '#E39A3B', pattern: (ink) => <path d="M2 3 l4 -1 l2 3 l-3 3 l-3 -2 z M9 10 l5 0 l1 4 l-4 1 l-2 -3 z" fill="none" stroke={ink} strokeWidth={0.8} /> },
  boulders: { label: 'Boulders', fill: '#D98A2B', pattern: (ink) => <path d="M1 2 l8 -1 l3 6 l-5 5 l-6 -3 z" fill="none" stroke={ink} strokeWidth={0.9} /> },
  volcanic_ash: { label: 'Volcanic ash', fill: '#CFCFCF', pattern: (ink) => <path d="M2 4 q2 3 4 0 M10 12 q2 3 4 0" fill="none" stroke={ink} strokeWidth={0.8} /> },
  topsoil: { label: 'Topsoil', fill: '#8C7A57', ink: '#F1E9D8', pattern: glyph('Mu', 5) },
  fill: { label: 'Fill / made ground', fill: '#C9CBCB', pattern: glyph('A') },
  slope_loam: { label: 'Slope loam', fill: '#CDB38A', pattern: diag },
  scree: { label: 'Scree', fill: '#D8B47A', pattern: combine(diag, triangles) },
  boulder_clay: { label: 'Boulder clay', fill: '#A7A38B', pattern: combine(diag, rings(1.6, [[5, 11], [12, 4]])) },
  till_marl: { label: 'Till marl', fill: '#B3B39A', pattern: combine(diag, rings(2, [[5, 11]])) },
  loess: { label: 'Loess', fill: '#E8D9A8', pattern: glyph('I') },
  loess_loam: { label: 'Loess loam', fill: '#DCC594', pattern: combine(diag, dots(0.9, [[4, 10], [11, 4]])) },
  marsh_clay: { label: 'Marsh clay', fill: '#9AA7A0', pattern: combine(dashes([5, 13], 4), dots(0.8, [[12, 5], [4, 13]])) },
  meadow_lime: { label: 'Meadow lime', fill: '#E6EEDD', pattern: (ink) => <path d="M2 6 l5 0 l-2 -2 M9 14 l5 0 l-2 -2" fill="none" stroke={ink} strokeWidth={0.8} /> },
  varved_clay: { label: 'Varved clay', fill: '#A99BBF', pattern: combine(hlines, dashes([8])) },
  gyttja: { label: 'Gyttja', fill: '#7F8C6A', ink: '#F1E9D8', pattern: dashes([4, 12], 3) },
  lignite: { label: 'Lignite', fill: '#4B3B2A', ink: '#E8DFCF', pattern: dashes([4, 12], 6) },
  conglomerate: { label: 'Conglomerate', fill: '#E2B46A', pattern: combine(hlines, rings(2, [[5, 8], [12, 0], [12, 16]])) },
  breccia: { label: 'Breccia', fill: '#D9A35C', pattern: combine(hlines, triangles) },
  sandstone: { label: 'Sandstone', fill: '#F2D58C', pattern: combine(hlines, dots(0.9, [[4, 8], [12, 0], [12, 16], [8, 8]])) },
  siltstone: { label: 'Siltstone', fill: '#C6D39B', pattern: combine(hlines, silt) },
  claystone: { label: 'Claystone', fill: '#A8A0BF', pattern: combine(hlines, dashes([8], 5)) },
  marlstone: { label: 'Marlstone', fill: '#B8C9A6', pattern: combine(bricks(), (ink) => <line x1={8} y1={3} x2={8} y2={6} stroke={ink} strokeWidth={0.8} />) },
  limestone: { label: 'Limestone', fill: '#9EC3E0', pattern: bricks() },
  dolomite: { label: 'Dolomite', fill: '#8FB0CF', pattern: bricks(3) },
  anhydrite: { label: 'Anhydrite', fill: '#E7D7EF', pattern: triangles },
  gypsum: { label: 'Gypsum', fill: '#F1E3F3', pattern: (ink) => <path d="M2 2 l3 5 l3 -5 M10 10 l3 5 l3 -5" fill="none" stroke={ink} strokeWidth={0.8} /> },
  salt_rock: { label: 'Salt rock', fill: '#F5C9C9', pattern: (ink) => <path d="M2 2 h4 v4 h-4 z M10 10 h4 v4 h-4 z" fill="none" stroke={ink} strokeWidth={0.8} /> },
  tuff: { label: 'Tuff', fill: '#D7C6A5', pattern: (ink) => <path d="M1 6 q3 3 6 0 M9 14 q3 3 6 0" fill="none" stroke={ink} strokeWidth={0.8} /> },
  hard_coal: { label: 'Hard coal', fill: '#2E2E2E', ink: '#BDBDBD', pattern: waves },
  quartzite: { label: 'Quartzite', fill: '#E8DCC2', pattern: combine(dots(0.9, [[4, 4], [12, 12]]), triangles) },
  volcanic_rock: { label: 'Volcanic rock', fill: '#C98E8E', pattern: glyph('+', 8) },
  plutonic_rock: { label: 'Plutonic rock', fill: '#F2A7A7', pattern: (ink) => <path d="M4 2 l0 3 l-2 2 M4 5 l2 2 M12 10 l0 3 l-2 2 M12 13 l2 2" fill="none" stroke={ink} strokeWidth={0.9} /> },
  massive_metamorphic: { label: 'Massive metamorphic', fill: '#C7B3A1', pattern: (ink) => <path d="M2 3 l3 4 l3 -4 M10 11 l3 4 l3 -4" fill="none" stroke={ink} strokeWidth={0.8} /> },
  foliated_metamorphic: { label: 'Foliated metamorphic', fill: '#B7A294', pattern: waves },
  chalk: { label: 'Chalk', fill: '#F3F3EE', pattern: glyph('I') },
  calc_tufa: { label: 'Calcareous tufa', fill: '#E9E4C9', pattern: combine(glyph('I'), hlines) },
  no_recovery: { label: 'No recovery', fill: '#FFFFFF', pattern: (ink) => <path d="M0 0 L16 16 M16 0 L0 16" stroke={ink} strokeWidth={0.6} /> },
  unknown: { label: 'Unknown', fill: '#E4E7E5', pattern: () => <></> },
}

export const lithStyle = (key: string): Style => LITHOLOGY[key] ?? LITHOLOGY.unknown

export const patternId = (key: string) => `lith-${key}`

/** Put once into an <svg> (or a hidden one) to make all patterns available by url(#lith-…). */
export function LithologyPatterns({ scale = 1 }: { scale?: number }) {
  return (
    <defs>
      {Object.entries(LITHOLOGY).map(([key, s]) => (
        <pattern
          key={key}
          id={patternId(key)}
          width={S}
          height={S}
          patternUnits="userSpaceOnUse"
          patternTransform={scale !== 1 ? `scale(${scale})` : undefined}
        >
          <rect width={S} height={S} fill={s.fill} />
          {s.pattern(s.ink ?? '#2B3336')}
        </pattern>
      ))}
    </defs>
  )
}

// Chronostratigraphic colours (ICS chart) as fallback when a log has no colour of its own.
const AGE_COLORS: [RegExp, string][] = [
  [/holoc/i, '#FEF2E0'],
  [/pleist/i, '#FFF2AE'],
  [/quatern|^q/i, '#F9F97F'],
  [/plioc/i, '#FFFF99'],
  [/mioc/i, '#FFFF00'],
  [/neogen/i, '#FFE619'],
  [/oligoc/i, '#FEC07A'],
  [/eocen/i, '#FDB46C'],
  [/paleoc/i, '#FDA75F'],
  [/paleogen|tertiar/i, '#FD9A52'],
  [/cretac/i, '#7FC64E'],
  [/jurass/i, '#34B2C9'],
  [/trias/i, '#812B92'],
  [/perm/i, '#F04028'],
  [/carbon/i, '#67A599'],
  [/devon/i, '#CB8C37'],
  [/silur/i, '#B3E1B6'],
  [/ordov/i, '#009270'],
  [/cambr/i, '#7FA056'],
  [/proteroz|precambr/i, '#F73563'],
  [/archea|archaea/i, '#F0047F'],
]

const MUTED = ['#D9E3E8', '#E5DDD0', '#DCE6D5', '#E8DCE3', '#DDE0EA', '#EDE6CF']

export function stratColor(layer: { stratigraphic_color: string | null; geologic_age: string | null; stratigraphic_code: string | null }): string {
  if (layer.stratigraphic_color && /^#[0-9a-f]{6}$/i.test(layer.stratigraphic_color)) return layer.stratigraphic_color
  for (const [re, color] of AGE_COLORS) {
    if (layer.geologic_age && re.test(layer.geologic_age)) return color
  }
  const code = layer.stratigraphic_code ?? ''
  if (!code) return '#F1F3F1'
  let h = 0
  for (const ch of code) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return MUTED[h % MUTED.length]
}
