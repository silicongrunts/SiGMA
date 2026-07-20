import { useState, useEffect, useRef, useCallback, useContext } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { ArrowLeft, Pencil, Play, RefreshCw, CheckCircle2, Settings, ChevronDown, FileCode, Cpu, Book, X, Loader2, AlertTriangle, Check, RotateCw, Redo2, Plus, Upload, FolderPlus, FileText, Loader, AlertCircle, Camera, Clock, ArrowUpDown, TerminalSquare, Lightbulb, Sun, Moon, Monitor, Trash2 } from 'lucide-react'
import { projectsAPI, filesAPI, notebooksAPI, libraryAPI, browserAPI } from '../api'
import { computeIsTexFile, getCompiledPreviewSource } from '../utils/constants'
import { toastError, toastSuccess } from './Toast'
import { ModalOverlay, ConfirmModal } from './Modal'
import { LibraryActionsContext } from './LibraryActionsContext'
import { Spinner } from './ui'
import Toggle from './Toggle'
import LanguageSelector from './LanguageSelector'
import { useTheme } from '../hooks/useTheme'

/**
 * Confirmation modal for rebuilding RAG index.
 * Uses existing ModalOverlay component.
 */
function RebuildConfirmModal({ isOpen, onClose, onConfirm, rebuilding }) {
  const { t } = useTranslation()
  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="flex flex-col">
        {/* Header */}
        <div className="flex items-center gap-4 px-6 pt-6 pb-4">
          <div className="w-12 h-12 rounded-2xl bg-amber-100 dark:bg-amber-900/30 flex items-center justify-center flex-shrink-0">
            <AlertTriangle className="w-6 h-6 text-amber-600 dark:text-amber-400" />
          </div>
          <div>
            <h2 className="text-xl font-black text-gray-900 dark:text-gray-100 tracking-tight">{t('editor.rebuild.title')}</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{t('editor.rebuild.subtitle')}</p>
          </div>
        </div>

        {/* Body */}
        <div className="px-6 py-2 space-y-3">
          <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/50 rounded-2xl p-4 space-y-2">
            <p className="text-sm text-amber-800 dark:text-amber-200 font-bold mb-2 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4" /> {t('editor.rebuild.whenToRebuild')}
            </p>
            <ul className="text-sm text-amber-700 dark:text-amber-300/90 list-disc list-inside space-y-1.5 pl-1">
              <li>{t('editor.rebuild.when1')}</li>
              <li>{t('editor.rebuild.when2')}</li>
              <li>{t('editor.rebuild.when3')}</li>
            </ul>
          </div>

          <div className="bg-blue-50 dark:bg-sigma-600/15 border border-blue-200 dark:border-sigma-600/40 rounded-2xl p-4 space-y-2">
            <p className="text-sm text-blue-800 dark:text-sigma-300 font-bold mb-2">{t('editor.rebuild.whatWillHappen')}</p>
            <ul className="text-sm text-blue-700 dark:text-sigma-300/90 list-disc list-inside space-y-1.5 pl-1">
              <li>{t('editor.rebuild.what1')}</li>
              <li>{t('editor.rebuild.what2')}</li>
              <li>{t('editor.rebuild.what3')}</li>
              <li>{t('editor.rebuild.what4')}</li>
            </ul>
          </div>

          <p className="text-xs text-gray-400 dark:text-gray-500 italic pt-1">
            {t('editor.rebuild.notSure')}
          </p>
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-6 py-5 border-t border-gray-100 dark:border-gray-800 mt-2">
          <button
            onClick={onClose}
            disabled={rebuilding}
            className="flex-1 py-3.5 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {t('editor.rebuild.cancel')}
          </button>
          <button
            onClick={onConfirm}
            disabled={rebuilding}
            className="flex-1 py-3.5 bg-amber-600 hover:bg-amber-700 text-white font-black rounded-2xl shadow-lg shadow-amber-200 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed disabled:shadow-none flex items-center justify-center gap-2"
          >
            {rebuilding ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                {t('editor.rebuild.rebuilding')}
              </>
            ) : (
              t('editor.rebuild.confirm')
            )}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}


