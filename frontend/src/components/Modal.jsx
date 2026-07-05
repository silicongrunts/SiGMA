import { useState, useEffect, useRef, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { X, Edit3, AlertTriangle, FileText, FileCode, TerminalSquare, Loader, Bot, UploadCloud, FileArchive } from 'lucide-react'
import { projectsAPI } from '../api'

export function ModalOverlay({ isOpen, onClose, children, isDanger }) {
  useEffect(() => {
    const handleEsc = (e) => { if (e.key === 'Escape') onClose() }
    if (isOpen) window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [isOpen, onClose])
  if (!isOpen) return null
  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onClose} />
      <div className="bg-white dark:bg-gray-900 rounded-3xl w-full max-w-md relative z-[5001] shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 animate-in zoom-in duration-300 overflow-hidden">
        {children}
      </div>
    </div>
  )
}

// --- Confirm Modal (for Deletion) ---
export function ConfirmModal({ isOpen, onClose, onConfirm, title, message, danger = false }) {
  const { t } = useTranslation()
  const [submitting, setSubmitting] = useState(false)
  const handleClose = () => { if (!submitting) onClose() }
  const handleConfirm = async () => {
    setSubmitting(true)
    try {
      await onConfirm()
    } catch {
      return  // don't close on error
    } finally {
      setSubmitting(false)
    }
    onClose()
  }
  return (
    <ModalOverlay isOpen={isOpen} onClose={handleClose}>
      <div className="p-8 text-center">
        <div className={`mx-auto w-16 h-16 rounded-full flex items-center justify-center mb-6 ${danger ? 'bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400' : 'bg-blue-50 dark:bg-sigma-600/20 text-blue-600 dark:text-sigma-400'}`}>
          <AlertTriangle className="w-8 h-8" />
        </div>
        <h2 className="text-2xl font-black text-gray-900 dark:text-gray-100 mb-2 tracking-tight">{title}</h2>
        <p className="text-gray-500 dark:text-gray-400 mb-8 leading-relaxed">{message}</p>
        <div className="flex gap-3">
          <button onClick={handleClose} disabled={submitting} className="flex-1 py-3.5 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors disabled:opacity-50">{t('modal.confirm.cancel')}</button>
          <button onClick={handleConfirm} disabled={submitting} className={`flex-1 py-3.5 text-white font-black rounded-2xl shadow-lg transition-all active:scale-95 disabled:opacity-80 flex items-center justify-center gap-2 ${danger ? 'bg-red-600 hover:bg-red-700 shadow-red-200 dark:shadow-none' : 'bg-sigma-600 hover:bg-sigma-700 shadow-blue-200 dark:shadow-none'}`}>
            {submitting && <Loader className="w-4 h-4 animate-spin" />}
            {submitting ? (danger ? t('modal.confirm.deleting') : t('modal.confirm.processing')) : t('modal.confirm.confirm')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}

/**
 * Upload conflict modal — shown when uploading files that already exist.
 *
 * Props:
 *   isOpen, onClose
 *   conflicts  — [{ name: string }] (list of conflicting file names)
 *   onOverwriteAll — overwrite all conflicting files
 *   onSkipConflicts — skip conflicting files, upload rest
 */
export function ConflictModal({ isOpen, onClose, conflicts, onOverwriteAll, onSkipConflicts }) {
  const { t } = useTranslation()
  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="p-8 text-center">
        <div className="mx-auto w-16 h-16 rounded-full flex items-center justify-center mb-6 bg-amber-50 dark:bg-amber-900/30 text-amber-500 dark:text-amber-400">
          <AlertTriangle className="w-8 h-8" />
        </div>
        <h2 className="text-2xl font-black text-gray-900 dark:text-gray-100 mb-2 tracking-tight">{t('modal.conflict.title')}</h2>
        <p className="text-gray-500 dark:text-gray-400 mb-4 leading-relaxed">
          {t('modal.conflict.summary', { count: conflicts.length })}
        </p>
        <div className="text-left bg-gray-50 dark:bg-gray-800 rounded-xl p-3 mb-6 max-h-60 overflow-auto text-sm">
          {conflicts.map(c => (
            <div key={c.name} className="text-gray-700 dark:text-gray-300 py-0.5 truncate font-mono text-xs">{c.name}</div>
          ))}
        </div>
        <div className="flex flex-col gap-2">
          <button onClick={() => { onOverwriteAll(); onClose(); }} className="w-full py-3 bg-red-600 text-white font-black rounded-2xl shadow-lg shadow-red-200 dark:shadow-none hover:bg-red-700 transition-all active:scale-95">
            {t('modal.conflict.overwriteAll')}
          </button>
          <button onClick={() => { onSkipConflicts(); onClose(); }} className="w-full py-3 bg-sigma-600 text-white font-black rounded-2xl shadow-lg shadow-blue-200 dark:shadow-none hover:bg-sigma-700 transition-all active:scale-95">
            {t('modal.conflict.skipConflicts')}
          </button>
          <button onClick={onClose} className="w-full py-3 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 font-bold rounded-2xl hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors">
            {t('modal.conflict.cancel')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}

export function InputModal({ isOpen, onClose, onConfirm, title, placeholder, initialValue = '', icon: Icon, mode, type, isNewFile }) {
  const { t } = useTranslation()
  const [value, setValue] = useState(initialValue)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef(null)
  useEffect(() => {
    if (isOpen) { setValue(initialValue); setError('') }
  }, [isOpen, initialValue])
  // Auto-focus input after modal opens (React commits DOM before effects)
  useEffect(() => {
    if (isOpen) inputRef.current?.focus()
  }, [isOpen])
  const handleConfirm = async () => {
    if (!value.trim()) return
    setSubmitting(true)
    setError('')
    try {
      await onConfirm(value)
      onClose()
    } catch (e) {
      setError(e.message || t('modal.input.failed'))
    } finally {
      setSubmitting(false)
    }
  }

  // Determine if we should show extension buttons
  const showExtButtons = isNewFile !== undefined ? isNewFile : (mode === 'create' && type === 'file')

  // Preset extensions with icons and colors
  const extensions = [
    { ext: '.tex',  icon: <FileText  className="w-3.5 h-3.5" />, color: 'text-blue-500 dark:text-blue-400',  hover: 'hover:bg-blue-50 dark:hover:bg-blue-900/30'  },
    { ext: '.md',  icon: <FileText  className="w-3.5 h-3.5" />, color: 'text-purple-500 dark:text-purple-400', hover: 'hover:bg-purple-50 dark:hover:bg-purple-900/30' },
    { ext: '.ipynb', icon: <FileCode  className="w-3.5 h-3.5" />, color: 'text-orange-500 dark:text-orange-400', hover: 'hover:bg-orange-50 dark:hover:bg-orange-900/30'  },
    { ext: '.py',  icon: <FileCode  className="w-3.5 h-3.5" />, color: 'text-green-600 dark:text-green-400',  hover: 'hover:bg-green-50 dark:hover:bg-green-900/30'  },
    { ext: '.sh',  icon: <TerminalSquare  className="w-3.5 h-3.5" />, color: 'text-gray-600 dark:text-gray-400',  hover: 'hover:bg-gray-100 dark:hover:bg-gray-700'  },
  ]

  const handleExtClick = (ext) => {
    setValue(prev => {
      const dotIndex = prev.lastIndexOf('.')
      if (dotIndex >= 0) {
        // Has existing extension — replace it
        return prev.substring(0, dotIndex) + ext
      }
      // No extension — append
      return prev + ext
    })
  }

  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="p-8">
        <div className="flex items-center gap-4 mb-6">
          <div className="p-3 bg-blue-50 dark:bg-sigma-600/20 rounded-2xl text-sigma-600">{Icon ? <Icon className="w-6 h-6" /> : <Edit3 className="w-6 h-6" />}</div>
          <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100 tracking-tight">{title}</h2>
        </div>

        {/* Extension selector buttons */}
        {showExtButtons && (
          <div className="flex items-center gap-2 mb-3 flex-wrap">
            {extensions.map(ext => (
              <button
                key={ext.ext}
                type="button"
                onClick={() => handleExtClick(ext.ext)}
                className={`${ext.color} ${ext.hover} flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border border-transparent hover:border-gray-200 dark:hover:border-gray-700 transition-all cursor-pointer`}
                title={t('modal.useExt', { ext: ext.ext })}
              >
                {ext.icon}
                <span>{ext.ext}</span>
              </button>
            ))}
          </div>
        )}

        <input ref={inputRef} type="text" value={value} onChange={e => setValue(e.target.value)} onKeyDown={e => { if(e.key === 'Enter') handleConfirm(); if(e.key === 'Escape') onClose(); }} placeholder={placeholder} className={`w-full px-5 py-3.5 bg-gray-50 dark:bg-gray-800 border rounded-2xl outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 transition-all font-medium ${error ? 'border-red-300 dark:border-red-800 mb-2' : 'border-gray-200 dark:border-gray-700 mb-8'}`} />
        {error && <div className="text-xs text-red-500 mb-6">{error}</div>}
        <div className="flex gap-3">
          <button onClick={onClose} disabled={submitting} className="flex-1 py-3 text-gray-500 dark:text-gray-400 font-bold hover:bg-gray-100 dark:hover:bg-gray-700 rounded-2xl transition-colors disabled:opacity-50">{t('modal.input.cancel')}</button>
          <button onClick={handleConfirm} disabled={submitting} className="flex-1 py-3 bg-sigma-600 text-white font-bold rounded-2xl shadow-lg hover:bg-sigma-700 active:scale-95 transition-all disabled:opacity-50 flex items-center justify-center gap-2">
            {submitting && <Loader className="w-4 h-4 animate-spin" />}
            {submitting ? t('modal.input.creating') : t('modal.input.confirm')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}

export function CreateProjectModal({ isOpen, onClose, onCreate }) {
  const { t } = useTranslation()
  const [name, setName] = useState(''); const [desc, setDesc] = useState(''); const [template, setTemplate] = useState('latex'); const inputRef = useRef(null)
  const [templates, setTemplates] = useState([])
  useEffect(() => {
    if (isOpen) {
      projectsAPI.listTemplates().then(list => {
        if (list.length > 0) {
          setTemplates(list)
          setTemplate(list[0].id)
        }
      }).catch(e => console.warn('Failed to load project templates:', e))
    }
  }, [isOpen])
  useEffect(() => { if (isOpen) inputRef.current?.focus() }, [isOpen])
  const handleSubmit = async () => {
    const trimmedName = name.trim()
    const trimmedDesc = desc || ''
    if (!trimmedName) return
    if (trimmedName.length > 100) return
    if (trimmedDesc.length > 500) return
    try {
      await onCreate({ name: trimmedName, description: trimmedDesc, template })
      onClose()
      setName('')
      setDesc('')
    } catch {
      // Don't close modal on failure
    }
  }
  const canCreate = name.trim().length > 0 && name.trim().length <= 100 && (desc || '').length <= 500
  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="p-8">
        <header className="flex justify-between items-center mb-8">
          <h2 className="text-2xl font-black text-gray-900 dark:text-gray-100 tracking-tight">{t('modal.createProject.title')}</h2>
          <button onClick={onClose} className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-xl transition-colors"><X className="w-5 h-5 text-gray-400 dark:text-gray-500" /></button>
        </header>
        <div className="space-y-5 mb-8">
          <div>
            <label className="block text-xs font-black uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-2">{t('modal.createProject.nameLabel')}</label>
            <div className="relative">
              <input ref={inputRef} type="text" value={name} onChange={e => setName(e.target.value)} placeholder={t('modal.createProject.namePlaceholder')} className="w-full px-5 py-3.5 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-2xl outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 transition-all font-bold" />
              <span className={`absolute right-3 top-1/2 -translate-y-1/2 text-[10px] ${name.length > 80 ? (name.length >= 100 ? 'text-red-500' : 'text-orange-500') : 'text-gray-400 dark:text-gray-500'}`}>
                {name.length}/100
              </span>
            </div>
          </div>
          <div>
            <label className="block text-xs font-black uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-2">{t('modal.createProject.descriptionLabel')}</label>
            <div className="relative">
              <textarea value={desc} onChange={e => setDesc(e.target.value)} rows="3" placeholder={t('modal.createProject.descriptionPlaceholder')} className="w-full px-5 py-3.5 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-2xl outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 transition-all resize-none font-medium text-sm" />
              <span className={`absolute right-3 bottom-1 text-[10px] ${desc.length > 400 ? (desc.length >= 500 ? 'text-red-500' : 'text-orange-500') : 'text-gray-400 dark:text-gray-500'}`}>
                {desc.length}/500
              </span>
            </div>
          </div>
          {/* Template selector */}
          <div>
            <label className="block text-xs font-black uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-2">{t('modal.createProject.templateLabel')}</label>
            <div className={templates.length <= 3 ? 'grid grid-cols-3 gap-2' : 'grid grid-cols-3 gap-2 max-h-48 overflow-y-auto'}>
              {templates.map(t => (
                <button key={t.id} type="button" onClick={() => setTemplate(t.id)}
                  className={`p-3 rounded-2xl border-2 text-left transition-all flex flex-col ${template === t.id ? 'border-sigma-600 bg-sigma-50 dark:bg-sigma-600/20' : 'border-gray-100 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 hover:border-gray-200 dark:hover:border-gray-600'}`}>
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`w-6 h-6 rounded-lg flex items-center justify-center text-[10px] font-black flex-shrink-0 ${template === t.id ? 'bg-sigma-600 text-white' : 'bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400'}`}>{t.icon}</span>
                    <span className={`text-sm font-bold whitespace-nowrap ${template === t.id ? 'text-sigma-700 dark:text-sigma-400' : 'text-gray-600 dark:text-gray-400'}`}>{t.name}</span>
                  </div>
                  <span className="text-[10px] text-gray-400 dark:text-gray-500 line-clamp-2">{t.desc}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
        <button onClick={handleSubmit} disabled={!canCreate} className="w-full py-4 bg-sigma-600 text-white font-black rounded-2xl shadow-[0_15px_35px_rgba(37,99,235,0.3)] hover:bg-sigma-700 active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none">{t('modal.createProject.submit')}</button>
      </div>
    </ModalOverlay>
  )
}

export function UploadProjectModal({ isOpen, onClose, onImportedProject }) {
  const { t } = useTranslation()
  const [file, setFile] = useState(null)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [showRegisterPanel, setShowRegisterPanel] = useState(false)
  const [unregistered, setUnregistered] = useState([])
  const [registeringName, setRegisteringName] = useState('')
  const inputRef = useRef(null)

  const MAX_BYTES = 1024 * 1024 * 1024 // 1 GiB — keep in sync with backend

  useEffect(() => {
    if (!isOpen) {
      setFile(null); setError(''); setSubmitting(false); setDragging(false)
      setShowRegisterPanel(false); setUnregistered([]); setRegisteringName('')
    }
  }, [isOpen])

  const derivedName = file ? file.name.replace(/\.zip$/i, '') : ''

  const acceptFile = (selected) => {
    setError('')
    if (!selected) return
    if (!selected.name.toLowerCase().endsWith('.zip')) {
      setError(t('modal.uploadProject.errors.notZip')); return
    }
    if (selected.size > MAX_BYTES) {
      setError(t('modal.uploadProject.errors.tooLarge', { mb: Math.floor(MAX_BYTES / (1024 * 1024)) }))
      return
    }
    setFile(selected)
  }

  const handleDrop = (e) => {
    e.preventDefault(); setDragging(false)
    acceptFile(e.dataTransfer.files?.[0])
  }

  const finishWith = async (projectPromise) => {
    setSubmitting(true); setError('')
    try {
      const p = await projectPromise
      onImportedProject(p)
      onClose()
    } catch (e) {
      setError(e.message || t('modal.uploadProject.errors.importFailed'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleSubmit = () => {
    if (!file || submitting) return
    const formData = new FormData()
    formData.append('file', file)
    return finishWith(projectsAPI.import(formData))
  }

  const openRegisterPanel = async () => {
    setShowRegisterPanel(true)
    setError('')
    try {
      const list = await projectsAPI.listUnregistered()
      setUnregistered(Array.isArray(list) ? list : [])
    } catch (e) {
      setError(e.message || t('modal.uploadProject.errors.loadFailed'))
    }
  }

  const handleRegister = (name) => {
    if (registeringName || submitting) return
    setRegisteringName(name)
    return finishWith(projectsAPI.register({ directory: name }).finally(() => setRegisteringName('')))
  }

  return (
    <ModalOverlay isOpen={isOpen} onClose={submitting ? () => {} : onClose}>
      <div className="p-8">
        <header className="flex justify-between items-center mb-8">
          <h2 className="text-2xl font-black text-gray-900 dark:text-gray-100 tracking-tight">{t('modal.uploadProject.title')}</h2>
          <button onClick={submitting ? undefined : onClose} disabled={submitting} className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-xl transition-colors disabled:opacity-40">
            <X className="w-5 h-5 text-gray-400 dark:text-gray-500" />
          </button>
        </header>

        <div
          onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => !submitting && !showRegisterPanel && inputRef.current?.click()}
          className={`cursor-pointer rounded-2xl border-2 border-dashed p-8 text-center transition-colors ${dragging ? 'border-sigma-600 bg-sigma-50 dark:bg-sigma-600/20' : 'border-gray-200 dark:border-gray-700 hover:border-sigma-400'} ${submitting || showRegisterPanel ? 'pointer-events-none opacity-60' : ''}`}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => acceptFile(e.target.files?.[0])}
          />
          {file ? (
            <div className="flex flex-col items-center gap-2">
              <FileArchive className="w-10 h-10 text-sigma-600" />
              <span className="font-bold text-gray-900 dark:text-gray-100 break-all">{file.name}</span>
              <span className="text-xs text-gray-400 dark:text-gray-500">{t('modal.uploadProject.willImportAs', { name: derivedName })}</span>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <UploadCloud className="w-10 h-10 text-gray-400 dark:text-gray-500" />
              <span className="font-bold text-gray-700 dark:text-gray-300">{t('modal.uploadProject.dropHere')}</span>
              <span className="text-xs text-gray-400 dark:text-gray-500">{t('modal.uploadProject.zipOnly')}</span>
            </div>
          )}
        </div>

        {error && (
          <div className="mt-4 flex items-start gap-2 text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/10 rounded-xl p-3">
            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <span className="font-medium">{error}</span>
          </div>
        )}

        <p className="mt-4 text-xs text-gray-400 dark:text-gray-500 leading-relaxed">{t('modal.uploadProject.hint')}</p>

        <button
          onClick={handleSubmit}
          disabled={!file || submitting}
          className="mt-6 w-full py-4 bg-sigma-600 text-white font-black rounded-2xl shadow-[0_15px_35px_rgba(37,99,235,0.3)] hover:bg-sigma-700 active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none flex items-center justify-center gap-2"
        >
          {submitting && <Loader className="w-4 h-4 animate-spin" />}
          {submitting ? t('modal.uploadProject.importing') : t('modal.uploadProject.submit')}
        </button>

        {/* Manual-copy fallback for projects too large to upload as a zip. */}
        {!showRegisterPanel ? (
          <button
            onClick={openRegisterPanel}
            disabled={submitting}
            className="mt-3 w-full text-xs text-gray-500 dark:text-gray-400 hover:text-sigma-600 dark:hover:text-sigma-400 font-medium transition-colors disabled:opacity-40"
          >
            {t('modal.uploadProject.registerLink')}
          </button>
        ) : (
          <div className="mt-4 rounded-2xl border border-gray-200 dark:border-gray-700 overflow-hidden">
            <div className="px-4 py-2.5 bg-gray-50 dark:bg-gray-800/60 border-b border-gray-200 dark:border-gray-700">
              <span className="text-xs font-black uppercase tracking-widest text-gray-500 dark:text-gray-400">
                {t('modal.uploadProject.registerTitle')}
              </span>
            </div>
            {unregistered.length === 0 ? (
              <div className="px-4 py-6 text-center text-xs text-gray-400 dark:text-gray-500">
                {t('modal.uploadProject.noUnregistered')}
              </div>
            ) : (
              <ul className="max-h-44 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800">
                {unregistered.map((d) => (
                  <li key={d.name} className="flex items-center justify-between gap-3 px-4 py-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-bold text-gray-800 dark:text-gray-200 truncate">{d.name}</div>
                      <div className="text-[10px] text-gray-400 dark:text-gray-500">
                        {d.has_sigma ? t('modal.uploadProject.sigmaProject') : t('modal.uploadProject.externalProject')}
                      </div>
                    </div>
                    <button
                      onClick={() => handleRegister(d.name)}
                      disabled={submitting}
                      className="text-xs font-bold px-3 py-1.5 rounded-lg bg-sigma-600 text-white hover:bg-sigma-700 disabled:opacity-40 transition-colors flex items-center gap-1"
                    >
                      {registeringName === d.name && <Loader className="w-3 h-3 animate-spin" />}
                      {t('modal.uploadProject.register')}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="px-4 py-2 bg-gray-50 dark:bg-gray-800/60 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => { setShowRegisterPanel(false); setError('') }}
                disabled={submitting}
                className="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 font-medium"
              >
                {t('modal.uploadProject.registerCancel')}
              </button>
            </div>
          </div>
        )}
      </div>
    </ModalOverlay>
  )
}

export function LogModal({ isOpen, onClose, logs, diagnostics, onAskSiGMA, onJumpToError }) {
  const { t } = useTranslation()

  // Build a lookup of clickable line indices: for each log line that
  // starts with "l.<number>", store { file, line } so clicking it
  // jumps to the right place in the editor.
  const logLines = useMemo(() => (logs || '').split('\n'), [logs])

  // Build a map from "l.<line>" text to the corresponding diagnostic
  const errorLineMap = useMemo(() => {
    const map = new Map()
    if (!diagnostics) return map
    for (const d of diagnostics) {
      map.set(d.line, d)
    }
    return map
  }, [diagnostics])

  if (!isOpen) return null

  const handleLineClick = (lineIdx) => {
    const raw = logLines[lineIdx]
    const m = raw && raw.match(/^l\.(\d+)/)
    if (!m || !onJumpToError) return
    const lineNo = parseInt(m[1], 10)
    const diag = errorLineMap.get(lineNo)
    onJumpToError(diag?.file || '', lineNo)
  }

  // Check if a line is an error line (l.<number>)
  const isErrorLine = (line) => /^l\.\d+/.test(line)
  // Check if a line starts with !
  const isErrorHeader = (line) => line.startsWith('!')

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onClose} />
      <div className="bg-white dark:bg-gray-900 rounded-3xl w-full max-w-4xl h-[75vh] flex flex-col relative z-[5001] shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 animate-in zoom-in duration-300 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800 flex-shrink-0">
          <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 tracking-tight flex items-center gap-2.5">
            <div className="p-1.5 bg-gray-100 dark:bg-gray-800 rounded-xl text-gray-500 dark:text-gray-400">
              <TerminalSquare className="w-4 h-4" />
            </div>
            {t('modal.log.title')}
          </h2>
          <div className="flex items-center gap-1">
            {onAskSiGMA && (
              <button
                onClick={() => onAskSiGMA(logs)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold text-sigma-600 dark:text-sigma-400 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded-xl transition-colors"
                title={t('modal.log.askSiGMATitle')}
              >
                <Bot className="w-4 h-4" />
                {t('modal.log.askSiGMA')}
              </button>
            )}
            <button onClick={onClose} className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-xl transition-colors">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
        {/* Log content */}
        <div className="flex-1 overflow-auto bg-gray-900 px-6 py-4 font-mono text-[11px] leading-relaxed whitespace-pre text-gray-300">
          {logs ? logLines.map((line, i) => {
            if (isErrorHeader(line)) {
              return <div key={i} className="text-red-400 font-bold">{line}</div>
            }
            if (isErrorLine(line) && onJumpToError) {
              return (
                <div key={i}
                  onClick={() => handleLineClick(i)}
                  className="text-amber-300 hover:text-amber-200 hover:bg-white/5 cursor-pointer rounded px-1 -mx-1 transition-colors"
                >
                  {line}
                </div>
              )
            }
            return <div key={i}>{line}</div>
          }) : <span className="text-gray-400 italic">{t('modal.log.empty')}</span>}
        </div>
      </div>
    </div>
  )
}
