/**
 * ContextPuck - the unified gauge glyph at the left of the chat input.
 *
 * One circular emblem carrying three orthogonal facts:
 * - the 260° top arc is the context gauge, filled relative to the compaction
 *   threshold with a smooth gradient: SiGMA brand blue at the start, fully
 *   amber at 60% of the threshold, red at 100%;
 * - the four dots seated in the bottom gap are the auto-approve toggles
 *   (hollow = off, solid = on), in the caller's fixed category order;
 * - the center menu icon marks the whole square as the settings entry point.
 */
import { Menu } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'

const CX = 22
const CY = 22
// Ring centerline radius. One step in from the viewBox edge (the 3px stroke
// reaches 19.5 of 22) so the ring reads clearly smaller than its hover
// square; the approval dots sit on the same centerline in the bottom gap.
const R = 18
const STROKE = 3

// Angles are clockwise from 12 o'clock. The gauge arc sweeps 260° over the
// top (230° → 130°); the four approval dots sit in the remaining bottom gap.
const ARC_START = 230
const ARC_END = 130
const DOT_ANGLES = [219, 193, 167, 141]

// Gradient anchors as fractions of the threshold, in sRGB. Interpolation is
// a straight blend between anchors — the blue → amber midpoint desaturates
// toward a neutral mix instead of rotating hue, which would sweep through
// green and turn the gauge into a rainbow.
const ANCHORS = [
  { at: 0, light: [37, 99, 235], dark: [96, 165, 250] },    // sigma-600 / blue-400
  { at: 0.6, light: [245, 158, 11], dark: [251, 191, 36] }, // amber-500 / amber-400
  { at: 1, light: [239, 68, 68], dark: [248, 113, 113] },   // red-500 / red-400
]

// SVG stroke gradients are bounding-box linear, not path-parametric, so the
// filled arc is drawn as many short slices whose colors interpolate between
// the anchors. At this size the slices are imperceptible and the fill reads
// as one smooth gradient. Each slice overlaps the next by a hair so the
// antialiased butt caps leave no seam (later slices paint on top).
const SLICES = 48
const SLICE_OVERLAP = 0.5 // pathLength units

function polar(angle, radius) {
  const rad = (angle * Math.PI) / 180
  return [CX + radius * Math.sin(rad), CY - radius * Math.cos(rad)]
}

const pt = (angle) => {
  const [x, y] = polar(angle, R)
  return `${x.toFixed(2)} ${y.toFixed(2)}`
}

const ARC_PATH = `M${pt(ARC_START)} A${R} ${R} 0 1 1 ${pt(ARC_END)}`

function anchorColor(t, isDark) {
  const pick = (a) => (isDark ? a.dark : a.light)
  if (t <= ANCHORS[0].at) return pick(ANCHORS[0])
  for (let i = 1; i < ANCHORS.length; i++) {
    if (t <= ANCHORS[i].at) {
      const from = pick(ANCHORS[i - 1])
      const to = pick(ANCHORS[i])
      const f = (t - ANCHORS[i - 1].at) / (ANCHORS[i].at - ANCHORS[i - 1].at)
      return [0, 1, 2].map((k) => from[k] + (to[k] - from[k]) * f)
    }
  }
  return pick(ANCHORS[ANCHORS.length - 1])
}

const rgb = ([r, g, b]) => `rgb(${Math.round(r)} ${Math.round(g)} ${Math.round(b)})`

function gradientSlices(fill, isDark) {
  const slices = []
  const step = 100 / SLICES
  for (let from = 0; from < fill; from += step) {
    // the last slice stops exactly at `fill` so the tip position stays exact
    const to = Math.min(from + step + SLICE_OVERLAP, fill)
    slices.push({
      from,
      len: to - from,
      color: rgb(anchorColor((from + to) / 200, isDark)),
    })
  }
  return slices
}

export default function ContextPuck({ ratio = 0, over = false, categories = [], approvals = {}, pendingKey = null }) {
  const { isDark } = useTheme()
  const fill = Math.max(0, Math.min(100, ratio))
  return (
    // The height keeps the wrapping button flush with the input container's
    // top and bottom; the narrower width shrinks the ring one step and hands
    // the difference to the input box (the square SVG letterboxes to width).
    <div className="relative h-11 w-10">
      <svg viewBox="0 0 44 44" className="h-full w-full" aria-hidden="true">
        {/* Free-context track (light) under the gradient fill */}
        <path d={ARC_PATH} pathLength={100} fill="none" stroke="currentColor"
          className="text-gray-200 dark:text-gray-700" strokeWidth={STROKE} strokeLinecap="butt" />
        {gradientSlices(fill, isDark).map((s) => (
          <path key={s.from} d={ARC_PATH} pathLength={100} fill="none" stroke={s.color}
            className={over ? 'animate-pulse' : undefined}
            strokeWidth={STROKE} strokeLinecap="butt"
            strokeDasharray={`${s.len.toFixed(3)} ${(100 - s.len).toFixed(3)}`} strokeDashoffset={-s.from} />
        ))}
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
