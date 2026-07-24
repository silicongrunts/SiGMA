import { useCallback, useEffect, useRef, useState } from 'react'
import { PdfViewerHost } from './pdfViewerHost'
import { usePdfWheelZoom } from './usePdfWheelZoom'

// Horizontal padding reserved around the page so it does not touch the
// container edges (matches the previous renderer's visual breathing room).
const PAGE_SIDE_PADDING = 80

// SyncTeX highlight pulse duration — mirrors the previous behaviour so the
// forward-jump indicator behaves the same to the user.
const SYNCTEX_INDICATOR_MS = 1500

// Breathing room kept around the SyncTeX target so the highlight isn't flush
// against the viewport edge. Matches the markdown side (Preview.jsx).
const SCROLL_MARGIN = 12

/**
 * Scroll the PDF scroll container by the minimum amount needed to bring
 * `targetTop` (a container-content pixel offset, NOT a scrollTop) into view.
 *
 * The three branches are mutually exclusive and exhaustive, so a given state
 * always yields the same delta — callers re-invoke on every forward jump, so
 * the branches MUST be idempotent (a structure flipping between two positions
 * would oscillate forever). Mirrors Preview.jsx's scrollIntoViewMinimal intent
 * but works on a bare offset instead of a DOM element rect.
 */
function scrollContainerToContainerOffset(container, targetTop) {
  const viewportH = container.clientHeight
  const curTop = container.scrollTop
  const margin = SCROLL_MARGIN
  if (targetTop >= curTop + margin && targetTop <= curTop + viewportH - margin) {
    return // already visible — do nothing (minimal scroll)
  }
  if (targetTop < curTop + margin) {
    // above the visible band — bring it to the top margin
    container.scrollTop = targetTop - margin
  } else {
    // below the visible band — bring it to the bottom margin
    container.scrollTop = targetTop - viewportH + margin
  }
}

/**
 * PDF preview pane built on pdf.js's PDFViewer.
 *
 * Responsibilities:
 *   - Construct and tear down the PdfViewerHost once per mounted container.
 *   - Load (and reload, after recompiles) the PDFDocumentProxy without a
 *     black flash (the host pins document height during the swap).
 *   - Translate the global zoomLevel (1.0 = fit-to-width) into pdf.js's
 *     numeric scale and back, so the toolbar percentage and the wheel zoom
 *     stay in sync.
 *   - Forward SyncTeX double-clicks and expose imperative navigation
 *     (scrollToPage / goToPage / currentPageNumber) to the parent via
 *     `registerApi`.
 *
 * The component owns no global state: zoomLevel and every callback come in as
 * props, which keeps Preview.jsx the single read/write point for the store.
 */
