import { useEffect, useRef, useCallback, useImperativeHandle, forwardRef, memo, useState } from 'react'
import { createPortal } from 'react-dom'
import { EditorView } from 'codemirror'
import { EditorState, Compartment, StateField, StateEffect, Transaction } from '@codemirror/state'
import { keymap, Decoration } from '@codemirror/view'
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete'
import { bracketMatching } from '@codemirror/language'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search'
import { lintKeymap, setDiagnostics } from '@codemirror/lint'
import { indentOnInput, foldGutter } from '@codemirror/language'
import { lineNumbers, highlightActiveLine, highlightActiveLineGutter, dropCursor } from '@codemirror/view'

import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { markdown } from '@codemirror/lang-markdown'
import { json } from '@codemirror/lang-json'
import { javascript } from '@codemirror/lang-javascript'
import { python } from '@codemirror/lang-python'
import { useStore } from '../store/useStore'
import { useTheme } from '../hooks/useTheme'
import { useEditorAppearance } from '../hooks/useEditorAppearance'
import { useTranslation } from 'react-i18next'
import { filesAPI } from '../api'
import { TEX_EXTS } from '../utils/constants'
import { getFontCss } from '../utils/editorFonts'
import { getSchemeExtension } from '../utils/highlightSchemes'
import { storage } from '../utils/storage'
import { matchAnnotation } from '../utils/annotationMatching'
import { AnnotationPopup } from './Annotations'
import ContextMenu from './ContextMenu'

const languageConf = new Compartment()

/** Format ISO timestamp for annotation UI → "2026-01-01 12:34:22" */
function formatTimestamp(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('sv-SE', { hour12: false }).replace('T', ' ')
}

// --- Annotation Effects ---
const addAnnoEffect = StateEffect.define()
const setAnnosEffect = StateEffect.define()
const delAnnoEffect = StateEffect.define()

/**
 * Map annotation status to CSS class.
 *   'exact' / 'valid' → solid yellow underline (exact match)
 *   'fuzzy' / 'modified' / 'orphan' → dashed orange underline (text changed or unmatched)
 */
const annoCls = (status) =>
  (status === 'exact' || status === 'valid') ? 'cm-annotation' : 'cm-annotation-modified'

const isBlankAnnotationText = (text) => !text || !text.trim()

const annotationField = StateField.define({
  create() { return Decoration.none },
  update(annos, tr) {
    annos = annos.map(tr.changes)
    for (let e of tr.effects) {
      if (e.is(addAnnoEffect)) {
        const { id, from, to, status } = e.value
        annos = annos.update({
          add: [Decoration.mark({ class: annoCls(status), attributes: { 'data-anno-id': id } }).range(from, to)]
        })
      } else if (e.is(setAnnosEffect)) {
        const docLen = tr.state.doc.length
        const validAnnos = e.value.map(a => {
          const from = Math.min(a.from ?? 0, docLen)
          const to = Math.min(a.to ?? 0, docLen)
          if (from >= to) return null
          return Decoration.mark({ class: annoCls(a.status), attributes: { 'data-anno-id': a.id } }).range(from, to)
        }).filter(Boolean)
        annos = Decoration.set(validAnnos, true)
      } else if (e.is(delAnnoEffect)) {
        annos = annos.update({ filter: (f, t, value) => value.spec.attributes['data-anno-id'] !== e.value })
      }
    }
    return annos
  },
  provide: f => EditorView.decorations.from(f)
})

/**
 * Read all annotation decorations from the LIVE CodeMirror field, sorted by
 * document position. Module-level (pure, depends only on `annotationField`).
 * Used by the annotation-nav imperative methods so they never read the stale
 * Zustand `annotations` array.
 */
function readAnnoPositions(view) {
  const field = view.state.field(annotationField)
  const list = []
  field.between(0, view.state.doc.length, (from, to, deco) => {
    const id = deco.spec?.attributes?.['data-anno-id']
    if (id) list.push({ id, from, to })
  })
  list.sort((a, b) => a.from - b.from || a.to - b.to)
  return list
}

const lightEditorTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: '#ffffff' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { overflow: 'auto !important' },
  '.cm-activeLine': { backgroundColor: '#f0f7ff' },
  '.cm-annotation': {
    backgroundColor: '#fef3c7',
    borderBottom: '2px solid #f59e0b',
    cursor: 'pointer',
    borderRadius: '2px',
    padding: '1px 0'
  },
  '.cm-annotation-modified': {
    backgroundColor: '#fff7ed',
    borderBottom: '2px dashed #f97316',
    cursor: 'pointer',
    borderRadius: '2px',
    padding: '1px 3px'
  },
})

const darkEditorTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: '#111827', color: '#e5e7eb' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { overflow: 'auto !important' },
  '.cm-activeLine': { backgroundColor: '#1e293b' },
  '.cm-activeLineGutter': { backgroundColor: '#1e293b' },
  '.cm-gutters': { backgroundColor: '#0f172a', color: '#4b5563', borderRight: '1px solid #1f2937' },
  '.cm-content': { caretColor: '#e5e7eb' },
  '.cm-cursor': { borderLeftColor: '#e5e7eb' },
  '.cm-selectionBackground, ::selection': { backgroundColor: '#264f78' },
  '&.cm-focused .cm-selectionBackground': { backgroundColor: '#264f78' },
  '.cm-annotation': {
    backgroundColor: 'rgba(245, 158, 11, 0.28)',
    borderBottom: '2px solid #f59e0b',
    cursor: 'pointer',
    borderRadius: '2px',
    padding: '1px 0'
  },
  '.cm-annotation-modified': {
    backgroundColor: 'rgba(249, 115, 22, 0.28)',
    borderBottom: '2px dashed #f97316',
    cursor: 'pointer',
    borderRadius: '2px',
    padding: '1px 3px'
  },
})

