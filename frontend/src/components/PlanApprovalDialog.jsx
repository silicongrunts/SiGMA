/**
 * PlanApprovalDialog — displays a plan for user approval.
 *
 * Triggered when the plan agent calls submit_plan_for_approval.
 * - Shows markdown plan content
 * - Approve / Revise (with feedback) / Cancel buttons
 * - Submits response via streamInteractionRequest
 */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'
import { MarkdownContent } from './ChatShared'
import { useStore } from '../store/useStore'

export default function PlanApprovalDialog() {
  const pendingInteraction = useStore(s => s.pendingInteraction)
  const clearPendingInteraction = useStore(s => s.clearPendingInteraction)
  const interactionDismissed = useStore(s => s.interactionDismissed)
  const setInteractionDismissed = useStore(s => s.setInteractionDismissed)

  if (!pendingInteraction || pendingInteraction.type !== 'submit_plan_for_approval' || interactionDismissed) return null

  const { data, sessionId } = pendingInteraction
  const taskId = data?.task_id
  const planContent = data?.plan_content || ''

  return (
    <PlanDialog
      planContent={planContent}
      sessionId={sessionId}
      taskId={taskId}
      onClose={() => setInteractionDismissed(true)}
      onResolved={clearPendingInteraction}
    />
  )
}

function PlanDialog({ planContent, sessionId, taskId, onClose, onResolved }) {
  const { t } = useTranslation()
  const [feedback, setFeedback] = useState('')
  const [showFeedback, setShowFeedback] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const handleApprove = async () => {
    setSubmitting(true)
    try {
      useStore.getState().setStreamInteractionRequest({
        message: '',
        resume: true,
        session_id: sessionId,
        task_id: taskId,
        interaction_response: { approved: true },
      })
      onResolved()
    } catch (e) {
      console.error('Failed to approve:', e)
      setSubmitting(false)
    }
  }

  const handleRevise = async () => {
    if (!showFeedback) {
      setShowFeedback(true)
      return
    }
    setSubmitting(true)
    try {
      useStore.getState().setStreamInteractionRequest({
        message: '',
        resume: true,
        session_id: sessionId,
        task_id: taskId,
        interaction_response: { approved: false, feedback },
      })
      onResolved()
    } catch (e) {
      console.error('Failed to reject:', e)
      setSubmitting(false)
    }
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
            disabled={submitting}
            className="px-5 py-2 text-sm font-semibold text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 rounded-xl hover:bg-amber-100 dark:hover:bg-amber-900/30 disabled:opacity-40 transition-all"
          >
            {showFeedback ? t('plan.reviseSubmit') : t('plan.revise')}
          </button>
          <button
            onClick={handleApprove}
            disabled={submitting}
            className="px-6 py-2 bg-sigma-600 text-white text-sm font-semibold rounded-xl hover:bg-sigma-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
          >
            {submitting ? t('plan.approving') : t('plan.approve')}
          </button>
        </div>
      </div>
    </div>
  )
}
