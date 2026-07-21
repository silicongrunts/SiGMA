/**
 * Editor appearance preferences (font family, font size, line height, syntax
 * scheme).
 *
 * Intentionally NOT kept in the main zustand store for the same reason as
 * useTheme: the store rehydrates asynchronously, which would apply the wrong
 * font/scheme to the editor on load. We read the saved preference
 * synchronously from localStorage (via `initEditorAppearance`, called before
 * React mounts) and keep a tiny module-level store that any component can
 * subscribe to with `useEditorAppearance()`.
 *
 * Cross-tab sync: the storage event listener keys on STORAGE_KEYS.global, so
 * it also fires when another tab changes the editor appearance; we re-read and
 * notify only when the value actually changed.
 */
import { useSyncExternalStore, useCallback } from 'react'
import { storage, STORAGE_KEYS } from '../utils/storage'

let currentAppearance = null
let initialized = false
const listeners = new Set()

function readAppearance() {
  return storage.getEditorAppearance()
}

function applyAppearance(next) {
  currentAppearance = next
  listeners.forEach((l) => l())
}

export function initEditorAppearance() {
  if (initialized || typeof window === 'undefined') return
  initialized = true
  currentAppearance = readAppearance()
}

function subscribe(listener) {
  listeners.add(listener)
  const onStorage = (e) => {
    if (e.key === STORAGE_KEYS.global) {
      const next = readAppearance()
      if (!currentAppearance || JSON.stringify(next) !== JSON.stringify(currentAppearance)) {
        applyAppearance(next)
      }
    }
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

function getSnapshot() {
  if (!currentAppearance) currentAppearance = readAppearance()
  return currentAppearance
}

export function useEditorAppearance() {
  const appearance = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  /**
   * Patch the editor appearance. Accepts either a partial object or an updater
   * function `(current) => partial` (mirrors the zustand set signature). The
   * returned reference is stable across renders, so callers can safely capture
   * it in long-lived closures (e.g. editor keymaps).
   */
  const setEditorAppearance = useCallback((patchOrUpdater) => {
    const patch = typeof patchOrUpdater === 'function'
      ? patchOrUpdater(readAppearance())
      : patchOrUpdater
    storage.setEditorAppearance(patch)
    // storage.setEditorAppearance runs through sanitize; re-read the
    // sanitized result so listeners always get a clean object.
    applyAppearance(readAppearance())
  }, [])

  return { appearance, setEditorAppearance }
}
