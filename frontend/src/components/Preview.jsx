import { useEffect, useRef, useState, useCallback, useImperativeHandle, forwardRef, memo } from 'react'
import { useTranslation } from 'react-i18next'
import * as pdfjsLib from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.js?url'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import 'highlight.js/styles/github-dark.css'
import DOMPurify from 'dompurify'
import renderMathInElement from 'katex/dist/contrib/auto-render.mjs'
import { useStore } from '../store/useStore'
import { filesAPI, compileAPI } from '../api'
import { storage } from '../utils/storage'
import { getCompiledPdfName } from '../utils/constants'
import { toastError } from './Toast'
import { ZoomIn, ZoomOut, ArrowUp, Maximize2, FileSearch, FileText, Download, AlertTriangle, ChevronLeft, ChevronRight } from 'lucide-react'

// Local worker (bundled, no CDN)
pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

// Extend the shared marked instance with syntax highlighting via highlight.js.
// Applied as a marked extension (not setOptions) so it stays local to this file's
// pipeline and does not alter token structure — ChatShared.jsx's own setOptions
// (gfm/breaks) still govern block parsing.
marked.use(markedHighlight({
  langPrefix: 'hljs language-',
  highlight(code, lang) {
    const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext'
    try {
      return hljs.highlight(code, { language }).value
    } catch {
      return code
    }
  },
}))

const PDFJS_ASSET_BASE = `${import.meta.env.BASE_URL || '/'}pdfjs/`

const PDFPage = memo(({ pdf, pageNumber, scale, onDoubleClick, isActive }) => {
  const canvasRef = useRef(null)
  const textLayerRef = useRef(null)
  const renderTaskRef = useRef(null)

  useEffect(() => {
    let isCancelled = false
    
    const renderPage = async () => {
      try {
        const page = await pdf.getPage(pageNumber)
        if (isCancelled) return
        
        const viewport = page.getViewport({ scale, rotation: page.rotate })
        const canvas = canvasRef.current
        if (!canvas) return
        
        // Use standard context for maximum stability across browsers
        const context = canvas.getContext('2d', { alpha: false })
        const dpr = window.devicePixelRatio || 1
        
        canvas.width = viewport.width * dpr
        canvas.height = viewport.height * dpr
        canvas.style.width = `${viewport.width}px`
        canvas.style.height = `${viewport.height}px`
        context.scale(dpr, dpr)
        
        // Cancel existing task if any
        if (renderTaskRef.current) {
            renderTaskRef.current.cancel()
        }
        
        renderTaskRef.current = page.render({ canvasContext: context, viewport })
        await renderTaskRef.current.promise
        
        if (isCancelled) return

        // Render Text Layer for selection
        if (textLayerRef.current) {
            const textLayer = textLayerRef.current
            textLayer.innerHTML = ''
            textLayer.style.width = `${viewport.width}px`
            textLayer.style.height = `${viewport.height}px`
            // Required by PDF.js 3+ CSS
            textLayer.style.setProperty('--scale-factor', scale)
            
            const textContent = await page.getTextContent()
            if (isCancelled) return
            
            await pdfjsLib.renderTextLayer({
                textContentSource: textContent,
                container: textLayer,
                viewport: viewport,
                textDivs: []
            }).promise
        }
      } catch (e) {
        if (e.name !== 'RenderingCancelledException') {
            console.error(`PDF Render Error [Page ${pageNumber}]:`, e)
        }
      }
    }

    renderPage()
    
    return () => { 
        isCancelled = true
        if (renderTaskRef.current) renderTaskRef.current.cancel()
    }
  }, [pdf, pageNumber, scale])

  return (
    <div 
      data-page-wrapper={pageNumber}
      className={`relative bg-white shadow-2xl mb-10 transition-all duration-300 select-text ${isActive ? 'ring-4 ring-blue-500/40' : ''}`}
      style={{ width: 'fit-content' }}
      onDoubleClick={(e) => {
          // Double-click for SyncTeX
          const rect = canvasRef.current.getBoundingClientRect()
          const x = (e.clientX - rect.left)
          const y = (e.clientY - rect.top)
          onDoubleClick(pageNumber, x / scale, y / scale)
      }}
    >
      <canvas ref={canvasRef} className="block shadow-inner pointer-events-none bg-white" />
      {/* The textLayer MUST be after canvas and NOT have pointer-events-none to allow selection */}
      <div ref={textLayerRef} className="textLayer absolute inset-0 overflow-hidden" dir="ltr" />
    </div>
  )
})

