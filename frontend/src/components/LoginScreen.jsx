/**
 * LoginScreen — full-screen access gate shown when SiGMA has a password set
 * and the current browser has no valid session cookie.
 *
 * On submit it calls POST /auth/login; the backend sets the HttpOnly session
 * cookie, then we navigate to the app. The cookie is the single source of
 * truth — this screen cannot grant access the backend refuses.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Lock, LogIn, Loader2 } from 'lucide-react'
import { authAPI } from '../api'
import { toastError } from './Toast'

export default function LoginScreen() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [checking, setChecking] = useState(true)
  const inputRef = useRef(null)

  useEffect(() => {
    // If no password is configured the instance is open — skip the login screen.
    let cancelled = false
    authAPI.status()
      .then(({ password_enabled }) => {
        if (cancelled) return
        if (!password_enabled) {
          navigate('/', { replace: true })
        } else {
          setChecking(false)
          inputRef.current?.focus()
        }
      })
      .catch(() => { if (!cancelled) setChecking(false) })
    return () => { cancelled = true }
  }, [navigate])

  if (checking) {
    return (
      <div className="fixed inset-0 z-[6000] flex items-center justify-center bg-gray-50 dark:bg-gray-950">
        <Loader2 className="w-6 h-6 text-sigma-600 animate-spin" />
      </div>
    )
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!password || submitting) return
    setSubmitting(true)
    setError('')
    try {
      await authAPI.login(password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err?.message || t('auth.loginFailed'))
      // Don't toast on every wrong password; the inline error is enough.
      if (!/password/i.test(err?.message || '')) toastError(err?.message || t('auth.loginFailed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[6000] flex items-center justify-center p-4 bg-gray-50 dark:bg-gray-950">
      <div className="relative bg-white dark:bg-gray-900 rounded-3xl w-full max-w-md p-8 shadow-[0_20px_70px_rgba(0,0,0,0.15)] border border-gray-100 dark:border-gray-800 animate-in zoom-in duration-300">
        <div className="flex justify-center mb-5">
          <div className="w-16 h-16 rounded-full bg-sigma-100 dark:bg-sigma-900/30 flex items-center justify-center">
            <Lock size={28} className="text-sigma-600 dark:text-sigma-400" />
          </div>
        </div>

        <h1 className="text-xl font-bold text-gray-900 dark:text-white text-center mb-1">
          {t('auth.loginTitle')}
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 text-center mb-6">
          {t('auth.loginSubtitle')}
        </p>

        <form onSubmit={handleSubmit}>
          <label className="block">
            <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">
              {t('auth.password')}
            </span>
            <input
              ref={inputRef}
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); setError('') }}
              autoComplete="current-password"
              className="w-full px-3 py-2.5 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm text-gray-900 dark:text-gray-100"
            />
          </label>

          {error && (
            <div className="text-xs text-red-500 dark:text-red-400 mt-2">{error}</div>
          )}

          <button
            type="submit"
            disabled={!password || submitting}
            className="w-full mt-5 flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-bold text-white bg-sigma-600 hover:bg-sigma-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
          >
            {submitting ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <LogIn size={16} />
            )}
            {t('auth.login')}
          </button>
        </form>
      </div>
    </div>
  )
}
