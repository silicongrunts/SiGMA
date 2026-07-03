/**
 * LibraryBrowser - Resource library with CRUD + RAG search
 * Layout: left list + right detail/edit panel
 * Features: sort, folders, multi-select, context menu, drag-and-drop
 */
import { useState, useEffect, useCallback, useRef, useLayoutEffect, useContext } from 'react'
import { useTranslation } from 'react-i18next'
import { useClickOutside } from '../hooks/useClickOutside'
import { Search, Plus, FileText, Trash2, Edit3, X, Upload, BookOpen, Tag, AlertCircle, File, CheckCircle, Loader, ScrollText, RotateCw, Redo2, Download, Pencil, Check, Sparkles, Folder, ChevronRight, FolderPlus, Move, ChevronDown, ArrowLeft } from 'lucide-react'
import { libraryAPI } from '../api'
import { toastError, toastSuccess } from './Toast'
import { MarkdownContent } from './ChatShared'
import { InputModal, ConfirmModal } from './Modal'
import { InlineEditableField } from './InlineEditableField'
import { FileDropzone, collectDropEntries } from './FileDropzone'
import { SelectedFilesList } from './SelectedFilesList'
import { LibraryActionsContext } from './LibraryActionsContext'
import { LoadingOverlay, Spinner, LoadingButton } from './ui'
import ContextMenu from './ContextMenu'
import { storage } from '../utils/storage'

const MOVE_FOLDER_PAGE_SIZE = 500
const EMBEDDING_MODEL_CHANGED_TEXT = 'Embedding model changed'

function getLibrarySearchErrorMessage(error, t) {
  const message = error?.message || ''
  if (message.includes(EMBEDDING_MODEL_CHANGED_TEXT)) {
    return t('library.toast.embeddingModelChanged')
  }
  return message || t('library.toast.searchFailed')
}

/** Highlight query keyword in text with <mark> tag */
function highlightQuery(text, query) {
  if (!query || !text) return text
  try {
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const parts = text.split(new RegExp(`(${escaped})`, 'gi'))
    return parts.map((part, i) =>
      part.toLowerCase() === query.toLowerCase()
        ? <mark key={i} className="bg-yellow-200 dark:bg-yellow-700 text-yellow-900 dark:text-yellow-200 rounded px-0.5">{part}</mark>
        : part
    )
  } catch {
    return text
  }
}

/* =========================================================================
 * ".." go-up row with drop support (move to parent folder)
 * ========================================================================= */
function GoUpRow({ onClick, parentFolderId, projectId, onMoved, onUploadToFolder }) {
  const { t } = useTranslation()
  const [isDragOver, setIsDragOver] = useState(false)
  return (
    <div
      onClick={onClick}
      onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setIsDragOver(true) }}
      onDragLeave={() => setIsDragOver(false)}
      onContextMenu={(e) => { e.preventDefault(); e.stopPropagation() }}
      onDrop={async (e) => {
        e.preventDefault(); e.stopPropagation(); setIsDragOver(false)
        // OS file drop → upload to parent folder
        if (e.dataTransfer.files?.length > 0) {
          const entries = await collectDropEntries(e.dataTransfer)
          onUploadToFolder?.(entries, parentFolderId)
          return
        }
        // Internal library item move
        try {
          const data = JSON.parse(e.dataTransfer.getData('text/plain'))
          await libraryAPI.moveItems(projectId, { ids: [data.id], target_folder_id: parentFolderId })
          onMoved?.()
        } catch (err) { toastError(err.message || t('library.toast.moveFailed')) }
      }}
      className={`px-4 py-3 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400
        ${isDragOver ? 'bg-blue-50 dark:bg-blue-900/30 ring-2 ring-blue-400 ring-inset rounded-lg' : ''}`}
    >
      <Folder className="w-4 h-4 text-gray-400 dark:text-gray-500" />
      <span>..</span>
    </div>
  )
}

/* =========================================================================
 * Move-to-folder Modal — hierarchical folder browser
 * ========================================================================= */
