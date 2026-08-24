/**
 * SnapshotHealthBanner — alerts the user when auto-snapshot protection is
 * broken (consecutive failures) and offers one-click repair (stale-lock
 * healing + immediate commit).
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, RotateCw } from 'lucide-react'
import { gitsAPI } from '../api'
import { toastError } from './Toast'

const HEALTH_POLL_MS = 60_000
const BANNER_FAILURES_THRESHOLD = 2

export default function SnapshotHealthBanner({ projectId }) {
  const { t } = useTranslation()
  const [health, setHealth] = useState(null)
  const [repairing, setRepairing] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setHealth(await gitsAPI.snapshotHealth(projectId))
    } catch {
      // Backend-down conditions have their own UX; skip silently.
    }
  }, [projectId])

  useEffect(() => {
    setHealth(null)
    refresh()
    const timer = setInterval(refresh, HEALTH_POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  if (!health) return null
  if (health.status !== 'error') return null
  if ((health.consecutive_failures ?? 0) < BANNER_FAILURES_THRESHOLD) return null

  const repair = async () => {
    setRepairing(true)
    try {
      await gitsAPI.repairSnapshot(projectId)
      await refresh()
    } catch (err) {
      toastError(err?.message || t('snapshot.repairFailed'))
    } finally {
      setRepairing(false)
    }
  }

  return (
    <div className="flex items-center gap-3 px-4 py-2 bg-amber-50 dark:bg-amber-900/40 border-b border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200 text-sm">
      <AlertTriangle className="w-4 h-4 shrink-0" />
      <span className="flex-1">{t('snapshot.bannerBody')}</span>
      <button
        onClick={repair}
        disabled={repairing}
        className="flex items-center gap-1.5 px-3 py-1 rounded-md font-medium bg-amber-600 hover:bg-amber-700 disabled:opacity-60 disabled:cursor-not-allowed text-white transition-colors"
      >
        <RotateCw className={`w-3.5 h-3.5 ${repairing ? 'animate-spin' : ''}`} />
        {repairing ? t('snapshot.repairing') : t('snapshot.repair')}
      </button>
    </div>
  )
}
