import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Download, Loader2, PackagePlus, RefreshCw, Search, Server, X } from 'lucide-react'
import { systemAPI } from '../api'
import { createSSEStreamParser } from '../utils/sse'
import { toastError, toastSuccess } from './Toast'

const REPOSITORY_OPTIONS = [
  ['official', 'tex.repo.official'],
  ['tuna', 'tex.repo.tuna'],
]

export default function TeXManagerPanel({ isOpen, onClose }) {
  const { t } = useTranslation()
  const [status, setStatus] = useState(null)
  const [repository, setRepository] = useState('official')
  const [customRepository, setCustomRepository] = useState('')
  const [targetYear, setTargetYear] = useState(String(new Date().getFullYear()))
  const [packageName, setPackageName] = useState('')
  const [query, setQuery] = useState('')
  const [logs, setLogs] = useState([])
  const [running, setRunning] = useState(false)
  const [pendingOperation, setPendingOperation] = useState(null)
  const abortRef = useRef(null)

  const selectedRepository = repository === 'custom' ? customRepository.trim() : repository

  const loadStatus = useCallback(async () => {
    if (!isOpen) return
    try {
      const nextStatus = await systemAPI.getTeXStatus()
      setStatus(nextStatus)
      setTargetYear(prev => {
        if (prev) return prev
        return String(nextStatus?.current_year || new Date().getFullYear())
      })
    } catch (err) {
      toastError(err.message || t('tex.toast.statusFailed'))
    }
  }, [isOpen, t])

  useEffect(() => { loadStatus() }, [loadStatus])

  useEffect(() => () => abortRef.current?.abort(), [])

  if (!isOpen) return null

  const appendLog = line => setLogs(prev => [...prev.slice(-499), line])

  const executeOperation = async (operation, extra = {}) => {
    if (running) return
    setRunning(true)
    setLogs([])
    const abort = new AbortController()
    abortRef.current = abort
    try {
      const stream = await systemAPI.runTeXOperation({
        operation,
        repository: selectedRepository || undefined,
        ...extra,
      }, abort.signal)
      const reader = stream.getReader()
      const decoder = new TextDecoder()
      const parser = createSSEStreamParser({
        onEvent: (type, data) => {
          if (type === 'start') appendLog(`$ ${data.operation}`)
          if (type === 'log') appendLog(data.line || '')
          if (type === 'done') toastSuccess(t('tex.toast.done'))
          if (type === 'done' && data.message) appendLog(data.message)
          if (type === 'error') {
            appendLog(data.message || `exit ${data.returncode ?? ''}`)
            toastError(data.message || t('tex.toast.failed'))
          }
        },
        onError: (error) => appendLog(error?.message || t('tex.toast.failed')),
      })
      await parser.start(reader, decoder, abort.signal)
    } catch (err) {
      if (!abort.signal.aborted) {
        appendLog(err.message || t('tex.toast.failed'))
        toastError(err.message || t('tex.toast.failed'))
      }
    } finally {
      setRunning(false)
      abortRef.current = null
      loadStatus()
    }
  }

  const requestOperation = (operation, extra = {}, confirmationKey = null) => {
    if (confirmationKey) {
      setPendingOperation({ operation, extra, confirmationKey })
      return
    }
    executeOperation(operation, extra)
  }

  const confirmPendingOperation = () => {
    if (!pendingOperation) return
    const { operation, extra } = pendingOperation
    setPendingOperation(null)
    executeOperation(operation, extra)
  }

  const stopOperation = () => abortRef.current?.abort()

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={running ? undefined : onClose} />
      <div className="relative z-[5001] w-full max-w-5xl h-[84vh] bg-white dark:bg-gray-900 rounded-2xl shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 overflow-hidden flex flex-col animate-in zoom-in duration-300">
        {pendingOperation && (
          <div className="absolute inset-0 z-[5010] flex items-center justify-center p-4">
            <div className="absolute inset-0 bg-gray-900/50 backdrop-blur-sm animate-in fade-in duration-200" onClick={() => setPendingOperation(null)} />
            <div className="relative w-full max-w-md bg-white dark:bg-gray-900 rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-800 p-6 animate-in zoom-in duration-200">
              <div className="mx-auto mb-4 w-12 h-12 rounded-full bg-amber-50 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 flex items-center justify-center">
                <AlertTriangle className="w-6 h-6" />
              </div>
              <h3 className="text-lg font-black text-center text-gray-900 dark:text-gray-100 mb-2">{t(`tex.confirm.${pendingOperation.confirmationKey}.title`)}</h3>
              <p className="text-sm text-gray-500 dark:text-gray-400 text-center leading-6 mb-6">{t(`tex.confirm.${pendingOperation.confirmationKey}.message`)}</p>
              <div className="flex gap-3">
                <button onClick={() => setPendingOperation(null)} className="flex-1 px-4 py-2.5 text-sm font-bold text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors">
                  {t('common.cancel')}
                </button>
                <button onClick={confirmPendingOperation} className="flex-1 px-4 py-2.5 text-sm font-bold text-white bg-amber-600 hover:bg-amber-700 rounded-lg transition-colors">
                  {t('tex.confirm.continue')}
                </button>
              </div>
            </div>
          </div>
        )}
        <header className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <div>
            <h2 className="text-lg font-black text-gray-900 dark:text-gray-100">{t('tex.title')}</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 font-mono truncate">
              {status?.texlive_root || '/usr/local/texlive'}{status?.current_year ? ` / ${status.current_year}` : ''}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={loadStatus} disabled={running} className="p-2 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg disabled:opacity-50" title={t('tex.refresh')}>
              <RefreshCw className="w-4 h-4" />
            </button>
            <button onClick={onClose} disabled={running} className="p-2 text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg disabled:opacity-50" title={t('common.close')}>
              <X className="w-5 h-5" />
            </button>
          </div>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] flex-1 min-h-0">
          <aside className="border-r border-gray-100 dark:border-gray-800 p-5 overflow-y-auto space-y-5">
            <section>
              <h3 className="text-sm font-black text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
                <Server className="w-4 h-4" />
                {t('tex.repository')}
              </h3>
              <select
                value={repository}
                onChange={e => setRepository(e.target.value)}
                disabled={running}
                className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm outline-none focus:ring-4 focus:ring-sigma-600/10"
              >
                {REPOSITORY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
                <option value="custom">{t('tex.repo.custom')}</option>
              </select>
              {repository === 'custom' && (
                <input
                  value={customRepository}
                  onChange={e => setCustomRepository(e.target.value)}
                  disabled={running}
                  placeholder="https://.../systems/texlive/tlnet"
                  className="mt-2 w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm outline-none focus:ring-4 focus:ring-sigma-600/10"
                />
              )}
              <button
                onClick={() => requestOperation('set_repository')}
                disabled={running || !selectedRepository}
                className="mt-2 w-full px-3 py-2 text-sm font-bold text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50"
              >
                {t('tex.applyRepository')}
              </button>
            </section>

            <section className="space-y-2">
              <h3 className="text-sm font-black text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
                <Download className="w-4 h-4" />
                {t('tex.maintenance')}
              </h3>
              <button onClick={() => requestOperation('update', {}, 'update')} disabled={running} className="w-full px-3 py-2 text-sm font-bold text-white bg-sigma-600 hover:bg-sigma-700 rounded-lg disabled:opacity-50">
                {t('tex.update')}
              </button>
              <button onClick={() => requestOperation('update_tlmgr', {}, 'updateTlmgr')} disabled={running} className="w-full px-3 py-2 text-sm font-bold text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50">
                {t('tex.updateTlmgr')}
              </button>
              <button onClick={() => requestOperation('install_full', {}, 'installFull')} disabled={running} className="w-full px-3 py-2 text-sm font-bold text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50">
                {t('tex.installFull')}
              </button>
              <div className="flex gap-2">
                <input
                  value={targetYear}
                  onChange={e => setTargetYear(e.target.value)}
                  disabled={running}
                  inputMode="numeric"
                  className="w-24 px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm outline-none focus:ring-4 focus:ring-sigma-600/10"
                  aria-label={t('tex.targetYear')}
                />
                <button onClick={() => requestOperation('switch_year', { target_year: targetYear }, 'switchYear')} disabled={running || !targetYear.trim()} className="flex-1 px-3 py-2 text-sm font-bold text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50">
                  {t('tex.switchYear')}
                </button>
              </div>
            </section>

            <section>
              <h3 className="text-sm font-black text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
                <PackagePlus className="w-4 h-4" />
                {t('tex.package')}
              </h3>
              <input
                value={packageName}
                onChange={e => setPackageName(e.target.value)}
                disabled={running}
                placeholder={t('tex.packagePlaceholder')}
                className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm outline-none focus:ring-4 focus:ring-sigma-600/10"
              />
              <button onClick={() => requestOperation('install_package', { package: packageName }, 'installPackage')} disabled={running || !packageName.trim()} className="mt-2 w-full px-3 py-2 text-sm font-bold text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50">
                {t('tex.installPackage')}
              </button>
            </section>

            <section>
              <h3 className="text-sm font-black text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
                <Search className="w-4 h-4" />
                {t('tex.search')}
              </h3>
              <input
                value={query}
                onChange={e => setQuery(e.target.value)}
                disabled={running}
                placeholder={t('tex.searchPlaceholder')}
                className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm outline-none focus:ring-4 focus:ring-sigma-600/10"
              />
              <button onClick={() => requestOperation('search', { query })} disabled={running || !query.trim()} className="mt-2 w-full px-3 py-2 text-sm font-bold text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50">
                {t('tex.searchPackages')}
              </button>
            </section>
          </aside>

          <main className="flex flex-col min-h-0">
            <div className="px-5 py-3 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
              <div className="text-sm font-bold text-gray-700 dark:text-gray-300">
                {running ? t('tex.running') : status?.tlmgr_available ? t('tex.ready') : t('tex.tlmgrMissing')}
              </div>
              {running && (
                <button onClick={stopOperation} className="px-3 py-1.5 text-xs font-bold text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg">
                  {t('common.cancel')}
                </button>
              )}
            </div>
            <pre className="flex-1 min-h-0 overflow-auto bg-gray-950 text-gray-100 p-4 text-xs leading-5 font-mono whitespace-pre-wrap">
              {running && logs.length === 0 ? <span><Loader2 className="inline w-4 h-4 animate-spin mr-2" />{t('tex.waiting')}</span> : logs.join('\n')}
            </pre>
          </main>
        </div>
      </div>
    </div>
  )
}
