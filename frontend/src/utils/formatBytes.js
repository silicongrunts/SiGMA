/**
 * Shared human-readable byte formatting.
 *
 * 1024-based, one decimal place for anything at or above KB:
 *   512 -> "512 B",  363084 -> "354.6 KB",  22568757 -> "21.5 MB",
 *   1374389535 -> "1.3 GB",  6001164224000 -> "5.6 TB"
 */
export function formatBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return null
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(1)} ${units[unit]}`
}
