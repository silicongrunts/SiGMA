import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  AlertTriangle, Check, ChevronDown, ChevronRight, Copy, ExternalLink, Loader2, LogIn, LogOut,
  Monitor, RefreshCw, ShieldCheck, Upload, Wallet, X, Zap,
} from 'lucide-react'
import { chatgptAPI } from '../api'
import i18n from '../i18n'
import { createSSEStreamParser } from '../utils/sse'
import { copyToClipboard } from '../utils/clipboard'
import { formatTimestamp } from '../utils/formatTimestamp'
import { ConfirmModal, ModalOverlay } from './Modal'
import { toastError, toastInfo, toastSuccess } from './Toast'

/**
 * ChatGPT subscription (Codex OAuth) management inside the system settings.
 * Three ways in — the embedded browser, the user's own browser, or a Codex
 * auth.json import — one running-login view that survives page refreshes,
 * and one way out (confirmed sign-out). Account status, rate-limit windows,
 * API credit balance, and rate-limit reset credits ("reset cards") round
 * it off.
 */

const LOGIN_EVENTS = new Set([
  'login_start', 'login_browser', 'login_listener', 'login_callback',
  'login_success', 'login_error', 'login_done',
])

// ---------------------------------------------------------------------------
// Shared subscription status
//
// Every consumer (the per-role status buttons and the panel's own status
// line) renders the same module-level snapshot, so a login, logout, or
// import in one role's panel instantly updates every other role's button.
// ---------------------------------------------------------------------------

let sharedStatus = null
const statusListeners = new Set()
let statusFetch = null

/**
 * Refresh the shared subscription snapshot (deduped while in flight) and
 * notify every subscriber. Resolves with the fresh snapshot (or null).
 */
export function fetchChatGPTStatus() {
  if (!statusFetch) {
    statusFetch = chatgptAPI.status()
      .then(s => { sharedStatus = s; return s })
      .catch(() => { sharedStatus = null; return null })
      .finally(() => {
        statusFetch = null
        for (const listener of statusListeners) listener(sharedStatus)
      })
  }
  return statusFetch
}

// While a login is in flight the snapshot is polled: the login SSE stream
// dies with a page refresh and cannot be re-attached to a running flow, so
// the poll is what lets the restored running-login view learn the outcome.
// fetchChatGPTStatus dedupes overlapping ticks from mounted consumers.
const PENDING_STATUS_POLL_MS = 4000

/** Subscribe a component to the shared subscription snapshot. */
export function useChatGPTStatus() {
  const [status, setStatus] = useState(sharedStatus)
  useEffect(() => {
    statusListeners.add(setStatus)
    if (sharedStatus === null) fetchChatGPTStatus()
    return () => { statusListeners.delete(setStatus) }
  }, [])
  useEffect(() => {
    if (!status?.login_pending) return
    const timer = setInterval(fetchChatGPTStatus, PENDING_STATUS_POLL_MS)
    return () => clearInterval(timer)
  }, [status?.login_pending])
  return status
}

/** Human-readable "resets in …" suffix for a rate-limit window. */
export function formatChatGPTWindowReset(seconds, t) {
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  const h = Math.floor(seconds / 3600)
  const m = Math.ceil((seconds % 3600) / 60)
  if (h > 0) return t('chatgpt.usage.resetInHm', { h, m })
  return t('chatgpt.usage.resetInM', { m })
}

function UsageBar({ label, rateWindow, t }) {
  if (!rateWindow) return null
  const used = Number(rateWindow.used_percent) || 0
  const reset = formatChatGPTWindowReset(rateWindow.reset_after_seconds, t)
  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2">
      <div className="flex items-center justify-between text-xs font-bold text-gray-600 dark:text-gray-300 mb-1.5">
        <span>{label}</span>
        <span className="text-gray-400 dark:text-gray-500">{used.toFixed(0)}%</span>
      </div>
      <div className="h-2 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${used >= 100 ? 'bg-red-500' : used >= 80 ? 'bg-amber-500' : 'bg-sigma-600'}`}
          style={{ width: `${Math.min(Math.max(used, 0), 100)}%` }}
        />
      </div>
      {reset && (
        <div className="text-[11px] text-gray-400 dark:text-gray-500 mt-1">
          {t('chatgpt.usage.resets')} {reset}
        </div>
      )}
    </div>
  )
}

