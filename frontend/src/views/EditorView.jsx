/**
 * EditorView — main workspace for a single project.
 *
 * Owns all business-logic hooks (useCompile, useSave, useFileActions,
 * useAutoCompile) and passes callbacks + refs down to child layout
 * components (SynthesisTab, Header, FileTree).
 *
 * A single ChatPanel instance is shared across Explore, Library, and Synthesis
 * so tab switches do not reload the active chat session.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { projectsAPI, filesAPI, libraryAPI } from '../api'
import { LogModal, ModalOverlay } from '../components/Modal'
import { EditorHeader } from '../components/Header'
import FileTree from '../components/FileTree'
import HistoryPanel from '../components/HistoryPanel'
import SynthesisTab from '../components/SynthesisTab'
import ChatPanel from '../components/ChatPanel'
import BrowserVNC from '../components/BrowserVNC'
import LibraryBrowser from '../components/LibraryBrowser'
import AskUserQuestionModal from '../components/AskUserQuestionModal'
import PlanApprovalDialog from '../components/PlanApprovalDialog'
import PermissionDialog from '../components/PermissionDialog'
import { ResizablePanels } from '../components/ResizablePanels'
import { LibraryActionsContext } from '../components/LibraryActionsContext'
import { useCompile } from '../hooks/useCompile'
import { useSave } from '../hooks/useSave'
import { useFileActions } from '../hooks/useFileActions'
import { useAutoCompile } from '../hooks/useAutoCompile'
import FileConflictModal from '../components/FileConflictModal'
import TerminalPanel from '../components/TerminalPanel'
import { storage } from '../utils/storage'
import { toastError } from '../components/Toast'
import { RotateCw, AlertTriangle, Database, ArrowLeft, Loader } from 'lucide-react'

// SiGMA IDs are 16-char lowercase hex (generate_id → uuid4().hex[:16]).
const ID_FORMAT = /^[0-9a-f]{16}$/

export default function EditorView() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { id: projectId } = useParams()

  // ── Refs (owned here, passed down to SynthesisTab) ──
  const editorRef = useRef(null)
  const previewRef = useRef(null)
  const fileTreeRef = useRef(null)

  // ── Clear stale store state when project changes ──
  useEffect(() => {
    const s = useStore.getState()
    if (s.currentProject?.id !== projectId) {
      s.clearCurrentFile()
      s.setHasUnsavedChanges(false)
    }

    return () => {
      s.setShowTerminal(false)
      s.clearTerminalState(projectId)
    }
  }, [projectId])

  // ── Store selectors (individual to avoid broad re-renders) ──
  const currentProject = useStore(s => s.currentProject)
  const setCurrentProject = useStore(s => s.setCurrentProject)
  const currentFile = useStore(s => s.currentFile)
  const setShowLogModal = useStore(s => s.setShowLogModal)
  const showLogModal = useStore(s => s.showLogModal)
  const compileLogs = useStore(s => s.compileLogs)
  const compileFailed = useStore(s => s.compileFailed)
  const compileDiagnostics = useStore(s => s.compileDiagnostics)
  const pendingCitation = useStore(s => s.pendingCitation)
  const clearCitation = useStore(s => s.clearCitation)
  const setActiveTab = useStore(s => s.setActiveTab)
  const setLeftTab = useStore(s => s.setLeftTab)
  const setPendingAutoMessage = useStore(s => s.setPendingAutoMessage)
  const setAnnotations = useStore(s => s.setAnnotations)
  const incrementFileVersion = useStore(s => s.incrementFileVersion)
  const incrementNotebookVersion = useStore(s => s.incrementNotebookVersion)
  const leftTab = useStore(s => s.leftTab)
  const activeTab = useStore(s => s.activeTab)
  const showTerminal = useStore(s => s.showTerminal)

  // ── LibraryActions context for Header ↔ LibraryBrowser ──
  const [libraryActions, setLibraryActions] = useState({
    onRefresh: null,
    onReprocessAll: null,
    statusSummary: null,
    hasFailed: false,
    onNewFolder: null,
    onUploadFiles: null,
    selectedDocId: null,
    selectedDocTitle: null,
    currentFolderPath: null,
    indexingStatus: null,
    revealFolderRequest: null,
    revealDocumentRequest: null,
  })

  const updateLibraryActions = useCallback((updates) => {
    setLibraryActions(prev => ({ ...prev, ...updates }))
  }, [])

  // ── Database incompatible state ──
  const [dbError, setDbError] = useState(null)
  const [resetConfirm, setResetConfirm] = useState(false)
  const [resetting, setResetting] = useState(false)

  const handleResetDatabase = useCallback(async () => {
    setResetting(true)
    try {
      await projectsAPI.resetDatabase(projectId)
      window.location.reload()
    } catch (err) {
      toastError(t('dbError.resetFailed') + ': ' + err.message)
      setResetting(false)
    }
  }, [projectId, t])

  // ── Shared hooks ──
  const handleCompileRef = useRef(null)
  const { handleSave, conflictState, resolveConflict } = useSave({ projectId, editorRef, handleCompileRef })
  const { handleCompile } = useCompile({ projectId, editorRef, previewRef, handleSave })
  const { handleFileSelect, handleExitNotebook } = useFileActions({ projectId, editorRef, previewRef, handleSave })
  handleCompileRef.current = handleCompile
  useAutoCompile({ currentFile, handleCompile })

  // ── Build user_state for LLM status context ──
  const getUserState = useCallback(() => {
    const state = useStore.getState()
    if (activeTab === 'explore') return { active_tab: 'explore' }
    if (activeTab === 'library') {
      const userState = { active_tab: 'library' }
      if (libraryActions.selectedDocId) {
        userState.viewing_document = {
          id: libraryActions.selectedDocId,
          title: libraryActions.selectedDocTitle,
        }
      }
      if (libraryActions.currentFolderPath && libraryActions.currentFolderPath !== 'Library') {
        userState.folder_path = libraryActions.currentFolderPath
      }
      if (libraryActions.indexingStatus) {
        const indexingStatus = libraryActions.indexingStatus
        const active = indexingStatus.pending + indexingStatus.processing + indexingStatus.indexing + (indexingStatus.cancelling || 0)
        if (active > 0 || indexingStatus.failed > 0) {
          userState.indexing_status = indexingStatus
        }
      }
      return userState
    }

    const userState = { active_tab: 'synthesis' }
    if (state.currentFile) userState.editor_file = state.currentFile
    if (state.previewSource.path) userState.preview_file = state.previewSource.path
    // Notebook-specific context
    if (state.currentFile?.endsWith('.ipynb')) {
      userState.notebook_mode = true
      if (state.activeKernels?.length > 0) {
        userState.active_kernels = state.activeKernels.map(k => ({
          id: k.id,
          display_name: k.display_name,
          execution_state: k.execution_state,
        }))
      }
    }
    const cursor = editorRef.current?.getCursorContext?.(50)
    if (cursor) userState.cursor = cursor
    if (state.pendingCitation?.fullText) userState.citation = state.pendingCitation.fullText
    return userState
  }, [activeTab, libraryActions])

  // ── Save editor content before sending chat (ensures AI sees latest file) ──
  const saveBeforeChat = useCallback(async () => {
    try {
      const state = useStore.getState()
      if (!state.currentFile || !editorRef.current) return true
      return await handleSave(false, false)
    } catch {
      return false
    }
  }, [handleSave])

  // ── Ask SiGMA from LogModal ──
  const handleAskSiGMA = useCallback((logs) => {
    setShowLogModal(false)
    setActiveTab('synthesis')
    setLeftTab('chat')
    // Truncate very long logs to the most relevant part (last errors)
    const maxLen = 4000
    const truncated = logs && logs.length > maxLen ? '...\n' + logs.slice(-maxLen) : (logs || '')
    setPendingAutoMessage({
      text: t('editor.askLogMessage', { log: truncated }),
    })
  }, [setShowLogModal, setActiveTab, setLeftTab, setPendingAutoMessage])

  // ── Jump to error line from LogModal ──
  const handleJumpToError = useCallback((file, line) => {
    setShowLogModal(false)
    if (file && file !== currentFile) {
      handleFileSelect({ path: file, name: file.split('/').pop() })
    }
    // Wait for the editor to load the file, then jump to line
    requestAnimationFrame(() => {
      editorRef.current?.whenReady()?.then(() => {
        editorRef.current?.gotoLine(line)
      })
    })
  }, [setShowLogModal, currentFile, handleFileSelect, editorRef])

  // ── Chat citation dispatcher: sigma:// links from LLM messages ──
  // Three shapes:
  //   sigma://library/folder/<id>
  //   sigma://library/doc/<id>
  //   sigma://synthesis/file?path=<relpath>&line=<n>
  // Every shape is validated before navigation: unknown schemes, malformed
  // IDs, missing items, folder/doc type confusion, project-escape paths,
  // and out-of-range line numbers are all rejected with a toast.
  const handleCitation = useCallback(async (href) => {
    let url
    try { url = new URL(href) } catch { toastError(t('chat.citation.badFormat')); return }
    const kind = url.hostname // 'library' or 'synthesis'
    if (kind === 'library') {
      const segments = url.pathname.replace(/^\/+/, '').split('/').filter(Boolean)
      const [sub, id] = segments
      if ((sub !== 'folder' && sub !== 'doc') || !id || !ID_FORMAT.test(id)) {
        toastError(t('chat.citation.badFormat')); return
      }
      // Verify the item exists and its type matches the URL's claim before
      // navigating — a folder ID in a doc slot (or vice-versa) is a type
      // error, not a navigation target.
      let doc
      try { doc = await libraryAPI.get(projectId, id, { include_content: false }) }
      catch { toastError(t('chat.citation.notFound')); return }
      if (!doc || doc.is_folder !== (sub === 'folder')) {
        toastError(t('chat.citation.typeMismatch')); return
      }
      setActiveTab('library')
      if (sub === 'folder') {
        updateLibraryActions({ revealFolderRequest: { folderId: id, requestId: Date.now() } })
      } else {
        updateLibraryActions({ revealDocumentRequest: { docId: id, requestId: Date.now() } })
      }
      return
    }
    if (kind === 'synthesis') {
      const rawPath = url.searchParams.get('path')
      if (!rawPath) { toastError(t('chat.citation.badFormat')); return }
      // Project-containment check: reject absolute paths and ".." traversal.
      const normalized = rawPath.replace(/\\/g, '/').replace(/^\.\/+/, '')
      if (/^(\/|[a-zA-Z]:[\\/]|\/\/)/.test(normalized)) {
        toastError(t('chat.citation.outsideProject')); return
      }
      const stack = []
      let escaped = false
      for (const seg of normalized.split('/')) {
        if (!seg || seg === '.') continue
        if (seg === '..') { if (stack.length === 0) { escaped = true; break }; stack.pop(); continue }
        stack.push(seg)
      }
      if (escaped || stack.length === 0) {
        toastError(t('chat.citation.outsideProject')); return
      }
      // line must be a positive integer when present.
      const line = url.searchParams.get('line')
      const lineNum = line === null ? null : parseInt(line, 10)
      if (line !== null && (!Number.isInteger(lineNum) || lineNum <= 0)) {
        toastError(t('chat.citation.badFormat')); return
      }
      const safePath = stack.join('/')
      setActiveTab('synthesis')
      // Open the file first; handleFileSelect returns false when the file
      // doesn't exist or is binary — abort and toast rather than jumping a
      // stray line in whatever file was previously open.
      const opened = await handleFileSelect({ path: safePath, name: safePath.split('/').pop() })
      if (!opened) { toastError(t('chat.citation.notFound')); return }
      if (lineNum !== null) {
        // requestAnimationFrame lets SynthesisTab mount (when arriving from
        // another tab) before we read editorRef and the editor's ready state.
        requestAnimationFrame(() => {
          editorRef.current?.whenReady()?.then(() => {
            const total = editorRef.current?.getLineCount?.() || 0
            if (lineNum > total) {
              toastError(t('chat.citation.lineOutOfRange', { line: lineNum })); return
            }
            editorRef.current?.gotoLine(lineNum)
          })
        })
      }
      return
    }
    // Unknown host (e.g. sigma://garbage/...) — not a valid citation form.
    toastError(t('chat.citation.badFormat'))
  }, [t, projectId, setActiveTab, updateLibraryActions, handleFileSelect, editorRef])

  const pathMatchesCurrentFile = useCallback((changedPath, currentPath) => {
    if (!changedPath || !currentPath) return false
    const changed = String(changedPath).replace(/\\/g, '/').replace(/^\.\/+/, '')
    const current = String(currentPath).replace(/\\/g, '/').replace(/^\.\/+/, '')
    if (changed === current) return true
    if (!projectId) return false

    const projectPrefix = `${projectId}/`
    if (changed.startsWith(projectPrefix)) {
      return changed.slice(projectPrefix.length) === current
    }

    const projectSegment = `/${projectId}/`
    const idx = changed.indexOf(projectSegment)
    if (idx >= 0) {
      return changed.slice(idx + projectSegment.length) === current
    }

    return false
  }, [projectId])

  // ── File change notification from AI tools ──
  const handleFileChanged = useCallback(async (paths) => {
    fileTreeRef.current?.refresh()
    const state = useStore.getState()
    const cur = state.currentFile
    if (!cur) return
    const isNotebook = cur.endsWith('.ipynb')
    if (paths.length === 0) {
      // run_bash / Agent — unknown which files changed, refresh everything
      if (state.hasUnsavedChanges) return
      if (isNotebook) incrementNotebookVersion()
      else incrementFileVersion()
    } else if (paths.some(p => pathMatchesCurrentFile(p, cur))) {
      if (state.hasUnsavedChanges) return
      if (isNotebook) incrementNotebookVersion()
      else incrementFileVersion()
    } else {
      return
    }
    // Sync markdown preview (editor's onContentChange is skipped during reload)
    if (cur.endsWith('.md')) {
      try {
        const data = await filesAPI.read(projectId, cur)
        const content = typeof data === 'string' ? data : (data?.content || '')
        previewRef.current?.setMarkdownContent(content)
      } catch (e) { console.warn('Failed to sync markdown preview:', e) }
    }
  }, [incrementFileVersion, incrementNotebookVersion, pathMatchesCurrentFile, projectId])

  const handleAnnotationChanged = useCallback((fileName) => {
    const cur = useStore.getState().currentFile
    if (!cur) return
    if (!fileName || fileName === cur) {
      // Wait for editor readiness (no-op if already ready) to avoid
      // validating annotations against stale document content.
      ;(editorRef.current?.whenReady?.() || Promise.resolve()).then(() => {
        filesAPI.loadAnnotations(projectId, cur).then(data => {
          const validated = editorRef.current?.revalidateBackendAnnos?.(data) ?? data
          setAnnotations(validated)
          editorRef.current?.dispatchSetAnnos?.(validated)
        }).catch(e => console.warn('Failed to refresh annotations:', e))
      })
    }
  }, [projectId])

  // ── Load project on mount ──
  useEffect(() => {
    if (!projectId) { navigate('/'); return }
    let cancelled = false
    projectsAPI.get(projectId).then(p => {
      if (cancelled) return
      if (p.db_status === 'incompatible' || p.db_status === 'error') {
        setCurrentProject(p)
        setDbError(p.db_status)
        return
      }
      setCurrentProject(p)
      storage.touchProject(projectId)
      const projectState = storage.getProject(projectId)
      setActiveTab(projectState.workspace.activeTab)
      setLeftTab(projectState.synthesis.leftTab)
      const last = projectState.synthesis.editorFile
      const fallback = p.main_file || ''
      const target = last || fallback
      if (target) {
        handleFileSelect({ path: target, name: target.split('/').pop() }).then(ok => {
          if (cancelled || ok) return
          // Failed to open (e.g. deleted file) — clear state on both sides.
          // clearCurrentFile() also resets previewSource, which fires Preview's
          // load effect and resets all rendering state — no separate reset call.
          const state = useStore.getState()
          state.clearCurrentFile()

          if (last && target !== fallback) {
            storage.removeLastFile(projectId)
            if (fallback) {
              handleFileSelect({ path: fallback, name: fallback.split('/').pop() }).then(fallbackOk => {
                if (!cancelled && !fallbackOk) {
                  useStore.getState().clearCurrentFile()
                }
              })
            }
            return
          }

          if (fallback && target === fallback) {
            const nextProject = { ...p, main_file: '' }
            setCurrentProject(nextProject)
            projectsAPI.update(projectId, { main_file: '' })
              .then(updated => {
                if (!cancelled && useStore.getState().currentProject?.id === projectId) {
                  setCurrentProject(updated)
                }
              })
              .catch(e => console.warn('Failed to sync cleared main_file:', e))
          }
        })
      }
    }).catch(() => navigate('/'))
    return () => { cancelled = true }
  }, [projectId])

  // ── Load auto-approve settings from backend on project change ──
  const loadAutoApproveSettings = useStore(s => s.loadAutoApproveSettings)
  useEffect(() => {
    if (projectId) loadAutoApproveSettings(projectId)
  }, [projectId, loadAutoApproveSettings])

  // ── Load annotations when Editor finishes loading a file ──
  // Called directly from Editor's onFileReady callback — guarantees the
  // document content is in CodeMirror and decorations can be placed.
  const handleFileReady = useCallback((pid, file) => {
    if (!pid || !file) return
    filesAPI.loadAnnotations(pid, file).then(data => {
      const validated = editorRef.current?.revalidateBackendAnnos?.(data) ?? data
      setAnnotations(validated)
      editorRef.current?.dispatchSetAnnos?.(validated)
    }).catch(e => console.warn('Failed to load annotations on file ready:', e))
    const synthesis = storage.getSynthesis(pid)
    const editorRatio = synthesis.editorScrollRatioByFile[file]
    const previewRatio = synthesis.previewScrollRatioByFile[file]
    const editorCursor = synthesis.editorCursorByFile[file]
    requestAnimationFrame(() => {
      if (Number.isFinite(editorRatio)) editorRef.current?.scrollToPercent?.(editorRatio)
      if (editorCursor) editorRef.current?.setCursorPosition?.(editorCursor)
      if (Number.isFinite(previewRatio)) previewRef.current?.scrollToPercent?.(previewRatio)
    })
  }, [setAnnotations])

  // Warn before closing tab with unsaved changes
  useEffect(() => {
    const handler = (e) => {
      if (useStore.getState().hasUnsavedChanges) {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [])

  // ── Database incompatible overlay ──
  if (dbError) return (
    <div className="h-screen w-screen flex flex-col items-center justify-center bg-white dark:bg-gray-900 px-6">
      <div className="max-w-md text-center">
        <AlertTriangle className="w-14 h-14 mx-auto text-red-500" />
        <h2 className="mt-5 text-lg font-bold text-gray-800 dark:text-gray-200">{t('dbError.title')}</h2>
        <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">{t('dbError.message')}</p>
        <div className="mt-7 flex flex-col gap-3">
          <button
            onClick={() => setResetConfirm(true)}
            className="flex items-center justify-center gap-2 px-5 py-2.5 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-medium transition-colors"
          >
            <Database className="w-4 h-4" />
            {t('dbError.resetButton')}
          </button>
          <button
            onClick={() => navigate('/')}
            className="flex items-center justify-center gap-2 px-5 py-2.5 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 text-sm font-medium hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            {t('dbError.back')}
          </button>
        </div>
      </div>
      <ModalOverlay isOpen={resetConfirm} onClose={() => !resetting && setResetConfirm(false)}>
        <div className="p-8 text-center">
          <div className="mx-auto w-16 h-16 rounded-full flex items-center justify-center mb-6 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400">
            <AlertTriangle className="w-8 h-8" />
          </div>
          <h2 className="text-2xl font-black text-gray-900 dark:text-gray-100 mb-6 tracking-tight">{t('dbError.resetTitle')}</h2>
          <div className="text-left space-y-4 mb-8">
            <div>
              <p className="font-semibold text-red-600 dark:text-red-400 text-sm">{t('dbError.willLose')}</p>
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{t('dbError.willLoseItems')}</p>
            </div>
            <div>
              <p className="font-semibold text-green-600 dark:text-green-400 text-sm">{t('dbError.willKeepLabel')}</p>
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{t('dbError.willKeep')}</p>
            </div>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => setResetConfirm(false)}
              disabled={resetting}
              className="flex-1 py-3.5 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors disabled:opacity-50"
            >{t('modal.confirm.cancel')}</button>
            <button
              onClick={handleResetDatabase}
              disabled={resetting}
              className="flex-1 py-3.5 text-white font-black rounded-2xl bg-red-600 hover:bg-red-700 shadow-lg shadow-red-200 dark:shadow-none transition-all active:scale-95 disabled:opacity-80 flex items-center justify-center gap-2"
            >
              {resetting && <Loader className="w-4 h-4 animate-spin" />}
              {resetting ? t('dbError.resetting') : t('dbError.confirmReset')}
            </button>
          </div>
        </div>
      </ModalOverlay>
    </div>
  )

  // ── Loading state ──
  if (!currentProject) return (
    <div className="h-screen w-screen flex flex-col items-center justify-center bg-white dark:bg-gray-900">
      <RotateCw className="w-10 h-10 animate-spin text-sigma-600 opacity-20" />
      <p className="mt-4 text-[10px] font-black uppercase tracking-[0.3em] text-gray-300 dark:text-gray-600">{t('editor.syncing')}</p>
    </div>
  )

  const chatPlaceholder =
    activeTab === 'explore' ? t('chat.explorePlaceholder') :
    activeTab === 'library' ? t('chat.libraryPlaceholder') :
    t('chat.askPlaceholder')

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-200 relative">
      <LibraryActionsContext.Provider value={{ ...libraryActions, updateLibraryActions }}>
        <EditorHeader
          onBack={() => navigate('/')}
          onCompile={() => handleCompile(false, false)}
          onShowLogs={() => setShowLogModal(true)}
          onSave={handleSave}
        />

        <ResizablePanels
          className="flex-1"
          initialSizes={['20%', '1']}
        >
          <aside className="h-full flex flex-col bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 overflow-hidden">
            {activeTab === 'synthesis' && (
              <div className="flex border-b border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-800/50 flex-shrink-0">
                <button onClick={() => { setLeftTab('files') }} className={`flex-1 py-3 text-xs font-bold uppercase tracking-widest transition-all ${leftTab === 'files' ? 'text-sigma-600 border-b-2 border-sigma-600 bg-white dark:bg-gray-900' : 'text-gray-400 dark:text-gray-500'}`}>{t('editor.sidebar.files')}</button>
                <button onClick={() => setLeftTab('git')} className={`flex-1 py-3 text-xs font-bold uppercase tracking-widest transition-all ${leftTab === 'git' ? 'text-sigma-600 border-b-2 border-sigma-600 bg-white dark:bg-gray-900' : 'text-gray-400 dark:text-gray-500'}`}>{t('editor.settings.history')}</button>
                <button onClick={() => { setLeftTab('chat') }} className={`flex-1 py-3 text-xs font-bold uppercase tracking-widest transition-all ${leftTab === 'chat' ? 'text-sigma-600 border-b-2 border-sigma-600 bg-white dark:bg-gray-900' : 'text-gray-400 dark:text-gray-500'}`}>{t('editor.sidebar.askSigma')}</button>
              </div>
            )}
            {activeTab === 'synthesis' && leftTab === 'files' && (
              <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
                <FileTree ref={fileTreeRef} onFileSelect={handleFileSelect} onSaveCurrentFile={handleSave} />
              </div>
            )}
            {activeTab === 'synthesis' && leftTab === 'git' && (
              <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
                <HistoryPanel />
              </div>
            )}
            <div className={`${activeTab !== 'synthesis' || leftTab === 'chat' ? 'flex' : 'hidden'} flex-1 min-h-0 flex-col overflow-hidden`}>
              <ChatPanel
                projectId={projectId}
                placeholder={chatPlaceholder}
                citation={pendingCitation}
                onClearCitation={clearCitation}
                onFileChanged={handleFileChanged}
                onAnnotationChanged={handleAnnotationChanged}
                getUserState={getUserState}
                onSaveBeforeChat={activeTab === 'synthesis' ? saveBeforeChat : null}
                onCitation={handleCitation}
              />
            </div>
          </aside>
          <div className="h-full overflow-hidden">
            {activeTab === 'synthesis' ? (
              <SynthesisTab
                projectId={projectId}
                editorRef={editorRef}
                previewRef={previewRef}
                handleSave={handleSave}
                handleFileSelect={handleFileSelect}
                handleExitNotebook={handleExitNotebook}
                onFileReady={handleFileReady}
                onSaveBeforeAnnotationChat={saveBeforeChat}
              />
            ) : activeTab === 'explore' ? (
              <BrowserVNC projectId={projectId} />
            ) : (
              <LibraryBrowser projectId={projectId} />
            )}
          </div>
        </ResizablePanels>
        <LogModal isOpen={showLogModal} onClose={() => setShowLogModal(false)} logs={compileLogs} diagnostics={compileDiagnostics} onAskSiGMA={compileFailed ? handleAskSiGMA : null} onJumpToError={handleJumpToError} />
        <AskUserQuestionModal />
        <PlanApprovalDialog />
        <PermissionDialog />
        {conflictState && (
          <FileConflictModal
            fileName={conflictState.fileName}
            diffLines={conflictState.diffLines}
            onForceSave={() => resolveConflict(true)}
            onCancel={() => resolveConflict(false)}
          />
        )}
        <TerminalPanel projectId={projectId} visible={showTerminal} />
      </LibraryActionsContext.Provider>
    </div>
  )
}
