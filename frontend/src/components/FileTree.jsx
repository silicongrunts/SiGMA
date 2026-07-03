import { useState, useEffect, useRef, useImperativeHandle, forwardRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useStore } from '../store/useStore'
import { filesAPI, projectsAPI } from '../api'
import { storage } from '../utils/storage'
import {
   File, FilePlus, FolderPlus, Upload, ChevronRight, ChevronDown,
   FileText, FileCode, FolderOpen, Folder, Edit3, Trash2, Download, RefreshCw, Package
} from 'lucide-react'
import { toastError, toastSuccess } from './Toast'
import { InputModal, ConfirmModal, ConflictModal } from './Modal'
import ContextMenu from './ContextMenu'
import { Spinner, LoadingOverlay } from './ui'
import { useTranslation } from 'react-i18next'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Recursively collect files from DataTransfer items (handles folders). */
async function collectEntries(items) {
  const files = []
  const queue = []

  for (const item of items) {
    const entry = item.webkitGetAsEntry?.()
    if (!entry) continue
    queue.push({ entry, basePath: '' })
  }

  while (queue.length > 0) {
    const { entry, basePath } = queue.shift()
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => { entry.file(resolve, reject) })
      files.push({ file, relativePath: basePath ? `${basePath}/${file.name}` : file.name })
    } else if (entry.isDirectory) {
      const reader = entry.createReader()
      const entries = await new Promise((resolve) => {
        const results = []
        const readBatch = () => {
          reader.readEntries((batch) => {
            if (batch.length === 0) { resolve(results); return }
            results.push(...batch)
            readBatch()
          })
        }
        readBatch()
      })
      for (const child of entries) {
        queue.push({ entry: child, basePath: basePath ? `${basePath}/${entry.name}` : entry.name })
      }
    }
  }

  return files
}

/** Collect all visible node paths in tree-display order. */
function getVisibleNodePaths(rootNodes, childrenCache) {
  const paths = []
  function walk(nodes) {
    for (const node of nodes) {
      paths.push(node.path)
      if (node.type === 'directory') {
        const children = childrenCache[node.path]
        if (children) walk(children)
      }
    }
  }
  walk(rootNodes)
  return paths
}

/** Check if two axis-aligned rects overlap. */
function rectsIntersect(a, b) {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top
}

/** Test if a filename is a supported archive. */
const ARCHIVE_RE = /\.(zip|tar(\.gz)?|tgz)$/

/** Compute the new path of a file after moving its ancestor directory/file. */
function computeMovedPath(currentFile, srcPath, destDir) {
  const effectiveDest = (!destDir || destDir === '.') ? '' : destDir
  const srcName = srcPath.split('/').pop()
  if (currentFile === srcPath) {
    return effectiveDest ? `${effectiveDest}/${srcName}` : srcName
  }
  if (currentFile.startsWith(srcPath + '/')) {
    const rest = currentFile.slice(srcPath.length)
    return effectiveDest ? `${effectiveDest}/${srcName}${rest}` : `${srcName}${rest}`
  }
  return null
}

function computeDestinationPath(srcPath, destDir) {
  const effectiveDest = (!destDir || destDir === '.') ? '' : destDir
  const srcName = srcPath.split('/').pop()
  return effectiveDest ? `${effectiveDest}/${srcName}` : srcName
}

function normalizeTreePath(path) {
  return String(path || '').replace(/^\/+/, '').replace(/\/+$/, '')
}

function pathMatchesOrIsChild(path, parentPath) {
  const normalizedPath = normalizeTreePath(path)
  const normalizedParent = normalizeTreePath(parentPath)
  return Boolean(normalizedPath && normalizedParent && (
    normalizedPath === normalizedParent || normalizedPath.startsWith(`${normalizedParent}/`)
  ))
}

function collectFilePathsFromTree(root) {
  const paths = []
  let truncated = false
  function walk(node) {
    if (!node) return
    if (node.truncated) truncated = true
    if (node.type === 'file' && node.path) paths.push(node.path)
    if (Array.isArray(node.children)) node.children.forEach(walk)
  }
  walk(root)
  return { paths, truncated }
}

// ---------------------------------------------------------------------------
// FileIcon
// ---------------------------------------------------------------------------

function FileIcon({ filename, isOpen }) {
  const ext = filename?.split('.').pop()?.toLowerCase()
  const iconMap = {
    pdf: <FileText className="w-4 h-4 text-red-500" />,
    tex: <FileText className="w-4 h-4 text-blue-500" />,
    md: <FileText className="w-4 h-4 text-purple-500" />,
    bib: <FileText className="w-4 h-4 text-green-600" />,
    sty: <FileText className="w-4 h-4 text-orange-500" />,
    cls: <FileText className="w-4 h-4 text-orange-500" />,
    json: <FileText className="w-4 h-4 text-yellow-500" />,
    ipynb: <FileCode className="w-4 h-4 text-orange-500" />,
  }
  if (isOpen !== undefined) return isOpen ? <FolderOpen className="w-4 h-4 text-blue-400" /> : <Folder className="w-4 h-4 text-blue-400" />
  return iconMap[ext] || <File className="w-4 h-4 text-gray-400" />
}

