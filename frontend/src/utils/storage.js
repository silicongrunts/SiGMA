/**
 * Local storage boundary for SiGMA.
 *
 * Storage is intentionally split into one global object and one object per
 * project. Keep all browser storage reads and writes in this module so key
 * ownership, migration, quota handling, and cleanup stay reviewable.
 */

export const STORAGE_KEYS = {
  global: 'sigma_global',
  project: (projectId) => `sigma_project_${projectId}`,
}

const LEGACY_KEYS = {
  lastFile: (projectId) => `sigma_last_file_${projectId}`,
  session: (projectId) => `sigma_session_${projectId}`,
  budgetPrefix: (projectId) => `sigma_budget_${projectId}_`,
  theme: 'sigma_theme',
  language: 'sigma_language',
  zustand: 'sigma-storage',
}

const STORAGE_VERSION = 1
const MAX_FILE_STATE_ENTRIES = 20

/** Language codes supported by the UI. Order is the selector display order. */
const SUPPORTED_LANGUAGE_CODES = ['en', 'zh-CN', 'zh-TW', 'ja', 'ko', 'hi', 'es', 'fr']

/**
 * Editor appearance option whitelists. Kept here (rather than imported from the
 * registry in utils/) so the storage layer never depends on a higher layer.
 * If the registry in utils/editorFonts.js or utils/highlightSchemes.js adds or
 * removes an option, update the matching array here too.
 */
const EDITOR_FONT_IDS = ['jetbrains-mono', 'fira-code', 'cascadia-code', 'source-code-pro', 'roboto-mono', 'system']
const EDITOR_SCHEME_IDS = ['default', 'github', 'solarized', 'dracula', 'monokai']
const EDITOR_FONT_SIZE_MIN = 12
const EDITOR_FONT_SIZE_MAX = 20
const EDITOR_LINE_HEIGHT_MIN = 1.2
const EDITOR_LINE_HEIGHT_MAX = 2.0

const DEFAULT_EDITOR_APPEARANCE = Object.freeze({
  fontFamily: 'jetbrains-mono',
  fontSize: 14,
  lineHeight: 1.5,
  syntaxScheme: 'default',
})

const DEFAULT_GLOBAL_STATE = Object.freeze({
  version: STORAGE_VERSION,
  theme: 'light',
  language: null,
  editorAppearance: { ...DEFAULT_EDITOR_APPEARANCE },
  updatedAt: 0,
})

const DEFAULT_PROJECT_STATE = Object.freeze({
  version: STORAGE_VERSION,
  updatedAt: 0,
  lastAccessedAt: 0,
  chat: {
    sessionId: null,
    budgetsBySession: {},
  },
  workspace: {
    activeTab: 'synthesis',
  },
  synthesis: {
    leftTab: 'files',
    editorFile: null,
    editorCursorByFile: {},
    editorScrollRatioByFile: {},
    previewFile: null,
    previewScrollRatioByFile: {},
  },
  library: {
    folderId: null,
    breadcrumbs: [],
    selectedDocId: null,
    sortBy: 'updated_at',
    sortOrder: 'desc',
    searchMode: 'keyword',
  },
})

function canUseStorage() {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined'
}

function now() {
  return Date.now()
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim() !== ''
}

function normalizeProjectPath(path) {
  return isNonEmptyString(path) ? path.replace(/^\/+/, '') : ''
}

function safeGetItem(key) {
  if (!canUseStorage()) return null
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSetItem(key, value) {
  if (!canUseStorage()) return false
  try {
    window.localStorage.setItem(key, value)
    return true
  } catch {
    return false
  }
}

function safeRemoveItem(key) {
  if (!canUseStorage()) return
  try {
    window.localStorage.removeItem(key)
  } catch {
    // Best-effort cleanup only; storage may be unavailable in private modes.
  }
}

function safeJsonParse(raw) {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    return isPlainObject(parsed) ? parsed : null
  } catch {
    return null
  }
}

function readJson(key) {
  return safeJsonParse(safeGetItem(key))
}

function writeJson(key, value) {
  return safeSetItem(key, JSON.stringify(value))
}

function sanitizeTheme(value) {
  return value === 'dark' ? 'dark' : 'light'
}

function sanitizeLanguage(value) {
  return SUPPORTED_LANGUAGE_CODES.includes(value) ? value : null
}

