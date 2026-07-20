import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import { storage } from '../utils/storage'
import { permissionsAPI } from '../api'

const initialState = {
  projects: [],
  currentProject: null,
  currentFile: null,
  isTexFile: false,
  activeTab: 'synthesis',
  isRebuildingIndex: false,
  // Monotonic counter bumped every time the user clears browser data via the
  // Explore settings menu. BrowserVNC subscribes to it so the noVNC iframe is
  // remounted (and its WebSocket reconnected) after the backend restarts the
  // browser stack — otherwise the iframe keeps a stale WS to the dead
  // websockify and shows a black screen until the user refreshes the page.
  browserDataClearedAt: 0,
  leftTab: 'files',
  // Single source of truth for the preview panel. Title and rendered content
  // both derive from this descriptor — see Preview.jsx. Never mutate partial
  // fields; always go through setPreviewSource or bumpCompileVersion.
  previewSource: { kind: 'none', path: null, compileVersion: 0 },
  zoomLevel: 1,
  compiling: false,
  compileFailed: false,
  compileDiagnostics: [],
  compileLogs: '',
  showCreateProjectModal: false,
  showLogModal: false,
  hasUnsavedChanges: false,
  lastSavedTime: null,
  lastSavedType: null, // 'manual' or 'auto'

  // Annotations
  annotations: [],
  activeAnnotationId: null,
  isAnnotationsLoaded: false,
  pendingCitation: null,
  siGMADOProcessingAnnotationId: null,

  // AI & Chat
  isNotebookMode: false,

  // Jupyter Kernels
  activeKernels: [],
  kernelsLoading: false,

  // Task & Interaction
  pendingInteraction: null,           // {type: 'ask_user_question'|'submit_plan_for_approval', data}
  interactionDismissed: false,        // user closed the interaction modal without resolving (input stays gated)
  pendingPermission: null,            // {session_id, tool, tool_name, path, operation, content, description, diff_lines, diff_truncated}
  autoApproveSettings: {},            // { [toolType]: boolean } per-project, loaded on project switch
  taskList: [],                       // [{id, subject, status}]
  expandedTasks: false,               // auto-expand when tools modify tasks
  streamInteractionRequest: null,     // non-null triggers ChatPanel to start SSE for interaction response
  pendingAutoMessage: null,           // non-null triggers ChatPanel to auto-send a message { text }

  // File version counter — bumped when AI modifies files, triggers editor reload
  fileVersion: 0,

  // Notebook version counter — bumped when AI modifies notebooks, triggers iframe refresh
  notebookVersion: 0,

  // Skills version counter — bumped whenever skills are modified (toggle/delete/import/
  // edit-SKILL.md) so ChatPanel's /skill submenu refetches the enabled list
  skillsVersion: 0,

  // Markdown sync scroll
  mdSyncScroll: true,

  // File loading indicator
  isLoadingFile: false,

  // File hash baseline — MD5 of disk content at load/save time, for conflict detection
  fileHash: null,

  // Terminal panel visibility
  showTerminal: false,

  // Terminal state per project — in-memory only, survives EditorView unmount
  terminalStates: {},  // { [projectId]: { terminals: [{id, slot}], activeId: string|null } }
}

