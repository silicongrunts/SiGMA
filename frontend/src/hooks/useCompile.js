/**
 * useCompile — unified LaTeX compilation hook.
 *
 * Guards compilation behind isTexFile (set by useFileActions). When the
 * response carries a pdf_path (clean compile, or errors-but-PDF-still-made),
 * bumps previewSource.compileVersion so the Preview component's load effect
 * re-fetches the freshly compiled PDF. When success is false, flashes the
 * log button yellow; only when there is no usable PDF does it open the log
 * modal / toast.
 *
 * Pre-compile save delegates to handleSave() so conflict detection is
 * handled uniformly.
 */
import { useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { compileAPI } from '../api'
import { toastError } from '../components/Toast'
import { getCompiledPdfName } from '../utils/constants'

export function useCompile({ projectId, editorRef, previewRef, handleSave }) {
  const { t } = useTranslation()
  const compilingRef = useRef(false)

  const handleCompile = useCallback(async (isSilent = false, skipSave = false) => {
    if (!projectId) return
    if (compilingRef.current) return

    // Only compile in TeX mode (isTexFile is the single source of truth)
    if (!useStore.getState().isTexFile) return

    compilingRef.current = true

    const state = useStore.getState()

    // Auto-save before compile (unless skipped)
    if (!skipSave) {
      const saved = await handleSave(false, false)
      if (!saved) {
        // Conflict cancelled or save failed — abort compile
        compilingRef.current = false
        return
      }
    }

    state.setCompiling(true)
    state.setCompileFailed(false)
    state.setCompileDiagnostics([])
    if (!isSilent) state.setCompileLogs(t('compile.compiling'))
    const compileMainFile = state.currentProject?.main_file || ''
    const compileOutputName = getCompiledPdfName(compileMainFile || state.currentFile)

    try {
      const res = await compileAPI.compile(projectId, {
        engine: state.currentProject?.engine || 'pdflatex',
        main_file: compileMainFile,
      })
      state.setCompileLogs(res.log)
      state.setCompileDiagnostics(res.diagnostics || [])

      // Decide preview refresh, log-button flash, and error modal from two
      // independent signals: pdf_path (a usable PDF exists) and success
      // (the compile was clean). Three outcomes:
      //   - clean + PDF        → refresh preview
      //   - errors + PDF       → refresh preview AND flash log yellow
      //   - errors + no PDF    → flash log yellow AND open error modal
      if (res.pdf_path) {
        const current = useStore.getState()
        const ps = current.previewSource
        const currentOutputName = ps.outputName || getCompiledPdfName(ps.mainFile || ps.path)
        if (current.isTexFile && ps.kind === 'pdf-compiled' && currentOutputName === compileOutputName) {
          // Tell Preview to reload — it will fetch the new PDF itself.
          // Going through previewSource.compileVersion keeps Preview a pure
          // function of the store (no hidden blob-passing side channel).
          current.bumpCompileVersion()
        }
      }

      if (!res.success) {
        state.setCompileFailed(true)
        // No PDF to show → surface the error log. When a PDF exists, the
        // flashing log button alone is enough (see the truth table above).
        if (!res.pdf_path && !isSilent) {
          toastError(t('compile.failed'))
          state.setShowLogModal(true)
        }
      }
    } catch (e) {
      if (!isSilent) {
        toastError(t('compile.error', { message: e.message }))
        state.setShowLogModal(true)
      }
    } finally {
      state.setCompiling(false)
      compilingRef.current = false
    }
  }, [projectId, editorRef, previewRef, handleSave, t])

  return { handleCompile }
}
