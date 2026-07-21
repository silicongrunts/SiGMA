import { useCallback, useEffect, useRef } from 'react'

// Ctrl/Cmd + wheel zoom that keeps the cursor anchored on the same PDF point.
//
// Two robustness guards borrowed from how serious PDF readers handle the same
// input:
//   - A 5ms "is zooming" lock coalesces bursts of wheel events (trackpad pinch
//     fires many small-deltaY events per frame).
//   - A 100ms "is scrolling" window suppresses zoom when the user was already
//     free-scrolling and only pressed Ctrl mid-scroll, so the page does not
//     jump unexpectedly.
//
// The cursor-anchoring math: after applying the new scale, scroll the
// container by (mouseDelta * scaleFactor - mouseDelta) on each axis so the
// point under the cursor stays under the cursor.

// Cap how much a single wheel tick can scale. Trackpads emit small deltas
// (slow, controlled zoom) while mouse wheels can emit very large deltas on a
// flick (which would otherwise blow the scale up in one step).
const MAX_SCALE_FACTOR = 1.2
const SCALE_FACTOR_DIVISOR = 20
const SCALE_MIN = 0.2
const SCALE_MAX = 9.99

// When set, pdf.js first applies a CSS transform to the existing canvas and
// only re-rasterises after this delay. That eliminates the black flash that
// re-allocating the canvas backing store would otherwise cause on every tick.
const DRAWING_DELAY_MS = 100

/**
 * @param {object|null} host - PdfViewerHost instance (or null while booting).
 * @param {(newScale: number) => void} applyScale - receives the new numeric
 *   scale so the caller can sync it back to its source of truth.
 */
export function usePdfWheelZoom(host, applyScale) {
  const isZoomingRef = useRef(false)
  const isScrollingRef = useRef(false)
  const scrollTimerRef = useRef(null)
  const applyScaleRef = useRef(applyScale)
  useEffect(() => { applyScaleRef.current = applyScale }, [applyScale])

  const performZoom = useCallback((event) => {
    const viewer = host?.viewer
    if (!viewer) return

    const scrollMagnitude = Math.abs(event.deltaY)
    const magnitude = Math.min(1 + scrollMagnitude / SCALE_FACTOR_DIVISOR, MAX_SCALE_FACTOR)
    const direction = Math.sign(event.deltaY)
    const approximateFactor = direction < 0 ? magnitude : 1 / magnitude

    const previousScale = viewer.currentScale
    const newScale = Math.max(
      SCALE_MIN,
      Math.min(SCALE_MAX, Math.round(previousScale * approximateFactor * 100) / 100),
    )
    if (newScale === previousScale) return
    const requestedFactor = newScale / previousScale

    // Use the drawing-delay path so pdf.js scales the existing canvas with a
    // CSS transform first and re-rasterises shortly after, instead of clearing
    // and redrawing synchronously on every wheel event.
    if (newScale > previousScale) {
      viewer.increaseScale({ drawingDelay: DRAWING_DELAY_MS, scaleFactor: requestedFactor, steps: 1 })
    } else {
      viewer.decreaseScale({ drawingDelay: DRAWING_DELAY_MS, scaleFactor: requestedFactor, steps: 1 })
    }

    // pdf.js clamps to its own MIN/MAX_SCALE, so re-derive the effective
    // factor from the scale it actually applied — otherwise the cursor
    // re-anchoring math would drift near the limits.
    const appliedScale = viewer.currentScale
    if (appliedScale === previousScale) return
    const exactFactor = appliedScale / previousScale
    applyScaleRef.current?.(appliedScale)

    // Re-anchor the cursor: the point under the mouse must remain under the
    // mouse after the scale change.
    const container = host.container
    const rect = container.getBoundingClientRect()
    const mouseX = event.clientX - rect.left
    const mouseY = event.clientY - rect.top
    container.scrollBy({
      left: mouseX * exactFactor - mouseX,
      top: mouseY * exactFactor - mouseY,
      behavior: 'instant',
    })
  }, [host])

  useEffect(() => {
    if (!host) return
    const container = host.container

    const onWheel = (event) => {
      if (event.metaKey || event.ctrlKey) {
        if (isScrollingRef.current) return // mid-scroll Ctrl press: ignore
        event.preventDefault()
        if (isZoomingRef.current) return // coalesce burst
        isZoomingRef.current = true
        performZoom(event)
        setTimeout(() => { isZoomingRef.current = false }, 5)
      } else {
        isScrollingRef.current = true
        if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current)
        scrollTimerRef.current = setTimeout(() => { isScrollingRef.current = false }, 100)
      }
    }

    container.addEventListener('wheel', onWheel, { passive: false })
    return () => {
      container.removeEventListener('wheel', onWheel)
      if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current)
    }
  }, [host, performZoom])
}