export default function PdfPreview({
  pdfDoc,
  zoomLevel,
  onZoomChange,
  onPageClick,
  onScrollRatio,
  onPageCount,
  onPageChange,
  containerWidth,
  registerApi,
}) {
  const containerRef = useRef(null) // the absolutely-positioned host container
  // host is held in STATE (not a ref) so that creating it triggers a re-render
  // and the dependent effects below (wheel zoom, page/scroll listeners, etc.)
  // re-bind to the real host. A ref would set silently and leave those effects
  // bound to null forever.
  const [host, setHost] = useState(null)
  const hostRef = useRef(null)
  useEffect(() => { hostRef.current = host }, [host])
  // Pending SyncTeX indicator auto-remove timers. Tracked so unmount (or a
  // document swap) can cancel them instead of firing on a detached node.
  const synctexTimersRef = useRef([])
  useEffect(() => () => {
    for (const id of synctexTimersRef.current) clearTimeout(id)
    synctexTimersRef.current = []
  }, [])
  // fit-to-width scale for page 1 at the current container width. Kept in a
  // ref (not state) because scale application is imperative and does not need
  // to trigger a re-render.
  const baseScaleRef = useRef(0)
  const zoomLevelRef = useRef(zoomLevel)
  useEffect(() => { zoomLevelRef.current = zoomLevel }, [zoomLevel])

  // Apply the current zoomLevel to pdf.js, derived from the fit-to-width base
  // scale. Defined before the effects that use it so the ordering is obvious.
  const applyZoomScale = useCallback(() => {
    const current = hostRef.current
    if (!current || baseScaleRef.current <= 0) return
    const target = baseScaleRef.current * zoomLevelRef.current
    if (Math.abs(current.viewer.currentScale - target) > 1e-6) {
      current.viewer.currentScale = target
    }
  }, [])

  // ── 1. Construct the host exactly once per mount ──────────────────────
  useEffect(() => {
    if (!containerRef.current) return
    const created = new PdfViewerHost(containerRef.current)
    setHost(created)
    return () => {
      created.cleanup()
      setHost(null)
    }
  }, [])

  // ── 2. Compute baseScale (fit-to-width) from page 1 ───────────────────
  useEffect(() => {
    if (!pdfDoc || containerWidth <= 0) return
    let cancelled = false
    pdfDoc.getPage(1).then(page => {
      if (cancelled) return
      const viewport = page.getViewport({ scale: 1 })
      const base = Math.max(0.1, (containerWidth - PAGE_SIDE_PADDING) / viewport.width)
      baseScaleRef.current = base
      applyZoomScale()
    }).catch(() => { /* leave previous base scale */ })
    return () => { cancelled = true }
  }, [pdfDoc, containerWidth, applyZoomScale])

  // ── 3. Load the document whenever the PDFDocumentProxy changes ────────
  useEffect(() => {
    if (!host || !pdfDoc) return
    onPageCount?.(pdfDoc.numPages || 0)

    host.loadDocument(pdfDoc)

    // pagesinit fires when the first page's data is ready and pdf.js has
    // laid out all page placeholders. Re-apply the scale synchronously so the
    // first paint uses our fit-to-width scale rather than pdf.js's default.
    const onPagesInit = () => applyZoomScale()
    host.eventBus.on('pagesinit', onPagesInit)
    return () => host.eventBus.off('pagesinit', onPagesInit)
  }, [host, pdfDoc, applyZoomScale, onPageCount])

  // ── 4. Re-apply scale when zoomLevel changes (toolbar buttons) ────────
  useEffect(() => {
    applyZoomScale()
  }, [zoomLevel, applyZoomScale])

  // ── 5. Expose imperative navigation to the parent ─────────────────────
  // The closures read hostRef.current at call time, so a single registration
  // always dispatches to the live host without re-registering on every swap.
  useEffect(() => {
    registerApi?.({
      scrollToPage: (pageNumber, x, y) => {
        const current = hostRef.current
        if (!current) return
        const offset = current.pointToContainerOffset(pageNumber, x, y)
        if (offset) scrollContainerToContainerOffset(current.container, offset.top)
        showSynctexIndicator(pageNumber, x, y)
      },
      goToPage: (pageNumber) => {
        const viewer = hostRef.current?.viewer
        if (!viewer) return
        if (pageNumber >= 1 && pageNumber <= viewer.pagesCount) {
          viewer.currentPageNumber = pageNumber
        }
      },
      scrollToRatio: (ratio) => {
        const c = hostRef.current?.container
        if (!c) return
        const max = c.scrollHeight - c.clientHeight
        if (max > 0) c.scrollTop = Math.max(0, Math.min(1, ratio)) * max
      },
      getCurrentPage: () => hostRef.current?.viewer.currentPageNumber ?? 1,
      getPageCount: () => hostRef.current?.viewer.pagesCount ?? 0,
    })
  }, [registerApi])

  // ── 6. Ctrl + wheel cursor-anchored zoom ──────────────────────────────
  const handleScaleApplied = useCallback((newScale) => {
    if (baseScaleRef.current <= 0) return
    onZoomChange?.(newScale / baseScaleRef.current)
  }, [onZoomChange])
  usePdfWheelZoom(host, handleScaleApplied)

  // ── 7. Track current page + scroll ratio ──────────────────────────────
  useEffect(() => {
    if (!host) return
    const onPageChanging = (evt) => onPageChange?.(evt.pageNumber)
    host.eventBus.on('pagechanging', onPageChanging)
    return () => host.eventBus.off('pagechanging', onPageChanging)
  }, [host, onPageChange])

  useEffect(() => {
    if (!host) return
    let raf = 0
    const onScroll = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        const c = host.container
        const max = c.scrollHeight - c.clientHeight
        if (max > 0) onScrollRatio?.(c.scrollTop / max)
      })
    }
    host.container.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      host.container.removeEventListener('scroll', onScroll)
      cancelAnimationFrame(raf)
    }
  }, [host, onScrollRatio])

  // ── 8. SyncTeX backward: double-click on the text layer ───────────────
  useEffect(() => {
    if (!host) return
    const onTextLayerRendered = (evt) => {
      const textLayerDiv = evt.source?.textLayerDiv ?? evt.source?.textLayer?.div
      if (!textLayerDiv || textLayerDiv.dataset.sigmaSynctexBound) return
      textLayerDiv.dataset.sigmaSynctexBound = '1'
      textLayerDiv.addEventListener('dblclick', (event) => {
        const pageIndex = evt.pageNumber - 1
        const point = host.clientToPdfPoint(pageIndex, event.clientX, event.clientY)
        if (point) onPageClick?.(evt.pageNumber, point.x, point.y)
      })
    }
    host.eventBus.on('textlayerrendered', onTextLayerRendered)
    return () => host.eventBus.off('textlayerrendered', onTextLayerRendered)
  }, [host, onPageClick])

  // Drop a transient pulse marker at a PDF-space coordinate (SyncTeX forward
  // search result). Appended onto the page's div and removed after the pulse.
  // The incoming (x, y) uses the SyncTeX "top-of-page" convention; convert to
  // pdf.js's bottom-origin PDF space before projecting to viewport pixels.
  const showSynctexIndicator = useCallback((pageNumber, pdfX, pdfYTop) => {
    const host = hostRef.current
    if (!host) return
    const pageView = host.viewer.getPageView(pageNumber - 1)
    if (!pageView?.viewport || !pageView?.div) return
    const viewBox = pageView.viewport.viewBox // [xMin, yMin, xMax, yMax] in PDF units
    const pdfYBottom = viewBox[3] - pdfYTop
    const [vx, vy] = pageView.viewport.convertToViewportPoint(pdfX, pdfYBottom)
    const indicator = document.createElement('div')
    indicator.className = 'synctex-indicator'
    indicator.style.left = `${vx}px`
    indicator.style.top = `${vy}px`
    pageView.div.appendChild(indicator)
    const timer = setTimeout(() => {
      indicator.remove()
      synctexTimersRef.current = synctexTimersRef.current.filter((id) => id !== timer)
    }, SYNCTEX_INDICATOR_MS)
    synctexTimersRef.current.push(timer)
  }, [])

  return (
    <div className="absolute inset-0">
      {/*
        pdf.js requires the scrolling container (the one passed to PDFViewer)
        to be absolutely positioned; the wrapper above gives it a relative
        positioning context and fills the pane.
      */}
      <div
        ref={containerRef}
        className="absolute inset-0 overflow-auto bg-[#f3f4f6] dark:bg-gray-900"
      >
        {/* PDFViewer injects page <div>s into this element. */}
        <div className="pdfViewer" />
      </div>
    </div>
  )
}
