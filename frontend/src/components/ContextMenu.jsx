import { useRef, useState, useLayoutEffect, useEffect } from 'react'

/**
 * Shared context menu used by FileTree and LibraryBrowser.
 *
 * Props:
 *   x, y       — screen coordinates for initial placement
 *   options    — [{ label, icon?, action, danger?, separator? }]
 *   onClose    — called when menu should close
 */
export default function ContextMenu({ x, y, options, onClose }) {
  const menuRef = useRef(null)
  const [pos, setPos] = useState({ x, y })

  useLayoutEffect(() => {
    if (menuRef.current) {
      const { offsetWidth, offsetHeight } = menuRef.current
      const winWidth = window.innerWidth
      const winHeight = window.innerHeight
      let newX = x
      let newY = y
      if (x + offsetWidth > winWidth) newX = winWidth - offsetWidth - 10
      if (y + offsetHeight > winHeight) newY = winHeight - offsetHeight - 10
      setPos({ x: newX, y: newY })
    }
  }, [x, y])

  useEffect(() => {
    const close = (e) => {
      // Don't close if the event is inside this menu
      if (e.target && menuRef.current?.contains(e.target)) return
      onClose()
    }
    // Delay contextmenu registration by one frame so the opening event
    // doesn't immediately close the menu via window bubbling.
    const id = requestAnimationFrame(() => {
      window.addEventListener('contextmenu', close)
    })
    window.addEventListener('click', close)
    window.addEventListener('mousedown', close)
    return () => {
      cancelAnimationFrame(id)
      window.removeEventListener('contextmenu', close)
      window.removeEventListener('click', close)
      window.removeEventListener('mousedown', close)
    }
  }, [onClose])

  if (!options || options.length === 0) return null

  return (
    <div
      ref={menuRef}
      className="fixed z-[1000] bg-white/95 dark:bg-gray-900/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-2xl rounded-xl py-1.5 min-w-[200px] animate-in fade-in zoom-in duration-150"
      style={{ left: pos.x, top: pos.y }}
      onClick={e => e.stopPropagation()}
    >
      {options.map((opt, i) =>
        opt.separator ? (
          <div key={i} className="h-px bg-gray-100 dark:bg-gray-800 my-1 mx-2" />
        ) : (
          <button
            key={i}
            disabled={opt.disabled || false}
            onClick={() => { if (!opt.disabled) { opt.action(); onClose() } }}
            className={`w-full flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${opt.disabled ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : opt.danger ? 'text-red-600 hover:bg-red-50 dark:hover:bg-red-900/30' : 'text-gray-700 hover:bg-blue-50 hover:text-blue-600 dark:text-gray-300 dark:hover:bg-blue-900/30 dark:hover:text-blue-400'}`}
          >
            {opt.icon}
            <span className="flex-1 text-left">{opt.label}</span>
          </button>
        )
      )}
    </div>
  )
}
