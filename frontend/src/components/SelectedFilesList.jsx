import { File, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

/**
 * Displays a list of selected files with remove buttons.
 *
 * @param {Object}     props
 * @param {Array}      props.files         Selected File objects or upload entries.
 * @param {Function}   props.onRemove      Called with the index to remove.
 * @param {string}     [props.className]   Additional classes on the wrapper.
 */
export function SelectedFilesList({ files, onRemove, className = '' }) {
  const { t } = useTranslation()
  if (!files || files.length === 0) return null

  const formatSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  return (
    <div className={`space-y-1 ${className}`}>
      {files.map((item, i) => {
        const file = item?.file || item
        const name = item?.relativePath || file.name
        return (
          <div
            key={`${name}-${i}`}
            className="flex items-center gap-2 px-3 py-1.5 bg-gray-50 dark:bg-gray-800 rounded-lg text-xs group"
          >
            <File className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 flex-shrink-0" />
            <span className="text-gray-700 dark:text-gray-300 truncate flex-1 font-medium">{name}</span>
            <span className="text-gray-400 dark:text-gray-500 flex-shrink-0">{formatSize(file.size)}</span>
            <button
              onClick={(e) => { e.stopPropagation(); onRemove(i) }}
              className="p-0.5 text-gray-400 dark:text-gray-500 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors flex-shrink-0"
              title={t('common.remove')}
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )
      })}
    </div>
  )
}
