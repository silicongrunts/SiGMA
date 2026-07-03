import { useEffect, useRef } from 'react'

/**
 * Fires *onClickOutside* when a mousedown event lands outside *ref*.
 *
 * @param {React.RefObject} ref              External ref to monitor.
 * @param {Function}        onClickOutside   Called on outside click.
 * @param {boolean}         [enabled=true]   When false, the listener is not attached.
 */
export function useClickOutside(ref, onClickOutside, enabled = true) {
  const stableCallback = useRef(onClickOutside)
  stableCallback.current = onClickOutside

  useEffect(() => {
    if (!enabled) return
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        stableCallback.current(e)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [enabled, ref])
}
