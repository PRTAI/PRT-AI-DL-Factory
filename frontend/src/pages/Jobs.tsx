import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ListChecks, RefreshCw, Search } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import type { Job, JobStatus, JobType } from '@/lib/types'
import { elapsedSeconds, formatDuration, formatRelative } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Input,
  LinkButton,
  Progress,
  Select,
  Table,
  Td,
  Textarea,
  Th,
  Tr,
} from '@/components/ui'
import { JOB_STATUS_OPTIONS, JobTypeBadge, PageHeader, StatusBadge } from '@/components/common'

const TYPE_OPTIONS: { value: JobType | 'all'; label: string }[] = [
  { value: 'all', label: '全部类型' },
  { value: 'sft', label: '监督训练' },
  { value: 'rl', label: '强化学习' },
  { value: 'verify', label: 'Agent 评测' },
]

export default function Jobs() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [status, setStatus] = React.useState<JobStatus | 'all'>(
    (searchParams.get('status') as JobStatus) ?? 'all',
  )
  const [type, setType] = React.useState<JobType | 'all'>((searchParams.get('type') as JobType) ?? 'all')
  const [query, setQuery] = React.useState(searchParams.get('q') ?? '')
  const [mineOnly, setMineOnly] = React.useState(searchParams.get('mine') === '1')
  const [reviewTarget, setReviewTarget] = React.useState<Job | null>(null)
  const [reviewComment, setReviewComment] = React.useState('')

  React.useEffect(() => {
    const next = new URLSearchParams()
    if (status !== 'all') next.set('status', status)
    if (type !== 'all') next.set('type', type)
    if (query) next.set('q', query)
    if (mineOnly) next.set('mine', '1')
    setSearchParams(next, { replace: true })
  }, [status, type, query, mineOnly, setSearchParams])

  const jobs = useQuery({
    queryKey: ['jobs', status, type, query, mineOnly],
    queryFn: () =>
      api.jobs({
        status: status === 'all' ? undefined : [status],
        type: type === 'all' ? undefined : [type],
        q: query || undefined,
        mine_only: mineOnly,
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

  const items = jobs.data?.items ?? []
  const pending = items.filter((job) => job.status === 'pending_review')

  return (
    <div className="space-y-4">
      <PageHeader
        title="任务列表"
        description="训练与评测任务的提交、审核与运行状态"
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

      {user?.role === 'admin' && pending.length > 0 && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-xs text-warning">
          <span>有 {pending.length} 个任务等待审核，通过后才会进入 GPU 排队队列。</span>
          <Button variant="outline" size="sm" onClick={() => setStatus('pending_review')}>
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
              placeholder="搜索任务名 / 备注 / 提交人 / 任务 ID"
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
          {user?.role === 'admin' && (
            <label className="flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-xs text-fg-muted">
              <input
                type="checkbox"
                checked={mineOnly}
                onChange={(event) => setMineOnly(event.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--primary)]"
              />
              只看我的
            </label>
          )}
        </div>

        {items.length ? (
          <Table>
            <thead>
              <tr>
                <Th>任务</Th>
                <Th className="w-44">类型</Th>
                <Th className="w-24">状态</Th>
                <Th className="w-44">进度</Th>
                <Th className="w-24">GPU</Th>
                <Th className="w-24">耗时</Th>
                <Th className="w-36">提交</Th>
                <Th className="w-[150px]">操作</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((job) => {
                const canControl = user?.role === 'admin' || job.owner_id === user?.id
                const isActive = ['queued', 'preparing', 'running'].includes(job.status)
                return (
                  <Tr key={job.id}>
                    <Td>
                      <Link to={`/jobs/${job.id}`} className="block min-w-0">
                        <div className="truncate font-medium text-fg hover:text-primary">{job.name}</div>
                        <div className="num mt-0.5 truncate text-[11px] text-fg-subtle">{job.id}</div>
                      </Link>
                      {job.note && <div className="mt-0.5 line-clamp-1 text-[11px] text-fg-muted">{job.note}</div>}
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
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                        <Link to={`/jobs/${job.id}`} className="link text-xs">
                          详情
                        </Link>
                        {user?.role === 'admin' && job.status === 'pending_review' && (
                          <>
                            <LinkButton
                              onClick={() => review.mutate({ id: job.id, approve: true, comment: '' })}
                              disabled={review.isPending}
                            >
                              通过
                            </LinkButton>
                            <LinkButton
                              onClick={() => {
                                setReviewTarget(job)
                                setReviewComment('')
                              }}
                            >
                              驳回
                            </LinkButton>
                          </>
                        )}
                        {canControl && isActive && (
                          <LinkButton tone="danger" onClick={() => cancel.mutate(job.id)}>
                            终止
                          </LinkButton>
                        )}
                        {canControl && !isActive && (
                          <LinkButton
                            tone="danger"
                            onClick={() => {
                              if (confirm(`确认删除任务「${job.name}」的记录？训练产物会保留。`)) remove.mutate(job.id)
                            }}
                          >
                            删除
                          </LinkButton>
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
