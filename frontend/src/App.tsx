import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { setUnauthorizedHandler, setToken } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { AppShell } from '@/components/AppShell'
import { ProjectGate } from '@/components/ProjectGate'
import { Spinner } from '@/components/ui'
import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import Jobs from '@/pages/Jobs'
import JobDetail from '@/pages/JobDetail'
import SubmitTraining from '@/pages/SubmitTraining'
import SubmitVerify from '@/pages/SubmitVerify'
import Datasets from '@/pages/Datasets'
import Models from '@/pages/Models'
import Projects from '@/pages/Projects'
import Deployments from '@/pages/Deployments'
import Review from '@/pages/Review'
import ReviewDetail from '@/pages/ReviewDetail'
import Admin from '@/pages/Admin'
import ProfileSettings from '@/pages/ProfileSettings'
import Docs from '@/pages/Docs'
import Issues from '@/pages/Issues'
import IssueDetail from '@/pages/IssueDetail'

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
      {/* 文档是独立页面（侧栏 Document 会在新标签页打开），不套控制台外壳 */}
      <Route
        path="/docs"
        element={
          <RequireAuth>
            <Docs />
          </RequireAuth>
        }
      />
      <Route
        path="/docs/:slug"
        element={
          <RequireAuth>
            <Docs />
          </RequireAuth>
        }
      />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/jobs/:jobId" element={<JobDetail />} />
        {/* 任务必须归属项目，所以提交页先过项目闸门 */}
        <Route
          path="/submit/train"
          element={
            <ProjectGate>
              <SubmitTraining />
            </ProjectGate>
          }
        />
        <Route
          path="/submit/verify"
          element={
            <ProjectGate>
              <SubmitVerify />
            </ProjectGate>
          }
        />
        <Route path="/datasets" element={<Datasets />} />
        <Route path="/models" element={<Models />} />
        <Route path="/deployments" element={<Deployments />} />
        {/* 审核在提交窗口必选项目；列表保留跨项目及已归档项目的历史查询 */}
        <Route path="/review" element={<Review />} />
        <Route path="/review/:reviewId" element={<ReviewDetail />} />
        <Route path="/issues" element={<Issues />} />
        <Route path="/issues/:issueId" element={<IssueDetail />} />
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