function MoveToFolderModal({ isOpen, onClose, projectId, onConfirm }) {
  const { t } = useTranslation()
  const [folderId, setFolderId] = useState(null)
  const [bc, setBc] = useState([{ id: null, name: t('library.root') }])
  const [folders, setFolders] = useState([])
  const [loading, setLoading] = useState(false)
  const [moving, setMoving] = useState(false)

  const loadFolders = useCallback(async (parentId) => {
    if (!projectId) return
    setLoading(true)
    try {
      const allDocs = []
      let offset = 0
      let total = Infinity
      while (offset < total) {
        const params = {
          sort: 'title',
          order: 'asc',
          limit: MOVE_FOLDER_PAGE_SIZE,
          offset,
        }
        if (parentId) params.parent_id = parentId
        const data = await libraryAPI.list(projectId, params)
        const docs = data.documents || []
        allDocs.push(...docs)
        total = Number(data.total ?? allDocs.length)
        if (docs.length === 0) break
        offset += docs.length
      }
      setFolders(allDocs.filter(d => d.is_folder))
    } catch { setFolders([]) }
    finally { setLoading(false) }
  }, [projectId])

  useEffect(() => {
    if (isOpen) { setFolderId(null); setBc([{ id: null, name: t('library.root') }]); loadFolders(null) }
  }, [isOpen, loadFolders])

  if (!isOpen) return null

  const navigate = (id, name) => { setFolderId(id); setBc(prev => [...prev, { id, name }]); loadFolders(id) }
  const goBack = () => { const n = bc.slice(0, -1); setBc(n); setFolderId(n[n.length - 1].id); loadFolders(n[n.length - 1].id) }
  const goBc = (i) => { const n = bc.slice(0, i + 1); setBc(n); setFolderId(n[n.length - 1].id); loadFolders(n[n.length - 1].id) }

  const handleConfirm = async () => {
    if (moving) return
    setMoving(true)
    try {
      // onConfirm (confirmMove) closes the modal on success and toasts on error.
      // The finally reset is a no-op on success (modal unmounts) but restores the
      // button on failure so the user can retry.
      await onConfirm(folderId)
    } finally {
      setMoving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 animate-in fade-in duration-300" onClick={onClose}>
      <div className="bg-white dark:bg-gray-900 rounded-2xl shadow-2xl dark:shadow-none w-[480px] max-h-[70vh] flex flex-col animate-in zoom-in duration-300" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <h2 className="text-base font-bold text-gray-800 dark:text-gray-200">{t('library.moveTo')}</h2>
          <button onClick={onClose} className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300"><X className="w-5 h-5" /></button>
        </div>

        {/* Breadcrumb navigation */}
        <div className="px-6 py-2 border-b border-gray-50 dark:border-gray-800 flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 overflow-x-auto flex-shrink-0">
          {bc.map((b, i) => (
            <span key={i} className="flex items-center gap-1 flex-shrink-0">
              {i > 0 && <ChevronRight className="w-3 h-3 text-gray-300 dark:text-gray-600" />}
              <button onClick={() => goBc(i)} className={`hover:text-sigma-600 transition-colors ${i === bc.length - 1 ? 'text-gray-800 dark:text-gray-200 font-medium' : ''}`}>
                {b.name}
              </button>
            </span>
          ))}
        </div>

        {/* Folder list */}
        <div className="flex-1 overflow-y-auto min-h-[200px]">
          {loading ? (
            <div className="flex items-center justify-center py-10 text-gray-400 dark:text-gray-500 text-sm">{t('library.loading')}</div>
          ) : (
            <div>
              {/* Back button when inside a subfolder */}
              {bc.length > 1 && (
                <button onClick={goBack}
                  className="w-full px-6 py-3 flex items-center gap-3 text-sm text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors border-b border-gray-50 dark:border-gray-800">
                  <ArrowLeft className="w-4 h-4" />{t('library.back')}
                </button>
              )}
              {folders.length === 0 && bc.length === 1 && (
                <div className="flex flex-col items-center justify-center py-10 text-gray-400 dark:text-gray-500">
                  <Folder className="w-8 h-8 mb-2 opacity-30" />
                  <p className="text-sm">{t('library.noFoldersYet')}</p>
                </div>
              )}
              {folders.length === 0 && bc.length > 1 && (
                <div className="flex items-center justify-center py-10 text-gray-400 dark:text-gray-500 text-sm">{t('library.noSubfolders')}</div>
              )}
              {folders.map(folder => (
                <button key={folder.id}
                  onClick={() => navigate(folder.id, folder.title)}
                  className="w-full px-6 py-3 flex items-center gap-3 text-sm text-gray-700 dark:text-gray-300 hover:bg-blue-50 dark:hover:bg-blue-900/30 hover:text-blue-600 dark:hover:text-blue-400 transition-colors border-b border-gray-50 dark:border-gray-800">
                  <Folder className="w-4 h-4 text-blue-500 flex-shrink-0" />
                  <span className="flex-1 text-left truncate">{folder.title}</span>
                  <ChevronRight className="w-4 h-4 text-gray-300 dark:text-gray-600" />
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Footer with Move Here button */}
        <div className="px-6 py-4 border-t border-gray-100 dark:border-gray-800 flex justify-between items-center">
          <span className="text-xs text-gray-400 dark:text-gray-500">
            {folderId ? t('library.targetName', { name: bc[bc.length - 1].name }) : t('library.targetRoot')}
          </span>
          <LoadingButton
            loading={moving}
            loadingLabel={t('library.moving')}
            onClick={handleConfirm}
            className="px-4 py-2 text-sm bg-sigma-600 text-white rounded-xl hover:bg-sigma-700 transition-colors">
            <Move className="w-4 h-4" />{t('library.moveHere')}
          </LoadingButton>
        </div>
      </div>
    </div>
  )
}

export default function LibraryBrowser({ projectId }) {
  const { t } = useTranslation()
  const rootBreadcrumb = useCallback(() => [{ id: null, name: t('library.root') }], [t])
  const getStoredLibraryState = useCallback(() => storage.getLibrary(projectId), [projectId])
  const [documents, setDocuments] = useState([])
  const [loading, setLoading] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchMode, setSearchMode] = useState(() => getStoredLibraryState().searchMode)
  const [selectedDocId, setSelectedDocId] = useState(() => getStoredLibraryState().selectedDocId)
  const [selectedDoc, setSelectedDoc] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [isSearchResult, setIsSearchResult] = useState(false)

  // Upload state
  const [showUploadModal, setShowUploadModal] = useState(false)
  const [uploadLoading, setUploadLoading] = useState(false)
  const [reprocessingIds, setReprocessingIds] = useState(() => new Set())
  const [reprocessingAll, setReprocessingAll] = useState(false)

  // Processing log state
  const [showLogModal, setShowLogModal] = useState(false)
  const [currentLog, setCurrentLog] = useState(null)
  const [logContent, setLogContent] = useState('')

  // Sort state
  const [sortBy, setSortBy] = useState(() => getStoredLibraryState().sortBy)  // 'title' | 'updated_at'
  const [sortOrder, setSortOrder] = useState(() => getStoredLibraryState().sortOrder)    // 'asc' | 'desc'

  // Keyword edit modal
  const [keywordEdit, setKeywordEdit] = useState(null) // { docId, keywords: [] } or null

  // Folder navigation
  const [currentFolderId, setCurrentFolderId] = useState(() => getStoredLibraryState().folderId)
  const [breadcrumbs, setBreadcrumbs] = useState(() => {
    const stored = getStoredLibraryState().breadcrumbs
    return stored.length > 0 ? stored : rootBreadcrumb()
  })

  // Multi-select
  const [selectedIds, setSelectedIds] = useState(new Set())

  // Context menu
  const [contextMenu, setContextMenu] = useState(null)

  // Folder creation modal
  const [showFolderModal, setShowFolderModal] = useState(false)

  // Move-to-folder modal
  const [showMoveModal, setShowMoveModal] = useState(false)

  // Delete confirmation modal
  const [deleteConfirm, setDeleteConfirm] = useState({ open: false, ids: [], message: '' })
  const [moveTargetIds, setMoveTargetIds] = useState([]) // ids being moved

  // Search mode dropdown state
  const [showSearchModeMenu, setShowSearchModeMenu] = useState(false)
  const searchModeRef = useRef(null)

  // Sort dropdown state
  const [showSortMenu, setShowSortMenu] = useState(false)
  const sortMenuRef = useRef(null)

  // Pagination state
  const [page, setPage] = useState(0)
  const [totalCount, setTotalCount] = useState(0)
  const [editingPage, setEditingPage] = useState(false)
  const [pageInput, setPageInput] = useState('')
  const pageInputRef = useRef(null)
  const PAGE_SIZE = 50

  // Refs

  // Close search mode menu on outside click
  useClickOutside(searchModeRef, () => setShowSearchModeMenu(false), showSearchModeMenu)

  // Close sort menu on outside click
  useClickOutside(sortMenuRef, () => setShowSortMenu(false), showSortMenu)

  useEffect(() => {
    const stored = storage.getLibrary(projectId)
    setSearchMode(stored.searchMode)
    setSortBy(stored.sortBy)
    setSortOrder(stored.sortOrder)
    setCurrentFolderId(stored.folderId)
    setBreadcrumbs(stored.breadcrumbs.length > 0 ? stored.breadcrumbs : rootBreadcrumb())
    setSelectedDocId(stored.selectedDocId)
    setSelectedDoc(null)
  }, [projectId, rootBreadcrumb])

  const persistLibraryState = useCallback((patch) => {
    storage.setLibrary(projectId, patch)
  }, [projectId])

  const resetToRoot = useCallback(() => {
    const root = rootBreadcrumb()
    setCurrentFolderId(null)
    setBreadcrumbs(root)
    setSelectedDocId(null)
    setSelectedDoc(null)
    setSelectedIds(new Set())
    persistLibraryState({ folderId: null, breadcrumbs: root, selectedDocId: null })
  }, [persistLibraryState, rootBreadcrumb])

  const changeSearchMode = useCallback((mode) => {
    setSearchMode(mode)
    persistLibraryState({ searchMode: mode })
  }, [persistLibraryState])

  const changeSort = useCallback((nextSortBy, nextSortOrder) => {
    setSortBy(nextSortBy)
    setSortOrder(nextSortOrder)
    persistLibraryState({ sortBy: nextSortBy, sortOrder: nextSortOrder })
  }, [persistLibraryState])

  const selectDocument = useCallback((docId) => {
    setSelectedDocId(docId)
    persistLibraryState({ selectedDocId: docId || null })
  }, [persistLibraryState])

  // Open move modal
  const openMoveModal = (ids) => {
    setMoveTargetIds(ids)
    setShowMoveModal(true)
  }

  // Confirm move
  const confirmMove = async (targetFolderId) => {
    try {
      await libraryAPI.moveItems(projectId, { ids: moveTargetIds, target_folder_id: targetFolderId })
      setShowMoveModal(false)
      setSelectedIds(new Set())
      loadDocuments()
    } catch (e) { toastError(e.message || t('library.toast.moveFailed')) }
  }

  const loadDocuments = useCallback(async () => {
    if (!projectId) return
    setIsSearchResult(false)
    setLoading(true)
    try {
      const params = { sort: sortBy, order: sortOrder }
      if (currentFolderId) params.parent_id = currentFolderId
      // Only apply pagination for non-search results
      const p = page * PAGE_SIZE
      params.limit = PAGE_SIZE
      params.offset = p
      const data = await libraryAPI.list(projectId, params)
      setDocuments(data.documents || [])
      setTotalCount(data.total || 0)
      if (data.status_summary) setStatusSummary(data.status_summary)
    } catch (e) {
      console.error('Failed to load documents:', e)
      if (currentFolderId) resetToRoot()
    } finally {
      setLoading(false)
    }
  }, [projectId, sortBy, sortOrder, currentFolderId, page, resetToRoot])

  useEffect(() => { loadDocuments() }, [loadDocuments])

  // Clear selection when folder changes or documents reload
  useEffect(() => { setSelectedIds(new Set()); setPage(0) }, [currentFolderId, sortBy, sortOrder])

  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE))

  useEffect(() => {
    if (!isSearchResult && totalCount > 0 && page >= totalPages) {
      setPage(totalPages - 1)
    }
  }, [isSearchResult, page, totalCount, totalPages])

  // Auto-exit search mode when search box is cleared
  useEffect(() => {
    if (searchQuery.trim() === '' && isSearchResult) {
      const timer = setTimeout(() => {
        setIsSearchResult(false)
        loadDocuments()
      }, 300)
      return () => clearTimeout(timer)
    }
  }, [searchQuery, isSearchResult])

  // Fetch full document detail when selection changes
  useEffect(() => {
    if (!selectedDocId || !projectId) {
      setSelectedDoc(null)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    libraryAPI.get(projectId, selectedDocId, { include_content: false })
      .then(doc => { if (!cancelled) setSelectedDoc(doc) })
      .catch(() => {
        if (!cancelled) {
          selectDocument(null)
          setSelectedDoc(null)
        }
      })
      .finally(() => { if (!cancelled) setDetailLoading(false) })
    return () => { cancelled = true }
  }, [selectedDocId, projectId, selectDocument])

  // Status summary for global indicators (all docs in project)
  const [statusSummary, setStatusSummary] = useState({ summary: {}, documents: [] })
  const hasFailed = (statusSummary.summary.failed || 0) > 0

  // Silent polling
  useEffect(() => {
    if (!projectId) return
    const timer = setInterval(async () => {
      try {
        if (isSearchResult) {
          if (selectedDocId) {
            const doc = await libraryAPI.get(projectId, selectedDocId, { include_content: false })
            setSelectedDoc(prev => {
              if (!prev || doc.processing_status !== prev.processing_status || doc.updated_at !== prev.updated_at) return doc
              return prev
            })
          }
          return
        }
        const pollParams = {
          sort: sortBy,
          order: sortOrder,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
          include_status_summary: false,
        }
        if (currentFolderId) pollParams.parent_id = currentFolderId
        const data = await libraryAPI.list(projectId, pollParams)
        const incoming = data.documents || []
        libraryAPI.statusSummary(projectId)
          .then(summary => setStatusSummary(summary))
          .catch(e => console.debug('Library status poll failed:', e.message))
        setDocuments(prev => {
          const changed = incoming.length !== prev.length ||
            incoming.some(d => {
              const old = prev.find(p => p.id === d.id)
              return !old || old.processing_status !== d.processing_status || old.updated_at !== d.updated_at
            })
          if (!changed) return prev
          if (selectedDocId) {
            const updated = incoming.find(d => d.id === selectedDocId)
            const oldSel = prev.find(d => d.id === selectedDocId)
            if (updated && oldSel && (updated.processing_status !== oldSel.processing_status || updated.updated_at !== oldSel.updated_at)) {
              libraryAPI.get(projectId, selectedDocId, { include_content: false })
                .then(doc => setSelectedDoc(doc))
                .catch(() => {})
            }
          }
          return incoming
        })
      } catch (e) {
        console.debug('Library poll failed:', e.message)
        if (currentFolderId) resetToRoot()
      }
    }, 5000)
    return () => clearInterval(timer)
  }, [projectId, selectedDocId, isSearchResult, sortBy, sortOrder, currentFolderId, page, resetToRoot])

  // Search handlers
  const handleSearch = async () => {
    if (!searchQuery.trim() || !projectId) return
    setLoading(true)
    try {
      const api = searchMode === 'semantic' ? libraryAPI.ragSearch : libraryAPI.search
      const options = { limit: 100 }
      if (currentFolderId) options.parent_id = currentFolderId
      const data = await api(projectId, searchQuery, options)
      setIsSearchResult(true)
      setDocuments(data.documents || [])
      setSelectedIds(new Set())
    } catch (e) {
      toastError(getLibrarySearchErrorMessage(e, t))
    } finally {
      setLoading(false)
    }
  }

  const handleKeywordSearch = async (keyword) => {
    if (!keyword || !projectId) return
    setSearchQuery(keyword)
    setLoading(true)
    try {
      const options = { limit: 100 }
      if (currentFolderId) options.parent_id = currentFolderId
      const data = await libraryAPI.search(projectId, keyword, options)
      setIsSearchResult(true)
      setDocuments(data.documents || [])
    } catch (e) {
      toastError(getLibrarySearchErrorMessage(e, t))
    } finally {
      setLoading(false)
    }
  }

  const handleDelete = async (docId) => {
    setDeleteConfirm({ open: true, ids: [docId], message: t('library.deleteOneConfirm') })
  }

  // Batch delete
  const handleBatchDelete = () => {
    if (selectedIds.size === 0) return
    setDeleteConfirm({ open: true, ids: [...selectedIds], message: t('library.deleteMultiConfirm', { count: selectedIds.size }) })
  }

  const executeDelete = async (ids) => {
    try {
      if (ids.length === 1) {
        await libraryAPI.delete(projectId, ids[0])
      } else {
        await libraryAPI.batchDelete(projectId, ids)
      }
      if (ids.includes(selectedDocId)) { selectDocument(null); setSelectedDoc(null) }
      setSelectedIds(prev => { const n = new Set(prev); ids.forEach(id => n.delete(id)); return n })
      loadDocuments()
    } catch (e) {
      toastError(t('library.toast.deleteFailed'))
    }
  }

  // Batch move
  // Upload handler — folderId: undefined=use current, null=root, otherwise=specific folder
  const handleUpload = async (selectedFiles, folderId = undefined) => {
    if (!projectId || selectedFiles.length === 0) return
    setUploadLoading(true)
    try {
      const targetFolder = folderId !== undefined ? folderId : currentFolderId
      const data = await libraryAPI.uploadFiles(projectId, selectedFiles, targetFolder)
      const errors = data.errors || []
      const errorSummary = errors.slice(0, 3).map(e => `${e.file}: ${e.reason}`).join('\n')

      if (data.count === 0 && errors.length > 0) {
        toastError(t('library.uploadAllFailed', { errors: errorSummary }))
      } else if (data.count > 0) {
        let msg = t('library.uploadSuccess', { count: data.count })
        if (errors.length > 0) {
          msg += t('library.uploadSkipped', { count: errors.length, errors: errorSummary })
        }
        toastSuccess(msg)
      }
      setShowUploadModal(false)
      loadDocuments()
    } catch (e) {
      toastError(t('library.uploadFailed', { message: e.message || t('chat.toast.unknownError') }))
    } finally {
      setUploadLoading(false)
    }
  }

  // Reprocess single document
  const handleReprocess = async (docId) => {
    if (reprocessingIds.has(docId) || reprocessingAll) return
    setReprocessingIds(prev => { const n = new Set(prev); n.add(docId); return n })
    try {
      await libraryAPI.reprocess(projectId, docId)
      toastSuccess(t('library.toast.reprocessStarted'))
      loadDocuments()
    } catch (e) {
      toastError(t('library.toast.reprocessFailed', { message: e.message || t('chat.toast.unknownError') }))
    } finally {
      setReprocessingIds(prev => { const n = new Set(prev); n.delete(docId); return n })
    }
  }

  // Reprocess all failed documents
  const handleReprocessAll = async () => {
    if (reprocessingAll) return
    setReprocessingAll(true)
    try {
      await libraryAPI.reprocessAll(projectId)
      toastSuccess(t('library.toast.reprocessAll'))
      loadDocuments()
    } catch (e) {
      toastError(t('library.toast.reprocessFailed', { message: e.message || t('chat.toast.unknownError') }))
    } finally {
      setReprocessingAll(false)
    }
  }

  // View processing log
  const handleViewLog = async (doc) => {
    try {
      const data = await libraryAPI.getProcessingLog(projectId, doc.id)
      setCurrentLog(doc)
      setLogContent(data.processing_log || '')
      setShowLogModal(true)
    } catch (e) {
      toastError(t('library.toast.logFailed'))
    }
  }

  // Inline field save
  const handleFieldSave = async (docId, field, value) => {
    try {
      const updated = await libraryAPI.update(projectId, docId, { [field]: value })
      setSelectedDoc(updated)
      loadDocuments()
    } catch (e) {
      throw new Error(e.message || t('library.toast.updateFailed'))
    }
  }

  // Create folder — throws on failure so InputModal can show error
  const handleCreateFolder = async (name) => {
    if (!name || !name.trim()) return
    try {
      const params = { name: name.trim() }
      if (currentFolderId) params.parent_id = currentFolderId
      await libraryAPI.createFolder(projectId, params)
      loadDocuments()
    } catch (e) {
      throw new Error(e.message || t('library.toast.folderCreateFailed'))
    }
  }

  // Navigate into folder
  const navigateToFolder = (folderId, folderName) => {
    const nextBreadcrumbs = [...breadcrumbs, { id: folderId, name: folderName }]
    setCurrentFolderId(folderId)
    setBreadcrumbs(nextBreadcrumbs)
    setSelectedDocId(null)
    setSelectedDoc(null)
    setSelectedIds(new Set())
    persistLibraryState({ folderId, breadcrumbs: nextBreadcrumbs, selectedDocId: null })
  }

  // Navigate to breadcrumb
  const navigateToBreadcrumb = (index) => {
    const newBreadcrumbs = breadcrumbs.slice(0, index + 1)
    setBreadcrumbs(newBreadcrumbs)
    setCurrentFolderId(newBreadcrumbs[newBreadcrumbs.length - 1].id)
    setSelectedDocId(null)
    setSelectedDoc(null)
    setSelectedIds(new Set())
    persistLibraryState({
      folderId: newBreadcrumbs[newBreadcrumbs.length - 1].id,
      breadcrumbs: newBreadcrumbs,
      selectedDocId: null,
    })
  }

  // Sort helpers
  const sortOptions = [
    { value: 'updated_at-desc', label: t('library.sortModifiedNew') },
    { value: 'updated_at-asc', label: t('library.sortModifiedOld') },
    { value: 'title-asc', label: t('library.sortNameAz') },
    { value: 'title-desc', label: t('library.sortNameZa') },
  ]
  const currentSortValue = `${sortBy}-${sortOrder}`

  // Multi-select toggle
  const toggleSelect = (id) => {
    setSelectedIds(prev => {
      const n = new Set(prev)
      if (n.has(id)) n.delete(id); else n.add(id)
      return n
    })
  }

  const toggleSelectAll = () => {
    const selectableIds = documents.filter(d => !d.is_folder || true).map(d => d.id)
    if (selectedIds.size === selectableIds.length && selectableIds.length > 0) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(selectableIds))
    }
  }

  const isAllSelected = documents.length > 0 && selectedIds.size === documents.length
  const isSomeSelected = selectedIds.size > 0 && !isAllSelected

  // Context menu handler
  const handleContextMenu = (e, item) => {
    e.preventDefault()
    e.stopPropagation()

    // If right-clicking an unselected item, clear selection and select only this one
    if (!selectedIds.has(item.id)) {
      setSelectedIds(new Set([item.id]))
    }

    const isSelectedMultiple = selectedIds.has(item.id) && selectedIds.size > 1
    const effectiveItem = isSelectedMultiple ? null : item // null means multi-select context menu

    setContextMenu({
      x: e.clientX,
      y: e.clientY,
      item: effectiveItem,
      isMulti: isSelectedMultiple,
      isEmptyArea: false,
    })
  }

  // Drag-and-drop handlers
  const handleItemDragStart = (e, item) => {
    e.dataTransfer.setData('text/plain', JSON.stringify({ id: item.id, isFolder: item.is_folder }))
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleFolderDrop = async (e, targetFolderId) => {
    e.preventDefault()
    e.stopPropagation()

    // OS file drop → upload into this folder
    if (e.dataTransfer.files?.length > 0) {
      const entries = await collectDropEntries(e.dataTransfer)
      handleUpload(entries, targetFolderId)
      return
    }

    // Internal library item move
    try {
      const data = JSON.parse(e.dataTransfer.getData('text/plain'))
      if (data.id === targetFolderId) return // Can't drop onto self
      await libraryAPI.moveItems(projectId, { ids: [data.id], target_folder_id: targetFolderId })
      loadDocuments()
    } catch (err) {
      toastError(err.message || t('library.toast.moveFailed'))
    }
  }

  // Container-level drag state for OS file drops onto empty area
  const [isContainerDragOver, setIsContainerDragOver] = useState(false)

  const submitPageInput = () => {
    const n = parseInt(pageInput, 10)
    if (n >= 1 && n <= totalPages) setPage(n - 1)
    setEditingPage(false)
  }

  const renderPagination = () => {
    if (isSearchResult || totalCount <= PAGE_SIZE) return null
    const currentPage = page + 1
    return (
      <div className="flex items-center gap-0.5 bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 px-0.5 py-0">
        <button
          onClick={() => setPage(0)}
          disabled={page === 0}
          className="px-1.5 py-0.5 text-[11px] font-bold text-gray-400 dark:text-gray-500 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
        >
          &lt;&lt;
        </button>
        <button
          onClick={() => setPage(p => Math.max(0, p - 1))}
          disabled={page === 0}
          className="px-1.5 py-0.5 text-[11px] font-bold text-gray-400 dark:text-gray-500 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
        >
          &lt;
        </button>
        <span className="px-1 text-[11px] font-medium text-gray-400 dark:text-gray-500">
          {t('library.pagePrefix')}
        </span>
        {editingPage ? (
          <input
            ref={pageInputRef}
            type="text"
            value={pageInput}
            onChange={e => setPageInput(e.target.value.replace(/\D/g, ''))}
            onBlur={submitPageInput}
            onKeyDown={e => {
              if (e.key === 'Enter') submitPageInput()
              else if (e.key === 'Escape') setEditingPage(false)
            }}
            className="w-8 text-center text-[11px] font-bold font-mono text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded outline-none focus:border-sigma-500 focus:ring-1 focus:ring-sigma-500/30"
            autoFocus
          />
        ) : (
          <button
            onClick={() => { setPageInput(String(currentPage)); setEditingPage(true) }}
            className="min-w-[20px] text-center text-[11px] leading-none py-0.5 font-bold font-mono text-gray-600 dark:text-gray-400 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 px-0.5 rounded transition-colors"
            title={t('preview.jumpToPage')}
          >
            {currentPage}
          </button>
        )}
        <span className="px-1 text-[11px] font-medium text-gray-400 dark:text-gray-500">
          {t('library.pageTotal', { total: totalPages })}
        </span>
        <button
          onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
          disabled={page >= totalPages - 1}
          className="px-1.5 py-0.5 text-[11px] font-bold text-gray-400 dark:text-gray-500 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
        >
          &gt;
        </button>
        <button
          onClick={() => setPage(totalPages - 1)}
          disabled={page >= totalPages - 1}
          className="px-1.5 py-0.5 text-[11px] font-bold text-gray-400 dark:text-gray-500 hover:text-sigma-600 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
        >
          &gt;&gt;
        </button>
      </div>
    )
  }

  const handleListEmptyAreaContextMenu = (e) => {
    const target = e.target
    const isEditable = target.closest('input, textarea, [contenteditable="true"], [contenteditable=""]')
    if (isEditable) return
    e.preventDefault()
    e.stopPropagation()
    setContextMenu({ x: e.clientX, y: e.clientY, item: null, isMulti: false, isEmptyArea: true })
  }

  // Status helpers
  const getStatusBadge = (status) => {
    if (status === 'completed') return (
      <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/30 px-1.5 py-0.5 rounded">
        <CheckCircle className="w-2.5 h-2.5" />{t('library.status.completed')}
      </span>
    )
    if (status === 'processing') return (
      <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-900/30 px-1.5 py-0.5 rounded">
        <Loader className="w-2.5 h-2.5 animate-spin" />{t('library.status.processing')}
      </span>
    )
    if (status === 'indexing') return (
      <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-900/30 px-1.5 py-0.5 rounded">
        <Loader className="w-2.5 h-2.5 animate-spin" />{t('library.status.indexing')}
      </span>
    )
    if (status === 'cancelling') return (
      <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-yellow-600 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/30 px-1.5 py-0.5 rounded">
        <Loader className="w-2.5 h-2.5 animate-spin" />{t('library.status.cancelling')}
      </span>
    )
    if (status === 'pending') return (
      <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-yellow-600 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/30 px-1.5 py-0.5 rounded">
        <Loader className="w-2.5 h-2.5" />{t('library.status.pending')}
      </span>
    )
    if (status === 'failed' || status === 'indexing_failed') return (
      <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/30 px-1.5 py-0.5 rounded">
        <AlertCircle className="w-2.5 h-2.5" />{t('library.status.failed')}
      </span>
    )
    return null
  }

  // Right panel width
  const [panelWidth, setPanelWidth] = useState(500)
  const dragRef = useRef(false)
  const dragStartX = useRef(0)
  const dragStartW = useRef(0)

  const handleDragStart = useCallback((e) => {
    e.preventDefault()
    dragRef.current = true
    dragStartX.current = e.clientX
    dragStartW.current = panelWidth
  }, [panelWidth])

  useEffect(() => {
    const onMove = (e) => {
      if (!dragRef.current) return
      const delta = dragStartX.current - e.clientX
      setPanelWidth(Math.max(300, Math.min(window.innerWidth * 0.7, dragStartW.current + delta)))
    }
    const onUp = () => { dragRef.current = false }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
  }, [])

  // Build context menu options
  const getContextMenuOptions = () => {
    if (!contextMenu) return []
    const { item, isMulti, isEmptyArea } = contextMenu

    if (isEmptyArea) {
      return [
        { label: t('common.newFolder'), icon: <FolderPlus className="w-4 h-4" />, action: () => setShowFolderModal(true) },
        { label: t('common.uploadFiles'), icon: <Upload className="w-4 h-4" />, action: () => setShowUploadModal(true) },
      ]
    }

    if (isMulti) {
      return [
        { label: t('library.moveTo'), icon: <Move className="w-4 h-4" />, action: () => openMoveModal([...selectedIds]) },
        { separator: true },
        { label: t('library.deleteCount', { count: selectedIds.size }), icon: <Trash2 className="w-4 h-4" />, danger: true, action: handleBatchDelete },
      ]
    }

    if (item.is_folder) {
      return [
        { label: t('library.open'), icon: <Folder className="w-4 h-4" />, action: () => navigateToFolder(item.id, item.title) },
        { label: t('common.rename'), icon: <Edit3 className="w-4 h-4" />, action: () => startRename(item.id, item.title, true) },
        { separator: true },
        { label: t('library.deleteFolder'), icon: <Trash2 className="w-4 h-4" />, danger: true, action: () => handleDelete(item.id) },
      ]
    }

    const opts = [
      { label: t('library.viewDetails'), icon: <FileText className="w-4 h-4" />, action: () => selectDocument(item.id) },
      { label: t('common.rename'), icon: <Edit3 className="w-4 h-4" />, action: () => startRename(item.id, item.title, false) },
      { separator: true },
      { label: t('library.moveTo'), icon: <Move className="w-4 h-4" />, action: () => openMoveModal([item.id]) },
    ]
    if (item.processing_status === 'failed') {
      opts.push({ label: t('common.reprocess'), icon: <Redo2 className="w-4 h-4" />, action: () => handleReprocess(item.id) })
    }
    opts.push({ separator: true })
    opts.push({ label: t('common.delete'), icon: <Trash2 className="w-4 h-4" />, danger: true, action: () => handleDelete(item.id) })
    return opts
  }

  // Fix 7: Rename state and functions
  const [renamingItemId, setRenamingItemId] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renameIsFolder, setRenameIsFolder] = useState(false)
  const renameInputRef = useRef(null)

  const startRename = (id, title, isFolder) => {
    setRenamingItemId(id)
    setRenameValue(title)
    setRenameIsFolder(isFolder)
    setContextMenu(null)
  }

  const handleRenameConfirm = async () => {
    if (!renameValue.trim()) return
    try {
      await libraryAPI.update(projectId, renamingItemId, { title: renameValue.trim() })
      setRenamingItemId(null)
      setRenameValue('')
      loadDocuments()
    } catch (e) {
      toastError(e.message || t('library.toast.renameFailed'))
    }
  }

  const handleRenameCancel = () => {
    setRenamingItemId(null)
    setRenameValue('')
  }

  // Expose library actions to EditorHeader via React Context
  // Use refs to avoid stale closures while keeping the dependency array stable
  const handleReprocessAllRef = useRef(handleReprocessAll)
  handleReprocessAllRef.current = handleReprocessAll

  const { updateLibraryActions } = useContext(LibraryActionsContext)

  useEffect(() => {
    const folderPath = breadcrumbs.map(b => b.name).join(' > ')
    const summary = statusSummary?.summary || {}
    const indexingStatus = {
      pending: summary.pending || 0,
      processing: summary.processing || 0,
      indexing: summary.indexing || 0,
      cancelling: summary.cancelling || 0,
      completed: summary.completed || 0,
      failed: summary.failed || 0,
    }
    updateLibraryActions({
      onRefresh: loadDocuments,
      onReprocessAll: () => handleReprocessAllRef.current(),
      reprocessingAll,
      statusSummary,
      hasFailed,
      onNewFolder: () => setShowFolderModal(true),
      onUploadFiles: () => setShowUploadModal(true),
      selectedDocId,
      selectedDocTitle: selectedDoc?.title || null,
      currentFolderPath: folderPath || null,
      indexingStatus,
    })
  }, [loadDocuments, statusSummary, hasFailed, updateLibraryActions,
      selectedDocId, selectedDoc, breadcrumbs, reprocessingAll])

  return (
    <div className="h-full flex bg-white dark:bg-gray-900" onClick={() => { setContextMenu(null) }}>
      {/* ===== Left: Document List ===== */}
      <div className={`flex flex-col ${selectedDoc ? '' : 'flex-1'}`} style={selectedDoc ? { width: `calc(100% - ${panelWidth}px - 4px)` } : undefined}>
        {/* Search bar + action row */}
        <div className="px-3 py-2 border-b border-gray-100 dark:border-gray-800 space-y-2 flex-shrink-0">
          <div className="flex items-center gap-2 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-1.5 focus-within:ring-2 focus-within:ring-sigma-600/20 focus-within:bg-white dark:focus:bg-gray-900 transition-all">
            {/* Search mode dropdown - custom style */}
            <div className="relative" ref={searchModeRef}>
              <button onClick={(e) => { e.stopPropagation(); setShowSearchModeMenu(!showSearchModeMenu) }}
                className="text-[11px] font-bold text-sigma-600 hover:text-sigma-700 transition-colors flex items-center gap-1 border-r border-gray-200 dark:border-gray-700 pr-2 mr-1">
                {searchMode === 'keyword' ? t('library.keyword') : t('library.semantic')}<ChevronDown className="w-3 h-3" />
              </button>
              {showSearchModeMenu && (
                <div className="absolute left-0 top-full mt-1 bg-white/95 dark:bg-gray-900/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-2xl dark:shadow-none rounded-xl py-1.5 min-w-[140px] z-50 animate-in fade-in zoom-in duration-150">
                  <button onClick={(e) => { e.stopPropagation(); changeSearchMode('keyword'); setShowSearchModeMenu(false) }}
                    className={`w-full flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${searchMode === 'keyword' ? 'text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20' : 'text-gray-700 dark:text-gray-300 hover:bg-blue-50 dark:hover:bg-blue-900/30 hover:text-blue-600 dark:hover:text-blue-400'}`}>
                    {t('library.keyword')}
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); changeSearchMode('semantic'); setShowSearchModeMenu(false) }}
                    className={`w-full flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${searchMode === 'semantic' ? 'text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20' : 'text-gray-700 dark:text-gray-300 hover:bg-blue-50 dark:hover:bg-blue-900/30 hover:text-blue-600 dark:hover:text-blue-400'}`}>
                    {t('library.semantic')}
                  </button>
                </div>
              )}
            </div>
            <input
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              placeholder={searchMode === "semantic" ? t('library.searchSemantic') : t('library.searchKeyword')}
              className="flex-1 bg-transparent text-sm outline-none"
            />
            {loading && <Spinner size="xs" className="text-sigma-600" />}
            <button onClick={handleSearch} className="text-xs font-bold text-sigma-600 hover:text-sigma-700">{t('library.search')}</button>
            {isSearchResult && (
              <button onClick={() => { setSearchQuery(''); loadDocuments(); }} className="p-0.5 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors">
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
          {/* Action row: sort dropdown + multi-select actions */}
          <div className="flex items-center gap-2">
            {!isSearchResult && (
              <div className="relative" ref={sortMenuRef}>
                <button onClick={(e) => { e.stopPropagation(); setShowSortMenu(!showSortMenu) }}
                  className="px-2 py-0.5 text-[10px] font-bold rounded-md transition-colors bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600 flex items-center gap-1">
                  {sortOptions.find(o => o.value === currentSortValue)?.label || t('library.sort')}<ChevronDown className="w-3 h-3" />
                </button>
                {showSortMenu && (
                  <div className="absolute left-0 top-full mt-1 bg-white/95 dark:bg-gray-900/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-2xl dark:shadow-none rounded-xl py-1.5 min-w-[180px] z-50 animate-in fade-in zoom-in duration-150">
                    {sortOptions.map(opt => (
                      <button key={opt.value} onClick={(e) => {
                        e.stopPropagation()
                        const [s, o] = opt.value.split('-')
                        changeSort(s, o)
                        setShowSortMenu(false)
                      }}
                        className={`w-full flex items-center gap-3 px-4 py-2 text-sm transition-colors ${currentSortValue === opt.value ? 'text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20 font-medium' : 'text-gray-700 dark:text-gray-300 hover:bg-blue-50 dark:hover:bg-blue-900/30 hover:text-blue-600 dark:hover:text-blue-400'}`}>
                        {opt.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
            {renderPagination()}
            {/* Multi-select actions inline */}
            {selectedIds.size > 0 && !isSearchResult && (
              <>
                <span className="text-[10px] font-bold text-blue-600 dark:text-blue-400">{t('library.selectedCount', { count: selectedIds.size })}</span>
                <button onClick={() => openMoveModal([...selectedIds])} title={t('library.moveToFolder')}
                  className="px-2 py-0.5 text-[10px] font-bold rounded-md bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 border border-blue-200 dark:border-blue-800/50 hover:bg-blue-100 dark:hover:bg-blue-800/40 transition-colors flex items-center gap-1">
                  <Move className="w-3 h-3" />{t('common.move')}
                </button>
                <button onClick={handleBatchDelete}
                  className="px-2 py-0.5 text-[10px] font-bold rounded-md bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 border border-red-200 dark:border-red-800/50 hover:bg-red-100 dark:hover:bg-red-800/40 transition-colors flex items-center gap-1">
                  <Trash2 className="w-3 h-3" />{t('common.delete')}
                </button>
                <button onClick={() => setSelectedIds(new Set())}
                  className="text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300">{t('common.clear')}</button>
              </>
            )}
          </div>
        </div>

        {/* Breadcrumbs */}
        {!isSearchResult && breadcrumbs.length > 1 && (
          <div className="px-3 py-1.5 border-b border-gray-100 dark:border-gray-800 flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 flex-shrink-0 overflow-x-auto">
            {breadcrumbs.map((bc, i) => (
              <span key={i} className="flex items-center gap-1 flex-shrink-0">
                {i > 0 && <ChevronRight className="w-3 h-3 text-gray-300 dark:text-gray-600" />}
                <button onClick={() => navigateToBreadcrumb(i)}
                  className={`hover:text-sigma-600 transition-colors ${i === breadcrumbs.length - 1 ? 'text-gray-800 dark:text-gray-200 font-medium' : ''}`}>
                  {bc.name}
                </button>
              </span>
            ))}
          </div>
        )}

        {/* Document list */}
        <div
          className={`flex-1 overflow-y-auto transition-colors relative
            ${isContainerDragOver ? 'bg-sigma-50/30 dark:bg-sigma-600/20' : ''}`}
          onDragOver={(e) => {
            e.preventDefault()
            if (e.dataTransfer.types?.includes('Files')) setIsContainerDragOver(true)
          }}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget)) setIsContainerDragOver(false)
          }}
          onDrop={async (e) => {
            e.preventDefault()
            setIsContainerDragOver(false)
            // OS file drop on empty area → upload to current folder
            if (e.dataTransfer.files?.length > 0) {
              const entries = await collectDropEntries(e.dataTransfer)
              handleUpload(entries)
              return
            }
          }}
          onContextMenu={handleListEmptyAreaContextMenu}
        >
          {/* Upload overlay — covers the document list while files are uploading */}
          {uploadLoading && <LoadingOverlay label={t('library.uploading')} />}
          {/* ".." go-up row when inside a folder — always show, even when empty */}
          {!isSearchResult && currentFolderId && (
            <GoUpRow
              onClick={() => navigateToBreadcrumb(breadcrumbs.length - 2)}
              parentFolderId={breadcrumbs.length >= 2 ? breadcrumbs[breadcrumbs.length - 2].id : null}
              projectId={projectId}
              onMoved={loadDocuments}
              onUploadToFolder={handleUpload}
            />
          )}
          {loading ? (
            <div className="flex items-center justify-center py-10 text-gray-400 dark:text-gray-500 text-sm">{t('library.loading')}</div>
          ) : documents.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-gray-400 dark:text-gray-500">
              <FileText className="w-8 h-8 mb-2 opacity-30" />
              <p className="text-sm">{currentFolderId ? t('library.folderEmpty') : t('library.noDocuments')}</p>
              <p className="text-xs mt-1">{isContainerDragOver ? t('library.dropFiles') : t('library.clickToAdd')}</p>
            </div>
          ) : (
            <div className="divide-y divide-gray-50 dark:divide-gray-800">
              {/* Select-all header */}
              {!isSearchResult && (
                <div
                  className="px-4 py-1.5 bg-gray-50/80 dark:bg-gray-900/80 border-b border-gray-100 dark:border-gray-800 flex items-center gap-3 flex-shrink-0"
                  onContextMenu={(e) => { e.preventDefault(); e.stopPropagation() }}
                >
                  <input type="checkbox"
                    ref={el => { if (el) el.indeterminate = isSomeSelected }}
                    checked={isAllSelected}
                    onChange={toggleSelectAll}
                    className="w-3.5 h-3.5 rounded border-gray-300 dark:border-gray-600 text-sigma-600 focus:ring-sigma-500 cursor-pointer"
                  />
                  <span className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider flex-1">
                    {t('library.itemCount', { count: documents.length })}
                  </span>
                </div>
              )}
              {documents.map((doc, idx) => (
                <LibraryItem
                  key={`${doc.id}-${idx}`}
                  doc={doc}
                  idx={idx}
                  projectId={projectId}
                  selectedDocId={selectedDocId}
                  isSelected={selectedIds.has(doc.id)}
                  isSearchResult={isSearchResult}
                  searchMode={searchMode}
                  searchQuery={searchQuery}
                  onSelectDoc={selectDocument}
                  onToggleSelect={toggleSelect}
                  onContextMenu={handleContextMenu}
                  onDragStart={handleItemDragStart}
                  onDropOnFolder={handleFolderDrop}
                  onNavigateFolder={navigateToFolder}
                  onReprocess={handleReprocess}
                  reprocessingIds={reprocessingIds}
                  onViewLog={handleViewLog}
                  onDelete={handleDelete}
                  getStatusBadge={getStatusBadge}
                  onKeywordSearch={handleKeywordSearch}
                  renamingItemId={renamingItemId}
                  renameValue={renameValue}
                  onRenameChange={(e) => setRenameValue(e.target.value)}
                  onRenameConfirm={handleRenameConfirm}
                  onRenameCancel={handleRenameCancel}
                  renameInputRef={renameInputRef}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ===== Draggable divider + Right Panel ===== */}
      {selectedDoc && (
        <>
          <div
            onMouseDown={handleDragStart}
            className="w-1 flex-shrink-0 cursor-col-resize hover:bg-sigma-300 active:bg-sigma-400 transition-colors bg-gray-100 dark:bg-gray-800"
          />
          <DetailPanel
            key={selectedDoc.id}
            doc={selectedDoc}
            projectId={projectId}
            onClose={() => selectDocument(null)}
            getStatusBadge={getStatusBadge}
            onFieldSave={handleFieldSave}
            onReprocess={handleReprocess}
            isReprocessing={reprocessingIds.has(selectedDoc.id) || reprocessingAll}
            onViewLog={handleViewLog}
            onDelete={handleDelete}
            onRefresh={async () => {
              await loadDocuments();
              if (selectedDocId) {
                try { setSelectedDoc(await libraryAPI.get(projectId, selectedDocId, { include_content: false })); } catch {}
              }
            }}
            onKeywordSearch={handleKeywordSearch}
            width={panelWidth}
          />
        </>
      )}

      {/* Upload Modal */}
      {showUploadModal && (
        <UploadModal
          onClose={() => setShowUploadModal(false)}
          onUpload={handleUpload}
          uploadLoading={uploadLoading}
        />
      )}

      {/* Processing Log Modal */}
      {showLogModal && (
        <LogModal
          doc={currentLog}
          logContent={logContent}
          onClose={() => { setShowLogModal(false); setCurrentLog(null) }}
        />
      )}

      {/* New Folder Modal */}
      <InputModal
        isOpen={showFolderModal}
        onClose={() => setShowFolderModal(false)}
        onConfirm={handleCreateFolder}
        title={t('common.newFolder')}
        placeholder={t('library.folderNamePlaceholder')}
        icon={FolderPlus}
      />

      {/* Move-to-folder Modal */}
      <MoveToFolderModal
        isOpen={showMoveModal}
        onClose={() => setShowMoveModal(false)}
        projectId={projectId}
        onConfirm={confirmMove}
      />

      {/* Delete Confirmation Modal */}
      <ConfirmModal
        isOpen={deleteConfirm.open}
        onClose={() => setDeleteConfirm({ open: false, ids: [], message: '' })}
        onConfirm={() => executeDelete(deleteConfirm.ids)}
        title={t('library.deleteTitle')}
        message={deleteConfirm.message}
        danger={true}
      />

      {/* Edit Keywords Modal */}
      <InputModal
        isOpen={!!keywordEdit}
        onClose={() => setKeywordEdit(null)}
        onConfirm={async (value) => {
          if (!keywordEdit) return
          const kws = value.split(',').map(t => t.trim()).filter(Boolean)
          await handleFieldSave(keywordEdit.docId, 'keywords', kws)
          setKeywordEdit(null)
        }}
        title={t('library.editKeywords')}
        placeholder={t('library.keywordsPlaceholder')}
        initialValue={keywordEdit?.keywords?.join(', ') || ''}
      />

      {/* Context Menu */}
      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          options={getContextMenuOptions()}
          onClose={() => setContextMenu(null)}
        />
      )}
    </div>
  )
}

/* =========================================================================
 * Library Item Row (document or folder)
 * ========================================================================= */
function LibraryItem({ doc, idx, selectedDocId, isSelected, isSearchResult, searchMode, searchQuery,
  onSelectDoc, onToggleSelect, onContextMenu, onDragStart, onDropOnFolder, onNavigateFolder,
  onReprocess, reprocessingIds, onViewLog, onDelete, getStatusBadge, projectId, onKeywordSearch,
  renamingItemId, renameValue, onRenameChange, onRenameConfirm, onRenameCancel, renameInputRef }) {
  const { t } = useTranslation()
  const [isDragOver, setIsDragOver] = useState(false)

  const handleClick = (e) => {
    // If checkbox area or if item is a folder, don't change doc selection
    if (doc.is_folder) {
      onNavigateFolder(doc.id, doc.title)
      return
    }
    onSelectDoc(doc.id === selectedDocId ? null : doc.id)
  }

  return (
    <div
      draggable
      onDragStart={(e) => onDragStart(e, doc)}
      onDragOver={doc.is_folder ? (e) => { e.preventDefault(); e.stopPropagation(); setIsDragOver(true) } : undefined}
      onDragLeave={doc.is_folder ? () => setIsDragOver(false) : undefined}
      onDrop={doc.is_folder ? (e) => { setIsDragOver(false); onDropOnFolder(e, doc.id) } : undefined}
      onClick={handleClick}
      onContextMenu={(e) => onContextMenu(e, doc)}
      className={`px-4 py-3 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 transition-all select-none
        ${selectedDocId === doc.id ? 'bg-sigma-50 dark:bg-sigma-600/20 border-l-2 border-sigma-600' : ''}
        ${isDragOver ? 'bg-blue-50 dark:bg-blue-900/30 ring-2 ring-blue-400 ring-inset rounded-lg' : ''}
        ${isSelected && selectedDocId !== doc.id ? 'bg-blue-50/40 dark:bg-blue-900/20' : ''}
      `}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2 flex-1 min-w-0">
          {/* Checkbox */}
          <input
            type="checkbox"
            checked={isSelected}
            onChange={(e) => { e.stopPropagation(); onToggleSelect(doc.id) }}
            onClick={(e) => e.stopPropagation()}
            className="w-3.5 h-3.5 mt-0.5 rounded border-gray-300 dark:border-gray-600 text-sigma-600 focus:ring-sigma-500 cursor-pointer flex-shrink-0"
          />
          {/* Icon */}
          {doc.is_folder
            ? <Folder className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
            : <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 flex-shrink-0" />
          }
          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <div className="text-sm font-semibold text-gray-800 dark:text-gray-200 truncate">
                {renamingItemId === doc.id ? (
                  <input
                    ref={renameInputRef}
                    value={renameValue}
                    onChange={onRenameChange}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); onRenameConfirm(doc.id) }
                      if (e.key === 'Escape') { e.preventDefault(); onRenameCancel() }
                    }}
                    onMouseDown={(e) => e.stopPropagation()}
                    onClick={(e) => e.stopPropagation()}
                    className="border-b-2 border-sigma-600 outline-none text-sm font-semibold bg-transparent px-1 w-40"
                  />
                ) : (
                  doc.title
                )}
              </div>
                {renamingItemId === doc.id && (
                  <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() => onRenameConfirm(doc.id)}
                      className="p-0.5 text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/30 rounded transition-colors"
                    ><Check className="w-3 h-3" /></button>
                    <button
                      onClick={() => onRenameCancel()}
                      className="p-0.5 text-red-500 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 rounded transition-colors"
                    ><X className="w-3 h-3" /></button>
                  </div>
                )}
              {!doc.is_folder && getStatusBadge(doc.processing_status)}
            </div>
            {doc.description && !doc.is_folder && <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-1">{doc.description}</div>}
            {/* Search snippet */}
            {isSearchResult && doc.search_snippets && doc.search_snippets.length > 0 && (
              <div className="mt-1 space-y-0.5">
                {doc.search_snippets.map((snippet, i) => (
                  <div key={i} className="text-[11px] text-gray-500 dark:text-gray-400 bg-yellow-50/50 dark:bg-yellow-900/30 border-l-2 border-yellow-200 dark:border-yellow-800/50 pl-2 py-0.5 rounded-r">
                    {searchMode === 'keyword' ? highlightQuery(snippet, searchQuery) : snippet}
                  </div>
                ))}
              </div>
            )}
            {/* Keywords and meta */}
            {!doc.is_folder && (
              <div className="flex items-center gap-2 mt-1.5">
                <span className="text-[10px] text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded">{doc.doc_type || 'text'}</span>
                {(doc.keywords || []).slice(0, 3).map((kw, i) => (
                  <span key={i} onClick={(e) => { e.stopPropagation(); onKeywordSearch?.(kw) }} className="text-[10px] text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20 px-1.5 py-0.5 rounded flex items-center gap-0.5 cursor-pointer hover:bg-sigma-100 transition-colors">
                    <Tag className="w-2.5 h-2.5" />{kw}
                  </span>
                ))}
                {isSearchResult && searchMode === 'semantic' && doc.relevance_score != null && (
                  <span className="text-[10px] font-bold text-sigma-600 bg-sigma-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
                    {doc.relevance_score.toFixed(2)}
                  </span>
                )}
              </div>
            )}
            {/* Folder item: show date */}
            {doc.is_folder && (
              <div className="text-[10px] text-gray-400 dark:text-gray-500 mt-1">
                {doc.updated_at ? new Date(doc.updated_at).toLocaleDateString() : ''}
              </div>
            )}
          </div>
        </div>
        {/* Actions */}
        {!doc.is_folder && (
          <div className="flex items-center gap-0.5">
            {doc.processing_status === 'failed' && (
              <>
                <button onClick={(e) => { e.stopPropagation(); onReprocess(doc.id) }}
                  disabled={reprocessingIds?.has(doc.id)}
                  title={t('library.reprocess')}
                  className={`p-1 rounded ${reprocessingIds?.has(doc.id) ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : 'text-orange-500 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300'}`}>
                  {reprocessingIds?.has(doc.id) ? <Spinner size="xs" /> : <Redo2 className="w-3.5 h-3.5" />}
                </button>
                <button onClick={(e) => { e.stopPropagation(); onViewLog(doc) }} title={t('library.viewProcessingLog')} className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 rounded">
                  <ScrollText className="w-3.5 h-3.5" />
                </button>
              </>
            )}
            {(doc.processing_status === 'processing' || doc.processing_status === 'indexing' || doc.processing_status === 'cancelling') && (
              <button onClick={(e) => { e.stopPropagation(); onViewLog(doc) }} title={t('library.viewLog')} className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 rounded">
                <ScrollText className="w-3.5 h-3.5" />
              </button>
            )}
            <button onClick={(e) => { e.stopPropagation(); onDelete(doc.id) }} className="p-1 text-gray-400 dark:text-gray-500 hover:text-red-500 dark:hover:text-red-400 rounded" title={t('common.delete')}>
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/* =========================================================================
 * Inline-editable field
 * ========================================================================= */
/* =========================================================================
 * Content field
 * ========================================================================= */
function ContentField({ docId, projectId, value, previewValue = '', truncated = false, onSave }) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [preview, setPreview] = useState(false)
  const [fullValue, setFullValue] = useState(value || '')
  const [loadingFull, setLoadingFull] = useState(false)
  const [draft, setDraft] = useState(value || previewValue || '')

  const loadFull = async () => {
    if (fullValue || !truncated || !projectId || !docId) return fullValue
    setLoadingFull(true)
    try {
      const doc = await libraryAPI.get(projectId, docId, { include_content: true })
      const content = doc.content || ''
      setFullValue(content)
      setDraft(content)
      return content
    } finally {
      setLoadingFull(false)
    }
  }

  const startEdit = async () => {
    const content = await loadFull()
    setDraft(content || value || previewValue || '')
    setEditing(true)
    setPreview(false)
  }
  const cancel = () => { setEditing(false); setPreview(false); setDraft(fullValue || value || previewValue || '') }
  const save = () => { onSave(draft); setFullValue(draft); setEditing(false); setPreview(false) }

  if (editing) {
    return (
      <div className="group">
        <div className="flex items-center gap-1.5 mb-1">
          <span className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider">{t('library.content')}</span>
          <button onClick={() => setPreview(!preview)} className="p-0.5 text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400 rounded transition-colors ml-1" title={preview ? t('common.edit') : t('common.preview')}>
            {preview ? <Edit3 className="w-3 h-3" /> : <ScrollText className="w-3 h-3" />}
          </button>
          <span className="flex-1" />
          <button onClick={save} className="p-0.5 text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/30 rounded transition-colors"><Check className="w-3 h-3" /></button>
          <button onClick={cancel} className="p-0.5 text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"><X className="w-3 h-3" /></button>
        </div>
        {preview ? (
          <div className="w-full px-4 py-3 border border-gray-200 dark:border-gray-700 rounded-lg min-h-[120px]">
            <MarkdownContent content={draft} />
          </div>
        ) : (
          <textarea
            autoFocus
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === 'Escape') cancel() }}
            rows={Math.max(8, draft.split('\n').length)}
            className="w-full px-3 py-2 border border-sigma-300 rounded-lg text-sm font-mono leading-relaxed focus:ring-2 focus:ring-sigma-600/20 focus:border-sigma-600 outline-none resize-y bg-white dark:bg-gray-900 dark:text-gray-200"
          />
        )}
      </div>
    )
  }

  return (
    <div className="group">
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider">{t('library.content')}</span>
        <button
          onClick={startEdit}
          className="p-0.5 text-transparent group-hover:text-gray-400 dark:group-hover:text-gray-500 hover:!text-sigma-600 rounded transition-colors"
          title={t('library.editContent')}
        >
          <Pencil className="w-3 h-3" />
        </button>
        {truncated && !fullValue && (
          <button
            onClick={loadFull}
            disabled={loadingFull}
            className="text-xs text-sigma-600 hover:text-sigma-700 disabled:text-gray-300 dark:disabled:text-gray-600"
          >
            {loadingFull ? t('common.loading') : t('library.loadFull')}
          </button>
        )}
      </div>
      <div className="text-sm text-gray-800 dark:text-gray-200 leading-relaxed">
        {(fullValue || value || previewValue)
          ? <MarkdownContent content={fullValue || value || previewValue} />
          : <span className="text-gray-300 dark:text-gray-600 italic">{t('library.emptyContent')}</span>}
      </div>
    </div>
  )
}

/* =========================================================================
 * Detail Panel
 * ========================================================================= */
function DetailPanel({ doc, projectId, onClose, getStatusBadge, onFieldSave, onReprocess, isReprocessing, onViewLog, onDelete, onRefresh, onKeywordSearch, width }) {
  const { t } = useTranslation()
  const parsedKeywords = doc.keywords || []
  const [aiExtracting, setAiExtracting] = useState(false)

  const handleAiExtract = async () => {
    setAiExtracting(true)
    try {
      await libraryAPI.extractFields(projectId, doc.id)
      toastSuccess(t('library.aiExtractCompleted'))
      await onRefresh?.()
    } catch (e) {
      toastError(e.message || t('library.aiExtractFailed'))
    } finally {
      setAiExtracting(false)
    }
  }

  return (
    <div className="min-w-0 bg-gray-50/30 dark:bg-gray-900/30 flex-shrink-0 grid grid-rows-[auto_1fr] h-full" style={{ width }}>
      <div className="flex items-center gap-2 px-5 py-3 border-b border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 flex-shrink-0">
        {getStatusBadge(doc.processing_status)}
        <span className="text-[10px] text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded">{doc.doc_type || 'text'}</span>
        <span className="flex-1" />
        <button onClick={handleAiExtract} disabled={aiExtracting || doc.processing_status !== 'completed'} title={doc.processing_status === 'completed' ? t('library.aiExtractFields') : t('library.docMustComplete')} className={`p-1.5 rounded-lg transition-colors ${aiExtracting ? 'text-purple-400 animate-pulse' : doc.processing_status !== 'completed' ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : 'text-gray-400 dark:text-gray-500 hover:text-purple-500 dark:hover:text-purple-400 hover:bg-purple-50 dark:hover:bg-purple-900/30'}`}>
          <Sparkles className="w-3.5 h-3.5" />
        </button>
        {doc.processing_status === 'failed' && (
          <button onClick={() => onReprocess(doc.id)} disabled={isReprocessing} title={t('library.reprocess')}
            className={`p-1.5 rounded-lg transition-colors ${isReprocessing ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : 'text-orange-500 dark:text-orange-400 hover:bg-orange-50 dark:hover:bg-orange-900/30'}`}>
            {isReprocessing ? <Spinner size="xs" /> : <Redo2 className="w-3.5 h-3.5" />}
          </button>
        )}
        {(doc.processing_status === 'failed' || doc.processing_status === 'processing' || doc.processing_status === 'indexing' || doc.processing_status === 'cancelling') && (
          <button onClick={() => onViewLog(doc)} title={t('library.viewLog')} className="p-1.5 text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors">
            <ScrollText className="w-3.5 h-3.5" />
          </button>
        )}
        <button onClick={() => onDelete(doc.id)} title={t('common.delete')} className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-red-500 dark:hover:text-red-400 rounded-lg transition-colors">
          <Trash2 className="w-3.5 h-3.5" />
        </button>
        <button onClick={onClose} className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 rounded-lg transition-colors">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="overflow-y-auto px-5 py-4 space-y-5 min-h-0">
        <InlineEditableField label={t('library.titleLabel')} value={doc.title} onSave={v => onFieldSave(doc.id, 'title', v)} aiHighlight={aiExtracting} />
        <InlineEditableField label={t('library.descriptionLabel')} value={doc.description} onSave={v => onFieldSave(doc.id, 'description', v)} aiHighlight={aiExtracting} />
        <InlineEditableField label={t('library.sourceLabel')} value={doc.source} onSave={v => onFieldSave(doc.id, 'source', v)} />
        <div className="group" style={aiExtracting ? {
          background: 'linear-gradient(90deg, rgba(191,219,254,0.3), rgba(221,214,254,0.3), rgba(191,219,254,0.3))',
          backgroundSize: '200% 100%',
          animation: 'aiShimmer 2s linear infinite',
          borderRadius: '8px',
        } : {}}>
          <div className="flex items-center gap-1.5 mb-1">
            <span className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider">{t('library.keywords')}</span>
            <button
              onClick={() => setKeywordEdit({ docId: doc.id, keywords: parsedKeywords })}
              className="p-0.5 text-transparent group-hover:text-gray-400 dark:group-hover:text-gray-500 hover:!text-sigma-600 rounded transition-colors"
              title={t('library.editKeywords')}
            >
              <Pencil className="w-3 h-3" />
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5 min-h-[1.5em]">
            {parsedKeywords.length > 0 ? parsedKeywords.map((kw, i) => (
              <span key={i} onClick={() => onKeywordSearch?.(kw)} className="text-[10px] text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20 px-1.5 py-0.5 rounded flex items-center gap-0.5 cursor-pointer hover:bg-sigma-100 transition-colors">
                <Tag className="w-2.5 h-2.5" />{kw}
              </span>
            )) : <span className="text-gray-300 dark:text-gray-600 italic text-sm">{t('library.emptyContent')}</span>}
          </div>
        </div>

        <div className="pt-3 border-t border-gray-100 dark:border-gray-800 space-y-2">
          <div className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-2">{t('library.meta')}</div>
          {doc.file_name && (
            <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              <File className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 flex-shrink-0" />
              <span className="truncate flex-1">{doc.file_name}</span>
              <a
                href={`/api/v1/library/${projectId}/documents/${doc.id}/download`}
                download
                title={t('library.downloadSource')}
                className="p-1 text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400 rounded transition-colors flex-shrink-0"
              >
                <Download className="w-3.5 h-3.5" />
              </a>
            </div>
          )}
          <div className="text-[11px] text-gray-400 dark:text-gray-500">
            {t('library.updatedLabel')}{doc.updated_at ? new Date(doc.updated_at).toLocaleString() : '-'}
          </div>
        </div>

        <ContentField
          docId={doc.id}
          projectId={projectId}
          value={doc.content}
          previewValue={doc.content_preview}
          truncated={doc.content_truncated}
          onSave={v => onFieldSave(doc.id, 'content', v)}
        />
      </div>
    </div>
  )
}

/* =========================================================================
 * Upload Modal
 * ========================================================================= */
function UploadModal({ onClose, onUpload, uploadLoading }) {
  const { t } = useTranslation()
  const [fileItems, setFileItems] = useState([])

  const handleFiles = (files) => {
    setFileItems(prev => [...prev, ...Array.from(files)])
  }

  const handleSubmit = () => {
    if (fileItems.length === 0) return
    onUpload(fileItems)
  }

  const removeFile = (idx) => {
    setFileItems(prev => { const n = [...prev]; n.splice(idx, 1); return n })
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 animate-in fade-in duration-300" onClick={onClose}>
      <div className="bg-white dark:bg-gray-900 rounded-2xl shadow-2xl dark:shadow-none w-[580px] max-h-[80vh] flex flex-col animate-in zoom-in duration-300" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <h2 className="text-base font-bold text-gray-800 dark:text-gray-200">{t('common.uploadFiles')}</h2>
          <button onClick={onClose} className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300"><X className="w-5 h-5" /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          <FileDropzone onFiles={handleFiles}>
            {t('library.clickOrDrag')}
          </FileDropzone>
          {fileItems.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-gray-500 dark:text-gray-400 uppercase">{t('library.selectedCountLabel', { count: fileItems.length })}</span>
                <button onClick={() => setFileItems([])} className="text-xs text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">{t('library.clearAll')}</button>
              </div>
              <SelectedFilesList files={fileItems} onRemove={removeFile} />
            </div>
          )}
        </div>
        <div className="px-6 py-4 border-t border-gray-100 dark:border-gray-800 flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition-colors">{t('common.cancel')}</button>
          <button
            onClick={handleSubmit}
            disabled={uploadLoading || fileItems.length === 0}
            className="px-4 py-2 text-sm bg-sigma-600 text-white rounded-xl hover:bg-sigma-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {uploadLoading && <Spinner size="sm" />}
            {uploadLoading ? t('library.uploadingCount', { count: fileItems.length }) : t('library.uploadButton', { count: fileItems.length })}
          </button>
        </div>
      </div>
    </div>
  )
}

