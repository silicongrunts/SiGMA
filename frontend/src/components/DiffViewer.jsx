import { useTranslation } from 'react-i18next'
import { Crosshair } from 'lucide-react'
import { MarkdownContent } from './ChatShared'

/**
 * Parse diff tags from text
 * Extracts all <diff>...</diff> blocks
 */
export function parseDiffs(text) {
  const diffRegex = /<diff>\s*<before>(.*?)<\/before>\s*<after>(.*?)<\/after>\s*<\/diff>/gs
  const diffs = []
  let match
  let lastIndex = 0
  let parts = []

  while ((match = diffRegex.exec(text)) !== null) {
    // Add text before this diff
    if (match.index > lastIndex) {
      parts.push({ type: 'text', content: text.slice(lastIndex, match.index) })
    }

    diffs.push({
      before: match[1],
      afterText: match[2]
    })

    parts.push({
      type: 'diff',
      before: match[1],
      after: match[2],
      diffIndex: diffs.length - 1
    })

    lastIndex = match.index + match[0].length
  }

  // Add remaining text
  if (lastIndex < text.length) {
    parts.push({ type: 'text', content: text.slice(lastIndex) })
  }

  return { diffs, parts }
}

/**
 * Side-by-side Diff Viewer Component
 *
 * `canApply` / `canRevert` gate the locate buttons next to the Original /
 * Suggested headers:
 *   • canApply  → Original (before) is uniquely in the doc (diff not applied)
 *   • canRevert → Suggested (after) is uniquely in the doc (diff applied)
 * Clicking the button scrolls the editor to that match and flashes it.
 */
export function SideBySideDiffViewer({ before, after, onAccept, onReject, canApply = false, canRevert = false, onLocate }) {
  const { t } = useTranslation()
  const locateBtn = (active, target) => (active && onLocate) ? (
    <button
      onClick={() => onLocate(target)}
      className="ml-1 p-0.5 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
      title={t('annotations.locate')}
    >
      <Crosshair className="w-3 h-3" />
    </button>
  ) : null
  return (
    <div className="flex gap-0 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden text-sm h-full flex flex-col">
      {/* Title bar - outside scroll area */}
      <div className="flex border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
        <div className="flex-1 flex items-center text-xs font-bold text-red-700 dark:text-red-300 py-1 px-3 bg-red-50 dark:bg-red-900/30 border-r border-gray-200 dark:border-gray-700">
          <span>{t('diff.original')}</span>
          {locateBtn(canApply, before)}
        </div>
        <div className="flex-1 flex items-center text-xs font-bold text-green-700 dark:text-green-300 py-1 px-3 bg-green-50 dark:bg-green-900/30">
          <span>{t('diff.suggested')}</span>
          {locateBtn(canRevert, after)}
        </div>
      </div>

      {/* Content area - scrollable. break-all so long unbreakable tokens
          (paths, URLs, base64) wrap instead of pushing the panel wider. */}
      <div className="flex-1 overflow-auto min-h-0">
        <div className="flex min-w-full">
          {/* Before (left side) - light red background */}
          <div className="flex-1 min-w-0 p-3 font-mono text-xs leading-snug border-r border-gray-200 dark:border-gray-700 bg-red-50 dark:bg-red-900/20">
            <div className="whitespace-pre-wrap break-all text-gray-800 dark:text-red-300">{before}</div>
          </div>
          {/* After (right side) - light green background */}
          <div className="flex-1 min-w-0 p-3 font-mono text-xs leading-snug bg-green-50 dark:bg-green-900/20">
            <div className="whitespace-pre-wrap break-all text-gray-800 dark:text-green-300">{after}</div>
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * Diff Viewer with inline expansion - simplified to only show buttons
 */
export function InlineDiffViewer({ annotation, message, messageIndex = 0, onApplyDiff, onDeleteAnnotation, onExpandDiff, expandedDiff, editorContent, projectId = null }) {
  // Use message.content if message is given, otherwise use annotation text.
  const textToParse = message?.content || annotation.text
  const { diffs, parts } = parseDiffs(textToParse)
  const { t } = useTranslation()

  if (diffs.length === 0) {
    // No diffs, render entire message as markdown
    return <MarkdownContent content={textToParse} projectId={projectId} />
  }

  const handleExpand = (diffIndex) => {
    const diff = diffs[diffIndex]
    if (onExpandDiff) {
      // Identify the expanded diff by BOTH its message and its index within
      // that message, so diffs in sibling messages (which each restart at
      // diffIndex 0) don't all appear expanded at once.
      onExpandDiff({
        before: diff.before,
        after: diff.afterText,
        diffIndex: diffIndex,
        messageIndex: messageIndex
      })
    }
  }

  return (
    <div className="text-sm leading-relaxed">
      {parts.map((part, i) => {
        if (part.type === 'text') {
          // Render text parts as markdown
          return <MarkdownContent key={i} content={part.content} projectId={projectId} />
        }

        const diff = diffs[part.diffIndex]
        const isExpanded = expandedDiff
          && expandedDiff.messageIndex === messageIndex
          && expandedDiff.diffIndex === part.diffIndex
        const found = editorContent?.includes(diff.before)

        return (
          <div key={i} className="my-0.5">
            {!isExpanded ? (
              <button
                onClick={() => handleExpand(part.diffIndex)}
                className={`inline-flex items-center gap-1 px-2 py-1 text-xs font-bold rounded transition-colors ${
                  found
                    ? 'bg-blue-100 hover:bg-blue-200 text-blue-700 dark:bg-blue-900/30 dark:hover:bg-blue-800/50 dark:text-blue-300'
                    : 'bg-gray-100 hover:bg-gray-200 text-gray-400 line-through dark:bg-gray-800 dark:hover:bg-gray-700 dark:text-gray-500'
                }`}
              >
                {t('diff.viewChanges', { preview: diff.before.slice(0, 20) })}
              </button>
            ) : (
              <button
                onClick={() => onExpandDiff(null)}
                className="inline-flex items-center gap-1 px-2 py-1 bg-gray-200 hover:bg-gray-300 text-gray-700 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300 text-xs font-bold rounded transition-colors"
              >
                {t('diff.hideChanges')}
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}
