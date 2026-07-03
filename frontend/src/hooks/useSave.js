/**
 * useSave — unified file save hook with conflict detection.
 *
 * ALL file saves must go through handleSave(). Other hooks (useCompile,
 * useFileActions) call handleSave() instead of filesAPI.write() directly.
 *
 * Conflict detection flow:
 *   1. Send baseline hash from store (obtained from backend at load/save time)
 *   2. Backend compares baseline hash against disk file hash
 *   3. If mismatch → conflict modal → user decides cancel or force-save
 *   4. On success, backend returns new hash → update store
 *
 * handleSave() returns boolean: true = saved, false = cancelled/failed.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { filesAPI } from '../api'
import { toastError } from '../components/Toast'

export function useSave({ projectId, editorRef, handleCompileRef }) {
  const { t } = useTranslation()
  const savingRef = useRef(false)
  const [conflictState, setConflictState] = useState(null)
  const conflictResolverRef = useRef(null)

  // Cleanup: if unmounted while conflict modal is showing, resolve with
  // cancel so savingRef doesn't stay permanently locked.
  useEffect(() => {
    return () => {
      if (conflictResolverRef.current) {
        conflictResolverRef.current(false)
        conflictResolverRef.current = null
      }
    }
  }, [])

  /** Called by the FileConflictModal to resolve the user's decision. */
  const resolveConflict = useCallback((forceSave) => {
    const resolve = conflictResolverRef.current
    conflictResolverRef.current = null
    resolve?.(forceSave)
    setConflictState(null)
  }, [])

  const handleSave = useCallback(async (shouldCompile = true, isAutoSave = false) => {
    if (savingRef.current) return false

    const state = useStore.getState()
    if (!projectId || !state.currentFile || !editorRef.current) return false

    savingRef.current = true
    try {
      const content = editorRef.current.getContent()
      if (content === null) return false

      // Sync annotation positions before saving the file.
      // Annotation sync failure must not block file save.
      try { await editorRef.current.syncAnnotationsNow?.() } catch { /* non-critical */ }

      const result = await filesAPI.write(projectId, state.currentFile, content, { hash: state.fileHash })

      if (result?.conflict) {
        // Pause save, show conflict modal, await user decision
        setConflictState({
          fileName: state.currentFile,
          diffLines: result.diff_lines,
        })
        const userChoice = await new Promise((resolve) => {
          conflictResolverRef.current = resolve
        })
        if (!userChoice) return false // cancelled

        // Force save — overwrite disk
        const forceResult = await filesAPI.write(projectId, state.currentFile, content, { force: true })
        state.setFileHash(forceResult?.hash ?? null)
      } else {
        // Normal save — update baseline hash from backend
        state.setFileHash(result?.hash ?? null)
      }

      state.markSaved(isAutoSave ? 'auto' : 'manual')

      if (shouldCompile && state.isTexFile) {
        handleCompileRef.current?.(true, true)
      }

      return true
    } catch (e) {
      toastError(t('common.saveFailed'))
      return false
    } finally {
      savingRef.current = false
    }
  }, [projectId, editorRef, t])

  return { handleSave, conflictState, resolveConflict }
}
