/**
 * useSave — unified file save hook with conflict detection.
 *
 * ALL file saves must go through handleSave(). Other hooks (useCompile,
 * useFileActions) call handleSave() instead of filesAPI.write() directly.
 *
 * A 409 never overwrites either file or annotation state. The user can reload
 * the published disk/DB snapshot and retry from the editor.
 *
 * handleSave() returns boolean: true = saved, false = cancelled/failed.
 */
import { useCallback, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { toastError } from '../components/Toast'

export function useSave({ projectId, editorRef, handleCompileRef }) {
  const { t } = useTranslation()
  const savingRef = useRef(false)
  const [conflictState, setConflictState] = useState(null)
  const dismissConflict = useCallback(() => {
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

      // The backend coordinates file CAS, annotation CAS, and the durable
      // journal. It does not infer deletion from omitted annotations.
      const result = await editorRef.current.syncAnnotationsNow?.(content)
      if (!result) return false
      state.setFileHash(result.fileHash ?? null)

      state.markSaved(isAutoSave ? 'auto' : 'manual')

      if (shouldCompile && state.isTexFile) {
        handleCompileRef.current?.(true, true)
      }

      return true
    } catch (e) {
      if (e.status === 409) {
        setConflictState({ fileName: state.currentFile, diffLines: [] })
        return false
      }
      toastError(t('common.saveFailed'))
      return false
    } finally {
      savingRef.current = false
    }
  }, [projectId, editorRef, t])

  return { handleSave, conflictState, dismissConflict }
}
