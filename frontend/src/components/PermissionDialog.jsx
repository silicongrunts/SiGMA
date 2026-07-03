/**
 * PermissionDialog — modal asking user to approve/deny a write operation
 * outside the project sandbox.
 *
 * Shows tool name, target path, and content preview.
 * The user's response is sent to the backend which relays it to the worker.
 *
 * Auto-approve mode: per-tool-type toggle. When enabled, dialog shows a
 * 3-second countdown and auto-approves unless the user disables it.
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { ShieldAlert, FileText, Check, X, Timer, ShieldOff, Loader2, AlertTriangle } from 'lucide-react'
import { useStore } from '../store/useStore'
import { permissionsAPI } from '../api'

const AUTO_APPROVE_SECONDS = 3

export default function PermissionDialog() {
  const pendingPermission = useStore(s => s.pendingPermission)
  const currentProject = useStore(s => s.currentProject)
  const autoApproveSettings = useStore(s => s.autoApproveSettings)
  const clearPendingPermission = useStore(s => s.clearPendingPermission)
  const setAutoApproveType = useStore(s => s.setAutoApproveType)

  if (!pendingPermission) return null

  const { task_id, request_id, tool, path, operation, content, description } = pendingPermission
  const isAutoApproved = autoApproveSettings[tool] === true

  const handleToggleAutoApprove = (toolType, enabled) => {
    if (currentProject) {
      setAutoApproveType(currentProject.id, toolType, enabled)
    }
  }

  return (
    <PermissionPrompt
      key={request_id}
      projectId={currentProject?.id}
      taskId={task_id}
      requestId={request_id}
      tool={tool}
      path={path}
      operation={operation}
      content={content || ''}
      description={description || ''}
      onResolved={clearPendingPermission}
      isAutoApproved={isAutoApproved}
      onToggleAutoApprove={handleToggleAutoApprove}
    />
  )
}

function PermissionPrompt({
  projectId, taskId, requestId, tool, path, operation, content, description,
  onResolved, isAutoApproved, onToggleAutoApprove
}) {
  const { t } = useTranslation()
  const [responding, setResponding] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [showDenyInput, setShowDenyInput] = useState(false)
  const [denyReason, setDenyReason] = useState('')

  // auto-approve: 'idle' | 'countdown' | 'disabled'
  const [autoMode, setAutoMode] = useState(() => isAutoApproved ? 'countdown' : 'idle')
  const [countdown, setCountdown] = useState(AUTO_APPROVE_SECONDS)
  const countdownRef = useRef(null)

  const handleRespond = useCallback(async (approved, reason = '') => {
    setResponding(true)
    setSubmitError('')
    setAutoMode(prev => prev === 'countdown' ? 'disabled' : prev)
    if (countdownRef.current) {
      clearInterval(countdownRef.current)
      countdownRef.current = null
    }
    try {
      await permissionsAPI.respond(projectId, taskId, { request_id: requestId, approved, reason })
    } catch (e) {
      setSubmitError(t('permission.respondFailed'))
      setResponding(false)
      return
    }
    onResolved()
  }, [projectId, taskId, requestId, onResolved, t])

  // Countdown auto-approve
  useEffect(() => {
    if (autoMode !== 'countdown') return

    let seconds = AUTO_APPROVE_SECONDS
    setCountdown(seconds)

    countdownRef.current = setInterval(() => {
      seconds -= 1
      if (seconds <= 0) {
        clearInterval(countdownRef.current)
        countdownRef.current = null
        handleRespond(true)
      } else {
        setCountdown(seconds)
      }
    }, 1000)

    return () => {
      if (countdownRef.current) {
        clearInterval(countdownRef.current)
        countdownRef.current = null
      }
    }
  }, [autoMode, handleRespond])

  const handleDenyClick = () => {
    if (!showDenyInput) {
      setShowDenyInput(true)
      return
    }
    handleRespond(false, denyReason.trim())
  }

  const handleDenyKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleRespond(false, denyReason.trim())
    }
  }

  const handleDisableAutoApprove = () => {
    if (countdownRef.current) {
      clearInterval(countdownRef.current)
      countdownRef.current = null
    }
    onToggleAutoApprove(tool, false)
    setAutoMode('disabled')
  }

  const MAX_PREVIEW = 800
  const previewContent = content.length > MAX_PREVIEW
    ? content.slice(0, MAX_PREVIEW) + '\n...'
    : content

  const isCounting = autoMode === 'countdown'
  const showNormalDeny = autoMode === 'idle' || autoMode === 'disabled'

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" />
      <div className="relative bg-white dark:bg-gray-900 rounded-3xl shadow-2xl max-w-xl w-full mx-4 max-h-[85vh] overflow-hidden flex flex-col animate-in zoom-in duration-300">
        {/* Header */}
        <div className="bg-amber-50 dark:bg-amber-900/20 border-b border-amber-100 dark:border-amber-800/50 px-6 py-4 flex items-center gap-3 flex-shrink-0">
          <div className="w-10 h-10 rounded-full bg-amber-100 dark:bg-amber-900/40 flex items-center justify-center flex-shrink-0">
            {isCounting
              ? <Timer className="w-5 h-5 text-amber-600 dark:text-amber-400" />
              : <ShieldAlert className="w-5 h-5 text-amber-600 dark:text-amber-400" />
            }
          </div>
          <div>
            <h2 className="text-sm font-bold text-amber-800 dark:text-amber-300">{t('permission.title')}</h2>
            <p className="text-xs text-amber-600 dark:text-amber-400/90 mt-0.5">
              {tool === 'bash'
                ? t('permission.bashDesc')
                : tool === 'notebook_run_cell'
                  ? t('permission.notebookDesc')
                  : t('permission.fileDesc', { operation })
              }
            </p>
          </div>
        </div>

        {/* Details */}
        <div className="px-6 py-5 space-y-4 overflow-y-auto flex-1">
          {/* Tool */}
          <div>
            <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.tool')}</div>
            <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">{tool}</div>
          </div>

          {/* Description */}
          {description && (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.description')}</div>
              <div className="text-sm text-gray-700 dark:text-gray-300 break-words">{description}</div>
            </div>
          )}

          {/* Target Path / Notebook */}
          {tool === 'notebook_run_cell' ? (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.notebook')}</div>
              <div className="flex items-start gap-2 bg-gray-50 dark:bg-gray-800 rounded-xl px-3 py-2.5">
                <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 flex-shrink-0" />
                <code className="text-xs text-gray-700 dark:text-gray-300 break-all font-mono leading-relaxed">{path}</code>
              </div>
            </div>
          ) : tool !== 'bash' && (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{t('permission.targetPath')}</div>
              <div className="flex items-start gap-2 bg-gray-50 dark:bg-gray-800 rounded-xl px-3 py-2.5">
                <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 flex-shrink-0" />
                <code className="text-xs text-gray-700 dark:text-gray-300 break-all font-mono leading-relaxed">{path}</code>
              </div>
            </div>
          )}

          {/* Content */}
          {previewContent && (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:text-gray-500 mb-1.5">{tool === 'notebook_run_cell' ? t('permission.code') : t('permission.content')}</div>
              <pre className="bg-gray-900 text-gray-100 rounded-xl px-4 py-3 text-xs font-mono leading-relaxed whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
                {previewContent}
              </pre>
            </div>
          )}
        </div>

        {/* Auto-approve checkbox (normal mode only) */}
        {!isCounting && autoMode === 'idle' && (
          <div className="px-6 py-3 border-t border-gray-100 dark:border-gray-800 flex-shrink-0">
            <label className="flex items-center gap-2 cursor-pointer select-none group">
              <input
                type="checkbox"
                onChange={(e) => onToggleAutoApprove(tool, e.target.checked)}
                className="w-3.5 h-3.5 rounded border-gray-300 dark:border-gray-600 text-amber-500 focus:ring-amber-400 focus:ring-1"
              />
              <span className="text-xs text-gray-400 dark:text-gray-500 group-hover:text-gray-600 dark:group-hover:text-gray-300 transition-colors">
                {t('permission.autoApprove', { tool })}
              </span>
            </label>
          </div>
        )}

        {/* Deny reason input */}
        {showDenyInput && (
          <div className="px-6 py-3 border-t border-gray-100 dark:border-gray-800 flex-shrink-0">
            <input
              type="text"
              value={denyReason}
              onChange={(e) => setDenyReason(e.target.value)}
              onKeyDown={handleDenyKeyDown}
              placeholder={t('permission.denyReasonPlaceholder')}
              className="w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:border-amber-400 focus:ring-1 focus:ring-amber-400 placeholder:text-gray-300 dark:placeholder:text-gray-600"
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
          {isCounting ? (
            /* Countdown mode — disable auto-approve */
            <button
              onClick={handleDisableAutoApprove}
              disabled={responding}
              className="flex-1 flex items-center justify-center gap-2 py-3.5 text-sm font-semibold text-gray-400 dark:text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-600 dark:hover:text-gray-300 transition-all disabled:opacity-40"
            >
              <ShieldOff className="w-4 h-4" />
              {t('permission.disableAutoApprove')}
            </button>
          ) : (
            /* Normal Deny button */
            <button
              onClick={handleDenyClick}
              disabled={responding}
              className="flex-1 flex items-center justify-center gap-2 py-3.5 text-sm font-semibold text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-200 transition-all disabled:opacity-40"
            >
              {responding ? <Loader2 className="w-4 h-4 animate-spin" /> : <X className="w-4 h-4" />}
              {showDenyInput ? (responding ? t('common.sending') : t('permission.confirmDeny')) : t('permission.deny')}
            </button>
          )}
          <button
            onClick={() => handleRespond(true)}
            disabled={responding}
            className="flex-1 flex items-center justify-center gap-2 py-3.5 text-sm font-semibold text-amber-600 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20 hover:text-amber-700 dark:hover:text-amber-300 transition-all disabled:opacity-40"
          >
            {responding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            {responding ? t('common.sending') : isCounting ? t('permission.allowCountdown', { count: countdown }) : t('permission.allow')}
          </button>
        </div>
      </div>
    </div>
  )
}