function sanitizeEditorAppearance(value) {
  const source = isPlainObject(value) ? value : {}
  const fontSize = Math.round(Number(source.fontSize))
  const lineHeight = Math.round(Number(source.lineHeight) * 10) / 10
  return {
    fontFamily: EDITOR_FONT_IDS.includes(source.fontFamily) ? source.fontFamily : DEFAULT_EDITOR_APPEARANCE.fontFamily,
    fontSize: Number.isSafeInteger(fontSize) && fontSize >= EDITOR_FONT_SIZE_MIN && fontSize <= EDITOR_FONT_SIZE_MAX
      ? fontSize
      : DEFAULT_EDITOR_APPEARANCE.fontSize,
    lineHeight: Number.isFinite(lineHeight) && lineHeight >= EDITOR_LINE_HEIGHT_MIN && lineHeight <= EDITOR_LINE_HEIGHT_MAX
      ? lineHeight
      : DEFAULT_EDITOR_APPEARANCE.lineHeight,
    syntaxScheme: EDITOR_SCHEME_IDS.includes(source.syntaxScheme) ? source.syntaxScheme : DEFAULT_EDITOR_APPEARANCE.syntaxScheme,
  }
}

function sanitizeBudget(value) {
  const n = Number(value)
  return Number.isSafeInteger(n) && n > 0 ? n : null
}

function sanitizeBreadcrumbs(value) {
  if (!Array.isArray(value)) return []
  return value
    .filter(item => isPlainObject(item) && ('id' in item) && isNonEmptyString(item.name))
    .map(item => ({ id: item.id || null, name: item.name }))
}

function sanitizeScrollMap(value) {
  if (!isPlainObject(value)) return {}
  const out = {}
  for (const [path, ratio] of Object.entries(value)) {
    const n = Number(ratio)
    const normalizedPath = normalizeProjectPath(path)
    if (normalizedPath && Number.isFinite(n)) {
      out[normalizedPath] = Math.max(0, Math.min(1, n))
    }
  }
  return trimObjectMap(out, MAX_FILE_STATE_ENTRIES)
}

function sanitizeCursorMap(value) {
  if (!isPlainObject(value)) return {}
  const out = {}
  for (const [path, cursor] of Object.entries(value)) {
    const normalizedPath = normalizeProjectPath(path)
    if (!normalizedPath || !isPlainObject(cursor)) continue
    const line = Number(cursor.line)
    const column = Number(cursor.column)
    if (Number.isSafeInteger(line) && line > 0 && Number.isSafeInteger(column) && column >= 0) {
      out[normalizedPath] = { line, column }
    }
  }
  return trimObjectMap(out, MAX_FILE_STATE_ENTRIES)
}

function sanitizeBudgetMap(value) {
  if (!isPlainObject(value)) return {}
  const out = {}
  for (const [sessionId, budget] of Object.entries(value)) {
    const n = sanitizeBudget(budget)
    if (isNonEmptyString(sessionId) && n) out[sessionId] = n
  }
  return out
}

function sanitizeGlobalState(value) {
  const source = isPlainObject(value) ? value : {}
  return {
    version: STORAGE_VERSION,
    theme: sanitizeTheme(source.theme),
    language: sanitizeLanguage(source.language),
    editorAppearance: sanitizeEditorAppearance(source.editorAppearance),
    updatedAt: Number.isFinite(source.updatedAt) ? source.updatedAt : 0,
  }
}

