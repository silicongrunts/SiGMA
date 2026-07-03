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
import { indentOnInput, syntaxHighlighting, defaultHighlightStyle, foldGutter } from '@codemirror/language'
import { oneDarkHighlightStyle } from '@codemirror/theme-one-dark'
import { lineNumbers, highlightActiveLine, highlightActiveLineGutter, dropCursor } from '@codemirror/view'

import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { markdown } from '@codemirror/lang-markdown'
import { json } from '@codemirror/lang-json'
import { javascript } from '@codemirror/lang-javascript'
import { python } from '@codemirror/lang-python'
import { useStore } from '../store/useStore'
import { useTheme } from '../hooks/useTheme'
import { useTranslation } from 'react-i18next'
import { filesAPI } from '../api'
import { TEX_EXTS } from '../utils/constants'
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

const lightEditorTheme = EditorView.theme({
  '&': { height: '100%', fontSize: '14px', backgroundColor: '#ffffff' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { overflow: 'auto !important', fontFamily: "'JetBrains Mono', monospace" },
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
  '&': { height: '100%', fontSize: '14px', backgroundColor: '#111827', color: '#e5e7eb' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { overflow: 'auto !important', fontFamily: "'JetBrains Mono', monospace" },
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

const getLanguage = (path) => {
  const ext = path?.split('.').pop()?.toLowerCase()
  if (TEX_EXTS.includes(ext)) return StreamLanguage.define(stex)
  if (ext === 'md') return markdown(); if (ext === 'json') return json(); if (ext === 'js') return javascript(); if (ext === 'py') return python()
  return []
}

// ── Pending annotation ID generator ──────────────────────────────────
let _pendingCounter = 0
function _genPendingId() { return `pending_${Date.now()}_${++_pendingCounter}` }

const Editor = forwardRef(({ onContentChange, onScroll, onSave, onAutoSave, onLineChange, onCursorChange, onFileReady, onSaveBeforeAnnotationChat }, ref) => {
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

  const callbacks = useRef({ onContentChange, onScroll, onSave, onAutoSave, onLineChange, onCursorChange, onFileReady, onSaveBeforeAnnotationChat })
  useEffect(() => { callbacks.current = { onContentChange, onScroll, onSave, onAutoSave, onLineChange, onCursorChange, onFileReady, onSaveBeforeAnnotationChat } })

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

    // Save to backend. The backend preserves stored anchors for orphan
    // annotations, so placeholder render positions are not written back.
    const toSave = resolved
    if (toSave.length > 0 || toRemove.length > 0) {
      try { await filesAPI.saveAnnotations(pid, file, toSave) } catch { /* non-critical */ }
    }
  }, [getDecorationPosition, setAnnotations])

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

    try {
      await filesAPI.saveAnnotations(currentProject.id, currentFile, updatedAnnotations)
    } catch (e) {
      console.error('Failed to save annotations after delete', e)
    }
  }, [currentProject?.id, currentFile, deleteAnnotation, handleCancelPending])

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

    setAnnotations(revalidated)

    try { await filesAPI.saveAnnotations(currentProject.id, currentFile, revalidated) } catch { /* non-critical */ }
    if (callbacks.current.onContentChange) callbacks.current.onContentChange(view.state.doc.toString())
  }, [currentProject?.id, currentFile, setAnnotations, getDecorationPosition])

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

  const handleReloadAnnotations = useCallback(async (annotationId = null) => {
    const view = viewRef.current
    const pid = useStore.getState().currentProject?.id
    const file = useStore.getState().currentFile
    if (!view || !pid || !file) return null

    const loaded = await filesAPI.loadAnnotations(pid, file)
    const validated = revalidateBackendAnnotations(loaded)
    setAnnotations(validated)
    view.dispatch({ effects: setAnnosEffect.of(validated) })
    return annotationId ? validated.find(a => a.id === annotationId) || null : null
  }, [revalidateBackendAnnotations, setAnnotations])

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
    const extensions = [
      lineNumbers(), highlightActiveLineGutter(), highlightActiveLine(),
      history(), dropCursor(), EditorState.allowMultipleSelections.of(false),
      indentOnInput(), syntaxCompartment.of(syntaxHighlighting(document.documentElement.classList.contains('dark') ? oneDarkHighlightStyle : defaultHighlightStyle, { fallback: true })),
      bracketMatching(), closeBrackets(), autocompletion(), highlightSelectionMatches(),
      EditorView.lineWrapping, foldGutter(), themeCompartment.of(document.documentElement.classList.contains('dark') ? darkEditorTheme : lightEditorTheme), annotationField,
      languageConf.of([]),
      keymap.of([
        { key: 'Mod-s', run: (view) => { if(autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); callbacks.current.onSave?.(); return true; }, preventDefault: true },
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
        const from = popupAnnoFromRef.current
        if (from != null) {
          const coords = view.coordsAtPos(from)
          if (coords) {
            setPopupStyle(prev => prev ? { left: coords.left, top: coords.top } : null)
          }
        }
    }, { passive: true })
    viewRef.current = view; return () => view.destroy()
  }, [])

  // ── Reconfigure editor theme when dark mode toggles ──
  useEffect(() => {
    if (!viewRef.current) return
    viewRef.current.dispatch({
      effects: [
        themeCompartment.reconfigure(isDark ? darkEditorTheme : lightEditorTheme),
        syntaxCompartment.reconfigure(syntaxHighlighting(isDark ? oneDarkHighlightStyle : defaultHighlightStyle, { fallback: true })),
      ],
    })
  }, [isDark])

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
    },

    /** Save-time annotation sync: matches, updates, deletes, saves to backend. */
    syncAnnotationsNow: () => syncAnnotationsNow(),

    /** Revalidate backend annotations against current document using matching algorithm. */
    revalidateBackendAnnos: revalidateBackendAnnotations,
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
