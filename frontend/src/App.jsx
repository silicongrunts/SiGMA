import { useEffect, useState } from 'react'
import { Routes, Route, useParams, Navigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { ToastContainer } from './components/Toast'
import LoginScreen from './components/LoginScreen'
import ProjectsView from './views/ProjectsView'
import EditorView from './views/EditorView'
import { authAPI, projectsAPI } from './api'

/** Wrapper that forces EditorView remount when project id changes. */
function EditorRoute() {
  const { id } = useParams()
  return <EditorView key={id} />
}

/**
 * Decide whether the current browser may use the app.
 *
 * The backend session cookie is the real authority. On load we ask
 * /auth/status: if no password is configured the instance is open and we pass.
 * If a password is configured we probe a protected endpoint to see whether a
 * valid cookie is already present (returning visitors skip the login screen).
 * Any later 401 from the API layer redirects to /login on its own.
 */
function useAuthGate() {
  const [state, setState] = useState('checking') // 'checking' | 'allowed' | 'denied'

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { password_enabled } = await authAPI.status()
        if (!password_enabled) {
          // Open instance — no gate.
          if (!cancelled) setState('allowed')
          return
        }
        // A password is configured. Probe a protected endpoint to test the
        // cookie without forcing a login if one is already valid.
        try {
          await projectsAPI.list()
          if (!cancelled) setState('allowed')
        } catch {
          if (!cancelled) setState('denied')
        }
      } catch {
        // Backend unreachable: let the normal backend-down UX handle it
        // (ProjectsView shows its own overlay). Treat as allowed here.
        if (!cancelled) setState('allowed')
      }
    })()
    return () => { cancelled = true }
  }, [])

  return state
}

function RequireAuth({ children }) {
  const gate = useAuthGate()
  if (gate === 'checking') {
    return (
      <div className="h-screen w-screen flex items-center justify-center bg-white dark:bg-gray-900">
        <Loader2 className="w-6 h-6 text-sigma-600 animate-spin" />
      </div>
    )
  }
  if (gate === 'denied') {
    return <LoginScreen />
  }
  return children
}

function App() {
  return (
    <div className="h-screen w-screen flex flex-col overflow-hidden bg-white dark:bg-gray-900 font-sans">
      <Routes>
        <Route path="/login" element={<LoginScreen />} />
        <Route path="/" element={<RequireAuth><ProjectsView /></RequireAuth>} />
        <Route path="/editor/:id" element={<RequireAuth><EditorRoute /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <ToastContainer />
    </div>
  )
}

export default App
