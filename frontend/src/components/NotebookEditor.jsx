import { useState, useEffect, useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { notebooksAPI } from '../api'
import { ChevronLeft, RotateCcw, ExternalLink, AlertTriangle } from 'lucide-react'

export default function NotebookEditor({ projectId, filePath, onBack }) {
  const { t } = useTranslation()
  const [url, setUrl] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [syncing, setSyncing] = useState(false)
  const iframeRef = useRef(null)
  const reloadTimerRef = useRef(null)
  const overlayTimerRef = useRef(null)
  const syncHideTimerRef = useRef(null)
  const restoreScrollRef = useRef(null)

  const notebookVersion = useStore(s => s.notebookVersion)
  const prevVersionRef = useRef(notebookVersion)

  const notebookPath = `${projectId}/${String(filePath || '').replace(/\\/g, '/')}`

  const clearReloadTimers = useCallback(() => {
    if (reloadTimerRef.current) {
      clearTimeout(reloadTimerRef.current)
      reloadTimerRef.current = null
    }
    if (overlayTimerRef.current) {
      clearTimeout(overlayTimerRef.current)
      overlayTimerRef.current = null
    }
    if (syncHideTimerRef.current) {
      clearTimeout(syncHideTimerRef.current)
      syncHideTimerRef.current = null
    }
  }, [])

  const readScrollTop = useCallback((win) => {
    try {
      const doc = win.document
      const candidates = [
        doc.querySelector('.jp-NotebookPanel-notebook'),
        doc.querySelector('#notebook-container'),
        doc.scrollingElement,
        doc.documentElement,
        doc.body,
      ].filter(Boolean)
      const target = candidates.find(el => el.scrollTop > 0) || candidates[0]
      return target ? { selector: target.id ? `#${target.id}` : null, top: target.scrollTop || win.scrollY || 0 } : null
    } catch {
      return null
    }
  }, [])

  const restoreScrollTop = useCallback(() => {
    const frame = iframeRef.current
    const win = frame?.contentWindow
    const snapshot = restoreScrollRef.current
    if (!win || !snapshot) return
    try {
      const doc = win.document
      const target = snapshot.selector ? doc.querySelector(snapshot.selector) : null
      if (target) target.scrollTop = snapshot.top
      else win.scrollTo(0, snapshot.top)
    } catch {}
  }, [])

  const reloadNotebookFrame = useCallback((overlayDelay = 250) => {
    const frame = iframeRef.current
    const win = frame?.contentWindow
    if (!win) return false
    clearReloadTimers()
    restoreScrollRef.current = readScrollTop(win)
    overlayTimerRef.current = setTimeout(() => {
      setSyncing(true)
    }, overlayDelay)
    syncHideTimerRef.current = setTimeout(() => {
      setSyncing(false)
    }, 10000)

    try {
      win.location.replace(win.location.href)
      return true
    } catch {
      const currentSrc = frame.getAttribute('src')
      if (!currentSrc) return false
      frame.setAttribute('src', currentSrc)
      return true
    }
  }, [clearReloadTimers, readScrollTop])

  const getNbclassicNotebook = useCallback(() => {
    return iframeRef.current?.contentWindow?.Jupyter?.notebook || null
  }, [])

  const waitForNbclassicNotebook = useCallback(async (timeoutMs = 5000) => {
    const startedAt = Date.now()
    while (Date.now() - startedAt < timeoutMs) {
      const notebook = getNbclassicNotebook()
      if (typeof notebook?.load_notebook === 'function') return notebook
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    const notebook = getNbclassicNotebook()
    return typeof notebook?.load_notebook === 'function' ? notebook : null
  }, [getNbclassicNotebook])

  const syncNotebookFromDisk = useCallback(async (overlayDelay = 250) => {
    const frame = iframeRef.current
    const win = frame?.contentWindow
    if (!win) return false
    clearReloadTimers()
    restoreScrollRef.current = readScrollTop(win)
    overlayTimerRef.current = setTimeout(() => {
      setSyncing(true)
    }, overlayDelay)
    syncHideTimerRef.current = setTimeout(() => {
      setSyncing(false)
    }, 10000)

    let handedOffToFrameReload = false
    try {
      const notebook = await waitForNbclassicNotebook()
      if (!notebook) {
        console.warn('nbclassic notebook API is not ready; falling back to notebook frame reload')
        handedOffToFrameReload = true
        return reloadNotebookFrame(overlayDelay)
      }
      const loadResult = notebook.load_notebook(notebookPath)
      if (loadResult && typeof loadResult.then === 'function') {
        await loadResult
      }
      setTimeout(restoreScrollTop, 50)
      return true
    } catch (err) {
      console.warn('Failed to soft sync nbclassic notebook from disk; falling back to notebook frame reload:', err)
      handedOffToFrameReload = true
      return reloadNotebookFrame(overlayDelay)
    } finally {
      if (!handedOffToFrameReload) {
        clearReloadTimers()
        setSyncing(false)
      }
    }
  }, [
    clearReloadTimers,
    notebookPath,
    readScrollTop,
    reloadNotebookFrame,
    restoreScrollTop,
    waitForNbclassicNotebook,
  ])

  const fetchUrl = useCallback(async () => {
    if (!projectId || !filePath) return
    setLoading(true)
    setError(null)
    try {
      const response = await notebooksAPI.getUrl(projectId, filePath)
      setUrl(response.url)
    } catch (err) {
      console.error('Failed to get Jupyter URL:', err)
      setError(err.message || t('notebook.connectFailed'))
      setUrl(null)
    } finally {
      setLoading(false)
    }
  }, [projectId, filePath])

  useEffect(() => {
    fetchUrl()
  }, [fetchUrl])

  useEffect(() => {
    return () => clearReloadTimers()
  }, [clearReloadTimers])

  useEffect(() => {
    if (notebookVersion <= prevVersionRef.current || !url) return
    prevVersionRef.current = notebookVersion
    clearReloadTimers()
    reloadTimerRef.current = setTimeout(() => {
      void syncNotebookFromDisk(250)
    }, 200)
  }, [clearReloadTimers, notebookVersion, syncNotebookFromDisk, url])

  const handleRefresh = () => {
    if (url) {
      void syncNotebookFromDisk(0)
    } else {
      fetchUrl()
    }
  }

  const handleFrameLoad = () => {
    clearReloadTimers()
    setSyncing(false)
    setTimeout(restoreScrollTop, 50)
  }

  const openExternal = () => {
    if (url) window.open(url, '_blank')
  }

  return (
    <div className="flex flex-col h-full w-full bg-white dark:bg-gray-900 overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 flex-shrink-0">
        <div className="flex items-center gap-2">
          <button
            onClick={onBack}
            className="p-1.5 rounded-md hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-600 dark:text-gray-400 transition-colors"
            title={t('notebook.close')}
          >
            <ChevronLeft size={18} />
          </button>
          <div className="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-0.5" />
          <span className="text-xs font-medium text-gray-600 dark:text-gray-400 truncate max-w-[300px]" title={filePath}>
            {filePath?.split('/').pop() || t('notebook.fallbackName')}
          </span>
        </div>

        <div className="flex items-center gap-1">
          <button
            onClick={handleRefresh}
            className="p-1.5 rounded-md hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400 transition-colors"
            title={t('notebook.refresh')}
          >
            <RotateCcw size={16} />
          </button>
          <button
            onClick={openExternal}
            className="p-1.5 rounded-md hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400 transition-colors"
            title={t('notebook.openNewTab')}
          >
            <ExternalLink size={16} />
          </button>
        </div>
      </div>

      {/* Content area */}
      <div className="flex-1 relative bg-gray-100 dark:bg-gray-800">
        {loading ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="flex flex-col items-center gap-3">
              <div className="animate-spin rounded-full h-8 w-8 border-2 border-blue-600 border-t-transparent" />
              <p className="text-sm text-gray-500 dark:text-gray-400 font-medium">{t('notebook.starting')}</p>
              <p className="text-xs text-gray-400 dark:text-gray-500">{t('notebook.startingHint')}</p>
            </div>
          </div>
        ) : error ? (
          <div className="absolute inset-0 flex items-center justify-center p-8">
            <div className="flex flex-col items-center gap-4 max-w-sm text-center">
              <AlertTriangle className="w-10 h-10 text-amber-500" />
              <div>
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-1">{t('notebook.loadFailed')}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400">{error}</p>
              </div>
              <button
                onClick={fetchUrl}
                className="px-4 py-2 bg-sigma-600 text-white text-sm rounded-lg hover:bg-sigma-700 transition-colors"
              >
                {t('common.retry')}
              </button>
            </div>
          </div>
        ) : url ? (
          <>
            <iframe
              ref={iframeRef}
              key={url}
              src={url}
              onLoad={handleFrameLoad}
              className="w-full h-full border-none"
              title={t('notebook.iframeTitle')}
              allow="accelerometer; ambient-light-sensor; camera; encrypted-media; geolocation; gyroscope; hid; microphone; midi; payment; usb; vr; xr-spatial-tracking"
              sandbox="allow-forms allow-popups allow-presentation allow-same-origin allow-scripts allow-downloads"
            />
            {syncing ? (
              <div className="absolute inset-0 pointer-events-none flex items-start justify-end p-3 bg-white/10 dark:bg-gray-900/10">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-sigma-600 border-t-transparent bg-white/70 dark:bg-gray-900/70" />
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  )
}
