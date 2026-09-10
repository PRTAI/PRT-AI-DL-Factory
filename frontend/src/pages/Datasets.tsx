import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Database, FlaskConical, GraduationCap, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import type { DatasetKind, DatasetSpec } from '@/lib/types'
import { cn, formatBytes, formatNumber, formatRelative } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Dialog,
  EmptyState,
  ErrorBox,
  Field,
  Input,
  LinkButton,
  Progress,
  Select,
  Skeleton,
  Table,
  Td,
  Textarea,
  Th,
  Tr,
} from '@/components/ui'
import { PageHeader } from '@/components/common'

export default function Datasets() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [uploadOpen, setUploadOpen] = React.useState(false)
  const [previewId, setPreviewId] = React.useState<string | null>(null)
  // 训练数据 / 评测数据：harbor_tasks 是评测用的任务树（做 agentic RL 时也能训），其余都是训练格式
  const [category, setCategory] = React.useState<'all' | 'train' | 'verify'>('all')

  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api.datasets() })
  const isVerifyKind = (kind: DatasetKind) => kind === 'harbor_tasks'
  const visible = (datasets.data ?? []).filter((d) =>
    category === 'all' ? true : category === 'verify' ? isVerifyKind(d.kind) : !isVerifyKind(d.kind),
  )
  const countTrain = (datasets.data ?? []).filter((d) => !isVerifyKind(d.kind)).length
  const countVerify = (datasets.data ?? []).filter((d) => isVerifyKind(d.kind)).length
  const specs = useQuery({ queryKey: ['dataset-specs'], queryFn: () => api.datasetSpecs() })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteDataset(id),
    onSuccess: () => {
      toast.success('数据集已删除')
      queryClient.invalidateQueries({ queryKey: ['datasets'] })
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '删除失败'),
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title="数据集"
        description="上传标准格式压缩包，平台自动解压、校验并登记到规范目录"
        actions={
          <>
            <Button variant="primary" size="sm" onClick={() => setUploadOpen(true)}>
              <Upload className="h-3.5 w-3.5" aria-hidden />
              上传数据集
            </Button>
          </>
        }
      />

      <div className="flex h-10 w-fit items-center gap-0.5 rounded-lg border border-border bg-bg-elevated p-0.5" role="group" aria-label="按用途筛选">
        {(
          [
            ['all', '全部', null, (datasets.data ?? []).length],
            ['train', '训练数据', GraduationCap, countTrain],
            ['verify', '评测数据', FlaskConical, countVerify],
          ] as const
        ).map(([key, label, Icon, n]) => (
          <button
            key={key}
            type="button"
            aria-pressed={category === key}
            onClick={() => setCategory(key)}
            className={cn(
              'flex h-full items-center gap-1.5 rounded-md px-4 text-[13px] transition-colors',
              category === key ? 'bg-card font-medium text-primary shadow-sm' : 'text-fg-muted hover:text-fg',
            )}
          >
            {Icon && <Icon className="h-3.5 w-3.5" aria-hidden />}
            {label}
            <span className="num text-[11px] text-fg-subtle">{n}</span>
          </button>
        ))}
      </div>

      <Card>
        <CardHeader
          title={category === 'train' ? '训练数据集' : category === 'verify' ? '评测数据集' : '已登记的数据集'}
          description={
            datasets.data
              ? `${category === 'all' ? `共 ${datasets.data.length} 个` : `${visible.length} 个`} · 解压后统一存放在 data/datasets/<dataset_id>/${
                  category === 'verify' ? ' · Harbor 任务树（task.toml / instruction.md / environment / tests）' : ''
                }`
              : undefined
          }
        />
        {datasets.isLoading ? (
          <CardBody>
            <Skeleton className="h-32 w-full" />
          </CardBody>
        ) : visible.length ? (
          <Table>
            <thead>
              <tr>
                <Th>名称</Th>
                <Th className="w-32">类型</Th>
                <Th className="w-24">状态</Th>
                <Th className="w-40">规模</Th>
                <Th className="w-40">上传者</Th>
                <Th className="w-32">操作</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((dataset) => (
                <Tr key={dataset.id}>
                  <Td>
                    <div className="font-medium text-fg">{dataset.name}</div>
                    {dataset.description && (
                      <div className="mt-0.5 line-clamp-1 text-[11px] text-fg-muted">{dataset.description}</div>
                    )}
                    <div className="num mt-0.5 truncate text-[11px] text-fg-subtle">{dataset.root_path}</div>
                    {dataset.stats.warnings.length > 0 && (
                      <div className="mt-1 text-[11px] text-warning">⚠ {dataset.stats.warnings[0]}</div>
                    )}
                  </Td>
                  <Td>
                    <Badge tone="primary">{dataset.kind}</Badge>
                    {dataset.tags.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {dataset.tags.map((tag) => (
                          <Badge key={tag}>{tag}</Badge>
                        ))}
                      </div>
                    )}
                  </Td>
                  <Td>
                    <Badge tone={dataset.status === 'ready' ? 'success' : dataset.status === 'invalid' ? 'danger' : 'info'}>
                      {dataset.status === 'ready' ? '可用' : dataset.status === 'invalid' ? '校验失败' : dataset.status}
                    </Badge>
                  </Td>
                  <Td className="num text-xs text-fg-muted">
                    {dataset.stats.n_tasks > 0 && <div>{formatNumber(dataset.stats.n_tasks)} 个任务</div>}
                    {dataset.stats.n_records > 0 && <div>{formatNumber(dataset.stats.n_records)} 条样本</div>}
                    <div>{formatBytes(dataset.stats.total_bytes)}</div>
                  </Td>
                  <Td>
                    <div className="text-xs text-fg-muted">{dataset.owner_email}</div>
                    <div className="text-[11px] text-fg-subtle">{formatRelative(dataset.created_at)}</div>
                  </Td>
                  <Td>
                    <div className="flex items-center gap-x-3">
                      <LinkButton onClick={() => setPreviewId(dataset.id)}>预览</LinkButton>
                      {(user?.role === 'admin' || dataset.owner_id === user?.id) && (
                        <LinkButton
                          tone="danger"
                          onClick={() => {
                            if (confirm(`确认删除数据集「${dataset.name}」？磁盘上的文件也会被移除。`)) {
                              remove.mutate(dataset.id)
                            }
                          }}
                        >
                          删除
                        </LinkButton>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            icon={<Database className="h-6 w-6" aria-hidden />}
            title={category === 'train' ? '还没有训练数据集' : category === 'verify' ? '还没有评测数据集' : '还没有数据集'}
            description="上传 zip / tar.gz，平台会按所选格式做结构校验，并生成框架需要的索引文件。"
            action={
              <Button variant="primary" size="sm" onClick={() => setUploadOpen(true)}>
                上传第一个数据集
              </Button>
            }
          />
        )}
      </Card>

      <UploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        specs={specs.data?.specs ?? []}
        onUploaded={() => queryClient.invalidateQueries({ queryKey: ['datasets'] })}
      />
      <PreviewDialog datasetId={previewId} onClose={() => setPreviewId(null)} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
function UploadDialog({
  open,
  onOpenChange,
  specs,
  onUploaded,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  specs: DatasetSpec[]
  onUploaded: () => void
}) {
  const [name, setName] = React.useState('')
  const [kind, setKind] = React.useState<DatasetKind>('sft_alpaca')
  const [description, setDescription] = React.useState('')
  const [tags, setTags] = React.useState('')
  const [file, setFile] = React.useState<File | null>(null)
  const [progress, setProgress] = React.useState(0)
  const [error, setError] = React.useState<string | null>(null)
  const [uploading, setUploading] = React.useState(false)

  const spec = specs.find((item) => item.kind === kind)

  const reset = () => {
    setName('')
    setDescription('')
    setTags('')
    setFile(null)
    setProgress(0)
    setError(null)
  }

  const submit = async () => {
    if (!file) return
    setError(null)
    setUploading(true)
    const form = new FormData()
    form.append('file', file)
    form.append('name', name || file.name.replace(/\.(zip|tar\.gz|tgz|tar)$/i, ''))
    form.append('kind', kind)
    form.append('description', description)
    form.append('tags', tags)
    try {
      const dataset = await api.uploadDataset(form, setProgress)
      toast.success(
        `上传成功：${dataset.name}（${dataset.stats.n_tasks || dataset.stats.n_records} ${
          dataset.stats.n_tasks ? '个任务' : '条样本'
        }）`,
      )
      onUploaded()
      onOpenChange(false)
      reset()
    } catch (exception) {
      setError(exception instanceof ApiError ? exception.message : '上传失败')
    } finally {
      setUploading(false)
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
      title="上传数据集"
      description="选择格式后，平台会在解压时按该格式的必需字段做校验；不通过会直接告诉你缺什么。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="primary" disabled={!file} loading={uploading} onClick={submit}>
            上传并校验
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="数据格式" required hint={spec?.summary}>
          <Select
            value={kind}
            onValueChange={(value) => setKind(value as DatasetKind)}
            options={specs.map((item) => ({ value: item.kind, label: item.title, description: item.summary }))}
          />
        </Field>

        {spec && (
          <div className="rounded-lg border border-border bg-bg-elevated p-3">
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">目录结构</div>
            <pre className="num overflow-x-auto text-[11px] leading-relaxed text-fg-muted">
              {spec.layout.join('\n')}
            </pre>
            {spec.required_fields.length > 0 && (
              <p className="mt-2 text-[11px] text-fg-muted">
                必需字段：
                {spec.required_fields.map((field) => (
                  <code key={field} className="num mx-1 rounded bg-muted px-1 py-0.5 text-[10px]">
                    {field}
                  </code>
                ))}
              </p>
            )}
          </div>
        )}

        <Field label="压缩包" required hint="支持 .zip / .tar / .tar.gz">
          <input
            type="file"
            accept=".zip,.tar,.tar.gz,.tgz"
            onChange={(event) => {
              const selected = event.target.files?.[0] ?? null
              setFile(selected)
              if (selected && !name) setName(selected.name.replace(/\.(zip|tar\.gz|tgz|tar)$/i, ''))
            }}
            className="w-full rounded-lg border border-dashed border-border bg-bg-elevated px-3 py-6 text-xs text-fg-muted file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1.5 file:text-xs file:text-primary-fg"
          />
        </Field>

        {file && (
          <p className="text-[11px] text-fg-subtle">
            {file.name} · {formatBytes(file.size)}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="数据集名称" required>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="terminal-bench-4k" />
          </Field>
          <Field label="标签" hint="逗号分隔">
            <Input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="benchmark,v1" />
          </Field>
        </div>

        <Field label="描述">
          <Textarea
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="数据来源、清洗方式、适用场景…"
          />
        </Field>

        {uploading && <Progress value={progress} showLabel tone="primary" />}
        {error && <ErrorBox message={error} />}
      </div>
    </Dialog>
  )
}

/* -------------------------------------------------------------------------- */
function PreviewDialog({ datasetId, onClose }: { datasetId: string | null; onClose: () => void }) {
  const dataset = useQuery({
    queryKey: ['dataset', datasetId],
    queryFn: () => api.dataset(datasetId!),
    enabled: Boolean(datasetId),
  })
  const preview = useQuery({
    queryKey: ['dataset-preview', datasetId],
    queryFn: () => api.datasetPreview(datasetId!),
    enabled: Boolean(datasetId),
  })

  return (
    <Dialog
      open={Boolean(datasetId)}
      onOpenChange={(open) => !open && onClose()}
      width="max-w-3xl"
      title={dataset.data?.name ?? '数据集预览'}
      description={dataset.data ? `${dataset.data.kind} · ${dataset.data.root_path}` : undefined}
    >
      {preview.isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : preview.data?.tasks ? (
        <div className="space-y-3">
          {(preview.data.tasks as { task: string; instruction: string; task_toml: string }[]).map((task) => (
            <div key={task.task} className="rounded-lg border border-border p-3">
              <div className="num text-xs font-medium text-fg">{task.task}</div>
              <pre className="num mt-1.5 max-h-40 overflow-auto rounded bg-bg-elevated p-2 text-[11px] leading-relaxed text-fg-muted">
                {task.instruction}
              </pre>
              <details className="mt-1.5">
                <summary className="cursor-pointer text-[11px] text-primary">task.toml</summary>
                <pre className="num mt-1 max-h-40 overflow-auto rounded bg-bg-elevated p-2 text-[11px]">
                  {task.task_toml}
                </pre>
              </details>
            </div>
          ))}
        </div>
      ) : (
        <pre className="num max-h-96 overflow-auto rounded-lg bg-bg-elevated p-3 text-[11px] leading-relaxed">
          {JSON.stringify(preview.data?.records ?? [], null, 2)}
        </pre>
      )}

      {dataset.data && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Badge>{formatNumber(dataset.data.stats.n_records)} 条样本</Badge>
          <Badge>{formatNumber(dataset.data.stats.n_tasks)} 个任务</Badge>
          <Badge>{formatBytes(dataset.data.stats.total_bytes)}</Badge>
          {dataset.data.stats.columns.slice(0, 8).map((column) => (
            <Badge key={column} tone="info">
              {column}
            </Badge>
          ))}
        </div>
      )}
    </Dialog>
  )
}
