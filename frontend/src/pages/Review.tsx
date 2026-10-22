/**
 * 样例审核：上传样例压缩包 + 需求文档（质检规范）+ 自定义提示词，平台按规范逐题静态验收，
 * 产出固定格式的返修报告（docx）。
 *
 * 和训练/评测不同，审核不占 GPU、不进调度队列、也不需要管理员放行——只花模型 API 的额度，
 * 所以这一页就是「提交 + 我的历史」，没有队列相关的东西。
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ClipboardCheck, FileDown, Package, Plus, Square, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useProject } from '@/lib/store'
import { supportsProtocol } from '@/lib/model-api'
import { formatBytes, formatPercent, formatRelative } from '@/lib/utils'
import { Mono, PageHeader, InlineHint } from '@/components/common'
import { ProjectTag } from '@/components/ProjectTag'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorBox,
  Field,
  Input,
  LinkButton,
  Progress,
  Select,
  Skeleton,
  StatusDot,
  Table,
  Td,
  Textarea,
  Th,
  Tr,
} from '@/components/ui'
import type { ReasoningEffort, ReviewAgent, ReviewStage, ReviewStatus, ReviewTask } from '@/lib/types'

const FILE_INPUT =
  'w-full rounded-lg border border-dashed border-border bg-bg-elevated px-3 py-5 text-xs text-fg-muted ' +
  'file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1.5 file:text-xs file:text-primary-fg'

export const REVIEW_STATUS: Record<ReviewStatus, { label: string; tone: 'success' | 'info' | 'danger' | 'neutral' }> = {
  queued: { label: '排队中', tone: 'neutral' },
  running: { label: '审核中', tone: 'info' },
  succeeded: { label: '已完成', tone: 'success' },
  failed: { label: '已失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'neutral' },
}

export const REVIEW_STAGE: Record<ReviewStage, string> = {
  pending: '待开始',
  collecting: '解压采证',
  planning: '拆解质检项',
  checking: '逐项判定',
  reporting: '汇总出报告',
  done: '已出报告',
}

const ARCHIVE_ACCEPT = '.zip,.tar,.tar.gz,.tgz'
const DOC_ACCEPT = '.docx,.pdf,.md,.txt'

const DEFAULT_PROMPT = `请按规范逐题静态验收，重点关注目录结构、必需文件、metadata 字段与脚本权限位。`
export const REVIEW_AGENTS: Record<ReviewAgent, string> = { 'claude-code': 'Claude Code', codex: 'Codex' }

/* -------------------------------------------------------------------------- */
/* 提交对话框                                                                  */
/* -------------------------------------------------------------------------- */
function SubmitDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (item: ReviewTask) => void
}) {
  const { projectId } = useProject()
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: api.modelApis, enabled: open })
  const projects = useQuery({ queryKey: ['projects', 'review-submit'], queryFn: () => api.projects(), enabled: open })
  const [reviewProjectId, setReviewProjectId] = React.useState('')

  const [archive, setArchive] = React.useState<File | null>(null)
  const [docs, setDocs] = React.useState<File[]>([])
  const [name, setName] = React.useState('')
  const [supplier, setSupplier] = React.useState('')
  const [specTitle, setSpecTitle] = React.useState('')
  const [prompt, setPrompt] = React.useState(DEFAULT_PROMPT)
  const [modelApiId, setModelApiId] = React.useState('')
  const [agent, setAgent] = React.useState<ReviewAgent>('claude-code')
  const [effort, setEffort] = React.useState<ReasoningEffort>('')
  const [progress, setProgress] = React.useState(0)
  const [error, setError] = React.useState<string | null>(null)
  const [submitting, setSubmitting] = React.useState(false)

  const protocol = agent === 'claude-code' ? 'anthropic' : 'responses'
  const ready = (modelApis.data ?? []).filter((item) => item.ready && supportsProtocol(item, protocol))
  const selectedModel = ready.find((item) => item.id === modelApiId)
  // 当前账号一个都没配密钥时不让提交——审核全程靠模型判定，没有 API 走不下去。
  const noModel = !modelApis.isLoading && ready.length === 0
  const selectedProject = projects.data?.find((row) => row.project.id === reviewProjectId)?.project

  React.useEffect(() => {
    if (open) setReviewProjectId(projectId)
  }, [open, projectId])

  React.useEffect(() => {
    if (!ready.some((item) => item.id === modelApiId)) setModelApiId(ready[0]?.id ?? '')
  }, [modelApiId, ready])

  const reset = () => {
    setArchive(null)
    setDocs([])
    setName('')
    setSupplier('')
    setSpecTitle('')
    setPrompt(DEFAULT_PROMPT)
    setProgress(0)
    setError(null)
  }

  const submit = async () => {
    if (!archive || docs.length === 0 || !selectedModel || !selectedProject || selectedProject.archived) return
    setError(null)
    setSubmitting(true)
    const form = new FormData()
    form.append('archive', archive)
    docs.forEach((doc) => form.append('docs', doc))
    form.append('name', name || archive.name.replace(/\.(zip|tar\.gz|tgz|tar)$/i, ''))
    form.append('supplier', supplier)
    form.append('spec_title', specTitle)
    form.append('prompt', prompt)
    form.append('model_api_id', modelApiId)
    form.append('agent', agent)
    form.append('reasoning_effort', effort)
    form.append('project_id', selectedProject.id)
    try {
      const item = await api.createReview(form, setProgress)
      toast.success('审核已提交', { description: `工作目录 ${item.workspace}` })
      onCreated(item)
      onOpenChange(false)
      reset()
    } catch (exception) {
      setError(exception instanceof ApiError ? exception.message : '提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) reset()
      }}
      width="max-w-2xl"
      title="提交样例审核"
      description="Agent 在独立容器中读取规范、检查原始文件；平台校验结论并生成返修报告。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!archive || docs.length === 0 || !selectedModel || !selectedProject || selectedProject.archived}
            loading={submitting}
            onClick={submit}
          >
            提交审核
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="所属项目" required hint="质检数据、结论和报告统一归属该项目。">
          <Select
            value={reviewProjectId}
            onValueChange={setReviewProjectId}
            placeholder={projects.isLoading ? '加载中…' : '请选择质检所属项目'}
            options={(projects.data ?? []).map(({ project }) => ({ value: project.id, label: project.name }))}
          />
          {!projects.isLoading && !projects.data?.length && (
            <p className="mt-1 text-xs text-fg-muted">请先到「项目」页创建项目。</p>
          )}
          {projects.isError && <ErrorBox message="项目加载失败，请重新打开提交窗口重试。" />}
        </Field>
        {noModel && (
          <InlineHint>
            没有支持当前 Agent 的可用模型。{agent === 'claude-code' ? 'Claude Code 需要 Anthropic 接口。' : 'Codex 需要 OpenAI Responses 接口。'}请检查模型接口和个人 API Key。
          </InlineHint>
        )}

        <Field label="样例压缩包" required hint="支持 .zip / .tar / .tar.gz；包内的内层压缩包会一并展开">
          <input
            type="file"
            accept={ARCHIVE_ACCEPT}
            className={FILE_INPUT}
            onChange={(event) => {
              const selected = event.target.files?.[0] ?? null
              setArchive(selected)
              if (selected && !name) setName(selected.name.replace(/\.(zip|tar\.gz|tgz|tar)$/i, ''))
            }}
          />
        </Field>
        {archive && (
          <p className="text-[11px] text-fg-subtle">
            {archive.name} · {formatBytes(archive.size)}
          </p>
        )}

        <Field
          label="需求文档"
          required
          hint="质检规范，docx / pdf / md / txt，可多选。质检项只能来自这些文档的原文"
        >
          <input
            type="file"
            multiple
            accept={DOC_ACCEPT}
            className={FILE_INPUT}
            onChange={(event) => setDocs(Array.from(event.target.files ?? []))}
          />
        </Field>
        {docs.length > 0 && (
          <ul className="space-y-0.5 text-[11px] text-fg-subtle">
            {docs.map((doc) => (
              <li key={doc.name}>
                {doc.name} · {formatBytes(doc.size)}
              </li>
            ))}
          </ul>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="批次名" required hint="进报告标题">
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="金融-0923" />
          </Field>
          <Field label="供应商" hint="进报告副标题">
            <Input value={supplier} onChange={(event) => setSupplier(event.target.value)} placeholder="铂瑞缇" />
          </Field>
        </div>

        <Field label="依据规范名" hint="写进报告「一、说明」的依据一行">
          <Input
            value={specTitle}
            onChange={(event) => setSpecTitle(event.target.value)}
            placeholder="外发版-评测题包交付规范"
          />
        </Field>

        <Field label="Agent harness" required hint="使用真正的工具调用循环，原始数据只读挂载，工具轨迹随任务留存。">
          <Select value={agent} onValueChange={(value) => setAgent(value as ReviewAgent)} options={[
            { value: 'claude-code', label: 'Claude Code', description: 'Anthropic Messages 接口，例如 DeepSeek Flash' },
            { value: 'codex', label: 'Codex', description: 'OpenAI Responses 接口' },
          ]} />
        </Field>
        <Field label="模型 API" required hint={`仅列支持 ${agent === 'claude-code' ? 'Anthropic Messages' : 'OpenAI Responses'} 且已配置密钥的模型`}>
          <Select
            value={modelApiId}
            onValueChange={setModelApiId}
            disabled={ready.length === 0}
            placeholder={modelApis.isLoading ? '加载中…' : '没有可用配置'}
            options={ready.map((item) => ({
              value: item.id,
              label: item.name,
              description: `${item.provider} · ${item.model_id}`,
            }))}
          />
        </Field>
        {selectedModel && <InlineHint>本次组合：{REVIEW_AGENTS[agent]} + {selectedModel.model_id}</InlineHint>}
        <Field label="思考强度" hint="默认交给 Agent；平台会按 harness 支持的档位设置。">
          <Select value={effort || 'default'} onValueChange={(value) => setEffort(value === 'default' ? '' : value as ReasoningEffort)} options={[
            { value: 'default', label: '默认' },
            ...['low', 'medium', 'high', 'xhigh', 'max'].map((value) => ({ value, label: value })),
          ]} />
        </Field>

        <Field label="自定义提示词" hint="补充说明这批数据的关注点；不会替代需求文档，只是叠加在上面">
          <Textarea rows={3} value={prompt} onChange={(event) => setPrompt(event.target.value)} />
        </Field>

        {submitting && <Progress value={progress} showLabel tone="primary" />}
        {error && <ErrorBox message={error} />}
      </div>
    </Dialog>
  )
}

/* -------------------------------------------------------------------------- */
/* 历史列表                                                                    */
/* -------------------------------------------------------------------------- */
export default function Review() {
  const { user } = useAuth()
  const { projectId, setProject } = useProject()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [mineOnly, setMineOnly] = React.useState(true)
  const [open, setOpen] = React.useState(false)
  const urlProject = searchParams.get('project') ?? ''
  const effectiveProject = urlProject || projectId
  const projects = useQuery({
    queryKey: ['projects', 'names'],
    queryFn: () => api.projects({ include_archived: true }),
    staleTime: 30_000,
  })

  React.useEffect(() => {
    if (!urlProject) return
    setProject(urlProject)
    setSearchParams({}, { replace: true })
  }, [urlProject, setProject, setSearchParams])

  const reviews = useQuery({
    queryKey: ['reviews', mineOnly, effectiveProject],
    queryFn: () => api.reviews({ mine_only: mineOnly, project_id: effectiveProject || undefined }),
    // 审核在进程内跑，没有 WebSocket 推送，靠轮询看进度
    refetchInterval: 4000,
  })

  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelReview(id),
    onSuccess: () => {
      toast.success('已取消')
      void queryClient.invalidateQueries({ queryKey: ['reviews'] })
    },
    onError: (exception) => toast.error(exception instanceof ApiError ? exception.message : '取消失败'),
  })

  const remove = useMutation({
    mutationFn: ({ id, purge }: { id: string; purge: boolean }) => api.deleteReview(id, purge),
    onSuccess: () => {
      toast.success('已删除')
      void queryClient.invalidateQueries({ queryKey: ['reviews'] })
    },
    onError: (exception) => toast.error(exception instanceof ApiError ? exception.message : '删除失败'),
  })

  const items = reviews.data ?? []

  return (
    <div>
      <PageHeader
        title="样例审核"
        description="通过 Agent harness + 模型在容器内审核原始数据，质检和报告归属于项目。不占 GPU。"
        actions={
          <>
            {user?.role === 'admin' && (
              <Button variant="ghost" onClick={() => setMineOnly((value) => !value)}>
                {mineOnly ? '看全部' : '只看我的'}
              </Button>
            )}
            <Button variant="primary" onClick={() => setOpen(true)}>
              <Plus className="h-3.5 w-3.5" />
              提交审核
            </Button>
          </>
        }
      />

      <div className="mb-4 max-w-sm">
        <Select
          value={effectiveProject || 'all'}
          onValueChange={(value) => setProject(value === 'all' ? '' : value)}
          options={[
            { value: 'all', label: '全部项目' },
            ...(projects.data ?? []).map(({ project }) => ({
              value: project.id,
              label: `${project.name}${project.archived ? '（已归档）' : ''}`,
            })),
          ]}
        />
      </div>
      <Card>
        {reviews.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
          </div>
        ) : reviews.isError ? (
          <ErrorBox message={reviews.error instanceof ApiError ? reviews.error.message : '审核记录加载失败'} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<ClipboardCheck className="h-6 w-6" />}
            title="还没有审核记录"
            description="上传一批样例和对应的质检规范，平台会逐题验收并出返修报告。"
            action={
              <Button variant="primary" onClick={() => setOpen(true)}>
                提交审核
              </Button>
            }
          />
        ) : (
          <Table>
            <thead>
              <Tr>
                <Th>批次</Th>
                <Th>供应商</Th>
                <Th>Agent / 模型</Th>
                <Th>状态</Th>
                <Th className="text-right">受检</Th>
                <Th className="text-right">不合格</Th>
                <Th className="text-right">待复核</Th>
                <Th className="text-right">通过率</Th>
                <Th>提交</Th>
                <Th className="text-right">操作</Th>
              </Tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <ReviewRow
                  key={item.id}
                  item={item}
                  canControl={user?.role === 'admin' || user?.id === item.owner_id}
                  onOpen={() => navigate(`/review/${item.id}`)}
                  onCancel={() => cancel.mutate(item.id)}
                  onDelete={(purge) => remove.mutate({ id: item.id, purge })}
                />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <SubmitDialog
        open={open}
        onOpenChange={setOpen}
        onCreated={(item) => {
          void queryClient.invalidateQueries({ queryKey: ['reviews'] })
          void queryClient.invalidateQueries({ queryKey: ['projects'] })
          setProject(item.project_id)
          navigate(`/review/${item.id}`)
        }}
      />
    </div>
  )
}

