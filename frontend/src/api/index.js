/**
 * SiGMA API Client - Unified Response Format
 *
 * All backend non-streaming endpoints now return:
 *   { request_id: str, success: bool, error: str|null, data: any }
 *
 * The request() function auto-unwraps the `data` field so component
 * code continues to work without changes.
 */

import { createSSEStreamParser } from '../utils/sse'
import i18n from '../i18n'

const API_BASE_URL = '/api/v1'

/** Unwrap the backend unified response format: { success, request_id, data }. */
function unwrapResponse(json) {
  if (json && typeof json === 'object' && 'success' in json && 'request_id' in json) {
    if (!json.success) throw new Error(json.error || i18n.t('api.requestFailed'))
    return json.data
  }
  return json
}

async function request(endpoint, options = {}, isText = false) {
  const url = `${API_BASE_URL}${endpoint}`
  // Let the browser set multipart/form-data boundary for FormData uploads.
  const isFormData = options.body instanceof FormData
  const defaultOptions = isFormData ? {} : { headers: { 'Content-Type': 'application/json' } }
  const config = { ...defaultOptions, ...options }

  try {
    const response = await fetch(url, config)
    const contentType = response.headers.get('content-type')

    if (!response.ok) {
      // A 401 on a non-auth endpoint means the session cookie is gone/expired
      // — bounce to the login screen. Auth endpoints manage their own 401 UX.
      if (response.status === 401 && !endpoint.startsWith('/auth/')) {
        if (window.location.pathname !== '/login') {
          window.location.href = '/login'
        }
        throw new Error(i18n.t('auth.loginRequired'))
      }
      let data = {}
      try {
        data = await response.json()
      } catch {
        // Response might be empty or not JSON
      }

      let errorMsg
      // Unified format error: { success: false, error: "message" }
      if (data.success === false && data.error) {
        errorMsg = typeof data.error === 'string' ? data.error : data.error.message || JSON.stringify(data.error)
      }
      // FastAPI 422 validation errors
      else if (Array.isArray(data.detail)) {
        errorMsg = data.detail.map(d => d.msg || d).join('; ')
      }
      // Nested error detail
      else if (typeof data.detail === 'string') {
        errorMsg = data.detail
      }
      else if (data.detail?.error?.message) {
        errorMsg = data.detail.error.message
      }
      else if (data.detail?.message) {
        errorMsg = data.detail.message
      }
      else if (data.message) {
        errorMsg = data.message
      }

      if (!errorMsg) {
        errorMsg = i18n.t('api.requestFailedStatus', { status: response.status })
      }
      throw new Error(errorMsg)
    }

    // 204 No Content
    if (response.status === 204) {
      return { success: true }
    }

    // Smart parsing based on content-type or explicit flag
    if (isText || (contentType && contentType.includes('text/'))) {
      return await response.text()
    }

    if (contentType && contentType.includes('application/json')) {
      const json = await response.json()

      return unwrapResponse(json)
    }

    return response
  } catch (error) {
    console.error(`API Error [${endpoint}]:`, error.message)
    throw error
  }
}

// ---------------------------------------------------------------------------
// Project API
// ---------------------------------------------------------------------------
export const projectsAPI = {
  list: () => request('/projects'),
  get: (id) => request(`/projects/${id}`),
  create: (data) => request('/projects', { method: 'POST', body: JSON.stringify(data) }),
  import: (formData) => request('/projects/import', { method: 'POST', body: formData }),
  listUnregistered: () => request('/projects/unregistered'),
  register: (data) => request('/projects/register', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => request(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => request(`/projects/${id}`, { method: 'DELETE' }),
  resetDatabase: (id) => request(`/projects/${id}/database`, { method: 'DELETE' }),
  listTemplates: () => request('/projects/templates'),
  getConfig: (id) => request(`/projects/${id}/config`),
  updateConfig: (id, data) => request(`/projects/${id}/config`, { method: 'PATCH', body: JSON.stringify(data) }),
  export: (id) => fetch(`${API_BASE_URL}/projects/${id}/export`).then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || d.detail || i18n.t('api.exportFailed')) })
      return r.blob()
  }),
}