/* =========================================================================
 * Processing Log Modal
 * ========================================================================= */
function LogModal({ doc, logContent, onClose }) {
  const { t } = useTranslation()
  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 animate-in fade-in duration-300" onClick={onClose}>
      <div className="bg-white dark:bg-gray-900 rounded-2xl shadow-2xl dark:shadow-none w-[640px] max-h-[80vh] flex flex-col animate-in zoom-in duration-300" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <div>
            <h2 className="text-base font-bold text-gray-800 dark:text-gray-200">{t('library.processingLog')}</h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{doc?.title}</p>
          </div>
          <button onClick={onClose} className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300"><X className="w-5 h-5" /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-6">
          <div className="bg-gray-900 text-green-400 rounded-xl p-4 font-mono text-xs whitespace-pre-wrap leading-relaxed max-h-[50vh] overflow-y-auto">
            {logContent || <span className="text-gray-500">{t('library.noLog')}</span>}
          </div>
        </div>
        <div className="px-6 py-4 border-t border-gray-100 dark:border-gray-800 flex justify-end">
          <button onClick={onClose} className="px-4 py-2 text-sm bg-sigma-600 text-white rounded-xl hover:bg-sigma-700 transition-colors">{t('common.close')}</button>
        </div>
      </div>
    </div>
  )
}
