import { useEffect, type RefObject } from 'react'
import { isTyping } from './dom'

/** Esc closes the open record. `close` must keep its identity, or the listener is registered anew on every change. */
export function useEscapeToClose(close: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target)) return
      // An open card editor keeps the record, so Esc never drops its drafts.
      if (e.key === 'Escape' && !document.querySelector('.detail .card-editing, .detail .head-edit')) close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])
}

/** Up and down step through the list in `sidebar` as it is shown: whatever list is open, collapsed groups skipped. */
export function useListArrowKeys(sidebar: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key !== 'ArrowDown' && e.key !== 'ArrowUp') || isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.target instanceof Element && e.target.closest('.maplibregl-map, [role="menu"], [role="listbox"], .detail, .tunnel-detail, .section')) return
      if (!sidebar.current || sidebar.current.inert) return
      const items = [...sidebar.current.querySelectorAll<HTMLButtonElement>('[data-list-item]')]
      const i = items.findIndex((b) => b.getAttribute('aria-current') === 'true')
      const next = items[i < 0 ? 0 : i + (e.key === 'ArrowDown' ? 1 : -1)]
      e.preventDefault()
      if (!next || next === items[i]) return
      next.click()  // the list's own selection; a click on the current item would close it
      next.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sidebar])
}
