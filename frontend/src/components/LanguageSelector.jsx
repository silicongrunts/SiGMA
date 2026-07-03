import { useLanguage, SUPPORTED_LANGUAGES } from '../hooks/useLanguage'

/**
 * Language `<select>` control. Renders the dropdown of supported languages
 * (native names) and applies the user's choice via `useLanguage`.
 *
 * The parent provides the surrounding row (label, icon, container styling)
 * so each menu can match its local layout — see ProjectsView and Header.
 */
export default function LanguageSelector() {
  const { lang, setLanguage } = useLanguage()

  return (
    <select
      value={lang}
      onChange={(e) => setLanguage(e.target.value)}
      className="px-2 py-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-xs font-bold text-gray-700 dark:text-gray-300 outline-none focus:ring-2 focus:ring-sigma-600/20 cursor-pointer"
    >
      {SUPPORTED_LANGUAGES.map(({ code, name }) => (
        <option key={code} value={code}>{name}</option>
      ))}
    </select>
  )
}
