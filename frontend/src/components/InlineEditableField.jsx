import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Pencil, Check, X } from 'lucide-react'

/**
 * Inline-editable field with label, edit/save/cancel.
 * Pure UI — the consumer provides *value*, *onSave*, and optional *multiline*.
 *
 * @param {Object}   props
 * @param {string}   props.label      Field label.
 * @param {string}   props.value      Current display value.
 * @param {Function} props.onSave     Called with the new string on save.
 * @param {boolean}  [props.multiline] Use textarea instead of input.
 * @param {string}   [props.type]     Input type (default "text").
 * @param {boolean}  [props.aiHighlight] Apply shimmer animation when true.
 * @param {boolean}  [props.markdown]  Render value as markdown (requires MarkdownContent).
 * @param {Function} [props.renderValue] Custom render function for display value.
 */
export function InlineEditableField({
  label, value, onSave,
  multiline = false, type = 'text',
  aiHighlight = false, markdown = false,
  renderValue,
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value || '')

  const startEdit = () => { setDraft(value || ''); setEditing(true) }
  const cancel = () => { setEditing(false); setDraft(value || '') }
  const save = () => {
    onSave(draft)
    setEditing(false)
  }
  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !multiline) { e.preventDefault(); save() }
    if (e.key === 'Escape') cancel()
  }

  if (editing) {
    return (
      <div className="group">
        <div className="flex items-center gap-1.5 mb-1">
          <span className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider">{label}</span>
          <button onClick={save} className="p-0.5 text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/20 rounded transition-colors"><Check className="w-3 h-3" /></button>
          <button onClick={cancel} className="p-0.5 text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"><X className="w-3 h-3" /></button>
        </div>
        {multiline ? (
          <textarea
            autoFocus
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === 'Escape') cancel() }}
            rows={Math.max(8, draft.split('\n').length)}
            className="w-full px-3 py-2 border border-sigma-300 dark:border-sigma-600/60 rounded-lg text-sm font-mono leading-relaxed focus:ring-2 focus:ring-sigma-600/20 focus:border-sigma-600 outline-none resize-y bg-white dark:bg-gray-800 dark:text-gray-200"
          />
        ) : (
          <input
            autoFocus
            type={type}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            className="w-full px-3 py-1.5 border border-sigma-300 dark:border-sigma-600/60 rounded-lg text-sm focus:ring-2 focus:ring-sigma-600/20 focus:border-sigma-600 outline-none bg-white dark:bg-gray-800 dark:text-gray-200"
          />
        )}
      </div>
    )
  }

  const highlightStyle = aiHighlight ? {
    background: 'linear-gradient(90deg, rgba(191,219,254,0.3), rgba(221,214,254,0.3), rgba(191,219,254,0.3))',
    backgroundSize: '200% 100%',
    animation: 'aiShimmer 2s linear infinite',
    borderRadius: '8px',
  } : {}

  return (
    <div className="group" style={highlightStyle}>
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-[10px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider">{label}</span>
        <button
          onClick={startEdit}
          className="p-0.5 text-transparent group-hover:text-gray-400 dark:group-hover:text-gray-500 hover:!text-sigma-600 rounded transition-colors"
          title={t('inline.editLabel', { label })}
        >
          <Pencil className="w-3 h-3" />
        </button>
      </div>
      {markdown && renderValue ? (
        renderValue(value)
      ) : multiline ? (
        <div className="text-sm text-gray-800 dark:text-gray-200 leading-relaxed min-h-[2em] whitespace-pre-wrap">
          {value || <span className="text-gray-300 dark:text-gray-600 italic">{t('common.empty')}</span>}
        </div>
      ) : (
        <div className="text-sm font-medium text-gray-900 dark:text-gray-100 min-h-[1.5em]">
          {value || <span className="text-gray-300 dark:text-gray-600 italic font-normal">{t('common.empty')}</span>}
        </div>
      )}
    </div>
  )
}
