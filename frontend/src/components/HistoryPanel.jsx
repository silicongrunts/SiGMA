import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { gitsAPI, fetchBlob } from '../api'
import { toastError, toastSuccess, toastInfo } from './Toast'
import { copyToClipboard } from '../utils/clipboard'
import DiffView from './DiffView'
import ContextMenu from './ContextMenu'
import { InputModal, ConfirmModal } from './Modal'
import { Spinner } from './ui'
import {
  RefreshCw, GitBranch, GitCommitHorizontal, GitCompare,
  File, Clock, X, ChevronRight, ChevronDown, Tag, Tags,
  Maximize, Minimize,
  Download, Eye, Code, Binary
} from 'lucide-react'

const SNAPSHOT_MESSAGE_PREFIX = 'sigma:snapshot:v1:'
const SNAPSHOT_CATEGORY_ORDER = ['added', 'deleted', 'modified']

function formatCommitMessage(message, t) {
  if (message === 'Initial commit') return t('history.snapshot.initialCommit')
  if (message === 'Auto-snapshot') return t('history.snapshot.autoSnapshot')
  if (!message || !message.startsWith(SNAPSHOT_MESSAGE_PREFIX)) return message

  try {
    const payload = JSON.parse(decodeURIComponent(message.slice(SNAPSHOT_MESSAGE_PREFIX.length)))
    const parts = SNAPSHOT_CATEGORY_ORDER.flatMap(category => {
      const entry = payload?.[category]
      if (!entry || !Array.isArray(entry.names) || !entry.names.length || !entry.total) return []
      const names = entry.names.join(',')
      const key = entry.total > entry.names.length
        ? `history.snapshot.${category}More`
        : `history.snapshot.${category}Exact`
      return [t(key, { names, count: entry.total })]
    })
    return parts.length ? parts.join(t('history.snapshot.separator')) : t('history.snapshot.autoSnapshot')
  } catch {
    return message
  }
}

function TagChip({ name, onDelete }) {
  const { t } = useTranslation()
  return (
    <span className={`group/tag inline-flex items-center gap-0.5 pl-1.5 ${onDelete ? 'pr-0.5' : 'pr-1.5'} py-0.5 rounded-full bg-amber-50 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 text-[9px] font-medium max-w-[130px]`}>
      <Tag className="w-2 h-2 shrink-0" />
      <span className="truncate">{name}</span>
      {onDelete && (
        <button
          onClick={(e) => { e.stopPropagation(); onDelete(name) }}
          className="shrink-0 p-0.5 rounded-full opacity-0 group-hover/tag:opacity-100 hover:bg-amber-200/60 dark:hover:bg-amber-800/60 transition-opacity"
          title={t('common.delete')}
        >
          <X className="w-2.5 h-2.5" />
        </button>
      )}
    </span>
  )
}

