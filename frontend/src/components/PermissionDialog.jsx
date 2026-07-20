/**
 * PermissionDialog — modal asking the user to approve/deny an operation
 * belonging to one of the four permission categories (file_external,
 * file_internal, bash, notebook).
 *
 * Shows the category, target path, and content preview. The user's response is
 * sent via the chat resume path (POST /chat/stream with resume=true), which
 * spawns a new worker task carrying the approval/denial. Auto-approve is
 * configured separately in the ChatPanel settings menu (persisted to the
 * backend), so this dialog only handles single-shot approval.
 */
import { useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { ShieldAlert, FileText, Check, X, Loader2, AlertTriangle } from 'lucide-react'
import { useStore } from '../store/useStore'
import { permissionsAPI } from '../api'
import { toastError } from './Toast'
import DiffView from './DiffView'

export default function PermissionDialog() {
  const pendingPermission = useStore(s => s.pendingPermission)
  const currentProject = useStore(s => s.currentProject)
  const clearPendingPermission = useStore(s => s.clearPendingPermission)
  const autoApproveSettings = useStore(s => s.autoApproveSettings)
  const setAutoApproveType = useStore(s => s.setAutoApproveType)

  if (!pendingPermission) return null

  const { session_id, tool, tool_name, path, operation, content, description, diff_lines, diff_truncated } = pendingPermission
  const isAutoApproved = autoApproveSettings[tool] === true

  return (
    <PermissionPrompt
      key={session_id + (path || '') + (operation || '') + (tool_name || '') + (diff_lines?.length ?? 0)}
      projectId={currentProject?.id}
      sessionId={session_id}
      tool={tool}
      toolName={tool_name || tool}
      path={path}
      operation={operation}
      content={content || ''}
      description={description || ''}
      diffLines={diff_lines}
      diffTruncated={!!diff_truncated}
      onResolved={clearPendingPermission}
      isAutoApproved={isAutoApproved}
      onToggleAutoApprove={(enabled) => setAutoApproveType(tool, enabled)}
    />
  )
}

function PermissionPrompt({
  projectId, sessionId, tool, toolName, path, operation, content, description,
  diffLines, diffTruncated,
  onResolved, isAutoApproved, onToggleAutoApprove,
}) {
  const { t } = useTranslation()
  const [responding, setResponding] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [showDenyInput, setShowDenyInput] = useState(false)
  const [denyReason, setDenyReason] = useState('')
  const [autoApproveSaving, setAutoApproveSaving] = useState(false)

  const hasDiff = Array.isArray(diffLines) && diffLines.length > 0
  const modalMaxW = hasDiff ? 'max-w-4xl' : 'max-w-xl'

  const handleToggleAutoApprove = async (checked) => {
    setAutoApproveSaving(true)
    try {
      await permissionsAPI.setAutoApprove(projectId, { category: tool, enabled: checked })
      onToggleAutoApprove(checked)
    } catch (e) {
      toastError(t('permission.toggleFailed'))
    } finally {
      setAutoApproveSaving(false)
    }
  }

  const handleRespond = useCallback(async (approved, reason = '') => {
    setResponding(true)
    setSubmitError('')
    if (!sessionId) {
      setSubmitError(t('permission.respondFailed'))
      setResponding(false)
      return
    }
    // Submit via the chat resume path — spawns a new worker task carrying
    // the approval/denial, which resumes the paused loop.
    useStore.getState().setStreamInteractionRequest({
      message: '',
      resume: true,
      session_id: sessionId,
      interaction_response: { approved, reason },
    })
    onResolved()
  }, [sessionId, onResolved, t])

  const handleDenyClick = () => {
    if (!showDenyInput) {
      setShowDenyInput(true)
      return
    }
    handleRespond(false, denyReason.trim())
  }

  // When a deny reason is entered, the Allow action is disabled to avoid an
  // ambiguous response — clear the reason to re-enable it.
  const hasDenyReason = denyReason.trim() !== ''

  // Category drives the header description and which detail fields render.
  const isBash = tool === 'bash'
  const isNotebook = tool === 'notebook'
  const isFileInternal = tool === 'file_internal'
  const categoryLabel = isBash
    ? t('permission.cat.bash')
    : isNotebook
      ? t('permission.cat.notebook')
      : isFileInternal
        ? t('permission.cat.fileInternal')
        : t('permission.cat.fileExternal')

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" />
      <div className={`relative bg-white dark:bg-gray-900 rounded-3xl shadow-2xl ${modalMaxW} w-full mx-4 max-h-[85vh] overflow-hidden flex flex-col animate-in zoom-in duration-300`}>
        {/* Header */}
        <div className="bg-amber-50 dark:bg-amber-900/20 border-b border-amber-100 dark:border-amber-800/50 px-6 py-4 flex items-center gap-3 flex-shrink-0">
          <div className="w-10 h-10 rounded-full bg-amber-100 dark:bg-amber-900/40 flex items-center justify-center flex-shrink-0">
            <ShieldAlert className="w-5 h-5 text-amber-600 dark:text-amber-400" />
          </div>
          <div>
            <h2 className="text-sm font-bold text-amber-800 dark:text-amber-300">{t('permission.title')}</h2>
            <p className="text-xs text-amber-600 dark:text-amber-400/90 mt-0.5">
              {isBash
                ? t('permission.bashDesc')
                : isNotebook
                  ? t('permission.notebookDesc')
                  : isFileInternal
                    ? t('permission.fileInternalDesc')
                    : t('permission.fileExternalDesc')}
            </p>
          </div>
        </div>

        {/* Details */}
        <div className="px-6 py-5 space-y-4 overflow-y-auto flex-1">
          {/* Tool */}
          <div>
            <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.tool')}</div>
            <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">{toolName}</div>
          </div>

          {/* Description */}
          {description && (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.description')}</div>
              <div className="text-sm text-gray-700 dark:text-gray-300 break-words">{description}</div>
            </div>
          )}

          {/* Target Path / Notebook */}
          {isNotebook ? (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.notebook')}</div>
              <div className="flex items-start gap-2 bg-gray-50 dark:bg-gray-800 rounded-xl px-3 py-2.5">
                <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 flex-shrink-0" />
                <code className="text-xs text-gray-700 dark:text-gray-300 break-all font-mono leading-relaxed">{path}</code>
              </div>
            </div>
          ) : !isBash && (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.targetPath')}</div>
              <div className="flex items-start gap-2 bg-gray-50 dark:bg-gray-800 rounded-xl px-3 py-2.5">
                <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 flex-shrink-0" />
                <code className="text-xs text-gray-700 dark:text-gray-300 break-all font-mono leading-relaxed">{path}</code>
              </div>
            </div>
          )}

          {/* Content */}
          {hasDiff ? (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.content')}</div>
              <div className="border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden">
                <DiffView
                  lines={diffLines}
                  leftLabel={t('permission.diffBefore')}
                  rightLabel={t('permission.diffAfter')}
                  maxH="max-h-[50vh]"
                />
              </div>
              {diffTruncated && (
                <div className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400 flex items-center gap-1">
                  <AlertTriangle className="w-3 h-3 flex-shrink-0" />
                  <span>{t('permission.diffTruncated')}</span>
                </div>
              )}
            </div>
          ) : content && (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{isNotebook ? t('permission.code') : t('permission.content')}</div>
              <pre className="bg-gray-900 text-gray-100 rounded-xl px-4 py-3 text-xs font-mono leading-relaxed whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
                {content}
              </pre>
            </div>
          )}
        </div>

        {/* Auto-approve toggle */}
        <div className="px-6 py-3 border-t border-gray-100 dark:border-gray-800 flex-shrink-0">
          <label className="flex items-center gap-2 cursor-pointer select-none group">
            <input
              type="checkbox"
              checked={isAutoApproved}
              disabled={autoApproveSaving}
              onChange={(e) => handleToggleAutoApprove(e.target.checked)}
              className="w-3.5 h-3.5 rounded border-gray-300 dark:border-gray-600 text-amber-500 focus:ring-amber-400 focus:ring-1"
            />
            {autoApproveSaving
              ? <Loader2 className="w-3 h-3 text-gray-400 animate-spin" />
              : <span className="text-xs text-gray-400 dark:text-gray-500 group-hover:text-gray-600 dark:group-hover:text-gray-300 transition-colors">
                  {isAutoApproved
                    ? t('permission.autoApproveOnHint')
                    : t('permission.autoApproveCategory', { category: categoryLabel })}
                </span>
            }
          </label>
        </div>

        {/* Deny reason input */}
        {showDenyInput && (
          <div className="px-6 py-3 border-t border-gray-100 dark:border-gray-800 flex-shrink-0">
            <textarea
              value={denyReason}
              onChange={(e) => setDenyReason(e.target.value)}
              placeholder={t('permission.denyReasonPlaceholder')}
              rows={3}
              className="w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:border-amber-400 focus:ring-1 focus:ring-amber-400 placeholder:text-gray-300 dark:placeholder:text-gray-600 resize-none overflow-y-auto whitespace-pre-wrap break-words max-h-[200px]"
              autoFocus
            />
          </div>
        )}

        {/* Submit error */}
        {submitError && (
          <div className="mx-6 my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50 text-red-600 dark:text-red-300 text-xs flex items-center gap-2">
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
            <span className="break-words min-w-0">{submitError}</span>
          </div>
        )}

        {/* Actions */}
        <div className="flex border-t border-gray-100 dark:border-gray-800 divide-x divide-gray-100 dark:divide-gray-800 flex-shrink-0">
          <button
            onClick={handleDenyClick}
            disabled={responding}
            className="flex-1 flex items-center justify-center gap-2 py-3.5 text-sm font-semibold text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-200 transition-all disabled:opacity-40"
          >
            {responding ? <Loader2 className="w-4 h-4 animate-spin" /> : <X className="w-4 h-4" />}
            {showDenyInput ? (responding ? t('common.sending') : t('permission.confirmDeny')) : t('permission.deny')}
          </button>
          <button
            onClick={() => handleRespond(true)}
            disabled={responding || hasDenyReason}
            className="flex-1 flex items-center justify-center gap-2 py-3.5 text-sm font-semibold text-amber-600 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20 hover:text-amber-700 dark:hover:text-amber-300 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {responding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            {responding ? t('common.sending') : t('permission.allow')}
          </button>
        </div>
      </div>
    </div>
  )
}
