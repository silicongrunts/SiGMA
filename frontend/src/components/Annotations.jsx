import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { Send, X, User, Bot, Check, MessageSquarePlus, Trash2, RotateCw, Sparkles, GripVertical, Square, Unlink } from 'lucide-react'
import { filesAPI } from '../api'
import { InlineDiffViewer } from './DiffViewer'
import { SideBySideDiffViewer } from './DiffViewer'
import { useStore } from '../store/useStore'
import { createSSEStreamParser } from '../utils/sse'
import { ThinkingProcess } from './ChatShared'
import { toastError } from './Toast'

const MIN_WIDTH = 320
const MIN_HEIGHT = 200

/** Format ISO timestamp → "2026-01-01 12:34:22" */
function formatTimestamp(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('sv-SE', { hour12: false }).replace('T', ' ')
}

/** Check if a message role represents a user message (case-insensitive). */
function isUserMsg(msg) {
  return msg?.role?.toLowerCase() === 'user'
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

/**
 * AnnotationPopup — draggable + resizable popup for annotation threads.
 *
 * SSE pattern mirrors ChatPanel: streaming state lives in the last thread
 * entry's `.process` array, not in separate state variables.
 * All thread mutations use functional store updates to avoid stale closures.
 */
export function AnnotationPopup({ annotation, projectId, filePath, editorContent, onDelete, onClose, onApplyDiff, onPersist, onConfirmAnchor, onReloadAnnotation, onSaveBeforeAnnotationChat, autoFocusReply, popupStyle, onAnnotationChanged }) {
  const { t } = useTranslation()
  const [reply, setReply] = useState('')
  const siGMADOProcessingAnnotationId = useStore(s => s.siGMADOProcessingAnnotationId)
  const setSiGMADOProcessingAnnotationId = useStore(s => s.setSiGMADOProcessingAnnotationId)
  const [position, setPosition] = useState(() => popupStyle ? { left: popupStyle.left, top: popupStyle.top } : null)
  const [size, setSize] = useState({ width: MIN_WIDTH, height: 380 })
  const [expandedDiff, setExpandedDiff] = useState(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const abortRef = useRef(null)
  const taskIdRef = useRef(null)
  const stopAbortTimerRef = useRef(null)
  const stopRequestedRef = useRef(false)
  const scrollRef = useRef(null)
  const replyInputRef = useRef(null)
  const wrapperRef = useRef(null)
  const isDraggingRef = useRef(false)
  const dragOffsetRef = useRef({ x: 0, y: 0 })
  // Tracks the active drag/resize document listeners so they can be removed on
  // unmount (a mouseup lost over an iframe would otherwise leak them forever).
  const activeDragRef = useRef(null)
  const annoIdRef = useRef(annotation.id)

  // Keep ref in sync
  annoIdRef.current = annotation.id

  // Auto-focus reply input for newly created (pending) annotations
  useEffect(() => {
    if (autoFocusReply && replyInputRef.current) {
      const timer = setTimeout(() => replyInputRef.current?.focus(), 150)
      return () => clearTimeout(timer)
    }
  }, [autoFocusReply])

  // Disconnect the popup's SSE subscription on unmount. The backend task keeps
  // running; data synchronization is owned by explicit open/recovery/terminal
  // paths rather than cleanup, so React dev-mode cleanup cannot duplicate reads.
  useEffect(() => {
    return () => {
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
      if (stopAbortTimerRef.current) {
        clearTimeout(stopAbortTimerRef.current)
        stopAbortTimerRef.current = null
      }
      stopRequestedRef.current = false
      taskIdRef.current = null
      if (activeDragRef.current) {
        document.removeEventListener('mousemove', activeDragRef.current.move)
        document.removeEventListener('mouseup', activeDragRef.current.up)
        activeDragRef.current = null
      }
      setIsStreaming(false)
      setSiGMADOProcessingAnnotationId(null)
    }
  }, [setSiGMADOProcessingAnnotationId])

  // ── Positioning ──
  // Clamp a desired {left,top} into the viewport using the wrapper's CURRENT
  // size. Returns the same reference when nothing changes so setState is a
  // no-op (avoids re-render loops). Shared by the anchor, resize, and
  // diff-expand effects so all three can never let the popup overflow.
  const clampToViewport = useCallback((desired) => {
    if (!desired || !wrapperRef.current) return desired
    const { offsetWidth, offsetHeight } = wrapperRef.current
    const winWidth = window.innerWidth
    const winHeight = window.innerHeight
    const padding = 10
    let { left, top } = desired
    if (left + offsetWidth > winWidth - padding) left = winWidth - offsetWidth - padding
    if (top + offsetHeight > winHeight - padding) top = winHeight - offsetHeight - padding
    if (left < padding) left = padding
    if (top < padding) top = padding
    if (left === desired.left && top === desired.top) return desired
    return { left, top }
  }, [])

  useLayoutEffect(() => {
    if (!popupStyle || isDraggingRef.current) return
    if (!wrapperRef.current) {
      setPosition({ left: popupStyle.left, top: popupStyle.top })
      return
    }

    // Anchor-driven placement: center on the anchor, then keep the whole
    // wrapper (which now includes the expanded diff panel) on screen.
    const anchorX = popupStyle.left
    const anchorY = popupStyle.top
    // offsetWidth is read after render, so it already reflects the diff panel
    // when one is open — no separate effect needed for width changes.
    const { offsetWidth, offsetHeight } = wrapperRef.current
    let left = anchorX - offsetWidth / 2
    let top = anchorY - offsetHeight - 8
    if (top < 10) top = anchorY + 20
    setPosition(clampToViewport({ left, top }))
  }, [popupStyle, clampToViewport])

  // Re-clamp when the wrapper's width changes because the diff panel opens or
  // closes — expanding a 600px panel near the right edge would otherwise push
  // the popup off-screen. Runs after layout so wrapperRef has the new size.
  useLayoutEffect(() => {
    if (position == null) return
    setPosition(prev => clampToViewport(prev))
  }, [expandedDiff, clampToViewport])

  // Re-clamp on viewport resize
  useEffect(() => {
    if (!position) return
    const handleResize = () => setPosition(prev => clampToViewport(prev))
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [position, clampToViewport])

  const isSiGMADOProcessing = siGMADOProcessingAnnotationId === annotation.id

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    // Only auto-scroll if user is near the bottom (within 80px)
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (isNearBottom) {
      el.scrollTop = el.scrollHeight
    }
  }, [annotation.thread])

  const lastMsg = annotation.thread?.length > 0 ? annotation.thread[annotation.thread.length - 1] : null
  const hasMessages = annotation.thread?.length > 0

  // ── Drag (position) ──
  const handleDragStart = (e) => {
    if (e.target.closest('button')) return
    isDraggingRef.current = true
    dragOffsetRef.current = {
      x: e.clientX - (position?.left ?? 0),
      y: e.clientY - (position?.top ?? 0),
    }
    const handleMouseMove = (ev) => {
      setPosition({ left: ev.clientX - dragOffsetRef.current.x, top: ev.clientY - dragOffsetRef.current.y })
    }
    const handleMouseUp = () => {
      isDraggingRef.current = false
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      activeDragRef.current = null
    }
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    activeDragRef.current = { move: handleMouseMove, up: handleMouseUp }
  }

  // ── Resize handle ──
  const handleResizeStart = (e) => {
    e.preventDefault()
    e.stopPropagation()
    const startX = e.clientX
    const startY = e.clientY
    const startSize = { ...size }

    const handleMouseMove = (ev) => {
      setSize({
        width: Math.max(MIN_WIDTH, startSize.width + (ev.clientX - startX)),
        height: Math.max(MIN_HEIGHT, startSize.height + (ev.clientY - startY)),
      })
    }
    const handleMouseUp = () => {
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      activeDragRef.current = null
    }
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    activeDragRef.current = { move: handleMouseMove, up: handleMouseUp }
  }

  // ── Functional store updates (avoid stale closures) ──

  /** Update the last thread entry via store — always reads latest state. */
  const updateLastThreadEntry = useCallback((updater) => {
    const id = annoIdRef.current
    useStore.setState(s => ({
      annotations: s.annotations.map(a => {
        if (a.id !== id) return a
        const thread = [...(a.thread || [])]
        if (thread.length === 0) return a
        thread[thread.length - 1] = updater(thread[thread.length - 1])
        return { ...a, thread }
      }),
    }))
  }, [])

  /** Append entries to the thread via store — always reads latest state. */
  const appendToThread = useCallback((...entries) => {
    const id = annoIdRef.current
    useStore.setState(s => ({
      annotations: s.annotations.map(a => {
        if (a.id !== id) return a
        return { ...a, thread: [...(a.thread || []), ...entries] }
      }),
    }))
  }, [])

  const clearStopAbortTimer = useCallback(() => {
    if (stopAbortTimerRef.current) {
      clearTimeout(stopAbortTimerRef.current)
      stopAbortTimerRef.current = null
    }
  }, [])

  const ensureStreamingEntry = useCallback(() => {
    const id = annoIdRef.current
    useStore.setState(s => ({
      annotations: s.annotations.map(a => {
        if (a.id !== id) return a
        const thread = [...(a.thread || [])]
        const last = thread[thread.length - 1]
        if (last?.isStreaming) return a
        return {
          ...a,
          thread: [
            ...thread,
            { role: 'SiGMA', content: '', process: [], isStreaming: true, created_at: new Date().toISOString() },
          ],
        }
      }),
    }))
  }, [])

  const consumeAnnotationStream = useCallback(async (stream, controller) => {
    const reader = stream.getReader()
    const decoder = new TextDecoder()
    let reachedTerminal = false

    const parser = createSSEStreamParser({
      onEvent: (type, data) => {
        if (type === 'task_id') {
          taskIdRef.current = data.task_id
        } else if (type === 'delta') {
          updateLastThreadEntry(entry => {
            const process = [...(entry.process || [])]
            const streamIdx = process.findLastIndex(s => s.type === 'streaming_text')
            if (streamIdx >= 0) {
              process[streamIdx] = { ...process[streamIdx], content: (process[streamIdx].content || '') + (data.content || '') }
            } else {
              process.push({ type: 'streaming_text', content: data.content || '' })
            }
            return { ...entry, process, isStreaming: true }
          })
        } else if (type === 'thought') {
          updateLastThreadEntry(entry => {
            const process = [...(entry.process || [])]
            if (data.message) {
              process.push({ type: 'hint', content: data.message, transient: true })
            }
            return { ...entry, process, isStreaming: true }
          })
        } else if (type === 'stream_status') {
          updateLastThreadEntry(entry => {
            const process = [...(entry.process || [])]
            const statusMessage = streamStatusText(data, t)
            if (statusMessage) {
              process.push({ type: 'hint', content: statusMessage, transient: true })
            }
            return { ...entry, process, isStreaming: true }
          })
        } else if (type === 'tool_start') {
          updateLastThreadEntry(entry => {
            let process = (entry.process || []).map(s =>
              s.type === 'streaming_text' ? { ...s, type: 'hint' } : s
            )
            process.push({ type: 'tool', tool: data.tool, params: data.params, status: 'running' })
            return { ...entry, process, isStreaming: true }
          })
        } else if (type === 'tool_end') {
          updateLastThreadEntry(entry => {
            const process = (entry.process || []).map(s =>
              s.type === 'tool' && s.status === 'running' && s.tool === data.tool
                ? { ...s, status: 'done', result: data.result_summary }
                : s
            )
            return { ...entry, process, isStreaming: true }
          })
        } else if (type === 'annotation_changed') {
          onAnnotationChanged?.(data.file_name || data.file_path)
        } else if (type === 'done' || type === 'cancelled') {
          reachedTerminal = true
          clearStopAbortTimer()
          stopRequestedRef.current = false
          setIsStreaming(false)
          setSiGMADOProcessingAnnotationId(null)
          updateLastThreadEntry(entry => {
            const process = [...(entry.process || [])]
            const streamIdx = process.findLastIndex(s => s.type === 'streaming_text')
            let content = entry.content || ''
            if (streamIdx >= 0) {
              content = process[streamIdx].content || ''
              process.splice(streamIdx, 1)
            }
            const cleanProcess = process.filter(s => !s.transient)
            return { ...entry, content, process: cleanProcess.length ? cleanProcess : undefined, isStreaming: false }
          })
        } else if (type === 'error') {
          reachedTerminal = true
          clearStopAbortTimer()
          stopRequestedRef.current = false
          setIsStreaming(false)
          setSiGMADOProcessingAnnotationId(null)
          updateLastThreadEntry(entry => {
            const process = [...(entry.process || [])]
            const content = data.content || data.error || data.message || t('chat.toast.unknownError')
            return {
              ...entry,
              content,
              process: process.filter(s => !s.transient && s.type !== 'streaming_text'),
              isStreaming: false,
            }
          })
        }
      },
      onError: (err) => {
        console.error('Annotation SSE stream error:', err)
      },
    })

    await parser.start(reader, decoder, controller.signal)
    if (reachedTerminal && !controller.signal.aborted) {
      await onReloadAnnotation?.(annoIdRef.current)
    }
  }, [clearStopAbortTimer, onAnnotationChanged, onReloadAnnotation, setSiGMADOProcessingAnnotationId, updateLastThreadEntry])

  // ── SiGMADO: SSE streaming ──
  const startSiGMADOStream = useCallback(async () => {
    // Read from store to avoid stale closure guard
    if (useStore.getState().siGMADOProcessingAnnotationId) return
    // Read annotation ID from ref (always up-to-date, even after persist changes ID)
    const currentAnnoId = annoIdRef.current
    setSiGMADOProcessingAnnotationId(currentAnnoId)
    setIsStreaming(true)

    ensureStreamingEntry()

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const stream = await filesAPI.streamAnnotationReply(
        projectId, filePath, currentAnnoId, controller.signal,
      )

      await consumeAnnotationStream(stream, controller)
    } catch (err) {
      if (err.name !== 'AbortError') {
        console.error('SiGMADO stream failed:', err)
      }
    } finally {
      // Only clear state if this is still the active controller.
      // A new handleSiGMADO call may have started between 'done' and here.
      if (abortRef.current === controller) {
        setIsStreaming(false)
        setSiGMADOProcessingAnnotationId(null)
        abortRef.current = null
        taskIdRef.current = null
        stopRequestedRef.current = false
        clearStopAbortTimer()
      }
    }
  }, [filePath, projectId, setSiGMADOProcessingAnnotationId, ensureStreamingEntry, consumeAnnotationStream, clearStopAbortTimer])

  const saveBeforeAnnotationChat = useCallback(async () => {
    if (!onSaveBeforeAnnotationChat) return true
    try {
      return await onSaveBeforeAnnotationChat()
    } catch (e) {
      console.warn('saveBeforeAnnotationChat failed:', e)
      return false
    }
  }, [onSaveBeforeAnnotationChat])

  const handleSiGMADO = useCallback(async () => {
    const saved = await saveBeforeAnnotationChat()
    if (!saved) return
    await startSiGMADOStream()
  }, [saveBeforeAnnotationChat, startSiGMADOStream])

  useEffect(() => {
    if (annotation.isPending || !projectId || !annotation.id) return
    if (useStore.getState().siGMADOProcessingAnnotationId === annotation.id) return

    let cancelled = false
    const controller = new AbortController()

    const reconcile = async () => {
      try {
        const active = await filesAPI.getActiveAnnotationReply(projectId, annotation.id)
        if (cancelled || !active?.active || !active.task_id) return
        if (active.status !== 'queued' && active.status !== 'running' && active.status !== 'cancelling') return
        if (useStore.getState().siGMADOProcessingAnnotationId) return

        setSiGMADOProcessingAnnotationId(annotation.id)
        setIsStreaming(true)
        ensureStreamingEntry()
        abortRef.current = controller
        taskIdRef.current = active.task_id

        const stream = await filesAPI.resumeAnnotationReplyStream(active.task_id, controller.signal)
        if (cancelled) return
        await consumeAnnotationStream(stream, controller)
      } catch (err) {
        if (err.name !== 'AbortError') {
          console.error('Failed to restore annotation stream:', err)
        }
      } finally {
        if (abortRef.current === controller) {
          setIsStreaming(false)
          setSiGMADOProcessingAnnotationId(null)
          abortRef.current = null
          taskIdRef.current = null
          stopRequestedRef.current = false
          clearStopAbortTimer()
        }
      }
    }

    reconcile()

    return () => {
      cancelled = true
      controller.abort()
      if (abortRef.current === controller) {
        abortRef.current = null
        taskIdRef.current = null
        stopRequestedRef.current = false
        clearStopAbortTimer()
        setIsStreaming(false)
        setSiGMADOProcessingAnnotationId(null)
      }
    }
  }, [annotation.id, annotation.isPending, projectId, ensureStreamingEntry, consumeAnnotationStream, onReloadAnnotation, setSiGMADOProcessingAnnotationId, clearStopAbortTimer])

  const handleStop = async () => {
    if (stopRequestedRef.current) return

    const taskId = taskIdRef.current
    const controller = abortRef.current
    if (!taskId || !projectId || !controller) {
      if (controller) controller.abort()
      abortRef.current = null
      taskIdRef.current = null
      stopRequestedRef.current = false
      clearStopAbortTimer()
      updateLastThreadEntry(entry => ({ ...entry, isStreaming: false }))
      setIsStreaming(false)
      setSiGMADOProcessingAnnotationId(null)
      onReloadAnnotation?.(annotation.id).catch(e => console.warn('Failed to reload annotation:', e))
      return
    }

    stopRequestedRef.current = true
    try { await filesAPI.cancelAnnotationReply(projectId, taskId) } catch (e) { console.warn('Failed to cancel annotation reply:', e) }

    clearStopAbortTimer()
    stopAbortTimerRef.current = setTimeout(async () => {
      if (abortRef.current !== controller) return
      controller.abort()
      abortRef.current = null
      taskIdRef.current = null
      stopAbortTimerRef.current = null
      stopRequestedRef.current = false
      updateLastThreadEntry(entry => ({ ...entry, isStreaming: false }))
      setIsStreaming(false)
      setSiGMADOProcessingAnnotationId(null)
      try { await onReloadAnnotation?.(annotation.id) } catch { /* best-effort */ }
    }, 10000)
  }

  const handleSend = async () => {
    if (!reply.trim()) return
    const saved = await saveBeforeAnnotationChat()
    if (!saved) return

    const replyText = reply
    const newMsg = {
      role: 'user',
      content: replyText,
      created_at: new Date().toISOString()
    }

    if (annotation.isPending && annotation.thread.length === 0) {
      let persistedId = null
      try {
        persistedId = await onPersist?.(annotation.id, replyText)
      } catch (e) {
        toastError(e.message || t('common.saveFailed'))
        return
      }
      if (!persistedId) return
      setReply('')
      // Update ref to new ID so handleSiGMADO uses the correct one
      annoIdRef.current = persistedId
      // Auto-trigger AI reply
      startSiGMADOStream()
      return
    }

    if (annotation.status === 'modified' || annotation.status === 'fuzzy') {
      try {
        await onConfirmAnchor?.(annotation.id)
      } catch (e) {
        toastError(e.message || t('common.saveFailed'))
        return
      }
    }

    try {
      // Persist only the user reply — does NOT wipe existing intermediate messages
      await filesAPI.replyAnnotation(projectId, annotation.id, replyText)
    } catch (e) {
      toastError(e.message || t('common.saveFailed'))
      return
    }

    // Append user message via store (functional update — no stale closure)
    appendToThread(newMsg)
    setReply('')

    // Trigger streaming AI reply
    startSiGMADOStream()
  }

  const wrapperStyle = position ? {
    position: 'fixed',
    left: position.left,
    top: position.top,
    zIndex: 9998,
  } : {}

  const isModified = annotation.status === 'modified' || annotation.status === 'fuzzy'
  const isOrphan = annotation.status === 'orphan'

  // SiGMADO: only when last message is from User AND not working
  const shouldShowSiGMADO = hasMessages && isUserMsg(lastMsg) && !isSiGMADOProcessing && !isStreaming

  // Check if the expanded diff's "before" text can still be found in the editor
  const diffApplicable = expandedDiff && editorContent?.includes(expandedDiff.before)

  const handleApplyDiffFromPanel = () => {
    if (expandedDiff && onApplyDiff && diffApplicable) {
      onApplyDiff(annotation.id, expandedDiff)
      setExpandedDiff(null)
    }
  }

  // Thread area height = total height - header(~48) - input(~52) - resize handle(~12)
  const threadMaxH = Math.max(100, size.height - 112)

  // Auto-resize reply textarea, capped at 3 visible lines (~72px at text-sm + py-1.5)
  function autoResizeReply() {
    const el = replyInputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 72) + 'px'
  }
  // Re-measure after content changes (typing/paste/send-clear) and after popup
  // width changes (drag-resize). Runs synchronously after DOM commit, so the
  // textarea's value/width are already up to date when we read scrollHeight.
  useLayoutEffect(() => { autoResizeReply() }, [reply, size.width])

  return (
    <div
      ref={wrapperRef}
      className="flex gap-0 animate-in fade-in zoom-in duration-200 pointer-events-auto"
      style={wrapperStyle}
    >
      {/* Main annotation popup */}
      <div
        className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-[0_10px_40px_rgba(0,0,0,0.15)] rounded-2xl overflow-hidden flex flex-col relative"
        style={{ width: size.width }}
      >
        {/* Header - Draggable */}
        <div
          onMouseDown={handleDragStart}
          className="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between bg-gray-50/50 dark:bg-gray-800/50 cursor-move select-none"
        >
          <div className="flex items-center gap-2 text-xs font-black text-sigma-600 dark:text-sigma-400 uppercase tracking-widest">
            <GripVertical className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />
            <MessageSquarePlus className="w-3.5 h-3.5" /> {t('annotations.title')}
          </div>
          <div className="flex items-center gap-1">
            {shouldShowSiGMADO && (
              <button
                onClick={handleSiGMADO}
                className="p-1.5 bg-sigma-600 text-white hover:bg-sigma-700 rounded-lg transition-colors text-[10px] font-bold flex items-center gap-1"
                title={t('annotations.askSigma')}
              >
                <Sparkles className="w-3.5 h-3.5" />
                {t('annotations.sigmaDo')}
              </button>
            )}
            {(isSiGMADOProcessing || isStreaming) && (
              <button
                onClick={handleStop}
                className="p-1.5 bg-red-500 text-white hover:bg-red-600 rounded-lg transition-colors text-[10px] font-bold flex items-center gap-1"
                title={t('annotations.stopResponse')}
              >
                <Square className="w-3 h-3 fill-current" />
                {t('common.stop')}
              </button>
            )}
            <button onClick={() => onDelete(annotation.id)} className="p-1.5 hover:bg-red-50 dark:hover:bg-red-900/20 text-red-400 hover:text-red-600 rounded-lg transition-colors" title={t('annotations.deleteTitle')}><Trash2 className="w-3.5 h-3.5" /></button>
            <button onClick={onClose} className="p-1 hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 rounded-lg"><X className="w-4 h-4" /></button>
          </div>
        </div>

        {/* Status warning banner */}
        {isModified && (
          <div className="px-4 py-2 bg-orange-50 dark:bg-orange-900/20 border-b border-orange-100 dark:border-orange-800/50 text-[10px] text-orange-600 dark:text-orange-400 flex items-center gap-1.5">
            <MessageSquarePlus className="w-3 h-3" />
            {t('annotations.modifiedWarning')}
          </div>
        )}
        {isOrphan && (
          <div className="px-4 py-2 bg-orange-50 dark:bg-orange-900/20 border-b border-orange-100 dark:border-orange-800/50 text-[10px] text-orange-600 dark:text-orange-400 flex items-center gap-1.5">
            <Unlink className="w-3 h-3" />
            {t('annotations.orphanWarning')}
          </div>
        )}

        {/* Original text preview — only for orphans, since it no longer exists
            in the document and there is no body decoration to read it from. */}
        {isOrphan && annotation.originalText && (
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/50">
            <div className="text-[10px] font-black uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">
              {t('annotations.originalText')}
            </div>
            <div className="text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words max-h-32 overflow-y-auto leading-relaxed">
              {annotation.originalText}
            </div>
          </div>
        )}

        {/* Thread */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-4 bg-white dark:bg-gray-900" style={{ maxHeight: threadMaxH }}>
          {annotation.thread.map((msg, i) => (
            <div key={i} className={`flex flex-col ${msg.role === 'SiGMA' ? 'items-start' : 'items-end'}`}>
              <div className={`flex items-center gap-1.5 mb-1 text-[10px] font-bold uppercase tracking-wider ${msg.role === 'SiGMA' ? 'text-sigma-600 dark:text-sigma-400' : 'text-gray-400 dark:text-gray-500'}`}>
                {msg.role === 'SiGMA' ? <Bot className="w-3 h-3" /> : <User className="w-3 h-3" />}
                {msg.role === 'SiGMA' ? t('chat.roleSigma') : t('annotations.senderYou')}
                {msg.isStreaming && <RotateCw className="w-2.5 h-2.5 animate-spin text-blue-400" />}
                {msg.created_at && !msg.isStreaming && (
                  <span className="text-[9px] text-gray-300 dark:text-gray-600 select-none font-normal normal-case tracking-normal">{formatTimestamp(msg.created_at)}</span>
                )}
              </div>
              <div className={`max-w-[85%] rounded-2xl text-sm leading-relaxed shadow-sm overflow-hidden break-words ${
                msg.role === 'SiGMA' ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-900 dark:text-blue-200 rounded-tl-none border border-blue-100 dark:border-blue-800/50 px-3 py-2' : 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 rounded-tr-none border border-gray-200 dark:border-gray-700 px-3 py-2'
              }`}>
                {msg.process?.length > 0 && (
                  <ThinkingProcess steps={msg.process} isStreaming={msg.isStreaming} />
                )}
                {msg.role === 'SiGMA' ? (
                  msg.content ? (
                    <InlineDiffViewer
                      annotation={annotation}
                      message={msg}
                      onApplyDiff={onApplyDiff}
                      onDeleteAnnotation={onDelete}
                      onExpandDiff={setExpandedDiff}
                      expandedDiff={expandedDiff}
                      editorContent={editorContent}
                      projectId={projectId}
                    />
                  ) : !msg.process?.length && msg.isStreaming ? (
                    <div className="flex items-center gap-2">
                      <span className="inline-block w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce" style={{ animationDelay: '0ms' }}></span>
                      <span className="inline-block w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce" style={{ animationDelay: '150ms' }}></span>
                      <span className="inline-block w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce" style={{ animationDelay: '300ms' }}></span>
                      <span className="text-xs text-blue-600">{t('chat.thinking')}</span>
                    </div>
                  ) : null
                ) : (
                  <div className="whitespace-pre-wrap">{msg.content}</div>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Input */}
        <div className="p-3 border-t border-gray-100 dark:border-gray-800 bg-gray-50/30 dark:bg-gray-800/30">
          <div className="flex items-end gap-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-1.5 focus-within:ring-2 focus-within:ring-sigma-600/20 transition-all shadow-sm">
            <textarea
              ref={replyInputRef}
              value={reply} onChange={e => setReply(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
              }}
              placeholder={t('annotations.replyPlaceholder')}
              rows={1}
              className="flex-1 bg-transparent text-sm outline-none py-1 resize-none max-h-[72px] overflow-y-auto text-gray-800 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-500"
              disabled={isSiGMADOProcessing || isStreaming}
            />
            {isStreaming ? (
              <button
                onClick={handleStop}
                className="p-1.5 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors shadow-sm"
                title={t('annotations.stopTitle')}
              >
                <Square className="w-3.5 h-3.5 fill-current" />
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={isSiGMADOProcessing || !reply.trim()}
                className="p-1.5 bg-sigma-600 text-white rounded-lg hover:bg-sigma-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors shadow-sm"
              >
                <Send className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* Resize handle */}
        <div
          onMouseDown={handleResizeStart}
          className="absolute bottom-0 right-0 w-3 h-3 cursor-se-resize opacity-40 hover:opacity-70 transition-opacity"
          style={{ touchAction: 'none' }}
        >
          <svg viewBox="0 0 6 6" className="w-full h-full text-gray-400">
            <line x1="5" y1="1" x2="1" y2="5" stroke="currentColor" strokeWidth="1" />
            <line x1="5" y1="3" x2="3" y2="5" stroke="currentColor" strokeWidth="1" />
          </svg>
        </div>
      </div>

      {/* Right side diff panel */}
      {expandedDiff && (
        <div className="w-[600px] bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-700 shadow-lg rounded-r-2xl overflow-hidden flex flex-col max-h-[400px]">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between bg-gray-50 dark:bg-gray-800 flex-shrink-0">
            <span className="text-xs font-bold text-gray-700 dark:text-gray-300">{t('annotations.suggestedChanges')}</span>
            <button onClick={() => setExpandedDiff(null)} className="p-1 hover:bg-gray-200 dark:hover:bg-gray-700 rounded transition-colors" title={t('annotations.hideChanges')}>
              <X className="w-4 h-4 text-gray-600 dark:text-gray-400" />
            </button>
          </div>
          <div className="flex-1 p-4 overflow-y-auto min-h-0">
            <SideBySideDiffViewer
              before={expandedDiff.before}
              after={expandedDiff.after}
              onAccept={handleApplyDiffFromPanel}
              onReject={() => setExpandedDiff(null)}
            />
          </div>
          <div className="p-3 border-t border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800 flex gap-2 flex-shrink-0">
            {diffApplicable ? (
              <button
                onClick={handleApplyDiffFromPanel}
                className="flex-1 flex items-center justify-center gap-1 py-2 bg-green-600 text-white text-xs font-bold rounded-lg hover:bg-green-700 transition-colors"
              >
                <Check className="w-3 h-3" /> {t('common.apply')}
              </button>
            ) : (
              <div className="flex-1 flex items-center justify-center gap-1 py-2 bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400 text-xs font-bold rounded-lg cursor-not-allowed select-none">
                {t('annotations.originalNotFound')}
              </div>
            )}
            <button
              onClick={() => setExpandedDiff(null)}
              className="flex-1 py-2 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 text-xs font-bold rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
            >
              {t('common.close')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
