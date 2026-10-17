import { lithStyle } from './lithology'
import type { BoreholeSummary } from './types'

export type TooltipBorehole = BoreholeSummary & {
  layer_top?: number
  layer_bottom?: number
  layer_lithology?: string
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string) {
  const node = document.createElement(tag)
  node.className = className
  if (text != null) node.textContent = text
  return node
}

export function tooltipContent(p: TooltipBorehole) {
  const root = el('div', 'tip')
  root.append(el('strong', 'tip-name', p.name))
  if (p.depth != null) root.append(el('span', 'tip-line', `Depth ${p.depth.toLocaleString('en', { maximumFractionDigits: 1 })} m`))
  if (p.loc_method === 'geocoded_place') root.append(el('span', 'tip-line', 'Approximate position'))
  if (p.layer_lithology) root.append(el('span', 'tip-line',
    `${p.layer_top}–${p.layer_bottom} m: ${lithStyle(p.layer_lithology).label}`))
  return root
}

export function estimationTooltip(lithology: string, top: number, bottom: number, leftName: string, rightName: string) {
  const root = el('div', 'tip')
  const depth = (value: number) => value.toLocaleString('en', { maximumFractionDigits: 1 })
  root.append(el('strong', 'tip-name', 'Estimation'))
  root.append(el('span', 'tip-line', lithStyle(lithology).label))
  root.append(el('span', 'tip-line', `Approx. ${depth(top)}–${depth(bottom)} m below ground`))
  root.append(el('span', 'tip-line', `Between ${leftName} and ${rightName}`))
  return root
}
