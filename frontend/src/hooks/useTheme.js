/**
 * Theme management for SiGMA dark mode.
 *
 * Theme is intentionally NOT kept in the main zustand store because that store
 * rehydrates asynchronously, which would flash the wrong theme on load. Instead
 * we read the saved preference synchronously from localStorage (via `initTheme`,
 * called before React mounts) and keep a tiny module-level store that any
 * component can subscribe to with `useTheme()`.
 */
import { useSyncExternalStore } from 'react'
import { storage, STORAGE_KEYS } from '../utils/storage'

let currentIsDark = false
let initialized = false
const listeners = new Set()

function applyTheme(dark) {
  currentIsDark = dark
  if (typeof document !== 'undefined') {
    document.documentElement.classList.toggle('dark', dark)
  }
  storage.setTheme(dark ? 'dark' : 'light')
  listeners.forEach((l) => l())
}

/** Apply the saved theme to <html> before React mounts (prevents FOUC). */
export function initTheme() {
  if (initialized || typeof window === 'undefined') return
  initialized = true
  currentIsDark = storage.getTheme() === 'dark'
  document.documentElement.classList.toggle('dark', currentIsDark)
}

function subscribe(listener) {
  listeners.add(listener)
  // Cross-tab sync: another tab changing localStorage updates us too.
  const onStorage = (e) => {
    if (e.key === STORAGE_KEYS.global) {
      const dark = storage.getTheme() === 'dark'
      if (dark !== currentIsDark) applyTheme(dark)
    }
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

function getSnapshot() {
  return currentIsDark
}

export function useTheme() {
  const isDark = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const toggleTheme = () => applyTheme(!currentIsDark)
  return { isDark, toggleTheme }
}
