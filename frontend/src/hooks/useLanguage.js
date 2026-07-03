/**
 * Language management for SiGMA i18n.
 *
 * Language is intentionally NOT kept in the main zustand store because that
 * store rehydrates asynchronously, which would flash the wrong language on
 * load. Instead we read the saved preference synchronously from localStorage
 * (via `initLanguage`, called before React mounts) and keep a tiny
 * module-level store that any component can subscribe to with `useLanguage()`.
 *
 * This mirrors `useTheme.js` exactly in structure.
 */
import { useSyncExternalStore } from 'react'
import { storage, STORAGE_KEYS } from '../utils/storage'
import i18n from '../i18n'

/** Languages supported by the UI, with native display names for the selector. */
export const SUPPORTED_LANGUAGES = [
  { code: 'en', name: 'English' },
  { code: 'zh-CN', name: '简体中文' },
  { code: 'zh-TW', name: '繁體中文' },
  { code: 'ja', name: '日本語' },
  { code: 'ko', name: '한국어' },
  { code: 'hi', name: 'हिन्दी' },
  { code: 'es', name: 'Español' },
  { code: 'fr', name: 'Français' },
]

let currentLang = 'en'
let initialized = false
const listeners = new Set()

function applyLanguage(lang) {
  currentLang = lang
  i18n.changeLanguage(lang)
  storage.setLanguage(lang)
  listeners.forEach((l) => l())
}

/** Read the saved language preference and apply it to i18next BEFORE React mounts. */
export function initLanguage() {
  if (initialized || typeof window === 'undefined') return
  initialized = true
  currentLang = storage.getLanguage()
  i18n.changeLanguage(currentLang)
}

function subscribe(listener) {
  listeners.add(listener)
  // Cross-tab sync: another tab changing localStorage updates us too.
  const onStorage = (e) => {
    if (e.key === STORAGE_KEYS.global) {
      const lang = storage.getLanguage()
      if (lang !== currentLang) applyLanguage(lang)
    }
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

function getSnapshot() {
  return currentLang
}

export function useLanguage() {
  const lang = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return { lang, setLanguage: applyLanguage }
}
