import { el } from './dom'
import { fmtNum } from './format'
import { lithStyle } from './lithology'
import type { MapPoint } from './types'

type TooltipBorehole = MapPoint & {
  layer_top?: number
  layer_bottom?: number
  layer_lithology?: string
}

export function tooltipContent(p: TooltipBorehole) {
  const root = el('div', 'tip')
  root.append(el('strong', 'tip-name', p.name))
  if (p.depth != null) root.append(el('span', 'tip-line', `Borehole depth ${fmtNum(p.depth, 1)} m`))
  if (p.loc_method === 'geocoded_place') root.append(el('span', 'tip-line', 'Approximate position'))
  if (p.layer_lithology) root.append(el('span', 'tip-line',
    `${fmtNum(p.layer_top, 1)}–${fmtNum(p.layer_bottom, 1)} m below ground: ${lithStyle(p.layer_lithology).label}`))
  return root
}

export function estimationTooltip(lithology: string, top: number, bottom: number, leftName: string, rightName: string) {
  const root = el('div', 'tip')
  root.append(el('strong', 'tip-name', 'Estimation'))
  root.append(el('span', 'tip-line', lithStyle(lithology).label))
  root.append(el('span', 'tip-line', `Approx. ${fmtNum(top, 1)}–${fmtNum(bottom, 1)} m below ground`))
  root.append(el('span', 'tip-line', `Between ${leftName} and ${rightName}`))
  return root
}
