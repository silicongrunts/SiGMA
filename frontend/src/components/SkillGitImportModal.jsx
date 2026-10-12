import { useState, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { X, Loader, GitBranch, AlertTriangle } from 'lucide-react'
import { ModalOverlay } from './Modal'
import { skillsAPI } from '../api'

/**
 * SkillGitImportModal — two-phase "import skills from Git" dialog.
 *
 * Phase 1 (input): the user enters a repository URL; the primary button
 * fetches a preview list without importing anything.
 *
 * Phase 2 (select): the URL input is locked, the primary button becomes
 * "Import", and a checklist of the repository's importable skills (name,
 * description) is shown.  Only the checked skills are imported.
 *
 * Failure behaviour:
 *   - scan failure   → inline error, back to an editable input
 *   - empty result   → notice, stays on an editable input
 *   - import failure → inline error, selection preserved for a retry
 *   - closing while importing is blocked; closing mid-scan invalidates
 *     the in-flight response so it can never land on stale state.
 *
 * Props:
 *   isOpen     — boolean, controls visibility
 *   onClose    — callback when the modal should close
 *   onImported — called with the import result {imported, skipped}
 */
export default function SkillGitImportModal({ isOpen, onClose, onImported }) {
  const { t } = useTranslation()
  const [url, setUrl] = useState('')
  const [phase, setPhase] = useState('input') // 'input' | 'select'
  const [scanning, setScanning] = useState(false)
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [skills, setSkills] = useState([])
  const [ignoredCount, setIgnoredCount] = useState(0)
  const [selected, setSelected] = useState(() => new Set())
  // Bumped on every new scan / close; responses from superseded requests
  // are dropped instead of overwriting current state.
  const scanSeqRef = useRef(0)
  const inputRef = useRef(null)

  // Fresh state every time the modal opens (the component stays mounted
  // inside SkillPanel, so state would otherwise survive across opens).
  useEffect(() => {
    if (!isOpen) return
    scanSeqRef.current += 1
    setUrl('')
    setPhase('input')
    setScanning(false)
    setImporting(false)
    setError('')
    setNotice('')
    setSkills([])
    setIgnoredCount(0)
    setSelected(new Set())
    // React commits the DOM before effects run, so the input exists here.
    inputRef.current?.focus()
  }, [isOpen])

  const handleClose = () => {
    if (importing) return
    scanSeqRef.current += 1 // invalidate any in-flight preview request
    onClose()
  }

  const handleScan = async () => {
    const trimmed = url.trim()
    if (!trimmed || scanning || importing) return
    const seq = ++scanSeqRef.current
    setScanning(true)
    setError('')
    setNotice('')
    try {
      const data = await skillsAPI.previewGit(trimmed)
      if (seq !== scanSeqRef.current) return // superseded or modal closed
      const list = data?.skills || []
      setSkills(list)
      setIgnoredCount(data?.ignored_count || 0)
      setSelected(new Set(list.map(s => s.path)))
      if (list.length === 0) {
        // Nothing importable — keep the input editable so the user can
        // point at a different repository right away.
        setNotice(t('skills.gitNoSkills'))
      } else {
        setPhase('select')
      }
    } catch (e) {
      if (seq !== scanSeqRef.current) return
      setError(e.message || t('skills.gitScanFailed'))
    } finally {
      if (seq === scanSeqRef.current) setScanning(false)
    }
  }

  const handleImport = async () => {
    if (importing || selected.size === 0) return
    setImporting(true)
    setError('')
    try {
      const result = await skillsAPI.importGit(url.trim(), [...selected])
      scanSeqRef.current += 1
      onImported?.(result)
      onClose()
    } catch (e) {
      setError(e.message || t('skills.gitImportFailed'))
      // Stay in the select phase with the selection intact — retrying is
      // one click away and nothing the user picked is lost.
    } finally {
      setImporting(false)
    }
  }

  const handleEditUrl = () => {
    if (scanning || importing) return
    setPhase('input')
    setSkills([])
    setSelected(new Set())
    setNotice('')
    setError('')
  }

  const toggleSkill = (path) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const toggleAll = () => {
    setSelected(prev => (prev.size === skills.length ? new Set() : new Set(skills.map(s => s.path))))
  }

  const inputLocked = scanning || phase === 'select'
  const allSelected = skills.length > 0 && selected.size === skills.length

  return (
    <ModalOverlay isOpen={isOpen} onClose={importing ? () => {} : handleClose}>
      <div className="p-8">
        <div className="flex items-center gap-4 mb-6">
          <div className="p-3 bg-blue-50 dark:bg-sigma-600/20 rounded-2xl text-sigma-600">
            <GitBranch className="w-6 h-6" />
          </div>
          <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100 tracking-tight flex-1">
            {t('skills.importGit')}
          </h2>
          <button
            onClick={handleClose}
            disabled={importing}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-xl transition-colors disabled:opacity-40"
          >
            <X className="w-5 h-5 text-gray-400 dark:text-gray-500" />
          </button>
        </div>

        <input
          ref={inputRef}
          type="text"
          value={url}
          onChange={e => setUrl(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && phase === 'input') handleScan()
            if (e.key === 'Escape') handleClose()
          }}
          disabled={inputLocked}
          placeholder={t('skills.gitPlaceholder')}
          className={`w-full px-5 py-3.5 bg-gray-50 dark:bg-gray-800 border rounded-2xl outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 transition-all font-medium ${
            error ? 'border-red-300 dark:border-red-800' : 'border-gray-200 dark:border-gray-700'
          } ${inputLocked ? 'opacity-60 cursor-not-allowed' : ''}`}
        />

        {phase === 'select' && (
          <button
            onClick={handleEditUrl}
            disabled={scanning || importing}
            className="mt-2 text-xs text-gray-500 dark:text-gray-400 hover:text-sigma-600 dark:hover:text-sigma-400 font-medium transition-colors disabled:opacity-40"
          >
            {t('skills.gitChangeUrl')}
          </button>
        )}

        {error && (
          <div className="mt-4 flex items-start gap-2 text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/10 rounded-xl p-3">
            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <span className="font-medium break-all">{error}</span>
          </div>
        )}
        {!error && notice && (
          <div className="mt-4 text-sm text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 rounded-xl p-3">
            {notice}
          </div>
        )}

        {phase === 'select' && (
          <>
            {ignoredCount > 0 && (
              <div className="mt-4 text-xs text-gray-400 dark:text-gray-500">
                {t('skills.gitIgnoredHint', { count: ignoredCount })}
              </div>
            )}
            <div className="mt-3 rounded-2xl border border-gray-200 dark:border-gray-700 overflow-hidden">
              <label className="flex items-center gap-2.5 px-4 py-2 bg-gray-50 dark:bg-gray-800/60 border-b border-gray-200 dark:border-gray-700 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  disabled={importing}
                  className="w-3.5 h-3.5 rounded border-gray-300 dark:border-gray-600 text-sigma-600 focus:ring-sigma-400 focus:ring-1"
                />
                <span className="text-xs font-black uppercase tracking-widest text-gray-500 dark:text-gray-400 flex-1">
                  {t('skills.gitSelectAll')}
                </span>
                <span className="text-xs text-gray-400 dark:text-gray-500">
                  {t('skills.gitSelectedCount', { count: selected.size })}
                </span>
              </label>
              <div className="max-h-64 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800">
                {skills.map(skill => (
                  <label
                    key={skill.path}
                    className="flex items-start gap-3 px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-800/50 cursor-pointer select-none transition-colors"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(skill.path)}
                      onChange={() => toggleSkill(skill.path)}
                      disabled={importing}
                      className="mt-0.5 w-3.5 h-3.5 flex-shrink-0 rounded border-gray-300 dark:border-gray-600 text-sigma-600 focus:ring-sigma-400 focus:ring-1"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-bold text-sm text-gray-900 dark:text-gray-100">{skill.name}</span>
                        {skill.path !== '.' && (
                          <span className="text-[10px] text-gray-400 dark:text-gray-500 font-mono truncate">{skill.path}</span>
                        )}
                        {skill.already_exists && (
                          <span
                            className="text-[10px] font-bold px-1.5 py-0.5 rounded-md bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400"
                            title={t('skills.gitAlreadyExistsTitle')}
                          >
                            {t('skills.gitAlreadyExists')}
                          </span>
                        )}
                      </div>
                      {skill.description && (
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 leading-relaxed line-clamp-2">
                          {skill.description}
                        </p>
                      )}
                    </div>
                  </label>
                ))}
              </div>
            </div>
          </>
        )}

        <div className="flex gap-3 mt-8">
          <button
            onClick={handleClose}
            disabled={importing}
            className="flex-1 py-3 text-gray-500 dark:text-gray-400 font-bold hover:bg-gray-100 dark:hover:bg-gray-700 rounded-2xl transition-colors disabled:opacity-50"
          >
            {t('modal.input.cancel')}
          </button>
          <button
            onClick={phase === 'input' ? handleScan : handleImport}
            disabled={phase === 'input' ? !url.trim() || scanning : selected.size === 0 || importing}
            className="flex-1 py-3 bg-sigma-600 text-white font-bold rounded-2xl shadow-lg hover:bg-sigma-700 active:scale-95 transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {(scanning || importing) && <Loader className="w-4 h-4 animate-spin" />}
            {scanning
              ? t('skills.gitScanning')
              : importing
                ? t('skills.gitImporting')
                : phase === 'input'
                  ? t('skills.gitScan')
                  : t('skills.gitImport')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}
