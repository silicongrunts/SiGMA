/**
 * FileHistoryModal — per-file commit history opened from the file tree
 * context menu. Lists the commits that touched the file (git log --follow),
 * newest first; expanding an entry lazily loads the file's diff in that
 * commit against the previous commit that touched the file.
 */
import { useState, useEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { Clock, ChevronRight, ChevronDown, File, RefreshCw, X, Maximize, Minimize, Download } from 'lucide-react'
import { gitsAPI, fetchBlob } from '../api'
import { toastError } from './Toast'
import { Spinner } from './ui'
import { formatCommitMessage, getCommitTime } from './HistoryPanel'
import DiffView from './DiffView'

export default function FileHistoryModal({ isOpen, onClose, projectId, path }) {
  const { t } = useTranslation()
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(new Set())
  const [diffs, setDiffs] = useState({})
  const [diffLoading, setDiffLoading] = useState({})
  const [fullscreen, setFullscreen] = useState(false)
  const [downloading, setDownloading] = useState(null)

  const loadHistory = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await gitsAPI.fileHistory(projectId, path)
      setHistory(data.history || [])
    } catch (err) {
      setError(err.message || String(err))
      setHistory([])
    } finally {
      setLoading(false)
    }
  }, [projectId, path])

  useEffect(() => {
    if (!isOpen || !path) return
    setHistory([])
    setExpanded(new Set())
    setDiffs({})
    setDiffLoading({})
    setFullscreen(false)
    loadHistory()
  }, [isOpen, path, loadHistory])

  const handleEntryClick = async (entry, parent) => {
    const next = new Set(expanded)
    if (next.has(entry.hash)) {
      next.delete(entry.hash)
      setExpanded(next)
      return
    }
    next.add(entry.hash)
    setExpanded(next)

    if (diffs[entry.hash] !== undefined || diffLoading[entry.hash]) return
    setDiffLoading(prev => ({ ...prev, [entry.hash]: true }))
    try {
      const data = await gitsAPI.diff(projectId, {
        path,
        commit: entry.hash,
        short_hash: entry.short_hash,
        parent_commit: parent || null,
      })
      setDiffs(prev => ({ ...prev, [entry.hash]: data.lines || [] }))
    } catch (err) {
      setDiffs(prev => ({ ...prev, [entry.hash]: { error: err.message || String(err) } }))
    } finally {
      setDiffLoading(prev => {
        const nextLoading = { ...prev }
        delete nextLoading[entry.hash]
        return nextLoading
      })
    }
  }

  const handleDownload = async (entry) => {
    if (downloading === entry.hash) return
    setDownloading(entry.hash)
    try {
      const blob = await fetchBlob(gitsAPI.blobDownload(projectId, path, entry.hash))
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = path.split('/').pop()
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      toastError(t('history.toast.downloadFailed') + ': ' + (err.message || String(err)))
    } finally {
      setDownloading(null)
    }
  }

  if (!isOpen || !path) return null

  const renderEntryBody = (entry) => {
    if (diffLoading[entry.hash]) {
      return (
        <div className="flex items-center gap-2 p-4 text-gray-400 text-xs">
          <RefreshCw className="w-4 h-4 animate-spin" />{t('common.loading')}
        </div>
      )
    }
    const diff = diffs[entry.hash]
    if (diff && !diff.error && diff.length > 0) {
      return (
        <DiffView
          lines={diff}
          leftLabel={t('history.previous')}
          rightLabel={t('history.current')}
          maxH={fullscreen ? 'max-h-[calc(100vh-250px)]' : 'max-h-80'}
        />
      )
    }
    return (
      <div className="p-4 text-sm text-gray-400 italic">
        {diff && diff.error ? diff.error : t('history.noDiff')}
      </div>
    )
  }

  return (
    <div className={`fixed inset-0 z-[5000] flex items-center justify-center ${fullscreen ? 'p-0' : 'p-4'}`}>
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onClose} />
      <div className={`bg-white dark:bg-gray-900 w-full relative z-[5001] shadow-2xl flex flex-col overflow-hidden animate-in zoom-in duration-300
        ${fullscreen ? 'h-full max-w-none rounded-none border-0' : 'max-w-5xl max-h-[85vh] rounded-2xl border border-gray-100 dark:border-gray-800'}`}>
        {/* Header */}
        <div className="p-5 border-b border-gray-100 dark:border-gray-800 flex justify-between items-center gap-3 bg-gray-50/50 dark:bg-gray-800/50">
          <h2 className="min-w-0 flex-1 text-lg font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <File className="w-5 h-5 text-gray-400 dark:text-gray-500 shrink-0" />
            <span className="break-all">{path}</span>
          </h2>
          <div className="flex items-center gap-1">
            <button onClick={() => setFullscreen(v => !v)} className="p-2 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-xl transition-colors text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400" title={fullscreen ? t('history.exitFullscreen') : t('history.fullscreen')}>
              {fullscreen ? <Minimize className="w-5 h-5" /> : <Maximize className="w-5 h-5" />}
            </button>
            <button onClick={onClose} className="p-2 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-xl transition-colors text-gray-400 dark:text-gray-500">
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Commit list */}
        <div className="flex-1 overflow-auto divide-y divide-gray-50 dark:divide-gray-800">
          {loading ? (
            <div className="flex items-center justify-center gap-2 px-4 py-6 text-[11px] text-gray-400 dark:text-gray-500">
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />{t('common.loading')}
            </div>
          ) : error ? (
            <div className="px-4 py-6 text-center text-[11px] text-red-500 dark:text-red-400">{error}</div>
          ) : history.length === 0 ? (
            <div className="px-4 py-6 text-center text-[11px] text-gray-400 dark:text-gray-500 italic">{t('history.file.empty')}</div>
          ) : (
            history.map((entry, idx) => {
              const isExp = expanded.has(entry.hash)
              return (
                <div key={entry.hash}>
                  <div
                    onClick={() => handleEntryClick(entry, history[idx + 1]?.hash || null)}
                    className="w-full text-left px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors flex items-start gap-2 cursor-pointer group/history-entry"
                  >
                    <span className="w-4 h-4 mt-0.5 shrink-0 flex items-center justify-center">
                      {isExp ? <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronRight className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm text-gray-700 dark:text-gray-300 font-medium truncate leading-snug">{formatCommitMessage(entry.message, t)}</div>
                      <div className="flex items-center gap-1.5 mt-1 text-[10px] text-gray-400 dark:text-gray-500">
                        <span className="flex items-center gap-1"><Clock className="w-2.5 h-2.5" />{getCommitTime(entry.date, t)}</span>
                        <span className="font-mono text-[9px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500">{entry.short_hash}</span>
                      </div>
                    </div>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDownload(entry) }}
                      disabled={downloading === entry.hash}
                      className={`p-1 rounded transition-all shrink-0 mt-0.5
                        ${downloading === entry.hash ? 'opacity-100' : 'opacity-0 group-hover/history-entry:opacity-100 hover:bg-green-50 dark:hover:bg-green-900/20'}`}
                      title={t('common.download')}
                    >
                      {downloading === entry.hash
                        ? <Spinner size="xs" className="text-sigma-600" />
                        : <Download className="w-3.5 h-3.5 text-green-600" />}
                    </button>
                  </div>
                  {isExp && (
                    <div className="border-t border-gray-100 dark:border-gray-800">{renderEntryBody(entry)}</div>
                  )}
                </div>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}
