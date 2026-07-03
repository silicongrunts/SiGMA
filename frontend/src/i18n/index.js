/**
 * i18next configuration for SiGMA.
 *
 * This module self-initializes on import: it registers the react-i18next
 * binding and bundles all locale resources. The active language is set
 * synchronously by `initLanguage()` (called from main.jsx before React
 * mounts), so no provider wrapper is needed — components call
 * `useTranslation()` directly.
 *
 * Detection is intentionally NOT delegated to `i18next-browser-languagedetector`;
 * it is hand-rolled in `utils/storage.js` + `hooks/useLanguage.js` to match
 * the existing `useTheme` pattern (synchronous read, no async hydration).
 */
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import en from './locales/en.json'
import zhCN from './locales/zh-CN.json'
import zhTW from './locales/zh-TW.json'
import ja from './locales/ja.json'
import ko from './locales/ko.json'
import hi from './locales/hi.json'
import es from './locales/es.json'
import fr from './locales/fr.json'

i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    'zh-CN': { translation: zhCN },
    'zh-TW': { translation: zhTW },
    ja: { translation: ja },
    ko: { translation: ko },
    hi: { translation: hi },
    es: { translation: es },
    fr: { translation: fr },
  },
  lng: 'en',
  fallbackLng: 'en',
  interpolation: {
    escapeValue: false, // React already escapes
  },
  react: {
    useSuspense: false, // resources are bundled; initLanguage() runs before mount
  },
})

export default i18n
