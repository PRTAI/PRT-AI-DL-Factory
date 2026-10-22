import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, ListChecks, RefreshCw, Search, Trash2, Users } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useProject } from '@/lib/store'
import type { Job, JobStatus, JobType } from '@/lib/types'
import { elapsedSeconds, formatDuration, formatRelative } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  HelpTip,
  Input,
  Progress,
  Select,
  Table,
  Td,
  Textarea,
  Th,
  Tooltip,
  Tr,
} from '@/components/ui'
import { JOB_STATUS_OPTIONS, JobTypeBadge, PageHeader, StatusBadge } from '@/components/common'
import { ProjectTag } from '@/components/ProjectTag'
import { JobCost } from '@/components/Billing'

const TYPE_OPTIONS: { value: JobType | 'all'; label: string }[] = [
  { value: 'all', label: '全部类型' },
  { value: 'sft', label: '监督训练' },
  { value: 'rl', label: '强化学习' },
  { value: 'verify', label: 'Agent 评测' },
]

export default function Jobs() {
  const { user } = useAuth()
  const { projectId, setProject } = useProject()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [status, setStatus] = React.useState<JobStatus | 'all'>(
    (searchParams.get('status') as JobStatus) ?? 'all',
  )
  const [type, setType] = React.useState<JobType | 'all'>((searchParams.get('type') as JobType) ?? 'all')
  const [query, setQuery] = React.useState(searchParams.get('q') ?? '')
  const [ownerId, setOwnerId] = React.useState(searchParams.get('owner') ?? '')
  // 默认只看当前项目；切成「全部项目」可以横向对比别的项目在跑什么
  const [scopeToProject, setScopeToProject] = React.useState(searchParams.get('all') !== '1')

  // 项目卡片上的「查看全部任务」带 ?project=<id> 过来：切过去并只看它
  const urlProject = searchParams.get('project') ?? ''
  React.useEffect(() => {
    if (!urlProject) return
    setProject(urlProject)
    setScopeToProject(true)
  }, [urlProject, setProject])
  const [reviewTarget, setReviewTarget] = React.useState<Job | null>(null)
  const [reviewComment, setReviewComment] = React.useState('')

  React.useEffect(() => {
    const next = new URLSearchParams()
    if (status !== 'all') next.set('status', status)
    if (type !== 'all') next.set('type', type)
    if (query) next.set('q', query)
    if (ownerId) next.set('owner', ownerId)
    if (!scopeToProject) next.set('all', '1')
    setSearchParams(next, { replace: true })
  }, [status, type, query, ownerId, scopeToProject, setSearchParams])

  const projects = useQuery({ queryKey: ['projects', '', '', 'all'], queryFn: () => api.projects() })
  const projectName = projects.data?.find((row) => row.project.id === projectId)?.project.name
  const members = useQuery({ queryKey: ['team-members'], queryFn: () => api.teamMembers() })

  const effectiveProject = scopeToProject ? projectId : ''
  const jobs = useQuery({
    queryKey: ['jobs', status, type, query, ownerId, effectiveProject],
    queryFn: () =>
      api.jobs({
        status: status === 'all' ? undefined : [status],
        type: type === 'all' ? undefined : [type],
        project_id: effectiveProject || undefined,
        owner_id: ownerId || undefined,
        q: query || undefined,
        limit: 200,
      }),
    refetchInterval: 8000,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
    queryClient.invalidateQueries({ queryKey: ['job-overview'] })
    queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  }

  const review = useMutation({
    mutationFn: ({ id, approve, comment }: { id: string; approve: boolean; comment: string }) =>
      api.reviewJob(id, approve, comment),
    onSuccess: (job) => {
      toast.success(job.status === 'queued' ? `已通过审核，任务进入排队：${job.name}` : `已驳回：${job.name}`)
      setReviewTarget(null)
      setReviewComment('')
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '操作失败'),
  })

  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelJob(id, '在任务列表终止'),
    onSuccess: () => {
      toast.success('已发送终止信号')
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '终止失败'),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteJob(id),
    onSuccess: () => {
      toast.success('任务记录已删除')
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '删除失败'),
  })

  const [selected, setSelected] = React.useState<string[]>([])
  const [batchResults, setBatchResults] = React.useState<Awaited<ReturnType<typeof api.batchResubmitJobs>> | null>(null)
  const batch = useMutation({
    mutationFn: (ids: string[]) => api.batchResubmitJobs(ids),
    retry: false,
    onSuccess: (result) => {
      setBatchResults(result)
      setSelected([])
      const message = `批量重跑：已创建 ${result.succeeded} 个任务，${result.failed} 个失败`
      if (!result.succeeded) toast.error(message)
      else if (result.failed) toast.warning(message)
      else toast.success(message)
      invalidate()
    },
    onError: () => {
      setSelected([])
      toast.error('未能确认批量重跑结果，请刷新任务列表检查已创建的任务，避免重复提交')
      invalidate()
    },
  })
  React.useEffect(() => {
    setSelected([])
  }, [status, type, query, ownerId, effectiveProject])
  const canResubmit = (job: Job) =>
    (user?.role === 'admin' || job.owner_id === user?.id) && ['succeeded', 'failed', 'cancelled', 'rejected'].includes(job.status)
  const items = jobs.data?.items ?? []
  const selectable = items.map((job) => job.id)
  const selectedVisible = selected.filter((id) => selectable.includes(id))
  const selectedResubmittable = items.filter((job) => selectedVisible.includes(job.id) && canResubmit(job)).map((job) => job.id)
  const batchDownload = useMutation({
    mutationFn: (ids: string[]) => api.batchDownloadJobs(ids),
    retry: false,
    onSuccess: () => toast.success('批量导出已开始下载'),
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '批量导出失败'),
  })
  const selectionBusy = batch.isPending || batchDownload.isPending
  const allSelected = selectable.length > 0 && selectedVisible.length === selectable.length
  const selectionRef = React.useRef<HTMLInputElement>(null)
  React.useEffect(() => {
    if (selectionRef.current) selectionRef.current.indeterminate = selectedVisible.length > 0 && !allSelected
  }, [selectedVisible.length, allSelected])
  const [downloading, setDownloading] = React.useState<string[]>([])
  const downloadJob = async (id: string) => {
    setDownloading((ids) => [...ids, id])
    try {
      await api.downloadJob(id)
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '导出下载失败')
    } finally {
      setDownloading((ids) => ids.filter((item) => item !== id))
    }
  }
  // 待审提醒要用**全量**计数：它是在催管理员去审，不该被当前筛选条件缩小
  const overview = useQuery({
    queryKey: ['job-overview', ''],
    queryFn: () => api.jobOverview(),
    refetchInterval: 15000,
  })
  const pendingCount = overview.data?.pending_review ?? 0

  return (
    <div className="space-y-4">
      <PageHeader
        title="任务列表"
        description={
          scopeToProject && projectId
            ? `当前项目：${projectName ?? projectId}。任务在团队内共享，写操作仍限于负责人与管理员。`
            : '全部项目的训练与评测任务，团队内共享可见'
        }
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => jobs.refetch()}>
              <RefreshCw className={`h-3.5 w-3.5 ${jobs.isFetching ? 'animate-spin' : ''}`} aria-hidden />
              刷新
            </Button>
            <Link to="/submit/train">
              <Button variant="primary" size="sm">
                提交训练
              </Button>
            </Link>
            <Link to="/submit/verify">
              <Button variant="secondary" size="sm">
                提交评测
              </Button>
            </Link>
          </>
        }
      />

      {user?.role === 'admin' && pendingCount > 0 && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-xs text-warning">
          <span>全平台有 {pendingCount} 个任务等待审核，通过后才会进入 GPU 排队队列。</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setStatus('pending_review')
              setScopeToProject(false)
              setOwnerId('')
            }}
          >
            只看待审核
          </Button>
        </div>
      )}

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <div className="relative min-w-52 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索任务名 / 备注 / 标签 / 任务 ID"
              className="pl-8"
              aria-label="搜索任务"
            />
          </div>
          <Select
            value={status}
            onValueChange={(value) => setStatus(value as JobStatus | 'all')}
            className="w-36"
            options={[{ value: 'all', label: '全部状态' }, ...JOB_STATUS_OPTIONS]}
          />
          <Select
            value={type}
            onValueChange={(value) => setType(value as JobType | 'all')}
            className="w-36"
            options={TYPE_OPTIONS}
          />
          <Select
            value={scopeToProject ? projectId || 'all' : 'all'}
            onValueChange={(value) => {
              if (value === 'all') {
                setScopeToProject(false)
              } else {
                setProject(value)
                setScopeToProject(true)
              }
            }}
            className="w-44"
            options={[
              { value: 'all', label: '全部项目' },
              ...(projects.data ?? []).map((row) => ({
                value: row.project.id,
                label: row.project.name,
                description: `${row.stats.total_jobs} 个任务`,
              })),
            ]}
          />
          {/* 负责人：任务在团队内共享，所以这里默认是「全部负责人」 */}
          <div className="relative">
            <Users
              className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle"
              aria-hidden
            />
            <select
              value={ownerId}
              onChange={(event) => setOwnerId(event.target.value)}
              aria-label="按负责人筛选"
              className="h-9 w-40 appearance-none rounded-md border border-border bg-card pl-8 pr-6 text-sm text-fg transition-colors hover:border-border-strong focus:border-primary focus:outline-none"
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
          {(query || status !== 'all' || type !== 'all' || ownerId) && (
            <button
              type="button"
              className="link text-xs"
              onClick={() => {
                setQuery('')
                setStatus('all')
                setType('all')
                setOwnerId('')
              }}
            >
              清除筛选
            </button>
          )}
        </div>

        {selectedVisible.length > 0 && <div className="flex flex-wrap items-center gap-2 border-b border-border bg-bg-elevated px-3 py-2 text-xs text-fg-muted">
          <span className="flex items-center gap-1">
            已选 {selectedVisible.length} 项 · {selectedResubmittable.length} 项可重跑
            <HelpTip>导出：评测任务仅打包 Harbor 原始记录；训练任务包含配置、日志、指标与产物。重跑：仅限已结束且有操作权限的任务。全选仅作用于当前列表。</HelpTip>
          </span>
          <Button size="sm" variant="primary" loading={batchDownload.isPending} disabled={selectionBusy} onClick={() => batchDownload.mutate([...selectedVisible])}>批量导出</Button>
          <Button size="sm" variant="outline" loading={batch.isPending} disabled={!selectedResubmittable.length || selectionBusy} onClick={() => { setBatchResults(null); batch.mutate([...selectedResubmittable]) }}>批量重跑</Button>
          <Button size="sm" variant="ghost" disabled={selectionBusy} onClick={() => setSelected([])}>清空</Button>
        </div>}
        {batchResults && <div className="max-h-64 space-y-1 overflow-auto border-b border-border p-3 text-xs" role="status">
          <p>已创建 {batchResults.succeeded} 个任务，失败 {batchResults.failed} 个</p>
          {batchResults.results.map((row) => <div key={row.source_id} className="break-all">
            <Link className="text-primary" to={`/jobs/${row.source_id}`}>{row.source_id}</Link>
            {row.job_id ? <> → <Link className="text-primary" to={`/jobs/${row.job_id}`}>查看新任务</Link></> : <span className="text-danger">：{row.error}</span>}
          </div>)}
        </div>}

        {items.length ? (
          <Table className="min-w-[1180px] table-fixed">
            <thead>
              {/* 为操作预留宽度；窄屏在表格内横向滚动，避免按钮文字竖排。 */}
              <tr>
                <Th className="w-[44px]"><input ref={selectionRef} type="checkbox" aria-label="全选当前列表任务" checked={allSelected} disabled={!selectable.length || selectionBusy} onChange={(event) => setSelected(event.target.checked ? selectable : [])} /></Th>
                <Th className="w-[240px]">任务</Th>
                {/* 每个任务都属于一个项目，所以这一列常驻 —— 哪怕已经按项目筛过，
                    看列表的人也不该靠记忆确认自己在看哪个项目 */}
                <Th className="w-[140px]">项目</Th>
                <Th className="w-[160px]">类型</Th>
                <Th className="w-[88px]">状态</Th>
                <Th className="w-[150px]">进度</Th>
                <Th className="w-[64px]">GPU</Th>
                <Th className="w-[80px]">耗时</Th>
                <Th className="w-[100px]">提交</Th>
                <Th className="w-[120px] whitespace-nowrap">操作</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((job) => {
                const canControl = user?.role === 'admin' || job.owner_id === user?.id
                const isActive = ['queued', 'preparing', 'running'].includes(job.status)
                return (
                  <Tr key={job.id}>
                    <Td><input type="checkbox" aria-label={`选择任务 ${job.name}`} checked={selectedVisible.includes(job.id)} disabled={selectionBusy} onChange={(event) => setSelected((ids) => event.target.checked ? [...ids, job.id] : ids.filter((id) => id !== job.id))} /></Td>
                    <Td>
                      <Link to={`/jobs/${job.id}`} className="block min-w-0">
                        <div className="truncate font-medium text-fg hover:text-primary">{job.name}</div>
                        <div className="num mt-0.5 truncate text-[11px] text-fg-subtle">{job.id}</div>
                      </Link>
                      {job.note && <div className="mt-0.5 line-clamp-1 text-[11px] text-fg-muted">{job.note}</div>}
                      <JobCost job={job} />
                    </Td>
                    <Td>
                      <ProjectTag projectId={job.project_id} className="max-w-full text-xs" />
                    </Td>
                    <Td>
                      <JobTypeBadge type={job.type} framework={job.framework} />
                    </Td>
                    <Td>
                      <StatusBadge status={job.status} />
                    </Td>
                    <Td>
                      {isActive || job.status === 'succeeded' ? (
                        <>
                          <Progress
                            value={job.progress.percent}
                            showLabel
                            tone={job.status === 'succeeded' ? 'success' : 'info'}
                          />
                          <div className="mt-0.5 truncate text-[11px] text-fg-subtle">{job.progress.message}</div>
                        </>
                      ) : (
                        <span className="text-[11px] text-fg-subtle">{job.progress.message || '—'}</span>
                      )}
                    </Td>
                    <Td className="num text-xs">
                      {job.resources.gpu_ids.length ? job.resources.gpu_ids.join(',') : `×${job.resources.n_gpus}`}
                    </Td>
                    <Td className="num text-xs text-fg-muted">
                      {formatDuration(elapsedSeconds(job.runtime.started_at, job.runtime.finished_at))}
                    </Td>
                    <Td>
                      <div className="text-xs text-fg-muted">{job.owner_email.split('@')[0]}</div>
                      <div className="text-[11px] text-fg-subtle">{formatRelative(job.created_at)}</div>
                    </Td>
                    <Td>
                      <div className="flex items-center gap-1">
                        <Tooltip label={job.type === 'verify' ? '导出 Harbor 原始记录（ZIP）' : '导出下载（配置、日志、指标与当前产物 ZIP）'}>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="w-8 justify-center px-0"
                            aria-label="导出下载"
                            loading={downloading.includes(job.id)}
                            onClick={() => downloadJob(job.id)}
                          >
                            {!downloading.includes(job.id) && <Download className="h-3.5 w-3.5" aria-hidden />}
                          </Button>
                        </Tooltip>
                        {user?.role === 'admin' && job.status === 'pending_review' && (
                          <>
                            <Button
                              variant="outline"
                              size="sm"
                              className="border-success/30 px-2 text-success hover:border-success hover:bg-success-soft hover:text-success"
                              onClick={() => review.mutate({ id: job.id, approve: true, comment: '' })}
                              disabled={review.isPending}
                            >
                              通过
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              className="border-danger/30 px-2 text-danger hover:border-danger hover:bg-danger-soft hover:text-danger"
                              onClick={() => {
                                setReviewTarget(job)
                                setReviewComment('')
                              }}
                            >
                              驳回
                            </Button>
                          </>
                        )}
                        {canControl && isActive && (
                          <Button
                            variant="outline"
                            size="sm"
                            className="border-danger/30 px-2 text-danger hover:border-danger hover:bg-danger-soft hover:text-danger"
                            disabled={cancel.isPending && cancel.variables === job.id}
                            onClick={() => cancel.mutate(job.id)}
                          >
                            终止
                          </Button>
                        )}
                        {canControl && !isActive && (
                          <Tooltip label="删除记录（训练产物保留）">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="w-8 justify-center px-0 text-danger hover:bg-danger-soft hover:text-danger"
                              aria-label="删除"
                              disabled={remove.isPending && remove.variables === job.id}
                              onClick={() => {
                                if (confirm(`确认删除任务「${job.name}」的记录？训练产物会保留。`)) remove.mutate(job.id)
                              }}
                            >
                              <Trash2 className="h-3.5 w-3.5" aria-hidden />
                            </Button>
                          </Tooltip>
                        )}
                      </div>
                    </Td>
                  </Tr>
                )
              })}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            icon={<ListChecks className="h-6 w-6" aria-hidden />}
            title="没有符合条件的任务"
            description="调整筛选条件，或提交一个新的训练 / 评测任务。"
            action={
              <Link to="/submit/train">
                <Button variant="primary" size="sm">
                  提交训练任务
                </Button>
              </Link>
            }
          />
        )}
      </Card>

      <Dialog
        open={reviewTarget !== null}
        onOpenChange={(open) => !open && setReviewTarget(null)}
        title={`驳回任务：${reviewTarget?.name ?? ''}`}
        description="驳回原因会通知提交人，请写明需要调整的内容。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setReviewTarget(null)}>
              取消
            </Button>
            <Button
              variant="danger"
              loading={review.isPending}
              onClick={() =>
                reviewTarget && review.mutate({ id: reviewTarget.id, approve: false, comment: reviewComment })
              }
            >
              确认驳回
            </Button>
          </>
        }
      >
        <Textarea
          value={reviewComment}
          onChange={(event) => setReviewComment(event.target.value)}
          placeholder="例如：batch size 过大，单卡会 OOM，请调整到 2 后重新提交。"
          rows={4}
        />
        {reviewTarget && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            <Badge>{reviewTarget.framework}</Badge>
            <Badge>GPU ×{reviewTarget.resources.n_gpus}</Badge>
            <Badge>{reviewTarget.owner_email}</Badge>
          </div>
        )}
      </Dialog>
    </div>
  )
}