const themeCompartment = new Compartment()
const syntaxCompartment = new Compartment()
const fontCompartment = new Compartment()

/**
 * Build a CodeMirror extension that applies the user-selected editor font
 * family, size, and line height. Lives in its own compartment so it can be
 * reconfigured at runtime via `setEditorAppearance` without rebuilding the
 * editor state. Background is intentionally NOT set here — only dark mode
 * controls it.
 */
function buildFontExtension(fontFamily, fontSize, lineHeight) {
  return EditorView.theme({
    '&': { fontSize: `${fontSize}px` },
    '.cm-scroller': { fontFamily: getFontCss(fontFamily), lineHeight },
  })
}

const getLanguage = (path) => {
  const ext = path?.split('.').pop()?.toLowerCase()
  if (TEX_EXTS.includes(ext)) return StreamLanguage.define(stex)
  if (ext === 'md') return markdown(); if (ext === 'json') return json(); if (ext === 'js') return javascript(); if (ext === 'py') return python()
  return []
}

// ── Pending annotation ID generator ──────────────────────────────────
let _pendingCounter = 0
function _genPendingId() { return `pending_${Date.now()}_${++_pendingCounter}` }

const Editor = forwardRef(({ onContentChange, onScroll, onSave, onAutoSave, onLineChange, onCursorChange, onFileReady, onSaveBeforeAnnotationChat, onAnnoNavScroll, onApplyDiffSave }, ref) => {
  const { t } = useTranslation()
  const containerRef = useRef(null); const viewRef = useRef(null); const cursorRef = useRef({ line: 1, column: 0 })
  const autoSaveTimerRef = useRef(null)
  const prevFileRef = useRef(null)
  const [isEditorReady, setIsEditorReady] = useState(false)
  const isEditorReadyRef = useRef(false)
  const readyResolversRef = useRef([])
  const [ctxMenu, setCtxMenu] = useState(null)
  const [popupStyle, setPopupStyle] = useState(null)
  // Multiple annotation selection: when click hits overlapping annotations
  const [annoChoices, setAnnoChoices] = useState(null)
  const popupAnnoFromRef = useRef(null)

  // ── Annotation navigation state ─────────────────────────────────────
  // Two refs read by different consumers but always written together with
  // the same value, keeping the navigation and display paths decoupled by
  // intent even though they currently agree:
  //   currentIndexRef — read by navToAnnotation as the base for its pure
  //     cur±1 arithmetic.
  //   displayIndexRef — read by getCurrentAnnotationIndex to render the
  //     counter.
  // Both are written by navToAnnotation (button click) and by the scroll
  // listener (free-scroll geometry re-derivation, only outside the
  // NAV_SUPPRESS_MS window). That suppression window is what keeps button
  // steps exactly 1 and oscillation-free: the 0/1/many scroll events a
  // single scrollIntoView may emit cannot overwrite the button's arithmetic
  // while the window is open.
  const currentIndexRef = useRef(1)
  const displayIndexRef = useRef(1)
  // lastNavScrollAt: timestamp of the last button-driven scroll. While inside
  //   the NAV_SUPPRESS_MS window, the scroll listener skips geometry
  //   re-derivation, so the scroll events emitted by a scrollIntoView cannot
  //   fight the button's arithmetic.
  const lastNavScrollAt = useRef(0)
  const NAV_SUPPRESS_MS = 350

  // Clamp both nav index refs to the live decoration count. Called after any
  // mutation that can shrink the count (delete, sync, reload, bulk replace);
  // without it the counter can read e.g. "2 / 1" until the next free-scroll
  // re-derivation in the scroll listener.
  const clampNavIndices = useCallback(() => {
    const view = viewRef.current
    if (!view) return
    const total = readAnnoPositions(view).length
    if (total === 0) return // refreshAnnoNav hides the panel via annoCount=0
    if (currentIndexRef.current > total) currentIndexRef.current = total
    if (displayIndexRef.current > total) displayIndexRef.current = total
  }, [])

  // Pending annotations: created locally, not yet persisted to backend (no replies sent).
  // Live in a ref (not zustand) — they don't survive file switches.
  const pendingAnnosRef = useRef([])

  const currentProject = useStore(s => s.currentProject)
  const currentFile = useStore(s => s.currentFile)
  const fileVersion = useStore(s => s.fileVersion)
  const setIsTexFile = useStore(s => s.setIsTexFile)
  const setFileHash = useStore(s => s.setFileHash)
  const annotations = useStore(s => s.annotations)
  const addAnnotation = useStore(s => s.addAnnotation)
  const updateAnnotation = useStore(s => s.updateAnnotation)
  const deleteAnnotation = useStore(s => s.deleteAnnotation)
  const activeAnnotationId = useStore(s => s.activeAnnotationId)
  const setActiveAnnotationId = useStore(s => s.setActiveAnnotationId)
  const setPendingCitation = useStore(s => s.setPendingCitation)
  const setLeftTab = useStore(s => s.setLeftTab)
  const setAnnotations = useStore(s => s.setAnnotations)
  const compileDiagnostics = useStore(s => s.compileDiagnostics)
  const { isDark } = useTheme()
  const { appearance, setEditorAppearance } = useEditorAppearance()

  /**
   * Bump editor font size by `delta` px, clamped to the storage whitelist.
   * Uses the updater form so the keymap closure (captured once at mount) always
   * reads the current font size instead of a stale render-time snapshot.
   */
  const adjustFontSize = useCallback((delta) => {
    setEditorAppearance((prev) => ({ fontSize: prev.fontSize + delta }))
  }, [setEditorAppearance])

  const callbacks = useRef({ onContentChange, onScroll, onSave, onAutoSave, onLineChange, onCursorChange, onFileReady, onSaveBeforeAnnotationChat, onAnnoNavScroll, onApplyDiffSave })
  useEffect(() => { callbacks.current = { onContentChange, onScroll, onSave, onAutoSave, onLineChange, onCursorChange, onFileReady, onSaveBeforeAnnotationChat, onAnnoNavScroll, onApplyDiffSave } })

  // ── Annotation helpers ──────────────────────────────────────────────

  /** Look up the CodeMirror decoration position for an annotation ID. */
  const getDecorationPosition = useCallback((view, annotationId) => {
    const field = view.state.field(annotationField)
    let result = null
    field.between(0, view.state.doc.length, (from, to, deco) => {
      if (deco.spec?.attributes?.['data-anno-id'] === annotationId) result = { from, to }
    })
    return result
  }, [])

  /** Find an annotation by ID across both pending and persisted lists. */
  const findAnnotation = useCallback((id) => {
    if (!id) return null
    return annotations.find(a => a.id === id)
      || pendingAnnosRef.current.find(a => a.id === id)
      || null
  }, [annotations])

  // ── Save-time annotation sync ───────────────────────────────────────
  // Called from handleSave (via imperative handle) BEFORE file content
  // is written to disk. Matches annotations against current document,
  // updates positions, deletes removed ones, and saves to backend.

  /** Sync all persisted annotations: read decoration positions, update text, save. */
  const syncAnnotationsNow = useCallback(async () => {
    const view = viewRef.current
    const pid = useStore.getState().currentProject?.id
    const file = useStore.getState().currentFile
    if (!view || !pid || !file) return

    const persisted = useStore.getState().annotations
    if (persisted.length === 0) return

    const doc = view.state.doc.toString()
    const toRemove = []   // decorations to delete
    const resolved = []   // annotations to keep

    for (const a of persisted) {
      // Skip orphan annotations — they have no match in the document and must
      // not have their position or text updated (per requirement: no save for orphans).
      if (a.status === 'orphan') {
        resolved.push(a)
        continue
      }

      const pos = getDecorationPosition(view, a.id)
      if (!pos || pos.from >= pos.to) {
        // Decoration collapsed = annotated text was deleted entirely
        toRemove.push(a)
        continue
      }

      // Read text directly from the document at the tracked decoration position.
      // CodeMirror's annos.map(tr.changes) keeps the decoration aligned with
      // edits, so this reflects exactly what the user sees in the editor.
      const text = doc.slice(pos.from, pos.to)
      if (isBlankAnnotationText(text)) {
        // A decoration can survive a large deletion by mapping onto nearby
        // whitespace. Whitespace-only ranges are not valid annotations.
        toRemove.push(a)
        continue
      }
      resolved.push({
        ...a,
        from: pos.from,
        to: pos.to,
        originalText: text,
        status: 'valid',
      })
    }

    // Remove collapsed decorations
    if (toRemove.length > 0) {
      view.dispatch({ effects: toRemove.map(a => delAnnoEffect.of(a.id)) })
      const activeId = useStore.getState().activeAnnotationId
      if (activeId && toRemove.some(a => a.id === activeId)) {
        setActiveAnnotationId(null)
      }
    }

    // Update store + decorations
    setAnnotations(resolved)
    view.dispatch({ effects: setAnnosEffect.of(resolved) })
    clampNavIndices()

    // Save to backend. The backend preserves stored anchors for orphan
    // annotations, so placeholder render positions are not written back.
    const toSave = resolved
    if (toSave.length > 0 || toRemove.length > 0) {
      try { await filesAPI.saveAnnotations(pid, file, toSave) } catch { /* non-critical */ }
    }
  }, [clampNavIndices, getDecorationPosition, setAnnotations])

  // ── Annotation CRUD ─────────────────────────────────────────────────

  /** Create a local (pending) annotation. Not persisted until first reply. */
  const handleAddAnnotation = useCallback(() => {
    if (!ctxMenu || !viewRef.current) return

    const view = viewRef.current
    const { from, to } = ctxMenu
    const text = view.state.doc.sliceString(from, to)

    // Reject empty / whitespace-only selections
    if (!text.trim()) {
      setCtxMenu(null)
      return
    }

    const pendingId = _genPendingId()
    const newAnno = {
      id: pendingId,
      from,
      to,
      originalText: text,
      status: 'exact',
      thread: [],
      isPending: true,
      createdAt: new Date().toISOString(),
    }

    view.dispatch({ effects: addAnnoEffect.of({ id: pendingId, from, to, status: 'exact' }) })
    pendingAnnosRef.current = [...pendingAnnosRef.current, newAnno]

    const coords = view.coordsAtPos(from)
    if (coords) {
      setPopupStyle({ left: coords.left, top: coords.top })
      setActiveAnnotationId(pendingId)
      popupAnnoFromRef.current = from
    }
    setCtxMenu(null)
  }, [ctxMenu])

  /** Persist a pending annotation to the backend on first reply. Returns the real backend ID. */
  const handlePersistAnnotation = useCallback(async (pendingId, replyText) => {
    const view = viewRef.current
    const pid = useStore.getState().currentProject?.id
    const file = useStore.getState().currentFile
    if (!view || !pid || !file) return null

    const pending = pendingAnnosRef.current.find(a => a.id === pendingId)
    if (!pending) return null

    // Get a real backend ID
    const { id: backendId } = await filesAPI.createAnnotation(pid, file, {
      from: pending.from,
      to: pending.to,
    })

    const persisted = {
      id: backendId,
      from: pending.from,
      to: pending.to,
      originalText: pending.originalText,
      status: 'valid',
      thread: [{
        role: 'user',
        content: replyText,
        created_at: new Date().toISOString(),
      }],
    }

    // Replace pending decoration with persisted one
    view.dispatch({
      effects: [
        delAnnoEffect.of(pendingId),
        addAnnoEffect.of({ id: backendId, from: pending.from, to: pending.to, status: 'valid' }),
      ]
    })

    const existingAnnotations = useStore.getState().annotations

    // Move from pending ref to zustand store
    pendingAnnosRef.current = pendingAnnosRef.current.filter(a => a.id !== pendingId)
    addAnnotation(persisted)
    setActiveAnnotationId(backendId)

    // Save immediately (first reply = first persistence)
    try { await filesAPI.saveAnnotations(pid, file, [...existingAnnotations, persisted]) } catch { /* non-critical */ }

    return backendId
  }, [addAnnotation])

  /** User replied to a fuzzy/modified annotation, confirming the visible anchor. */
  const handleConfirmAnnotationAnchor = useCallback(async (annotationId) => {
    const view = viewRef.current
    const pid = useStore.getState().currentProject?.id
    const file = useStore.getState().currentFile
    if (!view || !pid || !file) return false

    const annotation = useStore.getState().annotations.find(a => a.id === annotationId)
    if (!annotation || annotation.status === 'orphan') return false

    const pos = getDecorationPosition(view, annotationId)
    if (!pos || pos.from >= pos.to) return false

    const text = view.state.doc.sliceString(pos.from, pos.to)
    if (isBlankAnnotationText(text)) return false

    const confirmed = {
      ...annotation,
      from: pos.from,
      to: pos.to,
      originalText: text,
      status: 'valid',
    }
    const confirmedAnnotations = useStore.getState().annotations.map(a =>
      a.id === annotationId ? confirmed : a
    )

    setAnnotations(confirmedAnnotations)
    view.dispatch({ effects: setAnnosEffect.of(confirmedAnnotations) })

    try { await filesAPI.saveAnnotations(pid, file, confirmedAnnotations) } catch { /* non-critical */ }
    return true
  }, [getDecorationPosition, setAnnotations])

  /** Cancel a pending annotation (popup closed without any reply). */
  const handleCancelPending = useCallback((pendingId) => {
    const view = viewRef.current
    if (view) view.dispatch({ effects: delAnnoEffect.of(pendingId) })
    pendingAnnosRef.current = pendingAnnosRef.current.filter(a => a.id !== pendingId)
    setActiveAnnotationId(null)
    popupAnnoFromRef.current = null
  }, [])

  /** Delete an annotation. Handles both pending and persisted. */
  const handleDelAnnotation = useCallback(async (id) => {
    const view = viewRef.current

    // Pending annotation — just cancel it
    if (pendingAnnosRef.current.some(a => a.id === id)) {
      handleCancelPending(id)
      return
    }

    // Persisted annotation
    if (view) view.dispatch({ effects: delAnnoEffect.of(id) })

    const updatedAnnotations = useStore.getState().annotations.filter(a => a.id !== id)
    deleteAnnotation(id)
    setActiveAnnotationId(null)
    popupAnnoFromRef.current = null
    clampNavIndices()

    try {
      await filesAPI.saveAnnotations(currentProject.id, currentFile, updatedAnnotations)
    } catch (e) {
      console.error('Failed to save annotations after delete', e)
    }
  }, [clampNavIndices, currentProject?.id, currentFile, deleteAnnotation, handleCancelPending])

  /** Apply a diff suggestion from an annotation reply. */
  const handleApplyDiff = useCallback(async (annotationId, diff) => {
    const view = viewRef.current
    if (!view) return

    const currentAnnotations = useStore.getState().annotations
    const anno = currentAnnotations.find(a => a.id === annotationId)
    if (!anno) return

    const beforeText = diff.before ?? diff.beforeText ?? ''
    const afterText = diff.after ?? diff.afterText ?? ''
    const doc = view.state.doc.toString()

    // Simple indexOf — before text is exact, no fuzzy matching needed
    const idx = doc.indexOf(beforeText)
    if (idx === -1) {
      // before text no longer in file — mark annotation as modified
      setAnnotations(currentAnnotations.map(a =>
        a.id === annotationId ? { ...a, status: 'modified' } : a
      ))
      return
    }

    const matchFrom = idx
    const matchTo = idx + beforeText.length

    // Apply the edit
    view.dispatch({
      changes: { from: matchFrom, to: matchTo, insert: afterText },
    })

    // Position delta for shifting unaffected annotations
    const delta = afterText.length - beforeText.length
    const editEnd = matchTo

    // Revalidate: shift non-overlapping annotations, re-match overlapping ones
    const revalidated = currentAnnotations.map(a => {
      const pos = getDecorationPosition(view, a.id)
      if (!pos || pos.from >= pos.to) return null

      if (pos.to <= matchFrom) {
        // Entirely before the edit — no change needed
        return a
      } else if (pos.from >= editEnd) {
        // Entirely after the edit — shift positions
        const newFrom = pos.from + delta
        const newTo = pos.to + delta
        // Quick exact check at new position
        const exact = view.state.doc.slice(newFrom, newTo) === a.originalText
        return { ...a, from: newFrom, to: newTo, status: exact ? 'valid' : 'modified' }
      } else {
        // Overlaps with edit — read new text at CodeMirror-adjusted position
        const newOriginalText = view.state.doc.slice(pos.from, pos.to)
        return { ...a, from: pos.from, to: pos.to, originalText: newOriginalText, status: 'valid' }
      }
    }).filter(Boolean)

    // Seed the store so the save path's `syncAnnotationsNow` sees these
    // annotations (it no-ops on an empty store). `syncAnnotationsNow` then
    // re-derives positions straight from the CodeMirror decorations, which
    // is more accurate than the delta arithmetic above, so the revalidated
    // array is intentionally overwritten shortly after.
    setAnnotations(revalidated)

    if (callbacks.current.onContentChange) callbacks.current.onContentChange(view.state.doc.toString())
    try { await callbacks.current.onApplyDiffSave?.() } catch { /* non-critical: save failure surfaces elsewhere */ }
  }, [setAnnotations, getDecorationPosition])

  const revalidateBackendAnnotations = useCallback((annos) => {
    const view = viewRef.current
    if (!view) return annos
    const doc = view.state.doc.toString()
    if (doc.length === 0) return annos.map(a => ({ ...a, status: 'modified' }))
    return annos.map(a => {
      const m = matchAnnotation(doc, { from: a.from, to: a.to, originalText: a.originalText })
      if (m.status === 'orphan') {
        // No match found — render dashed decoration at end of document.
        // Keep original originalText; do NOT update position in backend saves.
        return { ...a, from: m.from, to: m.to, originalText: a.originalText, status: 'orphan' }
      }
      return {
        ...a,
        from: m.from,
        to: m.to,
        originalText: m.originalText || a.originalText,
        status: m.status === 'exact' ? 'valid' : 'modified',
      }
    })
  }, [])

  /**
   * Re-derive the annotation index from viewport geometry. Called only on
   * user-driven (free) scrolls to keep the counter in sync. The scroll
   * listener writes the result to both currentIndexRef and displayIndexRef,
   * but ONLY outside the NAV_SUPPRESS_MS window after a button click — so
   * the button's pure-arithmetic index is never overwritten by geometry
   * while a scrollIntoView is settling.
   * Returns the derived index, or null when there are no annotations.
   * Also stored in a ref so the mount-time scroll listener can call it.
   */
  const computeTopAnnoIndex = useCallback(() => {
    const view = viewRef.current
    if (!view) return null
    const positions = readAnnoPositions(view)
    if (!positions.length) return null
    const scrollTop = view.scrollDOM.scrollTop
    // Find the first annotation at or below the viewport top. If all are
    // above (scrolled past the last), the last index is the display value.
    let idx = positions.length
    for (let i = 0; i < positions.length; i++) {
      if (view.lineBlockAt(positions[i].from).top >= scrollTop) {
        idx = i + 1
        break
      }
    }
    return Math.max(1, Math.min(idx, positions.length))
  }, [])
  const computeTopAnnoIndexRef = useRef(computeTopAnnoIndex)
  useEffect(() => { computeTopAnnoIndexRef.current = computeTopAnnoIndex })

  const handleReloadAnnotations = useCallback(async (annotationId = null) => {
    const view = viewRef.current
    const pid = useStore.getState().currentProject?.id
    const file = useStore.getState().currentFile
    if (!view || !pid || !file) return null

    const loaded = await filesAPI.loadAnnotations(pid, file)
    const validated = revalidateBackendAnnotations(loaded)
    setAnnotations(validated)
    view.dispatch({ effects: setAnnosEffect.of(validated) })
    clampNavIndices()
    return annotationId ? validated.find(a => a.id === annotationId) || null : null
  }, [clampNavIndices, revalidateBackendAnnotations, setAnnotations])

  // ── Editor click & popup ────────────────────────────────────────────

  const handleEditorClick = (e) => {
    const view = viewRef.current; if (!view) return

    const annoEl = e.target.closest('.cm-annotation, .cm-annotation-modified')
    if (!annoEl) { setActiveAnnotationId(null); setAnnoChoices(null); return }
    const annoId = annoEl.dataset.annoId
    if (!annoId) { setActiveAnnotationId(null); setAnnoChoices(null); return }

    // Collect ALL annotation IDs at this click position (for overlapping annotations)
    const pos = view.posAtCoords({ x: e.clientX, y: e.clientY })
    if (pos === null) return

    const annotationDecos = view.state.field(annotationField)
    const foundIds = []
    annotationDecos.between(pos, pos, (f, t, value) => {
      const id = value?.spec?.attributes?.['data-anno-id']
      if (id && !foundIds.includes(id)) foundIds.push(id)
    })

    if (!foundIds.includes(annoId)) foundIds.push(annoId)
    if (foundIds.length === 0) { setActiveAnnotationId(null); setAnnoChoices(null); return }

    if (foundIds.length === 1) {
      setAnnoChoices(null)
      openAnnotationPopup(foundIds[0], e.clientX, e.clientY)
    } else {
      setAnnoChoices({ x: e.clientX, y: e.clientY, ids: foundIds })
    }
  }

  /** Open the annotation popup for a single annotation ID. */
  const openAnnotationPopup = useCallback((id, clientX, clientY) => {
    const view = viewRef.current
    if (!view) return

    const anno = findAnnotation(id)
    if (!anno) return

    const decoPos = getDecorationPosition(view, id)
    popupAnnoFromRef.current = decoPos?.from ?? null

    setPopupStyle({ left: clientX, top: clientY })
    setActiveAnnotationId(id)
    if (!anno.isPending) {
      handleReloadAnnotations(id).catch(() => {})
    }
  }, [findAnnotation, getDecorationPosition, handleReloadAnnotations])

  const handleContextMenu = (e) => {
    const view = viewRef.current; if (!view) return
    const selection = view.state.selection.main
    if (!selection.empty) { e.preventDefault(); setCtxMenu({ x: e.clientX, y: e.clientY, from: selection.from, to: selection.to }) }
  }

  // ── Editor mount ────────────────────────────────────────────────────

  useEffect(() => {
    if (!containerRef.current) return
    // Initial values are read synchronously at mount (same pattern as the
    // existing isDark DOM read below) so the first paint already reflects the
    // saved editor appearance without waiting for the hook to notify.
    const initialIsDark = document.documentElement.classList.contains('dark')
    const initialAppearance = storage.getEditorAppearance()
    const extensions = [
      lineNumbers(), highlightActiveLineGutter(), highlightActiveLine(),
      history(), dropCursor(), EditorState.allowMultipleSelections.of(false),
      indentOnInput(), syntaxCompartment.of(getSchemeExtension(initialAppearance.syntaxScheme, initialIsDark)),
      bracketMatching(), closeBrackets(), autocompletion(), highlightSelectionMatches(),
      EditorView.lineWrapping, foldGutter(),
      themeCompartment.of(initialIsDark ? darkEditorTheme : lightEditorTheme),
      fontCompartment.of(buildFontExtension(initialAppearance.fontFamily, initialAppearance.fontSize, initialAppearance.lineHeight)),
      annotationField,
      languageConf.of([]),
      keymap.of([
        { key: 'Mod-s', run: (view) => { if(autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); callbacks.current.onSave?.(); return true; }, preventDefault: true },
        { key: 'Mod-=', run: () => { adjustFontSize(1); return true }, preventDefault: true },
        { key: 'Mod--', run: () => { adjustFontSize(-1); return true }, preventDefault: true },
        ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
        ...completionKeymap, ...lintKeymap, indentWithTab,
      ]),
      EditorView.updateListener.of((u) => {
        if (u.docChanged) {
          if (!isEditorReadyRef.current) return;
          callbacks.current.onContentChange?.(u.state.doc.toString())
          if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current)
          autoSaveTimerRef.current = setTimeout(() => callbacks.current.onAutoSave?.(false), 30000)
        }
        if (u.selectionSet) {
          const pos = u.state.selection.main.head; const line = u.state.doc.lineAt(pos)
          cursorRef.current = { line: line.number, column: pos - line.from }
          callbacks.current.onCursorChange?.(cursorRef.current)
        }
      }),
    ]
    const view = new EditorView({ state: EditorState.create({ doc: '', extensions }), parent: containerRef.current })
    view.scrollDOM.addEventListener('scroll', () => {
        const topPos = view.scrollDOM.scrollTop; const lineBlock = view.lineBlockAtHeight(topPos); const lineNumber = view.state.doc.lineAt(lineBlock.from).number
        callbacks.current.onLineChange?.(lineNumber); if (view.scrollDOM.scrollHeight > view.scrollDOM.clientHeight) callbacks.current.onScroll?.(topPos / (view.scrollDOM.scrollHeight - view.scrollDOM.clientHeight))
        // Button-driven scrolls set currentIndexRef via pure arithmetic and
        // stamp lastNavScrollAt. During free scrolling (outside the
        // suppression window), re-derive BOTH indices from geometry so the
        // button picks up from where the user scrolled to. Inside the
        // window (scrollIntoView settling), skip re-derivation entirely so
        // the button's arithmetic is never overwritten by geometry noise.
        if (Date.now() - lastNavScrollAt.current >= NAV_SUPPRESS_MS) {
          const idx = computeTopAnnoIndexRef.current?.()
          if (idx != null) {
            currentIndexRef.current = idx
            displayIndexRef.current = idx
          }
        }
        // Echo the current display index to the UI.
        callbacks.current.onAnnoNavScroll?.()
        const from = popupAnnoFromRef.current
        if (from != null) {
          const coords = view.coordsAtPos(from)
          if (coords) {
            setPopupStyle(prev => prev ? { left: coords.left, top: coords.top } : null)
          }
        }
    }, { passive: true })
    viewRef.current = view; return () => { if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); view.destroy() }
  }, [])

  // ── Reconfigure editor theme/syntax/font when dark mode or appearance changes ──
  useEffect(() => {
    if (!viewRef.current) return
    viewRef.current.dispatch({
      effects: [
        themeCompartment.reconfigure(isDark ? darkEditorTheme : lightEditorTheme),
        syntaxCompartment.reconfigure(getSchemeExtension(appearance.syntaxScheme, isDark)),
        fontCompartment.reconfigure(buildFontExtension(appearance.fontFamily, appearance.fontSize, appearance.lineHeight)),
      ],
    })
  }, [isDark, appearance])

  // ── File loading ────────────────────────────────────────────────────

  useEffect(() => {
    if (!currentFile || !currentProject?.id || !viewRef.current) {
      if (!currentFile) setIsTexFile(false)
      return
    }
    let isMounted = true

    const reloading = isEditorReadyRef.current && prevFileRef.current === currentFile
    prevFileRef.current = currentFile

    setIsEditorReady(false); isEditorReadyRef.current = false
    readyResolversRef.current = []

    // Clear pending annotations on file switch
    pendingAnnosRef.current = []
    // Reset nav indices — will be re-derived after decorations load
    currentIndexRef.current = 1
    displayIndexRef.current = 1
    lastNavScrollAt.current = 0

    let savedScroll = null
    if (reloading && viewRef.current) {
      savedScroll = viewRef.current.scrollDOM.scrollTop
    }

    filesAPI.read(currentProject.id, currentFile).then(data => {
      if (!isMounted || !viewRef.current) return

      viewRef.current.dispatch({
        changes: { from: 0, to: viewRef.current.state.doc.length, insert: typeof data === 'string' ? data : (data.content || '') },
        effects: languageConf.reconfigure(getLanguage(currentFile)),
        annotations: Transaction.addToHistory.of(false)
      })
      setFileHash(data?.hash ?? null)

      if (savedScroll !== null) {
        requestAnimationFrame(() => {
          if (viewRef.current) viewRef.current.scrollDOM.scrollTop = savedScroll
        })
      }

      requestAnimationFrame(() => {
        if (!isMounted || !viewRef.current) return
        setIsEditorReady(true); isEditorReadyRef.current = true
        readyResolversRef.current.forEach(r => r())
        readyResolversRef.current = []
        callbacks.current.onFileReady?.(currentProject?.id, currentFile)
      })
    }).catch(() => {
      if (isMounted) {
        setIsTexFile(false)
        setFileHash(null)
      }
    })
    return () => { isMounted = false }
  }, [currentFile, currentProject?.id, fileVersion])

  // ── Compile diagnostics ─────────────────────────────────────────────

  useEffect(() => {
    if (!viewRef.current || !isEditorReady || !currentFile) return
    const view = viewRef.current

    if (!compileDiagnostics.length || !useStore.getState().isTexFile) {
      requestAnimationFrame(() => {
        if (!viewRef.current) return
        viewRef.current.dispatch(setDiagnostics(viewRef.current.state, []))
      })
      return
    }

    const filtered = compileDiagnostics.filter(d => d.file === currentFile)
    const cmDiags = filtered.map(d => {
      const line = view.state.doc.line(Math.max(1, Math.min(d.line, view.state.doc.lines)))
      return {
        from: line.from,
        to: line.to,
        severity: d.severity,
        message: d.message,
      }
    })
    requestAnimationFrame(() => {
      if (!viewRef.current) return
      viewRef.current.dispatch(setDiagnostics(viewRef.current.state, cmDiags))
    })
  }, [compileDiagnostics, isEditorReady, currentFile])

  // ── Imperative handle ───────────────────────────────────────────────

  useImperativeHandle(ref, () => ({
    getCursorPosition: () => cursorRef.current,
    getCursorContext: (charCount = 50) => {
      if (!viewRef.current || !isEditorReady) return null
      const view = viewRef.current
      const cursor = cursorRef.current
      const doc = view.state.doc
      const line = doc.line(cursor.line)
      const pos = line.from + cursor.column

      const startPos = Math.max(0, pos - charCount)
      const endPos = Math.min(doc.length, pos + charCount)

      const before = doc.sliceString(startPos, pos).replace(/\s/g, '')
      const after = doc.sliceString(pos, endPos).replace(/\s/g, '')

      return {
        line: cursor.line,
        column: cursor.column,
        before: before.slice(-charCount),
        after: after.slice(0, charCount)
      }
    },
    gotoLine: (n) => {
      if (!viewRef.current) return; const view = viewRef.current; const line = view.state.doc.line(Math.max(1, Math.min(n, view.state.doc.lines)))
      view.dispatch({ selection: { anchor: line.from, head: line.to }, scrollIntoView: true, effects: EditorView.scrollIntoView(line.from, { block: 'center' }) }); view.focus()
    },
    getLineCount: () => viewRef.current ? viewRef.current.state.doc.lines : 0,
    setCursorPosition: (cursor) => {
      if (!viewRef.current || !cursor) return
      const view = viewRef.current
      const lineNumber = Math.max(1, Math.min(Number(cursor.line) || 1, view.state.doc.lines))
      const line = view.state.doc.line(lineNumber)
      const column = Math.max(0, Math.min(Number(cursor.column) || 0, line.length))
      const pos = line.from + column
      cursorRef.current = { line: lineNumber, column }
      view.dispatch({
        selection: { anchor: pos },
        effects: EditorView.scrollIntoView(pos, { block: 'nearest' }),
      })
    },
    scrollToPercent: (pct) => {
      if (!viewRef.current) return
      const scrollDOM = viewRef.current.scrollDOM
      const maxScroll = scrollDOM.scrollHeight - scrollDOM.clientHeight
      if (maxScroll > 0) scrollDOM.scrollTop = Math.max(0, Math.min(1, pct)) * maxScroll
    },
    getContent: () => isEditorReady ? (viewRef.current?.state.doc.toString() || '') : null,
    whenReady: () => {
      if (isEditorReadyRef.current) return Promise.resolve()
      return new Promise(resolve => { readyResolversRef.current.push(resolve) })
    },

    /** Bulk-replace all annotation decorations (used for backend load / SSE refresh). */
    dispatchSetAnnos: (annos) => {
      const view = viewRef.current
      if (!view) return
      const docLen = view.state.doc.length
      // Filter out annotations with no valid position (from >= to or out of bounds)
      const placed = annos.filter(a =>
        a.from != null && a.to != null && a.from >= 0 && a.from < a.to && a.to <= docLen
      )
      view.dispatch({ effects: setAnnosEffect.of(placed) })
      clampNavIndices()
      // Keep the annotation nav panel in sync after decorations change.
      requestAnimationFrame(() => callbacks.current.onAnnoNavScroll?.())
    },

    /** Save-time annotation sync: matches, updates, deletes, saves to backend. */
    syncAnnotationsNow: () => syncAnnotationsNow(),

    /** Revalidate backend annotations against current document using matching algorithm. */
    revalidateBackendAnnos: revalidateBackendAnnotations,

    // ── Annotation navigation ─────────────────────────────────────────
    // Two independent code paths:
    //  • Button clicks (navToAnnotation): pure arithmetic on currentIndexRef —
    //    cur±1, clamp, scroll, echo. Step is always exactly 1, immune to
    //    pixel-exact viewport comparisons.
    //  • Free/user scrolling (computeTopAnnoIndex in the scroll listener):
    //    re-derives the index from content-space geometry (lineBlock top vs
    //    scrollTop), so the counter tracks where the user scrolled.
    //    Approximate by nature, but only affects the displayed number — not
    //    navigation.

    /** Snapshot of all annotation decorations sorted by position.
     *  Returns [{ id, from, to }, ...]. */
    getAnnotationPositions: () => {
      const view = viewRef.current
      return view ? readAnnoPositions(view) : []
    },

    /** Current DISPLAY index (1-based) — what the counter shows. Set by
     *  navToAnnotation immediately, and by free-scroll geometry re-derivation.
     *  NEVER feeds back into button navigation. */
    getCurrentAnnotationIndex: () => displayIndexRef.current,

    /** Navigate to the previous or next annotation. Pure arithmetic on
     *  currentIndexRef (cur±1, clamp) — never reads geometry. Writes both
     *  currentIndexRef (navigation source of truth) and displayIndexRef (so
     *  the counter updates instantly). Skips the scroll dispatch only when
     *  truly stuck at a multi-annotation boundary (avoids drift on repeated
     *  clicks at first/last); with a single annotation the scroll always
     *  fires so the user can jump to it even when the index is already 1. */
    navToAnnotation: (dir) => {
      const view = viewRef.current
      if (!view) return null
      const positions = readAnnoPositions(view)
      if (!positions.length) return null
      const total = positions.length
      const cur = currentIndexRef.current
      let next = cur
      if (dir === 'prev') next = Math.max(1, cur - 1)
      else if (dir === 'next') next = Math.min(total, cur + 1)
      else return null
      // Always sync both refs so the display tracks the button immediately.
      currentIndexRef.current = next
      displayIndexRef.current = next
      // Stamp the suppression window so free-scroll geometry re-derivation
      // is skipped while this scrollIntoView is settling.
      lastNavScrollAt.current = Date.now()
      const stuckAtBoundary = total > 1 && next === cur
      if (!stuckAtBoundary) {
        view.dispatch({
          effects: EditorView.scrollIntoView(positions[next - 1].from, { y: 'start' }),
        })
      }
      // Echo the index to the display (covers no-op-scroll case).
      requestAnimationFrame(() => callbacks.current.onAnnoNavScroll?.())
      return next
    },
  }))

  // ── Active annotation lookup ────────────────────────────────────────

  const activeAnno = findAnnotation(activeAnnotationId)

  // ── Context menu ────────────────────────────────────────────────────

  const ctxMenuSelectionText = ctxMenu
    ? viewRef.current?.state.doc.sliceString(ctxMenu.from, ctxMenu.to) || ''
    : ''
  const isSelectionEmpty = !ctxMenuSelectionText.trim()

  const ctxMenuOptions = ctxMenu ? [
    {
      label: t('annotations.title'),
      action: handleAddAnnotation,
      disabled: isSelectionEmpty,
    },
    {
      label: t('editor.citeInChat'),
      action: () => {
        const text = viewRef.current.state.doc.sliceString(ctxMenu.from, ctxMenu.to)
        setPendingCitation({ text: text.split('\n')[0] + (text.includes('\n') ? '...' : ''), fullText: text })
        setLeftTab('chat')
      },
    },
  ] : []

  // ── Render ──────────────────────────────────────────────────────────

  return (
    <div ref={containerRef} className="w-full h-full overflow-hidden relative" onContextMenu={handleContextMenu} onClick={handleEditorClick}>
        {ctxMenu && createPortal(
          <ContextMenu x={ctxMenu.x} y={ctxMenu.y} options={ctxMenuOptions} onClose={() => setCtxMenu(null)} />,
          document.body
        )}
        {/* Multiple annotation selection list */}
        {annoChoices && createPortal(
          <div className="fixed inset-0 z-[9997]" onClick={() => setAnnoChoices(null)}>
            <div
              className="absolute bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-[0_10px_40px_rgba(0,0,0,0.15)] rounded-xl overflow-hidden py-1 w-72 animate-in fade-in zoom-in duration-150"
              style={{ left: Math.min(annoChoices.x, window.innerWidth - 300), top: annoChoices.y }}
              onClick={e => e.stopPropagation()}
            >
              <div className="px-3 py-2 text-[10px] font-black uppercase tracking-widest text-gray-400 dark:text-gray-500 border-b border-gray-100 dark:border-gray-700">
                {t('annotations.count', { count: annoChoices.ids.length })}
              </div>
              {annoChoices.ids.map(id => {
                const a = findAnnotation(id)
                if (!a) return null
                const firstMsg = a.thread?.[0]?.content || ''
                const preview = (firstMsg || a.originalText || '').slice(0, 50)
                const time = a.thread?.[a.thread.length - 1]?.created_at || a.createdAt || a.created_at || ''
                return (
                  <button
                    key={id}
                    className="w-full text-left px-3 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors border-b border-gray-50 dark:border-gray-700 last:border-0"
                    onClick={() => {
                      setAnnoChoices(null)
                      openAnnotationPopup(id, annoChoices.x, annoChoices.y)
                    }}
                  >
                    <div className="text-xs text-gray-800 dark:text-gray-200 truncate">{preview}{preview.length >= 50 ? '...' : ''}</div>
                    {time && <div className="text-[9px] text-gray-400 dark:text-gray-500 mt-0.5">{formatTimestamp(time)}</div>}
                  </button>
                )
              })}
            </div>
          </div>,
          document.body
        )}
        {activeAnno && popupStyle && createPortal(
            <div onClick={e => e.stopPropagation()}>
                <AnnotationPopup
                    annotation={activeAnno}
                    projectId={currentProject?.id}
                    filePath={currentFile}
                    editorContent={viewRef.current?.state.doc.toString() || ''}
                    onDelete={handleDelAnnotation}
                    onClose={() => {
                      // Destroy pending annotations without replies
                      if (activeAnno.isPending && activeAnno.thread.length === 0) {
                        handleCancelPending(activeAnno.id)
                      } else {
                        setActiveAnnotationId(null)
                        popupAnnoFromRef.current = null
                      }
                    }}
                    onApplyDiff={handleApplyDiff}
                    onPersist={handlePersistAnnotation}
                    onConfirmAnchor={handleConfirmAnnotationAnchor}
                    onReloadAnnotation={handleReloadAnnotations}
                    onSaveBeforeAnnotationChat={() => callbacks.current.onSaveBeforeAnnotationChat?.()}
                    autoFocusReply={activeAnno.isPending && activeAnno.thread.length === 0}
                    popupStyle={popupStyle}
                />
            </div>,
            document.body
        )}
    </div>
  )
})

export default memo(Editor)
