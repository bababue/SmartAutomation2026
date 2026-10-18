import { useEffect, useState } from 'react'

/** A switch remembered in localStorage as `on` or `off`; without a stored choice `fallback` decides. */
export function useStoredFlag(key: string, on: string, off: string, fallback: () => boolean) {
  const [value, setValue] = useState(() => {
    try {
      const saved = localStorage.getItem(key)
      if (saved === on || saved === off) return saved === on
    } catch { /* storage blocked */ }
    return fallback()
  })
  useEffect(() => {
    try { localStorage.setItem(key, value ? on : off) } catch { /* private mode: the choice lasts this visit */ }
  }, [key, on, off, value])
  return [value, setValue] as const
}

const prefersDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches

/** The colour theme: the stored choice, else the system's, applied to the document. */
export function useTheme() {
  const [dark, setDark] = useStoredFlag('borehole-atlas.theme', 'dark', 'light', prefersDark)
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light' }, [dark])
  return [dark, setDark] as const
}
