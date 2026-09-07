import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CloudDownload, HardDriveDownload, RefreshCw, Search, Server, X } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
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
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import { CopyButton, FRAMEWORK_LABEL, Mono, PageHeader } from '@/components/common'
import { CompatDocButton } from '@/components/CompatDoc'

export default function Models() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [query, setQuery] = React.useState('')
  const [debounced, setDebounced] = React.useState('')
  const [downloadOpen, setDownloadOpen] = React.useState(searchParams.get('download') === '1')
  const [logId, setLogId] = React.useState<string | null>(null)

  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 250)
    return () => clearTimeout(timer)
  }, [query])

  const root = useQuery({ queryKey: ['models-root'], queryFn: () => api.modelsRoot() })
  const models = useQuery({ queryKey: ['models', debounced], queryFn: () => api.models({ q: debounced, limit: 500 }) })
  const downloads = useQuery({
    queryKey: ['downloads'],
    queryFn: () => api.downloads(),
    refetchInterval: (query) => {
      const items = query.state.data
      return items?.some((item) => item.status === 'running' || item.status === 'queued') ? 3000 : false
    },
  })

  const cancelDownload = useMutation({
    mutationFn: (id: string) => api.cancelDownload(id),
    onSuccess: () => {
      toast.success('已取消下载')
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '取消失败'),
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title="模型仓库"
        description={
          root.data
            ? `${root.data.models_root} · 已收录 ${root.data.count} 个模型`
            : '本地模型目录'
        }
        actions={
          <>
            <CompatDocButton />
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                await api.refreshModels()
                models.refetch()
                root.refetch()
                toast.success('已重新扫描模型目录')
              }}
            >
              <RefreshCw className={cn('h-3.5 w-3.5', models.isFetching && 'animate-spin')} aria-hidden />
              重新扫描
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setDownloadOpen(true)
                setSearchParams({}, { replace: true })
              }}
            >
              <CloudDownload className="h-3.5 w-3.5" aria-hidden />
              从 ModelScope 下载
            </Button>
          </>
        }
      />

      <Card>
        <div className="border-b border-border p-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索模型：厂家 / 名称 / 架构，例如 qwen3 / llama / vl"
              className="pl-8"
              aria-label="搜索模型"
            />
          </div>
        </div>

        {models.isLoading ? (
          <CardBody>
            <Skeleton className="h-32 w-full" />
          </CardBody>
        ) : models.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>模型</Th>
                <Th className="w-28">参数量</Th>
                <Th className="w-32">架构</Th>
                <Th className="w-40">LLaMA-Factory</Th>
                <Th className="w-40">可用框架</Th>
                <Th className="w-24">大小</Th>
              </tr>
            </thead>
            <tbody>
              {models.data.map((model) => (
                <Tr key={model.id}>
                  <Td>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-fg">{model.id}</span>
                      {model.is_multimodal && <Badge tone="info">多模态</Badge>}
                      {!model.has_tokenizer && <Badge tone="warning">缺少 tokenizer</Badge>}
                    </div>
                    <div className="num mt-0.5 truncate text-[11px] text-fg-subtle">{model.path}</div>
                    {model.notes.length > 0 && (
                      <div className="mt-0.5 text-[11px] text-warning">{model.notes[0]}</div>
                    )}
                  </Td>
                  <Td className="num text-xs">{model.num_parameters_b ? `${model.num_parameters_b}B` : '—'}</Td>
                  <Td className="num text-[11px] text-fg-muted">
                    <div>{model.model_type || '—'}</div>
                    <div className="truncate">{model.architectures[0] ?? ''}</div>
                  </Td>
                  <Td>
                    {model.llamafactory_template ? (
                      <Badge tone="primary">template: {model.llamafactory_template}</Badge>
                    ) : (
                      <span className="text-[11px] text-fg-subtle">未收录</span>
                    )}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {model.supported_frameworks.map((framework) => (
                        <Badge key={framework}>
                          {FRAMEWORK_LABEL[framework as keyof typeof FRAMEWORK_LABEL] ?? framework}
                        </Badge>
                      ))}
                    </div>
                  </Td>
                  <Td className="num text-xs text-fg-muted">
                    {formatBytes(model.size_bytes)}
                    <div className="mt-1">
                      <CopyButton value={model.path} label="复制路径" />
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            icon={<Server className="h-6 w-6" aria-hidden />}
            title="没有找到模型"
            description={
              <>
                模型目录：<Mono>{root.data?.models_root}</Mono>
                。可以从 ModelScope 按 <Mono>{'{厂家}/{模型名}'}</Mono> 下载。
              </>
            }
            action={
              <Button variant="primary" size="sm" onClick={() => setDownloadOpen(true)}>
                下载模型
              </Button>
            }
          />
        )}
      </Card>

      <Card>
        <CardHeader
          title="下载任务"
          description="调用 ModelScope CLI 拉取到模型目录，完成后会自动出现在上面的列表里"
        />
        {downloads.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>模型</Th>
                <Th className="w-24">状态</Th>
                <Th className="w-28">已下载</Th>
                <Th className="w-40">发起人</Th>
                <Th className="w-40">操作</Th>
              </tr>
            </thead>
            <tbody>
              {downloads.data.map((item) => (
                <Tr key={item.id}>
                  <Td>
                    <div className="num text-xs font-medium">{item.model_id}</div>
                    <div className="num truncate text-[11px] text-fg-subtle">{item.target_path}</div>
                    {item.error && <div className="text-[11px] text-danger">{item.error}</div>}
                  </Td>
                  <Td>
                    <Badge
                      tone={
                        item.status === 'succeeded'
                          ? 'success'
                          : item.status === 'failed'
                            ? 'danger'
                            : item.status === 'running'
                              ? 'info'
                              : 'neutral'
                      }
                    >
                      {item.status}
                    </Badge>
                  </Td>
                  <Td className="num text-xs">{formatBytes(item.downloaded_bytes)}</Td>
                  <Td>
                    <div className="text-xs text-fg-muted">{item.requested_by_email}</div>
                    <div className="text-[11px] text-fg-subtle">{formatRelative(item.created_at)}</div>
                  </Td>
                  <Td>
                    <div className="flex gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setLogId(item.id)}>
                        日志
                      </Button>
                      {user?.role === 'admin' && (item.status === 'running' || item.status === 'queued') && (
                        <Button variant="ghost" size="sm" onClick={() => cancelDownload.mutate(item.id)}>
                          <X className="h-3.5 w-3.5" aria-hidden />
                          取消
                        </Button>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            icon={<HardDriveDownload className="h-6 w-6" aria-hidden />}
            title="暂无下载任务"
            description="需要新模型时，直接粘贴 ModelScope 的「厂家/模型名」即可。"
          />
        )}
      </Card>

      <DownloadDialog
        open={downloadOpen}
        onOpenChange={setDownloadOpen}
        onStarted={() => queryClient.invalidateQueries({ queryKey: ['downloads'] })}
      />
      <DownloadLogDialog downloadId={logId} onClose={() => setLogId(null)} />
    </div>
  )
}

function DownloadDialog({
  open,
  onOpenChange,
  onStarted,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onStarted: () => void
}) {
  const [modelId, setModelId] = React.useState('')
  const [revision, setRevision] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)

  const start = useMutation({
    mutationFn: () => api.startDownload(modelId.trim(), revision.trim()),
    onSuccess: (item) => {
      toast.success(`已开始下载 ${item.model_id}`)
      onStarted()
      onOpenChange(false)
      setModelId('')
      setRevision('')
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '下载失败'),
  })

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="从 ModelScope 下载模型"
      description="粘贴 ModelScope 上的模型 ID，格式为 {厂家}/{模型名}"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!modelId.includes('/')}
            loading={start.isPending}
            onClick={() => {
              setError(null)
              start.mutate()
            }}
          >
            开始下载
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="模型 ID" required hint="例如 Qwen/Qwen3-8B、deepseek-ai/DeepSeek-R1-Distill-Qwen-7B">
          <Input
            value={modelId}
            onChange={(event) => setModelId(event.target.value)}
            placeholder="Qwen/Qwen3-8B"
            className="num"
          />
        </Field>
        <Field label="版本 revision（可选）" hint="留空表示 master">
          <Input value={revision} onChange={(event) => setRevision(event.target.value)} className="num" />
        </Field>
        <p className="text-[11px] leading-relaxed text-fg-subtle">
          下载会在后台执行，模型保存到 <Mono>{'<模型目录>/{厂家}/{模型名}'}</Mono>，完成后自动出现在模型列表中。
          大模型请预留足够磁盘空间。
        </p>
        {error && <ErrorBox message={error} />}
      </div>
    </Dialog>
  )
}

function DownloadLogDialog({ downloadId, onClose }: { downloadId: string | null; onClose: () => void }) {
  const log = useQuery({
    queryKey: ['download-log', downloadId],
    queryFn: () => api.downloadLog(downloadId!),
    enabled: Boolean(downloadId),
    refetchInterval: 3000,
  })
  return (
    <Dialog open={Boolean(downloadId)} onOpenChange={(open) => !open && onClose()} width="max-w-3xl" title="下载日志">
      <pre className="num max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-bg-elevated p-3 text-[11px] leading-relaxed">
        {log.data?.content || '（暂无输出）'}
      </pre>
      <p className="mt-2 text-[11px] text-fg-subtle">{formatNumber(log.data?.content.length ?? 0)} 字符</p>
    </Dialog>
  )
}
