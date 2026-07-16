/**
 * ChatPanel - Independent chat panel for Explore/Library tabs.
 * Each instance maintains its own message state.
 *
 * On page load, restores chat history from DB (survives refresh/close).
 * If a background task is running, silently reconnects to SSE for live updates.
 */
import { useState, useRef, useEffect, useCallback } from 'react'
import { useClickOutside } from '../hooks/useClickOutside'
import { MarkdownContent, ThinkingProcess } from './ChatShared'
import { Send, RotateCw, Bot, User, Zap, Square, Quote, X, Pencil, Check, ChevronDown, Archive, Trash2, Plus, TextQuote, Shield, Copy, Gauge, ArrowLeft, Image as ImageIcon, Menu, Loader2 } from 'lucide-react'
import { toastError, toastSuccess } from './Toast'
import { ModalOverlay, ConfirmModal } from './Modal'
import Toggle from './Toggle'
import { chatAPI, skillsAPI, permissionsAPI } from '../api'
import { createSSEStreamParser } from '../utils/sse'
import { storage, STORAGE_KEYS } from '../utils/storage'
import { copyToClipboard } from '../utils/clipboard'
import { useStore } from '../store/useStore'
import { useTranslation } from 'react-i18next'

/** Format ISO timestamp → "2026-01-01 12:34:22" */
function formatTimestamp(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('sv-SE', { hour12: false }).replace('T', ' ')
}

