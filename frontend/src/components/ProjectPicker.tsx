/**
 * 项目选择器：卡片网格 + 三个联合筛选条件（关键词 / 负责人 / 训练·评测）。
 *
 * 两处复用：提交页的「先选项目」闸门（居中），以及 `/projects` 管理页。
 *
 * 关键词同时匹配项目名和**项目里的任务名与负责人**，命中的任务直接列在卡片上——
 * 找「张三上周那个评测」时，不该先猜对项目名才能找到。
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  ArchiveRestore,
  ArrowRight,
  Check,
  FlaskConical,
  FolderKanban,
  GraduationCap,
  Pencil,
  Plus,
  LayoutGrid,
  Search,
  Trash2,
  Users,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useProject } from '@/lib/store'
import { cn, formatRelative } from '@/lib/utils'
import type { JobType, Project, ProjectMatch, ProjectWithStats } from '@/lib/types'
import { statusLabel } from './common'
import { Badge, Button, Input, LinkButton, Skeleton, Spinner, Textarea } from './ui'

type TypeFilter = 'all' | 'train' | 'verify'

const TYPE_TABS: { value: TypeFilter; label: string; Icon: typeof GraduationCap }[] = [
  { value: 'all', label: '全部', Icon: LayoutGrid },
  { value: 'train', label: '训练', Icon: GraduationCap },
  { value: 'verify', label: '评测', Icon: FlaskConical },
]

export function ProjectPicker({
  title = '选择项目',
  description = '训练与评测任务都归属于项目。项目和任务在团队内共享，可以直接进入别人的项目查看。',
  centered = false,
  manage = false,
  onPicked,
  onEdit,
  actions,
}: {
  title?: string
  description?: React.ReactNode
  /** 提交页的闸门用：整块居中显示 */
  centered?: boolean
  /** 管理页用：卡片上多出编辑 / 归档 / 删除（仍然只有负责人与管理员可点） */
  manage?: boolean
  onPicked?: (projectId: string) => void
  onEdit?: (project: Project) => void
  actions?: React.ReactNode
}) {
  const { user } = useAuth()
  const { projectId, setProject } = useProject()
  // 管理页把筛选同步到 URL，这样「张伟的评测」这种视图可以直接甩链接给别人；
  // 提交页的闸门不同步，免得和 ?from=<job> 之类的参数混在一起。
  const [searchParams, setSearchParams] = useSearchParams()
  const syncUrl = manage
  const [keyword, setKeyword] = React.useState(syncUrl ? (searchParams.get('q') ?? '') : '')
  const [debounced, setDebounced] = React.useState(keyword.trim())
  const [ownerId, setOwnerId] = React.useState(syncUrl ? (searchParams.get('owner') ?? '') : '')
  const [typeFilter, setTypeFilter] = React.useState<TypeFilter>(
    syncUrl ? ((searchParams.get('type') as TypeFilter) ?? 'all') : 'all',
  )
  const [creating, setCreating] = React.useState(false)

  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(keyword.trim()), 250)
    return () => clearTimeout(timer)
  }, [keyword])

  React.useEffect(() => {
    if (!syncUrl) return
    const next = new URLSearchParams()
    if (debounced) next.set('q', debounced)
    if (ownerId) next.set('owner', ownerId)
    if (typeFilter !== 'all') next.set('type', typeFilter)
    setSearchParams(next, { replace: true })
  }, [syncUrl, debounced, ownerId, typeFilter, setSearchParams])

  // 「训练」在后端是 sft + rl 两种类型，所以要发两次再合并
  const jobTypes: (JobType | undefined)[] =
    typeFilter === 'verify' ? ['verify'] : typeFilter === 'train' ? ['sft', 'rl'] : [undefined]

  const projects = useQuery({
    queryKey: ['projects', debounced, ownerId, typeFilter],
    queryFn: async () => {
      const batches = await Promise.all(
        jobTypes.map((jobType) =>
          api.projects({ q: debounced, owner_id: ownerId, job_type: jobType }),
        ),
      )
      if (batches.length === 1) return batches[0]
      // sft / rl 两批按项目合并，命中数相加、命中任务去重
      const merged = new Map<string, ProjectWithStats>()
      for (const row of batches.flat()) {
        const existing = merged.get(row.project.id)
        if (!existing) {
          merged.set(row.project.id, { ...row, matches: [...row.matches] })
          continue
        }
        existing.matched_jobs += row.matched_jobs
        const seen = new Set(existing.matches.map((m) => m.id))
        existing.matches = [...existing.matches, ...row.matches.filter((m) => !seen.has(m.id))]
          .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
          .slice(0, 3)
      }
      return [...merged.values()].sort((a, b) => b.matched_jobs - a.matched_jobs)
    },
  })

  const members = useQuery({ queryKey: ['team-members'], queryFn: () => api.teamMembers() })

  const filtering = Boolean(debounced || ownerId || typeFilter !== 'all')
  const rows = projects.data ?? []

  const pick = (id: string, name: string) => {
    setProject(id)
    toast.success(`已进入项目「${name}」`)
    onPicked?.(id)
  }

  const reset = () => {
    setKeyword('')
    setOwnerId('')
    setTypeFilter('all')
  }

  return (
    <div className={cn('w-full', centered && 'mx-auto max-w-5xl py-6')}>
      {/* ---------------------------------------------------------- 标题 -- */}
      <div className={cn('mb-5', centered && 'text-center')}>
        {centered && (
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-primary-soft">
            <FolderKanban className="h-5 w-5 text-primary" aria-hidden />
          </div>
        )}
        <div className={cn('flex items-start gap-3', centered ? 'justify-center' : 'justify-between')}>
          <div className={cn('min-w-0', centered && 'max-w-2xl')}>
            <h2 className="text-lg font-semibold tracking-tight text-fg">{title}</h2>
            <p className="mt-1 text-xs leading-relaxed text-fg-muted">{description}</p>
          </div>
          {!centered && actions}
        </div>
      </div>

      {/* ---------------------------------------------------------- 筛选 -- */}
      <div className="mb-4 rounded-xl border border-border bg-card p-2.5 shadow-sm">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[240px] flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-subtle"
              aria-hidden
            />
            <Input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索项目名、任务名称、备注、标签…"
              className="h-10 pl-9 pr-8"
              aria-label="搜索项目与任务"
            />
            {keyword && (
              <button
                type="button"
                aria-label="清空关键词"
                onClick={() => setKeyword('')}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-fg-subtle transition-colors hover:text-fg"
              >
                <X className="h-3.5 w-3.5" aria-hidden />
              </button>
            )}
          </div>

          {/* 负责人 */}
          <div className="relative">
            <Users
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-subtle"
              aria-hidden
            />
            <select
              value={ownerId}
              onChange={(event) => setOwnerId(event.target.value)}
              aria-label="按负责人筛选"
              className={cn(
                'h-10 min-w-[168px] appearance-none rounded-md border border-border bg-card pl-9 pr-7',
                'text-sm text-fg transition-colors hover:border-border-strong focus:border-primary focus:outline-none',
              )}
            >
              <option value="">全部负责人</option>
              {user && <option value={user.id}>我（{user.display_name}）</option>}
              {(members.data ?? [])
                .filter((member) => member.id !== user?.id)
                .map((member) => (
                  <option key={member.id} value={member.id}>
                    {member.display_name}
                    {member.jobs ? ` · ${member.jobs}` : ''}
                  </option>
                ))}
            </select>
          </div>

          {/* 训练 / 评测 */}
          <div
            className="flex h-10 items-center gap-0.5 rounded-md border border-border bg-bg-elevated p-0.5"
            role="group"
            aria-label="按任务类型筛选"
          >
            {TYPE_TABS.map((tab) => (
              <button
                key={tab.value}
                type="button"
                aria-pressed={typeFilter === tab.value}
                onClick={() => setTypeFilter(tab.value)}
                className={cn(
                  'flex h-full items-center gap-1.5 rounded px-3 text-[13px] transition-colors',
                  typeFilter === tab.value
                    ? 'bg-card font-medium text-primary shadow-sm'
                    : 'text-fg-muted hover:text-fg',
                )}
              >
                <tab.Icon className="h-3.5 w-3.5" aria-hidden />
                {tab.label}
              </button>
            ))}
          </div>

          <Button variant="primary" className="h-10" onClick={() => setCreating((v) => !v)}>
            <Plus className="h-4 w-4" aria-hidden />
            新建项目
          </Button>
        </div>

        {filtering && (
          <div className="mt-2 flex items-center gap-2 border-t border-border pt-2 text-[11px] text-fg-subtle">
            <span>
              命中 {rows.length} 个项目
              {rows.length > 0 && ` · ${rows.reduce((sum, row) => sum + row.matched_jobs, 0)} 个任务`}
            </span>
            <button type="button" className="link" onClick={reset}>
              清除筛选
            </button>
          </div>
        )}

        {creating && (
          <CreateInline
            onDone={(project) => {
              setCreating(false)
              pick(project.id, project.name)
            }}
            onCancel={() => setCreating(false)}
          />
        )}
      </div>

      {/* ---------------------------------------------------------- 列表 -- */}
      {projects.isLoading ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {[0, 1, 2, 3].map((index) => (
            <Skeleton key={index} className="h-44 w-full rounded-xl" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-card px-6 py-14 text-center">
          <FolderKanban className="mx-auto h-8 w-8 text-fg-subtle" aria-hidden />
          <p className="mt-3 text-sm font-medium text-fg">
            {filtering ? '没有符合条件的项目' : '还没有任何项目'}
          </p>
          <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-fg-muted">
            {filtering
              ? '换个关键词，或者清除筛选看看全部项目。'
              : '项目是任务的归属单位。创建一个项目，就可以在里面提交训练与评测了。'}
          </p>
          <div className="mt-4 flex justify-center gap-2">
            {filtering && (
              <Button variant="secondary" onClick={reset}>
                清除筛选
              </Button>
            )}
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" aria-hidden />
              新建项目
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          {rows.map((row) => (
            <ProjectCard
              key={row.project.id}
              row={row}
              current={row.project.id === projectId}
              filtering={filtering}
              canManage={manage && (user?.role === 'admin' || row.project.owner_id === user?.id)}
              onPick={() => pick(row.project.id, row.project.name)}
              onEdit={onEdit}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* 卡片                                                                        */
/* -------------------------------------------------------------------------- */
function ProjectCard({
  row,
  current,
  filtering,
  canManage,
  onPick,
  onEdit,
}: {
  row: ProjectWithStats
  current: boolean
  filtering: boolean
  canManage?: boolean
  onPick: () => void
  onEdit?: (project: Project) => void
}) {
  const { project, stats, matches, matched_jobs: matchedJobs } = row
  const { clearProject } = useProject()
  const queryClient = useQueryClient()

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['projects'] })
  const archive = useMutation({
    mutationFn: () => api.updateProject(project.id, { archived: !project.archived }),
    onSuccess: (updated) => {
      toast.success(updated.archived ? `已归档「${updated.name}」` : `已恢复「${updated.name}」`)
      if (updated.archived && current) clearProject()
      invalidate()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })
  const remove = useMutation({
    mutationFn: () => api.deleteProject(project.id),
    onSuccess: () => {
      toast.success('项目已删除')
      if (current) clearProject()
      invalidate()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <div
      className={cn(
        'group flex flex-col rounded-xl border bg-card transition-all',
        current
          ? 'border-primary/60 ring-1 ring-primary/25'
          : 'border-border hover:border-primary/40 hover:shadow-md',
      )}
    >
      {/* 头部 */}
      <div className="flex items-start gap-3 px-4 pt-3.5">
        <span
          className={cn(
            'mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[13px] font-semibold',
            current ? 'bg-primary text-primary-fg' : 'bg-primary-soft text-primary',
          )}
          aria-hidden
        >
          {project.name.slice(0, 1).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h3 className="truncate text-[14px] font-semibold text-fg" title={project.name}>
              {project.name}
            </h3>
            {current && (
              <Badge tone="primary" className="shrink-0">
                <Check className="h-2.5 w-2.5" aria-hidden />
                当前
              </Badge>
            )}
            {project.archived && <Badge className="shrink-0">已归档</Badge>}
          </div>
          <p className="mt-0.5 truncate text-[11px] text-fg-subtle">
            负责人 {project.owner_email}
            {stats.members.length > 1 && ` · ${stats.members.length} 人参与`}
            {stats.last_activity_at && ` · ${formatRelative(stats.last_activity_at)}`}
          </p>
        </div>
      </div>

      {project.description && (
        <p className="mt-2 line-clamp-2 px-4 text-[11px] leading-relaxed text-fg-muted">
          {project.description}
        </p>
      )}

      {/* 统计 */}
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5 px-4">
        <Chip label="任务" value={stats.total_jobs} />
        {stats.training_jobs > 0 && <Chip label="训练" value={stats.training_jobs} tone="primary" />}
        {stats.verify_jobs > 0 && <Chip label="评测" value={stats.verify_jobs} tone="info" />}
        {stats.review_tasks > 0 && (
          <Link to={`/review?project=${project.id}`} title="查看项目的质检记录">
            <Chip label="质检" value={stats.review_tasks} tone="primary" />
          </Link>
        )}
        {stats.running > 0 && <Chip label="运行中" value={stats.running} tone="success" pulse />}
        {stats.pending_review > 0 && <Chip label="待审" value={stats.pending_review} tone="warning" />}
        {project.tags.map((tag) => (
          <span key={tag} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-fg-muted">
            {tag}
          </span>
        ))}
      </div>

      {/* 命中 / 最近的任务 —— 团队里别人的任务也在这里 */}
      <div className="mt-3 min-h-[64px] flex-1 border-t border-border px-4 py-2">
        {matches.length ? (
          <>
            <p className="mb-1 text-[10px] uppercase tracking-wider text-fg-subtle">
              {filtering ? `命中 ${matchedJobs} 个任务` : '最近的任务'}
            </p>
            <ul className="space-y-0.5">
              {matches.map((match) => (
                <li key={match.id}>
                  <MatchRow match={match} />
                </li>
              ))}
            </ul>
            {matchedJobs > matches.length && (
              <p className="mt-1 text-[10px] text-fg-subtle">还有 {matchedJobs - matches.length} 个…</p>
            )}
          </>
        ) : (
          <p className="pt-3 text-center text-[11px] text-fg-subtle">
            {filtering ? '项目名匹配，但没有符合条件的任务' : '还没有任务'}
          </p>
        )}
      </div>

      {/* 操作 */}
      <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2.5">
        {canManage ? (
          <div className="flex items-center gap-2.5">
            <LinkButton onClick={() => onEdit?.(project)}>
              <Pencil className="mr-0.5 inline h-3 w-3" aria-hidden />
              编辑
            </LinkButton>
            <LinkButton onClick={() => archive.mutate()} disabled={archive.isPending}>
              {project.archived ? (
                <>
                  <ArchiveRestore className="mr-0.5 inline h-3 w-3" aria-hidden />
                  恢复
                </>
              ) : (
                <>
                  <Archive className="mr-0.5 inline h-3 w-3" aria-hidden />
                  归档
                </>
              )}
            </LinkButton>
            <LinkButton
              tone="danger"
              disabled={stats.total_jobs > 0 || remove.isPending}
              title={stats.total_jobs > 0 ? '项目下还有任务，只能归档' : undefined}
              onClick={() => {
                if (window.confirm(`确定删除项目「${project.name}」？`)) remove.mutate()
              }}
            >
              <Trash2 className="mr-0.5 inline h-3 w-3" aria-hidden />
              删除
            </LinkButton>
          </div>
        ) : (
          <Link
            to={`/jobs?project=${project.id}`}
            className="text-[11px] text-fg-muted transition-colors hover:text-primary"
          >
            查看全部任务
          </Link>
        )}
        <Button
          size="sm"
          variant={current ? 'secondary' : 'primary'}
          onClick={onPick}
          disabled={project.archived}
          title={project.archived ? '已归档的项目不能提交新任务' : undefined}
        >
          {current ? '继续使用' : '进入项目'}
          <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </div>
    </div>
  )
}

function MatchRow({ match }: { match: ProjectMatch }) {
  const Icon = match.type === 'verify' ? FlaskConical : GraduationCap
  const tone =
    match.status === 'running'
      ? 'text-success'
      : match.status === 'failed'
        ? 'text-danger'
        : match.status === 'pending_review'
          ? 'text-warning'
          : 'text-fg-subtle'
  return (
    <Link
      to={`/jobs/${match.id}`}
      className="flex items-center gap-1.5 rounded px-1 py-0.5 -mx-1 transition-colors hover:bg-card-hover"
    >
      <Icon className="h-3 w-3 shrink-0 text-fg-subtle" aria-hidden />
      <span className="min-w-0 flex-1 truncate text-[11px] text-fg" title={match.name}>
        {match.name}
      </span>
      <span className="shrink-0 text-[10px] text-fg-subtle">{match.owner_email.split('@')[0]}</span>
      <span className={cn('shrink-0 text-[10px]', tone)}>{statusLabel(match.status)}</span>
    </Link>
  )
}

function Chip({
  label,
  value,
  tone = 'neutral',
  pulse,
}: {
  label: string
  value: number
  tone?: 'neutral' | 'primary' | 'info' | 'success' | 'warning'
  pulse?: boolean
}) {
  const toneClass = {
    neutral: 'bg-muted text-fg-muted',
    primary: 'bg-primary-soft text-primary',
    info: 'bg-info-soft text-info',
    success: 'bg-success-soft text-success',
    warning: 'bg-warning-soft text-warning',
  }[tone]
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium',
        toneClass,
      )}
    >
      {pulse && <span className="h-1 w-1 rounded-full bg-current pulse-live" aria-hidden />}
      {label}
      <span className="num">{value}</span>
    </span>
  )
}

/* -------------------------------------------------------------------------- */
/* 内联创建                                                                    */
/* -------------------------------------------------------------------------- */
function CreateInline({
  onDone,
  onCancel,
}: {
  onDone: (project: { id: string; name: string }) => void
  onCancel: () => void
}) {
  const [name, setName] = React.useState('')
  const [description, setDescription] = React.useState('')
  const queryClient = useQueryClient()

  const create = useMutation({
    mutationFn: () => api.createProject({ name: name.trim(), description: description.trim() }),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      onDone(project)
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <form
      className="mt-2 space-y-2 border-t border-border pt-2.5"
      onSubmit={(event) => {
        event.preventDefault()
        if (name.trim()) create.mutate()
      }}
    >
      <div className="flex flex-wrap items-start gap-2">
        <Input
          autoFocus
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="项目名称，例如 Qwen3-4B 终端能力对齐"
          className="min-w-[240px] flex-1"
          aria-label="项目名称"
        />
        <Button type="submit" variant="primary" loading={create.isPending} disabled={!name.trim()}>
          创建并进入
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          取消
        </Button>
      </div>
      <Textarea
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder="这个项目要验证什么？（选填，方便别人理解）"
        className="min-h-14 text-xs"
        aria-label="项目描述"
      />
      {create.isPending && (
        <p className="flex items-center gap-1.5 text-[11px] text-fg-subtle">
          <Spinner className="h-3 w-3" />
          创建中…
        </p>
      )}
    </form>
  )
}
