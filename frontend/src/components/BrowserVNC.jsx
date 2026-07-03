/**
 * BrowserVNC - Chrome browser via noVNC
 * Uses iframe to embed noVNC client connecting through backend WebSocket proxy.
 */
import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { Monitor, Loader2, AlertTriangle, RefreshCw } from 'lucide-react'
import { browserAPI } from '../api'

export default function BrowserVNC({ projectId }) {
  const { t } = useTranslation()
  const [status, setStatus] = useState('starting') // starting | connected | error
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!projectId) return
    startBrowser()
  }, [projectId])

  const POLL_INTERVAL_MS = 500
  const POLL_MAX_ATTEMPTS = 20

  const waitForRunning = async (projectId) => {
    for (let i = 0; i < POLL_MAX_ATTEMPTS; i++) {
      try {
        const data = await browserAPI.getStatus(projectId)
        if (data && data.status === 'running') return true
      } catch { /* ignore — may not be ready yet */ }
      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS))
    }
    return false
  }

  const startBrowser = async () => {
    setStatus('starting')
    setError(null)
    try {
      // Check if already running (shared across all projects)
      const statusData = await browserAPI.getStatus(projectId)
      if (statusData && statusData.status === 'running') {
        setStatus('connected')
        return
      }

      // Not running — start it, then poll until ready
      const startData = await browserAPI.start(projectId)
      if (startData && (startData.status === 'running' || startData.status === 'starting')) {
        const ready = await waitForRunning(projectId)
        if (ready) {
          setStatus('connected')
          return
        }
      }

      throw new Error('Browser failed to start')
    } catch (err) {
      console.error('BrowserVNC error:', err)
      setStatus('error')
      setError(err.message || t('browser.connectionFailed'))
    }
  }

  if (status === 'error') {
    return (
      <div className="h-full flex flex-col items-center justify-center bg-gray-50 dark:bg-gray-900 text-gray-500 dark:text-gray-400 gap-4">
        <AlertTriangle className="w-12 h-12 text-amber-400" />
        <div className="text-sm font-medium max-w-md text-center">{error || t('browser.connectionFailed')}</div>
        <button onClick={startBrowser} className="flex items-center gap-2 px-4 py-2 bg-sigma-600 text-white rounded-xl hover:bg-sigma-700 transition-all text-sm">
          <RefreshCw className="w-4 h-4" /> {t('common.retry')}
        </button>
      </div>
    )
  }

  if (status === 'starting') {
    return (
      <div className="h-full flex flex-col items-center justify-center bg-gray-50 dark:bg-gray-900 text-gray-400 dark:text-gray-500 gap-4">
        <Loader2 className="w-12 h-12 animate-spin text-sigma-600/30" />
        <div className="text-sm font-medium">{t('browser.starting')}</div>
      </div>
    )
  }

  // Build iframe URL - served by backend, WS proxy also through backend
  const iframeSrc = `/vnc.html?ws=/api/v1/browser/${projectId}/vnc`

  return (
    <div className="h-full flex flex-col bg-black overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-900 text-gray-400 text-xs flex-shrink-0">
        <Monitor className="w-3.5 h-3.5" />
        <span className="font-medium">{t('browser.chromeBrowser')}</span>
        <div className="w-2 h-2 rounded-full bg-green-400" />
      </div>
      <iframe
        src={iframeSrc}
        className="flex-1 border-0"
        style={{ minHeight: 0 }}
        title={t('browser.iframeTitle')}
      />
    </div>
  )
}
