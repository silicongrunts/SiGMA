/**
 * Shared constants for TeX-related file handling.
 *
 * Single source of truth for TeX extension lists and the TeX mode
 * computation used by useFileActions, Header, useAutoCompile, etc.
 */

/** All TeX-related file extensions. */
export const TEX_EXTS = ['tex', 'cls', 'sty', 'bib']

/** Primary TeX extensions that trigger auto-set of main_file. */
export const PRIMARY_TEX_EXTS = ['tex']

/**
 * Determine whether a file should be treated as TeX mode.
 *
 *  - TeX-related files → TeX mode only when main_file is configured
 *  - everything else → not TeX mode
 */
export function computeIsTexFile(filePath, mainFile) {
  const ext = filePath?.split('.').pop()?.toLowerCase()
  if (!TEX_EXTS.includes(ext)) return false
  return !!mainFile
}

export function getCompiledPdfName(mainFile) {
  const normalized = String(mainFile || '').replace(/\\/g, '/')
  const lastSlash = normalized.lastIndexOf('/')
  if (lastSlash === -1) return 'output.pdf'
  const dir = normalized.slice(0, lastSlash)
  return dir ? `${dir}/output.pdf` : 'output.pdf'
}

export function getCompiledPreviewSource(sourcePath, mainFile, compileVersion = 0) {
  const effectiveMainFile = mainFile || sourcePath || ''
  return {
    kind: 'pdf-compiled',
    path: sourcePath,
    mainFile: effectiveMainFile,
    outputName: getCompiledPdfName(effectiveMainFile),
    compileVersion,
  }
}