function ReviewRow({
  item,
  canControl,
  onOpen,
  onCancel,
  onDelete,
}: {
  item: ReviewTask
  canControl: boolean
  onOpen: () => void
  onCancel: () => void
  onDelete: (purge: boolean) => void
}) {
  const status = REVIEW_STATUS[item.status]
  const running = item.status === 'queued' || item.status === 'running'
  const { stats } = item
  return (
    <Tr>
      <Td>
        <button type="button" onClick={onOpen} className="text-left text-primary hover:underline">
          {item.name}
        </button>
        <div className="mt-0.5 flex items-center gap-1.5">
          <Mono className="text-[10px] text-fg-subtle">{item.workspace}</Mono>
          <Link to={`/review?project=${item.project_id}`} title="查看该项目的质检记录">
            <ProjectTag projectId={item.project_id} linked={false} />
          </Link>
        </div>
      </Td>
      <Td className="text-fg-muted">{item.supplier || '—'}</Td>
      <Td>
        <div className="text-xs">{item.agent ? REVIEW_AGENTS[item.agent] : '直接 LLM（历史）'}</div>
        <div className="mt-0.5 max-w-56 truncate text-[10px] text-fg-subtle" title={item.model_label}>{item.model_label || '—'}</div>
      </Td>
      <Td>
        <StatusDot tone={status.tone} pulse={running} label={running ? REVIEW_STAGE[item.stage] : status.label} />
        {item.error && <div className="mt-0.5 max-w-60 truncate text-[10px] text-danger">{item.error}</div>}
      </Td>
      <Td className="num text-right">{stats.n_tasks || '—'}</Td>
      <Td className="num text-right">
        {stats.n_fail > 0 ? <Badge tone="danger">{stats.n_fail}</Badge> : '0'}
      </Td>
      <Td className="num text-right">
        {stats.n_needs_review > 0 ? <Badge tone="warning">{stats.n_needs_review}</Badge> : '0'}
      </Td>
      <Td className="num text-right">
        {stats.n_tasks ? formatPercent((stats.n_pass / stats.n_tasks) * 100) : '—'}
      </Td>
      <Td className="whitespace-nowrap text-fg-muted">{formatRelative(item.created_at)}</Td>
      <Td>
        <div className="flex items-center justify-end gap-2.5">
          {item.report_path && (
            <LinkButton onClick={() => void api.downloadReviewReport(item.id).catch(() => toast.error('下载失败'))}>
              <FileDown className="mr-0.5 inline h-3 w-3" />
              报告
            </LinkButton>
          )}
          <LinkButton onClick={() => void api.downloadReviewArchive(item.id).catch(() => toast.error('下载失败'))}>
            <Package className="mr-0.5 inline h-3 w-3" />
            原始包
          </LinkButton>
          {canControl && running && (
            <LinkButton tone="danger" onClick={onCancel}>
              <Square className="mr-0.5 inline h-3 w-3" />
              取消
            </LinkButton>
          )}
          {canControl && !running && (
            <LinkButton
              tone="danger"
              onClick={() => {
                // 原始数据要留存，所以默认只删记录；要一起清文件得明确确认。
                const purge = window.confirm('同时删除工作目录里的原始数据？\n确定 = 一起删除，取消 = 只删记录。')
                onDelete(purge)
              }}
            >
              <Trash2 className="mr-0.5 inline h-3 w-3" />
              删除
            </LinkButton>
          )}
        </div>
      </Td>
    </Tr>
  )
}
