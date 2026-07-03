/**
 * useAutoCompile — auto-compile when transitioning into TeX mode.
 *
 * Tracks the previous isTexFile state. When the current file changes
 * AND the mode transitions from non-TeX to TeX, triggers a silent
 * compilation (skip-save, since content was just loaded).
 *
 * Does NOT fire on initial mount or when staying within TeX mode.
 */
import { useEffect, useRef } from 'react'
import { useStore } from '../store/useStore'

export function useAutoCompile({ currentFile, handleCompile }) {
  const prevIsTexRef = useRef(null)   // null = first mount, don't fire
  const prevFileRef = useRef(null)

  useEffect(() => {
    if (!currentFile) {
      // File cleared (e.g. project switch) — reset so next load
      // is treated as a fresh start, not a mode transition.
      prevFileRef.current = null
      prevIsTexRef.current = null
      return
    }
    const isTex = useStore.getState().isTexFile

    // Only act when the file actually changed (not on initial mount)
    if (prevFileRef.current && prevFileRef.current !== currentFile) {
      // Transition: non-TeX → TeX mode → auto-compile
      if (prevIsTexRef.current === false && isTex === true) {
        handleCompile(true, true)   // silent=true, skipSave=true
      }
    }

    prevFileRef.current = currentFile
    prevIsTexRef.current = isTex
  }, [currentFile, handleCompile])
}