function SnapshotDisableConfirmModal({ isOpen, onClose, onConfirm }) {
  const { t } = useTranslation()
  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="flex flex-col">
        <div className="flex items-center gap-4 px-6 pt-6 pb-4">
          <div className="w-12 h-12 rounded-2xl bg-red-100 dark:bg-red-900/30 flex items-center justify-center flex-shrink-0">
            <AlertTriangle className="w-6 h-6 text-red-600 dark:text-red-400" />
          </div>
          <div>
            <h2 className="text-xl font-black text-gray-900 dark:text-gray-100 tracking-tight">{t('editor.snapshot.title')}</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{t('editor.snapshot.subtitle')}</p>
          </div>
        </div>
        <div className="px-6 py-2">
          <p className="text-sm text-gray-600 dark:text-gray-400 leading-relaxed">
            {t('editor.snapshot.warning')}
          </p>
        </div>
        <div className="flex gap-3 px-6 py-5 border-t border-gray-100 dark:border-gray-800 mt-2">
          <button
            onClick={onClose}
            className="flex-1 py-3.5 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            {t('editor.snapshot.cancel')}
          </button>
          <button
            onClick={onConfirm}
            className="flex-1 py-3.5 bg-red-600 hover:bg-red-700 text-white font-black rounded-2xl shadow-lg shadow-red-200 transition-all active:scale-95"
          >
            {t('editor.snapshot.confirm')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}

const TIPS_MAX_LENGTH = 6000

function TipsEditModal({ isOpen, onClose, initialValue, onSave }) {
  const { t } = useTranslation()
  const [value, setValue] = useState(initialValue || "")
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (isOpen) setValue(initialValue || "")
  }, [isOpen, initialValue])

  const charCount = value.length
  const overLimit = charCount > TIPS_MAX_LENGTH
  const counterColor = overLimit
    ? "text-red-500"
    : charCount > TIPS_MAX_LENGTH * 0.9
      ? "text-amber-500"
      : "text-gray-400"

  const handleSave = async () => {
    if (overLimit || saving) return
    setSaving(true)
    try {
      await onSave(value)
    } catch {
      // onSave handles toast
    } finally {
      setSaving(false)
    }
  }

  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div
        className="flex flex-col"
        style={{ maxWidth: "32rem" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-4 px-6 pt-6 pb-4">
          <div className="w-12 h-12 rounded-2xl bg-blue-50 dark:bg-sigma-600/20 flex items-center justify-center flex-shrink-0">
            <Lightbulb className="w-6 h-6 text-blue-500 dark:text-sigma-400" />
          </div>
          <div>
            <h2 className="text-xl font-black text-gray-900 dark:text-gray-100 tracking-tight">{t('editor.tips.title')}</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{t('editor.tips.subtitle')}</p>
          </div>
        </div>

        {/* Body */}
        <div className="px-6 py-2 space-y-3">
          <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-100 dark:border-amber-800/50 rounded-xl px-4 py-3">
            <p className="text-xs text-amber-700 dark:text-amber-300/90 leading-relaxed">
              {t('editor.tips.description')}
            </p>
          </div>
          <textarea
            value={value}
            onChange={(e) => setValue(e.target.value)}
            maxLength={TIPS_MAX_LENGTH}
            placeholder={t('editor.tips.placeholder')}
            className="w-full h-64 px-4 py-3 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl text-sm text-gray-800 dark:text-gray-200 leading-relaxed outline-none focus:ring-2 focus:ring-sigma-600/20 resize-none font-mono"
          />
          <div className="flex justify-end">
            <span className={`text-xs font-mono ${counterColor}`}>
              {charCount.toLocaleString()} / {TIPS_MAX_LENGTH.toLocaleString()}
            </span>
          </div>
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-6 py-5 border-t border-gray-100 dark:border-gray-800 mt-2">
          <button
            onClick={onClose}
            className="flex-1 py-3.5 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            {t('editor.tips.cancel')}
          </button>
          <button
            onClick={handleSave}
            disabled={overLimit || saving}
            className="flex-1 py-3.5 bg-sigma-600 hover:bg-sigma-700 text-white font-black rounded-2xl shadow-lg shadow-sigma-200 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin mx-auto" /> : t('editor.tips.save')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}


export function EditorHeader({ onBack, onCompile, onShowLogs, onSave }) {
  const currentProject = useStore(state => state.currentProject)
  const setCurrentProject = useStore(state => state.setCurrentProject)
  const activeTab = useStore(state => state.activeTab)
  const setActiveTab = useStore(state => state.setActiveTab)
  const isRebuildingIndex = useStore(state => state.isRebuildingIndex)
  const setIsRebuildingIndex = useStore(state => state.setIsRebuildingIndex)
  const bumpBrowserDataCleared = useStore(state => state.bumpBrowserDataCleared)
  const compiling = useStore(state => state.compiling)
  const compileFailed = useStore(state => state.compileFailed)
  const hasUnsavedChanges = useStore(state => state.hasUnsavedChanges)
  const lastSavedTime = useStore(state => state.lastSavedTime)
  const lastSavedType = useStore(state => state.lastSavedType)
  const isNotebookMode = useStore(state => state.isNotebookMode)
  const isTexFile = useStore(state => state.isTexFile)

  const { isDark, toggleTheme } = useTheme()
  const { t } = useTranslation()

  const currentFile = useStore(state => state.currentFile)
  const mdSyncScroll = useStore(state => state.mdSyncScroll)
  const toggleMdSyncScroll = useStore(state => state.toggleMdSyncScroll)
  const showTerminal = useStore(state => state.showTerminal)
  const toggleTerminal = useStore(state => state.toggleTerminal)

  // Library state from context (provided by EditorView, updated by LibraryBrowser)
  const { onRefresh, onReprocessAll, reprocessingAll, statusSummary, hasFailed, onNewFolder, onUploadFiles } = useContext(LibraryActionsContext)

  const activeKernels = useStore(state => state.activeKernels)
  const setActiveKernels = useStore(state => state.setActiveKernels)
  const kernelsLoading = useStore(state => state.kernelsLoading)
  const setKernelsLoading = useStore(state => state.setKernelsLoading)

  const [showSettings, setShowSettings] = useState(false)
  const [showKernelModal, setShowKernelModal] = useState(false)
  const [showLibraryAddMenu, setShowLibraryAddMenu] = useState(false)
  const [libraryStatusPopup, setLibraryStatusPopup] = useState(null) // key of open status popup
  const libraryAddMenuRef = useRef(null)
  const libraryStatusRef = useRef(null)
  const [texFiles, setTexFiles] = useState([])

  // Rebuild index state
  const [showRebuildConfirm, setShowRebuildConfirm] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)

  const [showClearBrowserConfirm, setShowClearBrowserConfirm] = useState(false)

  // Snapshot settings
  const [snapshotInterval, setSnapshotInterval] = useState(5)
  const [showSnapshotDisableWarning, setShowSnapshotDisableWarning] = useState(false)
  const [tabSwitching, setTabSwitching] = useState(false)
  const [settingUpdating, setSettingUpdating] = useState(false)

  // Tips settings
  const [tips, setTips] = useState("")
  const [showTipsModal, setShowTipsModal] = useState(false)

  // Load config when project changes
  useEffect(() => {
    if (currentProject?.id) {
      projectsAPI.getConfig(currentProject.id).then(config => {
        // Use 0 as the "disabled" sentinel so a single dropdown can express both
        // the off state and the on+N-minutes state.
        setSnapshotInterval(config.snapshot_enabled === false ? 0 : (config.snapshot_interval_minutes || 5))
        setTips(config.tips || "")
      }).catch(e => console.warn('Failed to load project config:', e))
    }
  }, [currentProject?.id])

  // Editable project title
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [editTitle, setEditTitle] = useState("")
  const titleInputRef = useRef(null)

  useEffect(() => {
    if (isEditingTitle && titleInputRef.current) {
      titleInputRef.current.focus()
      titleInputRef.current.select()
    }
  }, [isEditingTitle])

  useEffect(() => {
    if (isEditingTitle && currentProject) {
      setEditTitle(currentProject.name)
    }
  }, [currentProject, isEditingTitle])

  const handleTitleSubmit = async () => {
    const trimmed = editTitle.trim()
    if (trimmed.length > 100) {
      toastError(t('editor.toast.nameTooLong'))
      return
    }
    if (trimmed && trimmed !== currentProject?.name) {
      try {
        const updated = await projectsAPI.update(currentProject.id, { name: trimmed })
        setCurrentProject(updated)
      } catch (e) {
        toastError(e.message || t('editor.toast.updateNameFailed'))
        return // Don't exit edit mode on failure
      }
    }
    setIsEditingTitle(false)
  }

  const handleTitleKeyDown = (e) => {
    if (e.key === 'Enter') {
      handleTitleSubmit()
    } else if (e.key === 'Escape') {
      setIsEditingTitle(false)
    }
  }

  const settingsRef = useRef(null)

  // Click outside to close settings
  useEffect(() => {
    const clickOutside = (e) => {
      if (showSettings && settingsRef.current && !settingsRef.current.contains(e.target)) {
        setShowSettings(false)
      }
    }
    document.addEventListener('mousedown', clickOutside)
    return () => document.removeEventListener('mousedown', clickOutside)
  }, [showSettings])

  // Click outside to close library add menu
  useEffect(() => {
    if (!showLibraryAddMenu) return
    const handler = (e) => {
      if (libraryAddMenuRef.current && !libraryAddMenuRef.current.contains(e.target)) {
        setShowLibraryAddMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showLibraryAddMenu])

  // Click outside to close library status popup
  useEffect(() => {
    if (!libraryStatusPopup) return
    const handler = (e) => {
      if (libraryStatusRef.current && !libraryStatusRef.current.contains(e.target)) {
        setLibraryStatusPopup(null)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [libraryStatusPopup])

  // Click outside to close kernel modal
  const kernelModalRef = useRef(null)
  useEffect(() => {
    if (!showKernelModal) return
    const handler = (e) => {
      if (kernelModalRef.current && !kernelModalRef.current.contains(e.target)) {
        setShowKernelModal(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showKernelModal])

  // Load TeX files when settings open
  useEffect(() => {
    if (showSettings && currentProject?.id) {
      filesAPI.tree(currentProject.id).then(data => {
        const list = []
        const scan = (nodes) => {
          nodes.forEach(n => {
            if (n.type === 'file' && n.name.endsWith('.tex')) list.push(n.path)
            if (n.children) scan(n.children)
          })
        }
        if (data.root?.children) scan(data.root.children)
        setTexFiles(list)
      }).catch(e => console.warn('Failed to load TeX file list:', e))
    }
  }, [showSettings, currentProject?.id])

  // Fetch kernels
  const fetchKernels = useCallback(async () => {
    setKernelsLoading(true)
    try {
      const data = await notebooksAPI.listKernels()
      setActiveKernels(data?.kernels || [])
    } catch (e) {
      console.error('Failed to fetch kernels:', e)
    } finally {
      setKernelsLoading(false)
    }
  }, [setKernelsLoading, setActiveKernels])

  useEffect(() => {
    if (showSettings || showKernelModal) {
      fetchKernels()
      const timer = setInterval(fetchKernels, 5000)
      return () => clearInterval(timer)
    }
  }, [showSettings, showKernelModal, fetchKernels])

  const handleKillKernel = async (kernelId) => {
    try {
      await notebooksAPI.killKernel(kernelId)
      setActiveKernels(prev => prev.filter(k => (k.id || k.name) !== kernelId))
    } catch (e) {
      toastError(e.message || t('editor.toast.killKernelFailed'))
    }
  }

  const handleUpdateSetting = async (key, val) => {
    setSettingUpdating(true)
    try {
      const updated = await projectsAPI.update(currentProject.id, { [key]: val })
      setCurrentProject(updated)
      // When main_file changes, recalculate isTexFile and recompile
      if (key === 'main_file') {
        const s = useStore.getState()
        const nextIsTexFile = computeIsTexFile(s.currentFile, val)
        s.setIsTexFile(nextIsTexFile)
        const ps = s.previewSource
        if (nextIsTexFile || ps.kind === 'pdf-compiled') {
          if (val) {
            const sourcePath = ps.kind === 'pdf-compiled' ? (ps.path || s.currentFile) : s.currentFile
            s.setPreviewSource(getCompiledPreviewSource(sourcePath, val, ps.compileVersion || 0))
          } else if (ps.kind === 'pdf-compiled') {
            s.setPreviewSource({ kind: 'none', path: null, compileVersion: 0 })
          }
        }
        onCompile?.()
      }
    } catch (e) {
      toastError(t('editor.toast.updateSettingFailed'))
    } finally {
      setSettingUpdating(false)
    }
  }

  // Snapshot interval change handler. Value 0 means "Disabled" — selecting it
  // opens the confirmation modal rather than applying immediately, since it
  // stops tracking file changes. All other values flip snapshot_enabled on
  // (re-enabling if previously disabled) and persist the new interval.
  const handleSnapshotIntervalChange = async (value) => {
    if (value === 0) {
      setShowSnapshotDisableWarning(true)
      return
    }
    const prev = snapshotInterval
    setSnapshotInterval(value)
    try {
      await projectsAPI.updateConfig(currentProject.id, {
        snapshot_enabled: true,
        snapshot_interval_minutes: value,
      })
    } catch (e) {
      setSnapshotInterval(prev)
      toastError(t('editor.toast.updateIntervalFailed'))
    }
  }

  const handleConfirmDisableSnapshot = async () => {
    setShowSnapshotDisableWarning(false)
    const prev = snapshotInterval
    setSnapshotInterval(0)
    try {
      await projectsAPI.updateConfig(currentProject.id, { snapshot_enabled: false })
      toastSuccess(t('editor.toast.snapshotDisabled'))
    } catch (e) {
      setSnapshotInterval(prev)
      toastError(t('editor.toast.snapshotFailed'))
    }
  }

  // Tips save handler
  const handleSaveTips = async (value) => {
    await projectsAPI.updateConfig(currentProject.id, { tips: value })
    setTips(value)
    setShowTipsModal(false)
  }

  // Rebuild index handler
  const handleRebuildConfirm = async () => {
    if (!currentProject?.id) return
    setRebuilding(true)
    setIsRebuildingIndex(true)
    try {
      const result = await libraryAPI.rebuildIndex(currentProject.id)
      toastSuccess(result.message || t('editor.toast.rebuildSuccess'))
    } catch (e) {
      toastError(e.message || t('editor.toast.rebuildFailed'))
    } finally {
      setRebuilding(false)
      setIsRebuildingIndex(false)
    }
    setShowRebuildConfirm(false)
  }

  const handleClearBrowserConfirm = async () => {
    if (!currentProject?.id) return
    try {
      await browserAPI.clearData(currentProject.id)
      // The backend restarted the whole browser stack (websockify
      // included); bump the counter so BrowserVNC remounts its iframe and
      // opens a fresh WebSocket instead of staying stuck on the dead one.
      bumpBrowserDataCleared()
      toastSuccess(t('browser.dataCleared'))
    } catch (e) {
      toastError(e.message || t('browser.clearFailed'))
      throw e
    }
  }

  // Show rebuild button only on Library tab (activeTab === 'library')
  const showRebuildButton = activeTab === 'library'

  const showClearBrowserButton = activeTab === 'explore'

  // Determine what to show in the "Run" section of the dropdown
  const renderDropdown = () => (
    <div className="absolute top-full right-0 mt-2 w-72 bg-white/95 dark:bg-gray-800/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-[0_20px_50px_rgba(0,0,0,0.15)] rounded-2xl p-5 animate-in fade-in zoom-in duration-200">
      <div className="space-y-5">

        {/* LaTeX Section \u2013 only if editing a .tex file */}
        {showLatexFeatures && (
          <div>
            <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 uppercase tracking-widest mb-3">
              <Book className="w-3.5 h-3.5" /> {t('editor.settings.latex')}
              {settingUpdating && (
                <span className="ml-auto flex items-center gap-1 text-[9px] font-medium normal-case tracking-normal text-gray-400">
                  <Spinner size="xs" />{t('editor.settings.updating')}
                </span>
              )}
            </label>
            <div className="space-y-3">
              <div>
                <label className="flex items-center gap-2 text-[9px] font-semibold text-gray-400 mb-1.5"><Cpu className="w-3 h-3" /> {t('editor.settings.compiler')}</label>
                <select
                  value={currentProject?.engine || 'pdflatex'}
                  onChange={(e) => handleUpdateSetting('engine', e.target.value)}
                  disabled={settingUpdating}
                  className="w-full px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl text-xs font-bold text-gray-700 dark:text-gray-300 outline-none focus:ring-2 focus:ring-sigma-600/20 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <option value="pdflatex">{t('editor.settings.compiler.pdflatex')}</option>
                  <option value="xelatex">{t('editor.settings.compiler.xelatex')}</option>
                  <option value="lualatex">{t('editor.settings.compiler.lualatex')}</option>
                  <option value="latex">{t('editor.settings.compiler.latex')}</option>
                </select>
              </div>
              <div>
                <label className="flex items-center gap-2 text-[9px] font-semibold text-gray-400 dark:text-gray-500 mb-1.5"><FileCode className="w-3 h-3" /> {t('editor.settings.mainTexFile')}</label>
                <select
                  value={currentProject?.main_file || 'main.tex'}
                  onChange={(e) => handleUpdateSetting('main_file', e.target.value)}
                  disabled={settingUpdating}
                  className="w-full px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl text-xs font-bold text-gray-700 dark:text-gray-300 outline-none focus:ring-2 focus:ring-sigma-600/20 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {texFiles.length > 0 ? texFiles.map(path => (
                    <option key={path} value={path}>{path}</option>
                  )) : <option disabled>{t('editor.settings.noTexFiles')}</option>}
                </select>
              </div>
            </div>
          </div>
        )}

        {/* Jupyter Kernels \u2013 always shown */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 uppercase tracking-widest"><Cpu className="w-3.5 h-3.5" /> {t('editor.settings.jupyterKernels')}</label>
          </div>
          <button
            type="button"
            onClick={() => {
              setShowKernelModal(true)
              setShowSettings(false)
            }}
            className="w-full bg-sigma-600 hover:bg-sigma-700 text-white px-4 py-2.5 rounded-xl flex items-center justify-center gap-2 text-xs font-bold transition-all"
          >
            <Cpu className="w-4 h-4" />
            {t('editor.settings.viewActiveKernels')}
            {activeKernels.length > 0 && (
              <span className="ml-1 bg-white/20 rounded-full px-2 py-0.5 text-[10px]">{activeKernels.length}</span>
            )}
          </button>
        </div>

        {/* Auto-Snapshot Section */}
        <div>
          <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 uppercase tracking-widest mb-3">
            <Camera className="w-3.5 h-3.5" /> {t('editor.settings.history')}
          </label>
          <div>
            <label className="flex items-center gap-2 text-[9px] font-semibold text-gray-400 mb-1.5">
              <Clock className="w-3 h-3" /> {t('editor.settings.autoSnapshot')}
            </label>
            <select
              value={snapshotInterval}
              onChange={(e) => handleSnapshotIntervalChange(parseInt(e.target.value))}
              className="w-full px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl text-xs font-bold text-gray-700 dark:text-gray-300 outline-none focus:ring-2 focus:ring-sigma-600/20"
            >
              <option value={0}>{t('editor.settings.snapshot.disabled')}</option>
              <option value={1}>{t('editor.settings.snapshot.minutes', { count: 1 })}</option>
              <option value={5}>{t('editor.settings.snapshot.minutes', { count: 5 })}</option>
              <option value={10}>{t('editor.settings.snapshot.minutes', { count: 10 })}</option>
              <option value={30}>{t('editor.settings.snapshot.minutes', { count: 30 })}</option>
              <option value={60}>{t('editor.settings.snapshot.hour')}</option>
            </select>
          </div>
        </div>

        {/* Tips Section */}
        <div>
          <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 uppercase tracking-widest mb-3">
            <Lightbulb className="w-3.5 h-3.5" /> {t('editor.settings.tips')}
          </label>
          <button
            type="button"
            onClick={() => setShowTipsModal(true)}
            className="w-full px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl text-xs font-bold text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors text-left flex items-center justify-between"
          >
            <span className="truncate">{tips ? `${tips.substring(0, 40)}${tips.length > 40 ? '...' : ''}` : t('editor.settings.noTipsSet')}</span>
            <Pencil className="w-3 h-3 text-gray-400 flex-shrink-0 ml-2" />
          </button>
        </div>

        {/* Rebuild Index button\u2013 only on Library tab */}
        {showRebuildButton && (
          <div>
            <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 uppercase tracking-widest mb-3"><AlertTriangle className="w-3.5 h-3.5" /> {t('editor.settings.index')}</label>
            <button
              type="button"
              onClick={() => {
                setShowRebuildConfirm(true)
                setShowSettings(false)
              }}
              disabled={isRebuildingIndex}
              className="w-full bg-amber-600 hover:bg-amber-700 text-white px-4 py-2.5 rounded-xl flex items-center justify-center gap-2 text-xs font-bold transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isRebuildingIndex ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('editor.settings.rebuilding')}
                </>
              ) : (
                <>
                  <RefreshCw className="w-4 h-4" />
                  {t('editor.settings.rebuildIndex')}
                </>
              )}
            </button>
          </div>
        )}

        {/* Clear Browser Data button — only on Explore tab */}
        {showClearBrowserButton && (
          <div>
            <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 uppercase tracking-widest mb-3"><Monitor className="w-3.5 h-3.5" /> {t('editor.settings.browser')}</label>
            <button
              type="button"
              onClick={() => {
                setShowClearBrowserConfirm(true)
                setShowSettings(false)
              }}
              className="w-full bg-red-600 hover:bg-red-700 text-white px-4 py-2.5 rounded-xl flex items-center justify-center gap-2 text-xs font-bold transition-all"
            >
              <Trash2 className="w-4 h-4" />
              {t('editor.settings.clearBrowserData')}
            </button>
          </div>
        )}

        {/* Appearance — dark mode toggle */}
        <div>
          <label className="flex items-center gap-2 text-[10px] font-black text-gray-400 dark:text-gray-500 uppercase tracking-widest mb-3">
            {isDark ? <Moon className="w-3.5 h-3.5" /> : <Sun className="w-3.5 h-3.5" />} {t('settings.darkMode')}
          </label>
          <div className="flex items-center justify-between px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl">
            <span className="text-xs font-bold text-gray-700 dark:text-gray-300">{t('editor.settings.darkMode')}</span>
            <Toggle checked={isDark} onChange={toggleTheme} label={t('editor.toggleDarkMode')} />
          </div>
          <div className="flex items-center justify-between px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl mt-2">
            <span className="text-xs font-bold text-gray-700 dark:text-gray-300">{t('settings.language')}</span>
            <LanguageSelector />
          </div>
        </div>

      </div>
    </div>
  )

  // Click outside to close kernel modal
  const renderKernelModal = () => {
    if (!showKernelModal) return null
    return (
      <div
        ref={kernelModalRef}
        className="absolute top-full right-0 mt-2 w-96 bg-white/95 dark:bg-gray-800/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-[0_20px_50px_rgba(0,0,0,0.15)] rounded-2xl p-5 z-[100] animate-in fade-in zoom-in duration-200"
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-bold text-gray-800 dark:text-gray-200 flex items-center gap-2">
            <Cpu className="w-4 h-4" />
            {t('editor.kernels.title')}
          </h3>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => fetchKernels()}
              className="p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md transition-colors"
            >
              <RefreshCw className={`w-4 h-4 ${kernelsLoading ? 'animate-spin' : ''}`} />
            </button>
            <button
              type="button"
              onClick={() => setShowKernelModal(false)}
              className="p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md transition-colors"
            >
              <X className="w-4 h-4 text-gray-400 dark:text-gray-500" />
            </button>
          </div>
        </div>
        <div className="space-y-2 max-h-80 overflow-y-auto">
          {activeKernels.length === 0 ? (
            <p className="text-xs text-gray-400 dark:text-gray-500 italic py-4 text-center">{t('editor.kernels.none')}</p>
          ) : (
            activeKernels.map(kernel => (
              <div key={kernel.id} className="flex items-center justify-between bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded-lg px-3 py-2.5">
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-semibold text-gray-700 dark:text-gray-300 truncate">
                    {kernel.display_name || kernel.project_name || kernel.name || t('editor.kernels.kernel')}
                  </div>
                  <div className="text-[9px] text-gray-400 dark:text-gray-500">
                    {t('editor.kernels.connections', { count: kernel.connections || 0 })} · {kernel.display_name ? kernel.name || t('editor.kernels.kernel') : t('editor.kernels.kernel')}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => handleKillKernel(kernel.id)}
                  className="ml-2 p-1.5 text-red-400 dark:text-red-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-md transition-colors"
                  title={t('editor.kernels.kill')}
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    )
  }

  // Show LaTeX features when project has a TeX main_file (not tied to current file)
  const showLatexFeatures = isTexFile && activeTab === 'synthesis'

  return (
    <>
    <header className="h-14 border-b border-gray-200 dark:border-gray-800 grid grid-cols-[1fr_auto_1fr] items-center px-4 bg-white dark:bg-gray-900 z-[100] flex-shrink-0">
      {/* === Left: Back + Project Info === */}
      <div className="flex items-center gap-4 min-w-0 justify-self-start">
        <button onClick={async () => {
          if (tabSwitching) return
          if (useStore.getState().hasUnsavedChanges && onSave) {
            setTabSwitching(true)
            await onSave(false, false)
            setTabSwitching(false)
          }
          onBack()
        }} className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg text-gray-500 dark:text-gray-400 transition-colors flex-shrink-0"><ArrowLeft className="w-5 h-5" /></button>
        <div className="flex flex-col group">
          {isEditingTitle ? (
            <div className="flex items-center gap-1.5">
              <input
                ref={titleInputRef}
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                onKeyDown={handleTitleKeyDown}
                className="font-bold text-gray-900 dark:text-gray-100 leading-tight border-b-2 border-sigma-600 outline-none bg-transparent text-sm w-44"
              />
              <button data-edit-btn onClick={handleTitleSubmit} className="p-0.5 text-green-600 hover:bg-green-50 rounded transition-colors" title={t('common.save')}>
                <Check className="w-3.5 h-3.5" />
              </button>
              <button data-edit-btn onClick={() => setIsEditingTitle(false)} className="p-0.5 text-red-500 hover:bg-red-50 rounded transition-colors" title={t('common.cancel')}>
                <X className="w-3.5 h-3.5" />
              </button>
              <span className="text-[10px] text-gray-400">
                {editTitle.length}/100
              </span>
            </div>
          ) : (
            <div className="flex items-center gap-1">
              <h2
                className="font-bold text-gray-900 dark:text-gray-100 leading-tight truncate max-w-[180px] cursor-pointer group-hover:text-sigma-600 group-hover:underline decoration-sigma-400 underline-offset-4"
                onClick={() => {
                  if (currentProject) {
                    setIsEditingTitle(true)
                  }
                }}
              >
                {currentProject?.name || t('editor.title.project')}
              </h2>
              <Pencil
                className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0 cursor-pointer hover:text-gray-600 dark:hover:text-gray-300"
                onClick={() => setIsEditingTitle(true)}
              />
            </div>
          )}
          <div className="flex items-center gap-1.5 mt-0.5">
            {!currentFile ? (
              <span className="text-[10px] font-medium text-gray-300 uppercase tracking-wider">{t('editor.title.noFileOpen')}</span>
            ) : hasUnsavedChanges ? (
              <><div className="w-1.5 h-1.5 rounded-full bg-orange-400 animate-pulse" /><span className="text-[10px] font-bold text-orange-500 uppercase tracking-wider">{t('editor.title.unsaved')}</span></>
            ) : (
              <><CheckCircle2 className="w-2.5 h-2.5 text-green-500" /><span className="text-[10px] font-medium text-gray-400 uppercase tracking-wider">{lastSavedTime ? `${lastSavedTime} ${lastSavedType === 'auto' ? t('editor.title.autosaved') : t('editor.title.saved')}` : t('editor.title.allSaved')}</span></>
            )}
            {isNotebookMode && (
              <><Book className="w-2.5 h-2.5 text-orange-500" /><span className="text-[10px] font-medium text-orange-400 uppercase tracking-wider">{t('editor.title.notebook')}</span></>
            )}
          </div>
        </div>
      </div>

      {/* === Center: Tab Navigation === */}
      <nav className="flex h-full justify-self-center">
        {[['explore', t('editor.tabs.explore')], ['library', t('editor.tabs.library')], ['synthesis', t('editor.tabs.synthesis')]].map(([id, label]) => (
          <button key={id} onClick={async () => {
            if (tabSwitching) return
            if (id === activeTab) return
            // Leaving Synthesis — save unsaved changes first
            if (activeTab === 'synthesis' && useStore.getState().hasUnsavedChanges && onSave) {
              setTabSwitching(true)
              const saved = await onSave(false, false)
              setTabSwitching(false)
              if (!saved) return  // conflict/cancelled — stay on current tab
            }
            setActiveTab(id)
          }} className={`px-4 h-full text-sm font-bold transition-all flex items-center gap-1.5 ${activeTab === id ? 'border-b-2 border-sigma-600 text-sigma-600' : 'text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300'}`}>
            {label}
            {tabSwitching && id !== activeTab && <Loader2 className="w-3 h-3 animate-spin text-gray-400 dark:text-gray-500" />}
          </button>
        ))}
      </nav>

      {/* === Right: Action buttons + Settings (always rightmost) === */}
      <div className="relative flex items-center gap-2 justify-self-end">

        {/* Library action buttons — only on Library tab */}
        {activeTab === 'library' && (
          <div className="flex items-center gap-2" ref={libraryStatusRef}>
            {/* Status indicators */}
            {statusSummary && (() => {
              const summary = statusSummary.summary || {}
              const totalDocs = Object.values(summary).reduce((a, b) => a + b, 0)
              if (totalDocs > 0) {
                const activeIndicators = [
                  { key: 'processing', label: t('editor.status.processing'), icon: <Loader className="w-3 h-3 animate-spin" />, textClass: 'text-purple-600 dark:text-purple-400', bgClass: 'bg-purple-50 dark:bg-purple-900/30', hoverClass: 'hover:bg-purple-100 dark:hover:bg-purple-900/50' },
                  { key: 'indexing', label: t('editor.status.indexing'), icon: <Loader className="w-3 h-3 animate-spin" />, textClass: 'text-purple-600 dark:text-purple-400', bgClass: 'bg-purple-50 dark:bg-purple-900/30', hoverClass: 'hover:bg-purple-100 dark:hover:bg-purple-900/50' },
                  { key: 'cancelling', label: t('library.status.cancelling'), icon: <Loader className="w-3 h-3 animate-spin" />, textClass: 'text-yellow-600 dark:text-yellow-400', bgClass: 'bg-yellow-50 dark:bg-yellow-900/30', hoverClass: 'hover:bg-yellow-100 dark:hover:bg-yellow-900/50' },
                  { key: 'pending', label: t('editor.status.pending'), icon: <Loader className="w-3 h-3" />, textClass: 'text-yellow-600 dark:text-yellow-400', bgClass: 'bg-yellow-50 dark:bg-yellow-900/30', hoverClass: 'hover:bg-yellow-100 dark:hover:bg-yellow-900/50' },
                  { key: 'failed', label: t('editor.status.failed'), icon: <AlertCircle className="w-3 h-3" />, textClass: 'text-red-600 dark:text-red-400', bgClass: 'bg-red-50 dark:bg-red-900/30', hoverClass: 'hover:bg-red-100 dark:hover:bg-red-900/50' },
                ].filter(c => (summary[c.key] || 0) > 0)
                const docs = statusSummary.documents || []

                if (activeIndicators.length === 0) {
                  // All complete
                  return (
                    <span className="px-2 py-0.5 rounded-lg text-[10px] font-bold flex items-center gap-1 text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/30">
                      <CheckCircle2 className="w-3 h-3" /><span>{summary.completed || 0}</span>
                    </span>
                  )
                }
                return activeIndicators.map(cfg => {
                  const count = summary[cfg.key] || 0
                  const isOpen = libraryStatusPopup === cfg.key
                  const statusDocs = docs.filter(d => {
                    const s = d.processing_status
                    return cfg.key === 'failed' ? (s === 'failed' || s === 'indexing_failed') : s === cfg.key
                  })
                  return (
                    <div key={cfg.key} className="relative">
                      <button onClick={() => setLibraryStatusPopup(isOpen ? null : cfg.key)}
                        className={`px-2 py-0.5 rounded-lg text-[10px] font-bold flex items-center gap-1 transition-colors cursor-pointer ${cfg.textClass} ${cfg.bgClass} ${cfg.hoverClass}`}>
                        {cfg.icon}<span>{count} {cfg.label}</span>
                      </button>
                      {isOpen && statusDocs.length > 0 && (
                        <div className="absolute right-0 top-full mt-1 bg-white/95 dark:bg-gray-800/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-2xl rounded-xl py-1.5 min-w-[200px] max-w-[280px] max-h-[240px] overflow-y-auto z-50 animate-in fade-in zoom-in duration-150">
                          {statusDocs.map(doc => (
                            <div key={doc.id} className="flex items-center gap-2 px-3 py-2 text-sm text-gray-700 dark:text-gray-300">
                              <FileText className="w-3.5 h-3.5 flex-shrink-0 text-gray-400 dark:text-gray-500" />
                              <span className="truncate">{doc.title}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })
              }
              return null
            })()}
            {/* Refresh button */}
            <button onClick={() => onRefresh?.()} title={t('editor.actions.refreshLibrary')} className="p-2 text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors">
              <RotateCw className="w-4 h-4" />
            </button>
            {/* Reprocess all button */}
            {hasFailed && (
              <button onClick={() => onReprocessAll?.()} disabled={reprocessingAll}
                title={reprocessingAll ? t('editor.actions.reprocessing') : t('editor.actions.reprocessFailed')}
                className={`p-2 rounded-lg transition-colors ${reprocessingAll ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : 'text-orange-500 dark:text-orange-400 hover:bg-orange-50 dark:hover:bg-orange-900/20'}`}>
                {reprocessingAll ? <Spinner size="sm" /> : <Redo2 className="w-4 h-4" />}
              </button>
            )}
            {/* "+" add button */}
            <div className="relative" ref={libraryAddMenuRef}>
              <button onClick={() => setShowLibraryAddMenu(!showLibraryAddMenu)} title={t('editor.actions.add')} className="p-2 text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded-lg transition-colors">
                <Plus className="w-4 h-4" />
              </button>
              {showLibraryAddMenu && (
                <div className="absolute right-0 top-full mt-1 bg-white/95 dark:bg-gray-800/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-2xl rounded-xl py-1.5 min-w-[180px] z-50 animate-in fade-in zoom-in duration-150">
                  <button onClick={() => { setShowLibraryAddMenu(false); onNewFolder?.() }}
                    className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-gray-700 dark:text-gray-300 hover:bg-blue-50 dark:hover:bg-sigma-600/20 hover:text-blue-600 dark:hover:text-sigma-400 transition-colors">
                    <FolderPlus className="w-4 h-4" /><span>{t('editor.actions.newFolder')}</span>
                  </button>
                  <button onClick={() => { setShowLibraryAddMenu(false); onUploadFiles?.() }}
                    className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-gray-700 dark:text-gray-300 hover:bg-blue-50 dark:hover:bg-sigma-600/20 hover:text-blue-600 dark:hover:text-sigma-400 transition-colors">
                    <Upload className="w-4 h-4" /><span>{t('editor.actions.uploadFiles')}</span>
                  </button>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Quick action buttons — only when editing a .tex file */}
        {showLatexFeatures && (
          <>
            <button type="button" onClick={onShowLogs} className={`text-xs font-bold px-3 py-1.5 border rounded-lg transition-colors uppercase tracking-widest ${compileFailed ? 'logs-alert' : 'text-gray-400 dark:text-gray-500 border-gray-200 dark:border-gray-700 hover:text-gray-900 dark:hover:text-gray-200'}`}>{t('editor.actions.logs')}</button>
            <button
              type="button"
              onClick={() => { if (!compiling) onCompile?.(); }}
              disabled={compiling}
              className="bg-sigma-600 hover:bg-sigma-700 disabled:bg-gray-300 text-white px-4 py-1.5 rounded-lg flex items-center shadow-lg shadow-blue-100 dark:shadow-none text-sm font-bold transition-all active:scale-95 disabled:cursor-not-allowed"
            >
              {compiling ? <RefreshCw className="w-4 h-4 mr-2 animate-spin" /> : <Play className="w-4 h-4 mr-2" />}
              {compiling ? t('editor.actions.compiling') : t('editor.actions.compile')}
            </button>
          </>
        )}

        {/* Markdown sync scroll toggle — only when editing .md file in Synthesis */}
        {activeTab === 'synthesis' && currentFile?.endsWith('.md') && (
          <button
            type="button"
            onClick={toggleMdSyncScroll}
            title={mdSyncScroll ? t('editor.actions.syncScrollOn') : t('editor.actions.syncScrollOff')}
            className={`p-2 rounded-lg transition-all ${mdSyncScroll ? 'bg-blue-50 dark:bg-sigma-600/20 text-blue-600 dark:text-sigma-400' : 'text-gray-300 dark:text-gray-600 hover:text-gray-500 dark:hover:text-gray-300'}`}
          >
            <ArrowUpDown className="w-4 h-4" strokeWidth={mdSyncScroll ? 2.5 : 1.5} />
          </button>
        )}

        {/* Terminal toggle (always shown, left of Settings) */}
        <button
          type="button"
          onClick={toggleTerminal}
          className={`p-2 rounded-lg transition-all ${showTerminal ? 'bg-sigma-50 dark:bg-sigma-600/20 text-sigma-600' : 'text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'}`}
          title={t('editor.actions.toggleTerminal')}
        >
          <TerminalSquare className="w-4.5 h-4.5" />
        </button>

        {/* Settings (always shown, always rightmost) */}
        <div className="relative flex items-center gap-2" ref={settingsRef}>
          <button
            type="button"
            onClick={() => setShowSettings(!showSettings)}
            className={`p-2 rounded-lg transition-all flex items-center gap-1 ${showSettings ? 'bg-sigma-50 dark:bg-sigma-600/20 text-sigma-600' : 'text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'}`}
          >
            <Settings className={`w-4.5 h-4.5 transition-transform duration-500 ${showSettings ? 'rotate-90' : ''}`} />
            <ChevronDown className="w-3 h-3" />
          </button>

          {showSettings && renderDropdown()}
        </div>

        {/* Kernel modal — rendered as a sibling, with its own ref for click-outside */}
        {renderKernelModal()}
      </div>
    </header>

    {/* Rebuild Confirmation Modal */}
    <RebuildConfirmModal
      isOpen={showRebuildConfirm}
      onClose={() => setShowRebuildConfirm(false)}
      onConfirm={handleRebuildConfirm}
      rebuilding={rebuilding}
    />

    {/* Clear Browser Data confirmation */}
    <ConfirmModal
      isOpen={showClearBrowserConfirm}
      onClose={() => setShowClearBrowserConfirm(false)}
      onConfirm={handleClearBrowserConfirm}
      title={t('browser.clearDataTitle')}
      message={t('browser.clearDataConfirm')}
      danger
    />

    {/* Snapshot Disable Warning Modal */}
    <SnapshotDisableConfirmModal
      isOpen={showSnapshotDisableWarning}
      onClose={() => setShowSnapshotDisableWarning(false)}
      onConfirm={handleConfirmDisableSnapshot}
    />

    {/* Tips Edit Modal */}
    <TipsEditModal
      isOpen={showTipsModal}
      onClose={() => setShowTipsModal(false)}
      initialValue={tips}
      onSave={handleSaveTips}
    />
    </>
  )
}

export default EditorHeader
