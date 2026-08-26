/**
 * Shared ISO timestamp formatting for list rows and message headers.
 *
 * Renders "2026-01-01 12:34:22" via the `sv-SE` locale, which yields a
 * sortable `YYYY-MM-DD HH:mm:ss` shape across timezones.
 */
export function formatTimestamp(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('sv-SE', { hour12: false }).replace('T', ' ')
}
