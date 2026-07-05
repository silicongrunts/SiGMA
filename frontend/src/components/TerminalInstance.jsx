/**
 * TerminalInstance — a single xterm.js terminal bound to one PTY session.
 *
 * Owns the Terminal object, addons (Fit, WebLinks, Search), and the
 * WebSocket connection via the useTerminal hook.
 */
import { useEffect, useRef, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { SearchAddon } from '@xterm/addon-search'
import '@xterm/xterm/css/xterm.css'
import useTerminal from '../hooks/useTerminal'

// Dark theme matching VS Code-style terminals
const TERMINAL_THEME = {
  background: '#1e1e1e',
  foreground: '#d4d4d4',
  cursor: '#d4d4d4',
  cursorAccent: '#1e1e1e',
  selectionBackground: '#264f78',
  selectionForeground: '#d4d4d4',
  black: '#000000',
  red: '#cd3131',
  green: '#0dbc79',
  yellow: '#e5e510',
  blue: '#2472c8',
  magenta: '#bc3fbc',
  cyan: '#11a8cd',
  white: '#e5e5e5',
  brightBlack: '#666666',
  brightRed: '#f14c4c',
  brightGreen: '#23d18b',
  brightYellow: '#f5f543',
  brightBlue: '#3b8eea',
  brightMagenta: '#d670d6',
  brightCyan: '#29b8db',
  brightWhite: '#ffffff',
}

export default function TerminalInstance({ slot, projectId, active, onExit }) {
  const containerRef = useRef(null)
  const terminalRef = useRef(null)
  const fitAddonRef = useRef(null)
  const resizeObserverRef = useRef(null)

  // Initialize xterm.js instance once
  useEffect(() => {
    if (!containerRef.current || terminalRef.current) return

    const terminal = new Terminal({
      theme: TERMINAL_THEME,
      fontSize: 13,
      fontFamily: '"Cascadia Code", "Fira Code", Menlo, Monaco, "Courier New", monospace',
      cursorBlink: true,
      cursorStyle: 'bar',
      scrollback: 5000,
      allowProposedApi: true,
      allowTransparency: false,
      convertEol: true,
    })

    const fitAddon = new FitAddon()
    const webLinksAddon = new WebLinksAddon()
    const searchAddon = new SearchAddon()

    terminal.loadAddon(fitAddon)
    terminal.loadAddon(webLinksAddon)
    terminal.loadAddon(searchAddon)

    terminal.open(containerRef.current)

    // Delay first fit to ensure the container has layout
    requestAnimationFrame(() => {
      try { fitAddon.fit() } catch { /* container may not be visible yet */ }
    })

    terminalRef.current = terminal
    fitAddonRef.current = fitAddon

    return () => {
      searchAddon.dispose()
      webLinksAddon.dispose()
      fitAddon.dispose()
      terminal.dispose()
      terminalRef.current = null
      fitAddonRef.current = null
    }
  }, [])

  // ResizeObserver to refit when container size changes
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const observer = new ResizeObserver(() => {
      if (fitAddonRef.current && el.offsetParent !== null) {
        // Defer to next frame so the browser has settled layout before xterm recalculates rows.
        // Prevents the last row being partially obscured during rapid drag resize.
        requestAnimationFrame(() => {
          try { fitAddonRef.current.fit() } catch { /* container may not be visible yet */ }
        })
      }
    })

    observer.observe(el)
    resizeObserverRef.current = observer

    return () => {
      observer.disconnect()
      resizeObserverRef.current = null
    }
  }, [])

  // Refit when this terminal becomes the active tab
  useEffect(() => {
    if (active && fitAddonRef.current && containerRef.current?.offsetParent !== null) {
      requestAnimationFrame(() => {
        try { fitAddonRef.current.fit() } catch { /* container may not be visible yet */ }
      })
    }
  }, [active])

  // WebSocket connection
  useTerminal({ slot, projectId, terminalRef, onExit })

  return (
    <div className="w-full h-full" style={{ padding: '4px 0 0 8px' }}>
      <div
        ref={containerRef}
        className="w-full h-full"
      />
    </div>
  )
}
