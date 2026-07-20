/**
 * SynthesisTab — pure layout component for the editor + preview panel.
 *
 * All business-logic hooks (useCompile, useSave, useFileActions, useAutoCompile)
 * are called by the parent (EditorView) and passed down as props. This eliminates
 * the previous zustand EventBus pattern (_synthesis* refs) and the double-hook
 * problem where both EditorView and SynthesisTab called the same hooks.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { compileAPI } from '../api'
import { storage } from '../utils/storage'
import { toastError } from './Toast'
import Editor from './Editor'
import Preview from './Preview'
import NotebookEditor from './NotebookEditor'
import { ResizablePanels } from './ResizablePanels'
import { ChevronRight, ChevronUp, ChevronDown, FileText, Loader2 } from 'lucide-react'

export default function SynthesisTab({
  projectId,
  editorRef,
  previewRef,
  handleSave,
  handleFileSelect,
  handleExitNotebook,
  onFileReady,
  onSaveBeforeAnnotationChat,
}) {
  const { t } = useTranslation()
  const currentFile = useStore(s => s.currentFile)
  const isTexFile = useStore(s => s.isTexFile)
  const isNotebookMode = useStore(s => s.isNotebookMode)
  const isLoadingFile = useStore(s => s.isLoadingFile)
  const annotations = useStore(s => s.annotations)
  const setHasUnsavedChanges = useStore(s => s.setHasUnsavedChanges)

  // ── Annotation navigation ────────────────────────────────────────────
  // Count + current index read from the LIVE CodeMirror decoration field
  // (via the Editor imperative handle), not from the annotations array —
  // positions in Zustand go stale between save-time syncs and LLM-driven
  // decoration refreshes. Refresh is triggered on scroll (rAF-throttled),
  // on annotation store changes, and on file load.
  const [annoCount, setAnnoCount] = useState(0)
  const [topAnnoIndex, setTopAnnoIndex] = useState(null)
  const annoNavRafRef = useRef(0)

  const refreshAnnoNav = useCallback(() => {
    if (!editorRef.current) return
    const positions = editorRef.current.getAnnotationPositions?.() ?? []
    setAnnoCount(positions.length)
    // Read the explicit current index (set by button arithmetic or free-scroll
    // geometry re-derivation inside the editor). Never re-derive here — the
    // editor's scroll listener owns that decision.
    setTopAnnoIndex(editorRef.current.getCurrentAnnotationIndex?.() ?? null)
  }, [editorRef])

  const onAnnoNavScroll = useCallback(() => {
    cancelAnimationFrame(annoNavRafRef.current)
    annoNavRafRef.current = requestAnimationFrame(refreshAnnoNav)
  }, [refreshAnnoNav])

  // Refresh after file load / ready
  useEffect(() => {
    if (!currentFile) { setAnnoCount(0); setTopAnnoIndex(null); return }
    editorRef.current?.whenReady?.().then(refreshAnnoNav).catch(() => {})
  }, [currentFile, editorRef, refreshAnnoNav])

  // Refresh when decorations change via SSE (annotations array mutation)
  useEffect(() => { refreshAnnoNav() }, [annotations, refreshAnnoNav])

  useEffect(() => {
    return () => cancelAnimationFrame(annoNavRafRef.current)
  }, [])

  const prevAnnotation = useCallback(() => {
    // Pure arithmetic inside the editor (cur±1). Never reads geometry, so the
    // step is always exactly 1 and the button never gets stuck.
    editorRef.current?.navToAnnotation?.('prev')
  }, [editorRef])

  const nextAnnotation = useCallback(() => {
    editorRef.current?.navToAnnotation?.('next')
  }, [editorRef])

  const mdPreviewRafRef = useRef(0)
  const editorScrollRafRef = useRef(0)
  const editorCursorRafRef = useRef(0)
  const previewScrollRafRef = useRef(0)

  useEffect(() => {
    return () => {
      cancelAnimationFrame(editorScrollRafRef.current)
      cancelAnimationFrame(editorCursorRafRef.current)
      cancelAnimationFrame(previewScrollRafRef.current)
      cancelAnimationFrame(mdPreviewRafRef.current)
    }
  }, [])

  const showPreview = !isNotebookMode

  return (
    <ResizablePanels
      initialSizes={showPreview ? ['50%', '1'] : ['1']}
      resizerContent={showPreview ? [(
        <button
          onMouseDown={(e) => e.stopPropagation()}
          onClick={() => {
            const { line, column } = editorRef.current.getCursorPosition()
            compileAPI.synctex(projectId, {
              type: 'forward',
              file: currentFile || '',
              line, column
            }).then(res => {
              if (res.success) previewRef.current?.scrollToPage(res.page, res.x, res.y)
            }).catch(() => { /* no SyncTeX mapping for this cursor, or transient network error — both non-fatal for the forward-jump button */ })
          }} style={{ display: isTexFile ? 'block' : 'none' }} className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-[100] p-2.5 bg-sigma-600 text-white rounded-full shadow-2xl border-4 border-white hover:scale-110 active:scale-90 transition-all">
          <ChevronRight className="w-5 h-5" />
        </button>
      )] : []}
    >
      {/* Main content area */}
      <main className="h-full flex flex-col bg-white dark:bg-gray-900 overflow-hidden min-w-0 relative">
        {/* File-loading overlay */}
        {isLoadingFile && (
          <div className="absolute inset-0 z-50 flex items-center justify-center bg-white/70 dark:bg-gray-900/70 backdrop-blur-sm animate-in fade-in duration-200">
            <div className="flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-800 rounded-xl shadow-lg border border-gray-100 dark:border-gray-700 animate-in zoom-in duration-200">
              <Loader2 className="w-5 h-5 text-sigma-600 animate-spin" />
              <span className="text-sm text-gray-500 dark:text-gray-400">{t('common.loading')}</span>
            </div>
          </div>
        )}
        {isNotebookMode ? (
          <NotebookEditor projectId={projectId} filePath={currentFile} onBack={handleExitNotebook} />
        ) : !currentFile ? (
          <div className="h-full flex items-center justify-center bg-gray-50/50 dark:bg-gray-900">
            <div className="text-center text-gray-300 dark:text-gray-600">
              <FileText className="w-12 h-12 mx-auto mb-3" />
              <p className="text-sm font-medium">{t('synthesis.noFile')}</p>
              <p className="text-xs mt-1">{t('synthesis.selectFile')}</p>
            </div>
          </div>
        ) : (
          <div className="h-full flex flex-col min-h-0">
            <div className="h-8 border-b border-gray-100 dark:border-gray-800 px-4 flex items-center justify-between bg-white dark:bg-gray-900 flex-shrink-0">
              <div className="flex items-center min-w-0">
                <FileText className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 mr-2 flex-shrink-0" />
                <span className="text-xs font-bold text-gray-600 dark:text-gray-400 truncate">{currentFile}</span>
              </div>
              {annoCount > 0 && (
                <div className="flex items-center gap-1.5 flex-shrink-0 ml-2">
                  <span className="text-[11px] font-bold tabular-nums text-gray-500 dark:text-gray-400">
                    {t('annotations.navPosition', { current: topAnnoIndex ?? '—', total: annoCount })}
                  </span>
                  <button
                    onClick={prevAnnotation}
                    disabled={annoCount > 1 && topAnnoIndex === 1}
                    title={t('annotations.prevAnnotation')}
                    className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent dark:disabled:hover:bg-transparent transition-colors"
                  >
                    <ChevronUp className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={nextAnnotation}
                    disabled={annoCount > 1 && topAnnoIndex === annoCount}
                    title={t('annotations.nextAnnotation')}
                    className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent dark:disabled:hover:bg-transparent transition-colors"
                  >
                    <ChevronDown className="w-3.5 h-3.5" />
                  </button>
                </div>
              )}
            </div>
            <div className="flex-1 min-h-0">
              <Editor
                ref={editorRef}
                onFileReady={onFileReady}
                onSaveBeforeAnnotationChat={onSaveBeforeAnnotationChat}
                onContentChange={(c) => {
                  setHasUnsavedChanges(true)
                  if (currentFile?.endsWith('.md')) {
                    cancelAnimationFrame(mdPreviewRafRef.current)
                    mdPreviewRafRef.current = requestAnimationFrame(() => {
                      previewRef.current?.setMarkdownContent(c)
                    })
                  }
                }}
                onSave={() => handleSave(true)}
                onAutoSave={() => handleSave(false, true)}
                onScroll={(ratio) => {
                  if (!currentFile) return
                  cancelAnimationFrame(editorScrollRafRef.current)
                  editorScrollRafRef.current = requestAnimationFrame(() => {
                    storage.setEditorScroll(projectId, currentFile, ratio)
                  })
                }}
                onLineChange={(l) => {
                  if (currentFile?.endsWith('.md') && useStore.getState().mdSyncScroll) previewRef.current?.scrollToLine(l)
                }}
                onCursorChange={(cursor) => {
                  if (!currentFile) return
                  cancelAnimationFrame(editorCursorRafRef.current)
                  editorCursorRafRef.current = requestAnimationFrame(() => {
                    storage.setEditorCursor(projectId, currentFile, cursor)
                  })
                  if (currentFile.endsWith('.md') && useStore.getState().mdSyncScroll) {
                    previewRef.current?.highlightLine(cursor.line)
                  }
                }}
                onAnnoNavScroll={onAnnoNavScroll}
              />
            </div>
          </div>
        )}
      </main>

      {/* Preview panel */}
      {showPreview && (
        <aside className="h-full flex flex-col overflow-hidden shadow-inner">
          <Preview ref={previewRef} onPageClick={async (p, x, y) => {
            // Backward SyncTeX jump is only meaningful for compiled PDFs,
            // which carry a .synctex.gz. For standalone PDFs there is nothing
            // to jump to, so the double-click is a no-op (no error toast).
            if (useStore.getState().previewSource.kind !== 'pdf-compiled') return
            try {
              const res = await compileAPI.synctex(projectId, {
                type: 'backward', page: p, x, y
              })
              if (res.success && res.file && res.line) {
                if (res.file !== currentFile) {
                  const opened = await handleFileSelect({ path: res.file, name: res.file.split(/[/\\]/).pop() })
                  if (!opened) {
                    toastError(t('compile.synctexFailed'))
                    return
                  }
                }
                await editorRef.current?.whenReady()
                editorRef.current?.gotoLine(res.line)
              } else {
                toastError(t('compile.synctexFailed'))
              }
            } catch {
              toastError(t('compile.synctexFailed'))
            }
          }} onScroll={(ratio) => {
            // Key scroll position by the preview's actual source path, not by
            // the editor's currentFile — they legitimately differ when a
            // standalone PDF or binary file is being previewed.
            const previewSource = useStore.getState().previewSource
            const previewPath = previewSource.kind === 'pdf-compiled'
              ? (previewSource.outputName || previewSource.path || '')
              : (previewSource.path || '')
            if (!previewPath) return
            cancelAnimationFrame(previewScrollRafRef.current)
            previewScrollRafRef.current = requestAnimationFrame(() => {
              storage.setPreviewScroll(projectId, previewPath, ratio)
            })
          }} />
        </aside>
      )}
    </ResizablePanels>
  )
}
