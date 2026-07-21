import * as pdfjsLib from 'pdfjs-dist'
import { PDFViewer, PDFLinkService, EventBus, LinkTarget } from 'pdfjs-dist/web/pdf_viewer.js'

// Path to pdf.js annotation/image icons, served from the bundle root.
// cmaps and standard fonts are served by the vite pdfjsAssetsPlugin.
const IMAGE_RESOURCES_PATH = `${import.meta.env.BASE_URL || '/'}pdfjs/images/`

// Upper bound for a single canvas backing store. pdf.js re-rasterises pages
// below this size for crisp text; above it, it falls back to CSS scaling so
// huge zooms never allocate unbounded pixel buffers.
const MAX_CANVAS_PIXELS = 8192 * 8192

/**
 * Thin imperative wrapper around pdf.js's PDFViewer.
 *
 * Owns the PDFViewer / PDFLinkService / EventBus trio and exposes the few
 * operations the React layer needs: load/swap a document without a black
 * flash, jump to a PDF-space coordinate (SyncTeX forward), and convert a
 * double-click back to PDF-space coordinates (SyncTeX backward). It is
 * deliberately framework-free so it can be reasoned about and cleaned up
 * independently of React's lifecycle.
 */
export class PdfViewerHost {
  constructor(container) {
    this.container = container

    this.eventBus = new EventBus()

    this.linkService = new PDFLinkService({
      eventBus: this.eventBus,
      externalLinkTarget: LinkTarget.BLANK,
      externalLinkRel: 'noopener',
    })

    this.viewer = new PDFViewer({
      container,
      eventBus: this.eventBus,
      linkService: this.linkService,
      imageResourcesPath: IMAGE_RESOURCES_PATH,
      maxCanvasPixels: MAX_CANVAS_PIXELS,
      // Render annotations (links etc.) but keep them non-interactive-form;
      // SiGMA does not edit PDFs in place.
      annotationMode: pdfjsLib.AnnotationMode.ENABLE,
      annotationEditorMode: pdfjsLib.AnnotationEditorType.DISABLE,
      textLayerMode: 1, // TextLayerMode.ENABLE — required for selection + SyncTeX
    })

    this.linkService.setViewer(this.viewer)
  }

  /**
   * Load (or replace) a PDFDocumentProxy.
   *
   * pdf.js's setDocument synchronously clears existing page views, which would
   * collapse the scroll container and flash black while the new pages are
   * being initialised. To keep the viewport steady, we pin the current
   * document height with a min-height override and release it on pagesinit
   * (fired once the first page of the new document is ready).
   */
  loadDocument(pdfDocument) {
    const viewerEl = this.viewer.viewer // the inner .pdfViewer element
    if (viewerEl) {
      const prevHeight = viewerEl.getBoundingClientRect().height
      if (prevHeight > 0) {
        viewerEl.style.minHeight = `${prevHeight}px`
        const release = () => {
          viewerEl.style.minHeight = ''
          this.eventBus.off('pagesinit', release)
        }
        this.eventBus.on('pagesinit', release)
      }
    }

    this.viewer.setDocument(pdfDocument)
    this.linkService.setDocument(pdfDocument)
  }

  /**
   * Jump to a PDF-space coordinate. pdf.js's "XYZ" destination takes (x, y)
   * measured from the bottom-left of the page in PDF units, which matches the
   * convention the SyncTeX forward-search result already uses.
   */
  scrollToPdfPoint(pageNumber, x, y) {
    this.viewer.scrollPageIntoView({
      pageNumber,
      destArray: [null, { name: 'XYZ' }, x, y, null],
    })
  }

  /**
   * Convert a double-click's viewport pixel position to PDF-space coordinates
   * for backward SyncTeX search. The backend synctex edit expects (x, y) with
   * y measured from the top of the page in PDF units.
   */
  clientToPdfPoint(pageIndex, clientX, clientY) {
    const pageView = this.viewer.getPageView(pageIndex)
    if (!pageView?.div || !pageView?.viewport) return null
    const rect = pageView.div.getBoundingClientRect()
    const dx = clientX - rect.left
    const dy = clientY - rect.top
    const viewport = pageView.viewport
    const [px, py] = viewport.convertToPdfPoint(dx, dy)
    // convertToPdfPoint returns y measured from the bottom-left; convert to
    // top-left to match the backend's synctex edit convention.
    return { x: px, y: viewport.viewBox[3] - py }
  }

  cleanup() {
    try {
      this.viewer.cleanup()
    } catch {
      // best-effort cleanup during teardown
    }
  }
}
