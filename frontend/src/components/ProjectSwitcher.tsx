/**
 * 顶栏的项目切换器 —— 参考 wandb / swanlab：当前上下文永远可见、一键可换。
 * 项目列表全员共享，所以这里带搜索；创建入口也放在这里，省一次跳转。
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronsUpDown, FolderKanban, Plus, Search, Settings2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useProject } from '@/lib/store'
import { cn, formatRelative } from '@/lib/utils'
import { Button, Input, Spinner } from './ui'

export function ProjectSwitcher() {
  const { projectId, setProject } = useProject()
  const [open, setOpen] = React.useState(false)
  const [keyword, setKeyword] = React.useState('')
  const [creating, setCreating] = React.useState(false)
  const [name, setName] = React.useState('')
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.projects() })
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

  const create = useMutation({
    mutationFn: (payload: { name: string }) => api.createProject(payload),
    onSuccess: (project) => {
      toast.success(`已创建项目「${project.name}」`)
      setProject(project.id)
      setName('')
      setCreating(false)
      setOpen(false)
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={cn(
          'flex h-9 max-w-[280px] items-center gap-2 rounded-md border border-border bg-card px-2.5 text-left',
          'transition-colors hover:border-primary',
        )}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <FolderKanban className="h-4 w-4 shrink-0 text-primary" aria-hidden />
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
          <div className="absolute left-0 top-11 z-50 w-[340px] overflow-hidden rounded-lg border border-border bg-card shadow-xl">
            <div className="border-b border-border p-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
                <Input
                  autoFocus
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                  placeholder="搜索项目 / 负责人 / 标签"
                  className="h-8 pl-8 text-xs"
                  aria-label="搜索项目"
                />
              </div>
            </div>

            <ul className="max-h-72 overflow-y-auto py-1" role="listbox">
              {projects.isLoading && (
                <li className="flex justify-center py-6">
                  <Spinner />
                </li>
              )}
              {filtered.map(({ project, stats }) => (
                <li key={project.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={project.id === projectId}
                    className={cn(
                      'flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-card-hover',
                      project.id === projectId && 'bg-primary-soft',
                    )}
                    onClick={() => {
                      setProject(project.id)
                      setOpen(false)
                      toast.success(`已切换到项目「${project.name}」`)
                    }}
                  >
                    <span className="mt-0.5 w-3.5 shrink-0">
                      {project.id === projectId && <Check className="h-3.5 w-3.5 text-primary" aria-hidden />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] font-medium text-fg">{project.name}</span>
                      <span className="block truncate text-[11px] text-fg-subtle">
                        {project.owner_email} · {stats.total_jobs} 个任务
                        {stats.running > 0 && ` · ${stats.running} 运行中`}
                        {stats.last_activity_at && ` · ${formatRelative(stats.last_activity_at)}`}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
              {!projects.isLoading && !filtered.length && (
                <li className="px-3 py-6 text-center text-xs text-fg-subtle">
                  {keyword ? '没有匹配的项目' : '还没有项目，先创建一个'}
                </li>
              )}
            </ul>

            <div className="border-t border-border p-2">
              {creating ? (
                <form
                  className="flex items-center gap-2"
                  onSubmit={(event) => {
                    event.preventDefault()
                    if (name.trim()) create.mutate({ name: name.trim() })
                  }}
                >
                  <Input
                    autoFocus
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="项目名称"
                    className="h-8 text-xs"
                    aria-label="新项目名称"
                  />
                  <Button type="submit" size="sm" variant="primary" loading={create.isPending}>
                    创建
                  </Button>
                  <Button type="button" size="sm" variant="ghost" onClick={() => setCreating(false)}>
                    取消
                  </Button>
                </form>
              ) : (
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="ghost" className="flex-1 justify-start" onClick={() => setCreating(true)}>
                    <Plus className="h-3.5 w-3.5" aria-hidden />
                    新建项目
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setOpen(false)
                      navigate('/projects')
                    }}
                  >
                    <Settings2 className="h-3.5 w-3.5" aria-hidden />
                    管理
                  </Button>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
