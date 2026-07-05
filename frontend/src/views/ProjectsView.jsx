/**
 * ProjectsView — project listing page (home screen).
 * Extracted from App.jsx (originally lines 50-458).
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useStore } from '../store/useStore'
import { projectsAPI, systemAPI } from '../api'
import { storage } from '../utils/storage'
import { toastError } from '../components/Toast'
import { CreateProjectModal, ConfirmModal, UploadProjectModal } from '../components/Modal'
import SkillPanel from '../components/SkillPanel'
import SystemSettingsModal from '../components/SystemSettingsModal'
import TeXManagerPanel from '../components/TeXManagerPanel'
import BackendErrorOverlay from '../components/BackendErrorOverlay'
import LanguageSelector from '../components/LanguageSelector'
import { Spinner } from '../components/ui'
import Toggle from '../components/Toggle'
import { useTheme } from '../hooks/useTheme'
import { Folder, Plus, Search, Trash2, Download, Pencil, Clock, Check, X, Wrench, Settings, Sun, Moon, ChevronDown, Globe, FileText, FilePlus, UploadCloud } from 'lucide-react'

function formatDate(dateStr, t) {
  if (!dateStr) return t('time.never')
  const d = new Date(dateStr)
  const now = new Date()
  const diff = now - d
  const minutes = Math.floor(diff / 60000)
  const hours = Math.floor(minutes / 60)
  const days = Math.floor(hours / 24)
  if (minutes < 1) return t('time.justNow')
  if (minutes < 60) return t('time.minutesAgo', { count: minutes })
  if (hours < 24) return t('time.hoursAgo', { count: hours })
  if (days < 7) return t('time.daysAgo', { count: days })
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined })
}

/** Download a project ZIP and trigger browser save dialog. */
async function downloadProjectZip(project, t) {
  try {
    const blob = await projectsAPI.export(project.id)
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${project.name}.zip`
    a.click()
    URL.revokeObjectURL(a.href)
  } catch {
    toastError(t('projects.toast.downloadFailed'))
  }
}
export default function ProjectsView() {
  const navigate = useNavigate()
  const projects = useStore(s => s.projects)
  const setProjects = useStore(s => s.setProjects)
  const addProject = useStore(s => s.addProject)
  const removeProject = useStore(s => s.removeProject)
  const setShowCreateProjectModal = useStore(s => s.setShowCreateProjectModal)
  const showCreateProjectModal = useStore(s => s.showCreateProjectModal)
  const [searchQuery, setSearchQuery] = useState('')
  const [showSkillPanel, setShowSkillPanel] = useState(false)
  const [showSystemSettings, setShowSystemSettings] = useState(false)
  const [forceSettings, setForceSettings] = useState(false)  // true when critical models are unset → panel is mandatory
  const [showTeXManager, setShowTeXManager] = useState(false)
  const [showSettingsMenu, setShowSettingsMenu] = useState(false)
  const [showNewProjectMenuHeader, setShowNewProjectMenuHeader] = useState(false)
  const [showNewProjectMenuEmpty, setShowNewProjectMenuEmpty] = useState(false)
  const [showUploadProjectModal, setShowUploadProjectModal] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState(null) // { id, name } or null
  const settingsMenuRef = useRef(null)
  const newProjectMenuHeaderRef = useRef(null)
  const newProjectMenuEmptyRef = useRef(null)
  const { isDark, toggleTheme } = useTheme()
  const { t } = useTranslation()

  const [editingField, setEditingField] = useState({ projectId: null, field: null })
  const [nameEditValue, setNameEditValue] = useState('')
  const [descEditValue, setDescEditValue] = useState('')
  const [exportingId, setExportingId] = useState(null)
  const [backendError, setBackendError] = useState(false)
  const nameEditRef = useRef(null)
  const descEditRef = useRef(null)
  // Mirror the edit values into refs so finishEditing (called from the
  // click-outside listener bound once per editingField change) reads the latest
  // value instead of the one captured when editing started.
  const nameEditValueRef = useRef('')
  const descEditValueRef = useRef('')
  useEffect(() => { nameEditValueRef.current = nameEditValue }, [nameEditValue])
  useEffect(() => { descEditValueRef.current = descEditValue }, [descEditValue])
  // Mirror forceSettings into a ref so loadProjects (captured once by the
  // mount effect) reads the latest value instead of the initial false.
  const forceSettingsRef = useRef(false)
  useEffect(() => { forceSettingsRef.current = forceSettings }, [forceSettings])

  const loadProjects = async () => {
    try {
      const data = await projectsAPI.list()
      const list = Array.isArray(data) ? data : (data.projects || [])
      setProjects(list)
      storage.cleanupProjects(list.map(p => p.id))
      setBackendError(false)
      // Backend is reachable — verify that critical models are configured.
      // If supervisor / RA / embedding are unset the platform can't function,
      // so force-open the system settings panel.
      if (!forceSettingsRef.current) {
        checkCriticalModels()
      }
    } catch (e) {
      // Show the backend error overlay instead of a transient toast: the
      // most common cause is the container still starting up (loading AI
      // models takes 30-60 s) or the backend having crashed. Either way
      // it is a state, not a one-off error, so the user deserves a clear
      // full-screen mask with retry / log commands.
      setBackendError(true)
    }
  }
  useEffect(() => { loadProjects() }, [])

  // While the backend appears to be down, auto-retry every 5 s. The overlay
  // calls loadProjects via onRetry too; this effect covers the case where
  // the user just leaves the page alone.
  useEffect(() => {
    if (!backendError) return
    const id = setInterval(() => { loadProjects() }, 5000)
    return () => clearInterval(id)
  }, [backendError])

  // Check whether supervisor / RA / embedding models are configured.
  // If any is empty the platform can't function — force-open the system
  // settings panel with close disabled.
  const checkCriticalModels = useCallback(async () => {
    try {
      const data = await systemAPI.getSettings()
      const cfg = data.config
      const supervisor = cfg?.models?.supervisor?.model || ''
      const ra = cfg?.models?.ra?.model || ''
      const embedding = cfg?.models?.embedding?.model || ''
      const ok = !!(supervisor && ra && embedding)
      if (!ok) {
        setShowSystemSettings(true)
        setForceSettings(true)
      } else {
        setForceSettings(false)
      }
      return ok
    } catch {
      return false
    }
  }, [])

  const handleCloseSettings = useCallback(async () => {
    if (forceSettings) {
      const ok = await checkCriticalModels()
      if (ok) setShowSystemSettings(false)
      return
    }
    setShowSystemSettings(false)
  }, [forceSettings, checkCriticalModels])

  // Export a project as a ZIP. Disables the button per-project while the request is in flight.
  const handleExport = async (project) => {
    if (exportingId === project.id) return
    setExportingId(project.id)
    try {
      await downloadProjectZip(project, t)
    } finally {
      setExportingId(null)
    }
  }

  // Click outside editing field — save if changed, cancel if not
  useEffect(() => {
    if (!editingField.projectId || !editingField.field) return
    const handler = (e) => {
      const inputRef = editingField.field === 'name' ? nameEditRef.current : descEditRef.current
      if (inputRef && (e.target === inputRef || inputRef.contains(e.target))) return
      const isSaveCancelBtn = e.target.closest('[data-edit-btn]')
      if (isSaveCancelBtn) return
      finishEditing(editingField)
    }
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [editingField])

  // Close the settings dropdown when clicking outside it.
  useEffect(() => {
    if (!showSettingsMenu) return
    const handler = (e) => {
      if (settingsMenuRef.current && !settingsMenuRef.current.contains(e.target)) {
        setShowSettingsMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showSettingsMenu])

  useEffect(() => {
    if (!showNewProjectMenuHeader && !showNewProjectMenuEmpty) return
    const handler = (e) => {
      const inHeader = newProjectMenuHeaderRef.current?.contains(e.target)
      const inEmpty = newProjectMenuEmptyRef.current?.contains(e.target)
      if (!inHeader) setShowNewProjectMenuHeader(false)
      if (!inEmpty) setShowNewProjectMenuEmpty(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showNewProjectMenuHeader, showNewProjectMenuEmpty])

  const openCreate = () => {
    setShowNewProjectMenuHeader(false)
    setShowNewProjectMenuEmpty(false)
    setShowCreateProjectModal(true)
  }
  const openUpload = () => {
    setShowNewProjectMenuHeader(false)
    setShowNewProjectMenuEmpty(false)
    setShowUploadProjectModal(true)
  }

  const renderNewProjectMenuItems = () => (
    <div className="bg-white/95 dark:bg-gray-800/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-[0_20px_50px_rgba(0,0,0,0.15)] rounded-2xl p-2 animate-in fade-in zoom-in duration-200 z-50">
      <button
        onClick={openCreate}
        className="w-full flex items-center gap-3 px-3 py-2.5 text-sm font-bold text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-xl transition-colors"
      >
        <FilePlus className="w-4 h-4 text-gray-400 dark:text-gray-500" />
        <span>{t('projects.createProject')}</span>
      </button>
      <button
        onClick={openUpload}
        className="w-full flex items-center gap-3 px-3 py-2.5 text-sm font-bold text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-xl transition-colors"
      >
        <UploadCloud className="w-4 h-4 text-gray-400 dark:text-gray-500" />
        <span>{t('projects.uploadProject')}</span>
      </button>
    </div>
  )

  const handleCreateProject = useCallback(async (data) => {
    try {
      const p = await projectsAPI.create(data)
      addProject(p)
      loadProjects()
      navigate(`/editor/${p.id}`)
    } catch (error) {
      toastError(error.message || t('projects.toast.createFailed'))
      throw error
    }
  }, [addProject, navigate, t])

  const handleImportedProject = useCallback((project) => {
    addProject(project)
    loadProjects()
    navigate(`/editor/${project.id}`)
  }, [addProject, navigate, t])

  const handleDeleteProject = useCallback(async () => {
    if (!deleteTarget) return
    try {
      await projectsAPI.delete(deleteTarget.id)
      removeProject(deleteTarget.id)
      setDeleteTarget(null)
      loadProjects()
    } catch {
      toastError(t('projects.toast.deleteFailed'))
      setDeleteTarget(null)
    }
  }, [deleteTarget, removeProject])

  const handleOpenProject = (id) => { navigate(`/editor/${id}`) }

  const startEditName = (e, project) => {
    e.stopPropagation()
    setEditingField({ projectId: project.id, field: 'name' })
    setNameEditValue(project.name)
  }

  const startEditDesc = (e, project) => {
    e.stopPropagation()
    setEditingField({ projectId: project.id, field: 'description' })
    setDescEditValue(project.description || '')
  }

  const finishEditing = async (field) => {
    if (!field.projectId) { setEditingField({ projectId: null, field: null }); return }
    const isName = field.field === 'name'
    const currentVal = isName ? nameEditValueRef.current : descEditValueRef.current
    const trimmed = currentVal.trim()

    const maxLength = isName ? 100 : 500
    if (trimmed.length > maxLength) {
      toastError(t(isName ? 'projects.toast.nameTooLong' : 'projects.toast.descTooLong', { max: maxLength }))
      return
    }

    const originalProject = projects.find(p => p.id === field.projectId)
    if (!originalProject) { setEditingField({ projectId: null, field: null }); return }
    const originalVal = isName ? (originalProject.name || '') : (originalProject.description || '')
    if (trimmed !== originalVal) {
      try {
        const updates = {}
        if (isName) updates.name = trimmed || originalProject.name
        else updates.description = trimmed
        await projectsAPI.update(field.projectId, updates)
        loadProjects()
      } catch (err) {
        toastError(err.message || t('projects.toast.updateFailed'))
        return
      }
    }
    setEditingField({ projectId: null, field: null })
  }

  const handleNameKeyDown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finishEditing(editingField) }
    if (e.key === 'Escape') { setEditingField({ projectId: null, field: null }) }
  }

  const handleDescKeyDown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finishEditing(editingField) }
    if (e.key === 'Escape') { setEditingField({ projectId: null, field: null }) }
  }

  const filteredProjects = projects.filter(p => {
    const q = searchQuery.toLowerCase()
    return p.name.toLowerCase().includes(q) || (p.description || '').toLowerCase().includes(q)
  }).sort((a, b) => new Date(b.modified || 0) - new Date(a.modified || 0))

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-gray-50/50 dark:bg-gray-900 text-gray-800 dark:text-gray-200">
      {/* Top Bar */}
      <div className="flex items-center justify-between px-8 py-4 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 shadow-sm flex-shrink-0">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2.5">
            <img src="/logo.svg" alt="SiGMA" className="w-8 h-8 flex-shrink-0" />
            <span className="text-xl font-bold text-gray-900 dark:text-gray-100 tracking-tight font-sans">SiGMA</span>
          </div>
        </div>
        <div className="flex-1 max-w-md mx-8">
          <div className="relative">
            <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') e.target.blur() }}
              placeholder={t('projects.searchPlaceholder')}
              className="w-full pl-10 pr-4 py-2.5 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 focus:bg-white dark:focus:bg-gray-900 transition-all text-sm"
            />
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="relative" ref={settingsMenuRef}>
            <button
              onClick={() => setShowSettingsMenu(!showSettingsMenu)}
              className="bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300 px-4 py-2.5 rounded-xl flex items-center gap-2 transition-all active:scale-95 font-bold text-sm"
            >
              <Settings className="w-4 h-4" />
              {t('settings.title')}
              <ChevronDown className="w-3 h-3" />
            </button>
            {showSettingsMenu && (
              <div className="absolute top-full right-0 mt-2 w-60 bg-white/95 dark:bg-gray-800/95 backdrop-blur-xl border border-gray-200 dark:border-gray-700 shadow-[0_20px_50px_rgba(0,0,0,0.15)] rounded-2xl p-2 animate-in fade-in zoom-in duration-200 z-50">
                <button
                  onClick={() => { setShowSystemSettings(true); setShowSettingsMenu(false) }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 text-sm font-bold text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-xl transition-colors"
                >
                  <Settings className="w-4 h-4 text-gray-400 dark:text-gray-500" />
                  <span>{t('settings.system')}</span>
                </button>
                <button
                  onClick={() => { setShowSkillPanel(true); setShowSettingsMenu(false) }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 text-sm font-bold text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-xl transition-colors"
                >
                  <Wrench className="w-4 h-4 text-gray-400 dark:text-gray-500" />
                  <span>{t('settings.skills')}</span>
                </button>
                <button
                  onClick={() => { setShowTeXManager(true); setShowSettingsMenu(false) }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 text-sm font-bold text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-xl transition-colors"
                >
                  <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500" />
                  <span>{t('settings.tex')}</span>
                </button>
                <div className="h-px bg-gray-100 dark:bg-gray-700 my-1" />
                <div className="flex items-center justify-between px-3 py-2.5">
                  <span className="flex items-center gap-3 text-sm font-bold text-gray-700 dark:text-gray-300">
                    {isDark ? <Moon className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <Sun className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
                    <span>{t('settings.darkMode')}</span>
                  </span>
                  <Toggle checked={isDark} onChange={toggleTheme} label="Toggle dark mode" />
                </div>
                <div className="flex items-center justify-between px-3 py-2.5">
                  <span className="flex items-center gap-3 text-sm font-bold text-gray-700 dark:text-gray-300">
                    <Globe className="w-4 h-4 text-gray-400 dark:text-gray-500" />
                    <span>{t('settings.language')}</span>
                  </span>
                  <LanguageSelector />
                </div>
              </div>
            )}
          </div>
          <div className="relative" ref={newProjectMenuHeaderRef}>
            <button
              onClick={() => setShowNewProjectMenuHeader(!showNewProjectMenuHeader)}
              className="bg-sigma-600 hover:bg-sigma-700 text-white px-5 py-2.5 rounded-xl flex items-center gap-2 shadow-lg shadow-blue-100 dark:shadow-none transition-all active:scale-95 font-bold text-sm"
            >
              <Plus className="w-4.5 h-4.5" />
              {t('projects.newProject')}
              <ChevronDown className="w-3 h-3" />
            </button>
            {showNewProjectMenuHeader && (
              <div className="absolute top-full right-0 mt-2 w-56">
                {renderNewProjectMenuItems()}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Project List */}
      <div className="flex-1 overflow-y-auto p-8">
        <div className="max-w-6xl mx-auto w-full">
          <div className="flex items-center justify-between mb-5">
            <div>
              <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">{t('projects.recentProjects')}</h2>
              <p className="text-xs text-gray-400 dark:text-gray-500 font-medium mt-0.5">{t('projects.projectCount', { count: filteredProjects.length })}</p>
            </div>
          </div>

          {filteredProjects.length === 0 && (
            <div className="bg-white dark:bg-gray-800/60 rounded-2xl border border-dashed border-gray-200 dark:border-gray-700 py-16 text-center">
              <div className="p-4 bg-blue-50 dark:bg-sigma-600/20 rounded-2xl text-sigma-600 w-fit mx-auto mb-4">
                <Folder className="w-8 h-8" />
              </div>
              <h3 className="text-gray-900 dark:text-gray-100 font-bold mb-1">
                {searchQuery ? t('projects.noMatchingProjects') : t('projects.noProjects')}
              </h3>
              <p className="text-sm text-gray-400 dark:text-gray-500 mb-5">
                {searchQuery ? t('projects.tryDifferentSearch') : t('projects.createFirst')}
              </p>
              {!searchQuery && (
                <div className="relative inline-block" ref={newProjectMenuEmptyRef}>
                  <button
                    onClick={() => setShowNewProjectMenuEmpty(!showNewProjectMenuEmpty)}
                    className="bg-sigma-600 hover:bg-sigma-700 text-white px-5 py-2.5 rounded-xl inline-flex items-center gap-2 shadow-lg transition-all active:scale-95 font-bold text-sm"
                  >
                    <Plus className="w-4.5 h-4.5" />
                    {t('projects.newProject')}
                    <ChevronDown className="w-3 h-3" />
                  </button>
                  {showNewProjectMenuEmpty && (
                    <div className="absolute top-full left-1/2 -translate-x-1/2 mt-2 w-56">
                      {renderNewProjectMenuItems()}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {filteredProjects.map(project => {
            const editingName = editingField.projectId === project.id && editingField.field === 'name'
            const editingDesc = editingField.projectId === project.id && editingField.field === 'description'
            return (
              <div
                key={project.id}
                className={`group flex items-start gap-4 py-4 px-5 bg-white dark:bg-gray-800/60 rounded-xl border border-gray-100/60 dark:border-gray-700/60 border-b-gray-200 dark:border-b-gray-700 hover:bg-gray-50/80 dark:hover:bg-gray-800 transition-all cursor-default mb-1.5 ${editingName || editingDesc ? 'ring-2 ring-sigma-600/20 border-sigma-600' : ''}`}
              >
                <div
                  onClick={() => handleOpenProject(project.id)}
                  className="flex-shrink-0 w-10 h-10 bg-blue-50 dark:bg-sigma-600/20 rounded-xl flex items-center justify-center cursor-pointer transition-colors text-sigma-600"
                  title={t('projects.openTitle', { name: project.name })}
                >
                  <Folder className="w-5 h-5" />
                </div>

                <div className="flex-1 min-w-0">
                  {editingName ? (
                    <div onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}>
                      <div className="flex items-center gap-1.5">
                        <input
                          ref={nameEditRef}
                          value={nameEditValue}
                          onChange={e => setNameEditValue(e.target.value)}
                          onKeyDown={handleNameKeyDown}
                          autoFocus
                          onFocus={e => e.target.select()}
                          className="w-64 px-3 py-1 border-2 border-sigma-600 rounded-lg outline-none text-sm font-bold bg-white dark:bg-gray-900"
                        />
                        <button data-edit-btn onClick={() => finishEditing(editingField)} className="p-0.5 text-green-600 hover:bg-green-50 rounded transition-colors" title={t('common.save')}>
                          <Check className="w-4 h-4" />
                        </button>
                        <button data-edit-btn onClick={() => setEditingField({ projectId: null, field: null })} className="p-0.5 text-red-500 hover:bg-red-50 rounded transition-colors" title={t('common.cancel')}>
                          <X className="w-4 h-4" />
                        </button>
                        <span className={`text-[10px] ${nameEditValue.length > 80 ? (nameEditValue.length >= 100 ? 'text-red-500' : 'text-orange-500') : 'text-gray-400'}`}>
                          {nameEditValue.length}/100
                        </span>
                      </div>
                    </div>
                  ) : (
                    <div className="inline-flex items-center gap-0.5 group/name">
                      <h3
                        onClick={() => handleOpenProject(project.id)}
                        className="text-sm font-bold text-gray-900 dark:text-gray-100 leading-snug truncate max-w-[500px] cursor-pointer hover:text-sigma-600 transition-colors"
                      >
                        {project.name}
                      </h3>
                      <button
                        onClick={(e) => startEditName(e, project)}
                        onMouseDown={e => e.stopPropagation()}
                        className="opacity-0 group-hover/name:opacity-100 p-1 text-gray-400 hover:text-sigma-600 hover:bg-sigma-50 rounded-md transition-all flex-shrink-0"
                        title={t('common.rename')}
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  )}

                  <div className="flex items-center gap-2 mt-1.5">
                    <span className="flex items-center gap-1 text-[10px] font-bold text-gray-400 uppercase tracking-wider flex-shrink-0">
                      <Clock className="w-3 h-3" />
                      {formatDate(project.modified, t)}
                    </span>
                    <span className="text-gray-300">·</span>
                    {editingDesc ? (
                      <div onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}>
                        <div className="flex items-center gap-1.5">
                          <input
                            ref={descEditRef}
                            value={descEditValue}
                            onChange={e => setDescEditValue(e.target.value)}
                            onKeyDown={handleDescKeyDown}
                            autoFocus
                            onFocus={e => e.target.select()}
                            placeholder={t('projects.descriptionPlaceholder')}
                            className="w-80 px-3 py-1.5 border-2 border-sigma-600 rounded-lg outline-none text-xs bg-white dark:bg-gray-900"
                          />
                          <button data-edit-btn onClick={() => finishEditing(editingField)} className="p-0.5 text-green-600 hover:bg-green-50 rounded transition-colors" title={t('common.save')}>
                            <Check className="w-4 h-4" />
                          </button>
                          <button data-edit-btn onClick={() => setEditingField({ projectId: null, field: null })} className="p-0.5 text-red-500 hover:bg-red-50 rounded transition-colors" title={t('common.cancel')}>
                            <X className="w-4 h-4" />
                          </button>
                          <span className={`text-[10px] ${descEditValue.length > 400 ? (descEditValue.length >= 500 ? 'text-red-500' : 'text-orange-500') : 'text-gray-400'}`}>
                            {descEditValue.length}/500
                          </span>
                        </div>
                      </div>
                    ) : (
                      <div className="inline-flex items-center gap-0.5 group/desc">
                        <button
                          onClick={(e) => startEditDesc(e, project)}
                          onMouseDown={e => e.stopPropagation()}
                          className="flex items-center gap-1 text-[10px] text-gray-400 hover:text-sigma-600 hover:bg-sigma-50 px-1.5 py-0.5 rounded transition-all"
                        >
                          <span className="truncate max-w-[300px] text-ellipsis overflow-hidden">{project.description || '-'}</span>
                        </button>
                        <button
                          onClick={(e) => startEditDesc(e, project)}
                          onMouseDown={e => e.stopPropagation()}
                          className="opacity-0 group-hover/desc:opacity-100 p-0.5 text-gray-400 hover:text-sigma-600 rounded transition-all flex-shrink-0"
                          title={t('projects.editDescription')}
                        >
                          <Pencil className="w-3 h-3" />
                        </button>
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-1 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-all"
                  onMouseDown={e => e.stopPropagation()}
                  onClick={e => e.stopPropagation()}
                >
                  <button
                    onClick={() => handleExport(project)}
                    disabled={exportingId === project.id}
                    className={`p-2 rounded-lg transition-all disabled:cursor-not-allowed
                      ${exportingId === project.id ? 'text-sigma-600 bg-sigma-50 dark:bg-sigma-600/20' : 'text-gray-400 dark:text-gray-500 hover:text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20'}`}
                    title={t('projects.downloadZip')}
                  >
                    {exportingId === project.id ? <Spinner size="sm" /> : <Download className="w-4 h-4" />}
                  </button>
                  <button
                    onClick={() => setDeleteTarget({ id: project.id, name: project.name })}
                    className="p-2 text-gray-400 dark:text-gray-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-all"
                    title={t('common.delete')}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <CreateProjectModal isOpen={showCreateProjectModal} onClose={() => setShowCreateProjectModal(false)} onCreate={handleCreateProject} />
      <UploadProjectModal isOpen={showUploadProjectModal} onClose={() => setShowUploadProjectModal(false)} onImportedProject={handleImportedProject} />
      <SystemSettingsModal isOpen={showSystemSettings} onClose={handleCloseSettings} blockClose={forceSettings} />
      <SkillPanel isOpen={showSkillPanel} onClose={() => setShowSkillPanel(false)} />
      <TeXManagerPanel isOpen={showTeXManager} onClose={() => setShowTeXManager(false)} />
      <ConfirmModal
        isOpen={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDeleteProject}
        title={t('projects.deleteTitle')}
        message={deleteTarget ? t('projects.deleteConfirm', { name: deleteTarget.name }) : ''}
        danger
      />

      {backendError && (
        <BackendErrorOverlay onRetry={loadProjects} />
      )}
    </div>
  )
}
