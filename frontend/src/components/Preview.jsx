import { useEffect, useRef, useState, useCallback, useImperativeHandle, forwardRef, memo } from 'react'
import { useTranslation } from 'react-i18next'
import * as pdfjsLib from 'pdfjs-dist'
import { Marked } from 'marked'
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
import { rewriteProjectImageSrc } from './ChatShared'
import { extractMath, restoreMath, applyMathOverflow } from '../utils/mathGuard'
import PdfPreview from './preview/PdfPreview'
import { ZoomIn, ZoomOut, ArrowUp, Maximize2, FileSearch, FileText, Download, AlertTriangle, ChevronLeft, ChevronRight } from 'lucide-react'

// Dedicated marked instance for the editor preview pipeline, fully isolated
// from the shared global `marked` singleton that ChatShared.jsx configures.
// Using `new Marked(...)` ensures the highlight.js renderer + gfm/breaks
// options below do NOT leak into chat/annotation/diff rendering (which all go
// through ChatShared's MarkdownContent). Lexer and parser share this instance
// so the editor↔preview block map (which relies on marked.lexer token offsets)
// stays consistent with the rendered HTML.
const previewMarked = new Marked(
  { gfm: true, breaks: true },
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext'
      try {
        return hljs.highlight(code, { language }).value
      } catch {
        return code
      }
    },
  }),
)

// Block-level HTML containers whose marked output NESTS inner blocks instead
// of emitting them as siblings. E.g. `<details><summary>..</summary> <p>..</p>
// <ul>..</ul> </details>` renders as a single <details> direct child of .prose
// with <p>/<ul> as descendants — but marked.lexer still tokenizes the inner
// <p>/<ul> as separate top-level tokens. That mismatch breaks the lexer↔DOM
// 1:1 block alignment the highlight map relies on. `foldContainers` collapses
// each container's open-tag … close-tag token range into ONE synthetic token
// so it maps to the single DOM element it renders to.
const HTML_CONTAINER_TAGS = ['details', 'figure', 'dialog', 'table']
const HTML_CONTAINER_OPEN_RE = new RegExp(`^\\s*<(${HTML_CONTAINER_TAGS.join('|')})([\\s>])`, 'i')

function tagRe(tag) { return new RegExp(`</${tag}\\s*>`, 'gi') }
function openTagRe(tag) { return new RegExp(`<${tag}\\b`, 'gi') }

function foldContainers(tokens) {
  const out = []
  let i = 0
  while (i < tokens.length) {
    const t = tokens[i]
    const om = t.type === 'html' && typeof t.raw === 'string' ? HTML_CONTAINER_OPEN_RE.exec(t.raw) : null
    if (!om) { out.push(t); i++; continue }
    // Opening container found. Scan forward for the matching close tag,
    // accounting for nested same-tag containers. Depth starts by counting the
    // open tag in THIS token, plus any close tags already in it (covers the
    // self-contained form `<details>x</details>` which marked emits as one
    // html token, and same-line nesting `<details><details>..</details></details>`).
    const tag = om[1].toLowerCase()
    const openRe = openTagRe(tag)
    const closeRe = tagRe(tag)
    const selfOpens = (t.raw.match(openRe) || []).length
    const selfCloses = (t.raw.match(closeRe) || []).length
    let depth = selfOpens - selfCloses
    let j = i
    if (depth > 0) {
      // Still open after this token — scan forward for the matching close.
      j = i + 1
      while (j < tokens.length) {
        const tj = tokens[j]
        if (tj.type === 'html' && typeof tj.raw === 'string') {
          const opens = (tj.raw.match(openRe) || []).length
          const closes = (tj.raw.match(closeRe) || []).length
          depth += opens - closes
          if (depth <= 0) break
        }
        j++
      }
    }
    // Collapse tokens[i..j] into one synthetic token. Their raw fragments are
    // contiguous in the source (spaces included), so concatenation yields a
    // substring indexOf can locate in one shot. When depth never reaches 0
    // (unclosed container), j stops at the last token — the whole tail folds
    // into one block rather than misaligning everything after it.
    let raw = ''
    for (let k = i; k <= j && k < tokens.length; k++) raw += tokens[k].raw || ''
    out.push({ type: 'html', raw })
    i = j + 1
  }
  return out
}

