import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { setUnauthorizedHandler, setToken } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { AppShell } from '@/components/AppShell'
import { Spinner } from '@/components/ui'
import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import Jobs from '@/pages/Jobs'
import JobDetail from '@/pages/JobDetail'
import SubmitTraining from '@/pages/SubmitTraining'
import SubmitVerify from '@/pages/SubmitVerify'
import Datasets from '@/pages/Datasets'
import Models from '@/pages/Models'
import Admin from '@/pages/Admin'
import ProfileSettings from '@/pages/ProfileSettings'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" state={{ from: location.pathname }} replace />
  return <>{children}</>
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  if (user?.role !== 'admin') return <Navigate to="/" replace />
  return <>{children}</>
}

export default function App() {
  const { bootstrap, setUser } = useAuth()

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setToken(null)
      setUser(null)
    })
    void bootstrap()
  }, [bootstrap, setUser])

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/jobs/:jobId" element={<JobDetail />} />
        <Route path="/submit/train" element={<SubmitTraining />} />
        <Route path="/submit/verify" element={<SubmitVerify />} />
        <Route path="/datasets" element={<Datasets />} />
        <Route path="/models" element={<Models />} />
        <Route
          path="/admin"
          element={
            <RequireAdmin>
              <Admin />
            </RequireAdmin>
          }
        />
        <Route path="/settings" element={<ProfileSettings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