// --- Commit Detail Modal with Preview & Download (side-by-side diff) ---
function CommitModal({ isOpen, onClose, commit, parentHash, projectId, onDownloadSnapshot, titleOverride, commitTags, onAddTag, onDeleteTag }) {
  const { t } = useTranslation()
  const [diffs, setDiffs] = useState({})
  const [diffLoading, setDiffLoading] = useState({})
  const [expanded, setExpanded] = useState(new Set())
  const [changedFiles, setChangedFiles] = useState([])
  const [filesLoading, setFilesLoading] = useState(true)
  const [blobs, setBlobs] = useState({})
  const [blobLoading, setBlobLoading] = useState({})
  const [viewMode, setViewMode] = useState({})
  const [downloading, setDownloading] = useState(false)
  const [pendingTagDelete, setPendingTagDelete] = useState(null)
  const [fullscreen, setFullscreen] = useState(false)

  useEffect(() => {
    if (isOpen && commit) {
      setDiffs({})
      setDiffLoading({})
      setExpanded(new Set())
      setBlobs({})
      setBlobLoading({})
      setViewMode({})
      setDownloading(false)
      setPendingTagDelete(null)
      setFullscreen(false)
      setFilesLoading(true)
      setChangedFiles([])

      gitsAPI.commitFiles(projectId, {
        commit: commit.hash,
        parent_commit: parentHash || null,
      }).then(data => {
        if (data && data.files) {
          setChangedFiles(data.files)
        }
      }).catch(() => {
        setChangedFiles([])
      }).finally(() => {
        setFilesLoading(false)
      })
    }
  }, [isOpen])

  const handleFileClick = async (filePath) => {
    if (expanded.has(filePath)) {
      const next = new Set(expanded)
      next.delete(filePath)
      setExpanded(next)
    } else {
      const next = new Set(expanded)
      next.add(filePath)
      setExpanded(next)

      if (!blobs[filePath] && !blobLoading[filePath]) {
        setBlobLoading(prev => ({ ...prev, [filePath]: true }))
        try {
          const blobData = await gitsAPI.getBlob(projectId, {
            path: filePath,
            commit: commit.hash,
          })
          setBlobs(prev => ({ ...prev, [filePath]: blobData }))
        } catch (err) {
          setBlobs(prev => ({
            ...prev,
            [filePath]: { success: false, error: err.message, can_preview: false }
          }))
        } finally {
          setBlobLoading(prev => ({ ...prev, [filePath]: false }))
        }
      }

      if (!diffs[filePath] && !diffLoading[filePath]) {
        setDiffLoading(prev => ({ ...prev, [filePath]: true }))
        try {
          const diffData = await gitsAPI.diff(projectId, {
            path: filePath,
            commit: commit.hash,
            short_hash: commit.short_hash,
            parent_commit: parentHash || null,
          })
          setDiffs(prev => ({ ...prev, [filePath]: diffData.lines || [] }))
        } catch (err) {
          setDiffs(prev => ({
            ...prev,
            [filePath]: { error: err.message }
          }))
        } finally {
          setDiffLoading(prev => ({ ...prev, [filePath]: false }))
        }
      }
    }
  }

  const handleViewTab = (filePath, mode) => {
    setViewMode(prev => ({ ...prev, [filePath]: mode }))
  }

  const handleDownload = async (file) => {
    if (downloading) return
    setDownloading(true)
    try {
      const blobObj = blobs[file.path]
      if (blobObj && blobObj.content !== null && blobObj.content !== undefined) {
        downloadText(blobObj.content, file.name || file.path)
      } else {
        const downloadUrl = gitsAPI.snapshot(projectId, commit.hash)
        downloadFromUrl(downloadUrl, file.name || file.path)
      }
    } catch (err) {
      toastError(t('history.toast.downloadFailed') + ': ' + (err.message || String(err)))
    } finally {
      setDownloading(false)
    }
  }

  const downloadText = (text, filename) => {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    triggerDownload(blob, filename)
  }

  const downloadFromUrl = (url, filename) => {
    fetchBlob(url).then(blob => triggerDownload(blob, filename))
      .catch(err => { toastError(t('history.toast.downloadFailed') + ': ' + (err.message || String(err))) })
  }

  const triggerDownload = (blobObj, filename) => {
    const url = URL.createObjectURL(blobObj)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const formatFileSize = (bytes) => {
    if (bytes < 1024) return bytes + ' B'
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
  }

  const renderFileEntry = (file, idx) => {
    const isExp = expanded.has(file.path)
    const currentMode = viewMode[file.path] || 'view'
    const blobData = blobs[file.path]
    const isBlobLoading = blobLoading[file.path]
    // Fullscreen stretches the preview toward the window bottom instead of
    // the small fixed caps; the offsets absorb the modal chrome above it.
    const previewMaxClass = fullscreen ? 'max-h-[calc(100vh-220px)]' : 'max-h-80'
    const codeMaxClass = fullscreen ? 'max-h-[calc(100vh-260px)]' : 'max-h-72'

    return (
      <div key={idx} className="group">
        <button
          onClick={() => handleFileClick(file.path)}
          className="w-full flex items-center gap-3 px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-left"
        >
          <span className="w-6 h-6 flex items-center justify-center">
            {isExp ? <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronRight className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
          </span>
          <File className="w-4 h-4 text-gray-400 dark:text-gray-500" />
          <span className="flex-1 text-sm text-gray-700 dark:text-gray-300 font-medium truncate">{file.path}</span>
          <span className={`text-xs px-2 py-0.5 rounded-full ${
            file.status === 'M' ? 'bg-orange-50 dark:bg-orange-900/30 text-orange-600 dark:text-orange-400' :
            file.status === 'A' ? 'bg-green-50 dark:bg-green-900/30 text-green-600 dark:text-green-400' :
            file.status === 'D' ? 'bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400' :
            'bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400'
          }`}>
            {file.status === 'M' ? t('common.status.modified') : file.status === 'A' ? t('common.status.added') : file.status === 'D' ? t('common.status.deleted') : file.status}
          </span>
        </button>

        {isExp && (
          <div className="border-t border-gray-100 dark:border-gray-800 mb-1">
            <div className="flex items-center gap-1 px-4 py-2 border-b border-gray-50 dark:border-gray-800">
              <button onClick={() => handleViewTab(file.path, 'view')} className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${currentMode === 'view' ? 'bg-sigma-600/10 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-300' : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'}`}>
                <Eye className="w-3.5 h-3.5" />{t('history.view')}
              </button>
              <button onClick={() => handleViewTab(file.path, 'diff')} className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${currentMode === 'diff' ? 'bg-sigma-600/10 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-300' : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'}`}>
                <Code className="w-3.5 h-3.5" />{t('history.diff')}
              </button>
              <div className="flex-1" />
              <span className="text-[10px] text-gray-400 dark:text-gray-500">{blobData && blobData.size ? formatFileSize(blobData.size) : ''}</span>
              <button onClick={() => handleDownload(file)} disabled={downloading}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors
                  ${downloading ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : 'text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/20'}`}>
                {downloading ? <Spinner size="xs" /> : <Download className="w-3.5 h-3.5" />}{t('common.download')}
              </button>
            </div>

            {/* View mode */}
            {currentMode === 'view' && (
              <div className={`overflow-auto ${previewMaxClass}`}>
                {isBlobLoading ? (
                  <div className="flex items-center gap-2 p-4 text-gray-400 text-xs"><RefreshCw className="w-4 h-4 animate-spin" />{t('common.loading')}</div>
                ) : blobData ? (
                  blobData.success === false ? (
                    <div className="p-4 text-sm text-red-500 dark:text-red-400">{t('history.failedToLoad', { error: blobData.error })}</div>
                  ) : blobData.can_preview ? (
                    <div className={`font-mono text-xs overflow-auto ${codeMaxClass} border border-gray-100 dark:border-gray-800 rounded-b-lg bg-gray-50/50 dark:bg-gray-900/50`}>
                      {(blobData.content || '').split('\n').map((line, i) => (
                        <div key={i} className="flex hover:bg-sky-50 dark:hover:bg-sky-900/20 transition-colors leading-5">
                          <span className="w-12 shrink-0 text-right pr-3 select-none text-gray-300 dark:text-gray-600">{i + 1}</span>
                          <span className="flex-1 whitespace-pre-wrap break-all text-gray-700 dark:text-gray-300">{line}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="flex flex-col items-center justify-center py-10 px-4 text-center">
                      <div className="w-16 h-16 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center mb-4"><Binary className="w-8 h-8 text-gray-400 dark:text-gray-500" /></div>
                      <h4 className="text-sm font-bold text-gray-700 dark:text-gray-200 mb-1">{t('history.binaryFile')}</h4>
                      <p className="text-xs text-gray-400 dark:text-gray-500 mb-4 max-w-xs">{t('history.binaryDesc')}</p>
                      <div className="flex items-center gap-4 text-[11px] text-gray-500 dark:text-gray-400 mb-4">
                        <span>{t('history.fileLabel')} <strong className="text-gray-700 dark:text-gray-300">{blobData.name || file.name}</strong></span>
                        <span>{t('history.sizeLabel')} <strong className="text-gray-700 dark:text-gray-300">{formatFileSize(blobData.size)}</strong></span>
                      </div>
                      <button onClick={() => handleDownload(file)} className="flex items-center gap-2 px-5 py-2.5 bg-sigma-600 text-white rounded-xl hover:bg-sigma-700 transition-all text-sm font-medium shadow-sm">
                        <Download className="w-4 h-4" />{t('common.download')}
                      </button>
                    </div>
                  )
                ) : (
                  <div className="p-4 text-sm text-gray-400 italic">{t('history.loadingFile')}</div>
                )}
              </div>
            )}

            {/* Diff mode — side-by-side */}
            {currentMode === 'diff' && (
              <div className={`overflow-auto ${previewMaxClass}`}>
                {diffLoading[file.path] ? (
                  <div className="flex items-center gap-2 p-4 text-gray-400 text-xs"><RefreshCw className="w-4 h-4 animate-spin" />{t('history.loadingDiff')}</div>
                ) : diffs[file.path] && !diffs[file.path].error ? (
                  <DiffView lines={diffs[file.path]} leftLabel={t('history.previous')} rightLabel={t('history.current')} />
                ) : (
                  <div className="p-4 text-sm text-gray-400 italic">{t('history.noDiff')}</div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    )
  }

  if (!isOpen || !commit) return null
  const commitTitle = titleOverride || formatCommitMessage(commit.message, t)

  return (
    <>
    <div className={`fixed inset-0 z-[5000] flex items-center justify-center ${fullscreen ? 'p-0' : 'p-4'}`}>
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onClose} />
      <div className={`bg-white dark:bg-gray-900 w-full relative z-[5001] shadow-2xl flex flex-col overflow-hidden animate-in zoom-in duration-300 transition-all
        ${fullscreen ? 'h-full max-w-none rounded-none border-0' : 'max-w-5xl max-h-[85vh] rounded-2xl border border-gray-100 dark:border-gray-800'}`}>
        {/* Header */}
        <div className="p-5 border-b border-gray-100 dark:border-gray-800 flex justify-between items-start gap-3 bg-gray-50/50 dark:bg-gray-800/50">
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 break-words">{commitTitle}</h2>
            <div className="flex items-center gap-3 mt-2 text-xs text-gray-500 dark:text-gray-400 flex-wrap">
              <span className="flex items-center gap-1"><Clock className="w-3 h-3" />{getCommitTime(commit.date, t)}</span>
              <span className="font-mono text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-gray-800 px-2 py-0.5 rounded cursor-pointer hover:text-gray-600 dark:hover:text-gray-300"
                onClick={async () => { try { await copyToClipboard(commit.hash) } catch { /* best-effort */ } }}>
                {commit.short_hash}
              </span>
              {(commitTags || []).map(name => (
                <TagChip key={name} name={name} onDelete={onDeleteTag ? () => setPendingTagDelete(name) : undefined} />
              ))}
            </div>
          </div>
          <div className="flex items-center gap-1">
            {onAddTag && (
              <button onClick={onAddTag} className="p-2 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-xl transition-colors text-gray-400 dark:text-gray-500 hover:text-amber-600 dark:hover:text-amber-400" title={t('history.tag.add')}>
                <Tag className="w-5 h-5" />
              </button>
            )}
            <button onClick={() => setFullscreen(v => !v)} className="p-2 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-xl transition-colors text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400" title={fullscreen ? t('history.exitFullscreen') : t('history.fullscreen')}>
              {fullscreen ? <Minimize className="w-5 h-5" /> : <Maximize className="w-5 h-5" />}
            </button>
            <button onClick={async () => {
              if (downloading) return
              setDownloading(true)
              try { await onDownloadSnapshot() } finally { setDownloading(false) }
            }} disabled={downloading} className="p-2 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-xl transition-colors text-gray-400 dark:text-gray-500 hover:text-green-600 dark:hover:text-green-400 disabled:opacity-50 disabled:cursor-not-allowed" title={t('history.downloadVersion')}>
              {downloading ? <Spinner size="sm" /> : <Download className="w-5 h-5" />}
            </button>
            <button onClick={onClose} className="p-2 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-xl transition-colors text-gray-400 dark:text-gray-500"><X className="w-5 h-5" /></button>
          </div>
        </div>

        {/* Files list */}
        <div className="flex-1 overflow-auto">
          {filesLoading ? (
            <div className="p-8 text-center text-[11px] text-gray-400 dark:text-gray-500 italic">{t('history.loadingFiles')}</div>
          ) : changedFiles.length === 0 ? (
            <div className="p-8 text-center text-gray-400 dark:text-gray-500 text-sm italic">{t('history.noFilesCommit')}</div>
          ) : (
            <div className="divide-y divide-gray-50 dark:divide-gray-800">{changedFiles.map(renderFileEntry)}</div>
          )}
        </div>
      </div>
      </div>

      <ConfirmModal
        isOpen={Boolean(pendingTagDelete)}
        onClose={() => setPendingTagDelete(null)}
        onConfirm={() => onDeleteTag(pendingTagDelete)}
        title={t('history.tag.deleteTitle')}
        message={t('history.tag.deleteConfirm', { name: pendingTagDelete })}
        danger
      />
    </>
  )
}

// --- Main HistoryPanel ---
const PAGE_SIZE = 50

function HistoryPanel({ onBeforeCommit }) {
  const { t } = useTranslation()
  const currentProjectId = useStore(state => state.currentProject?.id)
  const [commits, setCommits] = useState([])
  const [loading, setLoading] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  const [showCommitModal, setShowCommitModal] = useState(false)
  const [selectedCommit, setSelectedCommit] = useState(null)
  const [snapshotDownloading, setSnapshotDownloading] = useState(null)
  const sentinelRef = useRef(null)
  const lastCommitHashRef = useRef(null)

  // Tag + toolbar state
  const [tags, setTags] = useState([])
  const [contextMenu, setContextMenu] = useState(null)
  const [tagInput, setTagInput] = useState(null)
  const [tagsOnly, setTagsOnly] = useState(false)
  const [selectingRange, setSelectingRange] = useState(false)
  const [rangeFirst, setRangeFirst] = useState(null)
  const [rangeView, setRangeView] = useState(null)
  const [committing, setCommitting] = useState(false)

  const loadLog = useCallback(async (reset = false) => {
    if (!currentProjectId) return
    setLoading(true)
    try {
      const before = reset ? null : lastCommitHashRef.current
      const data = await gitsAPI.log(currentProjectId, PAGE_SIZE, 0, before)
      const newCommits = data.commits || []
      setCommits(prev => {
        const next = reset ? newCommits : [...prev, ...newCommits]
        lastCommitHashRef.current = next[next.length - 1]?.hash || null
        return next
      })
      setHasMore(newCommits.length === PAGE_SIZE)
    } catch (err) {
      // Keep whatever is already loaded so a transient pagination failure
      // does not wipe the list; only a project switch / explicit reset clears it.
      console.error('Failed to load git log:', err)
      if (reset) {
        setCommits([])
        setHasMore(false)
      }
      toastError(err.message || t('history.toast.loadFailed'))
    }
    finally { setLoading(false) }
  }, [currentProjectId, t])

  const loadTags = useCallback(async () => {
    if (!currentProjectId) return
    try {
      const data = await gitsAPI.tags(currentProjectId)
      setTags(data.tags || [])
    } catch (err) {
      // Secondary data: rows just render without tag badges until next refresh.
      console.warn('Failed to load tags:', err)
    }
  }, [currentProjectId])

  useEffect(() => {
    lastCommitHashRef.current = null
    setCommits([])
    setHasMore(true)
    setTags([])
    setTagsOnly(false)
    setSelectingRange(false)
    setRangeFirst(null)
    loadLog(true)
    loadTags()
  }, [currentProjectId, loadLog, loadTags])

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    const observer = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting && hasMore && !loading) {
        loadLog(false)
      }
    }, { root: el.parentElement, rootMargin: '64px' })
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasMore, loading, loadLog])


  const handleViewCommit = (commitItem) => {
    setSelectedCommit(commitItem)
    setShowCommitModal(true)
  }

  // The two toolbar modes are mutually exclusive list states.
  const toggleRangeSelection = () => {
    if (!selectingRange) setTagsOnly(false)
    setSelectingRange(!selectingRange)
    setRangeFirst(null)
  }

  const toggleTagsOnly = () => {
    if (!tagsOnly) {
      setSelectingRange(false)
      setRangeFirst(null)
    }
    setTagsOnly(!tagsOnly)
  }

  // Context-menu entry into compare mode with the first endpoint pre-picked.
  const startRangeFrom = (commit) => {
    setTagsOnly(false)
    setSelectingRange(true)
    setRangeFirst(commit.hash)
  }

  // Clicking rows while comparing: first click fixes one endpoint, second
  // click fixes the other; order is derived from history (newer = "to").
  const handleRangePick = (commit) => {
    if (rangeFirst === commit.hash) {
      setRangeFirst(null)  // clicking the first pick again undoes it
      return
    }
    if (!rangeFirst) {
      setRangeFirst(commit.hash)
      return
    }
    const firstIdx = commits.findIndex(c => c.hash === rangeFirst)
    const secondIdx = commits.findIndex(c => c.hash === commit.hash)
    const from = secondIdx > firstIdx ? commit : commits[firstIdx]  // older
    const to = secondIdx > firstIdx ? commits[firstIdx] : commit    // newer
    setSelectingRange(false)
    setRangeFirst(null)
    setRangeView({ from: from.hash, to: to.hash })
  }

  const tagsByCommit = useMemo(() => {
    const map = {}
    for (const tag of tags) {
      ;(map[tag.commit] = map[tag.commit] || []).push(tag.name)
    }
    return map
  }, [tags])

  const visibleCommits = useMemo(
    () => (tagsOnly ? commits.filter(c => (tagsByCommit[c.hash] || []).length > 0) : commits),
    [tagsOnly, commits, tagsByCommit],
  )

  const selectedParentHash = useMemo(() => {
    if (!selectedCommit) return null
    const idx = commits.findIndex(c => c.hash === selectedCommit.hash)
    return idx >= 0 && idx + 1 < commits.length ? commits[idx + 1].hash : null
  }, [selectedCommit, commits])

  const rangeFirstShort = rangeFirst
    ? (commits.find(c => c.hash === rangeFirst)?.short_hash || rangeFirst.slice(0, 7))
    : ''

  const handleCreateTag = async (name, commitHash) => {
    // Errors propagate to the InputModal, which shows them inline.
    await gitsAPI.createTag(currentProjectId, { name, commit: commitHash })
    await loadTags()
    toastSuccess(t('history.toast.tagCreated'))
  }

  const handleDeleteTag = async (name) => {
    try {
      await gitsAPI.deleteTag(currentProjectId, name)
    } catch (err) {
      toastError(err.message || t('history.toast.tagDeleteFailed'))
      throw err  // keep the confirm modal open for retry
    }
    await loadTags()
    toastSuccess(t('history.toast.tagDeleted'))
  }

  const handleManualCommit = async () => {
    if (!currentProjectId || committing) return
    setCommitting(true)
    try {
      // Git only sees the working tree — flush the editor buffer to disk
      // first so unsaved edits are included in the snapshot.
      if (onBeforeCommit) await onBeforeCommit()
      const result = await gitsAPI.commitNow(currentProjectId)
      if (result?.success === false) toastInfo(t('history.toast.nothingToCommit'))
      else toastSuccess(t('history.toast.committed'))
      await Promise.all([loadLog(true), loadTags()])
    } catch (err) {
      toastError(err.message || t('history.toast.commitFailed'))
    } finally {
      setCommitting(false)
    }
  }

  const handleDownloadSnapshot = async (commitHash, commitShortHash, projectName) => {
    if (!currentProjectId || snapshotDownloading === commitHash) return
    setSnapshotDownloading(commitHash)
    try {
      const blob = await gitsAPI.downloadSnapshot(currentProjectId, commitHash)
      const filename = `${projectName || 'project'}_${commitShortHash}.zip`
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      toastError(err.message || t('history.toast.downloadFailed'))
    } finally {
      setSnapshotDownloading(null)
    }
  }

  const projectName = useStore(state => state.currentProject?.name)

  const rangeEndCommit = rangeView ? commits.find(c => c.hash === rangeView.to) : null
  const rangeStartCommit = rangeView ? commits.find(c => c.hash === rangeView.from) : null
  const rangeTitle = rangeView ? t('history.range.diffTitle', {
    from: rangeStartCommit?.short_hash || '',
    to: rangeEndCommit?.short_hash || '',
  }) : ''

  const toolbarButtonClass = 'p-1.5 rounded-lg text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed'

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center gap-0.5 px-3 py-2 border-b border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-800/50 flex-shrink-0">
        <button onClick={() => { loadLog(true); loadTags() }} disabled={loading} title={t('history.toolbar.refresh')} className={toolbarButtonClass}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
        <button onClick={handleManualCommit} disabled={committing} title={t('history.toolbar.commit')} className={toolbarButtonClass}>
          {committing ? <Spinner size="xs" /> : <GitCommitHorizontal className="w-3.5 h-3.5" />}
        </button>
        <button
          onClick={toggleRangeSelection}
          disabled={commits.length === 0}
          title={t('history.toolbar.range')}
          className={`${toolbarButtonClass} ${selectingRange ? 'text-sigma-600 dark:text-sigma-400 bg-sigma-600/10 dark:bg-sigma-600/20' : ''}`}
        >
          <GitCompare className="w-3.5 h-3.5" />
        </button>
        <div className="flex-1" />
        <button
          onClick={toggleTagsOnly}
          title={t('history.toolbar.tags')}
          className={`${toolbarButtonClass} ${tagsOnly ? 'text-amber-600 dark:text-amber-400 bg-amber-600/10 dark:bg-amber-900/30' : ''} relative`}
        >
          <Tags className="w-3.5 h-3.5" />
          {tags.length > 0 && (
            <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-0.5 flex items-center justify-center rounded-full bg-amber-500 text-white text-[8px] font-bold leading-none">{tags.length}</span>
          )}
        </button>
      </div>

      {/* Range-selection hint */}
      {selectingRange && (
        <div className="flex items-center gap-2 px-4 py-2 border-b border-gray-100 dark:border-gray-800 bg-sigma-600/5 dark:bg-sigma-600/10 text-xs text-sigma-700 dark:text-sigma-300 flex-shrink-0">
          <GitCompare className="w-3.5 h-3.5 shrink-0" />
          <span className="flex-1 truncate">
            {rangeFirst
              ? t('history.range.selectSecond', { hash: rangeFirstShort })
              : t('history.range.selectFirst')}
          </span>
          <button onClick={toggleRangeSelection} className="p-1 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors" title={t('common.cancel')}>
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* History list */}
      <div className="flex-1 overflow-auto divide-y divide-gray-50 dark:divide-gray-800">
        {visibleCommits.length === 0 ? (
          commits.length === 0 ? (
            loading ? (
              <div className="flex items-center justify-center gap-2 px-4 py-6 text-[11px] text-gray-400 dark:text-gray-500">
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />{t('common.loading')}
              </div>
            ) : (
              <div className="px-4 py-6 text-center text-[11px] text-gray-400 dark:text-gray-500 italic">{t('history.noHistory')}</div>
            )
          ) : (
            <div className="px-4 py-6 text-center text-[11px] text-gray-400 dark:text-gray-500 italic">{t('history.tag.noneTagged')}</div>
          )
        ) : (
          <>
          {visibleCommits.map((commit) => {
            const commitTitle = formatCommitMessage(commit.message, t)
            return (
            <div
              key={commit.hash}
              onClick={() => (selectingRange ? handleRangePick(commit) : handleViewCommit(commit))}
              onContextMenu={(e) => { e.preventDefault(); setContextMenu({ x: e.clientX, y: e.clientY, commit }) }}
              className={`w-full text-left px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors group/commit-row cursor-pointer
                ${rangeFirst === commit.hash ? 'bg-sigma-50 dark:bg-sigma-900/30 ring-1 ring-sigma-600/60' : ''}`}
            >
              <div className="flex items-start gap-2.5">
                <div className="mt-0.5">
                  <GitBranch className={`w-3.5 h-3.5 transition-colors ${rangeFirst === commit.hash ? 'text-sigma-600' : 'text-gray-300 dark:text-gray-600 group-hover/commit-row:text-sigma-600'}`} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-gray-700 dark:text-gray-300 font-medium truncate leading-snug">{commitTitle}</div>
                  <div className="flex items-center gap-1.5 mt-1 text-[10px] text-gray-400 dark:text-gray-500 flex-wrap">
                    <span className="flex items-center gap-1 shrink-0"><Clock className="w-2.5 h-2.5" />{getCommitTime(commit.date, t)}</span>
                    <span className={`font-mono text-[9px] px-1.5 py-0.5 rounded shrink-0 ${rangeFirst === commit.hash ? 'bg-sigma-600 text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500'}`}>{commit.short_hash}</span>
                    {(tagsByCommit[commit.hash] || []).map(name => <TagChip key={name} name={name} />)}
                  </div>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDownloadSnapshot(commit.hash, commit.short_hash, projectName) }}
                  disabled={snapshotDownloading === commit.hash}
                  className={`p-1 rounded transition-all shrink-0
                    ${snapshotDownloading === commit.hash ? 'opacity-100' : 'opacity-0 group-hover/commit-row:opacity-100 hover:bg-green-50 dark:hover:bg-green-900/20'}`}
                  title={t('history.downloadSnapshot')}
                >
                  {snapshotDownloading === commit.hash
                    ? <Spinner size="xs" className="text-sigma-600" />
                    : <Download className="w-3.5 h-3.5 text-green-600" />}
                </button>
              </div>
            </div>
            )
          })}
          <div ref={sentinelRef} className="px-4 py-3 text-center text-[11px] text-gray-400 dark:text-gray-500">
            {loading
              ? <span className="inline-flex items-center gap-1.5"><RefreshCw className="w-3 h-3 animate-spin" />{t('history.loadingMore')}</span>
              : !hasMore && <span className="italic">{t('history.noMore')}</span>}
          </div>
          </>
        )}
      </div>

      {/* Commit Detail Modal */}
      <CommitModal
        isOpen={showCommitModal}
        onClose={() => { setShowCommitModal(false); setSelectedCommit(null) }}
        commit={selectedCommit}
        parentHash={selectedParentHash}
        projectId={currentProjectId}
        commitTags={selectedCommit ? tagsByCommit[selectedCommit.hash] || [] : []}
        onAddTag={selectedCommit ? () => setTagInput({ commit: selectedCommit }) : undefined}
        onDeleteTag={handleDeleteTag}
        onDownloadSnapshot={async () => {
          await handleDownloadSnapshot(selectedCommit.hash, selectedCommit.short_hash, projectName)
        }}
      />

      {/* Range comparison: reuses the commit detail view with explicit endpoints */}
      <CommitModal
        isOpen={Boolean(rangeView)}
        onClose={() => setRangeView(null)}
        commit={rangeEndCommit}
        parentHash={rangeView?.from || null}
        projectId={currentProjectId}
        titleOverride={rangeTitle}
        commitTags={rangeEndCommit ? tagsByCommit[rangeEndCommit.hash] || [] : []}
        onAddTag={rangeEndCommit ? () => setTagInput({ commit: rangeEndCommit }) : undefined}
        onDeleteTag={handleDeleteTag}
        onDownloadSnapshot={async () => {
          if (rangeEndCommit) await handleDownloadSnapshot(rangeEndCommit.hash, rangeEndCommit.short_hash, projectName)
        }}
      />

      <InputModal
        isOpen={Boolean(tagInput)}
        onClose={() => setTagInput(null)}
        onConfirm={(name) => handleCreateTag(name, tagInput.commit.hash)}
        title={t('history.tag.add')}
        placeholder={t('history.tag.namePlaceholder')}
        icon={Tag}
      />

      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          options={[
            { label: t('history.context.view'), icon: <Eye className="w-4 h-4" />, action: () => handleViewCommit(contextMenu.commit) },
            { label: t('history.tag.add'), icon: <Tag className="w-4 h-4" />, action: () => setTagInput({ commit: contextMenu.commit }) },
            { label: t('history.toolbar.range'), icon: <GitCompare className="w-4 h-4" />, action: () => startRangeFrom(contextMenu.commit) },
          ]}
          onClose={() => setContextMenu(null)}
        />
      )}
    </div>
  )
}

// Real local time plus a relative suffix; the year is dropped for commits
// from the current calendar year (08-18 12:27:34 (1h ago)).
function getCommitTime(dateStr, t) {
  if (!dateStr || typeof dateStr !== 'string') return t('history.relativeUnknown')
  try {
    const date = new Date(dateStr)
    if (isNaN(date.getTime())) return dateStr
    const now = new Date()
    const diffSec = Math.floor((now - date) / 1000)
    const diffMin = Math.floor(diffSec / 60)
    const diffHour = Math.floor(diffMin / 60)
    const diffDay = Math.floor(diffHour / 24)

    let relative
    if (diffSec < 60) relative = t('time.justNow')
    else if (diffMin < 60) relative = t('history.relativeMin', { count: diffMin })
    else if (diffHour < 24) relative = t('history.relativeHour', { count: diffHour })
    else if (diffDay < 7) relative = t('history.relativeDay', { count: diffDay })
    else if (diffDay < 30) relative = t('history.relativeWeek', { count: Math.floor(diffDay / 7) })
    else if (diffDay < 365) relative = t('history.relativeMonth', { count: Math.floor(diffDay / 30) })
    else relative = t('history.relativeYear', { count: Math.floor(diffDay / 365) })

    const pad = (n) => String(n).padStart(2, '0')
    const mdhms = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    const datetime = date.getFullYear() === now.getFullYear() ? mdhms : `${date.getFullYear()}-${mdhms}`
    return t('history.timeWithRelative', { datetime, relative })
  } catch {
    return String(dateStr)
  }
}

export default HistoryPanel
