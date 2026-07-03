import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { EditorState, Compartment } from '@codemirror/state'
import { EditorView, keymap, lineNumbers } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language'
import { oneDarkHighlightStyle } from '@codemirror/theme-one-dark'
import { markdown } from '@codemirror/lang-markdown'
import { json } from '@codemirror/lang-json'
import { javascript } from '@codemirror/lang-javascript'
import { python } from '@codemirror/lang-python'
import { css } from '@codemirror/lang-css'
import { html } from '@codemirror/lang-html'
import { ModalOverlay, InputModal, ConfirmModal } from './Modal'
import { skillsAPI } from '../api'
import { toastError, toastSuccess } from './Toast'
import { useTheme } from '../hooks/useTheme'
import { useStore } from '../store/useStore'

// ---------------------------------------------------------------------------
// CodeMirror language resolution
// ---------------------------------------------------------------------------

const languageConf = new Compartment()
const cmThemeCompartment = new Compartment()
const cmSyntaxCompartment = new Compartment()

const lightCmTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: '#ffffff' },
  '.cm-scroller': { overflow: 'auto' },
  '.cm-content': { fontFamily: "'JetBrains Mono', 'Fira Code', monospace", fontSize: '13px' },
  '.cm-gutters': { backgroundColor: '#f8f9fa', borderRight: '1px solid #e5e7eb' },
})

const darkCmTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: '#111827', color: '#e5e7eb' },
  '.cm-scroller': { overflow: 'auto' },
  '.cm-content': { fontFamily: "'JetBrains Mono', 'Fira Code', monospace", fontSize: '13px', caretColor: '#e5e7eb' },
  '.cm-cursor': { borderLeftColor: '#e5e7eb' },
  '.cm-gutters': { backgroundColor: '#0f172a', borderRight: '1px solid #1f2937', color: '#4b5563' },
  '.cm-activeLine': { backgroundColor: '#1e293b' },
  '.cm-activeLineGutter': { backgroundColor: '#1e293b' },
  '.cm-selectionBackground, ::selection': { backgroundColor: '#264f78' },
  '&.cm-focused .cm-selectionBackground': { backgroundColor: '#264f78' },
})

function getLanguage(filename) {
  const ext = filename.includes('.') ? filename.split('.').pop().toLowerCase() : ''
  if (ext === 'md') return markdown()
  if (ext === 'json') return json()
  if (ext === 'js' || ext === 'jsx' || ext === 'ts' || ext === 'tsx') return javascript()
  if (ext === 'py') return python()
  if (ext === 'css') return css()
  if (ext === 'html' || ext === 'xml' || ext === 'svg') return html()
  return []
}

// ---------------------------------------------------------------------------
// Icons (inline SVG)
// ---------------------------------------------------------------------------