const actions = (set, get) => ({
  setProjects: (projects) => set({ projects }),
  setCurrentProject: (project) => set((state) => {
    if (state.currentProject?.id === project?.id) return { currentProject: project }
    // auto-approve settings are loaded from the backend by loadAutoApproveSettings()
    return { currentProject: project, autoApproveSettings: {} }
  }),
  addProject: (p) => set((s) => ({ projects: [...s.projects, p] })),
  removeProject: (id) => set((s) => {
    storage.removeProject(id)
    return { projects: s.projects.filter(p => p.id !== id) }
  }),
  setCurrentFile: (file) => set((state) => {
    if (state.currentFile === file) return {}
    return { currentFile: file, annotations: [], isAnnotationsLoaded: false, activeAnnotationId: null, fileHash: null }
  }),
  clearCurrentFile: () => set({
    currentFile: null,
    previewSource: { kind: 'none', path: null, compileVersion: 0 },
    isTexFile: false,
    isNotebookMode: false,
    annotations: [],
    isAnnotationsLoaded: false,
    activeAnnotationId: null,
    fileHash: null,
  }),
  setIsTexFile: (isTex) => set({ isTexFile: isTex }),
  setActiveTab: (tab) => set((state) => {
    if (state.currentProject?.id) storage.setWorkspace(state.currentProject.id, { activeTab: tab })
    return { activeTab: tab }
  }),
  setIsRebuildingIndex: (flag) => set({ isRebuildingIndex: flag }),
  bumpBrowserDataCleared: () => set((s) => ({ browserDataClearedAt: s.browserDataClearedAt + 1 })),
  setLeftTab: (tab) => set((state) => {
    if (state.currentProject?.id) storage.setSynthesis(state.currentProject.id, { leftTab: tab })
    return { leftTab: tab }
  }),
  setPreviewSource: (updater) => set((state) => {
    const prev = state.previewSource
    const next = typeof updater === 'function' ? updater(prev) : updater
    // Always allocate a fresh object so Preview's identity-keyed effect fires
    // even when only one field changed (covers same-path md re-select).
    if (!next || next === prev) return {}
    return { previewSource: { ...next } }
  }),
  bumpCompileVersion: () => set((state) => {
    const ps = state.previewSource
    if (ps.kind !== 'pdf-compiled') return {} // never touch non-compiled previews
    return { previewSource: { ...ps, compileVersion: ps.compileVersion + 1 } }
  }),
  setZoomLevel: (level) => set({ zoomLevel: level }),
  setCompiling: (compiling) => set({ compiling }),
  setCompileFailed: (failed) => set({ compileFailed: failed }),
  setCompileDiagnostics: (diagnostics) => set({ compileDiagnostics: diagnostics }),
  setCompileLogs: (logs) => set({ compileLogs: logs }),
  setShowCreateProjectModal: (show) => set({ showCreateProjectModal: show }),
  setShowLogModal: (show) => set({ showLogModal: show }),
  setHasUnsavedChanges: (val) => set({ hasUnsavedChanges: val }),
  markSaved: (type = 'manual') => set({ hasUnsavedChanges: false, lastSavedTime: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), lastSavedType: type }),

  // Annotation Actions
  setAnnotations: (annotations) => set({ annotations, isAnnotationsLoaded: true }),
  setActiveAnnotationId: (id) => set({ activeAnnotationId: id }),
  addAnnotation: (anno) => set(s => ({ annotations: [...s.annotations, anno], activeAnnotationId: anno.id, isAnnotationsLoaded: true })),
  updateAnnotation: (id, updates) => set(s => ({
    annotations: s.annotations.map(a => a.id === id ? { ...a, ...updates } : a),
    isAnnotationsLoaded: true
  })),
  deleteAnnotation: (id) => set(s => ({
    annotations: s.annotations.filter(a => a.id !== id),
    activeAnnotationId: s.activeAnnotationId === id ? null : s.activeAnnotationId,
    isAnnotationsLoaded: true
  })),

  setPendingCitation: (cite) => set({ pendingCitation: cite }),
  clearCitation: () => set({ pendingCitation: null }),
  setSiGMADOProcessingAnnotationId: (id) => set({ siGMADOProcessingAnnotationId: id }),

  // Chat
  setIsNotebookMode: (val) => set({ isNotebookMode: val }),

  // Jupyter Kernel Actions
  setActiveKernels: (kernels) => {
    if (typeof kernels === 'function') {
      set((prev) => {
        const currentArr = prev.activeKernels || []
        return { activeKernels: kernels(currentArr) }
      })
    } else if (kernels !== null && kernels !== undefined) {
      set({ activeKernels: kernels })
    }
  },
  setKernelsLoading: (val) => set({ kernelsLoading: val }),

  // Task & Interaction Actions
  setPendingInteraction: (val) => set({ pendingInteraction: val, interactionDismissed: false }),
  clearPendingInteraction: () => set({ pendingInteraction: null, interactionDismissed: false, expandedTasks: false }),
  setInteractionDismissed: (v) => set({ interactionDismissed: v }),
  setPendingPermission: (val) => set({ pendingPermission: val }),
  clearPendingPermission: () => set({ pendingPermission: null }),
  setAutoApproveType: (toolType, enabled) => set((s) => {
    // Local state update only. Callers must persist to the backend first and
    // call this only after the PUT succeeds (see ChatPanel's approvingCategory).
    const settings = { ...s.autoApproveSettings, [toolType]: enabled }
    return { autoApproveSettings: settings }
  }),
  loadAutoApproveSettings: async (projectId) => {
    // Fetch the four-category flags from the backend. Called after project switch.
    try {
      const data = await permissionsAPI.getAutoApprove(projectId)
      set({ autoApproveSettings: data || {} })
    } catch (e) {
      // Keep the empty default on failure — toggles show all-off, safest.
    }
  },
  setTaskList: (tasks) => set({ taskList: tasks }),
  setExpandedTasks: (val) => set({ expandedTasks: val }),
  setStreamInteractionRequest: (req) => set({ streamInteractionRequest: req }),
  setPendingAutoMessage: (msg) => set({ pendingAutoMessage: msg }),
  incrementFileVersion: () => set(s => ({ fileVersion: s.fileVersion + 1 })),
  incrementNotebookVersion: () => set(s => ({ notebookVersion: s.notebookVersion + 1 })),
  bumpSkillsVersion: () => set(s => ({ skillsVersion: s.skillsVersion + 1 })),
  setMdSyncScroll: (val) => set({ mdSyncScroll: val }),
  toggleMdSyncScroll: () => set(s => ({ mdSyncScroll: !s.mdSyncScroll })),
  setIsLoadingFile: (val) => set({ isLoadingFile: val }),
  setFileHash: (hash) => set({ fileHash: hash }),
  toggleTerminal: () => set(s => ({ showTerminal: !s.showTerminal })),
  setShowTerminal: (val) => set({ showTerminal: val }),
  setTerminalState: (projectId, state) => set(s => ({
    terminalStates: { ...s.terminalStates, [projectId]: state }
  })),
  clearTerminalState: (projectId) => set(s => {
    const next = { ...s.terminalStates }
    delete next[projectId]
    return { terminalStates: next }
  }),
})

export const useStore = create(
  devtools(
    (set, get) => ({ ...initialState, ...actions(set, get) }),
    { name: 'SiGMA Store' }
  )
)
