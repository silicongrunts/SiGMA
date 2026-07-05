/**
 * AskUserQuestionModal — displays questions and collects user answers.
 *
 * Question types:
 * - single: radio options + Other (free text)
 * - multi:  checkbox options + Other (free text)
 * - text:   single free-text input
 *
 * State is keyed by question index (not header) to avoid key collisions.
 * Submits via streamInteractionRequest with interaction_response.
 */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'
import { useStore } from '../store/useStore'

export default function AskUserQuestionModal() {
  const pendingInteraction = useStore(s => s.pendingInteraction)
  const clearPendingInteraction = useStore(s => s.clearPendingInteraction)
  const interactionDismissed = useStore(s => s.interactionDismissed)
  const setInteractionDismissed = useStore(s => s.setInteractionDismissed)

  if (!pendingInteraction || pendingInteraction.type !== 'ask_user_question' || interactionDismissed) return null

  const { data, sessionId } = pendingInteraction
  // Robust parsing: questions might be an array, JSON string, or wrapped in data
  const rawQuestions = (() => {
    let rq = data?.questions || data
    if (typeof rq === 'string') {
      try { rq = JSON.parse(rq) } catch {}
    }
    return rq
  })()

  return (
    <AskUserQuestionDialog
      rawQuestions={rawQuestions}
      sessionId={sessionId}
      onClose={() => setInteractionDismissed(true)}
      onResolved={clearPendingInteraction}
    />
  )
}

function AskUserQuestionDialog({ rawQuestions, sessionId, onClose, onResolved }) {
  const { t } = useTranslation()
  const [answers, setAnswers] = useState({})       // [i]: single→label, multi→[labels], text→string
  const [otherActive, setOtherActive] = useState({}) // [i]: single/multi only
  const [otherTexts, setOtherTexts] = useState({})   // [i]: single/multi only
  const [submitting, setSubmitting] = useState(false)

  const questionList = Array.isArray(rawQuestions) ? rawQuestions
    : (rawQuestions?.questions ? rawQuestions.questions : [])

  // ── Option selection ──
  const handleSelect = (i, optionLabel, isMulti) => {
    if (isMulti) {
      setAnswers(prev => {
        const current = Array.isArray(prev[i]) ? prev[i] : []
        const idx = current.indexOf(optionLabel)
        if (idx >= 0) return { ...prev, [i]: current.filter(l => l !== optionLabel) }
        return { ...prev, [i]: [...current, optionLabel] }
      })
    } else {
      setAnswers(prev => ({ ...prev, [i]: optionLabel }))
      setOtherActive(prev => ({ ...prev, [i]: false }))
    }
  }

  // ── Other toggle ──
  const handleOtherToggle = (i, isMulti) => {
    if (isMulti) {
      setOtherActive(prev => ({ ...prev, [i]: !prev[i] }))
      if (!otherActive[i]) setOtherTexts(prev => ({ ...prev, [i]: '' }))
    } else {
      setOtherActive(prev => ({ ...prev, [i]: true }))
      setAnswers(prev => ({ ...prev, [i]: '' }))
    }
  }

  const handleOtherText = (i, value) => {
    setOtherTexts(prev => ({ ...prev, [i]: value }))
  }

  const handleText = (i, value) => {
    setAnswers(prev => ({ ...prev, [i]: value }))
  }

  const handleSubmit = async () => {
    const finalAnswers = questionList.map((q, i) => {
      let answer
      if (q.type === 'text') {
        answer = (answers[i] || '').trim()
      } else if (q.type === 'multi') {
        const sel = Array.isArray(answers[i]) ? [...answers[i]] : []
        if (otherActive[i] && otherTexts[i]) sel.push(otherTexts[i])
        answer = sel
      } else { // single (default)
        answer = otherActive[i] ? (otherTexts[i] || '') : (answers[i] || '')
      }
      return { question: q.question, answer }
    })
    setSubmitting(true)
    try {
      // Signal ChatPanel to start a new SSE stream with interaction response
      useStore.getState().setStreamInteractionRequest({
        message: '',
        resume: true,
        session_id: sessionId,
        interaction_response: { answers: finalAnswers },
      })
      onResolved()
    } catch (e) {
      console.error('Failed to submit response:', e)
      setSubmitting(false)
    }
  }

  const canSubmit = questionList.length > 0 && questionList.every((q, i) => {
    if (q.type === 'text') return !!(answers[i] || '').trim()
    const sel = Array.isArray(answers[i]) ? answers[i] : (answers[i] ? [answers[i]] : [])
    return sel.length > 0 || (otherActive[i] && otherTexts[i])
  })

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onClose} />
      <div className="relative bg-white dark:bg-gray-900 rounded-3xl shadow-2xl max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto overflow-x-hidden p-8 animate-in zoom-in duration-300">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-lg font-bold text-gray-800 dark:text-gray-100">{t('question.title')}</h2>
          <button onClick={onClose} className="p-1.5 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors">
            <X className="w-5 h-5 text-gray-400 dark:text-gray-500" />
          </button>
        </div>

        <div className="space-y-6">
          {questionList.map((q, i) => (
            <QuestionBlock key={i} index={i} question={q}
              selected={answers[i]}
              otherActive={otherActive[i] || false}
              otherText={otherTexts[i] || ''}
              onSelect={handleSelect}
              onOtherToggle={handleOtherToggle}
              onOtherText={handleOtherText}
              onText={handleText} />
          ))}
        </div>

        <div className="flex justify-end gap-3 mt-8 pt-6 border-t border-gray-100 dark:border-gray-800">
          <button onClick={onClose} className="px-4 py-2 text-sm font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors">
            {t('common.cancel')}
          </button>
          <button onClick={handleSubmit} disabled={!canSubmit || submitting}
            className="px-6 py-2 bg-sigma-600 text-white text-sm font-semibold rounded-xl hover:bg-sigma-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all">
            {submitting ? t('common.sending') : t('common.submit')}
          </button>
        </div>
      </div>
    </div>
  )
}