function sanitizeProjectState(value) {
  const source = isPlainObject(value) ? value : {}
  const chat = isPlainObject(source.chat) ? source.chat : {}
  const workspace = isPlainObject(source.workspace) ? source.workspace : {}
  const synthesis = isPlainObject(source.synthesis) ? source.synthesis : {}
  const library = isPlainObject(source.library) ? source.library : {}

  const activeTab = ['synthesis', 'explore', 'library'].includes(workspace.activeTab)
    ? workspace.activeTab
    : DEFAULT_PROJECT_STATE.workspace.activeTab
  const leftTab = ['files', 'git', 'chat'].includes(synthesis.leftTab)
    ? synthesis.leftTab
    : DEFAULT_PROJECT_STATE.synthesis.leftTab
  const sortBy = ['title', 'updated_at'].includes(library.sortBy)
    ? library.sortBy
    : DEFAULT_PROJECT_STATE.library.sortBy
  const sortOrder = ['asc', 'desc'].includes(library.sortOrder)
    ? library.sortOrder
    : DEFAULT_PROJECT_STATE.library.sortOrder
  const searchMode = ['keyword', 'semantic'].includes(library.searchMode)
    ? library.searchMode
    : DEFAULT_PROJECT_STATE.library.searchMode

  return {
    version: STORAGE_VERSION,
    updatedAt: Number.isFinite(source.updatedAt) ? source.updatedAt : 0,
    lastAccessedAt: Number.isFinite(source.lastAccessedAt) ? source.lastAccessedAt : 0,
    chat: {
      sessionId: isNonEmptyString(chat.sessionId) ? chat.sessionId : null,
      budgetsBySession: sanitizeBudgetMap(chat.budgetsBySession),
    },
    workspace: { activeTab },
    synthesis: {
      leftTab,
      editorFile: normalizeProjectPath(synthesis.editorFile) || null,
      editorCursorByFile: sanitizeCursorMap(synthesis.editorCursorByFile),
      editorScrollRatioByFile: sanitizeScrollMap(synthesis.editorScrollRatioByFile),
      previewFile: normalizeProjectPath(synthesis.previewFile) || null,
      previewScrollRatioByFile: sanitizeScrollMap(synthesis.previewScrollRatioByFile),
    },
    library: {
      folderId: library.folderId || null,
      breadcrumbs: sanitizeBreadcrumbs(library.breadcrumbs),
      selectedDocId: library.selectedDocId || null,
      sortBy,
      sortOrder,
      searchMode,
    },
  }
}

function mergeProjectState(current, patch) {
  const nextPatch = typeof patch === 'function' ? patch(current) : patch
  if (!isPlainObject(nextPatch)) return current
  return sanitizeProjectState({
    ...current,
    ...nextPatch,
    chat: { ...current.chat, ...(nextPatch.chat || {}) },
    workspace: { ...current.workspace, ...(nextPatch.workspace || {}) },
    synthesis: { ...current.synthesis, ...(nextPatch.synthesis || {}) },
    library: { ...current.library, ...(nextPatch.library || {}) },
  })
}

function readProjectState(projectId) {
  if (!projectId) return sanitizeProjectState(DEFAULT_PROJECT_STATE)
  return sanitizeProjectState(readJson(STORAGE_KEYS.project(projectId)))
}

function writeProjectState(projectId, nextState) {
  if (!projectId) return false
  return writeJson(STORAGE_KEYS.project(projectId), {
    ...sanitizeProjectState(nextState),
    updatedAt: now(),
  })
}

function updateProjectState(projectId, patch) {
  const current = readProjectState(projectId)
  return writeProjectState(projectId, mergeProjectState(current, patch))
}

function migrateProject(projectId) {
  if (!projectId || safeGetItem(STORAGE_KEYS.project(projectId))) return
  const projectState = readProjectState(projectId)
  const lastFile = safeGetItem(LEGACY_KEYS.lastFile(projectId))
  const sessionId = safeGetItem(LEGACY_KEYS.session(projectId))
  const budgetPrefix = LEGACY_KEYS.budgetPrefix(projectId)
  const budgetsBySession = {}

  if (canUseStorage()) {
    try {
      for (let i = 0; i < window.localStorage.length; i += 1) {
        const key = window.localStorage.key(i)
        if (!key?.startsWith(budgetPrefix)) continue
        const sid = key.slice(budgetPrefix.length)
        const budget = sanitizeBudget(window.localStorage.getItem(key))
        if (isNonEmptyString(sid) && budget) budgetsBySession[sid] = budget
      }
    } catch {
      // Best-effort migration; unreadable legacy keys are left for cleanup.
    }
  }

  writeProjectState(projectId, {
    ...projectState,
    chat: {
      sessionId: isNonEmptyString(sessionId) ? sessionId : null,
      budgetsBySession,
    },
    synthesis: {
      ...projectState.synthesis,
      editorFile: isNonEmptyString(lastFile) ? lastFile : null,
      previewFile: isNonEmptyString(lastFile) ? lastFile : null,
    },
  })
}

function removeLegacyProjectKeys(projectId) {
  safeRemoveItem(LEGACY_KEYS.lastFile(projectId))
  safeRemoveItem(LEGACY_KEYS.session(projectId))
  const prefix = LEGACY_KEYS.budgetPrefix(projectId)
  removeKeysMatching(key => key.startsWith(prefix))
}

function removeKeysMatching(predicate) {
  if (!canUseStorage()) return
  const keys = []
  try {
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i)
      if (key && predicate(key)) keys.push(key)
    }
  } catch {
    return
  }
  keys.forEach(safeRemoveItem)
}