// ---------------------------------------------------------------------------
// TreeNode
// ---------------------------------------------------------------------------

function TreeNode({ node, projectId, level = 0, onFileClick, currentFile, onRefresh, onContextMenu, onUploadFiles, childrenCache, loadingPaths, onExpand, isSelected, selectedPaths, onSelectionClick, onClearSelection }) {
  const { t } = useTranslation()
  const [isExpanded, setIsExpanded] = useState(false)
  const isFolder = node.type === 'directory'
  const isActive = currentFile === node.path
  const [isDragOver, setIsDragOver] = useState(false)
  const childNodes = isFolder ? childrenCache[node.path] : undefined
  const isLoadingChildren = isFolder && loadingPaths.has(node.path)
  const hasLoadedChildren = childNodes !== undefined

  useEffect(() => {
    if (isFolder && isExpanded && !hasLoadedChildren && !isLoadingChildren && onExpand) {
      onExpand(node.path)
    }
  }, [isFolder, isExpanded, hasLoadedChildren, isLoadingChildren, onExpand, node.path])

  const handleToggle = useCallback(() => {
    if (!isFolder) return
    setIsExpanded(prev => !prev)
  }, [isFolder])

  const handleDrop = async (e) => {
    e.preventDefault(); e.stopPropagation(); setIsDragOver(false)
    if (!isFolder) return
    // OS file/folder drop → upload into this folder
    if (e.dataTransfer.items?.length > 0) {
      const entries = await collectEntries(e.dataTransfer.items)
      if (entries.length > 0) { onUploadFiles(entries, node.path); return }
    }
    if (e.dataTransfer.files?.length > 0) {
      const flat = Array.from(e.dataTransfer.files).map(f => ({ file: f, relativePath: f.name }))
      if (flat.length > 0) { onUploadFiles(flat, node.path); return }
    }
    // Multi-file drag → move all selected paths
    const jsonPaths = e.dataTransfer.getData('application/json')
    if (jsonPaths) {
      const paths = JSON.parse(jsonPaths)
      await onRefresh('move', paths, node.path)
      return
    }
    // Single internal drag → move
    const srcPath = e.dataTransfer.getData('sourcePath')
    if (srcPath && srcPath !== node.path) {
      await onRefresh('move', [srcPath], node.path)
    }
  }

  return (
    <div className={`select-none transition-all ${isDragOver ? 'bg-blue-50 dark:bg-blue-900/30 ring-2 ring-blue-400 dark:ring-blue-500 ring-inset rounded-lg' : ''}`}
        onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); if (isFolder) setIsDragOver(true); }} onDragLeave={() => setIsDragOver(false)} onDrop={handleDrop}>
      <div
        draggable={node.path !== ""}
        onDragStart={(e) => {
          if (selectedPaths && selectedPaths.size > 1 && selectedPaths.has(node.path)) {
            e.dataTransfer.setData('application/json', JSON.stringify([...selectedPaths]))
          }
          e.dataTransfer.setData('sourcePath', node.path)
          e.dataTransfer.effectAllowed = 'move'
        }}
        className={`flex items-center gap-2 py-1.5 px-3 cursor-pointer group hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors relative w-full ${
          isActive && isSelected ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-600 dark:text-blue-400 font-bold ring-1 ring-inset ring-blue-400 dark:ring-blue-500' :
          isActive ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 font-bold' :
          isSelected ? 'bg-blue-100/60 dark:bg-blue-900/30 ring-1 ring-inset ring-blue-300 dark:ring-blue-600 text-blue-700 dark:text-blue-300' :
          'text-gray-700 dark:text-gray-300'
        }`}
        style={{ paddingLeft: `${level * 12 + 12}px` }}
        data-file-tree-node="true"
        data-tree-path={node.path}
        onClick={(e) => {
          e.stopPropagation()
          if (e.shiftKey || e.ctrlKey || e.metaKey) {
            onSelectionClick?.(e, node)
            return
          }
          onClearSelection?.(node.path)
          isFolder ? handleToggle() : onFileClick?.(node)
        }}
        onContextMenu={(e) => { e.stopPropagation(); e.preventDefault(); if (node.path) onContextMenu(e, node); }}
      >
        {isActive && !isSelected && <div className="absolute left-0 top-0 bottom-0 w-1 bg-blue-600 rounded-r" />}
        {isActive && isSelected && <div className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r" />}
        <span className="w-4 h-4 flex items-center justify-center">
          {isFolder && (isExpanded ? <ChevronDown className="w-3 h-3 text-gray-400" /> : <ChevronRight className="w-3 h-3 text-gray-400" />)}
        </span>
        <FileIcon filename={node.name} isOpen={isFolder ? isExpanded : undefined} />
        <span className="text-sm truncate flex-1">{node.name}</span>
      </div>
      {isFolder && isExpanded && (
        (!hasLoadedChildren || isLoadingChildren) ? (
          <div className="flex items-center gap-2 py-1.5 text-gray-400" style={{ paddingLeft: `${(level + 1) * 12 + 12}px` }}>
            <Spinner size="xs" />
            <span className="text-xs">{t('common.loading')}</span>
          </div>
        ) : childNodes.length > 0 ? (
          <div>
            {childNodes.map((child) => (
              <TreeNode
                key={child.path}
                node={child}
                projectId={projectId}
                level={level + 1}
                onFileClick={onFileClick}
                currentFile={currentFile}
                onRefresh={onRefresh}
                onContextMenu={onContextMenu}
                onUploadFiles={onUploadFiles}
                childrenCache={childrenCache}
                loadingPaths={loadingPaths}
                onExpand={onExpand}
                isSelected={selectedPaths?.has(child.path) ?? false}
                selectedPaths={selectedPaths}
                onSelectionClick={onSelectionClick}
                onClearSelection={onClearSelection}
              />
            ))}
          </div>
        ) : (
          <div className="py-1 text-gray-400" style={{ paddingLeft: `${(level + 1) * 12 + 12}px` }}>
            <span className="text-xs">{t('common.emptyFolder')}</span>
          </div>
        )
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// FileTree (root)
// ---------------------------------------------------------------------------

export const FileTree = forwardRef(({ onFileSelect, onSaveCurrentFile }, ref) => {
  const { t } = useTranslation()
  const currentProjectId = useStore(state => state.currentProject?.id)
  const currentFile = useStore(state => state.currentFile)
  const setCurrentFile = useStore(s => s.setCurrentFile)
  const setCurrentProject = useStore(s => s.setCurrentProject)
  const [rootNodes, setRootNodes] = useState([])
  const [loading, setLoading] = useState(false)
  const [childrenCache, setChildrenCache] = useState({})
  const [loadingPaths, setLoadingPaths] = useState(new Set())
  const [menu, setMenu] = useState(null)
  const [modal, setModal] = useState({ open: false, type: 'file', parent: '', initial: '', title: '' })
  const [confirmModal, setConfirmModal] = useState({ open: false, node: null })
  const [conflictModal, setConflictModal] = useState({ open: false, conflicts: [], entries: null, targetDir: '' })
  const [extracting, setExtracting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [moving, setMoving] = useState(false)
  const [batchDownloading, setBatchDownloading] = useState(false)
  const fileInputRef = useRef(null)
  const cacheRef = useRef(childrenCache)
  cacheRef.current = childrenCache

  // --- Multi-selection state ---
  const [selectedPaths, setSelectedPaths] = useState(new Set())
  const [anchorPath, setAnchorPath] = useState(null)
  const [rubberBand, setRubberBand] = useState(null)
  const treeContainerRef = useRef(null)
  const visiblePathsRef = useRef([])
  const rubberBandCleanup = useRef(null)
  const anchorPathRef = useRef(null)
  const rubberBandDidSelectRef = useRef(false)

  // Rebuild visible paths when tree changes
  useEffect(() => {
    visiblePathsRef.current = getVisibleNodePaths(rootNodes, childrenCache)
  }, [rootNodes, childrenCache])

  // clearSelection: optionally set a new anchor (used by plain clicks)
  const clearSelection = useCallback((newAnchor = null) => {
    setSelectedPaths(new Set())
    setAnchorPath(newAnchor)
    anchorPathRef.current = newAnchor
  }, [])

  const clearMainFile = useCallback(async () => {
    const project = useStore.getState().currentProject
    if (!project?.id || !project.main_file) return
    setCurrentProject({ ...project, main_file: '' })
    try {
      const updated = await projectsAPI.update(project.id, { main_file: '' })
      if (useStore.getState().currentProject?.id === project.id) {
        setCurrentProject(updated)
      }
    } catch {
      // The deleted file no longer exists locally; keep UI state consistent.
    }
  }, [setCurrentProject])

  const reconcileRemovedPaths = useCallback((removedPaths) => {
    const paths = removedPaths.map(normalizeTreePath).filter(Boolean)
    if (paths.length === 0) return

    const state = useStore.getState()
    const activeFile = state.currentFile
    const previewSource = state.previewSource
    const previewPaths = [previewSource?.path, previewSource?.mainFile].filter(Boolean)

    const activeHit = activeFile && paths.some(path => pathMatchesOrIsChild(activeFile, path))
    const previewHit = previewPaths.some(previewPath =>
      paths.some(path => pathMatchesOrIsChild(previewPath, path))
    )

    if (activeHit) {
      // clearCurrentFile also resets previewSource, so it covers both.
      state.clearCurrentFile()
    } else if (previewHit) {
      // Editor isn't on a deleted file, but the preview is (e.g. previewing a
      // standalone PDF whose file got deleted). Clear only the preview side.
      state.setPreviewSource({ kind: 'none', path: null, compileVersion: 0 })
    }

    const mainFile = state.currentProject?.main_file
    if (mainFile && paths.some(path => pathMatchesOrIsChild(mainFile, path))) {
      clearMainFile()
    }
  }, [clearMainFile])

  // --- Selection click handler (Ctrl / Shift) ---
  // Uses refs for anchorPath/visiblePaths to avoid stale-closure issues
  const handleSelectionClick = useCallback((e, node) => {
    if (e.ctrlKey || e.metaKey) {
      setSelectedPaths(prev => {
        const next = new Set(prev)
        if (next.has(node.path)) next.delete(node.path)
        else next.add(node.path)
        return next
      })
      anchorPathRef.current = node.path
      setAnchorPath(node.path)
      return
    }
    if (e.shiftKey) {
      const anchor = anchorPathRef.current
      if (!anchor) {
        clearSelection()
        if (node.type !== 'directory') onFileSelect?.(node)
        return
      }
      const paths = visiblePathsRef.current
      const anchorIdx = paths.indexOf(anchor)
      const clickIdx = paths.indexOf(node.path)
      if (anchorIdx < 0 || clickIdx < 0) return
      const start = Math.min(anchorIdx, clickIdx)
      const end = Math.max(anchorIdx, clickIdx)
      setSelectedPaths(new Set(paths.slice(start, end + 1)))
      return
    }
  }, [clearSelection, onFileSelect])

  // --- Rubber band ---
  const startRubberBand = useCallback((e) => {
    // Only left button
    if (e.button !== 0) return
    // Only start if mousedown is on empty area (not on a tree node)
    let el = e.target
    for (let i = 0; i < 20 && el; i++) {
      if (el.getAttribute('data-file-tree-node') !== null) return
      el = el.parentElement
    }
    const startX = e.clientX
    const startY = e.clientY
    setRubberBand({ startX, startY, currentX: startX, currentY: startY })

    const onMove = (ev) => {
      setRubberBand(prev => prev ? { ...prev, currentX: ev.clientX, currentY: ev.clientY } : null)
    }
    const onUp = () => {
      setRubberBand(null)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      window.removeEventListener('wheel', onWheel)
      rubberBandCleanup.current = null
    }
    const onWheel = () => {
      setRubberBand(null)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      window.removeEventListener('wheel', onWheel)
      rubberBandCleanup.current = null
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    window.addEventListener('wheel', onWheel)
    rubberBandCleanup.current = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      window.removeEventListener('wheel', onWheel)
    }
  }, [])

  // Clean up rubber band listeners on unmount
  useEffect(() => {
    return () => { rubberBandCleanup.current?.() }
  }, [])

  // Compute rubber band selection — use tree container's X bounds so any vertical overlap selects rows
  useEffect(() => {
    if (!rubberBand) return
    const container = treeContainerRef.current
    if (!container) return
    const containerRect = container.getBoundingClientRect()

    const yTop = Math.min(rubberBand.startY, rubberBand.currentY)
    const yBottom = Math.max(rubberBand.startY, rubberBand.currentY)
    // Ignore tiny rectangles (clicks)
    if (yBottom - yTop < 4) return

    // Selection rect spans full tree width
    const selRect = {
      left: containerRect.left,
      right: containerRect.right,
      top: yTop,
      bottom: yBottom,
    }

    const elements = container.querySelectorAll('[data-tree-path]')
    const newSelection = new Set()
    for (const el of elements) {
      const elRect = el.getBoundingClientRect()
      if (rectsIntersect(selRect, elRect)) {
        newSelection.add(el.getAttribute('data-tree-path'))
      }
    }
    setSelectedPaths(newSelection)
    rubberBandDidSelectRef.current = newSelection.size > 0
  }, [rubberBand])

  // --- Move helper: save before moving the current file, update path after ---
  const performMove = useCallback(async (srcPaths, destDir) => {
    if (!currentProjectId) return
    const curFile = useStore.getState().currentFile
    const effectiveDest = (!destDir || destDir === '.') ? '' : destDir

    // Filter out no-op moves (source would end up at same location)
    const actualMoves = srcPaths.filter(src => {
      if (destDir && src === destDir) return false
      const srcName = src.split('/').pop()
      const finalPath = effectiveDest ? `${effectiveDest}/${srcName}` : srcName
      return finalPath !== src
    })
    if (actualMoves.length === 0) return

    // Check if any source path affects the current file
    let affectedNewPath = null
    for (const src of actualMoves) {
      const moved = computeMovedPath(curFile, src, destDir)
      if (moved) { affectedNewPath = moved; break }
    }

    // Save before moving if current file is affected
    if (affectedNewPath && useStore.getState().hasUnsavedChanges) {
      try { await onSaveCurrentFile?.() } catch { /* best effort */ }
    }

    setMoving(true)
    let ok = 0
    try {
      for (const src of actualMoves) {
        try {
          await filesAPI.move(currentProjectId, src, destDir || '.')
          storage.moveFileState(currentProjectId, src, computeDestinationPath(src, destDir))
          ok++
        } catch (err) { toastError(`${src}: ${err.message}`) }
      }
      if (ok > 0) {
        // Update current file path if it was moved
        if (affectedNewPath) {
          setCurrentFile(affectedNewPath)
        }
        loadRootNodes()
        clearSelection()
      }
    } finally {
      setMoving(false)
    }
  }, [currentProjectId, onSaveCurrentFile, clearSelection])

  // --- Tree data loading ---
  const loadRootNodes = useCallback(async () => {
    if (!currentProjectId) return
    setLoading(true)
    try {
      const data = await filesAPI.children(currentProjectId, '')
      setRootNodes(data.children || [])
      setChildrenCache({})
      clearSelection()
      try {
        const tree = await filesAPI.tree(currentProjectId)
        const { paths, truncated } = collectFilePathsFromTree(tree.root)
        if (!truncated) {
          storage.pruneFileState(currentProjectId, paths)
          const curFile = useStore.getState().currentFile
          const mainFile = useStore.getState().currentProject?.main_file
          const existing = new Set(paths.map(normalizeTreePath))
          const missingPaths = []
          if (curFile && !existing.has(normalizeTreePath(curFile))) missingPaths.push(curFile)
          if (mainFile && !existing.has(normalizeTreePath(mainFile))) missingPaths.push(mainFile)
          reconcileRemovedPaths(missingPaths)
        }
      } catch {
        // File-state pruning is a cache cleanup; root loading still succeeded.
      }
    } finally { setLoading(false) }
  }, [currentProjectId, clearSelection, reconcileRemovedPaths])

  const loadChildren = useCallback(async (parentPath) => {
    if (!currentProjectId || cacheRef.current[parentPath]) return
    setLoadingPaths(prev => new Set(prev).add(parentPath))
    try {
      const data = await filesAPI.children(currentProjectId, parentPath)
      setChildrenCache(prev => ({ ...prev, [parentPath]: data.children || [] }))
    } catch (err) {
      toastError(err.message)
    } finally {
      setLoadingPaths(prev => {
        const next = new Set(prev)
        next.delete(parentPath)
        return next
      })
    }
  }, [currentProjectId])

  // onRefresh: when called as a normal refresh (no args), just refresh tree.
  // When called with ('move', srcPaths, destDir), performs a move operation.
  const onRefresh = useCallback(async (action, srcPaths, destDir) => {
    if (action === 'move' && srcPaths) {
      await performMove(srcPaths, destDir)
    } else {
      loadRootNodes()
    }
  }, [loadRootNodes, performMove])

  useImperativeHandle(ref, () => ({ refresh: loadRootNodes }))
  useEffect(() => { loadRootNodes() }, [currentProjectId, loadRootNodes])

  // --- Modal handlers ---
  const handleModalConfirm = async (value) => {
    if (modal.mode === 'create') {
        const fullPath = modal.parent ? `${modal.parent}/${value}` : value
        await filesAPI.create(currentProjectId, fullPath, modal.type === 'folder')
    } else if (modal.mode === 'rename') {
        await filesAPI.rename(currentProjectId, modal.node.path, value)
        const parent = modal.node.path.includes('/') ? modal.node.path.split('/').slice(0, -1).join('/') : ''
        const nextPath = parent ? `${parent}/${value}` : value
        storage.moveFileState(currentProjectId, modal.node.path, nextPath)
    }
    loadRootNodes()
  }

  const handleDeleteConfirm = async () => {
    if (!confirmModal.node) return
    try {
        await filesAPI.delete(currentProjectId, confirmModal.node.path)
        storage.removeFileState(currentProjectId, confirmModal.node.path)
        reconcileRemovedPaths([confirmModal.node.path])
        loadRootNodes()
    } catch (err) { toastError(err.message) }
  }

  // --- Batch operations ---
  const handleBatchDelete = useCallback(async () => {
    if (selectedPaths.size === 0) return
    setConfirmModal({
      open: true,
      node: { name: `${selectedPaths.size} item${selectedPaths.size > 1 ? 's' : ''}` },
      isBatch: true,
    })
  }, [selectedPaths])

  const handleBatchDeleteConfirm = useCallback(async () => {
    const deletedPaths = []
    let ok = 0, fail = 0
    for (const path of selectedPaths) {
      try {
        await filesAPI.delete(currentProjectId, path)
        storage.removeFileState(currentProjectId, path)
        deletedPaths.push(path)
        ok++
      } catch { fail++ }
    }
    reconcileRemovedPaths(deletedPaths)
    if (ok > 0) { loadRootNodes(); clearSelection() }
    if (fail > 0) toastError(t('filetree.deletePartialFailed', { ok, fail }))
  }, [selectedPaths, currentProjectId, loadRootNodes, clearSelection, reconcileRemovedPaths])

  const handleBatchDownload = useCallback(async () => {
    if (selectedPaths.size === 0 || batchDownloading) return
    setBatchDownloading(true)
    try {
      if (selectedPaths.size === 1) {
        const path = [...selectedPaths][0]
        const blob = await filesAPI.download(currentProjectId, path)
        const name = path.split('/').pop()
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click()
        URL.revokeObjectURL(a.href)
      } else {
        const blob = await filesAPI.batchDownload(currentProjectId, [...selectedPaths])
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'selected.zip'; a.click()
        URL.revokeObjectURL(a.href)
      }
      toastSuccess(t('common.downloadStarted'))
    } catch (err) {
      toastError(err.message || t('projects.toast.downloadFailed'))
    } finally {
      setBatchDownloading(false)
    }
  }, [selectedPaths, currentProjectId, batchDownloading])

  // --- Extract ---
  const handleExtract = useCallback(async (node) => {
    setExtracting(true)
    try {
      const result = await filesAPI.extract(currentProjectId, node.path)
      if (result.conflicts && result.conflicts.length > 0) {
        setExtracting(false)
        setConflictModal({
          open: true,
          conflicts: result.conflicts.map(c => ({ name: c })),
          mode: 'extract',
          archivePath: node.path,
        })
      } else {
        toastSuccess(t('filetree.extractedSuccess'))
        loadRootNodes()
      }
    } catch (err) { toastError(err.message) }
    finally { setExtracting(false) }
  }, [currentProjectId, loadRootNodes])

  // --- Context menu ---
  const handleContextMenu = useCallback((e) => {
    let onNode = null;
    let el = e.target;
    for (let i = 0; i < 20 && el; i++) {
      if (el.getAttribute('data-file-tree-node') !== null) {
        onNode = el;
        break;
      }
      el = el.parentElement;
    }
    if (!onNode) {
      e.preventDefault();
      setMenu({ x: e.clientX, y: e.clientY, node: null, isEmptyArea: true });
    }
  }, []);

  const handleNodeContextMenu = useCallback((e, node) => {
    if (!node.path) { e.preventDefault(); return }
    e.preventDefault()
    // If clicking on an already-selected node in multi-selection, show batch menu
    if (selectedPaths.size > 1 && selectedPaths.has(node.path)) {
      setMenu({ x: e.clientX, y: e.clientY, node, isEmptyArea: false, isMultiSelect: true })
    } else {
      clearSelection()
      setMenu({ x: e.clientX, y: e.clientY, node, isEmptyArea: false, isMultiSelect: false })
    }
  }, [selectedPaths, clearSelection])

  // --- Upload ---
  const uploadEntries = useCallback(async (entries, targetDir = '') => {
    const uploadPaths = new Set()
    for (const { relativePath } of entries) {
      uploadPaths.add(targetDir ? `${targetDir}/${relativePath}` : relativePath)
    }

    let existingPaths = new Set()
    try {
      const tree = await filesAPI.tree(currentProjectId)
      const collectPaths = (nodes) => {
        for (const n of (nodes || [])) {
          if (n.type === 'file') existingPaths.add(n.path)
          if (n.children) collectPaths(n.children)
        }
      }
      collectPaths(tree.root?.children || [])
    } catch { /* best-effort */ }

    const conflicts = []
    const nonConflicts = []
    for (const entry of entries) {
      const targetPath = targetDir ? `${targetDir}/${entry.relativePath}` : entry.relativePath
      if (existingPaths.has(targetPath)) {
        conflicts.push({ name: entry.relativePath })
      } else {
        nonConflicts.push(entry)
      }
    }

    if (conflicts.length === 0) {
      await doUploadEntries(entries, targetDir, false)
      return
    }

    setConflictModal({ open: true, conflicts, entries, nonConflicts, targetDir, mode: 'upload' })
  }, [currentProjectId])

  const doUploadEntries = useCallback(async (entries, targetDir, overwrite) => {
    setUploading(true)
    let ok = 0
    try {
      for (const { file, relativePath } of entries) {
        const lastSlash = relativePath.lastIndexOf('/')
        const subDir = lastSlash >= 0 ? relativePath.substring(0, lastSlash) : ''
        const destDir = targetDir ? (subDir ? `${targetDir}/${subDir}` : targetDir) : subDir

        const fd = new FormData()
        fd.append('file', file, file.name)
        if (destDir) fd.append('path', destDir)
        if (overwrite) fd.append('overwrite', 'true')
        try { await filesAPI.upload(currentProjectId, fd); ok++ } catch (err) { toastError(`${relativePath}: ${err.message}`) }
      }
      if (ok > 0) { loadRootNodes(); toastSuccess(t('filetree.uploadedCount', { count: ok })) }
    } finally {
      setUploading(false)
    }
  }, [currentProjectId, loadRootNodes])

  // --- Root drop handler ---
  const handleRootDrop = useCallback(async (e) => {
    e.preventDefault()
    if (e.dataTransfer.items?.length > 0) {
      const entries = await collectEntries(e.dataTransfer.items)
      if (entries.length > 0) { uploadEntries(entries, ''); return }
    }
    if (e.dataTransfer.files?.length > 0) {
      const flat = Array.from(e.dataTransfer.files).map(f => ({ file: f, relativePath: f.name }))
      if (flat.length > 0) { uploadEntries(flat, ''); return }
    }
    // Multi-file drag → move all to root
    const jsonPaths = e.dataTransfer.getData('application/json')
    if (jsonPaths) {
      await performMove(JSON.parse(jsonPaths), '.')
      return
    }
    // Single drag → move to root
    const src = e.dataTransfer.getData('sourcePath')
    if (src) { await performMove([src], '.') }
  }, [uploadEntries, performMove])

  // --- Determine context menu options ---
  const getNodeMenuOptions = useCallback((node) => {
    const isArchive = node.type !== 'directory' && ARCHIVE_RE.test(node.name)
    const options = []
    if (node.type === 'directory') {
      options.push(
        { label: t('common.newFile'), icon: <FilePlus className="w-4 h-4" />, action: () => setModal({ open: true, mode: 'create', type: 'file', parent: node.path, title: t('common.newFile'), placeholder: t('filetree.filenamePlaceholder'), icon: FilePlus }) },
        { label: t('common.newFolder'), icon: <FolderPlus className="w-4 h-4" />, action: () => setModal({ open: true, mode: 'create', type: 'folder', parent: node.path, title: t('common.newFolder'), placeholder: t('filetree.foldernamePlaceholder'), icon: FolderPlus }) },
        { separator: true },
      )
    }
    if (isArchive) {
      options.push(
        { label: t('filetree.extractHere'), icon: <Package className="w-4 h-4" />, action: () => handleExtract(node) },
        { separator: true },
      )
    }
    options.push(
      { label: t('common.rename'), icon: <Edit3 className="w-4 h-4" />, action: () => setModal({ open: true, mode: 'rename', node, initial: node.name, title: t('common.rename'), placeholder: t('filetree.renamePlaceholder'), icon: Edit3 }) },
      { label: t('common.download'), icon: <Download className="w-4 h-4" />, action: async () => {
          try {
            const b = await filesAPI.download(currentProjectId, node.path)
            const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = `${node.name}${node.type === 'directory' ? '.zip' : ''}`; a.click();
            URL.revokeObjectURL(a.href)
            toastSuccess(t('common.downloadStarted'))
          } catch (err) {
            toastError(err.message || t('projects.toast.downloadFailed'))
          }
      }},
      { label: t('common.delete'), icon: <Trash2 className="w-4 h-4" />, danger: true, action: () => setConfirmModal({ open: true, node }) },
    )
    return options
  }, [currentProjectId, handleExtract, t])

  const getMultiSelectMenuOptions = useCallback(() => [
    { label: t('filetree.deleteSelected', { count: selectedPaths.size }), icon: <Trash2 className="w-4 h-4" />, danger: true, action: handleBatchDelete },
    { separator: true },
    { label: t('filetree.downloadSelected', { count: selectedPaths.size }), icon: <Download className="w-4 h-4" />, action: handleBatchDownload },
  ], [selectedPaths, handleBatchDelete, handleBatchDownload, t])

  return (
    <div className={`flex-1 flex flex-col min-h-0 bg-white dark:bg-gray-900 relative`}
      onContextMenu={handleContextMenu}
      onDrop={handleRootDrop}
      onDragOver={e => e.preventDefault()}
    >
      <div className="p-2 border-b border-gray-50 dark:border-gray-800 flex items-center gap-1 bg-gray-50/30 dark:bg-gray-900/30">
        <button onClick={() => setModal({ open: true, mode: 'create', type: 'file', parent: '', title: t('common.newFile'), placeholder: t('filetree.filenamePlaceholder'), icon: FilePlus })} className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-500 dark:text-gray-400" title={t('common.newFile')}><FilePlus className="w-4 h-4" /></button>
        <button onClick={() => setModal({ open: true, mode: 'create', type: 'folder', parent: '', title: t('common.newFolder'), placeholder: t('filetree.foldernamePlaceholder'), icon: FolderPlus })} className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-500 dark:text-gray-400" title={t('common.newFolder')}><FolderPlus className="w-4 h-4" /></button>
        <button onClick={() => fileInputRef.current?.click()} className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-500 dark:text-gray-400" title={t('filetree.uploadFile')}><Upload className="w-4 h-4" /></button>
        <button onClick={loadRootNodes} className={`p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-500 dark:text-gray-400 ${loading ? 'animate-spin text-sigma-600' : ''}`} title={t('filetree.refreshTree')}><RefreshCw className="w-4 h-4" /></button>
        <input ref={fileInputRef} type="file" multiple className="hidden" onChange={async (e) => {
            if (e.target.files.length) {
              const entries = Array.from(e.target.files).map(f => ({ file: f, relativePath: f.name }))
              uploadEntries(entries, '')
            }
            e.target.value = ''
        }} />
      </div>
      <div
        ref={treeContainerRef}
        className="flex-1 overflow-auto py-2"
        onMouseDown={startRubberBand}
        onClick={(e) => {
          // Prevent click-after-mouseup from clearing rubber band selection
          if (rubberBandDidSelectRef.current) {
            rubberBandDidSelectRef.current = false
            return
          }
          let el = e.target
          for (let i = 0; i < 20 && el; i++) {
            if (el.getAttribute('data-file-tree-node') !== null) return
            el = el.parentElement
          }
          clearSelection()
        }}
      >
        {rootNodes.map(node => (
          <TreeNode
            key={node.path}
            node={node}
            projectId={currentProjectId}
            onFileClick={onFileSelect}
            onRefresh={onRefresh}
            currentFile={currentFile}
            onUploadFiles={uploadEntries}
            onContextMenu={handleNodeContextMenu}
            childrenCache={childrenCache}
            loadingPaths={loadingPaths}
            onExpand={loadChildren}
            isSelected={selectedPaths.has(node.path)}
            selectedPaths={selectedPaths}
            onSelectionClick={handleSelectionClick}
            onClearSelection={clearSelection}
          />
        ))}
      </div>

      {/* Rubber band visual — clipped to tree container bounds */}
      {rubberBand && (() => {
        const container = treeContainerRef.current
        if (!container) return null
        const cr = container.getBoundingClientRect()
        const yTop = Math.min(rubberBand.startY, rubberBand.currentY)
        const yBottom = Math.max(rubberBand.startY, rubberBand.currentY)
        if (yBottom - yTop < 4) return null
        // Clip to container bounds
        const clipTop = Math.max(yTop, cr.top)
        const clipBottom = Math.min(yBottom, cr.bottom)
        if (clipBottom - clipTop < 2) return null
        return createPortal(
          <div style={{
            position: 'fixed',
            left: cr.left,
            top: clipTop,
            width: cr.width,
            height: clipBottom - clipTop,
            backgroundColor: 'rgba(37, 99, 235, 0.08)',
            borderTop: '1px solid rgba(37, 99, 235, 0.4)',
            borderBottom: '1px solid rgba(37, 99, 235, 0.4)',
            pointerEvents: 'none',
            zIndex: 999,
          }} />,
          document.body
        )
      })()}

      {/* Multi-select context menu */}
      {menu && !menu.isEmptyArea && menu.isMultiSelect && createPortal(
        <ContextMenu x={menu.x} y={menu.y} options={getMultiSelectMenuOptions()} onClose={() => setMenu(null)} />,
        document.body
      )}

      {/* Single-node context menu */}
      {menu && !menu.isEmptyArea && !menu.isMultiSelect && createPortal(
        <ContextMenu x={menu.x} y={menu.y} options={getNodeMenuOptions(menu.node)} onClose={() => setMenu(null)} />,
        document.body
      )}

      {/* Empty-area context menu */}
      {menu && menu.isEmptyArea && createPortal(
        <ContextMenu x={menu.x} y={menu.y} options={[
          { label: t('common.newFile'), icon: <FilePlus className="w-4 h-4" />, action: () => setModal({ open: true, mode: 'create', type: 'file', parent: '', title: t('common.newFile'), placeholder: t('filetree.filenamePlaceholder'), icon: FilePlus }) },
          { label: t('common.newFolder'), icon: <FolderPlus className="w-4 h-4" />, action: () => setModal({ open: true, mode: 'create', type: 'folder', parent: '', title: t('common.newFolder'), placeholder: t('filetree.foldernamePlaceholder'), icon: FolderPlus }) },
          { separator: true },
          { label: t('filetree.uploadFile'), icon: <Upload className="w-4 h-4" />, action: () => fileInputRef.current?.click() },
          { separator: true },
          { label: t('common.refresh'), icon: <RefreshCw className="w-4 h-4" />, action: loadRootNodes },
        ]} onClose={() => setMenu(null)} />,
        document.body
      )}

      <InputModal
        isOpen={modal.open} onClose={() => setModal({ ...modal, open: false })}
        onConfirm={handleModalConfirm} title={modal.title}
        placeholder={modal.placeholder} initialValue={modal.initial} icon={modal.icon}
        mode={modal.mode} type={modal.type} isNewFile={modal.mode === 'create' && modal.type === 'file'}
      />

      <ConfirmModal
        isOpen={confirmModal.open} onClose={() => setConfirmModal({ ...confirmModal, open: false })}
        onConfirm={confirmModal.isBatch ? handleBatchDeleteConfirm : handleDeleteConfirm}
        title={confirmModal.isBatch ? t('filetree.deleteItems') : t('filetree.deleteItem')}
        message={confirmModal.isBatch
          ? t('filetree.deleteMultipleConfirm', { count: selectedPaths.size })
          : t('filetree.deleteSingleConfirm', { name: confirmModal.node?.name })
        }
        danger={true}
      />

      <ConflictModal
        isOpen={conflictModal.open}
        conflicts={conflictModal.conflicts}
        onClose={() => setConflictModal({ ...conflictModal, open: false })}
        onOverwriteAll={() => {
          if (conflictModal.mode === 'extract') {
            setExtracting(true)
            filesAPI.extract(currentProjectId, conflictModal.archivePath, true)
              .then(() => { toastSuccess(t('filetree.extractedSuccess')); loadRootNodes() })
              .catch(err => toastError(err.message))
              .finally(() => setExtracting(false))
          } else {
            doUploadEntries(conflictModal.entries || [], conflictModal.targetDir, true)
          }
        }}
        onSkipConflicts={() => {
          if (conflictModal.mode === 'extract') {
            setExtracting(true)
            filesAPI.extract(currentProjectId, conflictModal.archivePath, false, true)
              .then(() => { toastSuccess(t('filetree.extractedConflicts')); loadRootNodes() })
              .catch(err => toastError(err.message))
              .finally(() => setExtracting(false))
            return
          }
          const skip = conflictModal.nonConflicts || []
          if (skip.length > 0) doUploadEntries(skip, conflictModal.targetDir, false)
        }}
      />

      {/* Loading overlay for extract / upload / move operations */}
      {(extracting || uploading || moving) && (
        <LoadingOverlay label={extracting ? t('filetree.extracting') : uploading ? t('filetree.uploading') : t('filetree.moving')} />
      )}
    </div>
  )
})

export default FileTree