function formatTokenCount(value) {
  const n = Number(value || 0)
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}K`
  return String(n)
}

function isAgentToolName(tool) {
  return String(tool || '').toLowerCase() === 'agent'
}

function withTransientHint(process, content) {
  const hint = { type: 'hint', content, transient: true }
  if (process.length > 0) {
    const last = process[process.length - 1]
    if (last.type === 'hint' && last.transient) {
      return [...process.slice(0, -1), hint]
    }
  }
  return [...process.filter(s => !(s.type === 'hint' && s.transient && s.content === content)), hint]
}

function streamStatusText(data, t) {
  if (data?.status === 'retrying') {
    return t('chat.llmRetrying', {
      attempt: data.attempt || 1,
      maxAttempts: data.max_attempts || data.maxAttempts || 1,
    })
  }
  return data?.message || ''
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

function stripLeadingSlashCommand(text) {
  const trimmed = text.trimStart()
  const match = trimmed.match(/^\/[a-zA-Z][\w-]*(?=\s|$)/)
  if (!match) return text
  return trimmed.slice(match[0].length).trimStart()
}

function replaceLeadingSlashCommand(text, command) {
  const body = stripLeadingSlashCommand(text)
  return `${command}${body ? ` ${body}` : ' '}`
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

function displayMessageText(text, planLabel) {
  const stripped = text.trim()
  if (stripped === '/plan') return planLabel || 'Create a plan for the current task.'
  if (stripped.startsWith('/plan ') || stripped.startsWith('/plan\t')) {
    return stripped.slice(5).trimStart()
  }
  return text
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

export default function ChatPanel({ projectId, placeholder, citation = null, onClearCitation = null, onFileChanged = null, onAnnotationChanged = null, getUserState = null, onSaveBeforeChat = null, onCitation = null }) {
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
  const currentHintRef = useRef('')
  const abortRef = useRef(null)
  const textareaRef = useRef(null)
  const imageInputRef = useRef(null)
  const pendingInteractionRef = useRef(null)  // deferred dispatch to avoid cross-render setState
  const genRef = useRef(0)  // generation counter — prevents stale finally() from overwriting isStreaming
  const isUserAtBottom = useRef(true)
  const taskIdRef = useRef(null)  // current backend task ID for cancellation
  const stopAbortTimerRef = useRef(null)
  const stopRequestedRef = useRef(false)
  const abnormalTerminalRef = useRef(false)
  const historyCursorRef = useRef(null)
  const historyLoadingRef = useRef(false)
  const lastSkillsVersionRef = useRef(-1)  // dedup /skill submenu fetches across re-renders
  const historyModeRef = useRef('latest')

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
  const loadSessions = useCallback(async () => {
    if (!projectId) return
    try {
      const list = await chatAPI.listSessions(projectId)
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
      // Reuse in-flight initialization if another ChatPanel is already resolving
      let lock = sessionInitLocks.get(projectId)
      if (lock) {
        const sid = await lock
        if (!cancelled) { setSessionId(sid) }
        return
      }

      const promise = (async () => {
        let sid = storage.getSession(projectId)
        if (sid) {
          try {
            const list = await chatAPI.listSessions(projectId)
            storage.pruneSessionState(projectId, list.map(s => s.id))
            const found = list.find(s => s.id === sid)
            if (!found) sid = null
            else setSessionTitle(found.title || '')
          } catch { sid = null }
        }
        if (!sid) {
          try {
            const list = await chatAPI.listSessions(projectId)
            storage.pruneSessionState(projectId, list.map(s => s.id))
            if (list && list.length > 0) {
              sid = list[0].id
              setSessionTitle(list[0].title || '')
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
    return {
      messages,
      has_more: Boolean(response?.has_more),
      next_before_seq: response?.next_before_seq ?? null,
    }
  }

  function setHistoryPaging(page) {
    const cursor = page.has_more ? page.next_before_seq : null
    historyCursorRef.current = Number.isFinite(Number(cursor)) ? Number(cursor) : null
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
    const params = { limit: HISTORY_PAGE_SIZE }
    if (beforeSeq !== null) params.beforeSeq = beforeSeq
    return normalizeHistoryPage(await chatAPI.history(projectId, sessionId, params))
  }

  async function refreshLatestHistory({ dropLocal = true } = {}) {
    if (!projectId || !sessionId) return []
    const page = await fetchHistoryPage()
    if (historyModeRef.current === 'latest') setHistoryPaging(page)
    setMessages(prev => mergeHistoryMessages(prev, page.messages, { dropLocal }))
    return page.messages
  }

  // ---- Effect 2: Load data + reconnect SSE (reacts to sessionId changes) ----
  useEffect(() => {
    if (!projectId || !sessionId) return
    const gen = ++genRef.current
    let cancelled = false
    const abortController = new AbortController()

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
        history = page.messages
        setHistoryPaging(page)
      } catch (e) {
        console.error('Failed to load chat history:', e)
      }

      if (cancelled) return

      // 2. Load sessions list
      try {
        const list = await chatAPI.listSessions(projectId)
        setSessions(list)
        const cur = list.find(s => s.id === sessionId)
        if (cur) setSessionTitle(cur.title || '')
      } catch { /* ignore */ }

      // Load tasks for this session
      try {
        const tasks = await chatAPI.getTasks(projectId, sessionId)
        if (Array.isArray(tasks) && tasks.length > 0) {
          useStore.getState().setTaskList(tasks)
          useStore.getState().setExpandedTasks(true)
        }
      } catch { /* ignore */ }

      // 3. Check for active background task BEFORE committing messages.
      let active = null
      let activeCheckFailed = false
      try {
        active = await chatAPI.getActive(projectId, sessionId)
      } catch (e) {
        activeCheckFailed = true
        console.error('Failed to check active task:', e)
      }
      if (isActiveStateUnknown(active)) {
        activeCheckFailed = true
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
        if (active.status === 'awaiting_input' && active.interaction) {
          const interactionType = active.interaction.interaction_type
            || active.interaction.interaction_data?.interaction_type
          if (interactionType === 'permission') {
            // Permission approval checkpoint — restore the PermissionDialog.
            // The payload fields (tool/path/operation/content/description) are
            // nested under interaction_data; spreading the outer wrapper would
            // lose them.
            useStore.getState().setPendingPermission({
              ...active.interaction.interaction_data,
              session_id: sessionId,
            })
          } else {
            useStore.getState().setPendingInteraction({
              type: interactionType,
              data: active.interaction.interaction_data || active.interaction,
              sessionId,
            })
          }
        } else {
          // Task active but not awaiting input — drop any stale interaction state
          useStore.getState().clearPendingInteraction()
          useStore.getState().clearPendingPermission()
        }

        if (active.status === 'running' || active.status === 'queued' || active.status === 'cancelling') {
          const preserved = []
          while (history.length > 0 && history[history.length - 1].role === 'SiGMA') {
            const popped = history.pop()
            if (Array.isArray(popped.process)) {
              preserved.unshift(...popped.process)
            }
          }
          const cleanProcess = preserved.filter(s => !s.transient)
          history.push({ role: 'SiGMA', content: '', process: cleanProcess })
        }
      } else if (active?.status === 'stale' && active.session_id === sessionId) {
        const staleMessage = active.message || t('chat.staleFallback')
        history.push({
          role: 'SiGMA',
          content: '',
          process: [{ type: 'hint', content: staleMessage }],
        })
      } else if (activeCheckFailed && looksIncomplete) {
        // getActive failed (network error, etc.) but the last entry looks like
        // a checkpoint artefact.  Conservatively pop it and rebuild so the
        // user sees a "working" state instead of stale intermediate text.
        const preserved = []
        while (history.length > 0 && history[history.length - 1].role === 'SiGMA') {
          const popped = history.pop()
          if (Array.isArray(popped.process)) {
            preserved.unshift(...popped.process)
          }
        }
        const cleanProcess = preserved.filter(s => !s.transient)
        history.push({ role: 'SiGMA', content: '', process: cleanProcess })
      } else {
        // No active task for this session — clear stale interaction state so
        // modals from a previous session don't bleed across.
        useStore.getState().clearPendingInteraction()
        useStore.getState().clearPendingPermission()
      }

      // Commit the final message array in a single setState.
      setMessages(history)

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
        setIsStreaming(true)
        abortRef.current = abortController
        try {
          const body = await chatAPI.resumeStream(active.task_id, abortController.signal)
          if (cancelled) return
          const reader = body.getReader()
          const decoder = new TextDecoder()
          await processSSEStream(reader, decoder, abortController.signal)
        } catch {
          // resumeStream or reader failed
        } finally {
          // Only update if this effect is still current
          if (genRef.current === gen) {
            // Skip history reload when pausing for input — the live process
            // array (including subagent subSteps) is the source of truth for
            // the pending interaction, and refreshLatestHistory({ dropLocal })
            // would discard the anonymous streaming bubble.
            const pausing = !!(
              useStore.getState().pendingPermission
              || useStore.getState().pendingInteraction
              || pendingInteractionRef.current
            )
            if (!pausing && !abortController.signal.aborted && projectId && sessionId) {
              try {
                if (genRef.current === gen) await refreshLatestHistory()
              } catch { /* best-effort */ }
            }
            setIsStreaming(false)
          }
        }
      }
    }
    load()
    return () => {
      cancelled = true
      abortController.abort()
      // Also abort any user-initiated stream (handleSendMessage etc.)
      if (abortRef.current && abortRef.current !== abortController) {
        abortRef.current.abort()
      }
      abortRef.current = null
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
  }, [projectId, sessionId, hasMoreHistory, isLoadingHistory])

  async function loadOlderHistory() {
    const cursor = historyCursorRef.current
    if (!projectId || !sessionId || !hasMoreHistory || historyLoadingRef.current || cursor === null) return
    const el = chatScrollRef.current
    const prevHeight = el?.scrollHeight || 0
    historyLoadingRef.current = true
    setIsLoadingHistory(true)
    try {
      const page = await fetchHistoryPage(cursor)
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

  useEffect(() => {
    if (chatScrollRef.current && isUserAtBottom.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight
    }
  }, [messages])

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

  async function handleInteractionStream(streamBody) {
    setIsStreaming(true)
    const gen = genRef.current
    // Immediately remove awaiting_input step on user submission
    setMessages(prev => {
      const newMsgs = [...prev]
      const lastIdx = newMsgs.length - 1
      if (lastIdx >= 0) {
        const lastMsg = { ...newMsgs[lastIdx] }
        lastMsg.process = (lastMsg.process || []).filter(s => s.type !== 'awaiting_input')
        newMsgs[lastIdx] = lastMsg
      }
      return newMsgs
    })
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const body = await chatAPI.stream(projectId, streamBody, controller.signal)
      const reader = body.getReader()
      const decoder = new TextDecoder()
      await processSSEStream(reader, decoder, controller.signal)
    } catch (e) {
      toastError(e.message || t('chat.toast.resumeFailed'))
    } finally {
      if (genRef.current === gen) {
        // Do NOT call refreshLatestHistory here. The resume stream appends to
        // the existing (anonymous) streaming bubble, which carries the live
        // process array — including subagent subSteps — that has no server-side
        // counterpart. refreshLatestHistory({ dropLocal: true }) would discard
        // it, erasing all subagent content from the UI. The pendingPermission/
        // pendingInteraction guards added elsewhere are ineffective here because
        // the dialog's onResolved() clears them before this function starts.
        // Server history syncs on the next natural message-send completion.
        setIsStreaming(false)
      }
    }
  }

  useEffect(() => {
    if (!streamInteractionRequest) return
    useStore.getState().setStreamInteractionRequest(null)
    handleInteractionStream(streamInteractionRequest)
  }, [streamInteractionRequest])

  // ---- Handle auto-message triggered from outside (e.g. LogModal "Ask SiGMA") ----
  const pendingAutoMessage = useStore(s => s.pendingAutoMessage)

  useEffect(() => {
    if (!pendingAutoMessage) return
    useStore.getState().setPendingAutoMessage(null)
    // Read the latest streaming/awaiting state from refs; the closure values
    // here are from the render when the message arrived and can be stale.
    if (isStreamingRef.current || awaitingRef.current) return
    handleDirectSend(pendingAutoMessage.text)
  }, [pendingAutoMessage])

  /** Send a message programmatically (not from user input). */
  async function handleDirectSend(text) {
    if (!text || isStreamingRef.current || awaitingRef.current || !projectId || !sessionId) return
    setIsStreaming(true)
    const gen = genRef.current
      setMessages(prev => [...prev, { role: 'user', content: displayMessageText(text, t('chat.planDisplay')), created_at: new Date().toISOString() }])
    setMessages(prev => [...prev, { role: 'SiGMA', content: '', process: [] }])
    currentHintRef.current = t('chat.thinking')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const body = await chatAPI.stream(projectId, {
        message: text,
        session_id: sessionId,
        ...(getUserState ? { user_state: getUserState() } : {}),
        ...(tokenBudget ? { token_budget: tokenBudget } : {}),
      }, controller.signal)
      if (controller.signal.aborted) return
      const reader = body.getReader()
      const decoder = new TextDecoder()
      await processSSEStream(reader, decoder, controller.signal)
    } catch (err) {
      if (err.name === 'AbortError') return
      toastError(t('chat.toast.connectionFailed', { message: err.message || '' }))
    } finally {
      if (genRef.current === gen) {
        // Reload history to get real message IDs and can_edit flags from server.
        // Must happen before setIsStreaming(false) so the edit buttons only
        // appear on messages with real server IDs.
        // Skip when pausing for input — see handleSendMessage for rationale.
        const pausing = !!(
          useStore.getState().pendingPermission
          || useStore.getState().pendingInteraction
          || pendingInteractionRef.current
        )
        if (!pausing && !controller.signal.aborted && projectId && sessionId) {
          try {
            if (genRef.current === gen) await refreshLatestHistory()
          } catch { /* best-effort */ }
        }
        setIsStreaming(false)
      }
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
  async function openArchived() {
    setShowDropdown(false)
    try {
      const list = await chatAPI.listSessions(projectId, { include_archived: true })
      const archived = list.filter(s => s.is_archived)
      setArchivedSessions(archived)
      setShowArchived(true)
    } catch { /* ignore */ }
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
  async function processSSEStream(reader, decoder, abortSignal) {
    const parser = createSSEStreamParser({
      onEvent(type, data) {
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
              const lastIdx = newMsgs.length - 1
              if (lastIdx >= 0 && newMsgs[lastIdx].role === 'SiGMA') {
                newMsgs[lastIdx] = { ...newMsgs[lastIdx], usage: data.usage }
              }
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
        setMessages(prev => {
          const newMsgs = [...prev]
          const lastIdx = newMsgs.length - 1
          if (lastIdx < 0) return prev
          const lastMsg = { ...newMsgs[lastIdx] }
          const currentProcess = [...(lastMsg.process || [])]

          if (type === 'thought') {
            // Only display explicit status messages. Reasoning token deltas
            // arrive as data.content and are intentionally hidden from chat UI.
            if (data.message) {
              currentHintRef.current = data.message
              lastMsg.process = withTransientHint(currentProcess, data.message)
            } else {
              lastMsg.process = currentProcess
            }
          } else if (type === 'stream_status') {
            const statusMessage = streamStatusText(data, t)
            if (statusMessage) {
              const processForStatus = data.status === 'retrying'
                ? currentProcess.filter(s => s.type !== 'streaming_text')
                : currentProcess
              lastMsg.process = withTransientHint(processForStatus, statusMessage)
            } else {
              lastMsg.process = currentProcess
            }
          } else if (type === 'step') {
            lastMsg.process = [...currentProcess, data]
          } else if (type === 'step_delta') {
            const lastIdx = currentProcess.length - 1
            if (lastIdx >= 0 && currentProcess[lastIdx].type === 'agent' && currentProcess[lastIdx].agent === data.agent) {
              currentProcess[lastIdx] = { ...currentProcess[lastIdx], content: currentProcess[lastIdx].content + data.content }
            } else {
              currentProcess.push({ type: 'agent', agent: data.agent || t('chat.thinkingAgent'), content: data.content })
            }
            lastMsg.process = currentProcess
          } else if (type === 'delta') {
            const streamIdx = currentProcess.findLastIndex(s => s.type === 'streaming_text')
            if (streamIdx >= 0) {
              currentProcess[streamIdx] = { ...currentProcess[streamIdx], content: currentProcess[streamIdx].content + data.content }
            } else {
              currentProcess.push({ type: 'streaming_text', content: data.content })
            }
            lastMsg.process = currentProcess
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
            finalProcess.push(toolStep)
            lastMsg.process = finalProcess
          } else if (type === 'tool_end') {
            const updated = currentProcess.map(s => {
              // Match by tool_call_id first, then by tool name + running status
              if (data.tool_call_id && s._toolCallId === data.tool_call_id) {
                return { ...s, result: data.result_summary, status: 'done' }
              }
              if (s.type === 'tool' && s.tool === data.tool && s.status === 'running' && !data.tool_call_id) {
                return { ...s, result: data.result_summary, status: 'done' }
              }
              return s
            })
            lastMsg.process = updated
          } else if (type === 'compact_start') {
            currentHintRef.current = data.message || t('chat.compacting')
            lastMsg.process = withTransientHint(currentProcess, data.message || t('chat.compacting'))
          } else if (type === 'compact_done') {
            setContextStats(data)
            currentHintRef.current = t('chat.thinking')
            currentProcess.push({ type: 'hint', content: t('chat.compacted') })
            lastMsg.process = currentProcess
            refreshCanEditFlags()
          } else if (type === 'agent_start') {
            currentProcess.push({ type: 'agent_call', agent: data.agent, target_agent: data.target_agent || data.agent, description: data.description || '' })
            lastMsg.process = currentProcess
          } else if (type === 'agent_end') {
            lastMsg.process = [...currentProcess, { type: 'agent_result', agent: data.agent, result: (data.result || '').slice(0, 200) }]
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
              // Last resort: match the last running Agent step
              agentStepIdx = currentProcess.findLastIndex(
                s => s.type === 'tool' && isAgentToolName(s.tool) && s.status === 'running'
              )
            }
            if (agentStepIdx >= 0) {
              const agentStep = { ...currentProcess[agentStepIdx] }
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
                subSteps.splice(0, subSteps.length, ...withTransientHint(subSteps, innerData.message || t('chat.compacting')))
              } else if (innerType === 'compact_done') {
                subSteps.push({ type: 'hint', content: t('chat.compacted') })
                refreshCanEditFlags()
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
                subSteps.push(subStep)
              } else if (innerType === 'tool_end') {
                // Match by tool_call_id first, then by tool name
                let matched = false
                if (innerData.tool_call_id) {
                  for (let i = subSteps.length - 1; i >= 0; i--) {
                    if (subSteps[i].type === 'tool' && subSteps[i]._toolCallId === innerData.tool_call_id) {
                      subSteps[i] = { ...subSteps[i], result: innerData.result_summary, status: 'done' }
                      matched = true
                      break
                    }
                  }
                }
                if (!matched) {
                  for (let i = subSteps.length - 1; i >= 0; i--) {
                    if (subSteps[i].type === 'tool' && subSteps[i].tool === innerData.tool && subSteps[i].status === 'running') {
                      subSteps[i] = { ...subSteps[i], result: innerData.result_summary, status: 'done' }
                      break
                    }
                  }
                }
              } else if (innerType === 'awaiting_input') {
                subSteps.push({ type: 'awaiting_input', interaction_type: innerData.interaction_type, data: innerData, transient: true })
                if (innerData.interaction_type === 'permission') {
                  useStore.getState().setPendingPermission({ ...innerData, session_id: sessionId })
                } else {
                  pendingInteractionRef.current = { type: innerData.interaction_type, data: innerData, sessionId }
                }
              }

              agentStep.subSteps = subSteps
              agentStep.agentType = data.agent_type
              agentStep.agentRunId = data.agent_run_id
              currentProcess[agentStepIdx] = agentStep
            }
            lastMsg.process = currentProcess
          } else if (type === 'awaiting_input') {
            currentProcess.push({ type: 'awaiting_input', interaction_type: data.interaction_type, data: data, transient: true })
            lastMsg.process = currentProcess
            if (data.interaction_type === 'permission') {
              useStore.getState().setPendingPermission({ ...data, session_id: sessionId })
            } else {
              pendingInteractionRef.current = { type: data.interaction_type, data: data, sessionId }
            }
          } else if (type === 'done') {
            const streamIdx = currentProcess.findLastIndex(s => s.type === 'streaming_text')
            if (streamIdx >= 0) {
              lastMsg.content = currentProcess[streamIdx].content
            } else if (!lastMsg.content && currentProcess.some(s => s.content === t('chat.compacted'))) {
              lastMsg.content = t('chat.compacted')
            }
            lastMsg.created_at = new Date().toISOString()
            if (data.usage) {
              lastMsg.usage = data.usage
            }
            const lastStep = currentProcess[currentProcess.length - 1]
            const isPausingForInput = lastStep?.type === 'awaiting_input'
            if (isPausingForInput) {
              lastMsg.process = currentProcess.filter(s => s.type !== 'streaming_text')
            } else {
              lastMsg.process = currentProcess.filter(s => !s.transient && s.type !== 'streaming_text')
            }
          } else if (type === 'error') {
            const content = data.content || data.error || data.message || t('chat.toast.unknownError')
            lastMsg.content = content
            lastMsg.created_at = new Date().toISOString()
            lastMsg.process = currentProcess.filter(s => !s.transient && s.type !== 'streaming_text')
            if (data.usage) {
              lastMsg.usage = data.usage
            }
          } else if (type === 'cancelled') {
            if (data.usage) {
              lastMsg.usage = data.usage
            }
          } else if (type === 'file_changed') {
            if (onFileChanged) onFileChanged(data.paths || [])
          } else if (type === 'annotation_changed') {
            if (onAnnotationChanged) onAnnotationChanged(data.file_name || '')
          }

          newMsgs[lastIdx] = lastMsg
          return newMsgs
        })
      },
    })

    await parser.start(reader, decoder, abortSignal)
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

  async function abortStoppedStream(controller) {
    controller.abort()
    abortRef.current = null
    stopAbortTimerRef.current = null
    stopRequestedRef.current = false
    if (projectId && sessionId) {
      try {
        await refreshLatestHistory()
      } catch { /* best-effort */ }
    }
    setIsStreaming(false)
  }

  function isActiveStateUnknown(active) {
    return active?.status === 'unknown'
  }

  // ---- Stop an ongoing stream ----
  async function handleStop() {
    if (stopRequestedRef.current) return

    let taskId = taskIdRef.current
    const controller = abortRef.current
    if (!projectId) {
      abortRef.current = null
      clearStopAbortTimer()
      stopRequestedRef.current = false
      setIsStreaming(false)
      return
    }
    if (!controller) {
      stopRequestedRef.current = true
      if (sessionId) {
        try {
          const active = await chatAPI.getActive(projectId, sessionId)
          if (isActiveStateUnknown(active)) {
            await showStopStillRunning()
            return
          }
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
      await showStopStillRunning()
      return
    }

    try { await chatAPI.cancel(projectId, taskId) } catch (e) { console.warn('Failed to cancel task:', e) }

    // Keep the SSE stream open so the backend can deliver cancelled/error
    // usage. If the worker stays active, surface that instead of pretending
    // the stop succeeded.
    const scheduleStopFallback = (attempt = 0) => {
      clearStopAbortTimer()
      stopAbortTimerRef.current = setTimeout(async () => {
        if (abortRef.current !== controller) return
        if (!sessionId) {
          await showStopStillRunning()
          return
        }
        try {
          const active = await chatAPI.getActive(projectId, sessionId)
          if (isActiveStateUnknown(active)) {
            await showStopStillRunning()
            return
          }
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
        await abortStoppedStream(controller)
      }, 10000)
    }
    scheduleStopFallback()
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

  /** Best-effort: reload can_edit flags from the server after compaction. */
  async function refreshCanEditFlags() {
    if (!projectId || !sessionId) return
    try {
      const page = await fetchHistoryPage()
      const canEditMap = new Map()
      for (const m of page.messages) {
        if (m.id) canEditMap.set(m.id, m.can_edit)
      }
      const boundarySeq = Math.max(
        ...page.messages
          .filter(m => m.is_boundary)
          .map(m => messageSeq(m))
          .filter(seq => seq !== null),
      )
      setMessages(prev => {
        const updated = prev.map(m => {
          if (m.id && canEditMap.has(m.id)) {
            const nextCanEdit = canEditMap.get(m.id)
            return m.can_edit === nextCanEdit ? m : { ...m, can_edit: nextCanEdit }
          }
          const seq = messageSeq(m)
          if (m.role === 'user' && Number.isFinite(boundarySeq) && seq !== null && seq <= boundarySeq) {
            return m.can_edit === false ? m : { ...m, can_edit: false }
          }
          return m
        })
        return mergeHistoryMessages(updated, page.messages, { dropLocal: false })
      })
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
    if (onSaveBeforeChat) {
      try { await onSaveBeforeChat() } catch { /* don't block chat on save failure */ }
    }

    const editIndex = messages.findIndex(m => m.id === messageId)
    setIsStreaming(true)
    currentHintRef.current = t('chat.thinking')
    const gen = genRef.current
    const controller = new AbortController()
    abortRef.current = controller
    let streamStarted = false
    try {
      const body = await chatAPI.editMessage(projectId, sessionId, {
        message_id: messageId,
        message: text,
        attachments,
        ...(getUserState ? { user_state: getUserState() } : {}),
        ...(tokenBudget ? { token_budget: tokenBudget } : {}),
      }, controller.signal)
      if (controller.signal.aborted) return
      streamStarted = true
      setMessages(prev => {
        const idx = prev.findIndex(m => m.id === messageId)
        const kept = idx >= 0 ? prev.slice(0, idx) : prev
        return [
          ...kept,
          { role: 'user', content: displayMessageText(text, t('chat.planDisplay')), attachments, created_at: new Date().toISOString() },
          { role: 'SiGMA', content: '', process: [] },
        ]
      })
      cancelEditMessage()
      const reader = body.getReader()
      const decoder = new TextDecoder()
      await processSSEStream(reader, decoder, controller.signal)
    } catch (err) {
      if (err.name !== 'AbortError') {
        toastError(t('chat.toast.editFailed', { message: err.message || '' }))
        if (editIndex >= 0) {
          try {
            const page = await fetchHistoryPage()
            historyModeRef.current = 'latest'
            setHistoryPaging(page)
            setMessages(page.messages)
          } catch { /* keep optimistic state */ }
        }
      }
    } finally {
      if (genRef.current === gen) {
        if (streamStarted && !controller.signal.aborted && projectId && sessionId) {
          try {
            if (genRef.current === gen) await refreshLatestHistory()
          } catch { /* best-effort */ }
        }
        setIsStreaming(false)
      }
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
    setIsUploadingAttachment(true)
    try {
      const uploaded = await Promise.all(imageFiles.map(file => chatAPI.uploadAttachment(projectId, sessionId, file)))
      setPendingAttachments(prev => [...prev, ...uploaded])
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
    if ((!msg && attachments.length === 0) || isStreaming || isUploadingAttachment || awaiting || !projectId) return

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

    // Save editor content before sending so AI sees the latest file
    if (onSaveBeforeChat) {
      try {
        const saved = await onSaveBeforeChat()
        if (!saved) return
      } catch {
        return
      }
    }

    setChatInput('')
    setPendingAttachments([])
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
    setIsStreaming(true)
    const gen = genRef.current

    setMessages(prev => [...prev, {
      role: 'user', content: displayMessageText(msg, t('chat.planDisplay')), attachments, created_at: new Date().toISOString(),
      ...(citation?.fullText ? { citation: citation.fullText } : {}),
    }])
    setMessages(prev => [...prev, { role: 'SiGMA', content: '', process: [] }])

    currentHintRef.current = isCompactCommand ? t('chat.compacting') : t('chat.thinking')

    const controller = new AbortController()
    abortRef.current = controller
    let streamStarted = false

    try {
      const streamBody = { message: msg || t('chat.inspectImage') }
      if (sessionId) streamBody.session_id = sessionId
      if (!isCompactCommand && attachments.length > 0) streamBody.attachments = attachments
      if (!isCompactCommand && getUserState) streamBody.user_state = getUserState()
      if (!isCompactCommand && tokenBudget) streamBody.token_budget = tokenBudget
      if (!isCompactCommand && onClearCitation) onClearCitation()

      const body = await chatAPI.stream(projectId, streamBody, controller.signal)
      if (controller.signal.aborted) return
      streamStarted = true
      const reader = body.getReader()
      const decoder = new TextDecoder()
      processSSEStream(reader, decoder, controller.signal).finally(async () => {
        if (genRef.current !== gen) return
        // Reload history to get real message IDs and can_edit flags.
        // Skip for compact commands — their bubbles are local-only (not persisted)
        // and would be lost if we replaced messages with server data.
        // Skip when pausing for input (awaiting_input / permission) — the live
        // process array (including subagent subSteps) is the source of truth for
        // the pending interaction, and refreshLatestHistory({ dropLocal: true })
        // would discard the anonymous streaming bubble, erasing all subagent
        // progress from the UI. Server history syncs on the next natural end.
        const pausing = !!(
          useStore.getState().pendingPermission
          || useStore.getState().pendingInteraction
          || pendingInteractionRef.current
        )
        if (!isCompactCommand && !pausing && !controller.signal.aborted && projectId && sessionId) {
          try {
            if (genRef.current === gen) await refreshLatestHistory()
          } catch { /* best-effort */ }
        }
        setIsStreaming(false)
      })
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
      if (genRef.current === gen) setIsStreaming(false)
    }
  }

  // ---- Render helpers ----
  const activeSessions = sessions.filter(s => !s.is_archived)
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

  function applySlashSuggestion(command) {
    // Suggestions are only shown when the input is exactly `/` or `/word` with
    // nothing after, so we can replace the whole input. (Using
    // replaceLeadingSlashCommand here would treat a bare `/` as body content
    // and produce `/compact /`.)
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
    <div className="flex-1 flex flex-col bg-gray-50/30 dark:bg-gray-900 overflow-hidden">
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
              <div className="px-3 py-2 border-b border-gray-100 dark:border-gray-800">
                <span className="text-[10px] font-black uppercase tracking-widest text-gray-400">{t('chat.sessions')}</span>
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
                  onClick={openArchived}
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

      <div ref={chatScrollRef} onScroll={handleChatScroll} className="flex-1 overflow-y-auto p-4 space-y-6 scroll-smooth">
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
          return (
            <div key={m.id || i} className={`group/message flex flex-col ${m.role === 'user' ? 'items-end' : 'items-start'}`}>
              <div className="flex items-center gap-2 mb-1.5 px-1">
                {m.role === 'SiGMA' ? <Zap className="w-3 h-3 text-sigma-600" /> : <User className="w-3 h-3 text-gray-400 dark:text-gray-500" />}
                <span className="text-[10px] font-black uppercase tracking-widest text-gray-400 dark:text-gray-500">{m.role}</span>
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
                {isStreaming && i === messages.length - 1 && !m.content && (
                  <div className="flex items-center gap-2 text-gray-400 dark:text-gray-500 mt-1">
                    <RotateCw className="w-3.5 h-3.5 animate-spin" />
                    <span className="text-[11px] font-bold italic tracking-wider animate-pulse">{currentHintRef.current}</span>
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

      <div className="p-4 bg-white dark:bg-gray-900 border-t border-gray-100 dark:border-gray-800 space-y-3 shadow-[0_-10px_20px_rgba(0,0,0,0.02)]">
        {tokenBudget && (
          <div className="flex items-center justify-between rounded-xl border border-amber-100 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-900/30 px-3 py-2 text-[10px] font-medium text-amber-700 dark:text-amber-300">
            <span>{t('chat.tokenBudget')}{formatTokenCount(tokenBudget)}{t('chat.tokenBudgetFor')}</span>
            <button onClick={() => { setTokenBudget(null); if (projectId && sessionId) storage.removeBudget(projectId, sessionId) }} className="p-0.5 rounded hover:bg-amber-100 dark:hover:bg-amber-800/50" title={t('chat.clearBudget')}>
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        {contextStats && (() => {
          const current = Number(contextStats.current_tokens || 0)
          const threshold = Number(contextStats.compact_threshold || 0)
          const max = Number(contextStats.max_context_length || 0)
          const pct = max > 0 ? Math.min(100, (current / max) * 100) : 0
          const thresholdPct = max > 0 ? Math.min(100, (threshold / max) * 100) : 0
          return (
            <div className="space-y-1">
              <div className="flex items-center justify-between text-[10px] font-mono text-gray-400 dark:text-gray-500">
                <span>{t('chat.ctxLength')}{formatTokenCount(current)}, {pct.toFixed(1)}%</span>
                <span>{t('chat.threshold')}{formatTokenCount(threshold)}{t('chat.maxToken')}{formatTokenCount(max)}</span>
              </div>
              <div className="relative h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                <div className="absolute inset-y-0 left-0 bg-pink-200/70" style={{ width: '100%' }} />
                <div className="absolute inset-y-0 left-0 bg-green-200/80" style={{ width: `${thresholdPct}%` }} />
                <div className="absolute inset-y-0 left-0 bg-blue-300/80" style={{ width: `${pct}%` }} />
              </div>
            </div>
          )
        })()}
        {citation && (
          <div className="bg-blue-50/50 dark:bg-blue-900/30 border border-blue-100 dark:border-blue-800/50 rounded-xl px-3 py-2 flex items-start gap-3 animate-in slide-in-from-bottom-2 duration-200" title={citation.fullText}>
            <Quote className="w-3.5 h-3.5 text-blue-400 mt-1 flex-shrink-0" />
            <div className="flex-1 text-[11px] font-medium text-blue-700 dark:text-blue-300 line-clamp-1">{citation.text}</div>
            {onClearCitation && (
              <button onClick={onClearCitation} className="p-1 hover:bg-blue-100 dark:hover:bg-blue-800/50 text-blue-400 rounded-lg transition-colors"><X className="w-3 h-3" /></button>
            )}
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
          <div className="flex items-end gap-2 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded-2xl px-4 py-2 focus-within:ring-2 focus-within:ring-sigma-600/20 focus-within:bg-white dark:focus-within:bg-gray-900 transition-all">
          <input
            ref={imageInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            className="hidden"
            onChange={e => uploadImageFiles(e.target.files)}
          />
          <button
            ref={autoApproveBtnRef}
            onClick={() => {
              const opening = !showAutoApproveMenu
              setShowAutoApproveMenu(opening)
              if (opening && autoApproveBtnRef.current) {
                setSettingsPanel('main')
                const rect = autoApproveBtnRef.current.getBoundingClientRect()
                setAutoApproveMenuPos({
                  bottom: window.innerHeight - rect.top + 4,
                  left: Math.max(4, rect.left),
                })
              }
            }}
            className={`p-2 rounded-xl transition-colors flex-shrink-0 ${showAutoApproveMenu ? 'text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20' : 'text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'}`}
            title={t('chat.autoApproveSettings')}
          >
            <Menu className="w-4 h-4" />
          </button>
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
          <button onClick={isStreaming ? handleStop : handleSendMessage}
            disabled={isUploadingAttachment || awaiting}
            className={`p-2 text-white rounded-xl transition-all active:scale-95 disabled:opacity-50 ${isStreaming ? 'bg-red-500 hover:bg-red-600' : 'bg-sigma-600 hover:bg-sigma-700'}`}>
            {isStreaming ? <Square className="w-4 h-4" /> : isUploadingAttachment ? <RotateCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          </button>
          </div>
        </div>
      </div>

      {/* ── Auto-approve settings menu ── */}
      {showAutoApproveMenu && (() => {
        const toolTypes = [
          { key: 'file_external', label: t('permission.cat.fileExternal'), desc: t('permission.cat.fileExternalDesc') },
          { key: 'file_internal', label: t('permission.cat.fileInternal'), desc: t('permission.cat.fileInternalDesc') },
          { key: 'bash', label: t('permission.cat.bash'), desc: t('permission.cat.bashDesc') },
          { key: 'notebook', label: t('permission.cat.notebook'), desc: t('permission.cat.notebookDesc') },
        ]
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
              {toolTypes.map(({ key, label, desc }) => {
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
                        <div key={mi} className={`flex flex-col ${m.role === 'user' ? 'items-end' : 'items-start'}`}>
                          <div className="flex items-center gap-1.5 mb-1">
                            <span className="text-[9px] font-black uppercase tracking-widest text-gray-400 dark:text-gray-500">{m.role === 'SiGMA' ? t('chat.roleSigma') : m.role}</span>
                            {m.created_at && <span className="text-[8px] text-gray-300 dark:text-gray-600">{formatTimestamp(m.created_at)}</span>}
                          </div>
                          <div className={`max-w-full px-3 py-2 rounded-xl text-xs ${m.role === 'user' ? 'bg-sigma-100 dark:bg-sigma-600/20 text-sigma-800 dark:text-sigma-300' : 'bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 text-gray-600 dark:text-gray-400'}`}>
                            {m.content ? (m.content.length > 300 ? m.content.slice(0, 300) + '…' : m.content) : (m.process?.length > 0 ? t('chat.toolCalls') : t('chat.emptyMsg'))}
                            {m.role === 'user' && (
                              <AttachmentStrip projectId={projectId} attachments={m.attachments} compact />
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Citation Viewer Modal ── */}
      <ModalOverlay isOpen={!!viewingCitation} onClose={() => setViewingCitation(null)}>
        <div className="p-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="p-2 bg-sigma-50 dark:bg-sigma-600/20 rounded-xl text-sigma-600">
              <TextQuote className="w-5 h-5" />
            </div>
            <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 tracking-tight">{t('chat.citation')}</h2>
          </div>
          <div className="bg-gray-50 dark:bg-gray-900 rounded-2xl p-4 max-h-64 overflow-y-auto text-sm text-gray-700 dark:text-gray-300 leading-relaxed whitespace-pre-wrap font-mono">
            {viewingCitation}
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