export const systemAPI = {
  getSettings: () => request('/system/settings'),
  updateSettings: (payload) => request('/system/settings', { method: 'PUT', body: JSON.stringify(payload) }),
  renderSettingsYaml: (config) => request('/system/settings/yaml', { method: 'POST', body: JSON.stringify({ config }) }),
  validateSettingsYaml: (content) => request('/system/settings/validate-yaml', { method: 'POST', body: JSON.stringify({ content }) }),
  listProviders: () => request('/system/litellm/providers'),
  listModels: ({ provider = '', baseUrl = '', apiKey = '' }) =>
    request('/system/litellm/models', {
      method: 'POST',
      body: JSON.stringify({ provider, base_url: baseUrl, api_key: apiKey }),
    }),
  getModelContext: ({ provider = '', model = '' }) => {
    const qs = new URLSearchParams({ provider, model })
    return request(`/system/litellm/context?${qs.toString()}`)
  },
  /** Check settings config and model connectivity — returns ReadableStream for SSE */
  checkSettings: (payload, signal) => fetch(`${API_BASE_URL}/system/settings/check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.detail || i18n.t('api.checkFailed')) })
    return r.body
  }),
  getTeXStatus: () => request('/system/tex/status'),
  runTeXOperation: (payload, signal) => fetch(`${API_BASE_URL}/system/tex/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.detail || i18n.t('api.requestFailed')) })
    return r.body
  }),
  restart: () => request('/system/restart', { method: 'POST' }),
}

