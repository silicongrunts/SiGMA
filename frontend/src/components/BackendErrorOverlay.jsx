/**
 * BackendErrorOverlay — full-screen mask shown when the projects list fails
 * to load (typically because the backend is still starting up, or because
 * it has crashed / is misconfigured).
 *
 * Three stages (driven by elapsed time since first failure):
 *   - 0–180 s: "Backend starting up" with a spinner.
 *   - 60 s+:   A "Show troubleshooting commands" link appears. Both the
 *              Linux/macOS docker compose commands AND the Windows Docker
 *              Desktop launcher instructions are listed together so the
 *              user can pick whichever applies — no fragile OS detection.
 *   - 180 s+:  Switches to "error" state with a more pointed message.
 *
 * The overlay cannot be dismissed by the user — it only disappears when the
 * parent (ProjectsView) reports a successful projects load.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, RefreshCw, Terminal, Copy, Check } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { toastError } from './Toast'

const INITIALIZING_THRESHOLD_SEC = 180
const SHOW_COMMANDS_AFTER_SEC = 60

// Linux / macOS: copy-pasteable docker compose commands.
const COMMANDS = [
  { key: 'logs', labelKey: 'backendError.cmd.logsLabel', cmd: 'docker compose logs -f sigma' },
  { key: 'restart', labelKey: 'backendError.cmd.restartLabel', cmd: 'docker compose restart sigma' },
]

export default function BackendErrorOverlay({ onRetry }) {
  const { t } = useTranslation()
  const startRef = useRef(Date.now())
  const [elapsed, setElapsed] = useState(0)
  const [showCommands, setShowCommands] = useState(false)
  const [copied, setCopied] = useState(null)

  // Tick elapsed seconds for the UI.
  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000))
    }, 1000)
    return () => clearInterval(id)
  }, [])

  const isInitializing = elapsed < INITIALIZING_THRESHOLD_SEC

  const copyCmd = async (key, cmd) => {
    try {
      await copyToClipboard(cmd)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      toastError(t('backendError.copyFailed'))
    }
  }

  return (
    <div className="fixed inset-0 z-[6000] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/60 backdrop-blur-md animate-in fade-in duration-300" />
      <div className="relative bg-white dark:bg-gray-900 rounded-3xl w-full max-w-md p-8 shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 animate-in zoom-in duration-300">
        <div className="flex justify-center mb-4">
          {isInitializing ? (
            <div className="w-16 h-16 rounded-full bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center">
              <RefreshCw size={28} className="text-blue-500 dark:text-blue-400 animate-spin" />
            </div>
          ) : (
            <div className="w-16 h-16 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center">
              <AlertTriangle size={28} className="text-red-500 dark:text-red-400" />
            </div>
          )}
        </div>

        <h2 className="text-xl font-semibold text-gray-900 dark:text-white text-center mb-2">
          {t(isInitializing ? 'backendError.title.initializing' : 'backendError.title.error')}
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 text-center mb-3">
          {t(isInitializing ? 'backendError.desc.initializing' : 'backendError.desc.error')}
        </p>
        <div className="text-xs text-gray-400 dark:text-gray-500 text-center mb-6">
          {t('backendError.elapsed', { seconds: elapsed })}
        </div>

        <button
          onClick={onRetry}
          className="w-full py-2.5 bg-gray-900 dark:bg-white text-white dark:text-gray-900 rounded-xl font-medium hover:opacity-90 transition-opacity mb-3"
        >
          {t('backendError.retry')}
        </button>

        {elapsed >= SHOW_COMMANDS_AFTER_SEC && (
          <button
            onClick={() => setShowCommands(s => !s)}
            className="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 flex items-center justify-center gap-1.5 transition-colors"
          >
            <Terminal size={14} />
            {t('backendError.viewCommands')}
          </button>
        )}

        {showCommands && (
          <div className="mt-3 space-y-3">
            <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
              Linux / macOS
            </div>
            {COMMANDS.map(({ key, labelKey, cmd }) => (
              <div key={key}>
                <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">
                  {t(labelKey)}
                </div>
                <div className="flex items-center gap-2 bg-gray-100 dark:bg-gray-800 rounded-lg p-2 font-mono text-xs">
                  <code className="flex-1 text-gray-700 dark:text-gray-300 break-all">{cmd}</code>
                  <button
                    onClick={() => copyCmd(key, cmd)}
                    className="p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 flex-shrink-0"
                    aria-label={t('backendError.copy')}
                  >
                    {copied === key ? <Check size={12} className="text-green-500" /> : <Copy size={12} />}
                  </button>
                </div>
              </div>
            ))}

            <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide pt-2">
              Windows
            </div>
            <div className="p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-xs text-gray-700 dark:text-gray-300 leading-relaxed">
              {t('backendError.windows.instructions')}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
