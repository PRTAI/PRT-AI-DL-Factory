import * as React from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  Database,
  FlaskConical,
  Gauge,
  GraduationCap,
  ListChecks,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Server,
  Settings,
  ShieldCheck,
  Sun,
  UserCog,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useAuth, useUi } from '@/lib/store'
import { cn, formatRelative } from '@/lib/utils'
import { Button, Separator, Tooltip, TooltipProvider } from './ui'

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
    items: [{ to: '/', label: '数据大屏', Icon: Gauge, end: true }],
  },
  {
    section: '任务',
    items: [
      { to: '/jobs', label: '任务列表', Icon: ListChecks },
      { to: '/submit/train', label: '提交训练', Icon: GraduationCap },
      { to: '/submit/verify', label: '提交评测', Icon: FlaskConical },
    ],
  },
  {
    section: '资源',
    items: [
      { to: '/datasets', label: '数据集', Icon: Database },
      { to: '/models', label: '模型仓库', Icon: Server },
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
    queryKey: ['job-overview'],
    queryFn: () => api.jobOverview(),
    refetchInterval: 15000,
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
      <div className="flex h-full min-h-screen bg-bg text-fg">
        {/* ------------------------------------------------------ sidebar -- */}
        <aside
          className={cn(
            'sticky top-0 flex h-screen shrink-0 flex-col border-r border-border bg-bg-elevated transition-[width] duration-200',
            sidebarCollapsed ? 'w-[64px]' : 'w-[228px]',
          )}
        >
          <div className="flex h-14 items-center gap-2.5 border-b border-border px-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary text-sm font-bold text-primary-fg">
              PF
            </div>
            {!sidebarCollapsed && (
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold leading-tight">PRT AI DL Factory</div>
                <div className="truncate text-[10px] text-fg-subtle">后训练 · 评测一体化平台</div>
              </div>
            )}
          </div>

          <nav className="flex-1 overflow-y-auto px-2 py-3" aria-label="主导航">
            {NAV.map((group) => {
              const items = group.items.filter((item) => !item.adminOnly || user.role === 'admin')
              if (!items.length) return null
              return (
                <div key={group.section} className="mb-4">
                  {!sidebarCollapsed && (
                    <div className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-wider text-fg-subtle">
                      {group.section}
                    </div>
                  )}
                  <ul className="space-y-0.5">
                    {items.map((item) => (
                      <li key={item.to}>
                        <NavLink
                          to={item.to}
                          end={item.end}
                          className={({ isActive }) =>
                            cn(
                              'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors',
                              sidebarCollapsed && 'justify-center px-0',
                              isActive
                                ? 'bg-primary font-medium text-primary-fg shadow-sm'
                                : 'text-fg-muted hover:bg-muted hover:text-fg',
                            )
                          }
                          title={sidebarCollapsed ? item.label : undefined}
                        >
                          <item.Icon className="h-4 w-4 shrink-0" aria-hidden />
                          {!sidebarCollapsed && <span className="truncate">{item.label}</span>}
                          {!sidebarCollapsed &&
                            item.to === '/jobs' &&
                            user.role === 'admin' &&
                            (overview.data?.pending_review ?? 0) > 0 && (
                              <span className="ml-auto rounded-md bg-warning px-1.5 py-0.5 text-[10px] font-semibold leading-4 text-white">
                                {overview.data?.pending_review}
                              </span>
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
            <Button variant="ghost" size="sm" className="w-full justify-center" onClick={toggleSidebar}>
              {sidebarCollapsed ? (
                <PanelLeftOpen className="h-4 w-4" aria-hidden />
              ) : (
                <>
                  <PanelLeftClose className="h-4 w-4" aria-hidden />
                  收起侧栏
                </>
              )}
            </Button>
          </div>
        </aside>

        {/* -------------------------------------------------------- main --- */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-border bg-bg-elevated/95 px-4 backdrop-blur">
            <div className="flex min-w-0 items-center gap-3">
              {settings.data?.banner ? (
                <p className="truncate rounded-md bg-warning-soft px-2.5 py-1 text-xs text-warning">
                  {settings.data.banner}
                </p>
              ) : (
                <p className="truncate text-xs text-fg-subtle">
                  {overview.data
                    ? `运行中 ${overview.data.running} · 排队 ${overview.data.queued} · GPU ${overview.data.gpu.busy.length}/${overview.data.gpu.total} 占用`
                    : ''}
                </p>
              )}
            </div>

            <div className="flex items-center gap-1.5">
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
                    <div className="absolute right-0 top-11 z-50 w-96 overflow-hidden rounded-xl border border-border bg-card shadow-2xl">
                      <div className="flex items-center justify-between border-b border-border px-3 py-2">
                        <span className="text-sm font-semibold">通知</span>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={async () => {
                            await api.markRead()
                            notifications.refetch()
                          }}
                        >
                          全部已读
                        </Button>
                      </div>
                      <ul className="max-h-96 divide-y divide-border/60 overflow-y-auto">
                        {(notifications.data ?? []).slice(0, 30).map((item) => (
                          <li key={item.id}>
                            <button
                              type="button"
                              className={cn(
                                'w-full px-3 py-2.5 text-left transition-colors hover:bg-card-hover',
                                !item.read && 'bg-primary-soft/40',
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
                              {item.body && (
                                <p className="mt-0.5 line-clamp-2 text-[11px] text-fg-muted">{item.body}</p>
                              )}
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

              <Separator className="mx-1 h-6 w-px" orientation="vertical" />

              <div className="flex items-center gap-2 pl-1">
                <div className="hidden text-right sm:block">
                  <div className="text-xs font-medium leading-tight">{user.display_name}</div>
                  <div className="text-[10px] text-fg-subtle">
                    {user.role === 'admin' ? '管理员' : '成员'} · {user.email}
                  </div>
                </div>
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-xs font-semibold">
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

          {user.must_change_password && (
            <div className="flex items-center gap-2 border-b border-warning/30 bg-warning-soft px-4 py-2 text-xs text-warning">
              <UserCog className="h-4 w-4" aria-hidden />
              初始密码尚未修改，请尽快到
              <NavLink to="/settings" className="underline underline-offset-2">
                个人设置
              </NavLink>
              修改。
            </div>
          )}

          <main className="min-w-0 flex-1 p-4 lg:p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}