const Preview = forwardRef(({ onPageClick, onScroll }, ref) => {
  const { t } = useTranslation()
  const currentProjectId = useStore(state => state.currentProject?.id)
  const previewSource = useStore(state => state.previewSource)

  const zoomLevel = useStore(state => state.zoomLevel)
  const setZoomLevel = useStore(state => state.setZoomLevel)
  const compiling = useStore(state => state.compiling)

  // Render state is driven by the single load effect. For compiled PDFs, the
  // loaded document identity is the output PDF plus compileVersion, not the
  // currently open source file.
  const [pdf, setPdf] = useState(null)
  const [numPages, setNumPages] = useState(0)
  const [mdContent, setMdContent] = useState('')
  const [activePage, setActivePage] = useState(null)
  const [visiblePage, setVisiblePage] = useState(1)
  const [pageInput, setPageInput] = useState('')
  const [editingPage, setEditingPage] = useState(false)
  const pageInputRef = useRef(null)
  const [containerWidth, setContainerWidth] = useState(0)
  const [baseScale, setBaseScale] = useState(0)

  // Derived view model — title and render branch both flow from previewSource.
  const previewKind = previewSource.kind
  const previewPath = previewSource.path
  const previewOutputName = previewKind === 'pdf-compiled'
    ? (previewSource.outputName || getCompiledPdfName(previewSource.mainFile || previewPath))
    : null
  const previewLoadPath = previewKind === 'pdf-compiled'
    ? previewOutputName
    : previewPath
  const previewLoadVersion = previewKind === 'pdf-compiled'
    ? (previewSource.compileVersion || 0)
    : 0
  const previewStorageKey = previewKind === 'pdf-compiled'
    ? (previewOutputName || previewPath)
    : previewPath
  const type = previewKind === 'pdf-compiled' || previewKind === 'pdf-standalone'
    ? 'pdf'
    : previewKind === 'markdown' ? 'markdown' : 'none'

  const containerRef = useRef(null)
  const pdfInstanceRef = useRef(null)
  const currentPdfUrlRef = useRef(null)
  const headingMapRef = useRef([])  // [{srcLine, el?}] for scroll sync
  const blockMapRef = useRef([])    // [{startLine, endLine, el}] for precise highlight
  const highlightedElsRef = useRef([])
  const restoredScrollKeyRef = useRef('')
  const previewLoadTokenRef = useRef(0)

  // Clear every piece of render state. Called at the start of every load
  // effect run so identity changes always start from a clean slate.
  const resetRenderingState = useCallback(() => {
    previewLoadTokenRef.current += 1
    setPdf(null)
    setNumPages(0)
    setMdContent('')
    setActivePage(null)
    setVisiblePage(1)
    setPageInput('')
    setEditingPage(false)
    setBaseScale(0)
    headingMapRef.current = []
    blockMapRef.current = []
    highlightedElsRef.current = []
    restoredScrollKeyRef.current = ''
    if (currentPdfUrlRef.current) {
      URL.revokeObjectURL(currentPdfUrlRef.current)
      currentPdfUrlRef.current = null
    }
  }, [])

  // Track container width for "Fit Width" calculation
  useEffect(() => {
    if (!containerRef.current) return
    const observer = new ResizeObserver(entries => { 
        if(entries[0]) setContainerWidth(entries[0].contentRect.width) 
    })
    observer.observe(containerRef.current)
    return () => observer.disconnect()
  }, [])

  // Track which PDF page is currently visible
  useEffect(() => {
    if (type !== 'pdf' || !containerRef.current) return
    const container = containerRef.current

    const update = () => {
      const scrollTop = container.scrollTop
      const pages = container.querySelectorAll('[data-page-wrapper]')
      for (const page of pages) {
        // Page whose top is at or just past the scroll position
        if (page.offsetTop + page.offsetHeight > scrollTop + 10) {
          const n = parseInt(page.getAttribute('data-page-wrapper'), 10)
          if (n > 0) setVisiblePage(n)
          return
        }
      }
    }

    container.addEventListener('scroll', update, { passive: true })
    // Set initial page
    update()
    return () => container.removeEventListener('scroll', update)
  }, [type, pdf, numPages])

  // Navigate to a specific page by number
  const goToPage = useCallback((n) => {
    const clamped = Math.max(1, Math.min(numPages, n))
    const pageWrapper = containerRef.current?.querySelector(`[data-page-wrapper="${clamped}"]`)
    if (pageWrapper) {
      containerRef.current.scrollTo({ top: pageWrapper.offsetTop - 10, behavior: 'smooth' })
    }
  }, [numPages])

  // Calculate scaling factor
  useEffect(() => {
    const calcBase = async () => {
        if (!pdf || containerWidth <= 0) return
        try {
            const page = await pdf.getPage(1)
            const viewport = page.getViewport({ scale: 1.0, rotation: page.rotate })
            const padding = 80 // 40px each side
            setBaseScale((containerWidth - padding) / viewport.width)
        } catch (e) {
            console.error('PDF base scale failed:', e)
            toastError(t('preview.loadFailed'))
        }
    }
    if (type === 'pdf') calcBase()
    else if (type === 'markdown') setBaseScale(1.0)
  }, [pdf, containerWidth, type])

  const loadPDF = useCallback(async (url) => {
    const token = previewLoadTokenRef.current + 1
    previewLoadTokenRef.current = token
    try {
      // Background loading to prevent white/black flash
      const loadingTask = pdfjsLib.getDocument({
        url,
        cMapUrl: `${PDFJS_ASSET_BASE}cmaps/`,
        cMapPacked: true,
        standardFontDataUrl: `${PDFJS_ASSET_BASE}standard_fonts/`,
        useSystemFonts: true,
      })
      const pdfDoc = await loadingTask.promise
      if (token !== previewLoadTokenRef.current) {
        URL.revokeObjectURL(url)
        return
      }

      if (currentPdfUrlRef.current) URL.revokeObjectURL(currentPdfUrlRef.current)
      currentPdfUrlRef.current = url

      setPdf(pdfDoc)
      setNumPages(pdfDoc.numPages)
    } catch (e) {
      console.error("PDF Load Error:", e)
      if (e.name !== 'AbortException') { URL.revokeObjectURL(url); toastError(t('preview.loadFailed')) }
    }
  }, [])

  // ── Single load effect — the ONLY place that fetches preview content. ──
  // Source-file switches inside one compiled TeX project must not reload the
  // PDF; recompiles still reload by bumping previewLoadVersion.
  useEffect(() => {
    if (!currentProjectId) return
    const kind = previewKind
    const path = previewLoadPath
    resetRenderingState()
    if (kind === 'none' || !path) return

    let cancelled = false
    if (kind === 'markdown') {
      filesAPI.read(currentProjectId, path).then(data => {
        if (cancelled) return
        setMdContent(typeof data === 'string' ? data : (data?.content ?? ''))
      }).catch(() => { /* leave blank — shows 'no preview' */ })
    } else if (kind === 'pdf-compiled') {
      compileAPI.getPDF(currentProjectId, path).then(blob => {
        if (cancelled) return
        loadPDF(URL.createObjectURL(blob))
      }).catch(() => { /* not yet compiled — shows 'no preview' */ })
    } else if (kind === 'pdf-standalone') {
      compileAPI.getPDF(currentProjectId, path).then(blob => {
        if (cancelled) return
        loadPDF(URL.createObjectURL(blob))
      }).catch(() => {
        // Fallback: raw download
        filesAPI.download(currentProjectId, path).then(blob => {
          if (cancelled) return
          loadPDF(URL.createObjectURL(blob))
        }).catch(() => { /* file unreadable — shows 'no preview' */ })
      })
    }
    // binary-error needs no fetch — render branch shows download UI from path.

    return () => { cancelled = true }
  }, [
    previewKind,
    previewLoadPath,
    previewLoadVersion,
    currentProjectId,
    resetRenderingState,
    loadPDF,
  ])

  // Revoke any held object URL on unmount.
  useEffect(() => {
    return () => {
      if (currentPdfUrlRef.current) {
        URL.revokeObjectURL(currentPdfUrlRef.current)
        currentPdfUrlRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (type === 'markdown' && containerRef.current) {
        const el = containerRef.current.querySelector('.prose')
        if (el) renderMathInElement(el, { delimiters: [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}], throwOnError:false })
    }
  }, [mdContent, type, zoomLevel])

  useEffect(() => {
    if (!currentProjectId || !previewStorageKey || type === 'none') return
    if (type === 'pdf' && (!pdf || numPages === 0)) return
    if (type === 'markdown' && !mdContent) return
    const restoreKey = `${currentProjectId}:${previewStorageKey}:${type}`
    if (restoredScrollKeyRef.current === restoreKey) return
    restoredScrollKeyRef.current = restoreKey
    const ratio = storage.getSynthesis(currentProjectId).previewScrollRatioByFile[previewStorageKey]
    if (!Number.isFinite(ratio)) return
    requestAnimationFrame(() => {
      const container = containerRef.current
      if (!container) return
      const maxScroll = container.scrollHeight - container.clientHeight
      if (maxScroll > 0) container.scrollTop = Math.max(0, Math.min(1, ratio)) * maxScroll
    })
  }, [currentProjectId, previewStorageKey, type, pdf, numPages, mdContent])

  // Build heading map for editor↔preview scroll sync.
  // Pairs source line numbers with DOM heading elements so scrollToLine
  // can interpolate positions between headings accurately.
  useEffect(() => {
    if (type !== 'markdown' || !mdContent || !containerRef.current) {
      headingMapRef.current = []
      return
    }
    const lines = mdContent.split('\n')
    let inCode = false
    const srcLines = []
    lines.forEach((line, i) => {
      if (line.trimStart().startsWith('```')) { inCode = !inCode; return }
      if (!inCode && /^#{1,6}\s/.test(line)) srcLines.push(i)
    })
    const domHeadings = containerRef.current.querySelectorAll('.prose h1,h2,h3,h4,h5,h6')
    const count = Math.min(srcLines.length, domHeadings.length)
    const map = [{ srcLine: 0, el: null }]  // sentinel: top of document
    for (let i = 0; i < count; i++) {
      map.push({ srcLine: srcLines[i], el: domHeadings[i] })
    }
    map.push({ srcLine: lines.length - 1, el: null })  // sentinel: bottom
    headingMapRef.current = map
  }, [mdContent, type])

  // Build precise block map using marked.lexer() + indexOf character tracking.
  // Each token's raw text is located in the source via indexOf, then converted
  // to line numbers via a precomputed line-offset table. This avoids the drift
  // that comes from counting lines via split('\n').
  useEffect(() => {
    // Clear previous highlight
    for (const el of highlightedElsRef.current) el.classList.remove('md-highlight')
    highlightedElsRef.current = []

    if (type !== 'markdown' || !mdContent || !containerRef.current) {
      blockMapRef.current = []
      return
    }
    try {
      // Precompute: character offset where each source line starts
      const lineStarts = [0]
      for (let i = 0; i < mdContent.length; i++) {
        if (mdContent[i] === '\n') lineStarts.push(i + 1)
      }
      const lineAtOffset = (offset) => {
        let lo = 0, hi = lineStarts.length - 1
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1
          if (lineStarts[mid] <= offset) lo = mid
          else hi = mid - 1
        }
        return lo
      }

      const tokens = marked.lexer(mdContent)
      const domBlocks = containerRef.current.querySelectorAll('.prose > *')
      let searchOffset = 0
      const map = []

      for (const token of tokens) {
        const raw = token.raw
        if (!raw) continue
        const idx = mdContent.indexOf(raw, searchOffset)
        if (idx === -1) continue
        const startLine = lineAtOffset(idx)
        const endLine = lineAtOffset(idx + raw.length - (raw.endsWith('\n') ? 1 : 0))
        searchOffset = idx + raw.length

        if (token.type !== 'space') {
          const domIdx = map.length  // maps 1:1 with non-space tokens
          if (domIdx < domBlocks.length) {
            map.push({ startLine, endLine, el: domBlocks[domIdx] })
          }
        }
      }

      // Validation: non-space token count must match DOM block count
      blockMapRef.current = (map.length === domBlocks.length) ? map : []
    } catch {
      blockMapRef.current = []
    }
  }, [mdContent, type])

  useImperativeHandle(ref, () => ({
    // Compile success → bump previewSource.compileVersion; the load effect
    // re-fetches the PDF. Never bypasses previewSource (no hidden blob path).
    showCompiledPDF: () => {
      useStore.getState().bumpCompileVersion()
    },
    // Live markdown preview while the user types. Strictly guarded: only
    // applies when the current previewSource is exactly this markdown file.
    // Does NOT change identity — title remains correct, only content refreshes.
    setMarkdownContent: (content) => {
      const s = useStore.getState()
      const ps = s.previewSource
      const cur = s.currentFile
      if (ps.kind !== 'markdown' || ps.path !== cur) return
      setMdContent(content ?? '')
    },
    scrollToPage: (n, x, y) => {
      const pageWrapper = containerRef.current?.querySelector(`[data-page-wrapper="${n}"]`)
      if (pageWrapper) {
        const finalScale = baseScale * zoomLevel
        const top = pageWrapper.offsetTop + (y * finalScale) - 100
        containerRef.current.scrollTo({ top, behavior: 'smooth' })
        setActivePage(n)

        const indicator = document.createElement('div')
        indicator.className = 'synctex-indicator'
        indicator.style.left = `${x * finalScale}px`
        indicator.style.top = `${y * finalScale}px`
        pageWrapper.appendChild(indicator)

        setTimeout(() => { indicator.remove(); setActivePage(null); }, 1500)
      }
    },
    scrollToPercent: (pct) => {
      if (containerRef.current) {
        const maxScroll = containerRef.current.scrollHeight - containerRef.current.clientHeight
        containerRef.current.scrollTop = Math.max(0, pct * maxScroll)
      }
    },
    scrollToLine: (line) => {
      const container = containerRef.current
      if (!container) return
      const n = line - 1  // CodeMirror lines are 1-indexed, source array is 0-indexed
      const map = headingMapRef.current
      // Fewer than 3 entries means no real headings — fall back to percentage
      if (map.length < 3) {
        const totalLines = (mdContent || '').split('\n').length
        const maxScroll = container.scrollHeight - container.clientHeight
        container.scrollTop = (n / Math.max(1, totalLines - 1)) * maxScroll
        return
      }
      // Binary search for the enclosing heading range
      let lo = 0, hi = map.length - 1
      while (lo < hi - 1) {
        const mid = (lo + hi) >> 1
        if (map[mid].srcLine <= n) lo = mid
        else hi = mid
      }
      const from = map[lo], to = map[hi]
      // Compute DOM positions on-demand (always correct after zoom/KaTeX changes)
      const containerRect = container.getBoundingClientRect()
      const fromTop = from.el
        ? from.el.getBoundingClientRect().top - containerRect.top + container.scrollTop
        : 0
      const toTop = to.el
        ? to.el.getBoundingClientRect().top - containerRect.top + container.scrollTop
        : container.scrollHeight
      // Interpolate
      const lineRange = to.srcLine - from.srcLine
      if (lineRange <= 0 || n <= from.srcLine) { container.scrollTop = fromTop; return }
      if (n >= to.srcLine) { container.scrollTop = toTop; return }
      container.scrollTop = fromTop + ((n - from.srcLine) / lineRange) * (toTop - fromTop)
    },
    highlightLine: (line) => {
      // Clear previous highlights
      for (const el of highlightedElsRef.current) el.classList.remove('md-highlight')
      highlightedElsRef.current = []

      const n = line - 1  // 1-indexed → 0-indexed
      const container = containerRef.current
      if (!container) return

      // Primary: use precise block map (indexOf-based, validated)
      const blocks = blockMapRef.current
      if (blocks.length > 0) {
        for (const block of blocks) {
          if (n >= block.startLine && n <= block.endLine) {
            block.el.classList.add('md-highlight')
            highlightedElsRef.current = [block.el]
            return
          }
        }
      }

      // Fallback: heading-section + height-weighted (when block map unavailable)
      const map = headingMapRef.current
      if (map.length < 2) return

      let lo = 0, hi = map.length - 1
      while (lo < hi - 1) {
        const mid = (lo + hi) >> 1
        if (map[mid].srcLine <= n) lo = mid
        else hi = mid
      }

      const mdBody = container.querySelector('.prose')
      if (!mdBody) return
      const sectionEls = []
      let startEl = map[lo].el || mdBody.firstElementChild
      const endEl = map[hi].el
      let el = startEl
      while (el && el !== endEl) {
        sectionEls.push(el)
        el = el.nextElementSibling
      }
      if (sectionEls.length === 0) return

      const target = sectionEls.length === 1 ? sectionEls[0] : (() => {
        const heights = sectionEls.map(e => e.offsetHeight)
        const totalHeight = heights.reduce((a, b) => a + b, 0)
        const srcRange = map[hi].srcLine - map[lo].srcLine
        const ratio = srcRange > 0 ? (n - map[lo].srcLine) / srcRange : 0
        let acc = 0
        for (let i = 0; i < sectionEls.length; i++) {
          acc += heights[i]
          if (acc >= ratio * totalHeight || i === sectionEls.length - 1) return sectionEls[i]
        }
        return sectionEls[sectionEls.length - 1]
      })()

      target.classList.add('md-highlight')
      highlightedElsRef.current = [target]
    }
  }))

  const handleWheel = useCallback((e) => {
    if (e.ctrlKey || e.metaKey) {
        e.preventDefault(); e.stopPropagation();
        const delta = e.deltaY > 0 ? -0.1 : 0.1
        setZoomLevel(Math.max(0.2, Math.min(4, zoomLevel + delta)))
    }
  }, [zoomLevel, setZoomLevel])

  useEffect(() => {
    const el = containerRef.current
    if (el) el.addEventListener('wheel', handleWheel, { passive: false })
    return () => el?.removeEventListener('wheel', handleWheel)
  }, [handleWheel])

  const finalScale = baseScale * zoomLevel

  // Pure derivation — title and content share the same source (previewSource),
  // so they cannot disagree by construction.
  const displayTitle = (() => {
    if (!previewPath) return t('preview.title')
    const base = previewPath.split('/').pop()
    if (previewKind === 'pdf-compiled') return previewOutputName || base.replace(/\.tex$/, '.pdf')
    return base
  })()

  const binaryErrorPath = previewKind === 'binary-error' ? previewPath : null
  const binaryErrorName = binaryErrorPath ? binaryErrorPath.split('/').pop() : null

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-[#f3f4f6] dark:bg-gray-900 relative group">
      <div className="h-8 border-b border-gray-100 dark:border-gray-800 px-4 flex items-center justify-between bg-white dark:bg-gray-900 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <FileText className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 flex-shrink-0" />
          <span className="text-xs font-bold text-gray-600 dark:text-gray-400 truncate">{displayTitle}</span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0 ml-2">
          {type === 'pdf' && numPages > 0 && (
            <div className="flex items-center gap-0.5 bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 px-0.5 py-0">
              <button
                onClick={() => goToPage(visiblePage - 1)}
                disabled={visiblePage <= 1}
                className="p-0.5 text-gray-400 dark:text-gray-500 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
              </button>
              {editingPage ? (
                <input
                  ref={pageInputRef}
                  type="text"
                  value={pageInput}
                  onChange={e => setPageInput(e.target.value.replace(/\D/g, ''))}
                  onBlur={() => {
                    const n = parseInt(pageInput, 10)
                    if (n >= 1 && n <= numPages) goToPage(n)
                    setEditingPage(false)
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      const n = parseInt(pageInput, 10)
                      if (n >= 1 && n <= numPages) goToPage(n)
                      setEditingPage(false)
                    } else if (e.key === 'Escape') {
                      setEditingPage(false)
                    }
                  }}
                  className="w-7 text-center text-[11px] font-bold font-mono text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded outline-none focus:border-sigma-500 focus:ring-1 focus:ring-sigma-500/30"
                  autoFocus
                />
              ) : (
                <button
                  onClick={() => { setPageInput(String(visiblePage)); setEditingPage(true) }}
                  className="min-w-[18px] text-center text-[11px] leading-none py-0 font-bold font-mono text-gray-600 dark:text-gray-400 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 px-0.5 rounded transition-colors"
                  title={t('preview.jumpToPage')}
                >
                  {visiblePage}
                </button>
              )}
              <span className="text-[11px] leading-none font-medium text-gray-300 dark:text-gray-600">/</span>
              <span className="text-[11px] leading-none font-medium text-gray-400 dark:text-gray-500 pr-0.5">{numPages}</span>
              <button
                onClick={() => goToPage(visiblePage + 1)}
                disabled={visiblePage >= numPages}
                className="p-0.5 text-gray-400 dark:text-gray-500 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
              >
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
          <div className="bg-blue-50 dark:bg-sigma-600/20 text-blue-600 dark:text-sigma-400 px-2 py-0.5 rounded font-mono text-[9px] border border-blue-100 dark:border-sigma-600/30">{Math.round(zoomLevel * 100)}%</div>
        </div>
      </div>

      <div className="absolute bottom-8 right-8 z-30 flex flex-col gap-2 opacity-0 group-hover:opacity-100 transition-all duration-300 transform translate-y-2 group-hover:translate-y-0">
        <div className="bg-white/90 dark:bg-gray-800/90 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-2xl rounded-2xl p-1.5 flex flex-col gap-1 text-gray-800 dark:text-gray-200">
            <button onClick={() => setZoomLevel(Math.min(4, zoomLevel + 0.1))} className="p-2.5 hover:bg-blue-50 dark:hover:bg-sigma-600/20 text-gray-600 dark:text-gray-400 hover:text-blue-600 dark:hover:text-sigma-400 rounded-xl transition-all shadow-sm"><ZoomIn className="w-5 h-5" /></button>
            <button onClick={() => setZoomLevel(Math.max(0.2, zoomLevel - 0.1))} className="p-2.5 hover:bg-blue-50 dark:hover:bg-sigma-600/20 text-gray-600 dark:text-gray-400 hover:text-blue-600 dark:hover:text-sigma-400 rounded-xl transition-all shadow-sm"><ZoomOut className="w-5 h-5" /></button>
            {type === 'pdf' && <button onClick={() => setZoomLevel(1.0)} className="p-2.5 hover:bg-blue-50 dark:hover:bg-sigma-600/20 text-gray-600 dark:text-gray-400 hover:text-blue-600 dark:hover:text-sigma-400 rounded-xl transition-all shadow-sm" title={t('preview.fitWidth')}><Maximize2 className="w-5 h-5" /></button>}
            <div className="h-px bg-gray-100 dark:bg-gray-700 mx-2 my-1" />
            <button onClick={() => containerRef.current?.scrollTo({top:0, behavior:'smooth'})} className="p-2.5 hover:bg-blue-50 dark:hover:bg-sigma-600/20 text-gray-600 dark:text-gray-400 hover:text-blue-600 dark:hover:text-sigma-400 rounded-xl transition-all shadow-sm"><ArrowUp className="w-5 h-5" /></button>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={(e) => {
          const el = e.currentTarget
          const maxScroll = el.scrollHeight - el.clientHeight
          if (maxScroll > 0) onScroll?.(el.scrollTop / maxScroll)
        }}
        className="flex-1 overflow-auto relative scroll-smooth flex flex-col"
      >
        {type === 'pdf' && pdf && baseScale > 0 ? (
          <div className="py-12 px-8 flex flex-col items-center min-w-full w-fit m-auto transition-opacity duration-500 min-h-full">
            {Array.from({ length: numPages }, (_, i) => i + 1).map(n => (
              <PDFPage key={n} pdf={pdf} pageNumber={n} scale={finalScale} onDoubleClick={onPageClick} isActive={activePage === n} />
            ))}
          </div>
        ) : type === 'markdown' ? (
            <div className="flex flex-col items-center w-full m-auto py-12 px-8">
                <div
                    className="prose dark:prose-invert max-w-none prose-pre:font-mono prose-code:font-mono prose-code:before:hidden prose-code:after:hidden bg-white dark:bg-gray-800 p-12 mx-auto shadow-2xl border border-gray-200 dark:border-gray-700 mb-12"
                    style={{ maxWidth: '48rem', width: '100%', fontSize: `${zoomLevel}rem` }}
                    dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(marked.parse(String(mdContent ?? ''))) }}
                />
            </div>
        ) : binaryErrorPath ? (
          <div className="flex flex-col items-center justify-center m-auto text-gray-400 dark:text-gray-500 px-8">
            <AlertTriangle className="w-16 h-12 mb-4 text-amber-300 dark:text-amber-500/50" />
            <p className="text-sm font-bold text-gray-600 dark:text-gray-300 mb-2">{t('preview.cannotPreview')}</p>
            <p className="text-xs text-gray-400 dark:text-gray-500 mb-4 text-center">{t('preview.cannotPreviewDesc')}</p>
            <a
              href={`/api/v1/files/${encodeURIComponent(currentProjectId)}/download?path=${encodeURIComponent(binaryErrorPath)}`}
              download={binaryErrorName}
              className="bg-sigma-600 hover:bg-sigma-700 text-white px-5 py-2.5 rounded-xl flex items-center gap-2 text-sm font-bold transition-all shadow-lg shadow-blue-100 dark:shadow-none active:scale-95"
            >
              <Download className="w-4 h-4" /> {t('preview.downloadName', { name: binaryErrorName })}
            </a>
          </div>
        ) : (!compiling && (
          <div className="flex flex-col items-center justify-center m-auto text-gray-300 dark:text-gray-600">
            <FileSearch className="w-16 h-12 mb-4 opacity-20" />
            <p className="text-[10px] font-black tracking-[0.3em] uppercase">{t('preview.noPreview')}</p>
          </div>
        ))}
      </div>
    </div>
  )
})

Preview.displayName = 'Preview'
export default memo(Preview)
