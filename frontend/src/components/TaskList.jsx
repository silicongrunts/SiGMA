/**
 * TaskList — renders task checkboxes at the bottom of ThinkingProcess.
 *
 * Features:
 * - ALL tasks displayed (pending, in_progress, completed)
 * - HTML checkboxes with strikethrough for completed
 * - Max 10 items with overflow summary
 */
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { useStore } from '../store/useStore'

const MAX_VISIBLE = 10

export default function TaskList({ expanded = false }) {
  const { t } = useTranslation()
  const taskList = useStore(s => s.taskList)
  const expandedTasks = useStore(s => s.expandedTasks)

  if (!taskList || taskList.length === 0) return null
  if (!expanded && !expandedTasks) return null

  // Sort: in_progress first, then pending, then completed
  const sorted = [...taskList].sort((a, b) => {
    const o = { in_progress: 0, pending: 1, completed: 2 }
    return (o[a.status] ?? 1) - (o[b.status] ?? 1)
  })

  const visible = sorted.slice(0, MAX_VISIBLE)
  const hidden = sorted.length - MAX_VISIBLE

  const getStatusIcon = (status) => {
    switch (status) {
      case 'in_progress': return <Loader2 className="w-3 h-3 text-sigma-600 animate-spin flex-shrink-0" />
      case 'completed': return <div className="w-3 h-3 rounded border border-green-400 bg-green-50 dark:bg-green-900/30 flex items-center justify-center flex-shrink-0"><svg className="w-2 h-2 text-green-500" viewBox="0 0 12 12" fill="none"><path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg></div>
      case 'pending':
      default: return <div className="w-3 h-3 rounded border border-gray-300 dark:border-gray-600 flex-shrink-0" />
    }
  }

  return (
    <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-800">
      <div className="flex flex-col gap-0.5">
        {visible.map(task => {
          const isCompleted = task.status === 'completed'
          return (
            <div key={task.id} className={`flex items-center gap-2 py-0.5 ${isCompleted ? 'opacity-50' : ''}`}>
              {getStatusIcon(task.status)}
              <span className={`text-[10px] leading-tight ${isCompleted ? 'line-through text-gray-400 dark:text-gray-500' : task.status === 'in_progress' ? 'font-semibold text-gray-700 dark:text-gray-300' : 'text-gray-600 dark:text-gray-400'}`}>
                {task.subject}
              </span>
            </div>
          )
        })}
      </div>
      {hidden > 0 && (
        <div className="text-[9px] text-gray-300 dark:text-gray-600 mt-0.5 pl-5">
          {t('tasklist.more', { count: hidden })}
        </div>
      )}
    </div>
  )
}
