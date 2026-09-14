import { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Send, X, User, Bot, Check, MessageSquarePlus, Trash2, RotateCw, RotateCcw, GripVertical, Square, Unlink } from 'lucide-react'
import { filesAPI } from '../api'
import { InlineDiffViewer } from './DiffViewer'
import { SideBySideDiffViewer } from './DiffViewer'
import { computeDiffState } from '../utils/diffState'
import { formatTimestamp } from '../utils/formatTimestamp'
import { useStore } from '../store/useStore'
import { createSSEStreamParser } from '../utils/sse'
import { STREAM_RECOVERY_MAX_ATTEMPTS, STREAM_RECOVERY_DELAY_MS, ThinkingProcess, streamStatusText, withTransientHint } from './ChatShared'
import { toastError } from './Toast'

const MIN_WIDTH = 320
const MIN_HEIGHT = 200
// Vertical space reserved by the card chrome (header + input + resize grip),
// so the scrollable thread area = total card height minus this. 100 is the
// absolute floor below which the thread becomes unusable.
const CARD_CHROME_HEIGHT = 112
const MIN_THREAD_HEIGHT = 100

/** Max scrollable thread height for a given total card height. Shared by the
 *  React-derived maxHeight and the DOM-direct resize path so they stay in sync. */
const threadMaxHeightFor = (cardHeight) =>
  Math.max(MIN_THREAD_HEIGHT, cardHeight - CARD_CHROME_HEIGHT)

/**
 * AnnotationPopup — draggable + resizable popup for annotation threads.
 *
 * SSE pattern mirrors ChatPanel: streaming state lives in the last thread
 * entry's `.process` array, not in separate state variables.
 * All thread mutations use functional store updates to avoid stale closures.
 */