/**
 * Shared quota/credits data owner: the Codex usage payload (rate-limit
 * windows, API credit balance) plus the reset-credit inventory, fetched
 * together so every surface shows the same numbers. Auto-loads pass
 * silent=true — failures stay quiet where a placeholder already invites a
 * manual retry; only explicit refreshes raise a toast.
 */
export function useChatGPTQuota() {
  const [usage, setUsage] = useState(null)
  const [resetCredits, setResetCredits] = useState(null)
  const [loading, setLoading] = useState(false)
  const load = useCallback(async (silent) => {
    setLoading(true)
    const [usageRes, creditsRes] = await Promise.allSettled([
      chatgptAPI.usage(),
      chatgptAPI.resetCredits(),
    ])
    setUsage(usageRes.status === 'fulfilled' ? usageRes.value : null)
    setResetCredits(creditsRes.status === 'fulfilled' ? creditsRes.value : null)
    setLoading(false)
    const failure = [usageRes, creditsRes].find(r => r.status === 'rejected')
    if (failure && !silent) {
      toastError(failure.reason?.message || i18n.t('chatgpt.toast.usageFailed'))
    }
  }, [])
  return { usage, resetCredits, loading, load }
}

/**
 * API credit balance row plus the rate-limit reset-credit inventory with
 * one-click redemption (double-confirmed; refreshes through onRefresh on
 * success). Pure display + action over data owned by useChatGPTQuota, so the
 * subscription panel and the chat quota popup render identically.
 */