// Scroll `container` by the minimum amount needed to bring `el` fully into
// view. If `el` is already visible, does nothing. Similar in intent to
// element.scrollIntoView({ block: 'nearest' }) but operates within a single
// scroll container (no ancestor scrolling, no horizontal change).
//
// The three branches are mutually exclusive and exhaustive, so a given
// (container, el) state always yields the same delta. Callers re-invoke this
// on every cursor move, so the branches MUST be idempotent — a structure that
// flips between two positions across calls would oscillate forever.
function scrollIntoViewMinimal(container, el) {
  const cRect = container.getBoundingClientRect()
  const eRect = el.getBoundingClientRect()
  const margin = 12  // breathing room so the highlight isn't flush against the edge
  const targetTop = cRect.top + margin
  const targetBottom = cRect.bottom - margin
  if (eRect.top >= targetTop && eRect.bottom <= targetBottom) return  // already visible
  if (eRect.height >= cRect.height - 2 * margin) {
    // Taller than the viewport — the bottom constraint is unsatisfiable, so
    // align the top; any other choice would oscillate on the next call.
    container.scrollTop += eRect.top - targetTop
  } else if (eRect.top < targetTop) {
    container.scrollTop -= targetTop - eRect.top
  } else {
    container.scrollTop += eRect.bottom - targetBottom
  }
}

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
  // Mirror of `pdf` for reading inside callbacks (e.g. loadPDF) that must not
  // depend on the pdf state to avoid stale closures.
  const pdfRef = useRef(null)
  useEffect(() => { pdfRef.current = pdf }, [pdf])
  const [mdContent, setMdContent] = useState('')
  const [visiblePage, setVisiblePage] = useState(1)
  const [pageInput, setPageInput] = useState('')
  const [editingPage, setEditingPage] = useState(false)
  const pageInputRef = useRef(null)
  const [containerWidth, setContainerWidth] = useState(0)

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
  const currentPdfUrlRef = useRef(null)
  const headingMapRef = useRef([])  // [{srcLine, el?}] for scroll sync
  const blockMapRef = useRef([])    // [{startLine, endLine, el}] for precise highlight
  const highlightedElsRef = useRef([])
  const currentHighlightLineRef = useRef(null) // last highlightLine() arg, re-applied after block-map rebuild
  const restoredScrollKeyRef = useRef('')
  const previewLoadTokenRef = useRef(0)
  // Imperative handle exposed by <PdfPreview> (scrollToPage / goToPage / etc).
  const pdfApiRef = useRef(null)

  // Revoke any held object URL. Used when switching away from a PDF or on
  // unmount. Returns the URL it revoked (or null) so callers can clear the ref.
  const revokePdfUrl = useCallback(() => {
    if (!currentPdfUrlRef.current) return null
    const url = currentPdfUrlRef.current
    URL.revokeObjectURL(url)
    currentPdfUrlRef.current = null
    return url
  }, [])

  // Reset non-PDF render state. Called at the start of every load effect run
  // so identity changes always start from a clean slate. PDF state is handled
  // separately: the load effect parses the new document BEFORE swapping it in,
  // so recompiles do not blank the preview (the previous PDF stays on screen
  // until the new one is ready, eliminating the black flash).
  const resetRenderingState = useCallback(() => {
    previewLoadTokenRef.current += 1
    setMdContent('')
    setPageInput('')
    setEditingPage(false)
    setVisiblePage(1)
    headingMapRef.current = []
    blockMapRef.current = []
    highlightedElsRef.current = []
    currentHighlightLineRef.current = null
    restoredScrollKeyRef.current = ''
  }, [])

  // Track container width for the PdfPreview fit-to-width calculation.
  useEffect(() => {
    if (!containerRef.current) return
    const observer = new ResizeObserver(entries => {
        if(entries[0]) setContainerWidth(entries[0].contentRect.width)
    })
    observer.observe(containerRef.current)
    return () => observer.disconnect()
  }, [])

  // Navigate to a specific page by number. Delegates to the PdfPreview API.
  const goToPage = useCallback((n) => {
    pdfApiRef.current?.goToPage?.(n)
  }, [])

  // Parse a fetched PDF into a PDFDocumentProxy. The previous document and its
  // object URL stay alive until the new one is ready, so recompiles never
  // blank the preview. Callers revoke the URL on failure; on success the URL
  // is retained (it backs the document's font/cmap requests) and the previous
  // one is released here.
  const loadPDF = useCallback(async (url) => {
    const token = previewLoadTokenRef.current + 1
    previewLoadTokenRef.current = token
    const assetBase = `${import.meta.env.BASE_URL || '/'}pdfjs/`
    try {
      const loadingTask = pdfjsLib.getDocument({
        url,
        cMapUrl: `${assetBase}cmaps/`,
        cMapPacked: true,
        standardFontDataUrl: `${assetBase}standard_fonts/`,
        useSystemFonts: true,
      })
      const pdfDoc = await loadingTask.promise
      if (token !== previewLoadTokenRef.current) {
        // A newer load superseded this one — discard quietly.
        URL.revokeObjectURL(url)
        pdfDoc.destroy?.()
        return
      }

      // Swap: release the PREVIOUS url only now that the new doc is ready.
      revokePdfUrl()
      currentPdfUrlRef.current = url

      // Defer destroying the previous document until the next frame so the
      // PdfPreview has a chance to start rendering the new one first — the
      // min-height pin in PdfViewerHost.loadDocument keeps the viewport steady
      // during this overlap. Read from the ref (not the state) so loadPDF does
      // not need pdf in its dependency array.
      const previous = pdfRef.current
      requestAnimationFrame(() => { previous?.destroy?.() })

      setPdf(pdfDoc)
      pdfRef.current = pdfDoc
      setNumPages(pdfDoc.numPages)
    } catch (e) {
      console.error('PDF Load Error:', e)
      if (e?.name !== 'AbortException') {
        URL.revokeObjectURL(url)
        toastError(t('preview.loadFailed'))
      }
    }
  }, [revokePdfUrl, t])

  // ── Single load effect — the ONLY place that fetches preview content. ──
  // Source-file switches inside one compiled TeX project must not reload the
  // PDF; recompiles still reload by bumping previewLoadVersion.
  useEffect(() => {
    if (!currentProjectId) return
    const kind = previewKind
    const path = previewLoadPath
    resetRenderingState()

    // When moving away from a PDF preview, drop the PDF state so the render
    // branch switches to the new (markdown/binary/none) view. The PDF branch
    // itself never nulls pdf here — loadPDF swaps in the new doc only once it
    // has finished parsing, which is what keeps recompiles flash-free.
    if (kind !== 'pdf-compiled' && kind !== 'pdf-standalone') {
      if (currentPdfUrlRef.current) revokePdfUrl()
      if (pdfRef.current) {
        const old = pdfRef.current
        requestAnimationFrame(() => { old.destroy?.() })
        pdfRef.current = null
      }
      setPdf(null)
      setNumPages(0)
    }

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
    revokePdfUrl,
    loadPDF,
  ])

  // Revoke any held object URL on unmount.
  useEffect(() => {
    return () => revokePdfUrl()
  }, [revokePdfUrl])

  useEffect(() => {
    if (type === 'markdown' && containerRef.current) {
        const el = containerRef.current.querySelector('.prose')
        if (el) {
            renderMathInElement(el, { delimiters: [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}], throwOnError:false })
            // After typesetting, flag inline formulas that truly overflow the
            // column so each gets its own scrollbar. See utils/mathGuard.js.
            applyMathOverflow(el)
        }
    }
  }, [mdContent, type, zoomLevel])

  // Restore the saved scroll ratio once the freshly-loaded content is ready.
  // PDF and markdown scroll inside different containers (PdfPreview owns the
  // PDF scroll element; markdown scrolls in containerRef), so route through
  // the pdfApiRef for PDFs.
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
      if (type === 'pdf') {
        pdfApiRef.current?.scrollToRatio?.(ratio)
      } else {
        const container = containerRef.current
        if (!container) return
        const maxScroll = container.scrollHeight - container.clientHeight
        if (maxScroll > 0) container.scrollTop = Math.max(0, Math.min(1, ratio)) * maxScroll
      }
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
  // Each token's raw text is located in the placeholder text via indexOf, then
  // mapped back to real source line numbers, then converted via a precomputed
  // line-offset table. This avoids the drift that comes from counting lines via
  // split('\n').
  //
  // Why placeholder text, not raw mdContent: the render path (below) parses the
  // math-guarded text, so the DOM it builds has a 1:1 block structure with the
  // guarded lexer output. Lexing raw markdown instead diverges (marked doesn't
  // know `$$`, so a multi-line math block tokenizes differently than the single
  // `<p>` it renders to), which fails the count-validation below and silently
  // disables precise highlight for any document containing math.
  useEffect(() => {
    // Clear previous highlight
    for (const el of highlightedElsRef.current) el.classList.remove('md-highlight')
    highlightedElsRef.current = []

    if (type !== 'markdown' || !mdContent || !containerRef.current) {
      blockMapRef.current = []
      return
    }
    try {
      // Precompute: character offset where each source line starts (in RAW mdContent)
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

      // Lex the SAME math-guarded text the render path parses, so token count
      // matches DOM block count. `spans` maps placeholder offsets back to the
      // original math block's source offsets (a multi-line `$$...$$` collapses
      // to a single-line placeholder, so guarded offsets ≠ source offsets).
      const { text: guarded, spans } = extractMath(mdContent)

      // Translate a guarded-text offset to a source-text offset.
      //  - `gOff` is a placeholder-text offset.
      //  - Strictly inside a placeholder (phStart < gOff < phEnd) → that math
      //    block's srcStart, so the placeholder maps to the math's first line.
      //  - At the boundary gOff === phStart the `inclusive` flag decides:
      //      true  → "inside"   (a token START at phStart is the placeholder)
      //      false → "verbatim" (a token END at phStart is exclusive, i.e. before it)
      //  - Otherwise verbatim: apply cumulative length delta across placeholders.
      const guardOffsetToSrc = (gOff, inclusive) => {
        let src = gOff
        for (const s of spans) {
          if (gOff < s.phStart) break              // before this placeholder
          if (gOff === s.phStart && !inclusive) break // token end at placeholder start
          if (gOff < s.phEnd) return s.srcStart     // strictly inside
          // gOff >= s.phEnd: past this placeholder, accumulate the delta
          src += (s.srcEnd - s.srcStart) - (s.phEnd - s.phStart)
        }
        return src
      }
      const guardToSrc = (gOff) => guardOffsetToSrc(gOff, true)
      // If a token's end offset touches/overlaps a placeholder, return that
      // math block's source end offset so endLine spans the whole formula.
      const guardEndToSrc = (gEnd) => {
        for (const s of spans) {
          if (s.phStart >= gEnd) break           // placeholder starts at/after end
          if (gEnd <= s.phEnd) return s.srcEnd   // end within placeholder
        }
        return guardOffsetToSrc(gEnd, false)
      }

      const tokens = foldContainers(previewMarked.lexer(guarded))
      const domBlocks = containerRef.current.querySelectorAll('.prose > *')
      let searchOffset = 0
      // `map` holds one entry per highlightable unit. Most top-level tokens
      // contribute one record; a `list` token contributes one record per <li>
      // so map.length can exceed the top-level block count.
      const map = []
      // topIdx tracks the next slot in domBlocks (.prose > *), advancing once
      // per non-space top-level token. This keeps the lexer↔DOM 1:1 alignment
      // used by the validation below intact even though the map itself is
      // finer-grained than the DOM top level.
      let topIdx = 0

      for (const token of tokens) {
        const raw = token.raw
        if (!raw) continue
        const idx = guarded.indexOf(raw, searchOffset)
        if (idx === -1) continue
        const srcStart = guardToSrc(idx)
        const srcEnd = guardEndToSrc(idx + raw.length)
        const startLine = lineAtOffset(srcStart)
        const endLine = lineAtOffset(srcEnd - 1)
        searchOffset = idx + raw.length

        if (token.type === 'space') continue

        const domEl = topIdx < domBlocks.length ? domBlocks[topIdx] : null
        topIdx += 1

        if (domEl && token.type === 'list' && Array.isArray(token.items) && token.items.length > 0) {
          // Expand the <ul>/<ol> into one record per top-level <li>. marked's
          // list.items[i].raw aligns 1:1 with the <li> direct children of the
          // rendered list element: nested items fold into their parent item's
          // raw, and the rendered nested <ul> sits inside the parent <li>, so
          // listEl.children stays the outer <li>s.
          const lis = Array.from(domEl.children).filter(c => c.tagName === 'LI')
          const liCount = Math.min(token.items.length, lis.length)
          let itemSearch = idx
          for (let k = 0; k < liCount; k++) {
            const itemRaw = token.items[k].raw
            if (!itemRaw) continue
            const j = guarded.indexOf(itemRaw, itemSearch)
            if (j === -1) continue
            const iStart = lineAtOffset(guardToSrc(j))
            const iEnd = lineAtOffset(guardEndToSrc(j + itemRaw.length) - 1)
            map.push({ startLine: iStart, endLine: iEnd, el: lis[k] })
            itemSearch = j + itemRaw.length
          }
          // Item count can mismatch <li> count on malformed markdown or when
          // DOMPurify rewrites the list; fall back to the whole-list block so
          // the section stays reachable.
          if (liCount === 0) map.push({ startLine, endLine, el: domEl })
          continue
        }

        if (domEl) map.push({ startLine, endLine, el: domEl })
      }

      // Validation: top-level non-space token count must match DOM block count.
      // Per-<li> expansion is internal to a single block and does not affect it.
      blockMapRef.current = (topIdx === domBlocks.length) ? map : []
    } catch {
      blockMapRef.current = []
    }

    // Re-apply the current highlight after the block map was rebuilt. Editing
    // the document changes mdContent → this effect runs → line 479 clears the
    // highlight. Without re-applying here, the highlight vanishes on every
    // keystroke until the user moves the cursor again.
    const hl = currentHighlightLineRef.current
    if (hl != null) {
      const n = hl - 1
      for (const block of blockMapRef.current) {
        if (n >= block.startLine && n <= block.endLine) {
          block.el.classList.add('md-highlight')
          highlightedElsRef.current = [block.el]
          break
        }
      }
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
      // Delegates to PdfPreview, which projects the PDF-space (x, y) onto the
      // rendered page, scrolls there, and drops the SyncTeX pulse marker.
      pdfApiRef.current?.scrollToPage?.(n, x, y)
    },
    scrollToPercent: (pct) => {
      // PDF scrolls inside PdfPreview's own container; markdown scrolls here.
      if (type === 'pdf') {
        pdfApiRef.current?.scrollToRatio?.(pct)
      } else if (containerRef.current) {
        const maxScroll = containerRef.current.scrollHeight - containerRef.current.clientHeight
        containerRef.current.scrollTop = Math.max(0, pct * maxScroll)
      }
    },
    scrollToLine: (line) => {
      const container = containerRef.current
      if (!container) return
      const n = line - 1  // CodeMirror lines are 1-indexed, source array is 0-indexed

      // Primary: precise block map. Resolves to the exact <li> for list lines,
      // avoiding the drift the heading-interpolation fallback suffers when one
      // source line maps to uneven DOM heights (long/wrapping list items).
      // Within a block we interpolate by source-line ratio, so multi-line
      // paragraphs scroll continuously while list items snap to their bounds.
      const blocks = blockMapRef.current
      if (blocks.length > 0) {
        for (const block of blocks) {
          if (n >= block.startLine && n <= block.endLine) {
            const cRect = container.getBoundingClientRect()
            const eRect = block.el.getBoundingClientRect()
            const elTop = eRect.top - cRect.top + container.scrollTop
            const elH = eRect.height
            const span = block.endLine - block.startLine
            const within = span > 0 ? (n - block.startLine) / span : 0
            const targetTop = elTop + within * elH
            const anchor = cRect.height / 3  // rest the target near the upper third
            container.scrollTop = Math.max(0, targetTop - anchor)
            return
          }
        }
      }

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
      currentHighlightLineRef.current = line
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
            scrollIntoViewMinimal(container, block.el)
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
      scrollIntoViewMinimal(container, target)
    }
  }))

  // Ctrl/Cmd + wheel zooms the preview. For PDF this is handled inside
  // PdfPreview (cursor-anchored, drawing-delayed). For markdown it adjusts the
  // font-size factor (zoomLevel) on this scroll container.
  const handleWheel = useCallback((e) => {
    if (type !== 'markdown') return
    if (e.ctrlKey || e.metaKey) {
        e.preventDefault(); e.stopPropagation();
        const delta = e.deltaY > 0 ? -0.1 : 0.1
        setZoomLevel(Math.max(0.2, Math.min(4, zoomLevel + delta)))
    }
  }, [type, zoomLevel, setZoomLevel])

  useEffect(() => {
    const el = containerRef.current
    if (el) el.addEventListener('wheel', handleWheel, { passive: false })
    return () => el?.removeEventListener('wheel', handleWheel)
  }, [handleWheel])

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
            <button onClick={() => {
              if (type === 'pdf') pdfApiRef.current?.scrollToRatio?.(0)
              else containerRef.current?.scrollTo({top:0, behavior:'smooth'})
            }} className="p-2.5 hover:bg-blue-50 dark:hover:bg-sigma-600/20 text-gray-600 dark:text-gray-400 hover:text-blue-600 dark:hover:text-sigma-400 rounded-xl transition-all shadow-sm"><ArrowUp className="w-5 h-5" /></button>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={(e) => {
          // Markdown scrolls in this container; PDF scroll is reported by
          // PdfPreview's own listener via onScrollRatio below.
          if (type !== 'markdown') return
          const el = e.currentTarget
          const maxScroll = el.scrollHeight - el.clientHeight
          if (maxScroll > 0) onScroll?.(el.scrollTop / maxScroll)
        }}
        className="flex-1 overflow-auto relative scroll-smooth flex flex-col"
      >
        {type === 'pdf' ? (
          pdf ? (
            <PdfPreview
              pdfDoc={pdf}
              zoomLevel={zoomLevel}
              onZoomChange={setZoomLevel}
              onPageClick={onPageClick}
              onScrollRatio={onScroll}
              onPageCount={setNumPages}
              onPageChange={setVisiblePage}
              containerWidth={containerWidth}
              registerApi={(api) => { pdfApiRef.current = api }}
            />
          ) : !compiling ? (
            <div className="flex flex-col items-center justify-center m-auto text-gray-300 dark:text-gray-600">
              <FileSearch className="w-16 h-12 mb-4 opacity-20" />
              <p className="text-[10px] font-black tracking-[0.3em] uppercase">{t('preview.noPreview')}</p>
            </div>
          ) : null
        ) : type === 'markdown' ? (
            <div className="flex flex-col items-center w-full m-auto py-12 px-8">
                <div
                    className="prose dark:prose-invert max-w-none prose-pre:font-mono prose-code:font-mono prose-code:before:hidden prose-code:after:hidden bg-white dark:bg-gray-800 p-12 mx-auto shadow-2xl border border-gray-200 dark:border-gray-700 mb-12"
                    style={{ maxWidth: '48rem', width: '100%', fontSize: `${zoomLevel}rem` }}
                    dangerouslySetInnerHTML={{ __html: (() => {
                        const md = String(mdContent ?? '')
                        const { text, map } = extractMath(md)
                        const restored = restoreMath(previewMarked.parse(text), map)
                        return DOMPurify.sanitize(rewriteProjectImageSrc(restored, currentProjectId))
                    })() }}
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
