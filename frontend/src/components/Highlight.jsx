/**
 * HighlightText — render text with a case-insensitive substring highlighted.
 *
 * Shared by library keyword results and chat search results; `query` is
 * matched literally (regex metacharacters escaped).
 */
export function HighlightText({ text, query }) {
  if (!query || !text) return text
  try {
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const parts = String(text).split(new RegExp(`(${escaped})`, 'gi'))
    return parts.map((part, i) =>
      part.toLowerCase() === query.toLowerCase()
        ? <mark key={i} className="bg-yellow-200 dark:bg-yellow-700 text-yellow-900 dark:text-yellow-200 rounded px-0.5">{part}</mark>
        : part
    )
  } catch {
    return text
  }
}
