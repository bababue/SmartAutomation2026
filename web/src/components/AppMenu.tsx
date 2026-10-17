import { useEffect, useRef, useState } from 'react'

interface Props {
  inReview: boolean
  inStats: boolean
  uploading: boolean
  dark: boolean
  columns3d: boolean
  onToggleReview: () => void
  onToggleStats: () => void
  onUpload: () => void
  onToggleDark: () => void
  onToggleColumns: () => void
}

type Sub = 'export'

const ICON = {
  review: 'M6.5 2.5h5v2h-5zM5 3.5H3.5v12h11v-12H13M6.5 10l1.75 1.75L11.5 8.5',
  map: 'M2.5 4.5 6.5 3l5 1.5 4-1.5v10.5l-4 1.5-5-1.5-4 1.5zM6.5 3v10.5M11.5 4.5V15',
  upload: 'M9 12V3M5.5 6.5 9 3l3.5 3.5M3 12v3h12v-3',
  export: 'M9 3v9M5.5 8.5 9 12l3.5-3.5M3 12v3h12v-3',
  column: 'M4.5 4.5c0-1.1 2-2 4.5-2s4.5.9 4.5 2v9c0 1.1-2 2-4.5 2s-4.5-.9-4.5-2zM4.5 4.5c0 1.1 2 2 4.5 2s4.5-.9 4.5-2M4.5 9c0 1.1 2 2 4.5 2s4.5-.9 4.5-2',
  dark: 'M14.5 11A6 6 0 0 1 7 3.5a6 6 0 1 0 7.5 7.5z',
  table: 'M3 4h12v10H3zM3 7.5h12M3 11h12M7.5 4v10',
  file: 'M5 2.5h5.5l3 3v10H5zM10.5 2.5v3h3',
  chevron: 'M7 4.5 11.5 9 7 13.5',
  stats: 'M3 15V9.5M7.5 15V4M12 15v-7M16 15H2',
}

function Icon({ d }: { d: string }) {
  return <svg className="menu-icon" width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d={d} /></svg>
}

export function AppMenu({ inReview, inStats, uploading, dark, columns3d, onToggleReview, onToggleStats, onUpload, onToggleDark, onToggleColumns }: Props) {
  const [open, setOpen] = useState(false)
  const [sub, setSub] = useState<Sub | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) { setSub(null); return }
    root.current?.querySelector<HTMLElement>('[role^="menuitem"]')?.focus()
    const onDown = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open])

  const close = () => { setOpen(false); button.current?.focus() }
  const pick = (action: () => void) => () => { action(); close() }

  // Submenus open to the left, because the menu sits at the right edge of the window.
  const openSub = (s: Sub) => {
    setSub(s)
    requestAnimationFrame(() => root.current?.querySelector<HTMLElement>(`#menu-${s} [role^="menuitem"]`)?.focus())
  }
  const closeSub = () => {
    const trigger = root.current?.querySelector<HTMLElement>(`[aria-controls="menu-${sub}"]`)
    setSub(null)
    trigger?.focus()
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) return
    const active = document.activeElement as HTMLElement | null
    const inSub = !!active?.closest('.menu-flyout')
    if (e.key === 'Escape' || (e.key === 'ArrowRight' && inSub)) {
      e.stopPropagation()
      e.preventDefault()
      if (sub) closeSub()
      else close()
      return
    }
    if (e.key === 'ArrowLeft' && active?.getAttribute('aria-haspopup')) {
      e.preventDefault()
      openSub(active.getAttribute('aria-controls')!.slice(5) as Sub)
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const list = active?.closest('[role="menu"]') ?? root.current?.querySelector('[role="menu"]')
    const items = [...(list?.querySelectorAll<HTMLElement>(':scope > [role^="menuitem"]:not(:disabled), :scope > .menu-sub > [role^="menuitem"]') ?? [])]
    const i = items.indexOf(active as HTMLElement)
    items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus()
  }

  const subTrigger = (s: Sub, label: string, icon: string) => (
    <button role="menuitem" className="app-menu-item" aria-haspopup="menu"
      aria-expanded={sub === s} aria-controls={`menu-${s}`} onClick={() => (sub === s ? setSub(null) : openSub(s))}>
      <Icon d={icon} /><span className="menu-text">{label}</span><Icon d={ICON.chevron} />
    </button>
  )

  return (
    <div className="app-menu" ref={root} onKeyDown={onKeyDown}>
      <button ref={button} className="btn btn-icon menu-button" aria-haspopup="menu" aria-expanded={open}
        aria-label="Menu" onClick={() => setOpen(!open)}>
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M3 5h12M3 9h12M3 13h12" /></svg>
      </button>
      {open && (
        <div className="app-menu-list" role="menu" aria-label="Menu">
          <button role="menuitem" className="app-menu-item" onClick={pick(onToggleReview)} onMouseEnter={() => setSub(null)}>
            <Icon d={inReview ? ICON.map : ICON.review} /><span className="menu-text">{inReview ? 'Back to map' : 'Review queue'}</span>
          </button>
          <button role="menuitem" className="app-menu-item" onClick={pick(onToggleStats)} onMouseEnter={() => setSub(null)}>
            <Icon d={inStats ? ICON.map : ICON.stats} /><span className="menu-text">{inStats ? 'Back to map' : 'Statistics'}</span>
          </button>
          <button role="menuitem" className="app-menu-item" onClick={pick(onUpload)} onMouseEnter={() => setSub(null)}
            title="Files chosen together are read as one batch. Or drop them anywhere on the page.">
            <Icon d={ICON.upload} /><span className="menu-text">{uploading ? 'Upload more documents' : 'Upload documents'}</span>
          </button>
          <div className="menu-sub" onMouseEnter={() => setSub('export')}>
            {subTrigger('export', 'Export released', ICON.export)}
            {sub === 'export' && (
              <div className="menu-flyout" id="menu-export">
                <div className="app-menu-list" role="menu" aria-label="Export released records">
                  <a role="menuitem" className="app-menu-item" href="/api/export.xlsx" onClick={close}
                    title="Released records in Herrenknecht's BoreholeDataFormat">
                    <Icon d={ICON.table} /><span className="menu-text">Excel</span>
                  </a>
                  <a role="menuitem" className="app-menu-item" href="/api/export.csv" onClick={close}
                    title="Released records, one row per layer">
                    <Icon d={ICON.file} /><span className="menu-text">CSV</span>
                  </a>
                </div>
              </div>
            )}
          </div>
          <button role="menuitemcheckbox" aria-checked={columns3d} className="app-menu-item" onClick={pick(onToggleColumns)}
            onMouseEnter={() => setSub(null)} title="Depth columns when zoomed in; off shows flat points">
            <Icon d={ICON.column} /><span className="menu-text">3D columns</span>
            <span className={`switch${columns3d ? ' switch-on' : ''}`} aria-hidden="true" />
          </button>
          <button role="menuitemcheckbox" aria-checked={dark} className="app-menu-item" onClick={pick(onToggleDark)}
            onMouseEnter={() => setSub(null)}>
            <Icon d={ICON.dark} /><span className="menu-text">Dark mode</span>
            <span className={`switch${dark ? ' switch-on' : ''}`} aria-hidden="true" />
          </button>
        </div>
      )}
    </div>
  )
}
