/**
 * PlanApprovalDialog — displays a plan for user approval.
 *
 * Triggered when the plan agent calls submit_plan_for_approval.
 * - Shows markdown plan content
 * - Approve / Revise (with feedback) / Cancel buttons
 * - Submits response via streamInteractionRequest
 */
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, X } from 'lucide-react'
import { MarkdownContent } from './ChatShared'
import { useStore } from '../store/useStore'

export default function PlanApprovalDialog() {
  const pendingInteraction = useStore(s => s.pendingInteraction)
  const clearPendingInteraction = useStore(s => s.clearPendingInteraction)
  const interactionDismissed = useStore(s => s.interactionDismissed)
  const setInteractionDismissed = useStore(s => s.setInteractionDismissed)
  const currentProject = useStore(s => s.currentProject)

  if (!pendingInteraction || pendingInteraction.type !== 'submit_plan_for_approval' || interactionDismissed) return null
  if (pendingInteraction.projectId && pendingInteraction.projectId !== currentProject?.id) return null

  const { data, sessionId } = pendingInteraction
  const projectId = pendingInteraction.projectId || currentProject?.id
  const taskId = data?.task_id
  const interactionId = data?.interaction_id
  const interactionType = data?.interaction_type
  const planContent = data?.plan_content || ''

  return (
    // task_id is unique per parked interaction — the stable identity that
    // forces a remount (fresh respond guard) when a new plan arrives.
    <PlanDialog
      key={`${taskId}:${interactionId}`}
      planContent={planContent}
      sessionId={sessionId}
      projectId={projectId}
      taskId={taskId}
      interactionId={interactionId}
      interactionType={interactionType}
      onClose={() => setInteractionDismissed(true)}
      onResolved={clearPendingInteraction}
    />
  )
}

function PlanDialog({ planContent, sessionId, projectId, taskId, interactionId, interactionType, onClose, onResolved }) {
  const { t } = useTranslation()
  const [feedback, setFeedback] = useState('')
  const [showFeedback, setShowFeedback] = useState(false)
  // Same guard as the permission dialog: a missing session is an error the
  // user can fix (the dialog stays open), not a consumed response.
  const [submitError, setSubmitError] = useState('')
  // The handoff is fire-and-forget and the dialog unmounts as soon as the
  // store consumes the request — a second click in the same frame must not
  // enqueue a second response.
  const respondedRef = useRef(false)

  const respond = approved => {
    if (respondedRef.current) return
    setSubmitError('')
    if (!sessionId) {
      setSubmitError(t('permission.respondFailed'))
      return
    }
    respondedRef.current = true
    useStore.getState().setStreamInteractionRequest({
      message: '',
      resume: true,
      projectId,
      session_id: sessionId,
      task_id: taskId,
      interaction_response: {
        task_id: taskId,
        interaction_id: interactionId,
        interaction_type: interactionType || 'submit_plan_for_approval',
        ...(approved ? { approved: true } : { approved: false, feedback }),
      },
    })
    onResolved()
  }

  const handleApprove = () => respond(true)

  const handleRevise = () => {
    if (!showFeedback) {
      setShowFeedback(true)
      return
    }
    respond(false)
  }

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" />
      <div className="relative bg-white dark:bg-gray-900 rounded-3xl shadow-2xl max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto p-8 animate-in zoom-in duration-300">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-lg font-bold text-gray-800 dark:text-gray-100">{t('plan.title')}</h2>
          <button onClick={onClose} className="p-1.5 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors">
            <X className="w-5 h-5 text-gray-400 dark:text-gray-500" />
          </button>
        </div>

        {/* Plan content */}
        <div className="bg-gray-50 dark:bg-gray-800 rounded-2xl p-6 mb-6 max-h-96 overflow-y-auto">
          <MarkdownContent content={planContent} />
        </div>

        {/* Feedback textarea */}
        {showFeedback && (
          <div className="mb-6">
            <label className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2 block">
              {t('plan.feedbackLabel')}
            </label>
            <textarea
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              placeholder={t('plan.feedbackPlaceholder')}
              className="w-full h-24 text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-4 py-3 focus:outline-none focus:border-sigma-400 resize-none"
              autoFocus
            />
          </div>
        )}

        {/* Submit error */}
        {submitError && (
          <div className="mb-6 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50 text-red-600 dark:text-red-300 text-xs flex items-center gap-2">
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
            <span className="break-words min-w-0">{submitError}</span>
          </div>
        )}

        {/* Footer */}
        <div className="flex justify-end gap-3 pt-6 border-t border-gray-100 dark:border-gray-800">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleRevise}
            className="px-5 py-2 text-sm font-semibold text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 rounded-xl hover:bg-amber-100 dark:hover:bg-amber-900/30 transition-all"
          >
            {showFeedback ? t('plan.reviseSubmit') : t('plan.revise')}
          </button>
          <button
            onClick={handleApprove}
            className="px-6 py-2 bg-sigma-600 text-white text-sm font-semibold rounded-xl hover:bg-sigma-700 transition-all"
          >
            {t('plan.approve')}
          </button>
        </div>
      </div>
    </div>
  )
}
