/**
 * BrowserVNC - Chrome browser via noVNC
 * Uses iframe to embed noVNC client connecting through backend WebSocket proxy.
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { Monitor, Loader2, AlertTriangle, RefreshCw } from 'lucide-react'
import { browserAPI } from '../api'
import { useStore } from '../store/useStore'

const POLL_INTERVAL_MS = 500
const POLL_MAX_ATTEMPTS = 20
// vnc.html reports the real stream state via postMessage and owns its own
// 30s connect timeout; this parent fallback only fires if that message is
// somehow lost (blocked scripts, ancient iframe cache).
const VNC_LIVE_FALLBACK_MS = 40_000

export default function BrowserVNC({ projectId }) {
  const { t } = useTranslation()
  // Monotonic counter bumped after the user clears browser data. Each bump
  // re-runs startBrowser() below, which cycles status through 'starting'
  // (no iframe rendered) and back to 'connected' (iframe re-rendered) so the
  // embedded noVNC client opens a fresh WebSocket to the restarted
  // websockify. Without this, the iframe keeps a stale WS to the dead
  // websockify and shows a black screen until the user manually refreshes.
  const browserDataClearedAt = useStore(state => state.browserDataClearedAt)
  const [status, setStatus] = useState('starting') // starting | connected | error
  const [error, setError] = useState(null)
  // Real VNC stream state, reported by vnc.html via postMessage. 'pending'
  // shows a connecting overlay above the (black) iframe; vncLoading switches
  // that overlay's copy once the handshake is done and pixels are expected
  // next; 'live' means the first frame is actually on screen; 'failed' means
  // the stream died or timed out.
  const [vncLive, setVncLive] = useState('pending') // pending | live | failed
  const [vncError, setVncError] = useState(null)
  const [vncLoading, setVncLoading] = useState(false)
  // Bumped on every start attempt; stale async chains bail out so they cannot
  // call setStatus/setError after the component unmounts, projectId changes,
  // or the user presses Retry while an earlier attempt is still in flight.
  const activeTokenRef = useRef(0)
  // Bumped by Retry when the stream itself failed; re-keys the iframe so
  // vnc.html remounts and reconnects.
  const [reloadToken, setReloadToken] = useState(0)
  const iframeRef = useRef(null)
  const vncLiveRef = useRef(vncLive)
  vncLiveRef.current = vncLive

  const startBrowser = useCallback(async () => {
    const myToken = ++activeTokenRef.current
    const isStale = () => myToken !== activeTokenRef.current
    setStatus('starting')
    setError(null)
    try {
      // Check if already running (shared across all projects)
      const statusData = await browserAPI.getStatus(projectId)
      if (isStale()) return
      if (statusData && statusData.status === 'running') {
        setStatus('connected')
        return
      }

      // Not running — start it, then poll until ready
      const startData = await browserAPI.start(projectId)
      if (isStale()) return
      if (startData && (startData.status === 'running' || startData.status === 'starting')) {
        // Poll until ready; abort early if a newer attempt supersedes this one.
        for (let i = 0; i < POLL_MAX_ATTEMPTS; i++) {
          if (isStale()) return
          try {
            const data = await browserAPI.getStatus(projectId)
            if (isStale()) return
            if (data && data.status === 'running') {
              setStatus('connected')
              return
            }
          } catch { /* ignore — may not be ready yet */ }
          await new Promise(r => setTimeout(r, POLL_INTERVAL_MS))
        }
      }

      if (isStale()) return
      throw new Error('Browser failed to start')
    } catch (err) {
      if (isStale()) return
      console.error('BrowserVNC error:', err)
      setStatus('error')
      setError(err.message || t('browser.connectionFailed'))
    }
  }, [projectId, t])

  useEffect(() => {
    if (!projectId) return
    startBrowser()
    return () => { activeTokenRef.current++ }  // invalidate the in-flight chain on unmount/projectId change
  }, [projectId, startBrowser, browserDataClearedAt])

  // Stream state resets to 'pending' whenever a fresh iframe mounts (start
  // success, data clear, or retry) — the backend being 'running' says nothing
  // about the noVNC WebSocket itself.
  useEffect(() => {
    if (status === 'connected') {
      setVncLive('pending')
      setVncLoading(false)
      setVncError(null)
    }
  }, [status, browserDataClearedAt, reloadToken])

  // vnc.html owns the connection lifecycle (including the wait for the first
  // painted frame) and reports it via postMessage; this listener mirrors it
  // into the toolbar dot and overlays.
  useEffect(() => {
    const onMessage = (e) => {
      if (e.source !== iframeRef.current?.contentWindow) return
      if (e.origin !== window.location.origin) return
      const data = e.data
      if (!data || data.source !== 'sigma-vnc') return
      if (data.status === 'connected') {
        setVncLive('live')
        setVncLoading(false)
      }
      else if (data.status === 'connecting') {
        setVncLive('pending')
        setVncLoading(false)
      }
      else if (data.status === 'loading') {
        setVncLive('pending')
        setVncLoading(true)
      }
      else if (data.status === 'error') {
        setVncLive('failed')
        setVncError(data.message || t('browser.connectionFailed'))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [t])

  // Fallback: if the iframe never reports in, fail visibly instead of leaving
  // a black pane under a forever-spinning overlay.
  useEffect(() => {
    if (status !== 'connected') return
    const timer = setTimeout(() => {
      if (vncLiveRef.current === 'pending') {
        setVncLive('failed')
        setVncError(t('browser.vncTimeout'))
      }
    }, VNC_LIVE_FALLBACK_MS)
    return () => clearTimeout(timer)
  }, [status, browserDataClearedAt, reloadToken, t])

  const retryStream = useCallback(() => {
    setVncError(null)
    setReloadToken(v => v + 1)  // re-keys iframe → effect resets vncLive to 'pending'
  }, [])

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
        {/* Stream state, not backend state: green only when pixels flow */}
        <div className={`w-2 h-2 rounded-full ${vncLive === 'live' ? 'bg-green-400' : vncLive === 'failed' ? 'bg-red-500' : 'bg-amber-400 animate-pulse'}`} />
      </div>
      <div className="flex-1 min-h-0 relative">
        {vncLive === 'failed' ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center bg-gray-950 text-gray-400 gap-3">
            <AlertTriangle className="w-10 h-10 text-amber-400" />
            <div className="text-sm max-w-md text-center">{vncError || t('browser.connectionFailed')}</div>
            <button onClick={retryStream} className="flex items-center gap-2 px-4 py-2 bg-sigma-600 text-white rounded-xl hover:bg-sigma-700 transition-all text-sm">
              <RefreshCw className="w-4 h-4" /> {t('common.retry')}
            </button>
          </div>
        ) : (
          <>
            <iframe
              key={`${browserDataClearedAt}-${reloadToken}`}
              ref={iframeRef}
              src={iframeSrc}
              className="absolute inset-0 w-full h-full border-0"
              title={t('browser.iframeTitle')}
            />
            {vncLive === 'pending' && (
              <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-black text-gray-400">
                <Loader2 className="w-10 h-10 animate-spin text-sigma-600/50" />
                <div className="text-sm">{vncLoading ? t('browser.vncLoading') : t('browser.vncConnecting')}</div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
