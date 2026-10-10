import * as React from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  BookOpen,
  ClipboardCheck,
  Database,
  ExternalLink,
  FlaskConical,
  FolderKanban,
  Gauge,
  GraduationCap,
  ListChecks,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Rocket,
  Server,
  Settings,
  ShieldCheck,
  Sun,
  UserCog,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useAuth, useProject, useUi } from '@/lib/store'
import { cn, formatRelative } from '@/lib/utils'
import { ProjectSwitcher } from './ProjectSwitcher'
import { Button, Tooltip, TooltipProvider } from './ui'

interface NavItem {
  to: string
  label: string
  Icon: typeof Gauge
  adminOnly?: boolean
  end?: boolean
}

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: '总览',
    items: [
      { to: '/', label: '数据大屏', Icon: Gauge, end: true },
      { to: '/projects', label: '项目', Icon: FolderKanban },
    ],
  },
  {
    section: '任务',
    items: [
      { to: '/jobs', label: '任务列表', Icon: ListChecks },
      { to: '/submit/train', label: '提交训练', Icon: GraduationCap },
      { to: '/submit/verify', label: '提交评测', Icon: FlaskConical },
      { to: '/review', label: '样例审核', Icon: ClipboardCheck },
    ],
  },
  {
    section: '资源',
    items: [
      { to: '/datasets', label: '数据集', Icon: Database },
      { to: '/models', label: '模型仓库', Icon: Server },
      { to: '/deployments', label: '模型部署', Icon: Rocket },
    ],
  },
  {
    section: '管理',
    items: [
      { to: '/admin', label: '平台管理', Icon: ShieldCheck, adminOnly: true },
      { to: '/settings', label: '个人设置', Icon: Settings },
    ],
  },
]

