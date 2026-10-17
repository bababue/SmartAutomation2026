import { useState, type KeyboardEvent, type PointerEvent } from 'react'
import { detailSheetRect } from '../dom'

const MIN = 480

const clamp = (width: number) => Math.round(Math.max(MIN, Math.min(width, window.innerWidth - 200)))

/** Drag handle on the left edge of the detail sheet; sets how much of the map stays visible. */
export function SheetResizer({ onResize }: { onResize: (width: number) => void }) {
  const [active, setActive] = useState(false)

  const width = () => detailSheetRect()?.width ?? MIN

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    e.preventDefault()  // no text selection while dragging
    e.currentTarget.setPointerCapture(e.pointerId)
    setActive(true)
  }
  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    if (active) onResize(clamp(window.innerWidth - e.clientX))
  }
  function onPointerUp() {
    setActive(false)
  }
  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === 'ArrowLeft' ? 40 : e.key === 'ArrowRight' ? -40 : 0
    if (!step) return
    e.preventDefault()
    onResize(clamp(width() + step))
  }

  return (
    <div className={`sheet-resizer${active ? ' sheet-resizer-active' : ''}`} role="separator" aria-orientation="vertical"
      aria-label="Resize map and details" title="Drag to resize (← → with the keyboard)" tabIndex={0}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown} />
  )
}