function QuestionBlock({ index, question, selected, otherActive, otherText, onSelect, onOtherToggle, onOtherText, onText }) {
  const { t } = useTranslation()

  // text type: single free-text input, no Other
  if (question.type === 'text') {
    return (
      <div>
        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-3 break-words">{question.question}</h3>
        <input type="text"
          value={selected || ''}
          onChange={e => onText(index, e.target.value)}
          placeholder={t('question.otherPlaceholder')}
          className="w-full text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 focus:outline-none focus:border-sigma-400" />
      </div>
    )
  }

  const isMulti = question.type === 'multi'
  const Input = isMulti ? 'checkbox' : 'radio'
  const name = `q${index}`
  // Normalize selected for multi: always an array of labels
  const selectedLabels = isMulti ? (Array.isArray(selected) ? selected : []) : (selected ? [selected] : [])

  return (
    <div>
      <div className="flex items-start gap-2 mb-3">
        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 min-w-0 break-words">{question.question}</h3>
        {isMulti && <span className="text-[9px] text-gray-400 dark:text-gray-500 flex-shrink-0 whitespace-nowrap">{t('common.multiSelect')}</span>}
      </div>

      <div className="space-y-1.5 ml-1">
        {(question.options || []).map((opt, oi) => {
          const isSelected = selectedLabels.includes(opt.label)
          return (
            <label key={oi}
              className={`flex items-start gap-2.5 px-3 py-2 rounded-xl cursor-pointer transition-all border ${
                isSelected ? 'bg-sigma-50 dark:bg-sigma-600/20 border-sigma-200 dark:border-sigma-600/50' : 'border-transparent hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}>
              <input type={Input} name={name}
                checked={isSelected}
                onChange={() => onSelect(index, opt.label, isMulti)}
                className="mt-0.5 accent-sigma-600" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-gray-700 dark:text-gray-300 break-words">
                  {opt.label}
                  {opt.recommended && <span className="text-[10px] text-sigma-500 ml-1 whitespace-nowrap">{t('common.recommended')}</span>}
                </div>
                <div className="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5 break-words">{opt.description}</div>
              </div>
            </label>
          )
        })}

        {/* "Other" — uses independent otherActive state, not answers */}
        <label className={`flex items-start gap-2.5 px-3 py-2 rounded-xl cursor-pointer transition-all border ${
          otherActive ? 'bg-sigma-50 dark:bg-sigma-600/20 border-sigma-200 dark:border-sigma-600/50' : 'border-transparent hover:bg-gray-50 dark:hover:bg-gray-800'
        }`}>
          <input type={Input} name={name}
            checked={otherActive}
            onChange={() => onOtherToggle(index, isMulti)}
            className="mt-0.5 accent-sigma-600" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('question.other')}</div>
            {otherActive && (
              <input type="text"
                value={otherText}
                onChange={e => onOtherText(index, e.target.value)}
                placeholder={t('question.otherPlaceholder')}
                className="mt-1 w-full text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-2 py-1 focus:outline-none focus:border-sigma-400"
                autoFocus />
            )}
          </div>
        </label>
      </div>
    </div>
  )
}
