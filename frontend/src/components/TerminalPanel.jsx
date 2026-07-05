/**
 * TerminalPanel — bottom slide-up panel containing terminal instances.
 *
 * Each terminal is identified by a stable integer *slot* (1, 2, 3…).
 * On mount the component queries ``GET /api/v1/terminal/{projectId}/sessions``
 * to discover existing sessions.  The backend is the sole source of truth.
 *
 * Keepalive: closing the panel via the ⌄ button hides it without destroying
 * terminals — the PTY sessions stay alive.  Clicking × on a tab kills that
 * session.  When a shell exits (logout / Ctrl-D) the tab is auto-closed.
 *
 * Terminal state is synced to Zustand (in-memory) so it survives
 * navigating between projects within the same page session.
 */
import { useState, useRef, useCallback, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { X, Plus, ChevronDown } from 'lucide-react'
import { useStore } from '../store/useStore'
import { terminalAPI } from '../api'
import TerminalInstance from './TerminalInstance'
import { markSlotForKill } from '../hooks/useTerminal'

const MIN_HEIGHT = 150
const MAX_HEIGHT_RATIO = 0.7
const DEFAULT_HEIGHT = 300

/** Find the smallest positive integer not present in *taken*. */
function nextSlot(taken) {
  const set = new Set(taken)
  let n = 1
  while (set.has(n)) n++
  return n
}

function moveItem(arr, fromIdx, toIdx) {
  const next = arr.slice()
  const [item] = next.splice(fromIdx, 1)
  next.splice(toIdx, 0, item)
  return next
}

export default function TerminalPanel({ projectId, visible }) {
  const { t } = useTranslation()
  const toggleTerminal = useStore(s => s.toggleTerminal)

  // ── Session discovery state ──
  const [discovering, setDiscovering] = useState(true)

  const [terminals, setTerminals] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const [isResizing, setIsResizing] = useState(false)
  const panelRef = useRef(null)
  const startYRef = useRef(0)
  const startHeightRef = useRef(0)

  // ── Tab drag state ──
  const dragIdxRef = useRef(null)

  // ── Sync terminal state to Zustand (survives project navigation within same page session) ──
  useEffect(() => {
    useStore.getState().setTerminalState(projectId, { terminals, activeId })
  }, [terminals, activeId, projectId])

  // Track if the panel has ever been opened in this view session
  const [hasOpened, setHasOpened] = useState(visible)
  useEffect(() => {
    if (visible) setHasOpened(true)
  }, [visible])

  // ── Session discovery — always ask backend (single source of truth) ──
  useEffect(() => {
    let cancelled = false
    setDiscovering(true)
    terminalAPI.listSessions(projectId)
      .then(res => {
        if (cancelled) return
        const sessions = res.sessions
        if (sessions && sessions.length > 0) {
          const terms = sessions.map(s => ({ id: `t-${s.slot}`, slot: s.slot }))
          setTerminals(terms)
          setActiveId(terms[0].id)
        }
      })
      .catch(() => { /* network error — fall through to auto-create */ })
      .finally(() => { if (!cancelled) setDiscovering(false) })
    return () => { cancelled = true }
  }, [projectId])

  // ── Create first terminal when panel opens and no sessions exist ──
  useEffect(() => {
    if (visible && !discovering && terminals.length === 0) {
      const slot = 1
      const term = { id: `t-${slot}`, slot }
      setTerminals([term])
      setActiveId(term.id)
    }
  }, [visible, discovering, terminals.length])

  // ── Add terminal (smallest unused slot) ──
  const addTerminal = useCallback(() => {
    setTerminals(prev => {
      const slot = nextSlot(prev.map(t => t.slot))
      const term = { id: `t-${slot}`, slot }
      setActiveId(term.id)
      return [...prev, term]
    })
  }, [])

  // ── Close terminal. *shouldKill*: true for X button, false for takeover ──
  const closeTerminal = useCallback((slot, shouldKill = true) => {
    if (shouldKill) markSlotForKill(slot)
    setTerminals(prev => {
      const next = prev.filter(t => t.slot !== slot)
      if (next.length === 0) {
        if (useStore.getState().showTerminal) {
          requestAnimationFrame(() => useStore.getState().toggleTerminal())
        }
        return []
      }
      setActiveId(activeId => {
        const stillActive = next.find(t => t.id === activeId)
        return stillActive ? activeId : next[next.length - 1].id
      })
      return next
    })
  }, [])

  // ── Shell exit / takeover callback from useTerminal ──
  const handleShellExit = useCallback((slot, reason) => {
    // 'taken_over': session alive on backend (owned by another tab) — don't kill
    // 'shell_exit':  session already dead on backend — kill is harmless cleanup
    closeTerminal(slot, reason !== 'taken_over')
  }, [closeTerminal])

  // ── Tab drag handlers ──
  const handleTabDragStart = useCallback((e, idx) => {
    dragIdxRef.current = idx
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', '')
  }, [])

  const handleTabDragOver = useCallback((e, idx) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    const from = dragIdxRef.current
    if (from === null || from === idx) return
    setTerminals(prev => moveItem(prev, from, idx))
    dragIdxRef.current = idx
  }, [])

  const handleTabDragEnd = useCallback(() => {
    dragIdxRef.current = null
  }, [])

  // ── Panel drag-to-resize ──
  const handleResizeStart = useCallback((e) => {
    e.preventDefault()
    setIsResizing(true)
    startYRef.current = e.clientY
    startHeightRef.current = height

    const onMove = (ev) => {
      setHeight(Math.max(MIN_HEIGHT,
        Math.min(window.innerHeight * MAX_HEIGHT_RATIO,
          startHeightRef.current + (startYRef.current - ev.clientY))))
    }
    const onUp = () => {
      setIsResizing(false)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }

    document.body.style.cursor = 'ns-resize'
    document.body.style.userSelect = 'none'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [height])

  // ── Loading state ──
  if (discovering) return null

  if (terminals.length === 0) return null

  // Prevent rendering terminal instances (and connecting WS) when panel is hidden on initial load
  if (!hasOpened && !visible) return null

  return (
    <div
      ref={panelRef}
      className="absolute bottom-0 left-0 right-0 z-[200] flex flex-col border-t border-gray-700 bg-[#1e1e1e] rounded-t-lg shadow-2xl"
      style={{ height, maxHeight: `${MAX_HEIGHT_RATIO * 100}vh`, display: visible ? 'flex' : 'none' }}
    >
      {/* Drag handle */}
      <div
        className="flex-shrink-0 h-2 cursor-ns-resize hover:bg-white/5 transition-colors flex items-center justify-center"
        onMouseDown={handleResizeStart}
      >
        <div className="w-10 h-0.5 rounded-full bg-gray-600" />
      </div>

      {/* Tab bar */}
      <div className="flex-shrink-0 flex items-center h-9 border-b border-gray-700 bg-[#252526] px-2 gap-1">
        {terminals.map((term, idx) => (
          <div
            key={term.id}
            draggable
            onDragStart={e => handleTabDragStart(e, idx)}
            onDragOver={e => handleTabDragOver(e, idx)}
            onDragEnd={handleTabDragEnd}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-t text-xs font-medium cursor-pointer transition-colors select-none ${
              activeId === term.id ? 'bg-[#1e1e1e] text-gray-200' : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
            }`}
            onClick={() => setActiveId(term.id)}
          >
            <span>{t('terminal.tab', { slot: term.slot })}</span>
            {terminals.length > 1 && (
              <button
                onClick={e => { e.stopPropagation(); closeTerminal(term.slot) }}
                className="ml-0.5 p-0.5 rounded hover:bg-white/10 text-gray-500 hover:text-gray-300"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
        ))}

        <button onClick={addTerminal} className="p-1 rounded hover:bg-white/10 text-gray-500 hover:text-gray-300 transition-colors" title={t('terminal.newTerminal')}>
          <Plus className="w-3.5 h-3.5" />
        </button>

        <div className="flex-1" />

        <button onClick={toggleTerminal} className="p-1 rounded hover:bg-white/10 text-gray-500 hover:text-gray-300 transition-colors" title={t('terminal.closePanel')}>
          <ChevronDown className="w-4 h-4" />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {terminals.map(term => (
          <div key={term.id} className="w-full h-full" style={{ display: activeId === term.id ? 'block' : 'none' }}>
            <TerminalInstance
              slot={term.slot}
              projectId={projectId}
              active={activeId === term.id}
              onExit={(reason) => handleShellExit(term.slot, reason)}
            />
          </div>
        ))}
      </div>
    </div>
  )
}
