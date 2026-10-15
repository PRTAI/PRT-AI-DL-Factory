/**
 * 样例审核：上传样例压缩包 + 需求文档（质检规范）+ 自定义提示词，平台按规范逐题静态验收，
 * 产出固定格式的返修报告（docx）。
 *
 * 和训练/评测不同，审核不占 GPU、不进调度队列、也不需要管理员放行——只花模型 API 的额度，
 * 所以这一页就是「提交 + 我的历史」，没有队列相关的东西。
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ClipboardCheck, FileDown, Package, Plus, Square, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useProject } from '@/lib/store'
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
import type { ReviewStage, ReviewStatus, ReviewTask } from '@/lib/types'

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

  const [archive, setArchive] = React.useState<File | null>(null)
  const [docs, setDocs] = React.useState<File[]>([])
  const [name, setName] = React.useState('')
  const [supplier, setSupplier] = React.useState('')
  const [specTitle, setSpecTitle] = React.useState('')
  const [prompt, setPrompt] = React.useState(DEFAULT_PROMPT)
  const [modelApiId, setModelApiId] = React.useState('')
  const [progress, setProgress] = React.useState(0)
  const [error, setError] = React.useState<string | null>(null)
  const [submitting, setSubmitting] = React.useState(false)

  const ready = (modelApis.data ?? []).filter((item) => item.ready)
  // 当前账号一个都没配密钥时不让提交——审核全程靠模型判定，没有 API 走不下去。
  const noModel = !modelApis.isLoading && ready.length === 0

  React.useEffect(() => {
    if (!modelApiId && ready.length > 0) setModelApiId(ready[0].id)
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
    if (!archive || docs.length === 0 || !modelApiId) return
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
    form.append('project_id', projectId)
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
      description="平台先解压采证，再按需求文档把质检内容拆成若干项逐题判定，最后出固定格式的返修报告。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!archive || docs.length === 0 || !modelApiId}
            loading={submitting}
            onClick={submit}
          >
            提交审核
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {noModel && (
          <InlineHint>
            还没有可用的模型 API（需要你已填密钥且配置已启用）。请先到「个人设置 → 我的 API Key」为平台填写自己的密钥。
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

        <Field label="模型 API" required hint="只列你已在个人设置中填写密钥、且已启用的配置">
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
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [mineOnly, setMineOnly] = React.useState(true)
  const [open, setOpen] = React.useState(false)

  const reviews = useQuery({
    queryKey: ['reviews', mineOnly],
    queryFn: () => api.reviews({ mine_only: mineOnly }),
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
        description="按需求文档对样例数据做逐题静态验收，输出固定格式的返修报告。不占 GPU，也不进调度队列。"
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

      <Card>
        {reviews.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
          </div>
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
          {item.project_id && <ProjectTag projectId={item.project_id} />}
        </div>
      </Td>
      <Td className="text-fg-muted">{item.supplier || '—'}</Td>
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
