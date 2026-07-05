/**
 * useFileActions — unified file-selection + navigation hook.
 *
 * The SOLE writer of `previewSource` in the store. Every code path that
 * should change what the preview panel displays goes through one of the
 * branches below and explicitly sets `previewSource`. This guarantees the
 * preview's title and rendered content cannot drift (both derive from the
 * same descriptor).
 *
 * Branch summary (matches the user-facing behavior matrix):
 *   .ipynb         → editor switches to notebook; previewSource untouched
 *                    (preview is hidden in notebook mode)
 *   .pdf           → previewSource = pdf-standalone; currentFile untouched
 *   .md / .tex ... → backend-validated text; currentFile + previewSource
 *                    both set (md → markdown, tex → pdf-compiled, other
 *                    text → previewSource untouched)
 *   binary         → previewSource = binary-error; currentFile untouched
 *
 * isTexFile is also set here as the single writer, so Header / AutoCompile /
 * Compile all see a consistent TeX-mode flag.
 *
 * When opening a .tex file in a project with no main_file configured,
 * automatically sets main_file to that file (optimistic store update +
 * background API sync).
 */
import { useCallback } from 'react'
import { useStore } from '../store/useStore'
import { filesAPI, projectsAPI } from '../api'
import { storage } from '../utils/storage'
import { TEX_EXTS, PRIMARY_TEX_EXTS, computeIsTexFile, getCompiledPdfName, getCompiledPreviewSource } from '../utils/constants'

export function useFileActions({ projectId, editorRef, previewRef, handleSave }) {
  const handleFileSelect = useCallback(async (node) => {
    if (!node?.path) return false

    const state = useStore.getState()

    // Auto-save current file before switching — delegate to handleSave
    // for unified conflict detection.
    if (state.hasUnsavedChanges && state.currentFile && editorRef.current) {
      const saved = await handleSave(false, false)
      if (!saved) return false // conflict cancelled — abort file switch
    }

    const ext = node.path.split('.').pop()?.toLowerCase()
    if (!ext) return false

    const project = state.currentProject

    // ── Notebook → switch editor to notebook mode; leave preview untouched ──
    if (ext === 'ipynb') {
      state.setIsNotebookMode(true)
      state.setIsTexFile(false)
      state.setCurrentFile(node.path)
      if (projectId) storage.setLastFile(projectId, node.path)
      return true
    }

    // ── Standalone PDF → preview only; editor unchanged ──
    if (ext === 'pdf') {
      state.setIsNotebookMode(false)
      state.setPreviewSource({ kind: 'pdf-standalone', path: node.path, compileVersion: 0 })
      if (projectId) storage.setLastFile(projectId, node.path)
      return true
    }

    // ── All other files (text or unknown) → validate content via backend ──
    state.setIsNotebookMode(false)

    const isSameFile = node.path === state.currentFile

    // For same-file re-selection of non-md files (e.g. SyncTeX backward jump),
    // skip the content fetch — Editor already has the content and re-fetching
    // would be redundant (and for .tex files, reset would destroy PDF preview).
    if (isSameFile && ext !== 'md') {
      state.setIsLoadingFile(false)
      return true
    }

    state.setIsLoadingFile(true)

    let isBinary = false
    let validatedContent = null
    try {
      const data = await filesAPI.read(projectId, node.path)
      validatedContent = data.content ?? ''
      if (data.hash) state.setFileHash(data.hash)
      // Binary content is surfaced via BinaryFileError on the backend side
      // (handled in the catch below); no client-side heuristic needed here.
    } catch (error) {
      if (error?.status === 400 && String(error.message || '').includes('Cannot open binary file')) {
        isBinary = true
      } else {
        state.setIsLoadingFile(false)
        return false
      }
    }

    if (isBinary) {
      // Editor stays on whatever it was; preview shows download UI for this file.
      state.setPreviewSource({ kind: 'binary-error', path: node.path, compileVersion: 0 })
      state.setIsLoadingFile(false)
      return false
    }

    // Content is text — classify and set state.
    const isTexExt = TEX_EXTS.includes(ext)
    const mainFile = project?.main_file || (PRIMARY_TEX_EXTS.includes(ext) ? node.path : '')
    const isTexFile = isTexExt && computeIsTexFile(node.path, mainFile)

    if (isTexFile) {
      state.setIsTexFile(true)
      state.setCurrentFile(node.path)
      const nextPreviewSource = getCompiledPreviewSource(node.path, mainFile)
      const previousPreviewSource = useStore.getState().previewSource
      if (previousPreviewSource?.kind === 'pdf-compiled') {
        const previousOutputName = previousPreviewSource.outputName
          || getCompiledPdfName(previousPreviewSource.mainFile || previousPreviewSource.path)
        if (previousOutputName === nextPreviewSource.outputName) {
          nextPreviewSource.compileVersion = previousPreviewSource.compileVersion || 0
        }
      }
      state.setPreviewSource(nextPreviewSource)
      if (projectId) storage.setLastFile(projectId, node.path)

      if (PRIMARY_TEX_EXTS.includes(ext) && project?.id && !project.main_file) {
        state.setCurrentProject({ ...project, main_file: node.path })
        projectsAPI.update(project.id, { main_file: node.path })
          .then(updated => {
            const current = useStore.getState()
            if (current.currentProject?.id !== project.id) return
            // If the user is still on this file, sync the authoritative project
            // record from the backend. Otherwise leave the optimistic update in
            // place; a later file selection will reconcile as needed.
            if (current.currentFile === node.path) {
              current.setCurrentProject(updated)
            }
          })
          .catch(e => console.warn('Failed to auto-set main_file:', e))
      }
    } else if (ext === 'md') {
      state.setIsTexFile(false)
      state.setCurrentFile(node.path)
      state.setPreviewSource({ kind: 'markdown', path: node.path, compileVersion: 0 })
      if (projectId) storage.setLastFile(projectId, node.path)
    } else {
      // Other editable text (.py, .cpp, .txt, .cls-without-main_file, ...):
      // editor shows the file; preview keeps showing whatever was there before.
      state.setIsTexFile(false)
      state.setCurrentFile(node.path)
      if (projectId) storage.setLastFile(projectId, node.path)
    }

    // Re-selecting current md file → Editor won't re-emit (docChanged=false
    // for same content), so push the content into Preview's live-md buffer.
    if (isSameFile && ext === 'md') {
      previewRef.current?.setMarkdownContent(validatedContent)
    }

    state.setIsLoadingFile(false)
    return true
  }, [projectId, editorRef, previewRef, handleSave])

  const handleExitNotebook = useCallback(() => {
    const state = useStore.getState()
    state.setIsNotebookMode(false)

    const lastFile = storage.getLastFile(projectId)
    const fallback = state.currentProject?.main_file || ''
    const target = (lastFile && !lastFile.endsWith('.ipynb')) ? lastFile : fallback
    if (target) {
      handleFileSelect({ path: target, name: target.split('/').pop() })
    }
  }, [projectId, handleFileSelect])

  return { handleFileSelect, handleExitNotebook }
}