// ---------------------------------------------------------------------------
// Auth API — access-password gate
// ---------------------------------------------------------------------------
export const authAPI = {
  /** Whether an access password is currently configured. */
  status: () => request('/auth/status'),
  /** Verify the password; the backend sets the session cookie on success. */
  login: (password) => request('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  }),
  /** Clear the session cookie. */
  logout: () => request('/auth/logout', { method: 'POST' }),
  /** Set, change, or clear the access password (empty string clears it). */
  setPassword: (newPassword) => request('/auth/password', {
    method: 'POST',
    body: JSON.stringify({ new_password: newPassword }),
  }),
}

// ---------------------------------------------------------------------------
// Files API
// ---------------------------------------------------------------------------
export const filesAPI = {
  tree: (projectId) => request(`/files/${projectId}/tree`),
  children: (projectId, path = '') => request(`/files/${projectId}/children?path=${encodeURIComponent(path)}`),
  read: async (projectId, path) => {
    const res = await fetch(`${API_BASE_URL}/files/${projectId}/content?path=${encodeURIComponent(path)}`, { headers: { 'Content-Type': 'application/json' } })
    if (!res.ok) {
      let data = {}
      try { data = await res.json() } catch { /* */ }
      const err = new Error(data.error || data.detail || i18n.t('api.readFailed'))
      err.status = res.status
      throw err
    }
    const content = await res.text()
    const hash = res.headers.get('X-Content-Hash')
    return { content, hash }
  },
  write: (projectId, path, content, { force = false, hash = null } = {}) => request(`/files/${projectId}/content`, { method: 'POST', body: JSON.stringify({ path, content, force, hash }) }),
  create: (projectId, path, isDir = false) => request(`/files/${projectId}/create`, { method: 'POST', body: JSON.stringify({ path, type: isDir ? 'directory' : 'file' }) }),
  delete: (projectId, path) => request(`/files/${projectId}?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),
  move: (projectId, source, destination) => request(`/files/${projectId}/move`, { method: 'POST', body: JSON.stringify({ source, destination }) }),
  rename: (projectId, path, newName) => request(`/files/${projectId}/rename`, { method: 'POST', body: JSON.stringify({ path, new_name: newName }) }),
  download: (projectId, path) => fetch(`${API_BASE_URL}/files/${projectId}/download?path=${encodeURIComponent(path)}`).then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || d.detail || i18n.t('api.downloadFailed')) })
      return r.blob()
  }),
  upload: (projectId, formData) => fetch(`${API_BASE_URL}/files/${projectId}/upload`, { method: 'POST', body: formData }).then(async r => {
      const d = await r.json()
      if (!r.ok) throw new Error(d.error || d.detail || i18n.t('api.uploadFailed'))
      return unwrapResponse(d)
  }),
  extract: (projectId, path, overwrite = false, skipConflicts = false) =>
    request(`/files/${projectId}/extract`, { method: 'POST', body: JSON.stringify({ path, overwrite, skip_conflicts: skipConflicts }) }),
  batchDownload: (projectId, paths) =>
    fetch(`${API_BASE_URL}/files/${projectId}/batch-download`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    }).then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || d.detail || i18n.t('api.downloadFailed')) })
      return r.blob()
    }),
  // Annotation Sync
  loadAnnotations: (projectId, path) => request(`/annotations/${projectId}?path=${encodeURIComponent(path)}`),
  createAnnotation: (projectId, path, data) => request(`/annotations/${projectId}/create?path=${encodeURIComponent(path)}`, { method: 'POST', body: JSON.stringify(data) }),
  saveAnnotations: (projectId, path, annotations) => request(`/annotations/${projectId}?path=${encodeURIComponent(path)}`, { method: 'POST', body: JSON.stringify({ annotations }) }),
  /** Append a user reply to an annotation (preserves existing messages) */
  replyAnnotation: (projectId, annotationId, content) => request(`/annotations/${projectId}/reply`, { method: 'POST', body: JSON.stringify({ annotationId, content }) }),
  /** Stream an AI reply for an annotation — returns ReadableStream for SSE parsing */
  streamAnnotationReply: (projectId, filePath, annotationId, signal) => fetch(`${API_BASE_URL}/annotations/stream/${projectId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filePath, annotationId }),
    signal,
  }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.error || i18n.t('api.annotationReplyFailed')) })
    return r.body
  }),
  getActiveAnnotationReply: (projectId, annotationId) =>
    request(`/annotations/active/${projectId}?annotation_id=${encodeURIComponent(annotationId)}`),
  resumeAnnotationReplyStream: (taskId, signal) => fetch(`${API_BASE_URL}/annotations/stream/${encodeURIComponent(taskId)}`, { signal }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.error || i18n.t('api.resumeFailed')) })
    return r.body
  }),
  cancelAnnotationReply: (projectId, taskId) =>
    request(`/annotations/cancel/${projectId}/${encodeURIComponent(taskId)}`, { method: 'POST' }),
}

// ---------------------------------------------------------------------------
// Compile API
// ---------------------------------------------------------------------------
export const compileAPI = {
  compile: (projectId, opts = {}) => request(`/compile/${projectId}`, { method: 'POST', body: JSON.stringify(opts) }),
  synctex: (projectId, data) => request(`/compile/${projectId}/synctex`, { method: 'POST', body: JSON.stringify(data) }),
  /** Fetch compiled PDF as a Blob (for preview display). */
  getPDF: (projectId, filename) => {
    const qs = filename ? `?filename=${encodeURIComponent(filename)}` : ''
    return fetch(`${API_BASE_URL}/compile/${encodeURIComponent(projectId)}/pdf${qs}`, { cache: 'no-store' }).then(async r => {
      if (!r.ok) throw new Error(i18n.t('api.pdfNotAvailable'))
      return await r.blob()
    })
  },
}

