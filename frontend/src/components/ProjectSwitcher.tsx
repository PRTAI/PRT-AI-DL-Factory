/**
 * 顶栏的项目切换器 —— 参考 wandb / swanlab：当前上下文永远可见、一键可换。
 *
 * 这里只做「快速切换」：搜索 + 最近活跃的项目。要按负责人 / 训练·评测做联合筛选，
 * 走「全部项目」到 `/projects`，那边用的是同一套卡片。
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Plus, Search, SlidersHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/lib/api'
import { useProject } from '@/lib/store'
import { cn, formatRelative } from '@/lib/utils'
import { Input, Spinner } from './ui'

export function ProjectSwitcher() {
  const { projectId, setProject } = useProject()
  const [open, setOpen] = React.useState(false)
  const [keyword, setKeyword] = React.useState('')
  const navigate = useNavigate()
  const containerRef = React.useRef<HTMLDivElement>(null)

  const projects = useQuery({ queryKey: ['projects', '', '', 'all'], queryFn: () => api.projects() })
  const rows = projects.data ?? []
  const current = rows.find((row) => row.project.id === projectId)

  const filtered = React.useMemo(() => {
    const needle = keyword.trim().toLowerCase()
    if (!needle) return rows
    return rows.filter(({ project }) =>
      `${project.name} ${project.slug} ${project.description} ${project.owner_email} ${project.tags.join(' ')}`
        .toLowerCase()
        .includes(needle),
    )
  }, [rows, keyword])

  // Esc 关闭：这个面板会盖住页面内容，键盘用户得有出路
  React.useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={cn(
          'flex h-10 max-w-[300px] items-center gap-2.5 rounded-lg border px-2 pr-2.5 text-left transition-colors',
          open
            ? 'border-primary bg-primary-soft'
            : 'border-border bg-card hover:border-primary/60 hover:bg-card-hover',
        )}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-primary text-[11px] font-semibold text-primary-fg"
          aria-hidden
        >
          {current ? current.project.name.slice(0, 1).toUpperCase() : '·'}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[10px] leading-tight text-fg-subtle">当前项目</span>
          <span className="block truncate text-[13px] font-medium leading-tight text-fg">
            {current?.project.name ?? (projectId ? '加载中…' : '未选择')}
          </span>
        </span>
        <ChevronsUpDown className="h-3.5 w-3.5 shrink-0 text-fg-subtle" aria-hidden />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} aria-hidden />
          <div className="absolute left-0 top-12 z-50 w-[360px] overflow-hidden rounded-xl border border-border bg-card shadow-xl">
            <div className="border-b border-border p-2">
              <div className="relative">
                <Search
                  className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle"
                  aria-hidden
                />
                <Input
                  autoFocus
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                  placeholder="搜索项目"
                  className="h-8 pl-8 text-xs"
                  aria-label="搜索项目"
                />
              </div>
            </div>

            <ul className="max-h-[320px] overflow-y-auto p-1" role="listbox">
              {projects.isLoading && (
                <li className="flex justify-center py-8">
                  <Spinner />
                </li>
              )}
              {filtered.map(({ project, stats }) => {
                const active = project.id === projectId
                return (
                  <li key={project.id}>
                    <button
                      type="button"
                      role="option"
                      aria-selected={active}
                      className={cn(
                        'flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors',
                        active ? 'bg-primary-soft' : 'hover:bg-card-hover',
                      )}
                      onClick={() => {
                        setProject(project.id)
                        setOpen(false)
                        setKeyword('')
                        navigate(`/jobs?project=${encodeURIComponent(project.id)}`)
                        toast.success(`已进入项目「${project.name}」`)
                      }}
                    >
                      <span
                        className={cn(
                          'flex h-7 w-7 shrink-0 items-center justify-center rounded text-[11px] font-semibold',
                          active ? 'bg-primary text-primary-fg' : 'bg-muted text-fg-muted',
                        )}
                        aria-hidden
                      >
                        {project.name.slice(0, 1).toUpperCase()}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <span
                            className={cn(
                              'truncate text-[13px]',
                              active ? 'font-medium text-primary' : 'text-fg',
                            )}
                          >
                            {project.name}
                          </span>
                          {active && <Check className="h-3 w-3 shrink-0 text-primary" aria-hidden />}
                        </span>
                        <span className="mt-0.5 flex items-center gap-1.5 text-[10px] text-fg-subtle">
                          <span className="truncate">{project.owner_email.split('@')[0]}</span>
                          <span>·</span>
                          <span className="num shrink-0">{stats.total_jobs} 任务</span>
                          {stats.running > 0 && (
                            <span className="shrink-0 text-success">{stats.running} 运行中</span>
                          )}
                          {stats.last_activity_at && (
                            <span className="shrink-0">{formatRelative(stats.last_activity_at)}</span>
                          )}
                        </span>
                      </span>
                    </button>
                  </li>
                )
              })}
              {!projects.isLoading && !filtered.length && (
                <li className="px-3 py-8 text-center text-xs text-fg-subtle">
                  {keyword ? '没有匹配的项目' : '还没有项目'}
                </li>
              )}
            </ul>

            <div className="flex items-center gap-1 border-t border-border p-1.5">
              <button
                type="button"
                className="flex flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] text-fg-muted transition-colors hover:bg-card-hover hover:text-fg"
                onClick={() => {
                  setOpen(false)
                  navigate('/projects')
                }}
              >
                <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
                全部项目与筛选
              </button>
              <button
                type="button"
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-primary transition-colors hover:bg-primary-soft"
                onClick={() => {
                  setOpen(false)
                  navigate('/projects')
                }}
              >
                <Plus className="h-3.5 w-3.5" aria-hidden />
                新建
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
