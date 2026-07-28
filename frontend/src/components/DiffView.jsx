/**
 * DiffView — shared side-by-side diff rendering component.
 *
 * Extracted from HistoryPanel so both HistoryPanel and FileConflictModal
 * can reuse the same diff rendering logic.
 *
 * Accepts typed diff lines with `type` in ('context', 'remove', 'add').
 * Produces paired left/right columns for side-by-side display.
 */
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { intraLineDiff } from '../utils/intraLineDiff'

function pairDiffLines(lines) {
  const paired = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (line.type === 'header') { i++; continue }
    if (line.type === 'hunk') {
      paired.push({ left: line, right: { ...line } })
      i++; continue
    }
    if (line.type === 'remove') {
      const removes = []
      while (i < lines.length && lines[i].type === 'remove') { removes.push(lines[i]); i++ }
      const adds = []
      while (i < lines.length && lines[i].type === 'add') { adds.push(lines[i]); i++ }
      const maxLen = Math.max(removes.length, adds.length)
      for (let j = 0; j < maxLen; j++) {
        const leftRaw = j < removes.length ? removes[j] : { type: 'empty', content: '', line_number: null }
        const rightRaw = j < adds.length ? adds[j] : { type: 'empty', content: '', line_number: null }
        // GitHub-style intra-line highlight: only on a clean 1:1 remove↔add
        // pair. Unpaired rows (uneven runs, truncated diffs) keep whole-line
        // tinting. We copy the line objects so context lines elsewhere stay
        // untouched.
        let left = leftRaw
        let right = rightRaw
        if (leftRaw.type === 'remove' && rightRaw.type === 'add') {
          const seg = intraLineDiff(leftRaw.content, rightRaw.content)
          if (seg) {
            left = { ...leftRaw, segments: seg.removed }
            right = { ...rightRaw, segments: seg.added }
          }
        }
        paired.push({ left, right })
      }
      continue
    }
    if (line.type === 'add') {
      paired.push({ left: { type: 'empty', content: '', line_number: null }, right: line })
      i++; continue
    }
    // context
    paired.push({ left: line, right: line })
    i++
  }
  return paired
}

/**
 * Render the content cell of a line. When `line.segments` is present (a
 * paired remove↔add row with an intra-line diff), changed spans get a deeper
 * tint so the exact edited words stand out against the lighter line bg.
 */
function renderLineContent(line) {
  if (!line.segments) {
    return line.content
  }
  const changedCls = line.type === 'remove'
    ? 'bg-red-200 dark:bg-red-900/40'
    : 'bg-green-200 dark:bg-green-900/40'
  return line.segments.map((seg, idx) =>
    seg.changed
      ? <span key={idx} className={`rounded-sm ${changedCls}`}>{seg.text}</span>
      : <span key={idx}>{seg.text}</span>
  )
}

function renderPairedLine(line, idx) {
  if (!line) return <div key={idx} className="h-5" />
  if (line.type === 'hunk' || line.type === 'header') {
    return <div key={idx} className="bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 px-2 py-0.5 text-[10px] font-bold truncate">{line.content}</div>
  }
  if (line.type === 'empty') {
    return <div key={idx} className="h-5" />
  }
  const bg = line.type === 'remove' ? 'bg-red-50 dark:bg-red-900/20 text-red-900 dark:text-red-300' :
             line.type === 'add' ? 'bg-green-50 dark:bg-green-900/20 text-green-900 dark:text-green-300' :
             'hover:bg-gray-50 dark:hover:bg-gray-800'
  return (
    <div key={idx} className={`font-mono text-xs leading-5 flex ${bg}`}>
      <span className="w-10 shrink-0 text-right pr-2 select-none text-gray-300 dark:text-gray-600">
        {line.line_number || ''}
      </span>
      <span className="flex-1 whitespace-pre-wrap break-all">{renderLineContent(line)}</span>
    </div>
  )
}

export default function DiffView({ lines, leftLabel, rightLabel, maxH = 'max-h-80' }) {
  const { t } = useTranslation()
  const left = leftLabel || t('diff.previous')
  const right = rightLabel || t('diff.current')
  const paired = useMemo(() => pairDiffLines(lines), [lines])

  return (
    <div className={`overflow-auto ${maxH}`}>
      <div className="flex divide-x divide-gray-200 dark:divide-gray-700">
        <div className="flex-1 min-w-0">
          <div className="px-3 py-1.5 text-[10px] font-bold text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/30 border-b border-gray-100 dark:border-gray-800 uppercase tracking-wider">{left}</div>
          <div className="font-mono text-xs overflow-x-auto">
            {paired.map((pair, i) => renderPairedLine(pair.left, i))}
          </div>
        </div>
        <div className="flex-1 min-w-0">
          <div className="px-3 py-1.5 text-[10px] font-bold text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/30 border-b border-gray-100 dark:border-gray-800 uppercase tracking-wider">{right}</div>
          <div className="font-mono text-xs overflow-x-auto">
            {paired.map((pair, i) => renderPairedLine(pair.right, i))}
          </div>
        </div>
      </div>
    </div>
  )
}