export function AnnotationPopup({ annotation, projectId, filePath, editorContent, onDelete, onClose, onApplyDiff, onLocateDiff, onClearHighlight, onPersist, onConfirmAnchor, onReloadAnnotation, onSaveBeforeAnnotationChat, autoFocusReply, popupStyle, isPopupDimmed, onAnnotationChanged }) {
  const { t } = useTranslation()
  const [reply, setReply] = useState('')
  const siGMADOProcessingAnnotationId = useStore(s => s.siGMADOProcessingAnnotationId)
  const setSiGMADOProcessingAnnotationId = useStore(s => s.setSiGMADOProcessingAnnotationId)
  const [position, setPosition] = useState(() => popupStyle ? { left: popupStyle.left, top: popupStyle.top } : null)
  const [size, setSize] = useState({ width: MIN_WIDTH, height: 380 })
  const [expandedDiff, setExpandedDiff] = useState(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)
  const abortRef = useRef(null)
  const taskIdRef = useRef(null)
  // Highest applied SSE event id for the current annotation stream; passed
  // back as the resume cursor so a reconnect replays only what was missed.
  const lastSeqRef = useRef(0)
  const stopAbortTimerRef = useRef(null)
  const stopRequestedRef = useRef(false)
  const scrollRef = useRef(null)
  // True once the thread has been auto-scrolled to the bottom on first mount.
  const hasAutoScrolledRef = useRef(false)
  const replyInputRef = useRef(null)

  const handleDelete = useCallback(async () => {
    if (isDeleting) return
    setIsDeleting(true)
    // Detach this popup's stream before asking the backend to drain the task;
    // buffered late events must not update an entity that is being deleted.
    abortRef.current?.abort()
    abortRef.current = null
    taskIdRef.current = null
    try {
      await onDelete(annotation.id)
    } catch (error) {
      setIsDeleting(false)
      throw error
    }
  }, [annotation.id, isDeleting, onDelete])
  const wrapperRef = useRef(null)
  const isDraggingRef = useRef(false)
  // True only while the resize grip is held. Gates the layout effect that
  // re-asserts the live card/row/thread sizes after each re-render (React
  // re-applies stale inline styles on re-render, which would otherwise snap
  // them back mid-gesture). Separate from isDraggingRef (drag moves the
  // wrapper, never resizes the card).
  const isResizingRef = useRef(false)
  const dragOffsetRef = useRef({ x: 0, y: 0 })
  // Tracks the active drag/resize document listeners so they can be removed on
  // unmount (a mouseup lost over an iframe would otherwise leak them forever).
  const activeDragRef = useRef(null)
  // The annotation card element; resize writes its width/height directly during
  // the gesture for responsiveness, then commits to `size` state on release.
  const cardRef = useRef(null)
  // The flex row holding the card and the side diff panel. It carries the
  // card's height so the panel's self-stretch resolves against a definite
  // cross size — with an auto-height row a long diff would inflate the row
  // itself and push the panel's apply buttons below the viewport. Written
  // directly during resize like the card size, committed to `size` on release.
  const rowRef = useRef(null)
  // Live card size while a resize gesture is held, so the re-assert effect
  // can restore the DOM values a re-render overwrites mid-gesture (e.g. an
  // SSE thread update).
  const liveSizeRef = useRef(null)
  // The annotation id the live stream belongs to, captured when the stream
  // starts. The stream's store updates target this captured id — never the
  // currently selected annotation — so switching the popup to another
  // annotation mid-stream can never write one thread's content into another.
  const streamAnnoIdRef = useRef(null)

  // Auto-focus reply input for newly created (pending) annotations
  useEffect(() => {
    if (autoFocusReply && replyInputRef.current) {
      const timer = setTimeout(() => replyInputRef.current?.focus(), 150)
      return () => clearTimeout(timer)
    }
  }, [autoFocusReply])

  // The popup instance survives switching to another annotation, but the diff
  // panel, reply draft, and first-open auto-scroll belong to one thread —
  // reset them whenever the annotation id changes. The pending→persisted id
  // swap passes through here before any of that state can hold content, so
  // it resets nothing.
  useEffect(() => {
    setExpandedDiff(null)
    setReply('')
    hasAutoScrolledRef.current = false
    onClearHighlight?.()
  }, [annotation.id, onClearHighlight])

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
      streamAnnoIdRef.current = null
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
    // On the popup's first mount we always jump to the bottom so the latest
    // message is visible. After that we only auto-scroll when the user is
    // already near the bottom, so growing the thread never yanks someone who
    // has scrolled up to read earlier messages.
    if (!hasAutoScrolledRef.current) {
      hasAutoScrolledRef.current = true
      el.scrollTop = el.scrollHeight
      return
    }
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (isNearBottom) {
      el.scrollTop = el.scrollHeight
    }
  }, [annotation.thread])

  // ── Drag (position) ──
  // For responsiveness we move the popup by writing left/top straight to the
  // DOM during the drag — no React re-render per move, which the heavy thread
  // + diff subtree could not keep up with. The wrapper's size is read ONCE at
  // drag start (not every move) so each mousemove avoids a forced layout
  // reflow, which is what made earlier versions feel laggy. The final position
  // is committed to `position` state on mouseup so React and the DOM agree.
  const handleDragStart = (e) => {
    if (e.target.closest('button')) return
    isDraggingRef.current = true
    const wrapper = wrapperRef.current
    // Cache the wrapper's current size; clamping during the drag is read-only
    // arithmetic, never touching offsetWidth/offsetHeight again.
    const w = wrapper?.offsetWidth || 0
    const h = wrapper?.offsetHeight || 0
    const winW = window.innerWidth
    const winH = window.innerHeight
    const padding = 10
    const clamp = (desired) => {
      let { left, top } = desired
      if (w && left + w > winW - padding) left = winW - w - padding
      if (h && top + h > winH - padding) top = winH - h - padding
      if (left < padding) left = padding
      if (top < padding) top = padding
      return { left, top }
    }
    dragOffsetRef.current = {
      x: e.clientX - (position?.left ?? 0),
      y: e.clientY - (position?.top ?? 0),
    }
    const apply = (desired) => {
      const next = clamp(desired)
      if (wrapper) {
        wrapper.style.left = `${next.left}px`
        wrapper.style.top = `${next.top}px`
      }
      return next
    }
    const handleMouseMove = (ev) => {
      apply({
        left: ev.clientX - dragOffsetRef.current.x,
        top: ev.clientY - dragOffsetRef.current.y,
      })
    }
    const handleMouseUp = (ev) => {
      isDraggingRef.current = false
      const finalPos = apply({
        left: ev.clientX - dragOffsetRef.current.x,
        top: ev.clientY - dragOffsetRef.current.y,
      })
      setPosition(finalPos)
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      activeDragRef.current = null
    }
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    activeDragRef.current = { move: handleMouseMove, up: handleMouseUp }
  }

  // ── Resize handle ──
  // Resizes the card live by writing width/height straight to the DOM. The
  // row's height (the cross size the diff panel stretches to) and the thread
  // area's maxHeight are React-derived from size.height too, so both are
  // updated in lockstep via the DOM — otherwise the card grows but they keep
  // their old caps until release. The final size is committed to `size` state
  // on mouseup; the layout effect below re-asserts the live values if a
  // re-render lands mid-gesture.
  const applyLiveSize = (next) => {
    liveSizeRef.current = next
    const card = cardRef.current
    if (card) {
      card.style.width = `${next.width}px`
      card.style.height = `${next.height}px`
    }
    if (rowRef.current) {
      rowRef.current.style.height = `${next.height}px`
    }
    if (scrollRef.current) {
      scrollRef.current.style.maxHeight = `${threadMaxHeightFor(next.height)}px`
    }
  }

  const handleResizeStart = (e) => {
    e.preventDefault()
    e.stopPropagation()
    isResizingRef.current = true
    const startX = e.clientX
    const startY = e.clientY
    const startSize = { ...size }

    const handleMouseMove = (ev) => {
      applyLiveSize({
        width: Math.max(MIN_WIDTH, startSize.width + (ev.clientX - startX)),
        height: Math.max(MIN_HEIGHT, startSize.height + (ev.clientY - startY)),
      })
    }
    const handleMouseUp = (ev) => {
      const finalSize = {
        width: Math.max(MIN_WIDTH, startSize.width + (ev.clientX - startX)),
        height: Math.max(MIN_HEIGHT, startSize.height + (ev.clientY - startY)),
      }
      setSize(finalSize)
      isResizingRef.current = false
      liveSizeRef.current = null
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      activeDragRef.current = null
    }
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    activeDragRef.current = { move: handleMouseMove, up: handleMouseUp }
  }

  // While a resize gesture is held, re-assert the live sizes after every
  // render: a re-render (e.g. an SSE thread update) re-applies the stale
  // inline `size` styles, which would snap the card, row, and thread back
  // mid-gesture. Runs on each render (no dep array) but writes only while
  // the gesture is live.
  useLayoutEffect(() => {
    if (!isResizingRef.current) return
    if (liveSizeRef.current) applyLiveSize(liveSizeRef.current)
  })

  // ── Functional store updates (avoid stale closures) ──

  /** Update the last thread entry of annotation `id` via store — always reads
   *  latest state. The id is captured by the caller at stream start. */
  const updateLastThreadEntry = useCallback((id, updater) => {
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

  /** Append entries to the thread of annotation `id` via store — always reads
   *  latest state. The id is captured by the caller at send time. */
  const appendToThread = useCallback((id, ...entries) => {
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

  const ensureStreamingEntry = useCallback((id) => {
    useStore.setState(s => ({
      annotations: s.annotations.map(a => {
        if (a.id !== id) return a
        const thread = [...(a.thread || [])]
        const last = thread[thread.length - 1]
        // A trailing streaming entry here is a stale partial from an aborted
        // earlier stream (the popup detached or remounted mid-reply) —
        // transient UI state, never persisted. Drop it so the fresh stream
        // rebuilds the reply from the full replay instead of appending after
        // the leftover text.
        if (last?.isStreaming) thread.pop()
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

  /** Remove the trailing streaming entry of annotation `id` — the in-flight
   *  partial reply this component created for a live stream. Only entries
   *  marked isStreaming are touched, so persisted history is never removed. */
  const removeTrailingStreamingEntry = useCallback((id) => {
    useStore.setState(s => ({
      annotations: s.annotations.map(a => {
        if (a.id !== id) return a
        const thread = [...(a.thread || [])]
        const last = thread[thread.length - 1]
        if (!last?.isStreaming) return a
        thread.pop()
        return { ...a, thread }
      }),
    }))
  }, [])

  const consumeAnnotationStream = useCallback(async (annoId, stream, controller) => {
    let reachedTerminal = false
    // True between a gap frame and the thread reload it triggers: the
    // reconnecting cursor predates the oldest buffered event, so mid-events
    // were evicted and the live view has a hole replay cannot fill.
    // replayUntil is the gap frame's highest replayed seq: replayed frames up
    // to it are already covered by the reloaded thread, so the seq gate —
    // not reload completion — decides when appending resumes. A missing value
    // falls back to dropping until the reload completes.
    let reloadingAfterGap = false
    let replayUntil = null

    const handleEvent = (type, data, id) => {
      if (Number.isInteger(id)) {
        // While a gap reload is in flight — and for every replayed frame up
        // to the gap's replay_until, even after the reload lands — the
        // reloaded thread already covers the event: drop it but keep the
        // cursor advancing so frames arriving after the replay batch pass
        // the gate. Frames beyond replay_until are genuinely live and append
        // even mid-reload. The small hole at the seam is corrected by the
        // terminal reload below.
        if (reloadingAfterGap || (replayUntil !== null && id <= replayUntil)) {
          lastSeqRef.current = id
          return
        }
        // Overlapping replay: skip anything already applied.
        if (id <= lastSeqRef.current) return
        lastSeqRef.current = id
      } else {
        // Control frames are never entered into the server's replay buffer,
        // so the seq gate above cannot apply to them. Known control types
        // fall through to the dispatch below; anything else is dropped
        // loudly instead of guessed at.
        if (type === 'error' && data?.reason === 'idle_timeout') {
          // Stream-health signal, not a failure: the server merely stopped
          // seeing traffic. Neither render the error nor end the turn — the
          // reconnect loop below reattaches with the cursor.
          return
        }
        if (type === 'gap') {
          // Rebuild the thread from the persisted annotation (the checkpoint
          // covers the evicted range), then resume appending once the stale
          // replay batch has passed. replay_until bounds that batch;
          // recompute per gap so a later reconnect's gap always governs its
          // own batch.
          replayUntil = Number.isInteger(data?.replay_until) ? data.replay_until : null
          if (!reloadingAfterGap) {
            reloadingAfterGap = true
            Promise.resolve(onReloadAnnotation?.(annoId))
              .catch(e => console.warn('Failed to reload annotation:', e))
              .finally(() => { reloadingAfterGap = false })
          }
          return
        }
        if (type !== 'task_id' && type !== 'done' && type !== 'error' && type !== 'cancelled') {
          console.warn('[Annotations] Dropping SSE frame without a seq id:', type)
          return
        }
      }
      if (type === 'task_id') {
        taskIdRef.current = data.task_id
      } else if (type === 'delta') {
        updateLastThreadEntry(annoId, entry => {
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
        // Reasoning arrives as per-token deltas: aggregate into the single
        // trailing transient hint instead of appending one entry per event.
        updateLastThreadEntry(annoId, entry => ({
          ...entry,
          process: withTransientHint(entry.process || [], data.content || ''),
          isStreaming: true,
        }))
      } else if (type === 'stream_status') {
        const statusMessage = streamStatusText(data, t)
        if (statusMessage) {
          updateLastThreadEntry(annoId, entry => ({
            ...entry,
            process: withTransientHint(entry.process || [], statusMessage),
            isStreaming: true,
          }))
        }
      } else if (type === 'tool_start') {
        updateLastThreadEntry(annoId, entry => {
          let process = (entry.process || []).map(s =>
            s.type === 'streaming_text' ? { ...s, type: 'hint' } : s
          )
          process.push({ type: 'tool', tool: data.tool, params: data.params, status: 'running' })
          return { ...entry, process, isStreaming: true }
        })
      } else if (type === 'tool_end') {
        updateLastThreadEntry(annoId, entry => {
          const process = (entry.process || []).map(s =>
            s.type === 'tool' && s.status === 'running' && s.tool === data.tool
              ? { ...s, status: 'done', result: data.result_summary }
              : s
          )
          return { ...entry, process, isStreaming: true }
        })
      } else if (type === 'annotation_changed') {
        onAnnotationChanged?.(data.file_path)
      } else if (type === 'done' || type === 'cancelled') {
        reachedTerminal = true
        clearStopAbortTimer()
        stopRequestedRef.current = false
        setIsStreaming(false)
        setSiGMADOProcessingAnnotationId(null)
        updateLastThreadEntry(annoId, entry => {
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
        updateLastThreadEntry(annoId, entry => {
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
    }

    let current = stream
    let reconnects = 0
    while (true) {
      reachedTerminal = false
      const appliedBefore = lastSeqRef.current
      const parser = createSSEStreamParser({
        onEvent: handleEvent,
        onError: (err) => console.error('Annotation SSE stream error:', err),
      })
      await parser.start(current.getReader(), new TextDecoder(), controller.signal)
      if (controller.signal.aborted) return
      if (reachedTerminal) break
      // The stream ended with no terminal event (connection drop, or the
      // server closed it after its send buffer overflowed). Reconnect with
      // the cursor while the task is still running; give up after a few
      // consecutive unproductive tries. A connection that replayed at least
      // one fresh (not already applied) event made real progress, so it does
      // not count toward the bound.
      if (lastSeqRef.current > appliedBefore) reconnects = 0
      reconnects += 1
      if (reconnects > STREAM_RECOVERY_MAX_ATTEMPTS || !taskIdRef.current) break
      // Pace reconnects like the chat consumer instead of retrying in a
      // tight loop against a backend that may still be down.
      await new Promise(resolve => setTimeout(resolve, STREAM_RECOVERY_DELAY_MS))
      // Cursor-based resume replays only the events after the last applied seq.
      try {
        current = await filesAPI.resumeAnnotationReplyStream(projectId, taskIdRef.current, controller.signal, lastSeqRef.current)
      } catch (err) {
        if (err.name === 'AbortError') return
        console.error('Failed to resume annotation stream:', err)
        break
      }
    }

    if (!reachedTerminal) {
      // No terminal event even after the reconnect attempt: stop the spinner
      // and surface the loss instead of leaving a half-rendered turn spinning.
      if (abortRef.current === controller) {
        setIsStreaming(false)
        setSiGMADOProcessingAnnotationId(null)
        abortRef.current = null
        taskIdRef.current = null
        streamAnnoIdRef.current = null
        stopRequestedRef.current = false
        clearStopAbortTimer()
      }
      updateLastThreadEntry(annoId, entry => ({ ...entry, isStreaming: false }))
      toastError(t('chat.toast.resumeFailed'))
      return
    }
    await onReloadAnnotation?.(annoId)
  }, [clearStopAbortTimer, onAnnotationChanged, onReloadAnnotation, projectId, setSiGMADOProcessingAnnotationId, t, updateLastThreadEntry])

  // ── SiGMADO: SSE streaming ──
  const startSiGMADOStream = useCallback(async (annoId) => {
    // Read from store to avoid stale closure guard
    if (useStore.getState().siGMADOProcessingAnnotationId) {
      // handleSend already persisted and rendered the user reply — say why
      // no AI response starts instead of letting it read as a failed send.
      toastError(t('annotations.replyBusy'))
      return
    }
    lastSeqRef.current = 0
    streamAnnoIdRef.current = annoId
    setSiGMADOProcessingAnnotationId(annoId)
    setIsStreaming(true)

    ensureStreamingEntry(annoId)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const stream = await filesAPI.streamAnnotationReply(
        projectId, filePath, annoId, controller.signal,
      )

      await consumeAnnotationStream(annoId, stream, controller)
    } catch (err) {
      if (err.name !== 'AbortError') {
        console.error('SiGMADO stream failed:', err)
        // The stream never started (e.g. 409): drop the entry
        // ensureStreamingEntry created so no empty bubble stays behind.
        removeTrailingStreamingEntry(annoId)
        toastError(t('annotations.replyStartFailed', { message: err.message || t('chat.toast.unknownError') }))
      }
    } finally {
      // Only clear state if this is still the active controller.
      // A new stream may have started between 'done' and here.
      if (abortRef.current === controller) {
        setIsStreaming(false)
        setSiGMADOProcessingAnnotationId(null)
        abortRef.current = null
        taskIdRef.current = null
        streamAnnoIdRef.current = null
        stopRequestedRef.current = false
        clearStopAbortTimer()
      }
    }
  }, [filePath, projectId, t, setSiGMADOProcessingAnnotationId, ensureStreamingEntry, consumeAnnotationStream, clearStopAbortTimer, removeTrailingStreamingEntry])

  const saveBeforeAnnotationChat = useCallback(async () => {
    if (!onSaveBeforeAnnotationChat) return true
    try {
      return await onSaveBeforeAnnotationChat()
    } catch (e) {
      console.warn('saveBeforeAnnotationChat failed:', e)
      return false
    }
  }, [onSaveBeforeAnnotationChat])

  // Detach a stream that belongs to a different annotation: switching the
  // popup to another thread aborts the in-flight fetch and clears the global
  // processing state, so the new thread can stream on its own. The server
  // task keeps running; the reconcile effect below re-attaches to it when
  // this annotation is opened again. The in-flight partial reply is transient
  // UI state — remove it so the reattach's full replay rebuilds the entry
  // fresh instead of appending after the stale text. The pending→persisted id
  // swap keeps the same logical thread (the stream already targets the
  // persisted id) and is not treated as a switch.
  useEffect(() => {
    if (!abortRef.current || streamAnnoIdRef.current === annotation.id) return
    const streamAnnoId = streamAnnoIdRef.current
    abortRef.current.abort()
    abortRef.current = null
    taskIdRef.current = null
    stopRequestedRef.current = false
    clearStopAbortTimer()
    removeTrailingStreamingEntry(streamAnnoId)
    streamAnnoIdRef.current = null
    setIsStreaming(false)
    setSiGMADOProcessingAnnotationId(null)
  }, [annotation.id, clearStopAbortTimer, removeTrailingStreamingEntry, setSiGMADOProcessingAnnotationId])

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
        streamAnnoIdRef.current = annotation.id
        ensureStreamingEntry(annotation.id)
        abortRef.current = controller
        taskIdRef.current = active.task_id
        lastSeqRef.current = 0

        const stream = await filesAPI.resumeAnnotationReplyStream(projectId, active.task_id, controller.signal, lastSeqRef.current)
        if (cancelled) return
        await consumeAnnotationStream(annotation.id, stream, controller)
      } catch (err) {
        if (err.name !== 'AbortError') {
          console.error('Failed to restore annotation stream:', err)
          // The resume never produced a stream: release the entry
          // ensureStreamingEntry created so the spinner does not hang.
          updateLastThreadEntry(annotation.id, entry => ({ ...entry, isStreaming: false }))
        }
      } finally {
        if (abortRef.current === controller) {
          setIsStreaming(false)
          setSiGMADOProcessingAnnotationId(null)
          abortRef.current = null
          taskIdRef.current = null
          streamAnnoIdRef.current = null
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
        streamAnnoIdRef.current = null
        stopRequestedRef.current = false
        clearStopAbortTimer()
        setIsStreaming(false)
        setSiGMADOProcessingAnnotationId(null)
      }
    }
  }, [annotation.id, annotation.isPending, projectId, ensureStreamingEntry, consumeAnnotationStream, onReloadAnnotation, setSiGMADOProcessingAnnotationId, clearStopAbortTimer, updateLastThreadEntry])

  const handleStop = async () => {
    if (stopRequestedRef.current) return

    const taskId = taskIdRef.current
    const controller = abortRef.current
    // The stream being stopped belongs to the annotation it started on.
    const streamAnnoId = streamAnnoIdRef.current || annotation.id
    if (!taskId || !projectId || !controller) {
      if (controller) controller.abort()
      abortRef.current = null
      taskIdRef.current = null
      stopRequestedRef.current = false
      clearStopAbortTimer()
      updateLastThreadEntry(streamAnnoId, entry => ({ ...entry, isStreaming: false }))
      setIsStreaming(false)
      setSiGMADOProcessingAnnotationId(null)
      onReloadAnnotation?.(streamAnnoId)?.catch(e => console.warn('Failed to reload annotation:', e))
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
      updateLastThreadEntry(streamAnnoId, entry => ({ ...entry, isStreaming: false }))
      setIsStreaming(false)
      setSiGMADOProcessingAnnotationId(null)
      try { await onReloadAnnotation?.(streamAnnoId) } catch { /* best-effort */ }
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
      // Auto-trigger AI reply, bound to the persisted annotation id
      startSiGMADOStream(persistedId)
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
      onAnnotationChanged?.(filePath)
    } catch (e) {
      toastError(e.message || t('common.saveFailed'))
      return
    }

    // Append user message via store (functional update, targeted at the
    // annotation the reply was sent to — captured, not read from the ref,
    // so a mid-flight annotation switch cannot misplace it)
    appendToThread(annotation.id, newMsg)
    setReply('')

    // Trigger streaming AI reply for this annotation
    startSiGMADOStream(annotation.id)
  }

  // Positioning + z-index for the outer wrapper. Opacity is NOT applied here:
  // this element carries the `zoom-in` entrance animation whose keyframe ends at
  // `opacity: 1`, and `animation-fill-mode: forwards` would otherwise pin that
  // end-state and override any inline opacity. The dim opacity lives on an
  // inner, animation-free wrapper instead.
  const wrapperStyle = position ? {
    position: 'fixed',
    left: position.left,
    top: position.top,
    zIndex: 9998,
  } : {}

  // Dim (70%) applied here, on a layer with no entrance animation, so the
  // opacity transition works in both directions. The popup stays interactive.
  const dimStyle = {
    opacity: isPopupDimmed ? 0.7 : 1,
    transition: 'opacity 120ms ease',
  }

  const isModified = annotation.status === 'modified' || annotation.status === 'fuzzy'
  const isOrphan = annotation.status === 'orphan'

  // Resolve the expanded diff's state against the live document. Shared with
  // the inline diff buttons (computeDiffState) so both always agree:
  //   • canApply  — before uniquely present, no applied copy yet
  //   • canRevert — applied copy uniquely present (before may still occur
  //                 nested inside it, e.g. insertion-style diffs)
  //   • blocked   — notFound / multipleOriginal / multipleApplied (ambiguous
  //                 or missing text: no safe action)
  const diffState = useMemo(() => {
    if (!expandedDiff) return { canApply: false, canRevert: false, blockedReason: null }
    return computeDiffState(editorContent, expandedDiff.before, expandedDiff.after)
  }, [expandedDiff, editorContent])

  const handleApplyDiffFromPanel = () => {
    if (expandedDiff && onApplyDiff && diffState.canApply) {
      onApplyDiff(annotation.id, expandedDiff)
      setExpandedDiff(null)
    }
  }

  // Revert an already-applied diff: swap before/after so the applied "after"
  // text in the document is replaced back by "before". Reuses onApplyDiff —
  // no separate replace path needed.
  const handleRevertDiffFromPanel = () => {
    if (expandedDiff && onApplyDiff && diffState.canRevert) {
      onApplyDiff(annotation.id, { before: expandedDiff.after, after: expandedDiff.before })
      setExpandedDiff(null)
    }
  }

  // Close the side diff panel and drop any active locate highlight. Used by
  // the panel's X, Close, and reject buttons so the flash never lingers after
  // the panel that owns it is gone.
  const closeDiffPanel = () => {
    setExpandedDiff(null)
    onClearHighlight?.()
  }

  const threadMaxH = threadMaxHeightFor(size.height)

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
      className="animate-in fade-in zoom-in duration-200 pointer-events-auto"
      style={wrapperStyle}
    >
      {/* Inner layer carries the dim opacity (kept off the animated wrapper so
          the entrance keyframe's `forwards` end-state can't pin it) and lays out
          the annotation card next to its optional diff panel. Its height is the
          card's height: the definite cross size is what keeps the diff panel's
          self-stretch capped at the card instead of growing with a long diff. */}
      <div
        ref={rowRef}
        className="flex gap-0"
        style={{ ...dimStyle, height: size.height }}
      >
      {/* Main annotation popup. Height is driven by `size.height` so the side
          diff panel (a flex sibling) stretches to match it. During a live
          resize the layout effect above re-asserts the DOM values after every
          re-render — React would otherwise re-apply the stale `size`. */}
      <div
        ref={cardRef}
        className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-[0_10px_40px_rgba(0,0,0,0.15)] rounded-2xl overflow-hidden flex flex-col relative"
        style={{ width: size.width, height: size.height }}
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
            <button onClick={handleDelete} disabled={isDeleting} className="p-1.5 hover:bg-red-50 dark:hover:bg-red-900/20 text-red-400 hover:text-red-600 rounded-lg transition-colors disabled:opacity-50" title={t('annotations.deleteTitle')}>
              {isDeleting ? <span className="text-[10px]">{t('common.waiting', 'Waiting…')}</span> : <Trash2 className="w-3.5 h-3.5" />}
            </button>
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

        {/* Thread. maxHeight follows the card's height; during a live resize
            the layout effect above re-asserts it in lockstep, same as the
            card/row sizes. */}
        <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4 bg-white dark:bg-gray-900" style={{ maxHeight: threadMaxH }}>
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
                      messageIndex={i}
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

      {/* Right side diff panel. self-stretch makes it match the annotation
          card's height (driven by size.height) instead of a fixed cap. */}
      {expandedDiff && (
        <div className="w-[600px] self-stretch bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-700 shadow-lg rounded-r-2xl overflow-hidden flex flex-col min-h-0">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between bg-gray-50 dark:bg-gray-800 flex-shrink-0">
            <span className="text-xs font-bold text-gray-700 dark:text-gray-300">{t('annotations.suggestedChanges')}</span>
            <button onClick={closeDiffPanel} className="p-1 hover:bg-gray-200 dark:hover:bg-gray-700 rounded transition-colors" title={t('annotations.hideChanges')}>
              <X className="w-4 h-4 text-gray-600 dark:text-gray-400" />
            </button>
          </div>
          <div className="flex-1 p-4 overflow-y-auto min-h-0">
            <SideBySideDiffViewer
              before={expandedDiff.before}
              after={expandedDiff.after}
              canApply={diffState.canApply}
              canRevert={diffState.canRevert}
              onLocate={onLocateDiff}
            />
          </div>
          <div className="p-3 border-t border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800 flex gap-2 flex-shrink-0">
            {diffState.canApply ? (
              <button
                onClick={handleApplyDiffFromPanel}
                className="flex-1 flex items-center justify-center gap-1 py-2 bg-green-600 text-white text-xs font-bold rounded-lg hover:bg-green-700 transition-colors"
              >
                <Check className="w-3 h-3" /> {t('common.apply')}
              </button>
            ) : diffState.canRevert ? (
              <button
                onClick={handleRevertDiffFromPanel}
                className="flex-1 flex items-center justify-center gap-1 py-2 bg-amber-600 text-white text-xs font-bold rounded-lg hover:bg-amber-700 transition-colors"
              >
                <RotateCcw className="w-3 h-3" /> {t('common.restore')}
              </button>
            ) : (
              <div className="flex-1 flex items-center justify-center gap-1 py-2 bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400 text-xs font-bold rounded-lg cursor-not-allowed select-none">
                {diffState.blockedReason === 'multipleOriginal'
                  ? t('annotations.multipleOriginal')
                  : diffState.blockedReason === 'multipleApplied'
                  ? t('annotations.multipleApplied')
                  : t('annotations.originalNotFound')}
              </div>
            )}
            <button
              onClick={closeDiffPanel}
              className="flex-1 py-2 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 text-xs font-bold rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
            >
              {t('common.close')}
            </button>
          </div>
        </div>
      )}
      </div>
    </div>
  )
}
