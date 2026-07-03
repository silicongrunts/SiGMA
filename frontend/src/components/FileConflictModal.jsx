/**
 * FileConflictModal — shown when a save conflicts with external disk changes.
 *
 * Displays a side-by-side diff between the disk version and the editor version,
 * with Cancel and Force Save buttons.
 */
import { AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import DiffView from './DiffView'

export default function FileConflictModal({ fileName, diffLines, onForceSave, onCancel }) {
  const { t } = useTranslation()
  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onCancel} />
      <div className="bg-white dark:bg-gray-900 rounded-3xl w-full max-w-4xl relative z-[5001] shadow-2xl border border-gray-100 dark:border-gray-800 overflow-hidden flex flex-col max-h-[85vh] animate-in zoom-in duration-300">
        {/* Header */}
        <div className="p-6 border-b border-gray-100 dark:border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-full bg-amber-50 dark:bg-amber-900/30 flex items-center justify-center">
              <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400" />
            </div>
            <div>
              <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">{t('conflict.title')}</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
                {t('conflict.description', { name: fileName })}
              </p>
            </div>
          </div>
        </div>

        {/* Diff view */}
        <div className="flex-1 overflow-hidden border-b border-gray-100 dark:border-gray-800">
          <DiffView lines={diffLines} leftLabel={t('conflict.diskVersion')} rightLabel={t('conflict.yourVersion')} maxH="max-h-[50vh]" />
        </div>

        {/* Actions */}
        <div className="p-5 flex items-center justify-end gap-3 bg-gray-50/50 dark:bg-gray-800/50">
          <button
            onClick={onCancel}
            className="px-5 py-2.5 bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 font-semibold rounded-xl hover:bg-gray-100 dark:hover:bg-gray-700 border border-gray-200 dark:border-gray-700 transition-colors text-sm"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={onForceSave}
            className="px-5 py-2.5 bg-red-600 text-white font-bold rounded-xl hover:bg-red-700 shadow-lg shadow-red-200 dark:shadow-none transition-all active:scale-95 text-sm"
          >
            {t('conflict.forceSave')}
          </button>
        </div>
      </div>
    </div>
  )
}
