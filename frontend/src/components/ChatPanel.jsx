/**
 * ChatPanel - Independent chat panel for Explore/Library tabs.
 * Each instance maintains its own message state.
 *
 * On page load, restores chat history from DB (survives refresh/close).
 * If a background task is running, silently reconnects to SSE for live updates.
 */
import { useState, useRef, useEffect, useCallback } from 'react'
import { flushSync } from 'react-dom'
import { useClickOutside } from '../hooks/useClickOutside'
import { MarkdownContent, ThinkingProcess, CompactSummaryNote, STREAM_RECOVERY_MAX_ATTEMPTS, STREAM_RECOVERY_DELAY_MS, isAgentToolName, streamStatusText, withTransientHint } from './ChatShared'
import { Send, RotateCw, Bot, User, Zap, Square, Quote, X, Pencil, Check, ChevronUp, ChevronDown, List, Archive, Trash2, Plus, TextQuote, Shield, Copy, Gauge, ArrowLeft, Image as ImageIcon, Loader2, GitBranch, Search, AlertTriangle } from 'lucide-react'
import ContextPuck from './ContextPuck'
import { toastError, toastSuccess } from './Toast'
import { ModalOverlay, ConfirmModal } from './Modal'
import ChatSearchModal from './ChatSearchModal'
import Toggle from './Toggle'
import { chatAPI, skillsAPI, permissionsAPI } from '../api'
import { createSSEStreamParser } from '../utils/sse'
import { storage, STORAGE_KEYS } from '../utils/storage'
import { copyToClipboard } from '../utils/clipboard'
import { formatTimestamp } from '../utils/formatTimestamp'
import { joinCitationTexts } from '../utils/citations'
import { useStore } from '../store/useStore'
import { useTranslation } from 'react-i18next'