function pathMatchesOrIsChild(path, removedPath) {
  const normalizedPath = normalizeProjectPath(path)
  const normalizedRemovedPath = normalizeProjectPath(removedPath)
  return normalizedPath === normalizedRemovedPath || normalizedPath.startsWith(`${normalizedRemovedPath}/`)
}

function movedPath(path, srcPath, destPath) {
  const normalizedPath = normalizeProjectPath(path)
  const normalizedSrcPath = normalizeProjectPath(srcPath)
  const normalizedDestPath = normalizeProjectPath(destPath)
  if (normalizedPath === normalizedSrcPath) return normalizedDestPath
  if (normalizedPath.startsWith(`${normalizedSrcPath}/`)) return `${normalizedDestPath}${normalizedPath.slice(normalizedSrcPath.length)}`
  return null
}

function removePathsFromMap(map, removedPath) {
  const next = {}
  for (const [path, value] of Object.entries(map || {})) {
    if (!pathMatchesOrIsChild(path, removedPath)) next[path] = value
  }
  return next
}

function movePathsInMap(map, srcPath, destPath) {
  const next = {}
  for (const [path, value] of Object.entries(map || {})) {
    next[movedPath(path, srcPath, destPath) || path] = value
  }
  return trimObjectMap(next, MAX_FILE_STATE_ENTRIES)
}

function trimObjectMap(map, maxEntries) {
  const entries = Object.entries(map || {})
  if (entries.length <= maxEntries) return map || {}
  return Object.fromEntries(entries.slice(-maxEntries))
}

function setRecentMapEntry(map, path, value) {
  const normalizedPath = normalizeProjectPath(path)
  const next = { ...(map || {}) }
  delete next[normalizedPath]
  next[normalizedPath] = value
  return trimObjectMap(next, MAX_FILE_STATE_ENTRIES)
}

function pruneMapToExistingPaths(map, existingPaths) {
  const next = {}
  for (const [path, value] of Object.entries(map || {})) {
    const normalizedPath = normalizeProjectPath(path)
    if (existingPaths.has(normalizedPath)) next[normalizedPath] = value
  }
  return trimObjectMap(next, MAX_FILE_STATE_ENTRIES)
}

