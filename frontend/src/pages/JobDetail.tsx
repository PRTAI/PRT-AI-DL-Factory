import * as React from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  CheckCircle2,
  Download,
  FileCode2,
  FlaskConical,
  Package,
  RefreshCw,
  Repeat,
  Terminal,
  XCircle,
  XOctagon,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { useJobStream } from '@/lib/ws'
import type { Job } from '@/lib/types'
import {
  cn,
  downloadText,
  elapsedSeconds,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatMetric,
  formatNumber,
} from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Dialog,
  EmptyState,
  ErrorBox,
  KeyValue,
  Progress,
  SectionTitle,
  Skeleton,
  Stat,
  Table,
  Td,
  Textarea,
  Th,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Tr,
} from '@/components/ui'
import { CopyButton, FRAMEWORK_LABEL, JobTypeBadge, Mono, PageHeader, StatusBadge } from '@/components/common'
import { ProjectTag } from '@/components/ProjectTag'
import { TrialBoard } from '@/components/TrialBoard'
import { VerifyOverview } from '@/components/VerifyOverview'
import { LineSeriesChart } from '@/components/charts'

export default function JobDetail() {
  const { jobId } = useParams<{ jobId: string }>()
  const { user } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  // Tab lives in the URL so links like /jobs/<id>?tab=logs are shareable.
  const [searchParams, setSearchParams] = useSearchParams()
  // 评测任务的核心信息是「每个任务到哪一步了」，默认落在任务进展
  const tabParam = searchParams.get('tab')
  const [rejectOpen, setRejectOpen] = React.useState(false)
  const [rejectComment, setRejectComment] = React.useState('')

  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.job(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const data = query.state.data as Job | undefined
      return data && ['queued', 'preparing', 'running'].includes(data.status) ? 5000 : false
    },
  })

  const tab = tabParam ?? (job.data?.type === 'verify' ? 'trials' : 'overview')
  const setTab = (next: string) => {
    const params = new URLSearchParams(searchParams)
    // 评测任务的默认页是任务进展，所以「概览」也要显式写进 URL，否则删掉参数就又跳回去了
    if (next === 'overview' && job.data?.type !== 'verify') params.delete('tab')
    else params.set('tab', next)
    setSearchParams(params, { replace: true })
  }
  const isActive = job.data ? ['queued', 'preparing', 'running'].includes(job.data.status) : false
  const stream = useJobStream(jobId, isActive)

  const metrics = useQuery({
    queryKey: ['job-metrics', jobId],
    queryFn: () => api.jobMetrics(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: isActive ? 8000 : false,
  })

  const staticLog = useQuery({
    queryKey: ['job-log', jobId],
    queryFn: () => api.jobLog(jobId!),
    enabled: Boolean(jobId) && !isActive,
  })

  const command = useQuery({
    queryKey: ['job-command', jobId],
    queryFn: () => api.jobCommand(jobId!),
    enabled: Boolean(jobId) && tab === 'config',
    retry: false,
  })

  const artifacts = useQuery({
    queryKey: ['job-artifacts', jobId],
    queryFn: () => api.jobArtifacts(jobId!),
    enabled: Boolean(jobId) && tab === 'artifacts',
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['job', jobId] })
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
    queryClient.invalidateQueries({ queryKey: ['job-overview'] })
  }

  const review = useMutation({
    mutationFn: ({ approve, comment }: { approve: boolean; comment: string }) =>
      api.reviewJob(jobId!, approve, comment),
    onSuccess: (updated) => {
      toast.success(updated.status === 'queued' ? '已通过审核' : '已驳回')
      setRejectOpen(false)
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '操作失败'),
  })

  const cancel = useMutation({
    mutationFn: () => api.cancelJob(jobId!, '在任务详情页终止'),
    onSuccess: () => {
      toast.success('已发送终止信号')
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '终止失败'),
  })

  const resubmit = useMutation({
    mutationFn: () => api.resubmitJob(jobId!),
    onSuccess: (created) => {
      toast.success('已按相同配置重新提交，等待审核')
      navigate(`/jobs/${created.id}`)
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '重跑失败'),
  })

  const project = useQuery({
    queryKey: ['project', job.data?.project_id],
    queryFn: () => api.project(job.data!.project_id),
    enabled: Boolean(job.data?.project_id),
    retry: false,
  })

  // LoRA 训练产物要先合并回底座才能部署，所以这里问后端有没有可导出的 adapter
  const exportable = useQuery({
    queryKey: ['export-candidates', jobId],
    queryFn: () => api.exportCandidates(jobId!),
    enabled: Boolean(jobId) && job.data?.type === 'sft' && job.data?.status === 'succeeded',
    retry: false,
  })

  const startExport = useMutation({
    mutationFn: () => api.createExport({ job_id: jobId! }),
    onSuccess: (task) => {
      toast.success(`已开始合并导出「${task.name}」，完成后可在模型部署页直接部署`)
      queryClient.invalidateQueries({ queryKey: ['exports'] })
      navigate('/deployments')
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '导出失败'),
  })

  if (job.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (job.isError || !job.data) {
    return <ErrorBox message={job.error instanceof ApiError ? job.error.message : '任务不存在或无权访问'} />
  }

  const data = job.data
  const canControl = user?.role === 'admin' || data.owner_id === user?.id
  const liveProgress = stream.progress ?? data.progress
  const liveStatus = stream.status ?? data.status
  const logContent = isActive ? stream.log : (staticLog.data?.content ?? '')

  return (
    <div className="space-y-4">
      <PageHeader
        title={data.name}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <Mono>{data.id}</Mono>
            <span>·</span>
            <Link to={`/jobs?project=${data.project_id}`} className="link">
              {project.data?.project.name ?? (data.project_id || '未归属项目')}
            </Link>
            <span>·</span>
            <span>{data.owner_email}</span>
            <span>·</span>
            <span>创建于 {formatDateTime(data.created_at)}</span>
          </span>
        }
        actions={
          <>
            <Link to="/jobs">
              <Button variant="ghost" size="sm">
                <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
                返回列表
              </Button>
            </Link>
            <Button variant="outline" size="sm" onClick={() => { job.refetch(); metrics.refetch(); staticLog.refetch() }}>
              <RefreshCw className={cn('h-3.5 w-3.5', job.isFetching && 'animate-spin')} aria-hidden />
              刷新
            </Button>
            {user?.role === 'admin' && data.status === 'pending_review' && (
              <>
                <Button variant="success" size="sm" loading={review.isPending} onClick={() => review.mutate({ approve: true, comment: '' })}>
                  <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                  通过审核
                </Button>
                <Button variant="outline" size="sm" onClick={() => setRejectOpen(true)}>
                  <XCircle className="h-3.5 w-3.5" aria-hidden />
                  驳回
                </Button>
              </>
            )}
            {canControl && ['queued', 'preparing', 'running'].includes(data.status) && (
              <Button variant="danger" size="sm" loading={cancel.isPending} onClick={() => cancel.mutate()}>
                <XOctagon className="h-3.5 w-3.5" aria-hidden />
                终止任务
              </Button>
            )}
            {canControl && ['succeeded', 'failed', 'cancelled', 'rejected'].includes(data.status) && (
              <>
                <Button
                  variant="secondary"
                  size="sm"
                  title="载入这次的配置到提交页，修改后再提交"
                  onClick={() =>
                    navigate(`${data.type === 'verify' ? '/submit/verify' : '/submit/train'}?rerun=${data.id}`)
                  }
                >
                  <Repeat className="h-3.5 w-3.5" aria-hidden />
                  编辑后重跑
                </Button>
                <Button variant="ghost" size="sm" loading={resubmit.isPending} onClick={() => resubmit.mutate()}>
                  原样重跑
                </Button>
              </>
            )}
            {canControl && exportable.data?.exportable && (
              <Button
                variant="secondary"
                size="sm"
                loading={startExport.isPending}
                onClick={() => startExport.mutate()}
                title={`合并 ${exportable.data.checkpoints[0]?.label ?? 'adapter'} 到 ${exportable.data.base_model_path}`}
              >
                <Package className="h-3.5 w-3.5" aria-hidden />
                合并导出
              </Button>
            )}
            {data.type !== 'verify' && data.status === 'succeeded' && (
              <Link to={`/submit/verify?from=${data.id}`}>
                <Button variant="primary" size="sm">
                  <FlaskConical className="h-3.5 w-3.5" aria-hidden />
                  评测该产物
                </Button>
              </Link>
            )}
          </>
        }
      >
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <StatusBadge status={liveStatus} />
          <JobTypeBadge type={data.type} framework={data.framework} />
          {data.tags.map((tag) => (
            <Badge key={tag}>{tag}</Badge>
          ))}
          {data.parent_job_id && (
            <Link to={`/jobs/${data.parent_job_id}`}>
              <Badge tone="info">来源任务</Badge>
            </Link>
          )}
        </div>
      </PageHeader>

      {data.note && (
        <Card>
          <CardBody className="py-2.5 text-xs text-fg-muted">备注：{data.note}</CardBody>
        </Card>
      )}

      {data.runtime.error && (
        <ErrorBox message={data.runtime.error.slice(0, 2000)} className="whitespace-pre-wrap font-mono" />
      )}

      {data.review.reviewed_at && (
        <div
          className={cn(
            'rounded-lg border px-3 py-2 text-xs',
            data.status === 'rejected'
              ? 'border-danger/30 bg-danger-soft text-danger'
              : 'border-success/30 bg-success-soft text-success',
          )}
        >
          {data.status === 'rejected' ? '驳回' : '审核通过'} · {data.review.reviewer_email} ·{' '}
          {formatDateTime(data.review.reviewed_at)}
          {data.review.comment && <span> —— {data.review.comment}</span>}
        </div>
      )}

      {/* --------------------------------------------------------- stats -- */}
      <div className={cn('grid grid-cols-2 gap-3', data.type === 'verify' ? 'md:grid-cols-4' : 'md:grid-cols-4 xl:grid-cols-5')}>
        <Stat
          label="进度"
          value={(liveProgress.percent ?? 0).toFixed(0)}
          unit="%"
          tone={liveStatus === 'failed' ? 'danger' : 'primary'}
          hint={liveProgress.message}
        />
        {data.type === 'verify' ? (
          <>
            <Stat
              label="已完成 trial"
              value={`${formatNumber(liveProgress.completed_trials ?? 0)}/${formatNumber(liveProgress.total_trials ?? 0)}`}
            />
            <Stat
              label="通过率"
              value={data.metrics.resolved_rate !== null && data.metrics.resolved_rate !== undefined
                ? `${(data.metrics.resolved_rate * 100).toFixed(1)}%`
                : '—'}
              tone="success"
              hint={data.metrics.n_errors ? `${data.metrics.n_errors} 个异常计为失败` : undefined}
            />
          </>
        ) : (
          <>
            <Stat
              label="步数"
              value={`${formatNumber(liveProgress.current_step ?? 0)}${liveProgress.total_steps ? `/${formatNumber(liveProgress.total_steps)}` : ''}`}
              hint={liveProgress.current_epoch ? `epoch ${liveProgress.current_epoch.toFixed(2)}` : undefined}
            />
            <Stat label="最新 loss" value={formatMetric(data.metrics.last_loss)} tone="primary" hint={`最优 ${formatMetric(data.metrics.best_loss)}`} />
            <Stat label="最新 reward" value={formatMetric(data.metrics.last_reward, 3)} tone="success" hint={`最优 ${formatMetric(data.metrics.best_reward, 3)}`} />
            <Stat label="学习率" value={formatMetric(data.metrics.learning_rate)} />
          </>
        )}
        <Stat
          label="已运行"
          value={formatDuration(elapsedSeconds(data.runtime.started_at, data.runtime.finished_at))}
          hint={liveProgress.eta_seconds ? `预计剩余 ${formatDuration(liveProgress.eta_seconds)}` : undefined}
        />
      </div>

      {isActive && <Progress value={liveProgress.percent ?? 0} tone="success" showLabel />}

      {/* ---------------------------------------------------------- tabs -- */}
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">概览</TabsTrigger>
          <TabsTrigger value="logs">实时日志</TabsTrigger>
          <TabsTrigger value="metrics">指标曲线</TabsTrigger>
          {data.type === 'verify' && <TabsTrigger value="trials">任务进展</TabsTrigger>}
          <TabsTrigger value="config">配置与命令</TabsTrigger>
          <TabsTrigger value="artifacts">产物</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-3 space-y-3">
          {data.type === 'verify' && <VerifyOverview jobId={data.id} live={isActive} />}
          <div className="grid gap-3 lg:grid-cols-2">
            <Card>
              <CardHeader title="运行信息" />
              <CardBody className="divide-y divide-border/50">
                <KeyValue label="所属项目">
                  <ProjectTag projectId={data.project_id} className="text-xs" />
                </KeyValue>
                <KeyValue label="框架">{FRAMEWORK_LABEL[data.framework]}</KeyValue>
                <KeyValue label="申请 GPU">{data.resources.n_gpus} 张</KeyValue>
                <KeyValue label="分配显卡">{data.resources.gpu_ids.join(', ') || '未分配'}</KeyValue>
                <KeyValue label="进程 PID">{data.runtime.pid ?? '—'}</KeyValue>
                <KeyValue label="退出码">{data.runtime.exit_code ?? '—'}</KeyValue>
                <KeyValue label="开始时间">{formatDateTime(data.runtime.started_at)}</KeyValue>
                <KeyValue label="结束时间">{formatDateTime(data.runtime.finished_at)}</KeyValue>
                <KeyValue label="工作目录">
                  <span className="num">{data.runtime.work_dir || '—'}</span>
                </KeyValue>
                <KeyValue label="产物目录">
                  <span className="num">{data.runtime.output_dir || '—'}</span>
                </KeyValue>
              </CardBody>
            </Card>

            <Card>
              <CardHeader title="任务配置摘要" />
              <CardBody className="divide-y divide-border/50">
                <ConfigSummary job={data} />
              </CardBody>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="logs" className="mt-3">
          <LogPanel
            content={logContent}
            live={isActive}
            connected={stream.connected}
            jobName={data.name}
            onRefresh={() => staticLog.refetch()}
          />
        </TabsContent>

        <TabsContent value="metrics" className="mt-3">
          <MetricsPanel jobType={data.type} payload={metrics.data} loading={metrics.isLoading} />
        </TabsContent>

        <TabsContent value="trials" className="mt-3">
          <TrialBoard jobId={data.id} live={isActive} />
        </TabsContent>

        <TabsContent value="config" className="mt-3">
          <div className="space-y-3">
            <Card>
              <CardHeader
                title="将要执行的命令"
                description="平台根据表单生成，密钥已脱敏；run.sh 与配置文件同步写入任务工作目录。"
                icon={<Terminal className="h-4 w-4" aria-hidden />}
                actions={command.data && <CopyButton value={command.data.command} />}
              />
              <CardBody>
                {command.isLoading ? (
                  <Skeleton className="h-20 w-full" />
                ) : command.isError ? (
                  <ErrorBox message={command.error instanceof ApiError ? command.error.message : '无法生成命令'} />
                ) : command.data ? (
                  <>
                    <pre className="num max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-bg-elevated p-3 text-[11px] leading-relaxed text-fg">
                      {command.data.command}
                    </pre>
                    <div className="mt-2 text-[11px] text-fg-subtle">工作目录：{command.data.cwd}</div>
                    {command.data.notes.length > 0 && (
                      <ul className="mt-2 space-y-1">
                        {command.data.notes.map((note) => (
                          <li key={note} className="text-[11px] text-fg-muted">
                            · {note}
                          </li>
                        ))}
                      </ul>
                    )}
                    {Object.keys(command.data.env).length > 0 && (
                      <div className="mt-3">
                        <SectionTitle>环境变量</SectionTitle>
                        <div className="num space-y-0.5 rounded-lg bg-bg-elevated p-2 text-[11px]">
                          {Object.entries(command.data.env).map(([key, value]) => (
                            <div key={key} className="truncate">
                              <span className="text-fg-muted">{key}</span>=<span className="text-fg">{value}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                ) : null}
              </CardBody>
            </Card>

            {command.data && Object.keys(command.data.files).length > 0 && (
              <Card>
                <CardHeader title="生成的配置文件" icon={<FileCode2 className="h-4 w-4" aria-hidden />} />
                <CardBody className="space-y-3">
                  {Object.entries(command.data.files).map(([name, content]) => (
                    <div key={name}>
                      <div className="mb-1 flex items-center justify-between">
                        <span className="num text-xs text-fg-muted">{name}</span>
                        <CopyButton value={content} />
                      </div>
                      <pre className="num max-h-72 overflow-auto rounded-lg bg-bg-elevated p-3 text-[11px] leading-relaxed">
                        {content}
                      </pre>
                    </div>
                  ))}
                </CardBody>
              </Card>
            )}

            <Card>
              <CardHeader
                title="原始配置 (JSON)"
                actions={<CopyButton value={JSON.stringify(data.sft ?? data.rl ?? data.verify, null, 2)} />}
              />
              <CardBody>
                <pre className="num max-h-96 overflow-auto rounded-lg bg-bg-elevated p-3 text-[11px] leading-relaxed">
                  {JSON.stringify(data.sft ?? data.rl ?? data.verify, null, 2)}
                </pre>
              </CardBody>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="artifacts" className="mt-3">
          <Card>
            <CardHeader
              title="产物文件"
              description={data.runtime.output_dir || '任务尚未产生输出'}
              actions={<CopyButton value={data.runtime.output_dir} label="复制路径" />}
            />
            {artifacts.data?.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>名称</Th>
                    <Th className="w-24">类型</Th>
                    <Th className="w-28">大小</Th>
                    <Th className="w-44">修改时间</Th>
                    <Th className="w-28">操作</Th>
                  </tr>
                </thead>
                <tbody>
                  {artifacts.data.map((item) => (
                    <Tr key={item.path}>
                      <Td className="num text-xs">{item.name}</Td>
                      <Td>
                        <Badge tone={item.is_dir ? 'info' : 'neutral'}>{item.is_dir ? '目录' : '文件'}</Badge>
                      </Td>
                      <Td className="num text-xs">{item.is_dir ? '—' : formatBytes(item.size_bytes)}</Td>
                      <Td className="text-xs text-fg-muted">{formatDateTime(item.modified_at)}</Td>
                      <Td>
                        {item.is_dir ? (
                          <CopyButton value={item.path} label="复制路径" />
                        ) : (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={async () => {
                              try {
                                await api.downloadArtifact(data.id, item.name)
                              } catch (exception) {
                                toast.error(exception instanceof ApiError ? exception.message : '下载失败')
                              }
                            }}
                          >
                            <Download className="h-3.5 w-3.5" aria-hidden />
                            下载
                          </Button>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                title="暂无产物"
                description="训练 checkpoint 或 Harbor 评测结果会在这里列出（文件保存在服务器上，可用路径直接访问）。"
              />
            )}
          </Card>
        </TabsContent>
      </Tabs>

      <Dialog
        open={rejectOpen}
        onOpenChange={setRejectOpen}
        title="驳回任务"
        description="请写明驳回原因，提交人会收到通知。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRejectOpen(false)}>
              取消
            </Button>
            <Button
              variant="danger"
              loading={review.isPending}
              onClick={() => review.mutate({ approve: false, comment: rejectComment })}
            >
              确认驳回
            </Button>
          </>
        }
      >
        <Textarea
          value={rejectComment}
          onChange={(event) => setRejectComment(event.target.value)}
          rows={4}
          placeholder="例如：数据集格式不符合 Harbor 规范，请补齐 environment/Dockerfile。"
        />
      </Dialog>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
function ConfigSummary({ job }: { job: Job }) {
  if (job.sft) {
    const config = job.sft
    return (
      <>
        <KeyValue label="算法">{config.stage.toUpperCase()}</KeyValue>
        <KeyValue label="模型">{config.model_id || config.model_path}</KeyValue>
        <KeyValue label="对话模板">{config.template}</KeyValue>
        <KeyValue label="微调方式">
          {config.finetuning_type === 'lora'
            ? `LoRA (rank ${config.lora.rank} / alpha ${config.lora.alpha})`
            : config.finetuning_type}
        </KeyValue>
        <KeyValue label="数据集">{config.dataset_ids.length} 个</KeyValue>
        <KeyValue label="学习率">{config.learning_rate}</KeyValue>
        <KeyValue label="epoch / cutoff">{`${config.num_train_epochs} / ${config.cutoff_len}`}</KeyValue>
        <KeyValue label="batch × 累积">{`${config.per_device_train_batch_size} × ${config.gradient_accumulation_steps}`}</KeyValue>
        <KeyValue label="精度 / DeepSpeed">{`${config.compute_type} / ${config.deepspeed_stage}`}</KeyValue>
      </>
    )
  }
  if (job.rl) {
    const config = job.rl
    return (
      <>
        <KeyValue label="算法">{config.algorithm.toUpperCase()}</KeyValue>
        <KeyValue label="模型">{config.model_id || config.model_path}</KeyValue>
        <KeyValue label="rollout">{`${config.rollout.backend} · n=${config.rollout.n} · T=${config.rollout.temperature}`}</KeyValue>
        <KeyValue label="prompt / response">{`${config.max_prompt_length} / ${config.max_response_length}`}</KeyValue>
        <KeyValue label="batch / mini">{`${config.train_batch_size} / ${config.mini_batch_size}`}</KeyValue>
        <KeyValue label="学习率">{config.learning_rate}</KeyValue>
        <KeyValue label="clip low/high">{`${config.clip_ratio_low} / ${config.clip_ratio_high}`}</KeyValue>
        <KeyValue label="动态采样">{config.filter_groups_enable ? '开启' : '关闭'}</KeyValue>
        {config.harbor.enabled && (
          <KeyValue label="Harbor 环境">{`${config.harbor.harness} · ${config.harbor.environment_type} · ${config.harbor.num_tasks ?? '全部'} 任务`}</KeyValue>
        )}
      </>
    )
  }
  if (job.verify) {
    const config = job.verify
    return (
      <>
        <KeyValue label="Agent">{config.agent}</KeyValue>
        <KeyValue label="模型">{config.model || '（无需模型）'}</KeyValue>
        <KeyValue label="pass@k">{config.n_attempts}</KeyValue>
        <KeyValue label="并发">{config.n_concurrent}</KeyValue>
        <KeyValue label="沙箱">{config.environment_type}</KeyValue>
        <KeyValue label="任务过滤">
          {config.task_names.length ? config.task_names.join(', ') : '全部'}
          {config.n_tasks ? ` · 上限 ${config.n_tasks}` : ''}
        </KeyValue>
        <KeyValue label="超时倍数">{config.timeout_multiplier}</KeyValue>
        <KeyValue label="环境变量">
          {Object.keys(config.agent_env).length + Object.keys(config.verifier_env).length} 项（值已脱敏）
        </KeyValue>
        <KeyValue label="导出轨迹">{config.export_traces ? '是' : '否'}</KeyValue>
      </>
    )
  }
  return null
}

/* -------------------------------------------------------------------------- */
function LogPanel({
  content,
  live,
  connected,
  jobName,
  onRefresh,
}: {
  content: string
  live: boolean
  connected: boolean
  jobName: string
  onRefresh: () => void
}) {
  const [autoScroll, setAutoScroll] = React.useState(true)
  const [wrap, setWrap] = React.useState(true)
  const [filter, setFilter] = React.useState('')
  const boxRef = React.useRef<HTMLPreElement>(null)

  const visible = React.useMemo(() => {
    if (!filter) return content
    const needle = filter.toLowerCase()
    return content
      .split('\n')
      .filter((line) => line.toLowerCase().includes(needle))
      .join('\n')
  }, [content, filter])

  React.useEffect(() => {
    if (autoScroll && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight
    }
  }, [visible, autoScroll])

  return (
    <Card>
      <CardHeader
        title="运行日志"
        icon={<Terminal className="h-4 w-4" aria-hidden />}
        description={
          live ? (connected ? '实时推送中（WebSocket）' : '连接中断，正在重连…') : '任务已结束，显示日志尾部'
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="过滤关键字"
              aria-label="过滤日志"
              className="h-8 w-40 rounded-lg border border-border bg-bg-elevated px-2 text-xs text-fg placeholder:text-fg-subtle focus:border-primary focus:outline-none"
            />
            <label className="flex items-center gap-1.5 text-[11px] text-fg-muted">
              <input
                type="checkbox"
                checked={autoScroll}
                onChange={(event) => setAutoScroll(event.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--primary)]"
              />
              自动滚动
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-fg-muted">
              <input
                type="checkbox"
                checked={wrap}
                onChange={(event) => setWrap(event.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--primary)]"
              />
              自动换行
            </label>
            {!live && (
              <Button variant="ghost" size="sm" onClick={onRefresh}>
                <RefreshCw className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={() => downloadText(`${jobName}.log`, content)}>
              <Download className="h-3.5 w-3.5" aria-hidden />
              下载
            </Button>
          </div>
        }
      />
      <pre
        ref={boxRef}
        className={cn(
          'num max-h-[560px] min-h-[320px] overflow-auto bg-bg-elevated p-3 text-[11.5px] leading-[1.65] text-fg',
          wrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre',
        )}
        aria-label="任务日志"
        tabIndex={0}
      >
        {visible || '（暂无日志输出）'}
      </pre>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
const SERIES_LABEL: Record<string, string> = {
  loss: 'loss',
  eval_loss: 'eval loss',
  lr: '学习率',
  reward: 'reward',
  accuracy: '准确率',
  throughput: '吞吐 (tok/s)',
  pg_loss: 'policy loss',
  entropy: '熵',
  kl_loss: 'KL',
  kl: 'KL',
  grad_norm: '梯度范数',
  response_length: '回复长度',
  score: 'score',
  val_acc: '验证准确率',
}

function MetricsPanel({
  jobType,
  payload,
  loading,
}: {
  jobType: Job['type']
  payload?: { series: Record<string, [number, number][]>; summary: Record<string, unknown> }
  loading: boolean
}) {
  if (loading) return <Skeleton className="h-64 w-full" />
  const series = payload?.series ?? {}
  const keys = Object.keys(series)

  if (!keys.length) {
    return (
      <Card>
        <EmptyState
          title="暂无指标数据"
          description={
            jobType === 'verify'
              ? '评测开始产生 trial 结果后，这里会显示 reward 分布与 pass@k。'
              : '训练开始输出日志后，loss / reward 曲线会自动出现。'
          }
        />
      </Card>
    )
  }

  const toChartData = (names: string[]) => {
    const buckets = new Map<number, Record<string, number>>()
    for (const name of names) {
      for (const [x, y] of series[name] ?? []) {
        const row = buckets.get(x) ?? {}
        row[name] = y
        buckets.set(x, row)
      }
    }
    return [...buckets.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([x, values]) => ({ x, ...values }))
  }

  const groups: { title: string; keys: string[]; rightAxis?: string[] }[] = []
  const lossKeys = keys.filter((key) => ['loss', 'eval_loss', 'pg_loss'].includes(key))
  if (lossKeys.length) groups.push({ title: '损失', keys: [...lossKeys, ...keys.filter((k) => k === 'lr')], rightAxis: ['lr'] })
  const rewardKeys = keys.filter((key) => ['reward', 'score', 'accuracy', 'val_acc'].includes(key))
  if (rewardKeys.length) groups.push({ title: '奖励 / 准确率', keys: rewardKeys })
  const otherKeys = keys.filter(
    (key) => !['loss', 'eval_loss', 'pg_loss', 'lr', 'reward', 'score', 'accuracy', 'val_acc', 'reward_distribution'].includes(key),
  )
  if (otherKeys.length) groups.push({ title: '其他指标', keys: otherKeys })

  return (
    <div className="space-y-3">
      {series.reward_distribution && (
        <Card>
          <CardHeader title="Reward 分布" description="横轴为 reward 取值，纵轴为 trial 数量" />
          <CardBody>
            <LineSeriesChart
              data={(series.reward_distribution ?? []).map(([x, y]) => ({ x, count: y }))}
              series={[{ key: 'count', label: 'trial 数', color: 'var(--chart-3)' }]}
              height={180}
              xFormatter={(value) => String(value)}
            />
          </CardBody>
        </Card>
      )}

      {groups.map((group) => (
        <Card key={group.title}>
          <CardHeader title={group.title} />
          <CardBody>
            <LineSeriesChart
              data={toChartData(group.keys)}
              height={260}
              series={group.keys.map((key) => ({
                key,
                label: SERIES_LABEL[key] ?? key,
                axis: group.rightAxis?.includes(key) ? 'right' : 'left',
              }))}
              xFormatter={(value) => `step ${value}`}
              yFormatter={(value) => formatMetric(value)}
            />
          </CardBody>
        </Card>
      ))}
    </div>
  )
}