function formatTokenCount(value) {
  const n = Number(value || 0)
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}K`
  return String(n)
}

// Auto-approve categories in fixed display order — single source shared by
// the ContextPuck dots, the hover legend, and the settings menu rows.
const APPROVAL_CATEGORIES = [
  { key: 'file_external', labelKey: 'permission.cat.fileExternal', descKey: 'permission.cat.fileExternalDesc' },
  { key: 'file_internal', labelKey: 'permission.cat.fileInternal', descKey: 'permission.cat.fileInternalDesc' },
  { key: 'bash', labelKey: 'permission.cat.bash', descKey: 'permission.cat.bashDesc' },
  { key: 'notebook', labelKey: 'permission.cat.notebook', descKey: 'permission.cat.notebookDesc' },
]

// Mark a running tool step done; edit/write calls additionally carry the
// backend's file_edit diff metadata for the timeline card.
function finishToolStep(step, payload) {
  const done = { ...step, result: payload.result_summary, status: 'done' }
  if (payload.file_edit) done.fileEdit = payload.file_edit
  return done
}

// Strip parked-turn "waiting for input" markers from a process timeline —
// at the top level and inside every tool step's subSteps. Called when the
// user answers the dialog: the pause is over, and the resumed stream's live
// events take over the rendering.
function stripAwaitingSteps(process) {
  if (!Array.isArray(process)) return process
  const kept = []
  for (const step of process) {
    if (step.type === 'awaiting_input') continue
    if (Array.isArray(step.subSteps) && step.subSteps.some(s => s.type === 'awaiting_input')) {
      kept.push({ ...step, subSteps: stripAwaitingSteps(step.subSteps) })
      continue
    }
    kept.push(step)
  }
  return kept
}

// Live reasoning window tuning: the flush throttle keeps token-frequency SSE
// chunks from re-rendering the panel every few milliseconds, and the tail
// slice bounds how much text enters React state per flush.
const THINK_FLUSH_MS = 120
const THINK_TAIL_CHARS = 800

// Stream-liveness tuning: while a stream is open, a periodic backend
// cross-check catches zombie streams whose terminal event was lost; when a
// stream drops without one, reconnect to the still-running task a bounded
// number of times before giving up and reloading history. The probe itself
// gets a hard timeout so a blackholed connection cannot hang the check; only
// after consecutive timed-out probes is the stream treated as dead.
const STREAM_WATCHDOG_INTERVAL_MS = 10000
const STREAM_PROBE_TIMEOUT_MS = 5000
const STREAM_PROBE_MAX_FAILURES = 2
// Consecutive getActive probes a recovery may spend while the network is
// still down before it gives up and surfaces the loss to the user. Sized so
// a ~30s outage stays inside the recovery window (5s probe timeout each).
const STREAM_PROBE_RECOVERY_ATTEMPTS = 5

// Events that never interrupt an open thinking segment — bookkeeping or
// out-of-band notifications arriving mid-thought.
const THINK_PASSTHROUGH_TYPES = new Set([
  'task_id', 'context_stats', 'task_list', 'turn_usage',
  'file_changed', 'annotation_changed',
])

// Spinner phase per SSE event, flipped at event-dispatch level so the
// setMessages updater stays free of setState/ref side effects. Unmapped
// events (reasoning deltas, stream-status noise, passthrough bookkeeping)
// leave the current phase unchanged.
const STREAM_PHASE_BY_EVENT = {
  delta: 'processing',
  tool_start: 'executing',
  // tool_end returns to "thinking": the next LLM iteration reopens with
  // reasoning before producing text or another tool call.
  tool_end: 'thinking',
  compact_start: 'compacting',
  compact_done: 'thinking',
  awaiting_input: null,
  done: null,
  error: null,
  cancelled: null,
}

// Last n characters of the reasoning buffer, backing off from a cut that
// would split a UTF-16 surrogate pair — the rendered content of the live
// thinking window.
function thinkingTail(text) {
  const s = text || ''
  if (s.length <= THINK_TAIL_CHARS) return s
  let start = s.length - THINK_TAIL_CHARS
  while (start > 0 && s.charCodeAt(start) >= 0xdc00 && s.charCodeAt(start) <= 0xdfff) {
    start -= 1
  }
  return s.slice(start)
}

// Elapsed ms from the preceding user bubble to now, or null when the turn has
// no user bubble (e.g. interaction resume) or an unparseable timestamp.
function turnDurationMs(newMsgs, lastIdx) {
  const prev = newMsgs[lastIdx - 1]
  if (prev?.role !== 'user' || !prev.created_at) return null
  const started = Date.parse(prev.created_at)
  return Number.isFinite(started) ? Date.now() - started : null
}

function formatDurationMs(ms) {
  const s = Math.max(1, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

function parseMillionTokenBudget(value) {
  const raw = String(value || '').trim()
  if (!/^\d+(?:\.\d+)?$/.test(raw)) return null
  const [whole, frac = ''] = raw.split('.')
  if (frac.length > 6) return null
  const padded = (frac + '000000').slice(0, 6)
  const tokens = Number(whole) * 1_000_000 + Number(padded)
  return Number.isSafeInteger(tokens) && tokens > 0 ? tokens : null
}

/**
 * Supported slash commands. Display labels/descriptions are looked up via t().
 * The command string is the exact text the user sends (e.g. '/compact').
 */
const SLASH_COMMANDS = [
  { command: '/compact', labelKey: 'chat.slashCompact', descKey: 'chat.slashCompactDesc' },
  { command: '/plan', labelKey: 'chat.slashPlan', descKey: 'chat.slashPlanDesc' },
  { command: '/clear', labelKey: 'chat.slashClear', descKey: 'chat.slashClearDesc' },
  { command: '/new', labelKey: 'chat.slashNew', descKey: 'chat.slashNewDesc' },
  { command: '/delete', labelKey: 'chat.slashDelete', descKey: 'chat.slashDeleteDesc' },
  { command: '/skill', labelKey: 'chat.slashSkill', descKey: 'chat.slashSkillDesc' },
]

/**
 * Returns the list of slash commands whose name has `text` as a strict prefix.
 * - `/`         → all commands
 * - `/c`        → commands starting with 'c' (e.g. /compact)
 * - `/compact ` → no match (trailing space breaks the "exact prefix" rule)
 * - `foo`       → no match (must start with /)
 */
function getSlashSuggestions(text) {
  const m = text.match(/^\/([a-zA-Z][\w-]*)?$/)
  if (!m) return []
  const q = (m[1] || '').toLowerCase()
  return SLASH_COMMANDS.filter(cmd => cmd.command.slice(1).toLowerCase().startsWith(q))
}

// Display text of a user message: a bare /plan renders as the localized plan
// label (callers pass t('chat.planDisplay')); /plan <text> renders the text
// after the command.
function displayMessageText(text, planLabel) {
  const stripped = text.trim()
  if (stripped === '/plan') return planLabel
  if (stripped.startsWith('/plan ') || stripped.startsWith('/plan\t')) {
    return stripped.slice(5).trimStart()
  }
  return text
}

// Sentinel highlight id for a just-sent/edit-resend bubble that has no server
// id yet — by definition the latest, so the list highlights its final entry.
const NAV_HIGHLIGHT_LAST = '__latest__'

// Platform-appropriate hint for the search shortcut.
const SEARCH_SHORTCUT_LABEL = navigator.userAgent.includes('Mac') ? '⌘K' : 'Ctrl+K'

// One-line summary for the message-list popup: whitespace only is collapsed
// (multi-line content must render as a single row). Visual truncation is
// left to CSS (`truncate`), which adapts to actual glyph widths — counting
// characters cannot, because CJK and Latin glyphs are not equally wide.
function summarizeForNav(text) {
  return (text || '').replace(/\s+/g, ' ').trim()
}

function attachmentSrc(projectId, attachment) {
  return `/api/v1/files/${encodeURIComponent(projectId)}/inline?path=${encodeURIComponent(attachment.path)}`
}

function imageFilesFromList(files) {
  return Array.from(files || []).map(item => {
    if (item instanceof File) return item
    if (item.kind === 'file' && item.type?.startsWith('image/')) return item.getAsFile()
    return null
  }).filter(file => file?.type?.startsWith('image/'))
}

function AttachmentStrip({ projectId, attachments, onRemove = null, compact = false }) {
  const { t } = useTranslation()
  if (!attachments?.length) return null
  return (
    <div className={`flex flex-wrap gap-2 ${compact ? 'mt-2' : ''}`}>
      {attachments.map(item => (
        <div key={item.path} className="relative group/attachment">
          <a
            href={attachmentSrc(projectId, item)}
            target="_blank"
            rel="noreferrer"
            className="block overflow-hidden rounded-lg border border-white/20 bg-white/10"
            title={item.name || item.path}
          >
            <img
              src={attachmentSrc(projectId, item)}
              alt={item.name || t('chat.attachedImage')}
              className={`${compact ? 'h-20 w-20' : 'h-16 w-16'} object-cover`}
            />
          </a>
          {onRemove && (
            <button
              type="button"
              onClick={() => onRemove(item.path)}
              className="absolute -right-1.5 -top-1.5 rounded-full bg-gray-900/80 p-0.5 text-white opacity-90 hover:bg-gray-900"
              title={t('chat.removeImage')}
            >
              <X className="w-3 h-3" />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

// ---- Intra-process lock: prevent concurrent session creation for the same project ----
const sessionInitLocks = new Map() // projectId → Promise<sessionId>
const HISTORY_PAGE_SIZE = 10
// Cap for full-history sweeps (nav list build, nav jump preload). Only guards
// against a runaway cursor — real sessions end via has_more far earlier. The
// list must never offer entries the jump cannot reach, so both sweeps share it.
const NAV_SWEEP_PAGE_LIMIT = 100

export default function ChatPanel({ projectId, placeholder, citations = [], onClearCitations = null, onRemoveCitation = null, onFileChanged = null, onAnnotationChanged = null, getUserState = null, onSaveBeforeChat = null, onCitation = null }) {
  const { t } = useTranslation()
  const resolvedPlaceholder = placeholder || t('chat.askPlaceholder')
  const [chatInput, setChatInput] = useState('')
  const [pendingAttachments, setPendingAttachments] = useState([])
  const [isUploadingAttachment, setIsUploadingAttachment] = useState(false)
  const [messages, setMessages] = useState([])
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [sessionId, setSessionId] = useState(null)
  const [sessionTitle, setSessionTitle] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  // Mirror isStreaming/awaiting into refs so the pendingAutoMessage effect and
  // handleDirectSend read the latest values without being closed over a stale
  // render (their effects would otherwise re-fire on every streaming token if
  // these were added to the dependency arrays).
  const isStreamingRef = useRef(false)
  const awaitingRef = useRef(false)
  const [viewingCitation, setViewingCitation] = useState(null)
  const chatScrollRef = useRef(null)
  // Inner content wrapper — ResizeObserver watches it so we can re-pin to the
  // bottom whenever async layout (KaTeX typesetting, image decode, entrance
  // animations) grows the content height after the initial render.
  const chatContentRef = useRef(null)
  // Streaming phase drives the spinner label (thinking/processing/executing/
  // compacting) and is derived from SSE events as they arrive. Reasoning is
  // shown only in the live window below the label while the phase is
  // "thinking" — it never reaches the persisted timeline.
  const [streamPhase, setStreamPhase] = useState(null)
  // Live "thinking" window. The buffer ref accumulates the current reasoning
  // segment in full; thinkingLive mirrors only its throttled tail so the
  // panel re-renders on the flush cadence, not per token.
  const [thinkingLive, setThinkingLive] = useState('')
  const liveBufferRef = useRef('')
  const liveFlushAtRef = useRef(0)
  const segOpenRef = useRef(false)
  // Auto-follow for the live window — released when the user scrolls up.
  const thinkBoxRef = useRef(null)
  const thinkFollowRef = useRef(true)
  useEffect(() => {
    const el = thinkBoxRef.current
    if (el && thinkFollowRef.current) el.scrollTop = el.scrollHeight
  }, [thinkingLive])
  // Identity of the CURRENT stream, { gen, controller, lastSeq }: gen is the
  // generation captured at turn start (ownership across session switches),
  // controller aborts the fetch, lastSeq is the highest applied SSE event id
  // (cursor for resuming a dropped stream).
  const streamHandleRef = useRef(null)
  const textareaRef = useRef(null)
  const imageInputRef = useRef(null)
  const pendingInteractionRef = useRef(null)  // deferred dispatch to avoid cross-render setState
  const genRef = useRef(0)  // generation counter — prevents stale finally() from overwriting isStreaming
  const isUserAtBottom = useRef(true)
  // Whether the chat content overflows the scroll container. Gates the message
  // navigation buttons — they are useless on a chat short enough to fit.
  const [chatScrollable, setChatScrollable] = useState(false)
  const taskIdRef = useRef(null)  // current backend task ID for cancellation
  // The live streaming bubble carries a locally-generated `localId` and events
  // target it by that id, so SSE handling survives mid-stream array mutations
  // (history merges, reorderings) instead of depending on "last element".
  const liveTurnIdRef = useRef(null)
  const liveTurnSeqRef = useRef(0)
  const stopAbortTimerRef = useRef(null)
  const stopRequestedRef = useRef(false)
  const abnormalTerminalRef = useRef(false)
  const historyCursorRef = useRef(null)
  const historyLoadingRef = useRef(false)
  const lastSkillsVersionRef = useRef(-1)  // dedup /skill submenu fetches across re-renders
  const historyModeRef = useRef('latest')
  const sessionIdRef = useRef(null)  // latest sessionId, for async guards (nav jump sweep)

  // Session title inline editing
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [editTitle, setEditTitle] = useState('')
  const titleInputRef = useRef(null)

  // Session dropdown
  const [showDropdown, setShowDropdown] = useState(false)
  const [sessions, setSessions] = useState([])
  const dropdownRef = useRef(null)
  const dropdownBtnRef = useRef(null)
  const [dropdownPos, setDropdownPos] = useState({ top: 0, left: 0 })

  // Chat search (opened from the sessions dropdown, or Ctrl/Cmd+K)
  const [searchOpen, setSearchOpen] = useState(false)
  const panelRootRef = useRef(null)
  // Brief row highlight so a jump landing is instantly visible.
  const [flashMessageId, setFlashMessageId] = useState(null)
  const flashTimerRef = useRef(null)
  // Search jump waiting on a session switch: consumed after the new
  // session's initial history commit (see the pendingJump effect).
  const pendingJumpRef = useRef(null)
  // Session id whose messages are currently committed in state — guards
  // the pending-jump effect against firing on the previous session's
  // still-rendered messages.
  const messagesOwnerRef = useRef(null)
  const historyGenerationRef = useRef(0)
  const panelOwnerRef = useRef(null)
  // Archive-modal focus target from a search hit: consumed once the target
  // session's messages render (see the archivedFocus effect).
  const archivedFocusRef = useRef(null)

  // Auto-approve settings menu
  const [showAutoApproveMenu, setShowAutoApproveMenu] = useState(false)
  const [settingsPanel, setSettingsPanel] = useState('main')
  const [slashActiveIdx, setSlashActiveIdx] = useState(0)
  // Skill submenu: enabledSkills is null until first /skill <space> triggers a load
  const [enabledSkills, setEnabledSkills] = useState(null)
  const [skillActiveIdx, setSkillActiveIdx] = useState(0)
  const autoApproveMenuRef = useRef(null)
  const autoApproveBtnRef = useRef(null)
  const [autoApproveMenuPos, setAutoApproveMenuPos] = useState({ bottom: 0, left: 0 })
  const autoApproveSettings = useStore(s => s.autoApproveSettings)
  const autoApproveLoadFailed = useStore(s => s.autoApproveLoadFailed)
  const loadAutoApproveSettings = useStore(s => s.loadAutoApproveSettings)
  const setAutoApproveType = useStore(s => s.setAutoApproveType)
  const currentProject = useStore(s => s.currentProject)
  const skillsVersion = useStore(s => s.skillsVersion)

  // Per-turn token budget
  const [tokenBudget, setTokenBudget] = useState(null)
  const [contextStats, setContextStats] = useState(null)
  const [budgetDraft, setBudgetDraft] = useState('')
  const [budgetError, setBudgetError] = useState('')

  // Per-category loading flag while an auto-approve toggle is being persisted.
  const [approvingCategory, setApprovingCategory] = useState(null)

  // Message editing
  const [editingMessageId, setEditingMessageId] = useState(null)
  const [editingText, setEditingText] = useState('')
  // Message id of the most recently copied message; shows a check briefly,
  // mirroring the docker-command copy feedback in BackendErrorOverlay.
  const [copiedMessageId, setCopiedMessageId] = useState(null)
  const copiedTimerRef = useRef(null)  // tracks the copy-feedback timeout for unmount cleanup

  // Archived sessions modal
  const [showArchived, setShowArchived] = useState(false)
  const [archivedSessions, setArchivedSessions] = useState([])
  const [expandedArchivedId, setExpandedArchivedId] = useState(null)
  const [archivedMessages, setArchivedMessages] = useState([])
  const [editingArchivedId, setEditingArchivedId] = useState(null)
  const [editArchivedTitle, setEditArchivedTitle] = useState('')
  const archivedTitleInputRef = useRef(null)

  // Delete confirmation modals
  const [deleteSessionTarget, setDeleteSessionTarget] = useState(null) // session id
  const [deleteArchivedTarget, setDeleteArchivedTarget] = useState(null) // session id

  useEffect(() => () => {
    clearStopAbortTimer()
    if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current)
  }, [])

  // ---- Lazy-load enabled skills for the /skill submenu ----
  // Refetch when SkillPanel modifies skills (toggle/delete/import/edit-SKILL.md),
  // signalled via the Zustand `skillsVersion` counter. Same version → cached.
  const loadEnabledSkills = useCallback(async () => {
    if (lastSkillsVersionRef.current === skillsVersion) return
    lastSkillsVersionRef.current = skillsVersion
    try {
      const list = await skillsAPI.list()
      setEnabledSkills(Array.isArray(list) ? list.filter(s => s.enabled) : [])
    } catch (e) {
      console.warn('Failed to load enabled skills:', e)
      setEnabledSkills([])
    }
  }, [skillsVersion])

  useEffect(() => {
    if (/^\/skill\s/.test(chatInput)) loadEnabledSkills()
  }, [chatInput, loadEnabledSkills])

  // ---- Load sessions list ----
  // The list includes archived sessions: the dropdown filters them out, the
  // archived counter reads them, and pruneSessionState must see every known
  // session id — pruning against the active-only subset would erase the
  // token budgets of archived sessions.
  const loadSessions = useCallback(async () => {
    if (!projectId) return
    try {
      const list = await chatAPI.listSessions(projectId, { include_archived: true })
      storage.pruneSessionState(projectId, list.map(s => s.id))
      setSessions(list)
      if (sessionId) {
        const cur = list.find(s => s.id === sessionId)
        if (cur) setSessionTitle(cur.title || '')
      }
    } catch { /* ignore */ }
  }, [projectId, sessionId])

  useEffect(() => {
    if (!projectId) return
    const onStorage = (e) => {
      if (e.key !== STORAGE_KEYS.project(projectId)) return
      if (e.newValue == null) {
        setSessionId(null)
        return
      }
      const nextSessionId = storage.getSession(projectId)
      if (nextSessionId && nextSessionId !== sessionId) {
        setSessionId(nextSessionId)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [projectId, sessionId])

  // ---- Effect 1: Resolve session ID (deduplicated across concurrent mounts) ----
  useEffect(() => {
    if (!projectId) return
    let cancelled = false

    const resolve = async () => {
      // Reuse an in-flight initialization for this project (StrictMode remounts
      // the effect while the first resolve is still running)
      let lock = sessionInitLocks.get(projectId)
      if (lock) {
        const sid = await lock
        if (!cancelled) { setSessionId(sid) }
        return
      }

      const promise = (async () => {
        // Pruning must cover archived sessions too (their budgets live in the
        // same map); session selection below stays scoped to active ones.
        let sid = storage.getSession(projectId)
        if (sid) {
          try {
            const list = await chatAPI.listSessions(projectId, { include_archived: true })
            storage.pruneSessionState(projectId, list.map(s => s.id))
            const found = list.find(s => s.id === sid && !s.is_archived)
            if (!found) sid = null
            else setSessionTitle(found.title || '')
          } catch { sid = null }
        }
        if (!sid) {
          try {
            const list = await chatAPI.listSessions(projectId, { include_archived: true })
            storage.pruneSessionState(projectId, list.map(s => s.id))
            const active = list.filter(s => !s.is_archived)
            if (active.length > 0) {
              sid = active[0].id
              setSessionTitle(active[0].title || '')
            }
          } catch { /* fall through to create */ }
        }
        if (!sid) {
          try {
            const session = await chatAPI.createSession(projectId)
            sid = session.id
            setSessionTitle(session.title || '')
          } catch (e) {
            console.error('Failed to create session:', e)
            return null
          }
        }
        storage.setSession(projectId, sid)
        return sid
      })()

      sessionInitLocks.set(projectId, promise)
      const sid = await promise
      sessionInitLocks.delete(projectId)
      if (sid && !cancelled) { setSessionId(sid) }
    }

    resolve()
    return () => { cancelled = true }
  }, [projectId])

  function normalizeHistoryPage(response) {
    const messages = Array.isArray(response?.messages) ? response.messages : []
    // Backfill the live-stream match keys from the backend's tool_call_id so
    // a resumed stream (tool_end / agent_event) can re-attach to a step that
    // was loaded from history after a refresh and finish it in place.
    const normalized = messages.map(m => {
      if (!Array.isArray(m?.process)) return m
      let touched = false
      const process = m.process.map(s => {
        if (s?.type === 'tool' && s.tool_call_id && !s.toolCallId) {
          touched = true
          return { ...s, toolCallId: s.tool_call_id, _toolCallId: s.tool_call_id }
        }
        return s
      })
      return touched ? { ...m, process } : m
    })
    return {
      messages: normalized,
      has_more: Boolean(response?.has_more),
      next_before_seq: response?.next_before_seq ?? null,
      boundary_seq: response?.boundary_seq ?? null,
    }
  }

  function setHistoryPaging(page) {
    // Test the raw value: Number(null) === 0 is finite, and an exhausted page
    // must leave the cursor null, not a bogus seq-0 cursor.
    const cursor = page.has_more ? page.next_before_seq : null
    historyCursorRef.current = Number.isFinite(cursor) ? cursor : null
    setHasMoreHistory(Boolean(page.has_more && historyCursorRef.current !== null))
  }

  function resetHistoryPaging() {
    historyCursorRef.current = null
    historyLoadingRef.current = false
    historyModeRef.current = 'latest'
    setHasMoreHistory(false)
    setIsLoadingHistory(false)
  }

  function messageSeq(message) {
    const seq = Number(message?.seq)
    return Number.isFinite(seq) ? seq : null
  }

  function mergeHistoryMessages(current, incoming, { dropLocal = false } = {}) {
    const byId = new Map()
    const anonymous = []
    const append = (message, replace = true) => {
      if (message?.id) {
        if (replace || !byId.has(message.id)) byId.set(message.id, message)
        return
      }
      if (!dropLocal) anonymous.push(message)
    }
    current.forEach(message => append(message, false))
    incoming.forEach(message => append(message, true))
    return [...byId.values(), ...anonymous].sort((a, b) => {
      const aSeq = messageSeq(a)
      const bSeq = messageSeq(b)
      if (aSeq !== null && bSeq !== null) return aSeq - bSeq
      if (aSeq !== null) return -1
      if (bSeq !== null) return 1
      return 0
    })
  }

  async function fetchHistoryPage(beforeSeq = null) {
    const generation = historyGenerationRef.current
    const ownerProjectId = projectId
    const ownerSessionId = sessionId
    const params = { limit: HISTORY_PAGE_SIZE }
    if (beforeSeq !== null) params.beforeSeq = beforeSeq
    const page = normalizeHistoryPage(await chatAPI.history(ownerProjectId, ownerSessionId, params))
    return generation === historyGenerationRef.current && ownerProjectId === projectId && ownerSessionId === sessionId
      ? page
      : null
  }

  async function refreshLatestHistory({ dropLocal = true } = {}) {
    if (!projectId || !sessionId) return []
    const page = await fetchHistoryPage()
    if (!page) return []
    if (historyModeRef.current === 'latest') setHistoryPaging(page)
    setMessages(prev => mergeHistoryMessages(prev, page.messages, { dropLocal }))
    return page.messages
  }

  function nextLiveTurnId() {
    liveTurnSeqRef.current += 1
    const id = `live-turn-${liveTurnSeqRef.current}`
    liveTurnIdRef.current = id
    return id
  }

  /** Index of the bubble live SSE events apply to: the locally-tagged
   * streaming bubble when present, else the trailing SiGMA bubble (a
   * resume/reconnect rebuilt it without the tag). -1 when there is none. */
  function findLiveTargetIndex(msgs) {
    const liveId = liveTurnIdRef.current
    if (liveId) {
      const idx = msgs.findIndex(m => m.localId === liveId)
      if (idx >= 0) return idx
    }
    const lastIdx = msgs.length - 1
    if (lastIdx >= 0 && msgs[lastIdx].role === 'SiGMA') return lastIdx
    return -1
  }

  /** Register the single active stream. Aborts any leftover stream first:
   *  after a lost terminal event its connection can hang indefinitely,
   *  leaking the fetch and its watchdog interval. */
  /** Token sums to carry onto a rebuilt live bubble: the trailing SiGMA
   *  entries being popped are the in-progress turn's persisted rows, and
   *  the usage line under the bubble should survive the rebuild (the next
   *  turn_usage event overwrites it with authoritative totals). */
  function collectTurnUsageSums(entries) {
    return entries.reduce((acc, e) => ({
      token_count: (acc.token_count || 0) + (e.token_count || 0),
      cached_tokens: (acc.cached_tokens || 0) + (e.cached_tokens || 0),
      input_tokens: (acc.input_tokens || 0) + (e.input_tokens || 0),
    }), {})
  }

  function beginStreamHandle(controller = new AbortController()) {
    const handle = { gen: genRef.current, controller, lastSeq: 0, probeFailures: 0, probeDead: false, recoveryGaveUp: false }
    streamHandleRef.current?.controller.abort()
    streamHandleRef.current = handle
    return handle
  }

  /** Lower the streaming flag only while *handle* still owns the live
   *  stream: a superseded turn's cleanup (its connection was aborted when a
   *  new stream took over, see beginStreamHandle) must not clobber the
   *  successor's state. A recovery that exhausted its budget while the task
   *  is still running (recoveryGaveUp) keeps the flag instead: spinner and
   *  stop button stay truthful and Stop remains a way out — dropping to idle
   *  would invite sends the backend rejects with 409. */
  function releaseStreaming(handle) {
    if (streamHandleRef.current !== handle) return
    if (handle.recoveryGaveUp) return
    setIsStreaming(false)
  }

  /** End-of-turn cleanup shared by every send path: refresh server history
   *  (real message IDs, can_edit flags) unless the turn parked for input —
   *  then the live process array is the source of truth and a refresh would
   *  discard the anonymous streaming bubble — then lower the streaming flag
   *  if this handle still owns it. */
  async function finalizeStreamTurn(handle, { refreshHistory = true } = {}) {
    if (genRef.current !== handle.gen) return
    const pausing = !!(
      useStore.getState().pendingPermission
      || useStore.getState().pendingInteraction
      || pendingInteractionRef.current
    )
    if (refreshHistory && !pausing && !handle.controller.signal.aborted && projectId && sessionId) {
      try {
        if (genRef.current === handle.gen) await refreshLatestHistory()
      } catch { /* best-effort */ }
    }
    releaseStreaming(handle)
  }

  /** Restore a pending interaction checkpoint that the live stream failed to
   * deliver (dropped chunk / lost connection): without it the turn sits paused
   * with no dialog to answer. Returns true when a dialog was restored.
   * force=true overwrites any dialog already showing (session-load path). */
  function restoreInteractionFromActive(active, { force = false } = {}) {
    if (!active?.active || !['awaiting_input', 'interaction_failed'].includes(active.status) || !active.interaction) return false
    if (!force && (
      useStore.getState().pendingPermission
      || useStore.getState().pendingInteraction
      || pendingInteractionRef.current
    )) return false
    const interactionType = active.interaction.interaction_type
      || active.interaction.interaction_data?.interaction_type
    // Parked turns own no stream, so this is the only place the cancel
    // entry can learn the backend task id from.
    taskIdRef.current = active.task_id || null
    if (interactionType === 'permission') {
      useStore.getState().setPendingPermission({
        ...active.interaction.interaction_data,
        project_id: projectId,
        session_id: sessionId,
      })
    } else {
      useStore.getState().setPendingInteraction({
        type: interactionType,
        data: active.interaction.interaction_data || active.interaction,
        sessionId,
        projectId,
      })
    }
    return true
  }

  // ---- Effect 2: Load data + reconnect SSE (reacts to sessionId changes) ----
  useEffect(() => {
    historyGenerationRef.current += 1
    if (!projectId || !sessionId) return
    // Session switch: drop every session-scoped interaction/global synchronously —
    // the async load below restores the new session's own state (e.g. a parked
    // interaction checkpoint); stale dialogs from the previous session must not
    // survive until then.
    pendingInteractionRef.current = null
    const sharedState = useStore.getState()
    const previousOwner = panelOwnerRef.current
    const ownsPrevious = value => previousOwner && value && (
      value.projectId || value.project_id
    ) === previousOwner.projectId && (
      value.sessionId || value.session_id
    ) === previousOwner.sessionId
    if (ownsPrevious(sharedState.pendingInteraction)) sharedState.clearPendingInteraction()
    if (previousOwner && sharedState.pendingPermission?.project_id === previousOwner.projectId && sharedState.pendingPermission?.session_id === previousOwner.sessionId) {
      sharedState.clearPendingPermission()
    }
    if (ownsPrevious(sharedState.streamInteractionRequest)) sharedState.setStreamInteractionRequest(null)
    panelOwnerRef.current = { projectId, sessionId }
    // The id belongs to the previous session's task.
    taskIdRef.current = null
    setPendingAttachments([])
    // Invalidate prior generations: their cleanups must stop owning state.
    genRef.current += 1
    let cancelled = false

    const load = async () => {
      // Always reset — new session starts clean
      setIsStreaming(false)
      resetHistoryPaging()
      useStore.getState().setTaskList([])
      useStore.getState().setExpandedTasks(false)

      // 1. Load chat history into a local variable (don't commit yet —
      //    we need active-task state to decide whether trailing SiGMA
      //    bubbles are checkpoint artefacts or final replies).
      let history = []
      try {
        const page = await fetchHistoryPage()
        if (page) {
          history = page.messages
          setHistoryPaging(page)
        }
      } catch (e) {
        console.error('Failed to load chat history:', e)
      }

      if (cancelled) return

      // 2. Load sessions list
      try {
        const list = await chatAPI.listSessions(projectId, { include_archived: true })
        // Stale-response gate: a response landing after another session
        // switch must not overwrite the new session's list/title.
        if (!cancelled) {
          setSessions(list)
          const cur = list.find(s => s.id === sessionId)
          if (cur) setSessionTitle(cur.title || '')
        }
      } catch { /* ignore */ }

      // Load tasks for this session
      try {
        const tasks = await chatAPI.getTasks(projectId, sessionId)
        if (!cancelled && Array.isArray(tasks) && tasks.length > 0) {
          useStore.getState().setTaskList(tasks)
          useStore.getState().setExpandedTasks(true)
        }
      } catch { /* ignore */ }

      // 3. Check for active background task BEFORE committing messages.
      // A 503 (TASK_STATE_UNAVAILABLE) means the read itself failed — the
      // session is NOT known to be idle. Retry with backoff and surface the
      // recoverable error instead of falling through to the idle path.
      let active = null
      let activeCheckFailed = false
      for (let attempt = 0; ; attempt += 1) {
        try {
          active = await chatAPI.getActive(projectId, sessionId)
          break
        } catch (e) {
          if (e?.status === 503 && !cancelled && attempt < STREAM_PROBE_RECOVERY_ATTEMPTS - 1) {
            await new Promise(resolve => setTimeout(resolve, STREAM_RECOVERY_DELAY_MS))
            continue
          }
          if (e?.status === 503) toastError(t('chat.toast.taskStateUnavailable'))
          activeCheckFailed = true
          console.error('Failed to check active task:', e)
          break
        }
      }
      if (cancelled) return

      // Determine whether the last SiGMA bubble looks like an incomplete turn
      // (checkpoint artefact).  The backend now sets content="" for incomplete
      // turns, but we also guard against getActive failures here.
      const lastHistoryEntry = history.length > 0 ? history[history.length - 1] : null
      const looksIncomplete = lastHistoryEntry?.role === 'SiGMA' && (
        !lastHistoryEntry.content ||
        (Array.isArray(lastHistoryEntry.process) && lastHistoryEntry.process.length > 0)
      )

      if (active?.active && active.session_id === sessionId) {
        if (['awaiting_input', 'interaction_failed'].includes(active.status) && active.interaction) {
          // Permission approval checkpoint — restore the PermissionDialog.
          // The payload fields (tool/path/operation/content/description) are
          // nested under interaction_data; spreading the outer wrapper would
          // lose them. force=true: a fresh session load replaces any dialog
          // left over from a previous session.
          if (active.status === 'interaction_failed') {
            toastError(active.error || t(
              'chat.toast.interactionRecovery',
              'This interaction needs recovery. Review the prompt and try again.',
            ))
          }
          restoreInteractionFromActive(active, { force: true })
        } else {
          // Task active but not awaiting input — drop any stale interaction state
          useStore.getState().clearPendingInteraction()
          useStore.getState().clearPendingPermission()
        }

        if (active.status === 'running' || active.status === 'queued' || active.status === 'cancelling') {
          const preserved = []
          const poppedEntries = []
          while (history.length > 0 && history[history.length - 1].role === 'SiGMA') {
            const popped = history.pop()
            poppedEntries.push(popped)
            if (Array.isArray(popped.process)) {
              preserved.unshift(...popped.process)
            }
          }
          // The agent anchor row is persisted at spawn time, so a mid-run
          // refresh rebuilds its step as "interrupted" — no result row
          // exists while the subagent runs. The task is live; restore the
          // state the step is actually in so replayed agent_events attach
          // to a running step instead of an "interrupted" label.
          const cleanProcess = preserved.filter(s => !s.transient).map(s =>
            (s.type === 'tool' && s.status === 'interrupted' && isAgentToolName(s.tool))
              ? { ...s, status: 'running' }
              : s
          )
          history.push({
            role: 'SiGMA', content: '', process: cleanProcess,
            localId: nextLiveTurnId(),
            ...collectTurnUsageSums(poppedEntries),
          })
        }
      } else if (activeCheckFailed && looksIncomplete) {
        // getActive failed (network error, etc.) but the last entry looks like
        // a checkpoint artefact.  Conservatively pop it and rebuild so the
        // user sees a "working" state instead of stale intermediate text.
        const preserved = []
        const poppedEntries = []
        while (history.length > 0 && history[history.length - 1].role === 'SiGMA') {
          const popped = history.pop()
          poppedEntries.push(popped)
          if (Array.isArray(popped.process)) {
            preserved.unshift(...popped.process)
          }
        }
        const cleanProcess = preserved.filter(s => !s.transient)
        history.push({
          role: 'SiGMA', content: '', process: cleanProcess,
          localId: nextLiveTurnId(),
          ...collectTurnUsageSums(poppedEntries),
        })
      } else {
        // No active task for this session — clear stale interaction state so
        // modals from a previous session don't bleed across.
        useStore.getState().clearPendingInteraction()
        useStore.getState().clearPendingPermission()
      }

      // Commit the final message array in a single setState.
      setMessages(history)
      // Mark ownership synchronously with the commit: the pending-jump
      // effect must only act once these messages belong to this session.
      messagesOwnerRef.current = sessionId

      // Load persisted token budget for this session
      const savedBudget = storage.getBudget(projectId, sessionId)
      if (!cancelled) setTokenBudget(savedBudget || null)

      try {
        const stats = await chatAPI.contextStats(projectId, sessionId)
        if (!cancelled) setContextStats(stats)
      } catch { /* ignore */ }

      // 4. Reconnect to live SSE stream if a task is running
      if (active?.active && (active.status === 'running' || active.status === 'queued' || active.status === 'cancelling')) {
        if (cancelled) return
        // The handle owns the connection for its whole lifetime: a probeDead
        // recovery swaps handle.controller, so the cleanup below aborts
        // through the ref instead of a captured controller that the swap
        // would leave behind.
        const handle = beginStreamHandle()
        setIsStreaming(true)
        // Reconnecting mid-task: assume thinking until the next SSE event
        // reports the actual phase.
        setStreamPhase('thinking')
        try {
          const body = await chatAPI.resumeStream(projectId, active.task_id, handle.controller.signal)
          if (cancelled) return
          const reader = body.getReader()
          const decoder = new TextDecoder()
          await processSSEStream(reader, decoder, handle)
        } catch {
          if (cancelled || handle.controller.signal.aborted) return
          // The first subscription failed right after the task was confirmed
          // running — recover through the same bounded pipeline a mid-stream
          // drop uses (shared retry budget, backoff, give-up toast) instead
          // of silently dropping to a false idle.
          await recoverFailedConnect(handle)
        } finally {
          await finalizeStreamTurn(handle)
        }
      }
    }
    load()
    return () => {
      cancelled = true
      // Abort at the owning handle's CURRENT controller: a probeDead recovery
      // swaps handle.controller in place, so the ref — not a captured
      // closure controller — is the only abort source that still reaches the
      // live connection. The ref holds only the owning handle (a superseded
      // one was aborted at takeover), and no newer handle can exist yet since
      // cleanups run before the next effect creates one, so a session switch
      // never aborts the incoming session's stream. This also aborts any
      // user-initiated stream (handleSendMessage etc.) and detaches it: its
      // finally must see the abort instead of owning the flag.
      streamHandleRef.current?.controller.abort()
      streamHandleRef.current = null
    }
  }, [projectId, sessionId])

  // ---- Auto-scroll ----
  const handleChatScroll = useCallback(() => {
    const el = chatScrollRef.current
    if (!el) return
    isUserAtBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50
    if (el.scrollTop < 80) {
      loadOlderHistory()
    }
  }, [projectId, sessionId, hasMoreHistory])

  // Loads one older history page for the scroll-top trigger (wheel-driven
  // pagination). The message-list jump does not use this — far-back targets
  // need bulk loading (see jumpToNavEntry), and while a jump is bulk-loading
  // its sweep owns the cursor: a wheel-triggered page load here would race
  // it for pages.
  async function loadOlderHistory() {
    if (navJumpingRef.current || !hasMoreHistory) return
    const cursor = historyCursorRef.current
    if (!projectId || !sessionId || cursor === null || historyLoadingRef.current) return
    const el = chatScrollRef.current
    const prevHeight = el?.scrollHeight || 0
    historyLoadingRef.current = true
    setIsLoadingHistory(true)
    try {
      const page = await fetchHistoryPage(cursor)
      if (!page) return
      historyModeRef.current = 'expanded'
      setHistoryPaging(page)
      if (page.messages.length > 0) {
        setMessages(prev => {
          return mergeHistoryMessages(prev, page.messages)
        })
        requestAnimationFrame(() => {
          if (el) el.scrollTop = el.scrollHeight - prevHeight
        })
      }
    } catch (e) {
      console.error('Failed to load older history:', e)
    } finally {
      historyLoadingRef.current = false
      setIsLoadingHistory(false)
    }
  }

  // Pin to the bottom whenever the content grows while the user is at the
  // bottom. Cold load and streaming both settle asynchronously — KaTeX
  // typesetting, image decode, and entrance animations keep growing
  // scrollHeight for ~1s after the first paint — so a one-shot scroll on
  // [messages] change lands at a stale height. A ResizeObserver on the inner
  // content re-pins on every real height change instead. Pagination is
  // unaffected: the user is at the top then, so isUserAtBottom is false.
  // scrollTop is set directly (not via scrollTo): the container has no smooth
  // scrolling, so each correction is instantaneous and never animates.
  useEffect(() => {
    const el = chatScrollRef.current
    const content = chatContentRef.current
    if (!el || !content) return
    const ro = new ResizeObserver(() => {
      if (isUserAtBottom.current) el.scrollTop = el.scrollHeight
      setChatScrollable(el.scrollHeight - el.clientHeight > 8)
    })
    // Observe the container too: dragging the sidebar resizes the viewport and
    // can flip scrollability without any content height change.
    ro.observe(content)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // ---- Message navigation (floating buttons) ----
  // The anchor line: the y position an aligned message's top sits at (the
  // scroll container's content top — border-box top plus its top padding).
  const chatAnchorTop = useCallback((el) => {
    return el.getBoundingClientRect().top + (parseFloat(getComputedStyle(el).paddingTop) || 0)
  }, [])

  // Aligns a message node's top with the anchor line. Instant, and disarms
  // auto-pin BEFORE scrolling: the scroll event will update the flag too, but
  // an interleaved ResizeObserver tick could otherwise read the stale value
  // and snap the jump back to the bottom mid-flight.
  const alignStopTop = useCallback((node, nodeTop) => {
    const el = chatScrollRef.current
    if (!el || !node) return
    isUserAtBottom.current = false
    el.scrollTop = Math.max(0, el.scrollTop + (nodeTop ?? node.getBoundingClientRect().top) - chatAnchorTop(el))
  }, [chatAnchorTop])

  // Stops are the message roots (data-chat-msg). Each press jumps to the
  // nearest stop strictly above/below the anchor. Pressing Up from
  // mid-message lands on that message's own start, then walks back one
  // message per press; Down mirrors it and falls through to the very bottom
  // once no message start lies below. Geometry is read from the DOM at click
  // time, so streaming updates, history prepends, and message-list refreshes
  // can never desync it. scrollTop is assigned directly (instant) for the
  // same reason the auto-pin above avoids smooth scrolling: an animation
  // would fight the pin/pagination corrections.
  const jumpChatMessage = useCallback((dir) => {
    const el = chatScrollRef.current
    if (!el) return
    const anchor = chatAnchorTop(el)
    let target = null
    let targetTop = 0
    for (const m of el.querySelectorAll('[data-chat-msg]')) {
      const top = m.getBoundingClientRect().top
      // 2px tolerance: sub-pixel layout and scrollTop rounding can leave an
      // aligned message a fraction of a pixel off the anchor
      if (dir < 0 && top < anchor - 2) { target = m; targetTop = top }
      if (dir > 0 && top > anchor + 2) { target = m; targetTop = top; break }
    }
    if (dir < 0 && !target) return
    if (target) {
      alignStopTop(target, targetTop)
    } else {
      // Terminal Down stop: the bottom. Re-arm auto-pin so an active stream
      // keeps following after we land.
      isUserAtBottom.current = true
      el.scrollTop = el.scrollHeight
    }
  }, [alignStopTop, chatAnchorTop])

  // ---- Message list popup ----
  const [navListOpen, setNavListOpen] = useState(false)
  const [navList, setNavList] = useState([])
  const [navListLoading, setNavListLoading] = useState(false)
  const [navHighlightId, setNavHighlightId] = useState(null)
  const navPopupRef = useRef(null)
  const navListScrollRef = useRef(null)
  const navListBtnRef = useRef(null)
  const navJumpingRef = useRef(false)

  // The "current" message: the last user bubble at or above the anchor (an
  // assistant reply maps back to the question that produced it).
  const computeNavHighlight = useCallback(() => {
    const el = chatScrollRef.current
    if (!el) return
    const anchor = chatAnchorTop(el)
    let currentId = null
    for (const m of el.querySelectorAll('[data-chat-msg][data-msg-role="user"]')) {
      if (m.getBoundingClientRect().top > anchor + 2) break
      currentId = m.getAttribute('data-msg-id') || NAV_HIGHLIGHT_LAST
    }
    setNavHighlightId(currentId)
  }, [chatAnchorTop])

  // The popup lists EVERY user message of the session, not just the loaded
  // window (initial load is only HISTORY_PAGE_SIZE). Refetched on every open —
  // a single-user local app only pays cheap SQLite-backed paged reads, and
  // having no cache means no stale cache to invalidate.
  const openNavList = useCallback(async () => {
    if (navListOpen) {
      setNavListOpen(false)
      return
    }
    setNavListOpen(true)
    if (!projectId || !sessionId) {
      setNavListLoading(false)
      return
    }
    setNavListLoading(true)
    setNavList([])
    try {
      const entries = []
      let beforeSeq = null
      // Full sweep; the page cap only guards a runaway cursor — real sessions
      // end far earlier via has_more.
      for (let pageIdx = 0; pageIdx < NAV_SWEEP_PAGE_LIMIT; pageIdx++) {
        const page = normalizeHistoryPage(await chatAPI.history(projectId, sessionId, { limit: 200, beforeSeq }))
        // Checked after the await: a session switch while a page was in
        // flight must stop the sweep before it can overwrite the new
        // session's list with the old session's entries.
        if (sessionIdRef.current !== sessionId) return
        for (const m of page.messages) {
          if (m.role !== 'user') continue
          const text = displayMessageText(m.content || '', t('chat.planDisplay'))
          const hasText = text.trim().length > 0
          entries.push({
            id: m.id,
            seq: messageSeq(m),
            summary: hasText ? summarizeForNav(text) : t('chat.navImageOnly'),
          })
        }
        if (!page.has_more || page.next_before_seq === null) break
        beforeSeq = page.next_before_seq
      }
      // Pages arrive newest-first; the list reads top-to-bottom, oldest-first.
      entries.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
      setNavList(entries)
    } catch (e) {
      console.error('Failed to load message list:', e)
      setNavList([])
    } finally {
      // After a mid-sweep session switch, a reopened popup is owned by the
      // new session's own sweep — do not clear its loading state from under it.
      if (sessionIdRef.current === sessionId) setNavListLoading(false)
    }
  }, [navListOpen, projectId, sessionId, t])

  // Brief row highlight so a jump landing is instantly visible; the row's
  // persistent transition-colors class fades the highlight back out.
  const flashMessage = useCallback((messageId) => {
    if (flashTimerRef.current) clearTimeout(flashTimerRef.current)
    setFlashMessageId(messageId)
    flashTimerRef.current = setTimeout(() => setFlashMessageId(null), 1600)
  }, [])
  useEffect(() => () => { if (flashTimerRef.current) clearTimeout(flashTimerRef.current) }, [])

  // Jumping to a far-back entry needs the whole span from that entry to the
  // latest message loaded. Loading it through the wheel-driven 10-turn pages
  // would re-render the full list once per page (minutes on long sessions)
  // and could not reach entries beyond its own page budget. So the jump
  // sweeps with the same 200-turn pages the list itself uses, merges
  // everything in a single render, and aligns. flushSync guarantees the DOM
  // is committed when alignment measures it — an rAF wait can fire before
  // React commits under heavy renders. The sweep continues from the current
  // cursor so pages the user already scrolled up to load are not refetched.
  const jumpToMessage = useCallback(async (messageId, { fallbackNode = null } = {}) => {
    if (navJumpingRef.current || !messageId) return
    const el = chatScrollRef.current
    if (!el) return
    navJumpingRef.current = true
    historyLoadingRef.current = true
    setIsLoadingHistory(true)
    try {
      const findNode = () => el.querySelector(`[data-msg-id="${CSS.escape(messageId)}"]`)
      let node = findNode() || fallbackNode
      if (!node && projectId && sessionId) {
        const older = []
        let beforeSeq = historyCursorRef.current
        for (let pageIdx = 0; beforeSeq !== null && pageIdx < NAV_SWEEP_PAGE_LIMIT; pageIdx++) {
          const page = normalizeHistoryPage(await chatAPI.history(projectId, sessionId, { limit: 200, beforeSeq }))
          // Checked after the await: a session switch while a page was in
          // flight must stop the sweep before it writes the old session's
          // cursor into paging state owned by the new session.
          if (sessionIdRef.current !== sessionId) return
          older.push(...page.messages)
          setHistoryPaging(page)
          if (page.messages.some(m => m.id === messageId) || !page.has_more || page.next_before_seq === null) break
          beforeSeq = page.next_before_seq
        }
        if (older.length > 0) {
          historyModeRef.current = 'expanded'
          // Drop the loading row and commit the merged span atomically: the
          // row's removal would otherwise shift alignment after the fact.
          flushSync(() => {
            setIsLoadingHistory(false)
            setMessages(prev => mergeHistoryMessages(prev, older))
          })
        }
        node = findNode()
      }
      // History exhausted without the target (deleted concurrently, etc.) —
      // leave the scroll position untouched rather than guessing.
      if (node) {
        alignStopTop(node)
        flashMessage(messageId)
      }
    } catch (e) {
      console.error('Failed to load messages for jump:', e)
    } finally {
      navJumpingRef.current = false
      // After a mid-sweep session switch, the new session's effect already
      // reset these (resetHistoryPaging) and its own in-flight page load may
      // own the lock — clearing unconditionally would release it and hide
      // its loading row.
      if (sessionIdRef.current === sessionId) {
        historyLoadingRef.current = false
        setIsLoadingHistory(false)
      }
    }
  }, [alignStopTop, flashMessage, projectId, sessionId])

  // The nav popup wrapper: a just-sent/edit-resent bubble has no server id
  // yet, but by definition it is the latest message — match it positionally.
  const jumpToNavEntry = useCallback(async (entry) => {
    if (!entry?.id) return
    // Jumping is a destination action — close first so the list never
    // lingers over the chat while pages load for a far-back target.
    setNavListOpen(false)
    let fallbackNode = null
    const el = chatScrollRef.current
    if (el) {
      const userStops = el.querySelectorAll('[data-chat-msg][data-msg-role="user"]')
      const lastUser = userStops[userStops.length - 1]
      if (!lastUser?.getAttribute('data-msg-id') && entry === navList[navList.length - 1]) fallbackNode = lastUser
    }
    await jumpToMessage(entry.id, { fallbackNode })
  }, [jumpToMessage, navList])

  // A search jump to another session: switchToSession only changes the id —
  // the messages arrive with Effect 2's initial history commit. The owner
  // ref guarantees this fires on the new session's committed messages, never
  // on the old session's still-rendered ones.
  useEffect(() => {
    const pending = pendingJumpRef.current
    if (!pending) return
    // A switch to any other session abandons the jump; keeping it would fire
    // when the user later opens that session on their own.
    if (pending.sessionId !== sessionId) {
      pendingJumpRef.current = null
      return
    }
    if (messagesOwnerRef.current !== sessionId) return
    pendingJumpRef.current = null
    jumpToMessage(pending.messageId)
  }, [sessionId, messages, jumpToMessage])

  // Close the popup when the session changes; its entries belong to the old
  // one. sessionIdRef lets the in-flight nav sweeps (list build, jump
  // preload) detect the switch and drop their results instead of applying
  // them to the new session.
  useEffect(() => {
    sessionIdRef.current = sessionId
    setNavListOpen(false)
  }, [sessionId])

  // Live highlight while open: follows both manual scrolling and jumps.
  useEffect(() => {
    if (!navListOpen) return
    const el = chatScrollRef.current
    if (!el) return
    computeNavHighlight()
    el.addEventListener('scroll', computeNavHighlight, { passive: true })
    return () => el.removeEventListener('scroll', computeNavHighlight)
  }, [navListOpen, navListLoading, computeNavHighlight])

  useEffect(() => {
    if (!navListOpen) return
    const onKey = (e) => { if (e.key === 'Escape') setNavListOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navListOpen])

  // One outside-click region = popup ∪ toggle button. Two separate
  // useClickOutside calls would treat popup clicks as "outside the button"
  // and unmount the entry before its click event could fire.
  useEffect(() => {
    if (!navListOpen) return
    const onMouseDown = (e) => {
      const target = e.target
      if (navPopupRef.current?.contains(target) || navListBtnRef.current?.contains(target)) return
      setNavListOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    return () => document.removeEventListener('mousedown', onMouseDown)
  }, [navListOpen])

  // One-shot: reveal the current entry when the list opens. The scroll is
  // computed by hand and applied only to the popup's own list: native
  // scrollIntoView() also scrolls every programmatically-scrollable ancestor
  // (overflow: hidden boxes included), which shifts the whole chat panel and
  // leaves a persistent blank strip at its bottom. Layout-based offsets
  // (offsetTop/offsetHeight) are used because the popup's entrance animation
  // transforms getBoundingClientRect() values mid-flight.
  useEffect(() => {
    if (!navListOpen || navListLoading) return
    const list = navListScrollRef.current
    const row = list?.querySelector('[data-nav-current]')
    if (!list || !row) return
    list.scrollTop = row.offsetTop - list.offsetTop + row.offsetHeight / 2 - list.clientHeight / 2
  }, [navListOpen, navListLoading, navList])

  // ---- Close dropdown on outside click ----
  useClickOutside(dropdownRef, () => setShowDropdown(false), showDropdown)

  // ---- Close auto-approve menu on outside click ----
  useEffect(() => {
    if (!showAutoApproveMenu) return
    const handler = (e) => {
      if (autoApproveMenuRef.current && !autoApproveMenuRef.current.contains(e.target) &&
          autoApproveBtnRef.current && !autoApproveBtnRef.current.contains(e.target)) {
        setShowAutoApproveMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => {
      document.removeEventListener('mousedown', handler)
    }
  }, [showAutoApproveMenu])

  // ---- Focus title input when editing ----
  useEffect(() => {
    if (isEditingTitle && titleInputRef.current) {
      titleInputRef.current.focus()
      titleInputRef.current.select()
    }
  }, [isEditingTitle])

  // ---- Focus archived title input ----
  useEffect(() => {
    if (editingArchivedId && archivedTitleInputRef.current) {
      archivedTitleInputRef.current.focus()
      archivedTitleInputRef.current.select()
    }
  }, [editingArchivedId])

  // ---- Defer pendingInteraction dispatch (avoid cross-render setState) ----
  useEffect(() => {
    if (pendingInteractionRef.current) {
      const interaction = pendingInteractionRef.current
      pendingInteractionRef.current = null
      queueMicrotask(() => useStore.getState().setPendingInteraction(interaction))
    }
  })

  // ---- Handle interaction response stream (triggered by modals) ----
  const streamInteractionRequest = useStore(s => s.streamInteractionRequest)
  const pendingInteraction = useStore(s => s.pendingInteraction)
  const interactionDismissed = useStore(s => s.interactionDismissed)
  const setInteractionDismissed = useStore(s => s.setInteractionDismissed)
  const pendingPermission = useStore(s => s.pendingPermission)
  const awaiting = !!(pendingInteraction || pendingPermission)
  // Keep the refs in sync so async guards read the latest streaming/awaiting state.
  useEffect(() => {
    isStreamingRef.current = isStreaming
    awaitingRef.current = awaiting
  }, [isStreaming, awaiting])
  // When awaiting + dismissed, render the "reopen" button in place of textarea.
  const dismissedAny = interactionDismissed
  const reopenTarget = pendingInteraction ? 'interaction' : null

  /** Recover after a failed first connection to a turn's stream (interaction
   *  handoff, or the post-refresh reconnect): without it a still-running task
   *  would be dropped to a false idle. The probe in resumeBrokenStream
   *  branches on live server state — a parked checkpoint is restored into the
   *  dialog, a still-running task is reattached with a cursor resume, and a
   *  finished turn falls through to the caller's history refresh. */
  async function recoverFailedConnect(handle) {
    // sessionIdRef guards a session switch that landed while the failed
    // resume was unwinding: the checkpoint belongs to the old session.
    if (!projectId || !sessionId || sessionIdRef.current !== sessionId) return
    const resumed = await resumeBrokenStream(handle)
    if (!resumed) return
    // Reattached mid-task: assume thinking until the next SSE event reports
    // the actual phase.
    setStreamPhase('thinking')
    await processSSEStream(resumed.reader, resumed.decoder, handle)
  }

  async function handleInteractionStream(streamBody) {
    setIsStreaming(true)
    setStreamPhase('thinking')
    resetThinkingLive()
    // Remove every awaiting_input marker on user submission — top level and
    // inside agent subSteps (a subagent park drops its marker there, and the
    // resumed stream must not leave a stale "waiting for input" chip behind
    // while the loop keeps running).
    setMessages(prev => {
      const newMsgs = [...prev]
      const lastIdx = newMsgs.length - 1
      if (lastIdx >= 0) {
        const lastMsg = { ...newMsgs[lastIdx] }
        lastMsg.process = stripAwaitingSteps(lastMsg.process)
        newMsgs[lastIdx] = lastMsg
      }
      return newMsgs
    })
    // beginStreamHandle aborts any leftover stream: after a lost terminal
    // event its connection can hang indefinitely (blackholed network path),
    // leaking the fetch and its watchdog interval. Aborting lets that loop
    // exit and clean up; its finally cannot clobber this turn because
    // releaseStreaming only lowers the flag while it still owns the handle.
    const handle = beginStreamHandle()
    try {
      const body = await chatAPI.stream(projectId, streamBody, handle.controller.signal)
      const reader = body.getReader()
      const decoder = new TextDecoder()
      await processSSEStream(reader, decoder, handle)
    } catch (e) {
      if (e.name !== 'AbortError') {
        toastError(e.message || t('chat.toast.staleInteraction', 'This prompt is no longer current. Refresh the chat to recover it.'))
        // The dialog already closed when it handed off — recover the turn:
        // restore the parked checkpoint, reattach to the still-running task,
        // or let the finally's history refresh settle a finished turn.
        await recoverFailedConnect(handle)
      }
    } finally {
      await finalizeStreamTurn(handle)
    }
  }

  useEffect(() => {
    if (!streamInteractionRequest) return
    // The resume must target the checkpoint of the session that opened the
    // dialog; a request belonging to another session is not this panel's to
    // destroy — leave it so the panel showing that session consumes it.
    if (streamInteractionRequest.session_id !== sessionId) return
    useStore.getState().setStreamInteractionRequest(null)
    handleInteractionStream(streamInteractionRequest)
  }, [streamInteractionRequest, sessionId])

  // ---- Handle auto-message triggered from outside (e.g. LogModal "Ask SiGMA") ----
  const pendingAutoMessage = useStore(s => s.pendingAutoMessage)

  useEffect(() => {
    if (!pendingAutoMessage) return
    useStore.getState().setPendingAutoMessage(null)
    // Read the latest streaming/awaiting state from refs; the closure values
    // here are from the render when the message arrived and can be stale.
    if (isStreamingRef.current || awaitingRef.current) {
      // The message is not queued anywhere — dropping it silently would
      // lose the caller's request, so say why it was not sent.
      toastError(t('chat.toast.autoMessageBusy'))
      return
    }
    handleDirectSend(pendingAutoMessage.text)
  }, [pendingAutoMessage])

  /** Send a message programmatically (not from user input). */
  async function handleDirectSend(text) {
    if (!text || isStreamingRef.current || awaitingRef.current || !projectId || !sessionId) return
    setIsStreaming(true)
    setMessages(prev => [...prev, { role: 'user', content: displayMessageText(text, t('chat.planDisplay')), created_at: new Date().toISOString() }])
    setMessages(prev => [...prev, { role: 'SiGMA', content: '', process: [], localId: nextLiveTurnId() }])
    setStreamPhase('thinking')
    resetThinkingLive()
    const handle = beginStreamHandle()
    try {
      const body = await chatAPI.stream(projectId, {
        message: text,
        session_id: sessionId,
        ...(getUserState ? { user_state: getUserState() } : {}),
        ...(tokenBudget ? { token_budget: tokenBudget } : {}),
      }, handle.controller.signal)
      if (handle.controller.signal.aborted) return
      const reader = body.getReader()
      const decoder = new TextDecoder()
      await processSSEStream(reader, decoder, handle)
    } catch (err) {
      if (err.name === 'AbortError') return
      toastError(t('chat.toast.connectionFailed', { message: err.message || '' }))
    } finally {
      // Refresh history to get real message IDs and can_edit flags from the
      // server before the streaming flag drops — guarded against pausing by
      // the shared helper.
      await finalizeStreamTurn(handle)
    }
  }
  function startEditTitle() {
    setEditTitle(sessionTitle || t('chat.untitled'))
    setIsEditingTitle(true)
  }

  async function submitTitle() {
    const trimmed = editTitle.trim()
    if (trimmed && trimmed !== sessionTitle && sessionId) {
      try {
        await chatAPI.updateSession(projectId, sessionId, { title: trimmed })
        setSessionTitle(trimmed)
        setSessions(prev => prev.map(s => s.id === sessionId ? { ...s, title: trimmed } : s))
      } catch { /* ignore */ }
    }
    setIsEditingTitle(false)
  }

  function cancelEditTitle() {
    setIsEditingTitle(false)
  }

  function handleTitleKeyDown(e) {
    if (e.key === 'Enter') submitTitle()
    else if (e.key === 'Escape') cancelEditTitle()
  }

  // ---- Auto-generate title after first exchange ----
  async function maybeGenerateTitle() {
    const isDefault = !sessionTitle || /^Untitled(-\d+)?$/.test(sessionTitle)
    if (!sessionId || !isDefault) return
    try {
      const result = await chatAPI.generateTitle(projectId, sessionId)
      if (result?.title) {
        setSessionTitle(result.title)
        setSessions(prev => prev.map(s => s.id === sessionId ? { ...s, title: result.title } : s))
      }
    } catch { /* ignore */ }
  }

  // ---- Session actions ----
  function switchToSession(sid) {
    if (sid === sessionId) { setShowDropdown(false); return }
    // Only update state — useEffect([projectId, sessionId]) handles everything:
    // aborts old stream, loads new history, checks active task, reconnects SSE.
    storage.setSession(projectId, sid)
    setSessionId(sid)
    setShowDropdown(false)
  }

  async function archiveSession(sid, e) {
    e.stopPropagation()
    try {
      await chatAPI.updateSession(projectId, sid, { is_archived: true })
      storage.removeSession(projectId, sid)
      if (sid === sessionId) {
        const freshList = await chatAPI.listSessions(projectId)
        const remaining = freshList.filter(s => s.id !== sid && !s.is_archived)
        if (remaining.length > 0) {
          switchToSession(remaining[0].id)
        } else {
          const s = await chatAPI.createSession(projectId)
          switchToSession(s.id)
        }
      } else {
        await loadSessions()
      }
    } catch (e) { toastError(t('chat.toast.archiveFailed')) }
  }

  async function deleteSessionAction(sid, e) {
    e.stopPropagation()
    setDeleteSessionTarget(sid)
  }

  async function confirmDeleteSession() {
    if (!deleteSessionTarget) return
    const sid = deleteSessionTarget
    try {
      await chatAPI.deleteSession(projectId, sid)
      storage.removeSession(projectId, sid)
      if (sid === sessionId) {
        const freshList = await chatAPI.listSessions(projectId)
        const remaining = freshList.filter(s => s.id !== sid && !s.is_archived)
        if (remaining.length > 0) {
          switchToSession(remaining[0].id)
        } else {
          const s = await chatAPI.createSession(projectId)
          switchToSession(s.id)
        }
      } else {
        await loadSessions()
      }
    } catch (e) { toastError(t('chat.toast.deleteFailed')) }
    setDeleteSessionTarget(null)
  }

  async function createNewSession() {
    try {
      const s = await chatAPI.createSession(projectId)
      setShowDropdown(false)
      switchToSession(s.id)
    } catch (e) { toastError(t('chat.toast.createFailed')) }
  }

  // Fork the session starting from message m: the new session receives m and
  // everything before it, then becomes the active session.
  async function forkFromMessage(m) {
    const forkTitle = sessionTitle ? t('chat.forkTitlePrefix') + sessionTitle : ''
    try {
      const s = await chatAPI.forkSession(projectId, sessionId, m.id, forkTitle)
      setShowDropdown(false)
      switchToSession(s.id)
    } catch (e) { toastError(t('chat.toast.forkFailed')) }
  }

  // /clear: delete the current session and create a fresh one in its place.
  // Unlike confirmDeleteSession (which prefers a remaining sibling), clear
  // always starts a blank session.
  async function clearCurrentSession() {
    if (!sessionId) return
    try {
      await chatAPI.deleteSession(projectId, sessionId)
      storage.removeSession(projectId, sessionId)
      const s = await chatAPI.createSession(projectId)
      switchToSession(s.id)
    } catch (e) { toastError(t('chat.toast.clearFailed')) }
  }

  // /skill <id>: inject a completed skill_load turn into the session (no LLM call).
  // Backend validates the id and enabled state; on success we reload history so
  // the injected user/assistant/tool/assistant turn renders via existing paths.
  async function handleLoadSkill(skillId) {
    if (isStreaming || !projectId || !sessionId || !skillId) return
    try {
      const res = await chatAPI.loadSkill(projectId, sessionId, skillId)
      await refreshLatestHistory()
      toastSuccess(t('chat.toast.skillLoaded', { name: res?.name || skillId }))
    } catch (e) {
      toastError(t('chat.toast.skillLoadFailed'))
    }
  }

  // ---- Archived sessions ----
  // *focus* (from a search hit) expands the target session immediately;
  // the archivedFocus effect scrolls to the message once it renders.
  async function openArchived(focus = null) {
    setShowDropdown(false)
    try {
      const list = await chatAPI.listSessions(projectId, { include_archived: true })
      const archived = list.filter(s => s.is_archived)
      setArchivedSessions(archived)
      setShowArchived(true)
      if (focus) {
        // Only row-level hits need staging; session-level jumps land on the
        // expanded session itself. Staged refs are consumed once rendered —
        // or dropped on failure so they cannot fire for a later manual open.
        if (focus.messageId) archivedFocusRef.current = focus
        if (focus.sessionId !== expandedArchivedId) {
          try {
            const msgs = await chatAPI.getSessionMessages(projectId, focus.sessionId)
            setArchivedMessages(msgs)
            setExpandedArchivedId(focus.sessionId)
          } catch {
            archivedFocusRef.current = null
            toastError(t('chat.toast.loadMessagesFailed'))
          }
        }
      }
    } catch { /* ignore */ }
  }

  // Scroll the archive modal to a search hit once its messages render.
  useEffect(() => {
    const focus = archivedFocusRef.current
    if (!showArchived || !focus?.messageId) return
    if (!archivedMessages.some(m => m.id === focus.messageId)) return
    archivedFocusRef.current = null
    const node = document.querySelector(`[data-archived-msg-id="${CSS.escape(focus.messageId)}"]`)
    if (node) {
      node.scrollIntoView({ block: 'center', behavior: 'smooth' })
      flashMessage(focus.messageId)
    }
  }, [showArchived, archivedMessages, flashMessage])

  // Global Ctrl/Cmd+K opens chat search — the dropdown entry is two clicks
  // away. Only a visible panel responds: the shared panel is display:none
  // while its tab is inactive, and hidden elements have no offsetParent.
  useEffect(() => {
    const handler = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        if (panelRootRef.current?.offsetParent != null) openSearch()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // ---- Chat search ----
  function openSearch() {
    setShowDropdown(false)
    setSearchOpen(true)
  }

  function handleSearchJump(session, match) {
    setSearchOpen(false)
    if (session.is_archived) {
      openArchived({ sessionId: session.id, messageId: match?.id || null })
      return
    }
    if (session.id === sessionId) {
      if (match) jumpToMessage(match.id)
      return
    }
    // Switch first; the pending jump lands after the new session's initial
    // history commit (see the pendingJump effect). Without a match the
    // switch itself is the whole navigation.
    pendingJumpRef.current = match ? { sessionId: session.id, messageId: match.id } : null
    switchToSession(session.id)
  }

  async function toggleArchivedMessages(sid) {
    if (expandedArchivedId === sid) {
      setExpandedArchivedId(null)
      setArchivedMessages([])
      return
    }
    try {
      const msgs = await chatAPI.getSessionMessages(projectId, sid)
      setArchivedMessages(msgs)
      setExpandedArchivedId(sid)
    } catch { toastError(t('chat.toast.loadMessagesFailed')) }
  }

  async function unarchiveSession(sid) {
    try {
      await chatAPI.updateSession(projectId, sid, { is_archived: false })
      setArchivedSessions(prev => prev.filter(s => s.id !== sid))
      setExpandedArchivedId(null)
      await loadSessions()
    } catch { toastError(t('chat.toast.unarchiveFailed')) }
  }

  async function deleteArchivedSession(sid) {
    setDeleteArchivedTarget(sid)
  }

  async function confirmDeleteArchived() {
    if (!deleteArchivedTarget) return
    const sid = deleteArchivedTarget
    try {
      await chatAPI.deleteSession(projectId, sid)
      storage.removeSession(projectId, sid)
      setArchivedSessions(prev => prev.filter(s => s.id !== sid))
      setExpandedArchivedId(null)
    } catch { toastError(t('chat.toast.deleteFailed')) }
    setDeleteArchivedTarget(null)
  }

  function startEditArchived(session) {
    setEditingArchivedId(session.id)
    setEditArchivedTitle(session.title)
  }

  async function submitArchivedTitle(sid) {
    const trimmed = editArchivedTitle.trim()
    if (trimmed) {
      try {
        await chatAPI.updateSession(projectId, sid, { title: trimmed })
        setArchivedSessions(prev => prev.map(s => s.id === sid ? { ...s, title: trimmed } : s))
      } catch { /* ignore */ }
    }
    setEditingArchivedId(null)
  }

  // ---- Process SSE stream (unified parser from utils/sse.js) ----
  // NOTE: This function does NOT manage isStreaming. The caller is responsible
  // for setting isStreaming=true before calling and isStreaming=false in a
  // finally block when the returned promise resolves.

  function resetThinkingLive() {
    segOpenRef.current = false
    liveBufferRef.current = ''
    liveFlushAtRef.current = 0
    thinkFollowRef.current = true
    setThinkingLive('')
  }

  /** Cross-check the backend while a stream is open: if the server reports no
   *  active task but no terminal event ever arrived (lost done, zombie stream),
   *  finalize from server truth instead of spinning forever. Restores a missed
   *  interaction checkpoint on the way. */
  async function verifyStreamAlive(handle) {
    if (handle.controller.signal.aborted || !projectId || !sessionId) return
    let active = null
    try {
      active = await chatAPI.getActive(projectId, sessionId, { signal: AbortSignal.timeout(STREAM_PROBE_TIMEOUT_MS) })
    } catch {
      // A blackholed connection hangs this probe too. Count consecutive
      // failures and only give up after several: a single blip must not tear
      // down a healthy stream. Once exceeded, abort the handle so the stuck
      // read loop unblocks and runs the normal drop recovery (resume).
      handle.probeFailures += 1
      if (handle.probeFailures < STREAM_PROBE_MAX_FAILURES) return
      if (streamHandleRef.current !== handle) return
      handle.probeDead = true
      handle.controller.abort()
      return
    }
    handle.probeFailures = 0
    // Aborted, or a newer stream owns the handle slot now (interaction
    // resume, next turn) — stand down instead of touching their state.
    if (handle.controller.signal.aborted || streamHandleRef.current !== handle) return
    if (restoreInteractionFromActive(active)) return
    if (active?.active) return
    try {
      await refreshLatestHistory()
    } catch { /* best-effort */ }
    // Re-check ownership: a takeover may have landed during the refresh.
    if (streamHandleRef.current !== handle) return
    setIsStreaming(false)
    handle.controller.abort()
  }

  /** Reopen the SSE stream after it dropped without a terminal event. Gives up
   *  (returning null) when the turn finished server-side, paused for input, or
   *  cannot be resumed — the caller's finally reloads history either way. A
   *  failing probe means the network is still down, not that the turn ended,
   *  so it is retried before giving up; the final give-up is toasted. */
  async function resumeBrokenStream(handle) {
    if (!projectId || !sessionId) return null
    // A probe abort only unblocks the read loop — the resume itself needs a
    // live connection, so swap in a fresh controller. Any other abort is
    // user/session-initiated: do not resume over it.
    if (handle.controller.signal.aborted) {
      if (!handle.probeDead) return null
      handle.probeDead = false
      handle.controller = new AbortController()
      handle.probeFailures = 0
    }
    await new Promise(resolve => setTimeout(resolve, STREAM_RECOVERY_DELAY_MS))
    if (handle.controller.signal.aborted || streamHandleRef.current !== handle) return null
    let active = null
    let probeFailures = 0
    while (true) {
      try {
        active = await chatAPI.getActive(projectId, sessionId, { signal: AbortSignal.timeout(STREAM_PROBE_TIMEOUT_MS) })
        break
      } catch {
        probeFailures += 1
        if (probeFailures >= STREAM_PROBE_RECOVERY_ATTEMPTS
          || handle.controller.signal.aborted
          || streamHandleRef.current !== handle) {
          if (streamHandleRef.current === handle && !handle.controller.signal.aborted) {
            toastError(t('chat.toast.resumeFailed'))
            // Budget exhausted while the task is (at best) still running —
            // keep the streaming state instead of dropping to a false idle.
            handle.recoveryGaveUp = true
          }
          return null
        }
        await new Promise(resolve => setTimeout(resolve, STREAM_RECOVERY_DELAY_MS))
      }
    }
    if (handle.controller.signal.aborted || streamHandleRef.current !== handle) return null
    if (restoreInteractionFromActive(active)) return null
    if (!active?.active) return null
    // Reattach only while the task is still executing (same gate as session
    // load). An awaiting_input task has a parked stream: subscribing to it
    // yields nothing until the server idle-timeout surfaces a terminal error
    // that would clobber the finished turn.
    if (active.status !== 'running' && active.status !== 'queued' && active.status !== 'cancelling') return null
    // Prefer the id our stream reported; the active-task payload covers a
    // drop before the task_id event was processed.
    const taskId = taskIdRef.current || active.task_id
    if (!taskId) return null
    try {
      // Cursor resume: only events with id > lastSeq are replayed, so
      // nothing already applied is duplicated and nothing after the drop
      // is lost.
      const body = await chatAPI.resumeStream(projectId, taskId, handle.controller.signal, handle.lastSeq)
      if (handle.controller.signal.aborted) return null
      // Fresh connection and a clean probe budget for the next watchdog cycle.
      handle.probeFailures = 0
      return { reader: body.getReader(), decoder: new TextDecoder() }
    } catch (err) {
      if (handle.controller.signal.aborted || streamHandleRef.current !== handle) return null
      // The task is still running but the reconnect failed — say so instead
      // of silently freezing on a half-rendered turn, and keep the streaming
      // state so the UI matches the server.
      toastError(err?.message || t('chat.toast.resumeFailed'))
      handle.recoveryGaveUp = true
      return null
    }
  }

  /** Open an interaction checkpoint delivered by awaiting_input: permission
   * gates go straight to the store dialog; other interactive tools defer
   * through the ref consumed by the dispatch effect below. */
  function dispatchInteraction(interactionData) {
    if (interactionData.interaction_type === 'permission') {
      useStore.getState().setPendingPermission({ ...interactionData, project_id: projectId, session_id: sessionId })
    } else {
      pendingInteractionRef.current = { type: interactionData.interaction_type, data: { ...interactionData, project_id: projectId }, sessionId, projectId }
    }
  }

  async function processSSEStream(reader, decoder, handle) {
    resetThinkingLive()
    let sawTerminal = false
    let reconnects = 0
    let appliedAny = false
    // True between a gap frame and the checkpoint reload it triggers: the
    // reconnecting cursor predates the oldest buffered event, so mid-events
    // were evicted and the live view has a hole replay cannot fill.
    // replayUntil is the gap frame's highest replayed seq: replayed frames up
    // to it are already covered by the reloaded checkpoint, so the seq gate —
    // not reload completion — decides when appending resumes. A missing value
    // falls back to dropping until the reload completes.
    let reloadingAfterGap = false
    let replayUntil = null
    const watchdogTimer = setInterval(() => {
      void verifyStreamAlive(handle)
    }, STREAM_WATCHDOG_INTERVAL_MS)
    try {
      while (true) {
        sawTerminal = false
        appliedAny = false
        const parser = createSSEStreamParser({
          onEvent: (type, data, id) => {
            if (streamHandleRef.current !== handle) return   // stale stream — drop
            if (Number.isInteger(id)) {
              // While a gap reload is in flight — and for every replayed
              // frame up to the gap's replay_until, even after the reload
              // lands — the reloaded checkpoint already covers the event:
              // drop it but keep the cursor advancing so frames arriving
              // after the replay batch pass the gate. Frames beyond
              // replay_until are genuinely live and append even mid-reload.
              // The small hole at the seam is corrected by the finalize-time
              // history refresh.
              if (reloadingAfterGap || (replayUntil !== null && id <= replayUntil)) {
                handle.lastSeq = id
                return
              }
              // Overlapping replay (full-buffer resume, duplicated delivery):
              // skip anything already applied so rendered text never doubles.
              if (id <= handle.lastSeq) return
              handle.lastSeq = id
              appliedAny = true
            } else {
              // Control frames are never entered into the server's replay
              // buffer, so the seq gate above cannot apply to them. Known
              // control types are handled here; anything else is dropped
              // loudly instead of guessed at.
              if (type === 'error' && data?.reason === 'idle_timeout') {
                // Stream-health signal, not a failure: the server merely
                // stopped seeing traffic. Neither render the error nor end
                // the turn — the reconnect below reattaches to the
                // still-running task with the cursor.
                return
              }
              if (type === 'gap') {
                // Rebuild the turn from the server's authoritative history
                // (the persisted checkpoint covers the evicted range), then
                // resume appending once the stale replay batch has passed.
                // replay_until bounds that batch; recompute per gap so a
                // later reconnect's gap always governs its own batch.
                replayUntil = Number.isInteger(data?.replay_until) ? data.replay_until : null
                if (!reloadingAfterGap) {
                  reloadingAfterGap = true
                  refreshLatestHistory()
                    .catch(() => { /* best-effort: the finalize-time refresh reconciles */ })
                    .finally(() => { reloadingAfterGap = false })
                }
                return
              }
              if (type !== 'task_id' && type !== 'done' && type !== 'error' && type !== 'cancelled') {
                console.warn('[Chat] Dropping SSE frame without a seq id:', type)
                return
              }
            }
            if (type === 'done' || type === 'error' || type === 'cancelled') sawTerminal = true
            handleStreamEvent(type, data)
          },
        })
        await parser.start(reader, decoder, handle.controller.signal)
        if (sawTerminal) return
        // The controller aborts on session switch / takeover (stop resuming)
        // and on a watchdog probe timeout (blackholed connection — the abort
        // only unblocks the read below; the resume rebuilds the connection).
        if (handle.controller.signal.aborted && !handle.probeDead) return
        // Stream ended with no terminal event — the connection dropped
        // mid-turn. Reconnect while the server still reports an active task;
        // give up after a few consecutive unproductive tries and let the
        // caller reload history. A connection that replayed at least one
        // fresh (not already applied) event made real progress, so it does
        // not count toward the bound.
        if (appliedAny) reconnects = 0
        reconnects += 1
        if (reconnects > STREAM_RECOVERY_MAX_ATTEMPTS) {
          if (streamHandleRef.current === handle && !handle.controller.signal.aborted) {
            toastError(t('chat.toast.resumeFailed'))
            // Budget exhausted while the task was just re-probed as active —
            // keep the streaming state instead of dropping to a false idle.
            handle.recoveryGaveUp = true
          }
          return
        }
        const resumed = await resumeBrokenStream(handle)
        if (!resumed) return
        reader = resumed.reader
        decoder = resumed.decoder
        // Reconnecting mid-task: assume thinking until the next SSE event
        // reports the actual phase.
        setStreamPhase('thinking')
      }
    } finally {
      clearInterval(watchdogTimer)
    }
  }

  function handleStreamEvent(type, data) {
    const isTerminal = type === 'done' || type === 'error' || type === 'cancelled'
    if (isTerminal) {
      clearStopAbortTimer()
      taskIdRef.current = null
      stopRequestedRef.current = false
    }
    // Capture backend task_id for cancellation
    if (type === 'task_id') {
      taskIdRef.current = data.task_id
      stopRequestedRef.current = false
      abnormalTerminalRef.current = false
      return
    }
    if (type === 'context_stats') {
      setContextStats(data)
      return
    }
    // Task list updates — Zustand only, no messages to modify
    if (type === 'task_list') {
      const tasks = data.tasks || []
      useStore.getState().setTaskList(tasks)
      if (tasks.length > 0) {
        useStore.getState().setExpandedTasks(true)
      }
      return
    }
    // Progressive usage update — refresh token stats after each LLM call
    if (type === 'turn_usage') {
      if (data.usage) {
        setMessages(prev => {
          const newMsgs = [...prev]
          const targetIdx = findLiveTargetIndex(newMsgs)
          if (targetIdx < 0) return prev
          newMsgs[targetIdx] = { ...newMsgs[targetIdx], usage: data.usage }
          return newMsgs
        })
      }
      return
    }
    // Auto-generate title on first exchange completion
    if (type === 'done') {
      if (!abnormalTerminalRef.current) maybeGenerateTitle()
    } else if (type === 'error' || type === 'cancelled') {
      abnormalTerminalRef.current = true
    }
    // Reasoning deltas — feed the live thinking window shown under the
    // spinner while the phase is "thinking". Each stretch of reasoning is
    // one segment: opened on the first delta, dropped (never appended to
    // the timeline) when a later non-reasoning event closes it. The
    // window only re-renders on the flush cadence.
    if (type === 'thought' && data.content) {
      setStreamPhase('thinking')
      const now = Date.now()
      if (!segOpenRef.current) {
        segOpenRef.current = true
        liveBufferRef.current = ''
      }
      liveBufferRef.current += data.content
      if (now - liveFlushAtRef.current >= THINK_FLUSH_MS) {
        liveFlushAtRef.current = now
        setThinkingLive(thinkingTail(liveBufferRef.current))
      }
      return
    }
    // A non-passive event ends reasoning — the live window is transient
    // by design (shown while thinking, dropped the moment it ends), so
    // just reset the segment; nothing is appended to the timeline.
    if (type !== 'thought' && !THINK_PASSTHROUGH_TYPES.has(type)) {
      resetThinkingLive()
    }
    if (type in STREAM_PHASE_BY_EVENT) setStreamPhase(STREAM_PHASE_BY_EVENT[type])
    setMessages(prev => {
      const newMsgs = [...prev]
      const targetIdx = findLiveTargetIndex(newMsgs)
      if (targetIdx < 0) return prev
      const targetMsg = { ...newMsgs[targetIdx] }
      const currentProcess = [...(targetMsg.process || [])]

      if (type === 'stream_status') {
        const statusMessage = streamStatusText(data, t)
        if (statusMessage) {
          const processForStatus = data.status === 'retrying'
            ? currentProcess.filter(s => s.type !== 'streaming_text')
            : currentProcess
          targetMsg.process = withTransientHint(processForStatus, statusMessage, {
            retry: data.status === 'retrying',
            error: data.status === 'retrying' ? data.error : undefined,
          })
        } else {
          targetMsg.process = currentProcess
        }
      } else if (type === 'delta') {
        const streamIdx = currentProcess.findLastIndex(s => s.type === 'streaming_text')
        if (streamIdx >= 0) {
          currentProcess[streamIdx] = { ...currentProcess[streamIdx], content: currentProcess[streamIdx].content + data.content }
        } else {
          currentProcess.push({ type: 'streaming_text', content: data.content })
        }
        targetMsg.process = currentProcess
      } else if (type === 'tool_start') {
        const finalProcess = currentProcess.map(s =>
          s.type === 'streaming_text' ? { type: 'hint', content: s.content } : s
        )
        const toolStep = { type: 'tool', tool: data.tool, params: data.params, status: 'running' }
        // Store tool_call_id for agent_event matching
        if (data.tool_call_id) {
          toolStep._toolCallId = data.tool_call_id
          toolStep.toolCallId = data.tool_call_id
        }
        // Upsert by tool_call_id: a replayed tool_start (cursor resume,
        // duplicated delivery) must not render a second step.
        const dupIdx = data.tool_call_id
          ? finalProcess.findIndex(s => s.type === 'tool' && s._toolCallId === data.tool_call_id)
          : -1
        if (dupIdx >= 0) finalProcess[dupIdx] = toolStep
        else finalProcess.push(toolStep)
        targetMsg.process = finalProcess
      } else if (type === 'tool_end') {
        const updated = currentProcess.map(s => {
          // Match by tool_call_id first, then by tool name + running status
          if (data.tool_call_id && s._toolCallId === data.tool_call_id) {
            return finishToolStep(s, data)
          }
          if (s.type === 'tool' && s.tool === data.tool && s.status === 'running' && !data.tool_call_id) {
            return finishToolStep(s, data)
          }
          return s
        })
        targetMsg.process = updated
      } else if (type === 'compact_start') {
        targetMsg.process = withTransientHint(currentProcess, t('chat.compacting'))
      } else if (type === 'compact_done') {
        // With the summary the step renders as the same expandable card
        // the history view shows after refresh; backends that predate the
        // summary field get the plain hint.
        currentProcess.push(data.summary
          ? { type: 'compact', content: data.summary }
          : { type: 'hint', content: t('chat.compacted'), plain: true })
        targetMsg.process = currentProcess
      } else if (type === 'agent_event') {
        // Subagent SSE event — nest inside the agent tool step with matching tool_call_id
        const parentTcId = data.parent_tool_call_id
        let agentStepIdx = -1
        if (parentTcId) {
          // Match by tool_call_id (robust)
          agentStepIdx = currentProcess.findIndex(
            s => s.type === 'tool' && isAgentToolName(s.tool) && s.toolCallId === parentTcId
          )
          if (agentStepIdx < 0) {
            // Fallback: match by tool_call_id stored at tool_start time
            agentStepIdx = currentProcess.findIndex(
              s => s.type === 'tool' && isAgentToolName(s.tool) && s.status === 'running' && s._toolCallId === parentTcId
            )
          }
        }
        if (agentStepIdx < 0) {
          // Last resort: the last agent step still able to receive events —
          // running live, or reloaded from history while parked (its subagent
          // resumes into it; the history-built step carries a tool_call_id).
          agentStepIdx = currentProcess.findLastIndex(
            s => s.type === 'tool' && isAgentToolName(s.tool)
              && (s.status === 'running' || s.status === 'awaiting_input')
          )
        }
        if (agentStepIdx < 0 && parentTcId) {
          // The parent agent step exists nowhere: neither in rebuilt history
          // (the anchor row was not yet persisted when this page was served)
          // nor in the replay buffer (evicted past the catch-up cap).
          // Synthesize the running step so live subagent progress keeps
          // rendering after a refresh instead of being silently dropped. A
          // replayed tool_start for the same id replaces it; tool_end closes
          // it in place via the stored _toolCallId.
          currentProcess.push({
            type: 'tool', tool: 'agent', params: {},
            status: 'running', toolCallId: parentTcId, _toolCallId: parentTcId,
          })
          agentStepIdx = currentProcess.length - 1
        }
        if (agentStepIdx >= 0) {
          const agentStep = { ...currentProcess[agentStepIdx] }
          // A live agent_event is proof the subagent is running: a step
          // rebuilt from history mid-run ("interrupted" — its result row
          // does not exist until the round completes) flips to running on
          // first contact, on every rebuild path uniformly.
          if (agentStep.status === 'interrupted') agentStep.status = 'running'
          const subSteps = [...(agentStep.subSteps || [])]
          const innerType = data.inner_type
          const innerData = data.inner_data || {}

          if (innerType === 'delta') {
            const lastSub = subSteps.length > 0 ? subSteps[subSteps.length - 1] : null
            if (lastSub && lastSub.type === 'streaming_text') {
              subSteps[subSteps.length - 1] = { ...lastSub, content: lastSub.content + (innerData.content || '') }
            } else {
              subSteps.push({ type: 'streaming_text', content: innerData.content || '' })
            }
          } else if (innerType === 'compact_start') {
            subSteps.splice(0, subSteps.length, ...withTransientHint(subSteps, t('chat.compacting')))
          } else if (innerType === 'compact_done') {
            subSteps.push(innerData.summary
              ? { type: 'compact', content: innerData.summary }
              : { type: 'hint', content: t('chat.compacted') })
          } else if (innerType === 'stream_status') {
            const statusMessage = streamStatusText(innerData, t)
            if (statusMessage) {
              const subStepsForStatus = innerData.status === 'retrying'
                ? subSteps.filter(s => s.type !== 'streaming_text')
                : subSteps
              subSteps.splice(0, subSteps.length, ...withTransientHint(subStepsForStatus, statusMessage))
            }
          } else if (innerType === 'tool_start') {
            const subStep = { type: 'tool', tool: innerData.tool, params: innerData.params, status: 'running' }
            if (innerData.tool_call_id) {
              subStep._toolCallId = innerData.tool_call_id
            }
            // Upsert by tool_call_id: a replayed subagent tool_start must not
            // render a second step (same duplicate risk as the top level).
            const dupIdx = innerData.tool_call_id
              ? subSteps.findIndex(s => s.type === 'tool' && s._toolCallId === innerData.tool_call_id)
              : -1
            if (dupIdx >= 0) subSteps[dupIdx] = subStep
            else subSteps.push(subStep)
          } else if (innerType === 'tool_end') {
            // Match by tool_call_id first, then by tool name
            let matched = false
            if (innerData.tool_call_id) {
              for (let i = subSteps.length - 1; i >= 0; i--) {
                if (subSteps[i].type === 'tool' && subSteps[i]._toolCallId === innerData.tool_call_id) {
                  subSteps[i] = finishToolStep(subSteps[i], innerData)
                  matched = true
                  break
                }
              }
            }
            if (!matched) {
              for (let i = subSteps.length - 1; i >= 0; i--) {
                if (subSteps[i].type === 'tool' && subSteps[i].tool === innerData.tool && subSteps[i].status === 'running') {
                  subSteps[i] = finishToolStep(subSteps[i], innerData)
                  break
                }
              }
            }
          } else if (innerType === 'awaiting_input') {
            subSteps.push({ type: 'awaiting_input', interaction_type: innerData.interaction_type, data: innerData, transient: true })
          }

          agentStep.subSteps = subSteps
          agentStep.agentType = data.agent_type
          agentStep.agentRunId = data.agent_run_id
          currentProcess[agentStepIdx] = agentStep
        }
        targetMsg.process = currentProcess
      } else if (type === 'awaiting_input') {
        currentProcess.push({ type: 'awaiting_input', interaction_type: data.interaction_type, data: data, transient: true })
        targetMsg.process = currentProcess
      } else if (type === 'done') {
        const streamIdx = currentProcess.findLastIndex(s => s.type === 'streaming_text')
        if (streamIdx >= 0) {
          targetMsg.content = currentProcess[streamIdx].content
        } else if (!targetMsg.content && currentProcess.some(s => s.type === 'compact' || (s.type === 'hint' && s.plain))) {
          targetMsg.content = t('chat.compacted')
        }
        targetMsg.created_at = new Date().toISOString()
        const duration = turnDurationMs(newMsgs, targetIdx)
        if (duration != null) targetMsg.durationMs = duration
        if (data.usage) {
          targetMsg.usage = data.usage
        }
        const lastStep = currentProcess[currentProcess.length - 1]
        const isPausingForInput = lastStep?.type === 'awaiting_input'
        if (isPausingForInput) {
          targetMsg.process = currentProcess.filter(s => s.type !== 'streaming_text')
        } else {
          targetMsg.process = currentProcess.filter(s => !s.transient && s.type !== 'streaming_text')
        }
      } else if (type === 'error') {
        const content = data.content || data.error || data.message || t('chat.toast.unknownError')
        targetMsg.content = content
        targetMsg.created_at = new Date().toISOString()
        const duration = turnDurationMs(newMsgs, targetIdx)
        if (duration != null) targetMsg.durationMs = duration
        targetMsg.process = currentProcess.filter(s => !s.transient && s.type !== 'streaming_text')
        if (data.usage) {
          targetMsg.usage = data.usage
        }
      } else if (type === 'cancelled') {
        targetMsg.interrupted = true
        const duration = turnDurationMs(newMsgs, targetIdx)
        if (duration != null) targetMsg.durationMs = duration
        if (data.usage) {
          targetMsg.usage = data.usage
        }
      }

      newMsgs[targetIdx] = targetMsg
      return newMsgs
    })
    // Side effects are decided from type/data only — never from values
    // written inside the setMessages updater, which React may not run
    // until the next render when updates are batched. The interaction
    // dispatch is unconditional even for agent_event: the dialog must
    // open even when its visual step cannot attach to an agent bubble.
    if (type === 'compact_done') {
      setContextStats(data)
      refreshCanEditFlags()
    } else if (type === 'agent_event' && data.inner_type === 'compact_done') {
      setContextStats(data.inner_data || {})
      refreshCanEditFlags()
    }
    if (type === 'awaiting_input') {
      dispatchInteraction(data)
    } else if (type === 'agent_event' && data.inner_type === 'awaiting_input') {
      dispatchInteraction(data.inner_data || {})
    }
    if (type === 'file_changed' && onFileChanged) onFileChanged(data.paths || [])
    if (type === 'annotation_changed' && onAnnotationChanged) onAnnotationChanged(data.file_path || '')
  }

  function clearStopAbortTimer() {
    if (stopAbortTimerRef.current) {
      clearTimeout(stopAbortTimerRef.current)
      stopAbortTimerRef.current = null
    }
  }

  async function showStopStillRunning() {
    clearStopAbortTimer()
    stopRequestedRef.current = false
    toastError(t('chat.toast.stopStillRunning'))
    if (projectId && sessionId) {
      try {
        await refreshLatestHistory()
      } catch { /* best-effort */ }
    }
  }

  async function abortStoppedStream(handle) {
    handle.controller.abort()
    // Only tear down UI ownership while this handle still owns the stream:
    // an interaction answer racing the stop may have replaced it, and the
    // successor must keep its streaming state.
    if (streamHandleRef.current === handle) {
      streamHandleRef.current = null
      setIsStreaming(false)
    }
    stopAbortTimerRef.current = null
    stopRequestedRef.current = false
    if (projectId && sessionId) {
      try {
        await refreshLatestHistory()
      } catch { /* best-effort */ }
    }
  }

  // ---- Stop an ongoing stream ----
  async function handleStop() {
    if (stopRequestedRef.current) return

    let taskId = taskIdRef.current
    const handle = streamHandleRef.current
    if (!projectId) {
      streamHandleRef.current = null
      clearStopAbortTimer()
      stopRequestedRef.current = false
      setIsStreaming(false)
      return
    }
    if (!handle) {
      stopRequestedRef.current = true
      if (sessionId) {
        try {
          const active = await chatAPI.getActive(projectId, sessionId)
          if (active?.active) {
            const activeTaskId = active.task_id || taskId
            if (activeTaskId) {
              taskIdRef.current = activeTaskId
              try { await chatAPI.cancel(projectId, activeTaskId) } catch (e) { console.warn('Failed to cancel task:', e) }
            }
            await showStopStillRunning()
            return
          }
        } catch {
          await showStopStillRunning()
          return
        }
      }
      if (taskId) {
        try { await chatAPI.cancel(projectId, taskId) } catch (e) { console.warn('Failed to cancel task:', e) }
        await showStopStillRunning()
        return
      }
      clearStopAbortTimer()
      stopRequestedRef.current = false
      setIsStreaming(false)
      return
    }

    stopRequestedRef.current = true
    if (!taskId && sessionId) {
      for (let attempt = 0; attempt < 3 && !taskId; attempt += 1) {
        try {
          const active = await chatAPI.getActive(projectId, sessionId)
          if (active?.active && active.task_id) {
            taskId = active.task_id
            taskIdRef.current = taskId
            break
          }
        } catch {
          break
        }
        if (attempt < 2) {
          await new Promise(resolve => setTimeout(resolve, 300))
        }
      }
    }

    if (!taskId) {
      // No backend task to cancel (e.g. the stream request never reached the
      // server). Abort the held connection and reset the streaming state —
      // the same cleanup the no-handle path performs — so a wedged turn is
      // always recoverable instead of staying stuck behind a toast.
      await abortStoppedStream(handle)
      return
    }

    try { await chatAPI.cancel(projectId, taskId) } catch (e) { console.warn('Failed to cancel task:', e) }

    // Keep the SSE stream open so the backend can deliver cancelled/error
    // events. If the in-process task is still running after the cancel
    // request, surface that instead of pretending the stop succeeded.
    const scheduleStopFallback = (attempt = 0) => {
      clearStopAbortTimer()
      stopAbortTimerRef.current = setTimeout(async () => {
        if (streamHandleRef.current !== handle) return
        if (!sessionId) {
          await showStopStillRunning()
          return
        }
        try {
          const active = await chatAPI.getActive(projectId, sessionId)
          if (active?.active) {
            const activeTaskId = active.task_id || taskId
            if (activeTaskId) {
              taskIdRef.current = activeTaskId
              try { await chatAPI.cancel(projectId, activeTaskId) } catch (e) { console.warn('Failed to cancel task:', e) }
            }
            if (attempt < 2) {
              scheduleStopFallback(attempt + 1)
            } else {
              await showStopStillRunning()
            }
            return
          }
        } catch {
          await showStopStillRunning()
          return
        }
        await abortStoppedStream(handle)
      }, 10000)
    }
    scheduleStopFallback()
  }

  /** Cancel a turn parked on user input (awaiting_input). No SSE stream is
   *  attached to a parked turn, so the keep-stream-open logic in handleStop
   *  does not apply: request the cancel, then drop the dialog state right
   *  away. The backend turns awaiting_input into cancelled and clears the
   *  checkpoint, so the history refresh below settles the turn as idle. */
  async function handleCancelAwaiting() {
    if (!projectId || !sessionId) return
    let taskId = taskIdRef.current
    if (!taskId) {
      try {
        const active = await chatAPI.getActive(projectId, sessionId)
        if (active?.active && active.task_id) {
          taskId = active.task_id
          taskIdRef.current = taskId
        }
      } catch (e) { console.warn('Failed to check active task:', e) }
    }
    if (taskId) {
      try {
        await chatAPI.cancel(projectId, taskId)
      } catch (e) {
        console.warn('Failed to cancel task:', e)
        // The checkpoint may still be answerable — keep the dialogs open.
        return
      }
    }
    useStore.getState().clearPendingInteraction()
    useStore.getState().clearPendingPermission()
    useStore.getState().setStreamInteractionRequest(null)
    taskIdRef.current = null
    clearStopAbortTimer()
    stopRequestedRef.current = false
    try {
      await refreshLatestHistory()
    } catch { /* best-effort */ }
  }

  async function copyMessage(message) {
    try {
      await copyToClipboard(message.content || '')
      setCopiedMessageId(message.id)
      if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current)
      copiedTimerRef.current = setTimeout(() => setCopiedMessageId(cur => (cur === message.id ? null : cur)), 1500)
    } catch {
      toastError(t('chat.toast.copyFailed'))
    }
  }

  /** Best-effort: refresh can_edit flags after compaction. Flag updates only —
   * never merges server rows into the live array: mid-turn the server page
   * holds half-shaped turns (empty in-progress bubbles, a not-yet-followed
   * boundary card), and merging those into the streaming timeline would show
   * them as stray empty bubbles and detached cards. The full reload happens
   * when the stream ends. */
  async function refreshCanEditFlags() {
    if (!projectId || !sessionId) return
    try {
      const page = await fetchHistoryPage()
      if (!page) return
      const canEditMap = new Map()
      for (const m of page.messages) {
        if (m.id) canEditMap.set(m.id, m.can_edit)
      }
      // Server's last-boundary seq: covers user messages from older pages
      // that aren't in canEditMap.
      const boundarySeq = page.boundary_seq
      setMessages(prev => prev.map(m => {
        if (m.id && canEditMap.has(m.id)) {
          const nextCanEdit = canEditMap.get(m.id)
          return m.can_edit === nextCanEdit ? m : { ...m, can_edit: nextCanEdit }
        }
        const seq = messageSeq(m)
        if (m.role === 'user' && Number.isFinite(boundarySeq) && seq !== null && seq <= boundarySeq) {
          return m.can_edit === false ? m : { ...m, can_edit: false }
        }
        return m
      }))
    } catch { /* best-effort: don't disrupt streaming */ }
  }

  function startEditMessage(message) {
    if (isStreaming || !message.can_edit) return
    setEditingMessageId(message.id)
    setEditingText(message.content || '')
  }

  function cancelEditMessage() {
    setEditingMessageId(null)
    setEditingText('')
  }

  async function submitEditMessage() {
    const text = editingText.trim()
    if (!text || !editingMessageId || isStreaming || awaiting || !projectId || !sessionId) return
    const messageId = editingMessageId
    const originalMessage = messages.find(m => m.id === messageId)
    const attachments = originalMessage?.attachments || []
    // Claim the streaming slot before the awaited save: a second submit while
    // onSaveBeforeChat runs must not pass the guard above.
    isStreamingRef.current = true
    if (onSaveBeforeChat) {
      try { await onSaveBeforeChat() } catch { /* don't block chat on save failure */ }
    }

    const editIndex = messages.findIndex(m => m.id === messageId)
    setIsStreaming(true)
    setStreamPhase('thinking')
    resetThinkingLive()
    const handle = beginStreamHandle()
    let streamStarted = false
    try {
      const body = await chatAPI.editMessage(projectId, sessionId, {
        message_id: messageId,
        message: text,
        attachments,
        ...(getUserState ? { user_state: getUserState() } : {}),
        ...(tokenBudget ? { token_budget: tokenBudget } : {}),
      }, handle.controller.signal)
      if (handle.controller.signal.aborted) return
      streamStarted = true
      setMessages(prev => {
        const idx = prev.findIndex(m => m.id === messageId)
        const kept = idx >= 0 ? prev.slice(0, idx) : prev
        return [
          ...kept,
          { role: 'user', content: displayMessageText(text, t('chat.planDisplay')), attachments, created_at: new Date().toISOString() },
          { role: 'SiGMA', content: '', process: [], localId: nextLiveTurnId() },
        ]
      })
      cancelEditMessage()
      const reader = body.getReader()
      const decoder = new TextDecoder()
      await processSSEStream(reader, decoder, handle)
    } catch (err) {
      if (err.name !== 'AbortError') {
        toastError(t('chat.toast.editFailed', { message: err.message || '' }))
        if (editIndex >= 0) {
          // Reconcile with the server page instead of replacing the array:
          // older pages the user already scrolled up to load must survive.
          try {
            await refreshLatestHistory()
          } catch { /* keep the current messages */ }
        }
      }
    } finally {
      // Refresh only when the stream began: a fetch failure before that is
      // handled by the catch, which already restored the history page.
      await finalizeStreamTurn(handle, { refreshHistory: streamStarted })
    }
  }

  function applyBudgetDraft() {
    const tokens = parseMillionTokenBudget(budgetDraft)
    if (!tokens) {
      setBudgetError(t('chat.budgetError'))
      return
    }
    setTokenBudget(tokens)
    if (projectId && sessionId) storage.setBudget(projectId, sessionId, tokens)
    setBudgetError('')
    setSettingsPanel('main')
    setShowAutoApproveMenu(false)
  }

  async function uploadImageFiles(files) {
    const imageFiles = imageFilesFromList(files)
    if (imageFiles.length === 0 || !projectId) return
    if (!sessionId) {
      if (imageInputRef.current) imageInputRef.current.value = ''
      toastError(t('chat.toast.chatNotReady'))
      return
    }
    // Attachments belong to the session they were uploaded to — a mid-upload session switch must not inject them into the new session's composer.
    const gen = genRef.current
    setIsUploadingAttachment(true)
    try {
      const uploaded = await Promise.all(imageFiles.map(file => chatAPI.uploadAttachment(projectId, sessionId, file)))
      if (genRef.current === gen) setPendingAttachments(prev => [...prev, ...uploaded])
    } catch (err) {
      toastError(err.message || t('chat.toast.imageUploadFailed'))
    } finally {
      setIsUploadingAttachment(false)
      if (imageInputRef.current) imageInputRef.current.value = ''
      requestAnimationFrame(() => textareaRef.current?.focus())
    }
  }

  function removePendingAttachment(path) {
    setPendingAttachments(prev => prev.filter(item => item.path !== path))
  }

  // ---- Auto-resize textarea ----
  function autoResizeTextarea() {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }

  // ---- Send a new message ----
  async function handleSendMessage() {
    const msg = chatInput.trim()
    const submittedInput = chatInput
    const attachments = pendingAttachments
    if ((!msg && attachments.length === 0) || isStreamingRef.current || isUploadingAttachment || awaiting || !projectId) return

    // Session-management slash commands: pure actions — no message bubble, no stream.
    if (msg === '/clear')  { setChatInput(''); clearCurrentSession();            return }
    if (msg === '/new')    { setChatInput(''); createNewSession();               return }
    if (msg === '/delete') { setChatInput(''); if (sessionId) setDeleteSessionTarget(sessionId); return }
    // /skill [id]: bare → no-op (submenu handles arg). With id → load (backend validates).
    if (msg === '/skill')  { setChatInput(''); return }
    {
      const m = msg.match(/^\/skill\s+(\S+)\s*$/)
      if (m) { setChatInput(''); handleLoadSkill(m[1]); return }
    }

    const isCompactCommand = msg === '/compact'

    // Claim the streaming slot before the awaited save: a second Enter during
    // onSaveBeforeChat must not pass the guard above (which reads the ref).
    isStreamingRef.current = true

    // Save editor content before sending so AI sees the latest file
    if (onSaveBeforeChat) {
      try {
        const saved = await onSaveBeforeChat()
        if (!saved) {
          isStreamingRef.current = false
          return
        }
      } catch {
        isStreamingRef.current = false
        return
      }
    }

    setChatInput('')
    setPendingAttachments([])
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
    setIsStreaming(true)

    setMessages(prev => [...prev, {
      role: 'user', content: displayMessageText(msg, t('chat.planDisplay')), attachments, created_at: new Date().toISOString(),
      ...(citations.length > 0 ? { citation: joinCitationTexts(citations) } : {}),
    }])
    setMessages(prev => [...prev, { role: 'SiGMA', content: '', process: [], localId: nextLiveTurnId() }])

    setStreamPhase(isCompactCommand ? 'compacting' : 'thinking')
    resetThinkingLive()

    const handle = beginStreamHandle()
    let streamStarted = false

    try {
      const streamBody = { message: msg || t('chat.inspectImage') }
      if (sessionId) streamBody.session_id = sessionId
      if (!isCompactCommand && attachments.length > 0) streamBody.attachments = attachments
      if (!isCompactCommand && getUserState) streamBody.user_state = getUserState()
      if (!isCompactCommand && tokenBudget) streamBody.token_budget = tokenBudget

      const body = await chatAPI.stream(projectId, streamBody, handle.controller.signal)
      if (handle.controller.signal.aborted) return
      streamStarted = true
      // Clear the citation strip only once the stream is actually
      // established: a failed establish restores input and attachments, and
      // the citations must survive that failure alongside them.
      if (!isCompactCommand && onClearCitations) onClearCitations()
      const reader = body.getReader()
      const decoder = new TextDecoder()
      // Compact commands skip the history refresh: their bubbles are
      // local-only (not persisted) and would be lost if server data
      // replaced messages.
      processSSEStream(reader, decoder, handle).finally(() => finalizeStreamTurn(handle, { refreshHistory: !isCompactCommand }))
    } catch (err) {
      if (err.name === 'AbortError') return
      if (!streamStarted) {
        setMessages(prev => {
          if (prev.length < 2) return prev
          const last = prev[prev.length - 1]
          const previous = prev[prev.length - 2]
          if (last.role === 'SiGMA' && !last.content && previous.role === 'user' && previous.content === displayMessageText(msg, t('chat.planDisplay'))) {
            return prev.slice(0, -2)
          }
          return prev
        })
      }
      setChatInput(prev => prev || submittedInput)
      setPendingAttachments(attachments)
      toastError(t('chat.toast.connectionFailed', { message: err.message || '' }))
      if (streamHandleRef.current === handle) setIsStreaming(false)
    }
  }

  // ---- Render helpers ----
  const activeSessions = sessions.filter(s => !s.is_archived)
  // Floating message-nav buttons: quiet by default (opacity-60), fully visible
  // on hover — they sit over the chat's right edge and must stay unobtrusive.
  const chatNavBtnClass = 'pointer-events-auto w-6 h-6 rounded-full bg-white/90 dark:bg-gray-800/90 border border-gray-200 dark:border-gray-700 shadow-sm backdrop-blur flex items-center justify-center text-gray-500 dark:text-gray-400 hover:text-sigma-600 dark:hover:text-sigma-200 opacity-60 hover:opacity-100 transition-opacity'
  const archivedCount = sessions.filter(s => s.is_archived).length

  const slashSuggestions = getSlashSuggestions(chatInput)
  const slashActiveSafe = slashSuggestions.length > 0 ? Math.min(slashActiveIdx, slashSuggestions.length - 1) : 0

  // Skill submenu: opens when input matches "/skill <partial>" (trailing space closes
  // the main slash popup, so the two menus are mutually exclusive). Requires the
  // enabled-skills list to have loaded (null = still loading).
  const skillMenuMatch = slashSuggestions.length === 0 ? chatInput.match(/^\/skill\s+(\S*)$/) : null
  const skillMenuOpen = !!skillMenuMatch && Array.isArray(enabledSkills)
  const skillSuggestions = (() => {
    if (!skillMenuOpen) return []
    const q = (skillMenuMatch[1] || '').toLowerCase()
    if (!q) return enabledSkills
    return enabledSkills.filter(s =>
      s.id.toLowerCase().includes(q) || (s.name || '').toLowerCase().includes(q)
    )
  })()
  const skillActiveSafe = skillSuggestions.length > 0 ? Math.min(skillActiveIdx, skillSuggestions.length - 1) : 0

  // Context gauge numbers. Fill is relative to the compaction threshold —
  // the hard max only appears in the hover bubble's threshold-vs-max strip.
  const ctxCurrent = contextStats ? Number(contextStats.current_tokens || 0) : 0
  const ctxThreshold = contextStats ? Number(contextStats.compact_threshold || 0) : 0
  const ctxMax = contextStats ? Number(contextStats.max_context_length || 0) : 0
  const ctxRatio = ctxThreshold > 0 ? Math.min(100, (ctxCurrent / ctxThreshold) * 100) : 0
  const ctxOver = ctxThreshold > 0 && ctxCurrent > ctxThreshold
  const ctxPct = ctxThreshold > 0 ? ((ctxCurrent / ctxThreshold) * 100).toFixed(1) : '0.0'
  const ctxPctOfMax = ctxMax > 0 ? Math.min(100, (ctxCurrent / ctxMax) * 100) : 0
  const ctxThresholdPctOfMax = ctxMax > 0 ? Math.min(100, (ctxThreshold / ctxMax) * 100) : 0

  function applySlashSuggestion(command) {
    // Suggestions are only shown when the input is exactly `/` or `/word` with
    // nothing after, so we can replace the whole input.
    setChatInput(`${command} `)
    requestAnimationFrame(() => {
      textareaRef.current?.focus()
      autoResizeTextarea()
    })
  }

  // Picking a skill from the submenu fills the input (does NOT load) — the
  // user sends the message to load. Trailing space closes the submenu so the
  // next Enter goes to the normal send path.
  function applySkillSuggestion(skillId) {
    setChatInput(`/skill ${skillId} `)
    requestAnimationFrame(() => {
      textareaRef.current?.focus()
      autoResizeTextarea()
    })
  }

  return (
    <div ref={panelRootRef} className="flex-1 flex flex-col bg-gray-50/30 dark:bg-gray-900 overflow-hidden">
      {/* ── Session header bar ── */}
      <div className="px-4 py-2.5 bg-white dark:bg-gray-900 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2 flex-shrink-0">
        {/* Title area */}
        <div className="group/title flex items-center gap-1.5 flex-1 min-w-0">
          {isEditingTitle ? (
            <div className="flex items-center gap-1 flex-1 min-w-0">
              <input
                ref={titleInputRef}
                value={editTitle}
                onChange={e => setEditTitle(e.target.value.slice(0, 100))}
                onKeyDown={handleTitleKeyDown}
                onBlur={submitTitle}
                className="flex-1 min-w-0 text-sm font-semibold bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg px-2 py-1 outline-none focus:ring-2 focus:ring-sigma-600/20 focus:border-sigma-600"
              />
              <button data-edit-btn onClick={submitTitle} className="p-1 text-green-600 hover:bg-green-50 dark:hover:bg-green-900/30 rounded"><Check className="w-3.5 h-3.5" /></button>
              <button data-edit-btn onClick={cancelEditTitle} className="p-1 text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"><X className="w-3.5 h-3.5" /></button>
              <span className="text-[9px] text-gray-300 font-mono">{editTitle.length}/100</span>
            </div>
          ) : (
            <>
              <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 truncate">{sessionTitle || t('chat.untitled')}</h3>
              <button onClick={startEditTitle} className="p-0.5 text-gray-300 opacity-0 group-hover/title:opacity-100 hover:text-sigma-600 transition-all flex-shrink-0">
                <Pencil className="w-3.5 h-3.5" />
              </button>
            </>
          )}
        </div>

        {/* Session dropdown */}
        <div className="relative" ref={dropdownRef}>
          <button
            ref={dropdownBtnRef}
            onClick={() => {
              const opening = !showDropdown
              setShowDropdown(opening)
              if (opening) {
                loadSessions()
                if (dropdownBtnRef.current) {
                  const rect = dropdownBtnRef.current.getBoundingClientRect()
                  setDropdownPos({ top: rect.bottom + 4, left: Math.max(4, rect.right - 288) })
                }
              }
            }}
            className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
          >
            <ChevronDown className="w-4 h-4" />
          </button>

          {showDropdown && (
            <div className="fixed z-[90] w-72 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-xl overflow-hidden animate-in fade-in zoom-in duration-150" style={{ top: dropdownPos.top, left: dropdownPos.left }}>
              <div className="px-3 py-2 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
                <span className="text-[10px] font-black uppercase tracking-widest text-gray-400">{t('chat.sessions')}</span>
                <button
                  onClick={openSearch}
                  className="p-1 text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  title={`${t('chat.search')} (${SEARCH_SHORTCUT_LABEL})`}
                  aria-label={t('chat.search')}
                >
                  <Search className="w-3.5 h-3.5" />
                </button>
              </div>

              <div className="max-h-64 overflow-y-auto">
                {activeSessions.length === 0 && (
                  <div className="px-3 py-6 text-center text-xs text-gray-400">{t('chat.noSessions')}</div>
                )}
                {activeSessions.map(s => (
                  <div
                    key={s.id}
                    onClick={() => switchToSession(s.id)}
                    className={`relative flex items-center justify-between px-3 py-2.5 cursor-pointer transition-colors border-b border-gray-50 dark:border-gray-800 ${s.id === sessionId ? 'bg-sigma-50 dark:bg-sigma-600/20' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                  >
                    {s.id === sessionId && <div className="absolute left-0 top-0 bottom-0 w-1 bg-sigma-600 rounded-r" />}
                    <div className="flex-1 min-w-0 mr-2">
                      <span className={`text-sm font-medium truncate block ${s.id === sessionId ? 'text-sigma-700 dark:text-sigma-300' : 'text-gray-700 dark:text-gray-300'}`}>
                        {s.title || t('chat.untitled')}
                      </span>
                      <span className="text-[9px] text-gray-400 dark:text-gray-500 mt-0.5 block">
                        {formatTimestamp(s.updated_at)}
                      </span>
                    </div>
                    <div className="flex items-center gap-0.5 flex-shrink-0">
                      <button
                        onClick={(e) => archiveSession(s.id, e)}
                        className="p-1 text-gray-300 dark:text-gray-500 hover:text-amber-500 hover:bg-amber-50 dark:hover:bg-amber-900/30 rounded transition-colors"
                        title={t('chat.archive')}
                      >
                        <Archive className="w-3.5 h-3.5" />
                      </button>
                      <button
                        onClick={(e) => deleteSessionAction(s.id, e)}
                        className="p-1 text-gray-300 dark:text-gray-500 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 rounded transition-colors"
                        title={t('common.delete')}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>

              <div className="border-t border-gray-100 dark:border-gray-800 p-1.5 space-y-0.5">
                <button
                  onClick={createNewSession}
                  className="w-full flex items-center gap-2 px-2 py-1.5 text-[10px] font-medium text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                >
                  <Plus className="w-3.5 h-3.5" />
                  {t('chat.newSession')}
                </button>
                <button
                  onClick={() => openArchived()}
                  className="w-full flex items-center gap-2 px-2 py-1.5 text-[10px] font-medium text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                >
                  <Archive className="w-3.5 h-3.5" />
                  {t('chat.viewArchived')} {archivedCount > 0 && `(${archivedCount})`}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="relative flex-1 min-h-0">
        {/* relative: without it the scroller is not the containing block for
            absolutely positioned content inside messages (KaTeX's hidden
            .katex-mathml a11y span), so such content escapes this box's
            clipping and silently inflates the panel root's scrollable
            overflow — room that programmatic scrolling must never have. */}
        <div ref={chatScrollRef} onScroll={handleChatScroll} className="relative h-full overflow-y-auto p-4">
          <div ref={chatContentRef} className="space-y-6">
          {isLoadingHistory && (
            <div className="text-center text-[10px] text-gray-400 dark:text-gray-500 font-medium">{t('chat.loadingHistory')}</div>
          )}
          {messages.length === 0 && (
            <div className="bg-white dark:bg-gray-900 p-6 rounded-3xl shadow-sm border border-gray-100 dark:border-gray-800 text-sm text-gray-500 dark:text-gray-400 italic leading-relaxed text-center mt-10">
              <Bot className="w-8 h-8 mx-auto mb-3 text-sigma-600/30" />
              {t('chat.welcome')}
            </div>
          )}
          {messages.map((m, i) => {
            const isLastMessage = i === messages.length - 1
            const isCurrentlyStreaming = isLastMessage && isStreaming
            // Compaction boundaries render as a collapsed card, not a bubble.
            if (m.is_boundary) {
              return (
                <div key={m.id || i} data-chat-msg="" data-msg-id={m.id} data-msg-role="system" className="w-full my-1 px-1">
                  <CompactSummaryNote summary={m.content} />
                </div>
              )
            }
            return (
              // localId first: the live streaming bubble has no server id yet,
              // and an index key would remount it whenever a history merge
              // reorders the array mid-stream.
              <div key={m.localId || m.id || i} data-chat-msg="" data-msg-id={m.id} data-msg-role={m.role} className={`group/message flex flex-col rounded-2xl transition-colors duration-700 ${m.role === 'user' ? 'items-end' : 'items-start'} ${m.id === flashMessageId ? 'bg-amber-100/60 dark:bg-amber-900/25' : ''}`}>
                <div className="flex items-center gap-2 mb-1.5 px-1">
                  {m.role === 'SiGMA' ? <Zap className="w-3 h-3 text-sigma-600" /> : <User className="w-3 h-3 text-gray-400 dark:text-gray-500" />}
                  <span className="text-[10px] font-black uppercase tracking-widest text-gray-400 dark:text-gray-500">
                    {t(m.role === 'SiGMA' ? 'chat.roleSigma' : 'chat.roleUser')}
                  </span>
                  {m.created_at && (
                    <span className="text-[9px] text-gray-300 select-none">
                      {formatTimestamp(m.created_at)}
                    </span>
                  )}
                </div>
                {m.role === 'SiGMA' && <ThinkingProcess steps={m.process} isStreaming={isCurrentlyStreaming} />}
                <div className={`max-w-[90%] px-4 py-3 rounded-2xl shadow-sm animate-in fade-in slide-in-from-bottom-1 duration-300 overflow-hidden break-words ${
                  m.role === 'user' ? 'bg-sigma-600 text-white rounded-tr-none' : 'bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-200 border border-gray-100 dark:border-gray-800 rounded-tl-none'
                }`}>
                  {m.role === 'SiGMA' ? <MarkdownContent content={m.content || ''} projectId={projectId} onCitation={onCitation} /> : (
                    <div className={`text-sm leading-relaxed whitespace-pre-wrap ${editingMessageId === m.id ? 'opacity-50' : ''}`}>{m.content}</div>
                  )}
                  {m.role === 'user' && (
                    <AttachmentStrip projectId={projectId} attachments={m.attachments} compact />
                  )}
                  {m.role === 'user' && m.citation && (
                    <button
                      onClick={() => setViewingCitation(m.citation)}
                      className="mt-2 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-white/60 hover:text-white/90 transition-colors"
                    >
                      <TextQuote className="w-3 h-3" />
                      {t('chat.viewCitation')}
                    </button>
                  )}
                  {isStreaming && i === messages.length - 1 && !m.content && streamPhase && (
                    <div className="mt-1 min-w-0">
                      <div className="flex items-center gap-2 text-gray-400 dark:text-gray-500">
                        <RotateCw className="w-3.5 h-3.5 animate-spin flex-shrink-0" />
                        <span className="shimmer-text text-[11px] font-bold italic tracking-wider flex-shrink-0">{t(`chat.${streamPhase}`)}</span>
                      </div>
                      {streamPhase === 'thinking' && thinkingLive && (
                        <div
                          ref={thinkBoxRef}
                          onScroll={() => {
                            // Release auto-follow when the user scrolls away
                            // from the bottom to read older reasoning.
                            const el = thinkBoxRef.current
                            if (el) {
                              thinkFollowRef.current =
                                el.scrollHeight - el.scrollTop - el.clientHeight < 8
                            }
                          }}
                          className="mt-1.5 h-[4.75rem] overflow-y-auto rounded border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-900 px-2 py-1"
                        >
                          <pre
                            className="whitespace-pre-wrap break-words text-[10px] leading-relaxed font-mono text-gray-500 dark:text-gray-400"
                            style={{
                              WebkitMaskImage: 'linear-gradient(to bottom, transparent, black 1.35em)',
                              maskImage: 'linear-gradient(to bottom, transparent, black 1.35em)',
                            }}
                          >{thinkingLive}</pre>
                        </div>
                      )}
                    </div>
                  )}
                  {!isStreaming && m.role === 'SiGMA' && (m.interrupted || m.durationMs != null) && (
                    <div className="mt-1 flex items-center gap-2 text-[9px] text-gray-400 dark:text-gray-500">
                      {m.interrupted && (
                        <span className="flex items-center gap-1 text-amber-500 dark:text-amber-400">
                          <span className="w-1 h-1 rounded-full bg-amber-400" />
                          {t('chat.interrupted')}
                        </span>
                      )}
                      {m.durationMs != null && <span>{formatDurationMs(m.durationMs)}</span>}
                    </div>
                  )}
                </div>
                {editingMessageId === m.id && m.role === 'user' && (
                  <div className="max-w-[90%] mt-2 p-3 rounded-xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-sm">
                    <textarea
                      value={editingText}
                      onChange={e => setEditingText(e.target.value)}
                      disabled={isStreaming}
                      autoFocus
                      className="w-full min-h-24 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2 text-sm text-gray-800 dark:text-gray-200 outline-none placeholder:text-gray-400 focus:ring-2 focus:ring-sigma-300 focus:border-sigma-300 disabled:opacity-60"
                    />
                    <div className="text-[10px] text-gray-400 dark:text-gray-500 mt-1.5">
                      {t('chat.editWarning')}
                    </div>
                    <div className="flex justify-end gap-2 mt-2">
                      <button disabled={isStreaming} onClick={cancelEditMessage} className="px-3 py-1.5 text-xs font-semibold rounded-lg border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">{t('common.cancel')}</button>
                      <button disabled={isStreaming} onClick={submitEditMessage} className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-sigma-600 text-white hover:bg-sigma-700 disabled:opacity-50">{t('common.send')}</button>
                    </div>
                  </div>
                )}
                {editingMessageId !== m.id && (
                  <div className={`mt-1 px-1 flex items-center gap-1 opacity-0 group-hover/message:opacity-100 transition-opacity ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                    <button onClick={() => copyMessage(m)} className="p-1 text-gray-300 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-white dark:hover:bg-gray-900 rounded-md border border-transparent hover:border-gray-100 dark:hover:border-gray-800" title={t('chat.copy')}>
                      {copiedMessageId === m.id ? <Check className="w-3 h-3 text-green-500" /> : <Copy className="w-3 h-3" />}
                    </button>
                    {m.id && m.role === 'SiGMA' && !isStreaming && !awaiting && (
                      <button onClick={() => forkFromMessage(m)} className="p-1 text-gray-300 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-white dark:hover:bg-gray-900 rounded-md border border-transparent hover:border-gray-100 dark:hover:border-gray-800" title={t('chat.fork')}>
                        <GitBranch className="w-3 h-3" />
                      </button>
                    )}
                    {m.role === 'user' && m.can_edit && !isStreaming && !awaiting && (
                      <button onClick={() => startEditMessage(m)} className="p-1 text-gray-300 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-white dark:hover:bg-gray-900 rounded-md border border-transparent hover:border-gray-100 dark:hover:border-gray-800" title={t('common.edit')}>
                        <Pencil className="w-3 h-3" />
                      </button>
                    )}
                  </div>
                )}
                {m.role === 'SiGMA' && (m.usage || m.token_count > 0) && (
                  <div className="text-[9px] text-gray-300 dark:text-gray-600 select-none px-1 mt-1">
                    {(() => {
                      const u = m.usage || {}
                      const fmt = n => {
                        if (n == null || n === 0) return null
                        if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'
                        if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k'
                        return String(n)
                      }
                      const parts = []
                      const inp = fmt(u.input ?? m.input_tokens)
                      const out = fmt(u.output ?? m.token_count)
                      const cached = fmt(u.cached ?? m.cached_tokens)
                      if (inp) parts.push(`${t('chat.tokenInput')}${inp}`)
                      if (out) parts.push(`${t('chat.tokenOutput')}${out}`)
                      if (cached) parts.push(`${t('chat.tokenCached')}${cached}`)
                      return parts.join(' · ')
                    })()}
                  </div>
                )}
              </div>
            )
          })}
          </div>
        </div>
        {/* Overlay outside the scroller so it never scrolls away with the
            content; the wrapper is click-through — only the buttons hit-test. */}
        {chatScrollable && (
          <div className="absolute right-2 top-1/2 -translate-y-1/2 z-10 flex flex-col gap-1 pointer-events-none">
            <button onClick={() => jumpChatMessage(-1)} className={chatNavBtnClass} title={t('chat.navPrevMessage')} aria-label={t('chat.navPrevMessage')}>
              <ChevronUp className="w-3.5 h-3.5" />
            </button>
            {messages.some(m => m.role === 'user') && (
              <button
                ref={navListBtnRef}
                onClick={openNavList}
                className={`${chatNavBtnClass} ${navListOpen ? 'opacity-100 text-sigma-600 dark:text-sigma-200' : ''}`}
                title={t('chat.navMessageList')}
                aria-label={t('chat.navMessageList')}
                aria-expanded={navListOpen}
              >
                <List className="w-3 h-3" />
              </button>
            )}
            <button onClick={() => jumpChatMessage(1)} className={chatNavBtnClass} title={t('chat.navNextMessage')} aria-label={t('chat.navNextMessage')}>
              <ChevronDown className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
        {navListOpen && (
          // Height-capped on purpose: without the cap a long list would
          // swallow the whole panel. The offset clears the floating button
          // column. The nesting isolates the centering translate from the
          // entrance animation: the animation's forwards-filled transform
          // replaces the utility transform it animates over, which would
          // drop the popup to start at the panel's vertical center.
          <div className="absolute top-1/2 -translate-y-1/2 right-11 z-20 w-56 max-w-[calc(100%_-_56px)]">
            <div
              ref={navPopupRef}
              className="max-h-[min(320px,50vh)] flex flex-col rounded-2xl bg-white/95 dark:bg-gray-900/95 shadow-[0_8px_30px_rgba(0,0,0,0.12)] border border-gray-100 dark:border-gray-800 overflow-hidden animate-in fade-in zoom-in duration-150"
            >
              {navListLoading ? (
                <div className="flex items-center gap-2 px-3.5 py-3.5 text-xs text-gray-400 dark:text-gray-500">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {t('chat.navLoading')}
                </div>
              ) : navList.length === 0 ? (
                <div className="px-3.5 py-3.5 text-xs text-gray-400 dark:text-gray-500">{t('chat.navEmpty')}</div>
              ) : (
                <div ref={navListScrollRef} className="overflow-y-auto p-1.5 flex flex-col gap-0.5">
                  {navList.map((entry, idx) => {
                    const current = entry.id === navHighlightId
                      || (navHighlightId === NAV_HIGHLIGHT_LAST && idx === navList.length - 1)
                    return (
                      <button
                        key={entry.id ?? idx}
                        data-nav-current={current || undefined}
                        onClick={() => jumpToNavEntry(entry)}
                        title={entry.summary}
                        className={`w-full px-2.5 py-1.5 rounded-lg text-left transition-colors ${
                          current
                            ? 'bg-sigma-100 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-200'
                            : 'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'
                        }`}
                      >
                        <span className="block truncate text-xs">{entry.summary}</span>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="px-4 pt-2 pb-2 bg-white dark:bg-gray-900 border-t border-gray-100 dark:border-gray-800 space-y-3 shadow-[0_-10px_20px_rgba(0,0,0,0.02)]">
        {tokenBudget && (
          <div className="flex items-center justify-between rounded-xl border border-amber-100 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-900/30 px-3 py-2 text-[10px] font-medium text-amber-700 dark:text-amber-300">
            <span>{t('chat.tokenBudget')}{formatTokenCount(tokenBudget)}{t('chat.tokenBudgetFor')}</span>
            <button onClick={() => { setTokenBudget(null); if (projectId && sessionId) storage.removeBudget(projectId, sessionId) }} className="p-0.5 rounded hover:bg-amber-100 dark:hover:bg-amber-800/50" title={t('chat.clearBudget')}>
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        {citations.length > 0 && (
          <div className="bg-blue-50/50 dark:bg-blue-900/30 border border-blue-100 dark:border-blue-800/50 rounded-xl px-3 py-2 flex flex-col gap-1 animate-in slide-in-from-bottom-2 duration-200">
            {citations.map((cite, idx) => (
              <div key={idx} className="flex items-start gap-3" title={cite.fullText}>
                <Quote className="w-3.5 h-3.5 text-blue-400 mt-1 flex-shrink-0" />
                <div className="flex-1 text-[11px] font-medium text-blue-700 dark:text-blue-300 line-clamp-1">{cite.text}</div>
                {onRemoveCitation && (
                  <button onClick={() => onRemoveCitation(idx)} className="p-1 hover:bg-blue-100 dark:hover:bg-blue-800/50 text-blue-400 rounded-lg transition-colors"><X className="w-3 h-3" /></button>
                )}
              </div>
            ))}
          </div>
        )}
        {pendingAttachments.length > 0 && (
          <div className="rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 py-2">
            <AttachmentStrip
              projectId={projectId}
              attachments={pendingAttachments}
              onRemove={removePendingAttachment}
            />
          </div>
        )}
        <div className="relative">
          {slashSuggestions.length > 0 && (
            <div className="absolute bottom-full left-0 right-0 mb-2 bg-white dark:bg-gray-900 rounded-2xl shadow-[0_8px_30px_rgba(0,0,0,0.12)] border border-gray-100 dark:border-gray-800 overflow-hidden max-h-60 overflow-y-auto z-[80] animate-in fade-in zoom-in duration-150">
              {slashSuggestions.map((cmd, idx) => (
                <button
                  key={cmd.command}
                  onMouseDown={e => { e.preventDefault(); applySlashSuggestion(cmd.command) }}
                  className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${idx === slashActiveSafe ? 'bg-sigma-50 dark:bg-sigma-600/20' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                >
                  <span className="w-20 font-mono text-xs font-bold text-sigma-600">{cmd.command}</span>
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-gray-700 dark:text-gray-300">{t(cmd.labelKey)}</div>
                    <div className="text-[10px] text-gray-400 dark:text-gray-500 truncate">{t(cmd.descKey)}</div>
                  </div>
                </button>
              ))}
              <div className="px-3 py-1.5 border-t border-gray-100 dark:border-gray-800 text-[10px] text-gray-400 dark:text-gray-500 flex items-center gap-2">
                <kbd className="font-mono px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800">Tab</kbd>
                <span>{t('chat.slashInsert')}</span>
                <kbd className="font-mono px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800">↑↓</kbd>
                <span>{t('chat.slashNavigate')}</span>
              </div>
            </div>
          )}
          {skillMenuOpen && (
            <div className="absolute bottom-full left-0 right-0 mb-2 bg-white dark:bg-gray-900 rounded-2xl shadow-[0_8px_30px_rgba(0,0,0,0.12)] border border-gray-100 dark:border-gray-800 overflow-hidden max-h-60 overflow-y-auto z-[80] animate-in fade-in zoom-in duration-150">
              {skillSuggestions.length === 0 ? (
                <div className="px-4 py-3 text-xs text-gray-400 dark:text-gray-500">
                  {t('chat.skillEmpty')}
                </div>
              ) : skillSuggestions.map((sk, idx) => (
                <button
                  key={sk.id}
                  onMouseDown={e => { e.preventDefault(); applySkillSuggestion(sk.id) }}
                  className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${idx === skillActiveSafe ? 'bg-sigma-50 dark:bg-sigma-600/20' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                >
                  <span className="w-24 font-mono text-xs font-bold text-sigma-600 truncate">{sk.id}</span>
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-gray-700 dark:text-gray-300">{sk.name}</div>
                    <div className="text-[10px] text-gray-400 dark:text-gray-500 truncate">{sk.description}</div>
                  </div>
                </button>
              ))}
              <div className="px-3 py-1.5 border-t border-gray-100 dark:border-gray-800 text-[10px] text-gray-400 dark:text-gray-500 flex items-center gap-2">
                <kbd className="font-mono px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800">Tab</kbd>
                <span>{t('chat.slashInsert')}</span>
                <kbd className="font-mono px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800">↑↓</kbd>
                <span>{t('chat.slashNavigate')}</span>
              </div>
            </div>
          )}
          {/* Negative margins hang the row into the footer's side padding:
              the puck 8px into the left, the input box 8px into the right,
              so both edges sit 8px from the panel edge with a 3px seam
              between them. The transparent border matches the input
              container's 1px border and `block` avoids the inline-block
              baseline gap, keeping tops and bottoms flush. */}
          <div className="-mr-2 flex items-end gap-[3px]">
            <div className="group/puck relative -ml-2 flex-shrink-0">
              <button
                ref={autoApproveBtnRef}
                onClick={() => {
                  const opening = !showAutoApproveMenu
                  setShowAutoApproveMenu(opening)
                  if (opening && autoApproveBtnRef.current) {
                    setSettingsPanel('main')
                    // Re-read from the backend on every open — the store is only a
                    // snapshot and other tabs / the dialog checkbox can change it.
                    if (currentProject) loadAutoApproveSettings(currentProject.id)
                    const rect = autoApproveBtnRef.current.getBoundingClientRect()
                    setAutoApproveMenuPos({
                      bottom: window.innerHeight - rect.top + 4,
                      left: Math.max(4, rect.left),
                    })
                  }
                }}
                aria-label={t('chat.autoApproveSettings')}
                className={`block rounded-2xl border border-transparent transition-colors ${showAutoApproveMenu ? 'bg-sigma-50 dark:bg-sigma-600/20' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}
              >
                <ContextPuck
                  ratio={ctxRatio}
                  over={ctxOver}
                  categories={APPROVAL_CATEGORIES}
                  approvals={autoApproveSettings}
                  pendingKey={approvingCategory}
                />
              </button>
              <div
                role="tooltip"
                className="pointer-events-none absolute bottom-full left-0 z-[90] mb-2 hidden w-max max-w-[280px] space-y-1 rounded-lg border border-gray-200 bg-gray-800 px-3 py-2 font-mono text-[11px] leading-relaxed text-gray-100 shadow-2xl group-hover/puck:block group-focus-within/puck:block dark:border-gray-700 dark:bg-gray-900"
              >
                {contextStats && (
                  <>
                    <div>{t('chat.ctxLength')}{formatTokenCount(ctxCurrent)} / {formatTokenCount(ctxThreshold)} ({ctxPct}%)</div>
                    <div>{t('chat.threshold')}{formatTokenCount(ctxThreshold)}{t('chat.maxToken')}{formatTokenCount(ctxMax)}</div>
                    <div className="relative h-1 overflow-hidden rounded-full bg-gray-600 dark:bg-gray-700">
                      <div className="absolute inset-y-0 left-0 bg-blue-400" style={{ width: `${ctxPctOfMax}%` }} />
                      <div className="absolute inset-y-0 w-px bg-gray-200 dark:bg-gray-300" style={{ left: `${ctxThresholdPctOfMax}%` }} />
                    </div>
                  </>
                )}
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 pt-0.5">
                  {APPROVAL_CATEGORIES.map(({ key, labelKey }) => {
                    const on = autoApproveSettings[key] === true
                    return (
                      <span key={key} className="inline-flex items-center gap-1 whitespace-nowrap">
                        <span className={on ? 'text-emerald-400' : 'text-gray-500 dark:text-gray-400'}>{on ? '●' : '○'}</span>
                        {t(labelKey)}
                      </span>
                    )
                  })}
                </div>
              </div>
            </div>
            <div className="flex min-w-0 flex-1 items-end gap-2 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded-2xl pl-4 pr-2 py-1.5 focus-within:ring-2 focus-within:ring-sigma-600/20 focus-within:bg-white dark:focus-within:bg-gray-900 transition-all">
              <input
                ref={imageInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                multiple
                className="hidden"
                onChange={e => uploadImageFiles(e.target.files)}
              />
              {awaiting && dismissedAny && reopenTarget ? (
                <button type="button"
                  onClick={() => setInteractionDismissed(false)}
                  className="flex-1 flex items-center justify-center text-sm font-medium text-sigma-600 dark:text-sigma-400 hover:text-sigma-700 dark:hover:text-sigma-300 cursor-pointer py-1.5 transition-colors">
                  {t('chat.reopenQuestion')}
                </button>
              ) : (
              <textarea
                ref={textareaRef}
                disabled={awaiting}
                value={chatInput}
                onChange={e => { setChatInput(e.target.value); setSlashActiveIdx(0); setSkillActiveIdx(0); autoResizeTextarea() }}
                onPaste={e => {
                  const images = imageFilesFromList(e.clipboardData?.files?.length ? e.clipboardData.files : e.clipboardData?.items)
                  if (images.length > 0) {
                    e.preventDefault()
                    uploadImageFiles(images)
                  }
                }}
                onDrop={e => {
                  const images = imageFilesFromList(e.dataTransfer?.files)
                  if (images.length > 0) {
                    e.preventDefault()
                    uploadImageFiles(images)
                  }
                }}
                onDragOver={e => {
                  if (imageFilesFromList(e.dataTransfer?.items || []).length > 0) e.preventDefault()
                }}
                onKeyDown={e => {
                  if (slashSuggestions.length > 0) {
                    if (e.key === 'Tab') {
                      e.preventDefault()
                      applySlashSuggestion(slashSuggestions[slashActiveSafe].command)
                      return
                    }
                    if (e.key === 'ArrowDown') {
                      e.preventDefault()
                      setSlashActiveIdx(i => (i + 1) % slashSuggestions.length)
                      return
                    }
                    if (e.key === 'ArrowUp') {
                      e.preventDefault()
                      setSlashActiveIdx(i => (i - 1 + slashSuggestions.length) % slashSuggestions.length)
                      return
                    }
                  }
                  // Skill submenu navigation (mutually exclusive with the slash popup)
                  if (skillSuggestions.length > 0) {
                    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
                      e.preventDefault()
                      applySkillSuggestion(skillSuggestions[skillActiveSafe].id)
                      return
                    }
                    if (e.key === 'ArrowDown') {
                      e.preventDefault()
                      setSkillActiveIdx(i => (i + 1) % skillSuggestions.length)
                      return
                    }
                    if (e.key === 'ArrowUp') {
                      e.preventDefault()
                      setSkillActiveIdx(i => (i - 1 + skillSuggestions.length) % skillSuggestions.length)
                      return
                    }
                  }
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage() }
                }}
                onFocus={autoResizeTextarea}
                placeholder={resolvedPlaceholder}
                rows={1}
                className="flex-1 bg-transparent text-sm outline-none py-1.5 resize-none max-h-[200px] overflow-y-auto text-gray-800 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-500"
              />
              )}
              <button onClick={isStreaming ? handleStop : awaiting ? handleCancelAwaiting : handleSendMessage}
                disabled={isUploadingAttachment}
                title={awaiting ? t('chat.cancelWaiting') : undefined}
                className={`p-2 text-white rounded-xl transition-all active:scale-95 disabled:opacity-50 ${isStreaming || awaiting ? 'bg-red-500 hover:bg-red-600' : 'bg-sigma-600 hover:bg-sigma-700'}`}>
                {isStreaming || awaiting ? <Square className="w-4 h-4" /> : isUploadingAttachment ? <RotateCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ── Auto-approve settings menu ── */}
      {showAutoApproveMenu && (() => {
        const toolTypes = APPROVAL_CATEGORIES.map(({ key, labelKey, descKey }) => ({
          key, label: t(labelKey), desc: t(descKey),
        }))
        return (
          <div
            ref={autoApproveMenuRef}
            className="fixed z-[90] w-72 bg-white/95 dark:bg-gray-900/95 backdrop-blur-xl rounded-2xl shadow-[0_8px_30px_rgba(0,0,0,0.12)] border border-gray-100 dark:border-gray-800 overflow-hidden animate-in fade-in zoom-in duration-150"
            style={{ bottom: autoApproveMenuPos.bottom, left: autoApproveMenuPos.left }}
          >
            <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800">
              {settingsPanel === 'main' ? (
                <div className="text-xs font-bold text-gray-600 dark:text-gray-400">{t('chat.settings')}</div>
              ) : (
                <button onClick={() => setSettingsPanel('main')} className="flex items-center gap-2 text-xs font-bold text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200">
                  <ArrowLeft className="w-3.5 h-3.5" />
                  {settingsPanel === 'approve' ? t('chat.menuAutoApprove') : t('chat.menuTokenBudget')}
                </button>
              )}
            </div>
            {settingsPanel === 'main' && (
              <div className="py-1">
                <button
                  onClick={() => {
                    imageInputRef.current?.click()
                    setShowAutoApproveMenu(false)
                  }}
                  className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-800 text-left"
                >
                  <ImageIcon className="w-4 h-4 text-sigma-600" />
                  <div>
                    <div className="text-xs font-semibold text-gray-700 dark:text-gray-300">{t('chat.menuUploadImage')}</div>
                    <div className="text-[10px] text-gray-400 dark:text-gray-500">{t('chat.menuUploadImageDesc')}</div>
                  </div>
                </button>
                <button onClick={() => setSettingsPanel('approve')} className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-800 text-left">
                  <Shield className="w-4 h-4 text-sigma-600" />
                  <div>
                    <div className="text-xs font-semibold text-gray-700 dark:text-gray-300">{t('chat.menuAutoApprove')}</div>
                    <div className="text-[10px] text-gray-400 dark:text-gray-500">{t('chat.menuAutoApproveDesc')}</div>
                  </div>
                </button>
                <button onClick={() => { setBudgetDraft(tokenBudget ? String(tokenBudget / 1_000_000) : ''); setBudgetError(''); setSettingsPanel('budget') }} className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-800 text-left">
                  <Gauge className="w-4 h-4 text-sigma-600" />
                  <div>
                    <div className="text-xs font-semibold text-gray-700 dark:text-gray-300">{t('chat.menuTokenBudget')}</div>
                    <div className="text-[10px] text-gray-400 dark:text-gray-500">{t('chat.menuBudgetDesc')}</div>
                  </div>
                </button>
              </div>
            )}
            {settingsPanel === 'approve' && (
              <div className="px-3 py-2 space-y-2">
              {autoApproveLoadFailed ? (
                // The stored snapshot is untrustworthy (could read all-off
                // while the backend auto-approves) — hide the toggles until a
                // successful reload instead of showing a fake state.
                <div className="flex items-center gap-2 px-3 py-2.5 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-xl">
                  <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 text-amber-500 dark:text-amber-400" />
                  <span className="flex-1 min-w-0 text-[10px] leading-snug text-amber-700 dark:text-amber-300">{t('permission.loadFailed')}</span>
                  <button
                    onClick={() => currentProject && loadAutoApproveSettings(currentProject.id)}
                    disabled={!currentProject}
                    className="flex-shrink-0 px-2 py-1 rounded-lg text-[10px] font-semibold bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-200 hover:bg-amber-200 dark:hover:bg-amber-900/60 transition-colors"
                  >
                    {t('permission.retry')}
                  </button>
                </div>
              ) : toolTypes.map(({ key, label, desc }) => {
                const enabled = autoApproveSettings[key] === true
                const isLoading = approvingCategory === key
                return (
                  <div key={key} className="flex items-center justify-between px-3 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-700 rounded-xl">
                    <div className="min-w-0 flex items-center gap-2">
                      {isLoading && <Loader2 className="w-3 h-3 text-gray-400 dark:text-gray-500 animate-spin flex-shrink-0" />}
                      <div className="min-w-0">
                        <div className="text-xs font-bold text-gray-700 dark:text-gray-300">{label}</div>
                        <div className="text-[10px] text-gray-400 dark:text-gray-500 truncate">{desc}</div>
                      </div>
                    </div>
                    <Toggle
                      checked={enabled}
                      disabled={isLoading || !currentProject}
                      onChange={async () => {
                        if (!currentProject || isLoading) return
                        const next = !enabled
                        setApprovingCategory(key)
                        try {
                          await permissionsAPI.setAutoApprove(currentProject.id, { category: key, enabled: next })
                          setAutoApproveType(key, next)
                          // Reconcile the whole snapshot from the backend —
                          // also clears a stale load-failed banner now that
                          // the backend is proven reachable.
                          loadAutoApproveSettings(currentProject.id)
                        } catch (e) {
                          toastError(t('permission.toggleFailed'))
                        } finally {
                          setApprovingCategory(null)
                        }
                      }}
                      label={label}
                    />
                  </div>
                )
              })}
              </div>
            )}
            {settingsPanel === 'budget' && (
              <div className="p-4 space-y-3">
                <div className="text-[11px] text-gray-500 dark:text-gray-400 leading-relaxed">
                  {t('chat.budgetInfo')}
                </div>
                <div className="flex items-center gap-2">
                  <input
                    value={budgetDraft}
                    onChange={e => { setBudgetDraft(e.target.value); setBudgetError('') }}
                    onKeyDown={e => { if (e.key === 'Enter') applyBudgetDraft() }}
                    placeholder="0.1"
                    className="flex-1 min-w-0 rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2 text-sm outline-none focus:border-sigma-600 focus:ring-2 focus:ring-sigma-600/20 text-gray-800 dark:text-gray-200"
                  />
                  <span className="text-xs font-bold text-gray-400 dark:text-gray-500">M</span>
                </div>
                {budgetError && <div className="text-[10px] text-red-500 dark:text-red-400">{budgetError}</div>}
                <div className="flex justify-end gap-1.5">
                  <button onClick={() => { setTokenBudget(null); if (projectId && sessionId) storage.removeBudget(projectId, sessionId) }} className="px-2 py-1.5 text-[10px] font-bold text-gray-400 dark:text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800 rounded-lg">{t('common.clear')}</button>
                  <button onClick={applyBudgetDraft} className="px-2 py-1.5 text-[10px] font-bold text-white bg-sigma-600 hover:bg-sigma-700 rounded-lg">{t('common.apply')}</button>
                </div>
              </div>
            )}
          </div>
        )
      })()}

      {/* ── Archived Sessions Modal ── */}
      {showArchived && (
        <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={() => { setShowArchived(false); setExpandedArchivedId(null) }} />
          <div className="bg-white dark:bg-gray-900 rounded-3xl w-full max-w-lg max-h-[80vh] flex flex-col relative z-[5001] shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 overflow-hidden animate-in zoom-in duration-300" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800">
              <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 tracking-tight flex items-center gap-2.5">
                <div className="p-1.5 bg-gray-100 dark:bg-gray-800 rounded-xl text-gray-500 dark:text-gray-400">
                  <Archive className="w-4 h-4" />
                </div>
                {t('chat.archivedSessions')}
              </h2>
              <button onClick={() => { setShowArchived(false); setExpandedArchivedId(null) }} className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-xl transition-colors">
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto">
              {archivedSessions.length === 0 && (
                <div className="px-5 py-12 text-center text-sm text-gray-400 dark:text-gray-500">{t('chat.noArchived')}</div>
              )}
              {archivedSessions.map(s => (
                <div key={s.id} className="border-b border-gray-50 dark:border-gray-800">
                  <div className="px-5 py-3">
                    <div className="flex items-center justify-between">
                      <div className="flex-1 min-w-0">
                        {editingArchivedId === s.id ? (
                          <div className="flex items-center gap-1">
                            <input
                              ref={archivedTitleInputRef}
                              value={editArchivedTitle}
                              onChange={e => setEditArchivedTitle(e.target.value.slice(0, 100))}
                              onKeyDown={e => { if (e.key === 'Enter') submitArchivedTitle(s.id); else if (e.key === 'Escape') setEditingArchivedId(null) }}
                              onBlur={() => submitArchivedTitle(s.id)}
                              className="flex-1 min-w-0 text-sm font-medium bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg px-2 py-1 outline-none focus:ring-2 focus:ring-sigma-600/20"
                            />
                            <button data-edit-btn onClick={() => submitArchivedTitle(s.id)} className="p-0.5 text-green-600 hover:bg-green-50 dark:hover:bg-green-900/30 rounded"><Check className="w-3 h-3" /></button>
                            <button data-edit-btn onClick={() => setEditingArchivedId(null)} className="p-0.5 text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"><X className="w-3 h-3" /></button>
                          </div>
                        ) : (
                          <button
                            onClick={() => toggleArchivedMessages(s.id)}
                            className="text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-sigma-600 transition-colors text-left truncate w-full"
                          >
                            {s.title || t('chat.untitled')}
                          </button>
                        )}
                        <div className="text-[10px] text-gray-400 dark:text-gray-500 mt-0.5">
                          {formatTimestamp(s.updated_at)}
                        </div>
                      </div>
                      <div className="flex items-center gap-0.5 ml-2">
                        <button onClick={() => startEditArchived(s)} className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title={t('common.rename')}>
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={() => unarchiveSession(s.id)} className="p-1 text-gray-400 dark:text-gray-500 hover:text-amber-500 hover:bg-amber-50 dark:hover:bg-amber-900/30 rounded-lg transition-colors" title={t('chat.unarchive')}>
                          <Archive className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={() => deleteArchivedSession(s.id)} className="p-1 text-gray-400 dark:text-gray-500 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 rounded-lg transition-colors" title={t('common.delete')}>
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                  </div>

                  {expandedArchivedId === s.id && (
                    <div className="px-5 pb-4 space-y-3 max-h-64 overflow-y-auto border-t border-gray-50 dark:border-gray-800 pt-3 bg-gray-50/30 dark:bg-gray-900/30">
                      {archivedMessages.length === 0 && (
                        <div className="text-xs text-gray-400 dark:text-gray-500 text-center py-4">{t('chat.noMessages')}</div>
                      )}
                      {archivedMessages.map((m, mi) => (
                        m.is_boundary ? (
                          <div key={mi} data-archived-msg-id={m.id} className="w-full my-1">
                            <CompactSummaryNote summary={m.content} />
                          </div>
                        ) : (
                        <div key={mi} data-archived-msg-id={m.id} className={`flex flex-col rounded-lg transition-colors duration-700 ${m.role === 'user' ? 'items-end' : 'items-start'} ${m.id === flashMessageId ? 'bg-amber-100/60 dark:bg-amber-900/25' : ''}`}>
                          <div className="flex items-center gap-1.5 mb-1">
                            <span className="text-[9px] font-black uppercase tracking-widest text-gray-400 dark:text-gray-500">{t(m.role === 'SiGMA' ? 'chat.roleSigma' : 'chat.roleUser')}</span>
                            {m.created_at && <span className="text-[8px] text-gray-300 dark:text-gray-600">{formatTimestamp(m.created_at)}</span>}
                          </div>
                          <div className={`max-w-full px-3 py-2 rounded-xl text-xs ${m.role === 'user' ? 'bg-sigma-100 dark:bg-sigma-600/20 text-sigma-800 dark:text-sigma-300' : 'bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 text-gray-600 dark:text-gray-400'}`}>
                            {m.content
                              ? (m.role === 'SiGMA'
                                  ? <MarkdownContent content={m.content} projectId={projectId} onCitation={onCitation} />
                                  : <div className="whitespace-pre-wrap">{m.content}</div>)
                              : (m.process?.length > 0 ? t('chat.toolCalls') : t('chat.emptyMsg'))}
                            {m.role === 'user' && (
                              <AttachmentStrip projectId={projectId} attachments={m.attachments} compact />
                            )}
                          </div>
                        </div>
                        )
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Chat Search Modal ── */}
      <ChatSearchModal
        isOpen={searchOpen}
        onClose={() => setSearchOpen(false)}
        projectId={projectId}
        currentSessionId={sessionId}
        onJump={handleSearchJump}
      />

      {/* ── Citation Viewer Modal ── */}
      <ModalOverlay isOpen={!!viewingCitation} onClose={() => setViewingCitation(null)}>
        <div className="p-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="p-2 bg-sigma-50 dark:bg-sigma-600/20 rounded-xl text-sigma-600">
              <TextQuote className="w-5 h-5" />
            </div>
            <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 tracking-tight">{t('chat.citation')}</h2>
          </div>
          {/* Library citations embed sigma:// pointers; make them clickable so
              the viewer can jump to the item. The dispatcher (onCitation)
              validates the URL, so a loose match here is safe. */}
          <div className="bg-gray-50 dark:bg-gray-900 rounded-2xl p-4 max-h-64 overflow-y-auto text-sm text-gray-700 dark:text-gray-300 leading-relaxed whitespace-pre-wrap font-mono">
            {(viewingCitation || '').split(/(sigma:\/\/[^\s)]+)/g).map((part, i) => (
              part.startsWith('sigma://') && onCitation ? (
                <button key={i} onClick={() => { setViewingCitation(null); onCitation(part) }}
                  className="text-sigma-600 hover:text-sigma-700 underline underline-offset-2 break-all">
                  {part}
                </button>
              ) : (
                <span key={i}>{part}</span>
              )
            ))}
          </div>
          <button onClick={() => setViewingCitation(null)} className="w-full mt-5 py-3 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors">
            {t('common.close')}
          </button>
        </div>
      </ModalOverlay>

      <ConfirmModal
        isOpen={!!deleteSessionTarget}
        onClose={() => setDeleteSessionTarget(null)}
        onConfirm={confirmDeleteSession}
        title={t('chat.deleteSession')}
        message={t('chat.deleteSessionConfirm')}
        danger
      />
      <ConfirmModal
        isOpen={!!deleteArchivedTarget}
        onClose={() => setDeleteArchivedTarget(null)}
        onConfirm={confirmDeleteArchived}
        title={t('chat.deleteArchived')}
        message={t('chat.deleteArchivedConfirm')}
        danger
      />
    </div>
  )
}