export const storage = {
  keys: STORAGE_KEYS,

  initialize() {
    const legacyTheme = safeGetItem(LEGACY_KEYS.theme)
    const legacyLanguage = safeGetItem(LEGACY_KEYS.language)
    const globalState = sanitizeGlobalState(readJson(STORAGE_KEYS.global))
    const nextGlobal = {
      ...globalState,
      theme: legacyTheme ? sanitizeTheme(legacyTheme) : globalState.theme,
      language: sanitizeLanguage(legacyLanguage) || globalState.language,
      updatedAt: now(),
    }
    writeJson(STORAGE_KEYS.global, nextGlobal)
    safeRemoveItem(LEGACY_KEYS.theme)
    safeRemoveItem(LEGACY_KEYS.language)
    safeRemoveItem(LEGACY_KEYS.zustand)
  },

  getGlobal() {
    return sanitizeGlobalState(readJson(STORAGE_KEYS.global))
  },

  setGlobal(patch) {
    const current = this.getGlobal()
    const nextPatch = typeof patch === 'function' ? patch(current) : patch
    if (!isPlainObject(nextPatch)) return false
    return writeJson(STORAGE_KEYS.global, sanitizeGlobalState({
      ...current,
      ...nextPatch,
      updatedAt: now(),
    }))
  },

  getProject(projectId) {
    migrateProject(projectId)
    return readProjectState(projectId)
  },

  setProject(projectId, patch) {
    migrateProject(projectId)
    return updateProjectState(projectId, patch)
  },

  touchProject(projectId) {
    this.setProject(projectId, { lastAccessedAt: now() })
  },

  removeProject(projectId) {
    if (!projectId) return
    safeRemoveItem(STORAGE_KEYS.project(projectId))
    removeLegacyProjectKeys(projectId)
  },

  cleanupProjects(existingProjectIds) {
    const existing = new Set((existingProjectIds || []).filter(Boolean))
    for (const projectId of existing) migrateProject(projectId)
    removeKeysMatching((key) => {
      if (!key.startsWith('sigma_project_')) return false
      const projectId = key.slice('sigma_project_'.length)
      return !existing.has(projectId)
    })
    removeKeysMatching((key) => {
      if (!key.startsWith('sigma_last_file_') &&
          !key.startsWith('sigma_auto_approve_') &&
          !key.startsWith('sigma_session_') &&
          !key.startsWith('sigma_budget_')) return false
      return true
    })
  },

  getLastFile(projectId) {
    return this.getProject(projectId).synthesis.editorFile
  },

  setLastFile(projectId, path) {
    const normalizedPath = normalizeProjectPath(path)
    if (!normalizedPath) return
    this.setProject(projectId, {
      synthesis: {
        editorFile: normalizedPath,
        previewFile: normalizedPath,
      },
    })
  },

  removeLastFile(projectId) {
    this.setProject(projectId, {
      synthesis: {
        editorFile: null,
        previewFile: null,
      },
    })
  },

  removeFileState(projectId, path) {
    const normalizedPath = normalizeProjectPath(path)
    if (!normalizedPath) return
    this.setProject(projectId, (current) => {
      const editorFile = pathMatchesOrIsChild(current.synthesis.editorFile || '', normalizedPath)
        ? null
        : current.synthesis.editorFile
      const previewFile = pathMatchesOrIsChild(current.synthesis.previewFile || '', normalizedPath)
        ? null
        : current.synthesis.previewFile
      return {
        synthesis: {
          editorFile,
          previewFile,
          editorCursorByFile: removePathsFromMap(current.synthesis.editorCursorByFile, normalizedPath),
          editorScrollRatioByFile: removePathsFromMap(current.synthesis.editorScrollRatioByFile, normalizedPath),
          previewScrollRatioByFile: removePathsFromMap(current.synthesis.previewScrollRatioByFile, normalizedPath),
        },
      }
    })
  },

  moveFileState(projectId, srcPath, destPath) {
    const normalizedSrcPath = normalizeProjectPath(srcPath)
    const normalizedDestPath = normalizeProjectPath(destPath)
    if (!normalizedSrcPath || !normalizedDestPath || normalizedSrcPath === normalizedDestPath) return
    this.setProject(projectId, (current) => {
      const editorFile = movedPath(current.synthesis.editorFile || '', normalizedSrcPath, normalizedDestPath) || current.synthesis.editorFile
      const previewFile = movedPath(current.synthesis.previewFile || '', normalizedSrcPath, normalizedDestPath) || current.synthesis.previewFile
      return {
        synthesis: {
          editorFile,
          previewFile,
          editorCursorByFile: movePathsInMap(current.synthesis.editorCursorByFile, normalizedSrcPath, normalizedDestPath),
          editorScrollRatioByFile: movePathsInMap(current.synthesis.editorScrollRatioByFile, normalizedSrcPath, normalizedDestPath),
          previewScrollRatioByFile: movePathsInMap(current.synthesis.previewScrollRatioByFile, normalizedSrcPath, normalizedDestPath),
        },
      }
    })
  },

  pruneFileState(projectId, existingPaths) {
    const existing = new Set((existingPaths || []).map(normalizeProjectPath).filter(Boolean))
    this.setProject(projectId, (current) => ({
      synthesis: {
        editorFile: existing.has(current.synthesis.editorFile) ? current.synthesis.editorFile : null,
        previewFile: existing.has(current.synthesis.previewFile) ? current.synthesis.previewFile : null,
        editorCursorByFile: pruneMapToExistingPaths(current.synthesis.editorCursorByFile, existing),
        editorScrollRatioByFile: pruneMapToExistingPaths(current.synthesis.editorScrollRatioByFile, existing),
        previewScrollRatioByFile: pruneMapToExistingPaths(current.synthesis.previewScrollRatioByFile, existing),
      },
    }))
  },

  getSession(projectId) {
    return this.getProject(projectId).chat.sessionId
  },

  setSession(projectId, sessionId) {
    this.setProject(projectId, {
      chat: { sessionId: isNonEmptyString(sessionId) ? sessionId : null },
    })
  },

  getBudget(projectId, sessionId) {
    if (!sessionId) return null
    return this.getProject(projectId).chat.budgetsBySession[sessionId] || null
  },

  setBudget(projectId, sessionId, value) {
    const budget = sanitizeBudget(value)
    if (!sessionId || !budget) return
    this.setProject(projectId, (current) => ({
      chat: {
        budgetsBySession: {
          ...current.chat.budgetsBySession,
          [sessionId]: budget,
        },
      },
    }))
  },

  removeBudget(projectId, sessionId) {
    if (!sessionId) return
    this.setProject(projectId, (current) => {
      const budgetsBySession = { ...current.chat.budgetsBySession }
      delete budgetsBySession[sessionId]
      return { chat: { budgetsBySession } }
    })
  },

  removeSession(projectId, sessionId) {
    if (!sessionId) return
    this.setProject(projectId, (current) => {
      const budgetsBySession = { ...current.chat.budgetsBySession }
      delete budgetsBySession[sessionId]
      return {
        chat: {
          sessionId: current.chat.sessionId === sessionId ? null : current.chat.sessionId,
          budgetsBySession,
        },
      }
    })
  },

  pruneSessionState(projectId, existingSessionIds) {
    const existing = new Set((existingSessionIds || []).filter(Boolean))
    this.setProject(projectId, (current) => {
      const budgetsBySession = {}
      for (const [sessionId, budget] of Object.entries(current.chat.budgetsBySession)) {
        if (existing.has(sessionId)) budgetsBySession[sessionId] = budget
      }
      return {
        chat: {
          sessionId: existing.has(current.chat.sessionId) ? current.chat.sessionId : null,
          budgetsBySession,
        },
      }
    })
  },

  getTheme() {
    return this.getGlobal().theme
  },

  setTheme(value) {
    this.setGlobal({ theme: sanitizeTheme(value) })
  },

  getLanguage() {
    const stored = this.getGlobal().language
    return stored || detectBrowserLanguage()
  },

  setLanguage(value) {
    const language = sanitizeLanguage(value)
    if (language) this.setGlobal({ language })
  },

  getEditorAppearance() {
    return this.getGlobal().editorAppearance
  },

  setEditorAppearance(patch) {
    if (!isPlainObject(patch)) return
    this.setGlobal((current) => ({
      editorAppearance: sanitizeEditorAppearance({ ...current.editorAppearance, ...patch }),
    }))
  },

  getWorkspace(projectId) {
    return this.getProject(projectId).workspace
  },

  setWorkspace(projectId, workspace) {
    this.setProject(projectId, { workspace })
  },

  getSynthesis(projectId) {
    return this.getProject(projectId).synthesis
  },

  setSynthesis(projectId, synthesis) {
    this.setProject(projectId, { synthesis })
  },

  setEditorScroll(projectId, path, ratio) {
    const normalizedPath = normalizeProjectPath(path)
    if (!normalizedPath) return
    this.setProject(projectId, (current) => ({
      synthesis: {
        editorScrollRatioByFile: setRecentMapEntry(current.synthesis.editorScrollRatioByFile, normalizedPath, ratio),
      },
    }))
  },

  setEditorCursor(projectId, path, cursor) {
    const normalizedPath = normalizeProjectPath(path)
    if (!normalizedPath || !isPlainObject(cursor)) return
    const line = Number(cursor.line)
    const column = Number(cursor.column)
    if (!Number.isSafeInteger(line) || line < 1 || !Number.isSafeInteger(column) || column < 0) return
    this.setProject(projectId, (current) => ({
      synthesis: {
        editorCursorByFile: setRecentMapEntry(current.synthesis.editorCursorByFile, normalizedPath, { line, column }),
      },
    }))
  },

  setPreviewScroll(projectId, path, ratio) {
    const normalizedPath = normalizeProjectPath(path)
    if (!normalizedPath) return
    this.setProject(projectId, (current) => ({
      synthesis: {
        previewScrollRatioByFile: setRecentMapEntry(current.synthesis.previewScrollRatioByFile, normalizedPath, ratio),
      },
    }))
  },

  getLibrary(projectId) {
    return this.getProject(projectId).library
  },

  setLibrary(projectId, library) {
    this.setProject(projectId, { library })
  },
}

/**
 * Detect the best matching supported language from navigator.language(s).
 * Exact match (e.g. 'zh-CN') is preferred, then a prefix match (e.g. 'en-US'
 * -> 'en'). A bare 'zh' without a region defaults to Simplified Chinese.
 * Returns 'en' when nothing matches.
 */
function detectBrowserLanguage() {
  if (typeof navigator === 'undefined') return 'en'
  const candidates = [navigator.language, ...(navigator.languages || [])]
  for (const lang of candidates) {
    if (!lang) continue
    const lower = lang.toLowerCase()
    const exact = SUPPORTED_LANGUAGE_CODES.find((s) => s.toLowerCase() === lower)
    if (exact) return exact
    const prefix = lower.split('-')[0]
    const byPrefix = SUPPORTED_LANGUAGE_CODES.find((s) => s.toLowerCase() === prefix)
    if (byPrefix) return byPrefix
    if (prefix === 'zh') return 'zh-CN'
  }
  return 'en'
}