const IconFolder = () => (
  <svg className="w-4 h-4 text-amber-500 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" />
  </svg>
)
const IconFile = () => (
  <svg className="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
  </svg>
)
const IconChevron = ({ open }) => (
  <svg className={`w-3.5 h-3.5 text-gray-400 transition-transform flex-shrink-0 ${open ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
  </svg>
)
const IconPlus = () => (
  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
  </svg>
)
const IconEdit = () => (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125" />
  </svg>
)
const IconTrash = () => (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
  </svg>
)
const IconRefresh = () => (
  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M2.985 19.644l3.181-3.182" />
  </svg>
)

// ---------------------------------------------------------------------------
// FileTree helper — builds nested tree from flat file list
// ---------------------------------------------------------------------------

function buildTree(files) {
  const root = { children: [] }
  for (const f of files) {
    const parts = f.path.split('/')
    let node = root
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i]
      let child = node.children.find(c => c.name === part)
      if (!child) {
        child = {
          name: part,
          path: parts.slice(0, i + 1).join('/'),
          type: i === parts.length - 1 ? f.type : 'directory',
          children: [],
          size: f.size,
        }
        node.children.push(child)
      }
      node = child
    }
  }
  // Sort: directories first, then files, alphabetical within groups
  const sortNodes = (nodes) => {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'directory' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    for (const n of nodes) sortNodes(n.children)
  }
  sortNodes(root.children)
  return root.children
}

// ---------------------------------------------------------------------------
// FileTreeNode — recursive tree item
// ---------------------------------------------------------------------------

function FileTreeNode({ node, depth, activePath, onSelect, onAction, protectedPaths }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(true)
  const isDir = node.type === 'directory'
  const isActive = node.path === activePath
  const isProtected = protectedPaths.has(node.path)

  const handleClick = () => {
    if (isDir) { setOpen(prev => !prev); return }
    onSelect(node.path)
  }

  const handleAction = (e, action) => {
    e.stopPropagation()
    onAction(action, node)
  }

  return (
    <div>
      <div
        className={`flex items-center gap-1.5 px-2 py-1 cursor-pointer rounded-lg text-sm group transition-colors
          ${isActive ? 'bg-sigma-50 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-300 font-medium' : 'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'}
          ${depth === 0 ? 'font-medium' : ''}`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        onClick={handleClick}
      >
        {isDir && <IconChevron open={open} />}
        {isDir ? <IconFolder /> : <IconFile />}
        <span className="flex-1 truncate text-xs">{node.name}</span>
        {!isProtected && (
          <div className="hidden group-hover:flex items-center gap-0.5">
            <button onClick={(e) => handleAction(e, 'rename')} className="p-0.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded" title={t('common.rename')}>
              <IconEdit />
            </button>
            <button onClick={(e) => handleAction(e, 'delete')} className="p-0.5 hover:bg-red-100 dark:hover:bg-red-900/30 text-red-500 rounded" title={t('common.delete')}>
              <IconTrash />
            </button>
          </div>
        )}
      </div>
      {isDir && open && node.children.map(child => (
        <FileTreeNode
          key={child.path}
          node={child}
          depth={depth + 1}
          activePath={activePath}
          onSelect={onSelect}
          onAction={onAction}
          protectedPaths={protectedPaths}
        />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SkillPanel — main component
// ---------------------------------------------------------------------------

/**
 * SkillPanel — modal for viewing, toggling and editing global skills.
 *
 * Two views:
 *   1. Skill list: toggle on/off, edit button
 *   2. File editor: file tree + CodeMirror editor
 *
 * Props:
 *   isOpen  — boolean, controls visibility
 *   onClose — callback when the panel should close
 */
export default function SkillPanel({ isOpen, onClose }) {
  const { t } = useTranslation()
  // ── Skill list state ──
  const [skills, setSkills] = useState([])
  const [loading, setLoading] = useState(true)
  const [togglingId, setTogglingId] = useState(null)

  // ── Editor state ──
  const [editingSkill, setEditingSkill] = useState(null)
  const [files, setFiles] = useState([])
  const [activeFilePath, setActiveFilePath] = useState(null)
  const [fileContent, setFileContent] = useState('')
  const [fileHash, setFileHash] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [filesLoading, setFilesLoading] = useState(false)
  // ── Modal state ──
  const [inputModal, setInputModal] = useState({ open: false })
  const [confirmModal, setConfirmModal] = useState({ open: false })
  const [discardModal, setDiscardModal] = useState({ open: false, message: '' })
  const [errorModal, setErrorModal] = useState({ open: false, message: '' })
  const [importMenuOpen, setImportMenuOpen] = useState(false)
  const [importing, setImporting] = useState(false)
  const zipInputRef = useRef(null)
  const confirmResolverRef = useRef(null)

  // ── CodeMirror refs ──
  const editorRef = useRef(null)
  const cmViewRef = useRef(null)
  const isInternalUpdate = useRef(false)
  const savingRef = useRef(false)
  const saveHandlerRef = useRef(null)

  // Paths that cannot be renamed or deleted
  const protectedPaths = useMemo(() => new Set(['SKILL.md']), [])
  const { isDark } = useTheme()

  // -----------------------------------------------------------------------
  // Data fetching
  // -----------------------------------------------------------------------

  const fetchSkills = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const data = await skillsAPI.list()
      setSkills(data || [])
    } catch {
      toastError(t('skills.toast.loadFailed'))
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => { if (isOpen) fetchSkills() }, [isOpen, fetchSkills])

  // Reset editor on close
  useEffect(() => {
    if (!isOpen) {
      setEditingSkill(null)
      setFiles([])
      setActiveFilePath(null)
      setFileContent('')
      setDirty(false)
    }
  }, [isOpen])

  const fetchFiles = useCallback(async (skillId) => {
    setFilesLoading(true)
    try {
      const data = await skillsAPI.listFiles(skillId)
      setFiles(data || [])
      return data || []
    } catch (e) {
      toastError(e.message || t('skills.toast.loadFilesFailed'))
      setFiles([])
      return []
    } finally {
      setFilesLoading(false)
    }
  }, [])

  const loadFile = useCallback(async (skillId, filePath) => {
    try {
      const data = await skillsAPI.readFile(skillId, filePath)
      isInternalUpdate.current = true
      setFileContent(data.content)
      setFileHash(data.hash)
      setActiveFilePath(filePath)
      setDirty(false)
    } catch (e) {
      toastError(e.message || t('skills.toast.readFailed'))
    }
  }, [])

  // -----------------------------------------------------------------------
  // Unsaved changes guard (modal-based, no native confirm)
  // -----------------------------------------------------------------------

  const resolveDiscard = (result) => {
    setDiscardModal({ open: false, message: '' })
    confirmResolverRef.current?.(result)
    confirmResolverRef.current = null
  }

  const confirmDiscard = (message = t('skills.discardConfirm')) => new Promise((resolve) => {
    if (!dirty) { resolve(true); return }
    confirmResolverRef.current = resolve
    setDiscardModal({ open: true, message })
  })

  const handleClose = () => {
    if (discardModal.open || confirmModal.open || inputModal.open || errorModal.open) return
    if (!dirty) { onClose(); return }
    confirmResolverRef.current = (ok) => { if (ok) onClose() }
    setDiscardModal({ open: true, message: t('skills.discardCloseConfirm') })
  }

  // ESC key handler — skip when a child modal is open
  useEffect(() => {
    if (!isOpen) return
    const handler = (e) => { if (e.key === 'Escape') handleClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isOpen, dirty, discardModal.open, confirmModal.open, inputModal.open, errorModal.open])

  // -----------------------------------------------------------------------
  // Skill list actions
  // -----------------------------------------------------------------------

  const handleToggle = async (skillId) => {
    setTogglingId(skillId)
    try {
      const updated = await skillsAPI.toggle(skillId)
      // Update in place so the list order stays stable while the backend
      // returns the renamed skill id and enabled state.
      setSkills(prev => prev.map(s =>
        s.id === skillId
          ? { ...s, id: updated.id, enabled: updated.enabled }
          : s
      ))
      useStore.getState().bumpSkillsVersion()
      // If editing this skill, exit editor (toggle changes folder name)
      if (editingSkill) {
        const bare = editingSkill.id.replace(/^\./, '')
        const toggled = skillId.replace(/^\./, '')
        if (bare === toggled) {
          setEditingSkill(null)
          setFiles([])
          setActiveFilePath(null)
          setDirty(false)
        }
      }
    } catch {
      toastError(t('skills.toast.toggleFailed'))
    } finally {
      setTogglingId(null)
    }
  }

  const handleEditSkill = async (skill) => {
    if (!await confirmDiscard()) return
    setEditingSkill(skill)
    setActiveFilePath(null)
    setFileContent('')
    setDirty(false)
    const fileList = await fetchFiles(skill.id)
    // Auto-load SKILL.md
    if (fileList.some(f => f.path === 'SKILL.md')) {
      await loadFile(skill.id, 'SKILL.md')
    }
  }

  const handleDeleteSkill = (skill) => {
    setConfirmModal({
      open: true,
      title: t('skills.deleteSkillTitle'),
      message: t('skills.deleteSkillConfirm', { name: skill.name }),
      onConfirm: async () => {
        await skillsAPI.delete(skill.id)
        useStore.getState().bumpSkillsVersion()
        await fetchSkills(true)
      },
    })
  }

  const handleBackToList = async () => {
    if (!await confirmDiscard()) return
    setEditingSkill(null)
    setFiles([])
    setActiveFilePath(null)
    setFileContent('')
    setDirty(false)
    await fetchSkills(true)
  }

  // -----------------------------------------------------------------------
  // File actions
  // -----------------------------------------------------------------------

  const handleFileSelect = async (filePath) => {
    if (filePath === activeFilePath) return
    if (!await confirmDiscard()) return
    await loadFile(editingSkill.id, filePath)
  }

  const handleSave = async () => {
    if (!activeFilePath || !editingSkill || savingRef.current) return
    savingRef.current = true
    setSaving(true)
    try {
      await skillsAPI.writeFile(editingSkill.id, activeFilePath, fileContent, fileHash)
      const data = await skillsAPI.readFile(editingSkill.id, activeFilePath)
      isInternalUpdate.current = true
      setFileContent(data.content)
      setFileHash(data.hash)
      setDirty(false)
      await fetchSkills(true)
      // SKILL.md edits can change ChatPanel's /skill submenu labels.
      if (activeFilePath === 'SKILL.md') {
        useStore.getState().bumpSkillsVersion()
      }
    } catch (e) {
      const msg = e.message || t('skills.toast.saveFailed')
      if (msg.includes('Invalid SKILL.md')) {
        setErrorModal({ open: true, message: msg })
      } else if (msg.includes('modified externally') || msg.includes('hash')) {
        setErrorModal({ open: true, message: t('skills.externalModError') })
      } else {
        toastError(msg)
      }
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }

  // Keep ref in sync so Ctrl+S inside CodeMirror always calls the latest handler
  saveHandlerRef.current = handleSave

  const handleRevert = () => {
    if (activeFilePath && editingSkill) {
      loadFile(editingSkill.id, activeFilePath)
    }
  }

  const handleFileAction = (action, node) => {
    if (action === 'rename') {
      setInputModal({
        open: true,
        title: t('skills.renameTitle', { name: node.name }),
        placeholder: t('skills.renamePlaceholder'),
        initial: node.name,
        isNewFile: false,
        onConfirm: async (newName) => {
          await skillsAPI.renameFile(editingSkill.id, node.path, newName)
          await fetchFiles(editingSkill.id)
          // If the renamed file was active, update active path
          if (activeFilePath === node.path) {
            const dir = node.path.includes('/') ? node.path.substring(0, node.path.lastIndexOf('/') + 1) : ''
            const newPath = dir + newName
            setActiveFilePath(newPath)
          }
        },
      })
    } else if (action === 'delete') {
      setConfirmModal({
        open: true,
        title: t('skills.deleteItemTitle'),
        message: t('skills.deleteItemConfirm', { name: node.name }),
        onConfirm: async () => {
          await skillsAPI.deleteFile(editingSkill.id, node.path)
          await fetchFiles(editingSkill.id)
          if (activeFilePath === node.path) {
            setActiveFilePath(null)
            setFileContent('')
            setDirty(false)
          }
        },
      })
    }
  }

  const handleCreateNew = (type) => {
    setInputModal({
      open: true,
      title: type === 'directory' ? t('common.newFolder') : t('common.newFile'),
      placeholder: type === 'directory' ? t('skills.folderPlaceholder') : t('skills.filePlaceholder'),
      initial: '',
      isNewFile: type === 'file',
      onConfirm: async (name) => {
        await skillsAPI.createFile(editingSkill.id, name, type)
        await fetchFiles(editingSkill.id)
        if (type === 'file') await loadFile(editingSkill.id, name)
      },
    })
  }

  // -----------------------------------------------------------------------
  // Import — ZIP upload & Git clone
  // -----------------------------------------------------------------------

  const showImportResult = (result) => {
    const { imported = [], skipped = [] } = result
    if (imported.length > 0) {
      const names = imported.map(s => s.name).join(', ')
      toastSuccess(t('skills.importedCount', { count: imported.length, names }))
    }
    if (skipped.length > 0) {
      const items = skipped.map(s => `${s.path} (${s.reason})`).join('; ')
      toastError(t('skills.skippedCount', { count: skipped.length, items }))
    }
    if (imported.length === 0 && skipped.length === 0) {
      toastError(t('skills.noValidSkills'))
    }
  }

  const handleZipUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setImportMenuOpen(false)
    setImporting(true)
    try {
      const result = await skillsAPI.importZip(file)
      showImportResult(result)
      await fetchSkills(true)
      if (result?.imported?.length > 0) {
        useStore.getState().bumpSkillsVersion()
      }
    } catch (err) {
      toastError(err.message || t('skills.zipImportFailed'))
    } finally {
      setImporting(false)
      // Reset file input so the same file can be re-selected
      if (zipInputRef.current) zipInputRef.current.value = ''
    }
  }

  const handleGitImport = () => {
    setImportMenuOpen(false)
    setInputModal({
      open: true,
      title: t('skills.importGit'),
      placeholder: t('skills.gitPlaceholder'),
      initial: '',
      isNewFile: false,
      onConfirm: async (url) => {
        setImporting(true)
        try {
          const result = await skillsAPI.importGit(url)
          showImportResult(result)
          await fetchSkills(true)
          if (result?.imported?.length > 0) {
            useStore.getState().bumpSkillsVersion()
          }
        } finally {
          setImporting(false)
        }
      },
    })
  }

  // Close dropdown when clicking outside
  useEffect(() => {
    if (!importMenuOpen) return
    const handler = (e) => {
      if (!e.target.closest('[data-import-menu]')) setImportMenuOpen(false)
    }
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [importMenuOpen])

  // -----------------------------------------------------------------------
  // CodeMirror integration
  // -----------------------------------------------------------------------

  // Create/destroy editor when active file changes
  useEffect(() => {
    if (!editorRef.current || !activeFilePath) return

    if (cmViewRef.current) {
      cmViewRef.current.destroy()
      cmViewRef.current = null
    }

    const lang = getLanguage(activeFilePath)
    const state = EditorState.create({
      doc: fileContent,
      extensions: [
        lineNumbers(),
        history(),
        keymap.of([
          { key: 'Mod-s', run: () => { saveHandlerRef.current?.(); return true }, preventDefault: true },
          ...defaultKeymap, ...historyKeymap, indentWithTab,
        ]),
        syntaxHighlighting(document.documentElement.classList.contains('dark') ? oneDarkHighlightStyle : defaultHighlightStyle),
        languageConf.of(lang),
        cmThemeCompartment.of(isDark ? darkCmTheme : lightCmTheme),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && !isInternalUpdate.current) {
            setFileContent(update.state.doc.toString())
            setDirty(true)
          }
          isInternalUpdate.current = false
        }),
      ],
    })
    cmViewRef.current = new EditorView({ state, parent: editorRef.current })

    return () => {
      if (cmViewRef.current) { cmViewRef.current.destroy(); cmViewRef.current = null }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeFilePath])

  // Sync content from external source (loadFile)
  useEffect(() => {
    if (!cmViewRef.current || !isInternalUpdate.current) return
    const currentDoc = cmViewRef.current.state.doc.toString()
    if (currentDoc !== fileContent) {
      cmViewRef.current.dispatch({
        changes: { from: 0, to: cmViewRef.current.state.doc.length, insert: fileContent },
      })
    }
    isInternalUpdate.current = false
  }, [fileContent])

  // Reconfigure CodeMirror theme when dark mode toggles
  useEffect(() => {
    if (!cmViewRef.current) return
    cmViewRef.current.dispatch({
      effects: [
        cmThemeCompartment.reconfigure(isDark ? darkCmTheme : lightCmTheme),
        cmSyntaxCompartment.reconfigure(syntaxHighlighting(isDark ? oneDarkHighlightStyle : defaultHighlightStyle)),
      ],
    })
  }, [isDark])

  // -----------------------------------------------------------------------
  // Computed
  // -----------------------------------------------------------------------

  const tree = useMemo(() => buildTree(files), [files])

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (!isOpen) return null

  return (
    <>
      {/* Overlay */}
      <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={handleClose} />
        <div className="bg-white dark:bg-gray-900 rounded-3xl w-full max-w-4xl max-h-[85vh] relative z-[5001] shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 overflow-hidden flex flex-col animate-in zoom-in duration-300">

          {/* Header */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-gray-100 dark:border-gray-800 flex-shrink-0 gap-3">
            <div className="flex items-center gap-3 min-w-0">
              {editingSkill && (
                <button onClick={handleBackToList} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors flex-shrink-0">
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
                  </svg>
                </button>
              )}
              <h2 className="text-base font-bold text-gray-900 dark:text-gray-100 tracking-tight break-words">
                {editingSkill ? editingSkill.name : t('skills.title')}
              </h2>
              {editingSkill && (
                <span className="text-xs font-mono text-gray-400 dark:text-gray-500">{editingSkill.id}</span>
              )}
            </div>
            <div className="flex items-center gap-1">
              {!editingSkill && (
                <div className="relative" data-import-menu>
                  <button
                    onClick={() => setImportMenuOpen(prev => !prev)}
                    disabled={importing}
                    className={`p-1.5 text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded-lg transition-colors ${importing ? 'opacity-50 cursor-wait' : ''}`}
                    title={t('skills.importSkills')}
                  >
                    {importing ? (
                      <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                    ) : (
                      <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                      </svg>
                    )}
                  </button>
                  {importMenuOpen && (
                    <div className="absolute right-0 top-full mt-1 bg-white dark:bg-gray-800 rounded-xl shadow-lg border border-gray-200 dark:border-gray-700 py-1 z-10 min-w-[180px] animate-in fade-in zoom-in duration-150">
                      <button
                        onClick={() => zipInputRef.current?.click()}
                        className="w-full flex items-center gap-2.5 px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                      >
                        <svg className="w-4 h-4 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
                        </svg>
                        {t('skills.uploadZip')}
                      </button>
                      <button
                        onClick={handleGitImport}
                        className="w-full flex items-center gap-2.5 px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                      >
                        <svg className="w-4 h-4 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" />
                        </svg>
                        {t('skills.importGit')}
                      </button>
                    </div>
                  )}
                  <input
                    ref={zipInputRef}
                    type="file"
                    accept=".zip"
                    onChange={handleZipUpload}
                    className="hidden"
                  />
                </div>
              )}
              <button onClick={handleClose} className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>

          {/* Body */}
          {editingSkill ? renderEditor() : renderSkillList()}

          {/* Footer */}
          {!editingSkill && !loading && skills.length > 0 && (
            <div className="px-5 py-3 border-t border-gray-100 dark:border-gray-800 text-center flex-shrink-0">
              <span className="text-xs text-gray-400 dark:text-gray-500">
                {t('skills.footerCount', { enabled: skills.filter(s => s.enabled).length, total: skills.length })}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Child modals */}
      <InputModal
        isOpen={inputModal.open}
        onClose={() => setInputModal({ open: false })}
        onConfirm={inputModal.onConfirm}
        title={inputModal.title}
        placeholder={inputModal.placeholder}
        initialValue={inputModal.initial || ''}
        isNewFile={inputModal.isNewFile}
      />
      <ConfirmModal
        isOpen={confirmModal.open}
        onClose={() => setConfirmModal({ open: false })}
        onConfirm={confirmModal.onConfirm}
        title={confirmModal.title}
        message={confirmModal.message}
        danger
      />

      {/* Unsaved-changes confirmation */}
      <ConfirmModal
        isOpen={discardModal.open}
        onClose={() => resolveDiscard(false)}
        onConfirm={() => resolveDiscard(true)}
        title={t('skills.unsavedTitle')}
        message={discardModal.message}
      />

      {/* Validation / error dialog */}
      <ModalOverlay isOpen={errorModal.open} onClose={() => setErrorModal({ open: false, message: '' })}>
        <div className="p-8 text-center">
          <div className="mx-auto w-16 h-16 rounded-full flex items-center justify-center mb-6 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400">
            <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
          </div>
          <h2 className="text-2xl font-black text-gray-900 dark:text-gray-100 mb-2 tracking-tight">{t('skills.validationError')}</h2>
          <p className="text-gray-500 dark:text-gray-400 mb-8 leading-relaxed text-sm break-all">{errorModal.message}</p>
          <button onClick={() => setErrorModal({ open: false, message: '' })} className="w-full py-3.5 bg-sigma-600 text-white font-black rounded-2xl shadow-lg shadow-blue-200 dark:shadow-none hover:bg-sigma-700 transition-all active:scale-95">
            {t('common.ok')}
          </button>
        </div>
      </ModalOverlay>
    </>
  )

  // -----------------------------------------------------------------------
  // Sub-renders (defined after hooks to satisfy rules of hooks)
  // -----------------------------------------------------------------------

  function renderEditor() {
    return (
      <div className="flex flex-1 min-h-0">
        {/* File tree sidebar */}
        <div className="w-52 border-r border-gray-100 dark:border-gray-800 flex flex-col flex-shrink-0">
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-50 dark:border-gray-800">
            <span className="text-xs font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider">{t('skills.files')}</span>
            <div className="flex items-center gap-1">
              <button onClick={() => handleCreateNew('file')} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300" title={t('skills.newFileTitle')}>
                <IconPlus />
              </button>
              <button onClick={() => handleCreateNew('directory')} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded text-gray-400 dark:text-gray-500 hover:text-amber-600 dark:hover:text-amber-400" title={t('skills.newFolderTitle')}>
                <IconFolder />
              </button>
              <button onClick={() => fetchFiles(editingSkill.id)} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300" title={t('common.refresh')}>
                <IconRefresh />
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto py-1">
            {filesLoading ? (
              <div className="flex items-center justify-center py-8 text-gray-400 dark:text-gray-500">
                <svg className="w-4 h-4 animate-spin mr-2" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <span className="text-xs">{t('common.loading')}</span>
              </div>
            ) : tree.length === 0 ? (
              <div className="text-center py-8 text-xs text-gray-400 dark:text-gray-500">{t('skills.noFiles')}</div>
            ) : (
              tree.map(node => (
                <FileTreeNode
                  key={node.path}
                  node={node}
                  depth={0}
                  activePath={activeFilePath}
                  onSelect={handleFileSelect}
                  onAction={handleFileAction}
                  protectedPaths={protectedPaths}
                />
              ))
            )}
          </div>
        </div>

        {/* Editor area */}
        <div className="flex-1 flex flex-col min-w-0">
          {activeFilePath ? (
            <>
              <div className="flex items-center justify-between px-4 py-2 border-b border-gray-50 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-800/50">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono text-gray-500 dark:text-gray-400 truncate">{activeFilePath}</span>
                  {dirty && <span className="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0" title={t('skills.unsavedChanges')} />}
                </div>
                <div className="flex items-center gap-2">
                  {dirty && (
                    <button onClick={handleRevert} className="px-2.5 py-1 text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors">
                      {t('skills.revert')}
                    </button>
                  )}
                  <button
                    onClick={handleSave}
                    disabled={saving || !dirty}
                    className={`px-3 py-1 text-xs font-bold rounded-lg transition-all ${
                      dirty
                        ? 'bg-sigma-600 text-white hover:bg-sigma-700 shadow-sm'
                        : 'bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 cursor-not-allowed'
                    }`}
                  >
                    {saving ? t('common.saving') : t('common.save')}
                  </button>
                </div>
              </div>
              <div className="flex-1 min-h-0 overflow-hidden" ref={editorRef} />
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center text-gray-400 dark:text-gray-500">
              <div className="text-center">
                <svg className="w-8 h-8 mx-auto text-gray-300 dark:text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                </svg>
                <p className="text-sm mt-3 text-gray-500 dark:text-gray-400">{t('skills.selectFile')}</p>
              </div>
            </div>
          )}
        </div>
      </div>
    )
  }

  function renderSkillList() {
    return (
      <div className="flex-1 overflow-y-auto p-5">
        {/* Warning */}
        <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/50 rounded-xl px-4 py-3 mb-4 text-sm text-amber-800 dark:text-amber-300 leading-relaxed">
          <span className="font-semibold">{t('skills.note')}</span>{' '}
          {t('skills.noteDesc')}
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-12 text-gray-400 dark:text-gray-500">
            <svg className="w-5 h-5 animate-spin mr-2" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            {t('skills.loading')}
          </div>
        ) : skills.length === 0 ? (
          <div className="text-center py-12 text-gray-400 dark:text-gray-500">
            <svg className="w-10 h-10 mx-auto mb-3 text-gray-300 dark:text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
            </svg>
            <p className="text-sm font-medium text-gray-500 dark:text-gray-400">{t('skills.noSkills')}</p>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
              {t('skills.placeIn')} <code className="bg-gray-100 dark:bg-gray-800 px-1 rounded text-xs">userdata/.SiGMA/skill/&lt;skill-name&gt;/</code>
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {skills.map(skill => (
              <div
                key={skill.id}
                className={`flex items-start gap-3 p-3.5 rounded-xl border transition-colors ${
                  skill.enabled
                    ? 'bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
                    : 'bg-gray-50 dark:bg-gray-800/40 border-gray-100 dark:border-gray-800 opacity-60'
                }`}
              >
                <button
                  onClick={() => handleToggle(skill.id)}
                  disabled={togglingId === skill.id}
                  className={`mt-0.5 relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition-colors focus:outline-none ${
                    skill.enabled ? 'bg-sigma-600' : 'bg-gray-300 dark:bg-gray-600'
                  } ${togglingId === skill.id ? 'opacity-50 cursor-wait' : 'cursor-pointer'}`}
                >
                  <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow-sm transition-transform ${
                    skill.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'
                  }`} />
                </button>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-sm text-gray-900 dark:text-gray-100 truncate">{skill.name}</span>
                    <span className="text-xs text-gray-400 dark:text-gray-500 font-mono truncate">{skill.id}</span>
                  </div>
                  {skill.description && (
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 leading-relaxed line-clamp-2">{skill.description}</p>
                  )}
                </div>

                <div className="flex items-center gap-1.5 flex-shrink-0">
                  <button
                    onClick={() => handleEditSkill(skill)}
                    className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-sigma-600 dark:hover:text-sigma-400 hover:bg-sigma-50 dark:hover:bg-sigma-600/20 rounded-lg transition-colors"
                    title={t('skills.editFiles')}
                  >
                    <IconEdit />
                  </button>
                  <button
                    onClick={() => handleDeleteSkill(skill)}
                    className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors"
                    title={t('skills.deleteSkill')}
                  >
                    <IconTrash />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }
}
