/**
 * useTerminal — WebSocket connection hook for a single terminal instance.
 *
 * Manages the bidirectional connection between an xterm.js Terminal and
 * the backend PTY session.  Handles reconnection with exponential backoff
 * and keep-alive pings.
 *
 * Keepalive:
 * - Default unmount (navigation): closes WS normally → backend orphans session.
 * - Tab X close (kill message): sends {"type":"kill"} → backend kills session.
 * - Network disconnect: backend keeps session alive for 10 min.
 * - Reconnect: backend replays buffered output, terminal resets and restores.
 * - Taken over (code 4001): another browser tab claimed this slot — do NOT reconnect.
 */
import { useEffect, useRef, useCallback } from 'react'

const WS_BASE = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/v1/terminal`
const PING_INTERVAL = 30_000
const RECONNECT_BASE_DELAY = 1000
const RECONNECT_MAX_DELAY = 30_000

// Module-level set: slots marked for kill on next unmount (tab X close)
const _killSlots = new Set()

export function markSlotForKill(slot) {
  _killSlots.add(slot)
}

export default function useTerminal({ slot, projectId, terminalRef, onExit }) {
  const wsRef = useRef(null)
  const pingTimerRef = useRef(null)
  const reconnectTimerRef = useRef(null)
  const reconnectDelayRef = useRef(RECONNECT_BASE_DELAY)
  const disposedRef = useRef(false)
  const replayRef = useRef(false)
  const onExitRef = useRef(onExit)

  // Keep onExitRef current so WebSocket handlers never capture a stale callback
  onExitRef.current = onExit

  const sendJson = useCallback((obj) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj))
    }
  }, [])

  useEffect(() => {
    disposedRef.current = false

    if (!terminalRef.current || !projectId || slot == null) return

    const terminal = terminalRef.current

    // ── WebSocket message handlers ──

    function onWsMessage(event) {
      if (typeof event.data === 'string') return // ignore text frames (errors)
      // On reconnect, first binary frame triggers a reset before replay
      if (replayRef.current) {
        replayRef.current = false
        terminal.reset()
      }
      terminal.write(new Uint8Array(event.data))
    }

    function onWsClose(event) {
      stopPing()
      if (disposedRef.current) return
      // Code 4001 = session taken over by another tab — do NOT reconnect or kill
      if (event.code === 4001) {
        onExitRef.current?.('taken_over')
        return
      }
      // Code 1000 = shell exited / logout / Ctrl-D → session already dead on backend
      if (event.code === 1000) {
        onExitRef.current?.('shell_exit')
        return
      }
      // Any other code (1006 network, etc.) → reconnect
      scheduleReconnect()
    }

    function onWsError() {
      // onWsClose will fire after this
    }

    // ── xterm data handler ──

    function onTerminalData(data) {
      sendJson({ type: 'input', data: btoa(data) })
    }

    // ── xterm resize handler ──

    function onTerminalResize({ cols, rows }) {
      sendJson({ type: 'resize', cols, rows })
    }

    // ── Connection management ──

    function connect() {
      if (disposedRef.current) return

      // Pass cols/rows in URL so server can size the PTY before replay snapshot
      const ws = new WebSocket(`${WS_BASE}/${projectId}?slot=${slot}&cols=${terminal.cols}&rows=${terminal.rows}`)
      ws.binaryType = 'arraybuffer'
      ws.addEventListener('message', onWsMessage)
      ws.addEventListener('close', onWsClose)
      ws.addEventListener('error', onWsError)

      ws.addEventListener('open', () => {
        replayRef.current = true // first binary data will be buffer replay
        reconnectDelayRef.current = RECONNECT_BASE_DELAY
        sendJson({ type: 'resize', cols: terminal.cols, rows: terminal.rows })
        startPing()
      })

      wsRef.current = ws
    }

    function startPing() {
      stopPing()
      pingTimerRef.current = setInterval(() => {
        sendJson({ type: 'ping' })
      }, PING_INTERVAL)
    }

    function stopPing() {
      if (pingTimerRef.current) {
        clearInterval(pingTimerRef.current)
        pingTimerRef.current = null
      }
    }

    function scheduleReconnect() {
      if (disposedRef.current) return
      const delay = reconnectDelayRef.current
      reconnectDelayRef.current = Math.min(delay * 2, RECONNECT_MAX_DELAY)
      reconnectTimerRef.current = setTimeout(connect, delay)
    }

    // ── Attach xterm listeners ──

    const dataDisposable = terminal.onData(onTerminalData)
    const resizeDisposable = terminal.onResize(onTerminalResize)

    // ── Initial connection ──
    connect()

    // ── Cleanup ──
    return () => {
      disposedRef.current = true
      stopPing()
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      dataDisposable.dispose()
      resizeDisposable.dispose()

      const shouldKill = _killSlots.delete(slot)
      const ws = wsRef.current
      if (ws) {
        ws.removeEventListener('message', onWsMessage)
        ws.removeEventListener('close', onWsClose)
        ws.removeEventListener('error', onWsError)
        if (shouldKill) {
          // Tab X — kill session immediately
          if (ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ type: 'kill' })) } catch { /* ignore */ }
          }
          if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
            ws.close(3001, 'shutdown')
          }
        } else {
          // Navigation / other — close normally, backend will orphan session
          if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
            ws.close(1000)
          }
        }
        wsRef.current = null
      }
    }
  }, [projectId, terminalRef, sendJson, slot])
}