export function ChatGPTCreditsReset({ usage, resetCredits, loading, onRefresh }) {
  const { t } = useTranslation()
  const [resetBusy, setResetBusy] = useState(false)
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false)
  // Reset-credit rows are capped by the backend; available_count is the
  // authoritative total (same reading as the official client).
  const availableCards = (resetCredits?.credits || []).filter(
    c => c.status === 'available' && c.reset_type === 'codex_rate_limits',
  )
  const availableCount = resetCredits?.available_count ?? availableCards.length

  const doResetCredit = async () => {
    setResetBusy(true)
    try {
      const outcome = await chatgptAPI.consumeResetCredit()
      if (outcome?.code === 'reset') {
        toastSuccess(t('chatgpt.reset.success', { count: outcome.windows_reset ?? 1 }))
      } else if (outcome?.code === 'nothing_to_reset') {
        toastInfo(t('chatgpt.reset.nothingToReset'))
      } else {
        toastError(t('chatgpt.reset.rejected', { code: outcome?.code || 'unknown' }))
      }
      onRefresh?.()
    } catch (err) {
      toastError(err.message || t('chatgpt.reset.failed'))
      throw err // keep the confirmation modal open — the reset did not happen
    } finally {
      setResetBusy(false)
    }
  }

  return (
    <>
      {usage?.credits && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-xs">
          <Wallet className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />
          <span className="font-bold text-gray-600 dark:text-gray-300">{t('chatgpt.usage.creditBalance')}</span>
          <span className="ml-auto font-black text-gray-900 dark:text-gray-100">
            {usage.credits.unlimited
              ? t('chatgpt.usage.creditUnlimited')
              : `$${(Number(usage.credits.balance) || 0).toFixed(2)}`}
          </span>
        </div>
      )}

      <div className="mt-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <Zap className="w-3.5 h-3.5 text-amber-500 flex-shrink-0" />
          <span className="text-xs font-bold text-gray-600 dark:text-gray-300">{t('chatgpt.reset.title')}</span>
          {loading && !resetCredits ? (
            <Loader2 className="w-3 h-3 animate-spin text-gray-400" />
          ) : resetCredits ? (
            <span className="text-xs font-black text-gray-900 dark:text-gray-100">
              {t('chatgpt.reset.count', { count: availableCount })}
            </span>
          ) : (
            <span className="text-xs text-gray-400 dark:text-gray-500">—</span>
          )}
          <button
            type="button"
            onClick={() => setResetConfirmOpen(true)}
            disabled={loading || resetBusy || !resetCredits || availableCount < 1}
            className="ml-auto flex items-center gap-1 px-2.5 py-1 text-[11px] font-bold text-white bg-amber-500 hover:bg-amber-600 disabled:opacity-40 rounded-md transition-colors"
          >
            {resetBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
            {t('chatgpt.reset.action')}
          </button>
        </div>
        {availableCards.length > 0 && (
          <div className="mt-2 flex flex-col gap-1.5">
            {availableCards.map(card => (
              <div
                key={card.id}
                className="flex items-start gap-2 rounded-md bg-gray-50 dark:bg-gray-900 px-2 py-1.5"
              >
                <div className="min-w-0">
                  <div className="text-[11px] font-bold text-gray-700 dark:text-gray-200 truncate">
                    {card.title || t('chatgpt.reset.cardFallback')}
                  </div>
                  {card.description && (
                    <div className="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                      {card.description}
                    </div>
                  )}
                </div>
                {card.expires_at && (
                  <span className="ml-auto flex-shrink-0 text-[10px] text-gray-400 dark:text-gray-500">
                    {t('chatgpt.reset.expires', {
                      date: formatTimestamp(card.expires_at).slice(0, 10),
                    })}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <ConfirmModal
        isOpen={resetConfirmOpen}
        onClose={() => { if (!resetBusy) setResetConfirmOpen(false) }}
        onConfirm={doResetCredit}
        title={t('chatgpt.reset.confirmTitle')}
        message={t('chatgpt.reset.confirmMessage')}
        danger
      />
    </>
  )
}

/** Plan tier pill ("plus" → "Plus"); hidden when the claim is absent. */
function PlanBadge({ plan }) {
  if (!plan) return null
  return (
    <span className="flex-shrink-0 px-1.5 py-0.5 rounded bg-sigma-50 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-400 text-[10px] font-black capitalize">
      {plan}
    </span>
  )
}

export default function ChatGPTSubscriptionSection() {
  const { t } = useTranslation()
  const status = useChatGPTStatus()
  const [busy, setBusy] = useState(false)
  const [confirmLogout, setConfirmLogout] = useState(false)
  // The running login. loginMode records which entry started it; once null
  // (fresh mount), the mode comes from the status snapshot, so the view
  // survives page refreshes — the backend flow does.
  const [loginRunning, setLoginRunning] = useState(false)
  const [loginMode, setLoginMode] = useState(null) // 'embedded' | 'manual'
  const [authUrl, setAuthUrl] = useState('')
  const [embeddedFailed, setEmbeddedFailed] = useState(false)
  const [listenerFailed, setListenerFailed] = useState(false)
  const [manualOpen, setManualOpen] = useState(false)
  const [vncVisible, setVncVisible] = useState(false)
  const [pasteUrl, setPasteUrl] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [importText, setImportText] = useState('')
  const loginAbortRef = useRef(null)
  // Auto-load guards: fetch once per panel mount while signed in (and again
  // after a fresh login), without re-fetching on every unrelated snapshot
  // update. Reset while signed out so the next login triggers a fresh load.
  const autoUsageRef = useRef(false)

  useEffect(() => {
    // Fresh data on every panel open — the shared snapshot may be stale.
    fetchChatGPTStatus()
    return () => {
      // Closing the settings modal never aborts the backend login flow — it
      // is service-owned precisely so it survives refreshes and disconnects.
      loginAbortRef.current?.abort()
      loginAbortRef.current = null
    }
  }, [])

  const handleLoginEvent = useCallback((type, data) => {
    if (!LOGIN_EVENTS.has(type)) return
    if (type === 'login_start') {
      if (data.authorize_url) setAuthUrl(data.authorize_url)
    } else if (type === 'login_browser') {
      if (data.opened === true) {
        setVncVisible(true)
      } else {
        // No embedded window — the own-browser path is the way through.
        setEmbeddedFailed(true)
        setManualOpen(true)
      }
    } else if (type === 'login_listener' && data.listening === false) {
      // The loopback callback port is taken: automatic completion is
      // impossible, so surface the own-browser path right away.
      setListenerFailed(true)
      setManualOpen(true)
    } else if (type === 'login_success') {
      toastSuccess(t('chatgpt.login.success', { email: data.email || '' }))
    } else if (type === 'login_error') {
      // A user-initiated cancel is already reflected by the view.
      if (data.error_type !== 'cancelled') {
        toastError(data.message || t('chatgpt.toast.loginFailed'))
      }
    } else if (type === 'login_done') {
      setLoginRunning(false)
      setLoginMode(null)
      setAuthUrl('')
      setEmbeddedFailed(false)
      setListenerFailed(false)
      setManualOpen(false)
      setVncVisible(false)
      loginAbortRef.current = null
      fetchChatGPTStatus()
    }
  }, [t])

  const startLogin = async (withBrowser) => {
    if (loginAbortRef.current || status?.login_pending) return
    const abort = new AbortController()
    loginAbortRef.current = abort
    setLoginMode(withBrowser ? 'embedded' : 'manual')
    setLoginRunning(true)
    setAuthUrl('')
    setEmbeddedFailed(false)
    setListenerFailed(false)
    setManualOpen(!withBrowser)
    setVncVisible(false)
    try {
      const stream = await chatgptAPI.login({ open_browser: withBrowser }, abort.signal)
      const reader = stream.getReader()
      const decoder = new TextDecoder()
      const parser = createSSEStreamParser({
        onEvent: handleLoginEvent,
        onError: () => {
          // The backend flow outlives this stream; the status snapshot keeps
          // the running-login view alive without it.
          setLoginRunning(false)
          loginAbortRef.current = null
          fetchChatGPTStatus()
        },
      })
      await parser.start(reader, decoder, abort.signal)
    } catch (err) {
      if (abort.signal.aborted) return
      toastError(err.message || t('chatgpt.toast.loginFailed'))
      setLoginRunning(false)
      loginAbortRef.current = null
      fetchChatGPTStatus()
    }
  }

  const { usage, resetCredits, loading: usageLoading, load: loadQuota } = useChatGPTQuota()

  // The running-login view is driven by the backend snapshot (which survives
  // page refreshes), with the SSE stream only accelerating its updates.
  const pending = loginRunning || !!status?.login_pending
  const mode = loginMode || (status?.login_open_browser === false ? 'manual' : 'embedded')
  const liveAuthUrl = authUrl || (pending ? status?.login_authorize_url || '' : '')
  // expired counts as signed in: the refresh token renews it automatically.
  const loggedIn = !!status && (status.state === 'active' || status.state === 'expired')

  const cancelLogin = async () => {
    try {
      await chatgptAPI.cancelLogin()
    } catch { /* the flow may have just finished on its own */ }
    fetchChatGPTStatus()
  }

  const copyAuthUrl = async () => {
    try {
      await copyToClipboard(liveAuthUrl)
      toastSuccess(t('chatgpt.toast.linkCopied'))
    } catch {
      toastError(t('chatgpt.toast.linkCopiedFailed'))
    }
  }

  const onImportFile = (e) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-picking the same file after an edit
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => setImportText(String(reader.result || ''))
    reader.onerror = () => toastError(t('chatgpt.import.readFailed'))
    reader.readAsText(file)
  }

  const submitPaste = async () => {
    if (!pasteUrl.trim()) return
    setBusy(true)
    try {
      await chatgptAPI.deliverCallback(pasteUrl.trim())
      setPasteUrl('')
      await fetchChatGPTStatus()
    } catch (err) {
      toastError(err.message || t('chatgpt.toast.pasteFailed'))
    } finally {
      setBusy(false)
    }
  }

  const submitImport = async () => {
    if (!importText.trim()) return
    setBusy(true)
    try {
      await chatgptAPI.importAuth(importText.trim())
      setImportText('')
      setShowImport(false)
      await fetchChatGPTStatus()
    } catch (err) {
      toastError(err.message || t('chatgpt.toast.importFailed'))
    } finally {
      setBusy(false)
    }
  }

  const doLogout = async () => {
    setBusy(true)
    try {
      await chatgptAPI.logout()
      setConfirmLogout(false)
      await fetchChatGPTStatus()
    } catch (err) {
      toastError(err.message || t('chatgpt.toast.logoutFailed'))
    } finally {
      setBusy(false)
    }
  }

  const refreshUsage = () => loadQuota(false)

  useEffect(() => {
    if (loggedIn) {
      if (!autoUsageRef.current) {
        autoUsageRef.current = true
        loadQuota(true)
      }
    } else {
      autoUsageRef.current = false
    }
  }, [loggedIn, loadQuota])

  // Sign-in guidance for the own-browser path: shared by the manual wizard
  // and the "continue in my browser" escape hatch of the embedded flow.
  const manualContent = (
    <>
      {liveAuthUrl && (
        <div className="flex flex-wrap items-center gap-2">
          <a
            href={liveAuthUrl}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-bold text-white bg-sigma-600 hover:bg-sigma-700 transition-colors"
          >
            <ExternalLink className="w-3.5 h-3.5" />
            {t('chatgpt.login.openAuthorize')}
          </a>
          <button
            type="button"
            onClick={copyAuthUrl}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-bold text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <Copy className="w-3.5 h-3.5" />
            {t('chatgpt.login.copyLink')}
          </button>
        </div>
      )}
      <textarea
        value={pasteUrl}
        onChange={e => setPasteUrl(e.target.value)}
        rows={2}
        placeholder="http://localhost:1455/auth/callback?code=…"
        className="w-full resize-none rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-xs font-mono outline-none focus:border-sigma-600 focus:ring-4 focus:ring-sigma-600/10"
      />
      <div>
        <button
          type="button"
          onClick={submitPaste}
          disabled={busy || !pasteUrl.trim()}
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-bold text-white bg-sigma-600 hover:bg-sigma-700 disabled:opacity-50 rounded-lg transition-colors"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
          {t('chatgpt.action.pasteSubmit')}
        </button>
      </div>
    </>
  )

  const stateTone = {
    needs_relogin: 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400',
    logged_out: 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400',
  }
  const statusLabelKey = {
    needs_relogin: 'chatgpt.status.needsRelogin',
    logged_out: 'chatgpt.status.loggedOut',
  }
  const rateLimit = usage?.rate_limit || {}

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm">
      <div className="flex items-start gap-3 min-w-0 mb-3">
        <div className="w-9 h-9 rounded-lg bg-sigma-50 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-400 flex items-center justify-center flex-shrink-0">
          <ShieldCheck className="w-4 h-4" />
        </div>
        <div className="min-w-0">
          <h4 className="text-sm font-black text-gray-900 dark:text-gray-100">{t('chatgpt.title')}</h4>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 leading-5">{t('chatgpt.description')}</p>
        </div>
      </div>

      {pending ? (
        // Every entry path converges here while a login is in flight; the
        // view is restored from the status snapshot after a page refresh.
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2.5 flex flex-col gap-2.5">
          <div className="flex items-center gap-2 text-sm font-bold text-gray-700 dark:text-gray-200">
            <Loader2 className="w-4 h-4 animate-spin text-sigma-600" />
            {t('chatgpt.login.waitingBrowser')}
          </div>
          {mode === 'embedded' && !embeddedFailed && (
            <button
              type="button"
              onClick={() => setVncVisible(true)}
              className="self-start flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-bold text-sigma-700 dark:text-sigma-400 bg-sigma-50 dark:bg-sigma-600/20 hover:bg-sigma-100 dark:hover:bg-sigma-600/30 rounded-md transition-colors"
            >
              <Monitor className="w-3.5 h-3.5" />
              {t('chatgpt.login.reopenWindow')}
            </button>
          )}
          {mode === 'manual' ? (
            <div className="flex flex-col gap-2">
              <div className="text-xs text-gray-600 dark:text-gray-300 leading-5">
                {t('chatgpt.login.manualGuide')}
              </div>
              {manualContent}
            </div>
          ) : (
            <div>
              <button
                type="button"
                onClick={() => setManualOpen(o => !o)}
                className="flex items-center gap-1 text-xs font-bold text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
              >
                {manualOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                {t('chatgpt.login.manualTitle')}
              </button>
              {manualOpen && (
                <div className="mt-2 flex flex-col gap-2">
                  {(embeddedFailed || listenerFailed) && (
                    <div className="text-xs text-amber-700 dark:text-amber-400 leading-5">
                      {t(embeddedFailed ? 'chatgpt.login.manualHint' : 'chatgpt.login.pasteOnly')}
                    </div>
                  )}
                  {manualContent}
                </div>
              )}
            </div>
          )}
          <button
            type="button"
            onClick={cancelLogin}
            className="self-start flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-bold text-gray-700 dark:text-gray-200 bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 rounded-md transition-colors"
          >
            <X className="w-3.5 h-3.5" />
            {t('chatgpt.action.cancelLogin')}
          </button>
        </div>
      ) : loggedIn ? (
        <>
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-bold bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-400">
            <ShieldCheck className="w-4 h-4 flex-shrink-0" />
            <span className="min-w-0 truncate">
              {t('chatgpt.status.active', { email: status.email || '' })}
            </span>
            <PlanBadge plan={status.plan_type} />
            <button
              type="button"
              onClick={() => (confirmLogout ? doLogout() : setConfirmLogout(true))}
              onBlur={() => setConfirmLogout(false)}
              disabled={busy}
              className={`ml-auto flex-shrink-0 flex items-center gap-1 px-2 py-1 text-xs font-bold rounded-md transition-colors disabled:opacity-50 ${
                confirmLogout
                  ? 'text-white bg-red-600 hover:bg-red-700'
                  : 'hover:bg-green-100 dark:hover:bg-green-900/40'
              }`}
            >
              <LogOut className="w-3.5 h-3.5" />
              {confirmLogout ? t('chatgpt.action.logoutConfirm') : t('chatgpt.action.logout')}
            </button>
          </div>

          <div className="mt-3">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-bold text-gray-500 dark:text-gray-400">{t('chatgpt.usage.title')}</span>
              <button
                type="button"
                onClick={refreshUsage}
                disabled={usageLoading}
                className="flex items-center gap-1 text-xs font-bold text-sigma-600 dark:text-sigma-400 hover:text-sigma-700 disabled:opacity-50"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${usageLoading ? 'animate-spin' : ''}`} />
                {t('common.refresh')}
              </button>
            </div>
            {usage ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                <UsageBar label={t('chatgpt.usage.fiveHourWindow')} rateWindow={rateLimit.primary_window} t={t} />
                <UsageBar label={t('chatgpt.usage.weeklyWindow')} rateWindow={rateLimit.secondary_window} t={t} />
              </div>
            ) : usageLoading ? (
              <div className="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                {t('common.loading')}
              </div>
            ) : (
              <div className="text-xs text-gray-400 dark:text-gray-500">{t('chatgpt.usage.notLoaded')}</div>
            )}

            <ChatGPTCreditsReset
              usage={usage}
              resetCredits={resetCredits}
              loading={usageLoading}
              onRefresh={() => loadQuota(true)}
            />
          </div>
        </>
      ) : (
        <>
          <div className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-bold ${stateTone[status?.state] || stateTone.logged_out}`}>
            <AlertTriangle className="w-4 h-4 flex-shrink-0" />
            <span className="min-w-0 truncate">
              {t(statusLabelKey[status?.state] || 'chatgpt.status.loggedOut')}
            </span>
          </div>

          <div className="flex flex-col items-stretch gap-2 mt-3">
            <button
              type="button"
              onClick={() => startLogin(true)}
              disabled={busy}
              className="flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-bold text-white bg-sigma-600 hover:bg-sigma-700 disabled:opacity-50 rounded-lg transition-colors"
            >
              <LogIn className="w-4 h-4" />
              {t('chatgpt.action.login')}
            </button>
            <button
              type="button"
              onClick={() => startLogin(false)}
              disabled={busy}
              className="flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-bold text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 rounded-lg transition-colors"
            >
              <Monitor className="w-4 h-4" />
              {t('chatgpt.action.loginOwnBrowser')}
            </button>
            <p className="text-[11px] text-gray-400 dark:text-gray-500 leading-4 -mt-0.5">
              {t('chatgpt.login.ownBrowserHint')}
            </p>
            <button
              type="button"
              onClick={() => setShowImport(open => !open)}
              disabled={busy}
              className="flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-bold text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 rounded-lg transition-colors"
            >
              <Upload className="w-4 h-4" />
              {t('chatgpt.action.import')}
              <ChevronDown className={`w-3.5 h-3.5 transition-transform ${showImport ? 'rotate-180' : ''}`} />
            </button>

            {showImport && (
              <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 flex flex-col gap-2">
                <div className="text-xs text-gray-600 dark:text-gray-300 leading-5">
                  {t('chatgpt.import.hint')}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-bold text-gray-600 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 hover:border-sigma-600 cursor-pointer transition-colors">
                    <Upload className="w-3.5 h-3.5" />
                    {t('chatgpt.import.pickFile')}
                    <input type="file" accept=".json,application/json" className="hidden" onChange={onImportFile} />
                  </label>
                  <span className="text-[11px] text-gray-400 dark:text-gray-500">
                    {t('chatgpt.import.pathHint')}
                  </span>
                </div>
                <textarea
                  value={importText}
                  onChange={e => setImportText(e.target.value)}
                  rows={5}
                  placeholder='{"tokens": {"access_token": "…", "refresh_token": "…"}}'
                  className="w-full resize-y rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-xs font-mono outline-none focus:border-sigma-600 focus:ring-4 focus:ring-sigma-600/10"
                />
                <div>
                  <button
                    type="button"
                    onClick={submitImport}
                    disabled={busy || !importText.trim()}
                    className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-bold text-white bg-sigma-600 hover:bg-sigma-700 disabled:opacity-50 rounded-lg transition-colors"
                  >
                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                    {t('chatgpt.action.importSubmit')}
                  </button>
                </div>
                <div className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400 leading-4">
                  <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-0.5" />
                  {t('chatgpt.import.rotationWarning')}
                </div>
              </div>
            )}
          </div>
        </>
      )}

      {vncVisible && (
        <div className="fixed inset-0 z-[6000] flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-gray-900/60 backdrop-blur-sm animate-in fade-in duration-300" />
          <div className="relative z-[6001] w-full max-w-4xl h-[80vh] bg-gray-900 rounded-2xl shadow-[0_20px_70px_rgba(0,0,0,0.5)] border border-gray-700 overflow-hidden flex flex-col animate-in zoom-in duration-300">
            <div className="flex items-center gap-2 px-4 py-2.5 bg-gray-800 border-b border-gray-700 text-gray-300 text-xs flex-shrink-0">
              <Monitor className="w-4 h-4 text-sigma-400 flex-shrink-0" />
              <span className="font-bold flex-shrink-0">{t('chatgpt.login.browserTitle')}</span>
              <span className="min-w-0 truncate text-gray-500">{t('chatgpt.login.browserHint')}</span>
              <button
                type="button"
                onClick={() => setVncVisible(false)}
                className="ml-auto flex-shrink-0 p-1 rounded hover:bg-gray-700 text-gray-400 hover:text-gray-200 transition-colors"
                title={t('common.close')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <iframe
              key="chatgpt-login-vnc"
              src="/vnc.html?ws=/api/v1/chatgpt/login/vnc"
              className="flex-1 border-0 w-full bg-black"
              style={{ minHeight: 0 }}
              title={t('chatgpt.login.browserTitle')}
            />
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Standalone quota popup opened by clicking the quota strip in the chat
 * input's puck tooltip: credit balance + reset cards + redemption — exactly
 * the same shared widgets as the subscription panel, with its own data load
 * per open.
 */
export function ChatGPTQuotaModal({ isOpen, onClose }) {
  const { t } = useTranslation()
  const { usage, resetCredits, loading, load } = useChatGPTQuota()

  useEffect(() => {
    if (isOpen) load(true)
  }, [isOpen, load])

  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="p-5">
        <div className="flex items-center gap-2 mb-4">
          <Zap className="w-4 h-4 text-amber-500 flex-shrink-0" />
          <h3 className="text-sm font-black text-gray-900 dark:text-gray-100">
            {t('chatgpt.quota.detailsTitle')}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto p-1.5 rounded-lg text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
            title={t('common.close')}
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        {loading && !usage ? (
          <div className="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500 py-2">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            {t('common.loading')}
          </div>
        ) : !usage ? (
          <div className="flex items-center justify-between gap-2 py-2">
            <span className="text-xs text-gray-400 dark:text-gray-500">{t('chatgpt.usage.notLoaded')}</span>
            <button
              type="button"
              onClick={() => load(false)}
              className="flex items-center gap-1 text-xs font-bold text-sigma-600 dark:text-sigma-400 hover:text-sigma-700"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              {t('common.refresh')}
            </button>
          </div>
        ) : (
          <ChatGPTCreditsReset
            usage={usage}
            resetCredits={resetCredits}
            loading={loading}
            onRefresh={() => load(true)}
          />
        )}
      </div>
    </ModalOverlay>
  )
}

/**
 * Compact per-role field for provider=chatgpt: replaces the Base URL / API
 * key inputs (the subscription needs neither) with a login-status button
 * that opens the full subscription panel. Status refreshes on mount and
 * whenever the panel closes, so a login completed inside the panel is
 * reflected immediately.
 */
const FIELD_STATES = {
  active: {
    dot: 'bg-green-500',
    text: 'text-green-700 dark:text-green-400',
    key: 'chatgpt.field.active',
  },
  needs_relogin: {
    dot: 'bg-red-500',
    text: 'text-red-700 dark:text-red-400',
    key: 'chatgpt.field.needsRelogin',
  },
  logged_out: {
    dot: 'bg-gray-400',
    text: 'text-gray-500 dark:text-gray-400',
    key: 'chatgpt.field.loggedOut',
  },
}

export function ChatGPTProviderField() {
  const { t } = useTranslation()
  const status = useChatGPTStatus()
  const [panelOpen, setPanelOpen] = useState(false)

  const closePanel = () => {
    setPanelOpen(false)
    // Pick up anything that changed while the panel was open (login,
    // logout, import) so the button never shows a stale state.
    fetchChatGPTStatus()
  }

  // expired counts as signed in: the refresh token renews it automatically.
  const state = status?.state === 'expired' ? 'active' : (status?.state || 'logged_out')
  const field = FIELD_STATES[state]
  const pending = !!status?.login_pending

  return (
    <div className="lg:col-span-2">
      <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">
        {t('chatgpt.field.label')}
      </span>
      <button
        type="button"
        onClick={() => setPanelOpen(true)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm hover:border-sigma-600 transition-colors"
      >
        {pending ? (
          <Loader2 className="w-3.5 h-3.5 flex-shrink-0 animate-spin text-sigma-600" />
        ) : (
          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${field.dot}`} />
        )}
        <span className={`min-w-0 truncate font-bold ${field.text}`}>
          {status
            ? t(pending ? 'chatgpt.field.pending' : field.key, { email: status.email || '' })
            : t('common.loading')}
        </span>
        <PlanBadge plan={status?.plan_type} />
        <ChevronRight className="w-4 h-4 ml-auto flex-shrink-0 text-gray-400" />
      </button>

      {panelOpen && (
        <div className="fixed inset-0 z-[6000] flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300"
            onClick={closePanel}
          />
          <div className="relative z-[6001] w-full max-w-5xl max-h-[86vh] bg-white dark:bg-gray-900 rounded-2xl shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 overflow-hidden flex flex-col animate-in zoom-in duration-300">
            <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex-shrink-0">
              <ShieldCheck className="w-5 h-5 text-sigma-600 dark:text-sigma-400 flex-shrink-0" />
              <h3 className="text-sm font-black text-gray-900 dark:text-gray-100 min-w-0 truncate">
                {t('chatgpt.title')}
              </h3>
              <button
                type="button"
                onClick={closePanel}
                className="ml-auto p-1.5 rounded-lg text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                title={t('common.close')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="min-h-0 overflow-y-auto p-4">
              <ChatGPTSubscriptionSection />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
