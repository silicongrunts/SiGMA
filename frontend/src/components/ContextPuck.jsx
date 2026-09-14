/**
 * ContextPuck - the unified gauge glyph at the left of the chat input.
 *
 * One circular emblem carrying three orthogonal facts:
 * - the 260° top arc is the context gauge, filled relative to the compaction
 *   threshold in three discrete color zones (blue < 60%, amber < 90%,
 *   red at 100%, matching the backend's warning levels);
 * - the four dots seated in the bottom gap are the auto-approve toggles
 *   (hollow = off, solid = on), in the caller's fixed category order;
 * - the center menu icon marks the whole square as the settings entry point.
 */
import { Menu } from 'lucide-react'

const CX = 22
const CY = 22
// Ring centerline radius: the 3px stroke reaches 21.5 of the 44px viewBox,
// so the visible circle fills the square (the approval dots reach the edge).
const R = 20
const STROKE = 3

// Angles are clockwise from 12 o'clock. The gauge arc sweeps 260° over the
// top (230° → 130°); the four approval dots sit in the remaining bottom gap.
const ARC_START = 230
const ARC_END = 130
const DOT_ANGLES = [219, 193, 167, 141]

// Color zones as percentages of the threshold. Boundaries mirror the
// backend's context warning levels (60% / 90% / compaction at 100%).
const ZONES = [
  { from: 0, to: 60, color: 'text-blue-500 dark:text-blue-400' },
  { from: 60, to: 90, color: 'text-amber-500 dark:text-amber-400' },
  { from: 90, to: 100, color: 'text-red-500 dark:text-red-400' },
]

function polar(angle, radius) {
  const rad = (angle * Math.PI) / 180
  return [CX + radius * Math.sin(rad), CY - radius * Math.cos(rad)]
}

const pt = (angle) => {
  const [x, y] = polar(angle, R)
  return `${x.toFixed(2)} ${y.toFixed(2)}`
}

const ARC_PATH = `M${pt(ARC_START)} A${R} ${R} 0 1 1 ${pt(ARC_END)}`

export default function ContextPuck({ ratio = 0, over = false, categories = [], approvals = {}, pendingKey = null }) {
  const fill = Math.max(0, Math.min(100, ratio))
  return (
    <div className="relative h-11 w-11">
      <svg viewBox="0 0 44 44" className="h-full w-full" aria-hidden="true">
        {/* Free-context track (light) under the filled zones */}
        <path d={ARC_PATH} pathLength={100} fill="none" stroke="currentColor"
          className="text-gray-200 dark:text-gray-700" strokeWidth={STROKE} strokeLinecap="butt" />
        {ZONES.map(({ from, to, color }) => {
          const end = Math.min(to, fill)
          if (end <= from) return null
          const len = end - from
          return (
            <path key={to} d={ARC_PATH} pathLength={100} fill="none" stroke="currentColor"
              className={`${color}${over && to === 100 ? ' animate-pulse' : ''}`}
              strokeWidth={STROKE} strokeLinecap="butt"
              strokeDasharray={`${len} ${100 - len}`} strokeDashoffset={-from} />
          )
        })}
        {categories.map(({ key }, i) => {
          const angle = DOT_ANGLES[i % DOT_ANGLES.length]
          const [cx, cy] = polar(angle, R)
          const on = approvals[key] === true
          return (
            <g key={key} className={pendingKey === key ? 'animate-pulse' : undefined}>
              {on ? (
                <circle cx={cx} cy={cy} r={2} fill="currentColor" className="text-emerald-500 dark:text-emerald-400" />
              ) : (
                <circle cx={cx} cy={cy} r={1.65} fill="none" strokeWidth={0.7} stroke="currentColor"
                  className="text-gray-400 dark:text-gray-500" />
              )}
            </g>
          )
        })}
      </svg>
      <Menu className="absolute left-1/2 top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 text-gray-400 dark:text-gray-500" />
    </div>
  )
}