export function AppShell() {
  const { user, logout } = useAuth()
  const { projectId } = useProject()
  const { theme, toggleTheme, sidebarCollapsed, toggleSidebar } = useUi()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const notifications = useQuery({
    queryKey: ['notifications'],
    queryFn: () => api.notifications(),
    refetchInterval: 20000,
  })
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => api.settings() })
  const overview = useQuery({
    queryKey: ['job-overview', projectId],
    queryFn: () => api.jobOverview(projectId),
    refetchInterval: 15000,
  })
  // 常驻的 vLLM 服务在侧栏挂个计数，避免忘了它们还占着显卡
  const deployments = useQuery({
    queryKey: ['deployments', 'active'],
    queryFn: () => api.deployments({ active_only: true }),
    refetchInterval: 20000,
  })

  const unread = (notifications.data ?? []).filter((item) => !item.read)
  const [showNotifications, setShowNotifications] = React.useState(false)

  if (!user) return null

  const handleLogout = async () => {
    await logout()
    queryClient.clear()
    navigate('/login', { replace: true })
  }

  return (
    <TooltipProvider>
      <div className="flex min-h-screen flex-col bg-bg text-fg">
        {/* ================================================== 顶部全局栏 == */}
        <header className="sticky top-0 z-40 flex h-16 shrink-0 items-center justify-between gap-4 border-b border-border bg-bg-elevated pl-7 pr-4">
          <NavLink to="/" className="flex shrink-0 items-center gap-3.5" aria-label="返回数据大屏">
            <img src="/logo.png" alt="PRT" className="logo-img h-9 w-auto" />
            <span className="h-7 w-px bg-border" aria-hidden />
            <span className="wordmark text-[22px] leading-none">AI DL Factory</span>
          </NavLink>

          <span className="ml-2 hidden h-7 w-px bg-border md:block" aria-hidden />
          <div className="hidden md:block">
            <ProjectSwitcher />
          </div>

          <div className="flex min-w-0 flex-1 items-center justify-center">
            {settings.data?.banner && (
              <p className="truncate rounded bg-warning-soft px-2.5 py-1 text-xs text-warning">
                {settings.data.banner}
              </p>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            <Tooltip label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}>
              <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label="切换主题">
                {theme === 'dark' ? <Sun className="h-4 w-4" aria-hidden /> : <Moon className="h-4 w-4" aria-hidden />}
              </Button>
            </Tooltip>

            <div className="relative">
              <Button
                variant="ghost"
                size="icon"
                aria-label={`通知，${unread.length} 条未读`}
                onClick={() => setShowNotifications((open) => !open)}
              >
                <Bell className="h-4 w-4" aria-hidden />
              </Button>
              {unread.length > 0 && (
                <span
                  role="status"
                  aria-atomic="true"
                  className="pointer-events-none absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold text-white"
                >
                  {unread.length > 99 ? '99+' : unread.length}
                  <span className="sr-only">条未读通知</span>
                </span>
              )}
              {showNotifications && (
                <>
                  <div className="fixed inset-0 z-40" onClick={() => setShowNotifications(false)} aria-hidden />
                  <div className="absolute right-0 top-10 z-50 w-96 overflow-hidden rounded-lg border border-border bg-card shadow-lg">
                    <div className="flex items-center justify-between border-b border-border px-3 py-2">
                      <span className="text-sm font-medium">通知</span>
                      <button
                        type="button"
                        className="link text-xs"
                        onClick={async () => {
                          await api.markRead()
                          notifications.refetch()
                        }}
                      >
                        全部已读
                      </button>
                    </div>
                    <ul className="max-h-96 divide-y divide-border overflow-y-auto">
                      {(notifications.data ?? []).slice(0, 30).map((item) => (
                        <li key={item.id}>
                          <button
                            type="button"
                            className={cn(
                              'w-full px-3 py-2.5 text-left transition-colors hover:bg-card-hover',
                              !item.read && 'bg-primary-soft',
                            )}
                            onClick={() => {
                              setShowNotifications(false)
                              if (item.link) navigate(item.link)
                            }}
                          >
                            <div className="flex items-start justify-between gap-2">
                              <span className="text-xs font-medium text-fg">{item.title}</span>
                              <span className="shrink-0 text-[10px] text-fg-subtle">{formatRelative(item.at)}</span>
                            </div>
                            {item.body && <p className="mt-0.5 line-clamp-2 text-[11px] text-fg-muted">{item.body}</p>}
                          </button>
                        </li>
                      ))}
                      {!notifications.data?.length && (
                        <li className="px-3 py-8 text-center text-xs text-fg-subtle">暂无通知</li>
                      )}
                    </ul>
                  </div>
                </>
              )}
            </div>

            <span className="mx-1 hidden h-4 w-px bg-border sm:block" aria-hidden />

            <div className="flex items-center gap-2">
              <div className="hidden text-right sm:block">
                <div className="text-xs font-medium leading-tight text-fg">{user.display_name}</div>
                <div className="text-[10px] leading-tight text-fg-subtle">
                  {user.role === 'admin' ? '管理员' : '成员'} · {user.email}
                </div>
              </div>
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-fg">
                {user.display_name.slice(0, 1).toUpperCase()}
              </div>
              <Tooltip label="退出登录">
                <Button variant="ghost" size="icon" onClick={handleLogout} aria-label="退出登录">
                  <LogOut className="h-4 w-4" aria-hidden />
                </Button>
              </Tooltip>
            </div>
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          {/* ============================================== 左侧导航 ====== */}
          <aside
            className={cn(
              'sticky top-16 flex h-[calc(100vh-4rem)] shrink-0 flex-col border-r border-border bg-bg transition-[width] duration-200',
              sidebarCollapsed ? 'w-[56px]' : 'w-[188px]',
            )}
          >
            <nav className="flex-1 overflow-y-auto px-2 py-3" aria-label="主导航">
              {NAV.map((group) => {
                const items = group.items.filter((item) => !item.adminOnly || user.role === 'admin')
                if (!items.length) return null
                return (
                  <div key={group.section} className="mb-3">
                    {!sidebarCollapsed && (
                      <div className="mb-1 px-2.5 text-[11px] text-fg-subtle">{group.section}</div>
                    )}
                    <ul>
                      {items.map((item) => (
                        <li key={item.to}>
                          <NavLink
                            to={item.to}
                            end={item.end}
                            className={({ isActive }) =>
                              cn(
                                'relative flex items-center gap-2.5 rounded px-2.5 py-2 text-[13px] transition-colors',
                                sidebarCollapsed && 'justify-center px-0',
                                isActive
                                  ? 'bg-primary-soft font-medium text-primary'
                                  : 'text-fg-muted hover:bg-card-hover hover:text-fg',
                              )
                            }
                            title={sidebarCollapsed ? item.label : undefined}
                          >
                            {({ isActive }) => (
                              <>
                                {isActive && (
                                  <span
                                    className="absolute inset-y-1 left-0 w-[3px] rounded-r bg-primary"
                                    aria-hidden
                                  />
                                )}
                                <item.Icon className="h-4 w-4 shrink-0" aria-hidden />
                                {!sidebarCollapsed && <span className="truncate">{item.label}</span>}
                                {!sidebarCollapsed &&
                                  item.to === '/jobs' &&
                                  user.role === 'admin' &&
                                  (overview.data?.pending_review ?? 0) > 0 && (
                                    <span className="ml-auto rounded-full bg-danger px-1.5 text-[10px] font-semibold leading-4 text-white">
                                      {overview.data?.pending_review}
                                    </span>
                                  )}
                                {!sidebarCollapsed &&
                                  item.to === '/deployments' &&
                                  (deployments.data?.length ?? 0) > 0 && (
                                    <span className="ml-auto rounded-full bg-success-soft px-1.5 text-[10px] font-semibold leading-4 text-success">
                                      {deployments.data?.length}
                                    </span>
                                  )}
                              </>
                            )}
                          </NavLink>
                        </li>
                      ))}
                    </ul>
                  </div>
                )
              })}
            </nav>

            <div className="border-t border-border p-2">
              <a
                href="/docs"
                target="_blank"
                rel="noreferrer"
                className={cn(
                  'flex items-center gap-2.5 rounded px-2.5 py-2 text-[13px] text-fg-muted transition-colors hover:bg-card-hover hover:text-fg',
                  sidebarCollapsed && 'justify-center px-0',
                )}
                title="平台文档（新标签页打开）"
              >
                <BookOpen className="h-4 w-4 shrink-0" aria-hidden />
                {!sidebarCollapsed && (
                  <>
                    <span className="truncate">Document</span>
                    <ExternalLink className="ml-auto h-3 w-3 shrink-0 text-fg-subtle" aria-hidden />
                  </>
                )}
              </a>
            </div>

            <div className="border-t border-border p-1.5">
              <Button
                variant="ghost"
                size="sm"
                className="w-full justify-center text-fg-subtle"
                onClick={toggleSidebar}
              >
                {sidebarCollapsed ? (
                  <PanelLeftOpen className="h-4 w-4" aria-hidden />
                ) : (
                  <>
                    <PanelLeftClose className="h-4 w-4" aria-hidden />
                    收起
                  </>
                )}
              </Button>
            </div>
          </aside>

          {/* ================================================== 内容区 ==== */}
          <div className="flex min-w-0 flex-1 flex-col">
            {user.must_change_password && (
              <div className="flex items-center gap-2 border-b border-warning/30 bg-warning-soft px-5 py-2 text-xs text-warning">
                <UserCog className="h-4 w-4" aria-hidden />
                初始密码尚未修改，请尽快到
                <NavLink to="/settings" className="underline underline-offset-2">
                  个人设置
                </NavLink>
                修改。
              </div>
            )}

            <main className="min-w-0 flex-1 p-5">
              <Outlet />
            </main>
          </div>
        </div>
      </div>
    </TooltipProvider>
  )
}
