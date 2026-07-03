import React, { useRef, useState, useEffect, useCallback } from 'react'

export function ResizablePanels({ direction = 'horizontal', children, className = '', initialSizes = [], resizerContent = [] }) {
  const containerRef = useRef(null)
  const isHorizontal = direction === 'horizontal'

  const childrenArray = React.Children.toArray(children).filter(Boolean)

  const [sizes, setSizes] = useState(() => {
    if (initialSizes.length === childrenArray.length) return initialSizes
    return childrenArray.map(() => (100 / childrenArray.length) + '%')
  })

  // Re-sync sizes when the number of panels changes (e.g. entering/exiting notebook mode)
  useEffect(() => {
    if (initialSizes.length === childrenArray.length) {
      setSizes(initialSizes)
    } else {
      setSizes(childrenArray.map(() => (100 / childrenArray.length) + '%'))
    }
  }, [childrenArray.length, initialSizes.length])

  // Drag state — all in ref to survive React re-renders from SSE / message stream
  const dragRef = useRef({
    active: false,       // true during drag
    index: null,         // which resizer
    panels: [],          // cached DOM elements [data-panel]
    rafId: null,         // pending requestAnimationFrame
    overlay: null,       // overlay DOM element
  })

  // Called during React render — if dragging, use the live DOM size instead of
  // the sizes[] state.  This prevents SSE-driven re-renders from overwriting
  // the manual flexBasis that the rAF callback wrote directly on the DOM.
  const getPanelStyle = useCallback((index) => {
    const d = dragRef.current
    if (d.active && d.panels[index]) {
      const panel = d.panels[index]
      const size = isHorizontal ? panel.offsetWidth : panel.offsetHeight
      const container = containerRef.current
      if (container && size > 0) {
        const containerSize = isHorizontal ? container.offsetWidth : container.offsetHeight
        if (containerSize > 0 && d.panels.length === 2 && index === 1) {
          // Second panel in 2-panel layout stays flexible
          return { flexBasis: '0', flexGrow: 1, flexShrink: 1 }
        }
        return { flexBasis: `${(size / containerSize) * 100}%`, flexGrow: 0, flexShrink: 0 }
      }
    }
    // Not dragging — use normal sizes state
    const s = sizes[index]
    return {
      flexBasis: s === '1' ? '0' : s,
      flexGrow: s === '1' ? 1 : 0,
      flexShrink: s === '1' ? 1 : 0,
    }
  }, [isHorizontal, sizes])

  // ── overlay helpers ──────────────────────────────────────────────────
  // A transparent full-screen overlay captures mouse events on top of
  // iframes (VNC, Jupyter) so that mousemove/mouseup always fire on the
  // parent window — never inside the iframe where they'd be lost.

  const ensureOverlay = useCallback(() => {
    if (dragRef.current.overlay) return
    const div = document.createElement('div')
    Object.assign(div.style, {
      position: 'fixed', top: '0', left: '0', right: '0', bottom: '0',
      zIndex: '99999', cursor: isHorizontal ? 'col-resize' : 'row-resize',
      // transparent but must be non-null to block iframe interaction
      background: 'transparent',
    })
    document.body.appendChild(div)
    dragRef.current.overlay = div
  }, [isHorizontal])

  const removeOverlay = useCallback(() => {
    const ov = dragRef.current.overlay
    if (ov) {
      ov.remove()
      dragRef.current.overlay = null
    }
  }, [])

  // Cleanup overlay on unmount (defensive)
  useEffect(() => {
    return () => {
      removeOverlay()
    }
  }, [removeOverlay])

  // ── event handlers ───────────────────────────────────────────────────

  const handleMouseDown = useCallback((e, index) => {
    e.preventDefault()
    if (!containerRef.current) return
    const panels = Array.from(containerRef.current.querySelectorAll('[data-panel]'))
    dragRef.current = {
      active: true,
      index,
      panels,
      rafId: null,
      overlay: dragRef.current.overlay,  // preserve overlay if exists
    }
    ensureOverlay()
  }, [ensureOverlay])

  const handleMouseMove = useCallback((e) => {
    const d = dragRef.current
    if (!d.active || !containerRef.current) return

    if (d.rafId !== null) {
      cancelAnimationFrame(d.rafId)
      d.rafId = null
    }

    d.rafId = requestAnimationFrame(() => {
      d.rafId = null
      const { panels, index } = d
      if (!d.active || !containerRef.current) return

      const containerRect = containerRef.current.getBoundingClientRect()
      const mousePos = isHorizontal ? e.clientX : e.clientY
      const startPos = isHorizontal ? containerRect.left : containerRect.top

      // Read all sizes at once
      const panelSizes = panels.map(el => (isHorizontal ? el.offsetWidth : el.offsetHeight))
      const prevPanelsSize = panelSizes.slice(0, index).reduce((acc, s) => acc + s, 0)
      const newSizePx = Math.max(150, mousePos - startPos - prevPanelsSize)

      if (panels.length === 2) {
        panels[index].style.flexBasis = `${newSizePx}px`
        panels[index].style.flexGrow = '0'
        panels[index + 1].style.flexBasis = '0'
        panels[index + 1].style.flexGrow = '1'
      } else {
        const currentTotalSize = panelSizes[index] + (panelSizes[index + 1] ?? 0)
        const nextPanelSizePx = currentTotalSize - newSizePx
        if (nextPanelSizePx < 150) return

        panels[index].style.flexBasis = `${newSizePx}px`
        panels[index].style.flexGrow = '0'
        panels[index + 1].style.flexBasis = `${nextPanelSizePx}px`
        panels[index + 1].style.flexGrow = '0'
      }
    })
  }, [isHorizontal])

  const handleMouseUp = useCallback(() => {
    const d = dragRef.current
    if (!d.active || !containerRef.current) return
    d.active = false  // prevent further rAF callbacks

    if (d.rafId !== null) {
      cancelAnimationFrame(d.rafId)
      d.rafId = null
    }

    removeOverlay()

    const container = containerRef.current
    const { panels } = d
    const containerSize = isHorizontal ? container.offsetWidth : container.offsetHeight

    if (containerSize > 0) {
      const newSizes = panels.map(el => {
        const size = isHorizontal ? el.offsetWidth : el.offsetHeight
        return `${(size / containerSize) * 100}%`
      })

      if (panels.length === 2) {
        newSizes[1] = '1'
      }

      setSizes(newSizes)
    }

    d.index = null
    d.panels = []
  }, [isHorizontal, removeOverlay])

  // Always-on event listeners
  useEffect(() => {
    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }
  }, [handleMouseMove, handleMouseUp])

  // ── render ───────────────────────────────────────────────────────────

  return (
    <div
      ref={containerRef}
      className={`flex ${isHorizontal ? 'flex-row' : 'flex-col'} ${className} w-full h-full overflow-hidden`}
    >
      {childrenArray.map((child, index) => {
        const isLast = index === childrenArray.length - 1
        const panelStyle = getPanelStyle(index)

        return (
          <React.Fragment key={index}>
            <div
              data-panel
              className="flex flex-col min-w-0 min-h-0 overflow-hidden relative"
              style={panelStyle}
            >
              {child}
            </div>
            {!isLast && (
              <div
                data-resizer
                className={`resizer ${isHorizontal ? 'w-[6px] cursor-col-resize h-full' : 'h-[6px] cursor-row-resize w-full'} ${dragRef.current.index === index ? 'resizing' : ''} bg-gray-100 dark:bg-gray-800 hover:bg-blue-500 dark:hover:bg-blue-600 transition-colors z-50 relative`}
                onMouseDown={(e) => handleMouseDown(e, index)}
              >
                {resizerContent[index] || null}
              </div>
            )}
          </React.Fragment>
        )
      })}
    </div>
  )
}

export default ResizablePanels