// ---------------------------------------------------------------------------
// Chat API — streaming + task management
// ---------------------------------------------------------------------------
export const chatAPI = {
  uploadAttachment: (projectId, file) => {
    const formData = new FormData()
    formData.append('file', file)
    return fetch(`${API_BASE_URL}/chat/attachments/${projectId}`, {
      method: 'POST',
      body: formData,
    }).then(async r => {
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail?.message || data.error || i18n.t('api.uploadFailed'))
      return unwrapResponse(data)
    })
  },

  /** Send a message and get an SSE stream back */
  stream: (projectId, data, signal) => fetch(`${API_BASE_URL}/chat/stream/${projectId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    signal,
  }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.detail?.message || d.error || i18n.t('api.chatFailed')) })
    return r.body  // readable stream for SSE
  }),

  /** Check for an active (running / stale) background task for a specific session */
  getActive: (projectId, sessionId) =>
    request(`/chat/active/${projectId}?session_id=${encodeURIComponent(sessionId || '')}`),

  /** Current estimated LLM context stats for a session */
  contextStats: (projectId, sessionId) =>
    request(`/chat/context-stats/${projectId}?session_id=${encodeURIComponent(sessionId || '')}`),

  /** Reconnect to an existing task's SSE stream */
  resumeStream: (taskId, signal) => fetch(`${API_BASE_URL}/chat/stream/${taskId}`, { signal }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.error || i18n.t('api.resumeFailed')) })
    return r.body  // readable stream for SSE
  }),

  /** Load chat history for a session */
  history: (projectId, sessionId, params = {}) => {
    const query = new URLSearchParams()
    if (sessionId) query.set('session_id', sessionId)
    if (params.limit) query.set('limit', String(params.limit))
    if (params.beforeSeq != null) query.set('before_seq', String(params.beforeSeq))
    const qs = query.toString()
    return request(`/chat/history/${projectId}${qs ? '?' + qs : ''}`)
  },

  /** Edit a user message and stream the replacement reply */
  editMessage: (projectId, sessionId, data, signal) => fetch(`${API_BASE_URL}/chat/edit/${projectId}/${sessionId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    signal,
  }).then(r => {
    if (!r.ok) return r.json().then(d => { throw new Error(d.detail?.message || d.error || i18n.t('api.editFailed')) })
    return r.body
  }),

  /** Clear chat history for a session */
  clearHistory: (projectId, sessionId) => {
    const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
    return request(`/chat/history/${projectId}${qs}`, { method: 'DELETE' })
  },

  /** List sessions for a project */
  listSessions: (projectId, params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/chat/sessions/${projectId}${qs ? '?' + qs : ''}`)
  },

  /** Create a new session */
  createSession: (projectId) =>
    request(`/chat/sessions/${projectId}`, { method: 'POST' }),

  /** Update session (title, is_archived) */
  updateSession: (projectId, sessionId, data) =>
    request(`/chat/sessions/${projectId}/${sessionId}`, { method: 'PATCH', body: JSON.stringify(data) }),

  /** Delete a session */
  deleteSession: (projectId, sessionId) =>
    request(`/chat/sessions/${projectId}/${sessionId}`, { method: 'DELETE' }),

  /** Generate a title for a session using AI */
  generateTitle: (projectId, sessionId) =>
    request(`/chat/sessions/${projectId}/${sessionId}/generate-title`, { method: 'POST' }),

  /** Inject a completed skill_load tool turn into the session (no LLM call) */
  loadSkill: (projectId, sessionId, skillId) =>
    request(`/chat/sessions/${projectId}/${sessionId}/load-skill`, { method: 'POST', body: JSON.stringify({ skill_id: skillId }) }),

  /** Get messages for a specific session (archive preview) */
  getSessionMessages: (projectId, sessionId) =>
    request(`/chat/sessions/${projectId}/${sessionId}/messages`),

  /** Cancel a running LLM task */
  cancel: (projectId, taskId) =>
    request(`/chat/cancel/${projectId}/${taskId}`, { method: 'POST' }),

  /** Get tasks for a session */
  getTasks: (projectId, sessionId) =>
    request(`/chat/tasks/${projectId}?session_id=${encodeURIComponent(sessionId || '')}`),
}

// ---------------------------------------------------------------------------
// Git API
// ---------------------------------------------------------------------------
export const gitsAPI = {
  init: (projectId) => request(`/git/${projectId}/init`, { method: 'POST' }),
  log: (projectId, limit = 50, offset = 0, before = null) => {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (before) qs.set('before', before)
    return request(`/git/${projectId}/log?${qs.toString()}`)
  },
  diff: (projectId, params) => {
    const qs = new URLSearchParams(params);
    return request(`/git/${projectId}/diff?${qs.toString()}`);
  },
  fileHistory: (projectId, path) => request(`/git/${projectId}/history?path=${encodeURIComponent(path)}`),
  commitFiles: (projectId, data) => {
    const params = new URLSearchParams({ commit: data.commit });
    if (data.parent_commit) params.set('parent_commit', data.parent_commit);
    return request(`/git/${projectId}/commit-files?${params.toString()}`);
  },
  getBlob: (projectId, data) => {
    const params = new URLSearchParams({ path: data.path, commit: data.commit });
    return request(`/git/${projectId}/blob?${params.toString()}`);
  },
  snapshot: (projectId, commit) => {
    return `${API_BASE_URL}/git/${projectId}/snapshot?commit=${encodeURIComponent(commit)}`;
  },
  downloadSnapshot: (projectId, commitHash) => fetch(
    `${API_BASE_URL}/git/${projectId}/snapshot?commit=${encodeURIComponent(commitHash)}`
  ).then(async r => {
    if (!r.ok) throw new Error(await r.text())
    return await r.blob()
  }),
}

// ---------------------------------------------------------------------------
// Notebook API — backed by an embedded Jupyter server.
// ---------------------------------------------------------------------------
export const notebooksAPI = {
  read: (projectId, path) => request(`/notebooks/${projectId}?path=${encodeURIComponent(path)}`),
  write: (projectId, path, notebook) => request(`/notebooks/${projectId}`, { method: 'POST', body: JSON.stringify({ path, notebook }) }),
  create: (projectId, path) => request(`/notebooks/${projectId}/create`, { method: 'POST', body: JSON.stringify({ path }) }),
  getUrl: (projectId, path) => request(`/notebooks/${projectId}/url?path=${encodeURIComponent(path)}`),
  listKernels: () => request('/notebooks/kernels'),
  killKernel: (kernelId) => request(`/notebooks/kernels/${kernelId}`, { method: 'DELETE' })
}

// ---------------------------------------------------------------------------
// Library API — document management with RAG search
// ---------------------------------------------------------------------------
export const libraryAPI = {
  list: (projectId, params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/library/${projectId}/documents${qs ? '?' + qs : ''}`)
  },
  get: (projectId, docId, params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/library/${projectId}/documents/${docId}${qs ? '?' + qs : ''}`)
  },
  getAncestors: (projectId, docId) => request(`/library/${projectId}/documents/${docId}/ancestors`),
  create: (projectId, data) => request(`/library/${projectId}/documents`, { method: 'POST', body: JSON.stringify(data) }),
  update: (projectId, docId, data) => request(`/library/${projectId}/documents/${docId}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (projectId, docId) => request(`/library/${projectId}/documents/${docId}`, { method: 'DELETE' }),
  statusSummary: (projectId) => request(`/library/${projectId}/status-summary`),
  search: (projectId, query, options = {}) => request(`/library/${projectId}/search`, { method: 'POST', body: JSON.stringify({ query, ...options }) }),
  ragSearch: (projectId, query, options = {}) => request(`/library/${projectId}/rag-search`, { method: 'POST', body: JSON.stringify({ query, ...options }) }),
  rebuildIndex: (projectId) => request(`/library/${projectId}/rebuild-index`, { method: 'POST' }),
  extractFields: (projectId, docId) => request(`/library/${projectId}/extract-fields/${docId}`, { method: 'POST' }),
  reprocess: (projectId, docId) => request(`/library/${projectId}/reprocess/${docId}`, { method: 'POST' }),
  reprocessAll: (projectId) => request(`/library/${projectId}/reprocess-all`, { method: 'POST' }),
  getProcessingLog: (projectId, docId) => request(`/library/${projectId}/processing-log/${docId}`),
  createFolder: (projectId, data) => request(`/library/${projectId}/folders`, { method: 'POST', body: JSON.stringify(data) }),
  moveItems: (projectId, data) => request(`/library/${projectId}/move`, { method: 'POST', body: JSON.stringify(data) }),
  batchDelete: (projectId, ids) => request(`/library/${projectId}/batch-delete`, { method: 'POST', body: JSON.stringify({ ids }) }),
  uploadFiles: (projectId, files, folderId) => {
    const formData = new FormData();
    files.forEach(item => {
      const file = item?.file || item
      const relativePath = item?.relativePath || file.webkitRelativePath || file.name
      formData.append('files', file, file.name)
      formData.append('relative_paths', relativePath)
    });
    if (folderId) formData.append('folder_id', folderId);
    return fetch(`${API_BASE_URL}/library/${projectId}/upload`, { method: 'POST', body: formData }).then(async r => {
      const d = await r.json()
      if (!r.ok) throw new Error(d.error || d.detail || i18n.t('api.uploadFailed'))
      return unwrapResponse(d)
    });
  },
}

