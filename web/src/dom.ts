/** An element for map popups and tooltips; text goes in as textContent, since names come from documents. */
export function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string) {
  const node = document.createElement(tag)
  node.className = className
  if (text != null) node.textContent = text
  return node
}

/** Keyboard shortcuts stay off while the user types in a field. */
export const isTyping = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)

/** Bounds of the open sheet, which covers the right part of the map. */
export const detailSheetRect = () => document.querySelector('.detail, .tunnel-detail')?.getBoundingClientRect()

/** A colour of the current theme, for canvases (MapLibre) that cannot read CSS variables. */
export const cssColor = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