// ---------------------------------------------------------------------------
// Browser API — Chrome + noVNC management
// ---------------------------------------------------------------------------
export const browserAPI = {
  getStatus: (projectId) => request(`/browser/${projectId}/status`),
  start: (projectId) => request(`/browser/${projectId}/start`, { method: 'POST' }),
  stop: (projectId) => request(`/browser/${projectId}/stop`, { method: 'POST' }),
}

// ---------------------------------------------------------------------------
// Terminal API — session discovery
// ---------------------------------------------------------------------------
export const terminalAPI = {
  listSessions: (projectId) => request(`/terminal/${projectId}/sessions`),
}

// ---------------------------------------------------------------------------
// Skills API — global skill management
// ---------------------------------------------------------------------------
export const skillsAPI = {
  list: () => request('/skills'),
  toggle: (skillId) => request(`/skills/${skillId}/toggle`, { method: 'PATCH' }),
  delete: (skillId) => request(`/skills/${skillId}`, { method: 'DELETE' }),
  listFiles: (skillId) => request(`/skills/${skillId}/files`),
  readFile: (skillId, filePath) =>
    request(`/skills/${skillId}/files/content?file_path=${encodeURIComponent(filePath)}`),
  writeFile: (skillId, filePath, content, hash) =>
    request(`/skills/${skillId}/files/content`, {
      method: 'PUT',
      body: JSON.stringify({ file_path: filePath, content, hash }),
    }),
  createFile: (skillId, path, type) =>
    request(`/skills/${skillId}/files/create`, {
      method: 'POST',
      body: JSON.stringify({ path, type }),
    }),
  renameFile: (skillId, path, newName) =>
    request(`/skills/${skillId}/files/rename`, {
      method: 'POST',
      body: JSON.stringify({ path, new_name: newName }),
    }),
  deleteFile: (skillId, filePath) =>
    request(`/skills/${skillId}/files?file_path=${encodeURIComponent(filePath)}`, {
      method: 'DELETE',
    }),
  importZip: (file) => {
    const formData = new FormData()
    formData.append('file', file)
    return fetch(`${API_BASE_URL}/skills/import/zip`, {
      method: 'POST',
      body: formData,
    }).then(async r => {
      const d = await r.json()
      if (!r.ok) throw new Error(d.error || d.detail || i18n.t('api.importFailed'))
      return unwrapResponse(d)
    })
  },
  importGit: (url) =>
    request('/skills/import/git', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
}

// ---------------------------------------------------------------------------
// Permission API — worker write-approval relay
// ---------------------------------------------------------------------------
export const permissionsAPI = {
  respond: (projectId, taskId, body) =>
    request(`/permissions/${projectId}/${taskId}/respond`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

// ---------------------------------------------------------------------------
// Download helpers — blob fetch with unified error handling
// ---------------------------------------------------------------------------
export function fetchBlob(url) {
  return fetch(url).then(r => {
    if (!r.ok) throw new Error(`${i18n.t('api.downloadFailed')}: ${r.statusText}`)
    return r.blob()
  })
}

export default { projects: projectsAPI, files: filesAPI, compile: compileAPI, git: gitsAPI, notebooks: notebooksAPI, library: libraryAPI, browser: browserAPI, chat: chatAPI, terminal: terminalAPI, skills: skillsAPI, permissions: permissionsAPI }
