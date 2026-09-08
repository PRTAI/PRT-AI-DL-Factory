/**
 * 模型部署（vLLM）+ checkpoint 合并导出。
 *
 * 两件事放一个页面：LoRA 训练完之后的动作链条是「合并导出 → 部署 → 评测时选这个端点」，
 * 分成两页会让用户在中间断掉。
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Copy,
  FileDown,
  Package,
  Plus,
  Rocket,
  ScrollText,
  Server,
  Square,
  Trash2,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useProject } from '@/lib/store'
import { copyText, formatBytes, formatDuration, formatRelative } from '@/lib/utils'
import { Mono, PageHeader, InlineHint } from '@/components/common'
import { ProjectTag } from '@/components/ProjectTag'
import { ModelPicker } from '@/components/pickers'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  KeyValue,
  LinkButton,
  NumberInput,
  Select,
  Skeleton,
  StatusDot,
  Switch,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Textarea,
  Th,
  Tr,
} from '@/components/ui'
import type { Deployment, DeploymentStatus, ExportStatus, ModelEntry, VLLMConfig } from '@/lib/types'

const DEPLOY_STATUS: Record<DeploymentStatus, { label: string; tone: 'success' | 'info' | 'danger' | 'neutral' }> = {
  starting: { label: '启动中', tone: 'info' },
  serving: { label: '服务中', tone: 'success' },
  failed: { label: '已失败', tone: 'danger' },
  stopped: { label: '已停止', tone: 'neutral' },
}

const EXPORT_STATUS: Record<ExportStatus, { label: string; tone: 'success' | 'info' | 'danger' | 'neutral' }> = {
  queued: { label: '排队中', tone: 'neutral' },
  running: { label: '导出中', tone: 'info' },
  succeeded: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
}

export default function Deployments() {
  const [tab, setTab] = React.useState('serving')
  const [creating, setCreating] = React.useState(false)
  const [exporting, setExporting] = React.useState(false)
  const [logFor, setLogFor] = React.useState<{ kind: 'deployment' | 'export'; id: string; name: string } | null>(null)

  return (
    <div>
      <PageHeader
        title="模型部署"
        description="用 vLLM 把本地模型拉成 OpenAI 兼容服务，评测时可直接选用；LoRA 训练产物先合并导出再部署。"
        actions={
          <>
            <Button onClick={() => setExporting(true)}>
              <Package className="h-4 w-4" aria-hidden />
              合并导出
            </Button>
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" aria-hidden />
              部署模型
            </Button>
          </>
        }
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-4">
          <TabsTrigger value="serving">推理服务</TabsTrigger>
          <TabsTrigger value="exports">合并导出</TabsTrigger>
        </TabsList>

        <TabsContent value="serving">
          <DeploymentList onShowLog={(id, name) => setLogFor({ kind: 'deployment', id, name })} />
        </TabsContent>

        <TabsContent value="exports">
          <ExportList
            onShowLog={(id, name) => setLogFor({ kind: 'export', id, name })}
            onCreate={() => setExporting(true)}
          />
        </TabsContent>
      </Tabs>

      <CreateDeploymentDialog open={creating} onOpenChange={setCreating} />
      <CreateExportDialog open={exporting} onOpenChange={setExporting} />
      <LogDialog target={logFor} onClose={() => setLogFor(null)} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* 推理服务列表                                                                */
/* -------------------------------------------------------------------------- */
function DeploymentList({ onShowLog }: { onShowLog: (id: string, name: string) => void }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const deployments = useQuery({
    queryKey: ['deployments'],
    queryFn: () => api.deployments(),
    // 启动中的部署要盯着 /v1/models 探测结果变化
    refetchInterval: (query) =>
      (query.state.data ?? []).some((item) => item.status === 'starting') ? 3000 : 15000,
  })

  const stop = useMutation({
    mutationFn: (id: string) => api.stopDeployment(id),
    onSuccess: () => {
      toast.success('已请求停止')
      queryClient.invalidateQueries({ queryKey: ['deployments'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteDeployment(id),
    onSuccess: () => {
      toast.success('记录已删除')
      queryClient.invalidateQueries({ queryKey: ['deployments'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  // 容器内可达地址（宿主机 IP）由后端算，前端拿不到宿主机 IP
  const endpoints = useQuery({
    queryKey: ['deployment-endpoints'],
    queryFn: () => api.deploymentEndpoints(),
    refetchInterval: 20000,
  })
  const sandbox = new Map((endpoints.data ?? []).map((row) => [row.id, row.sandbox_base_url]))

  const rows = deployments.data ?? []
  const canControl = (item: Deployment) => user?.role === 'admin' || item.owner_id === user?.id

  if (deployments.isLoading) {
    return (
      <Card className="space-y-2 p-4">
        {[0, 1].map((index) => (
          <Skeleton key={index} className="h-14 w-full" />
        ))}
      </Card>
    )
  }

  if (!rows.length) {
    return (
      <Card>
        <EmptyState
          icon={<Rocket className="h-8 w-8" aria-hidden />}
          title="还没有部署任何模型"
          description="部署会在本机起一个 vLLM 的 OpenAI 兼容服务；评测 codex / claude-code 这类 agent 时可以直接指向它。"
        />
      </Card>
    )
  }

  return (
    <Card>
      <Table>
        <thead>
          <tr>
            <Th>服务</Th>
            <Th className="w-[96px]">状态</Th>
            <Th className="w-[220px]">端点 / 模型名</Th>
            <Th className="w-[110px]">GPU / 并行</Th>
            <Th className="w-[130px]">负责人</Th>
            <Th className="w-[96px]">时长</Th>
            <Th className="w-[150px]">操作</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const meta = DEPLOY_STATUS[item.status]
            const uptime =
              item.started_at && item.status === 'serving'
                ? formatDuration((Date.now() - new Date(item.started_at).getTime()) / 1000)
                : null
            return (
              <Tr key={item.id}>
                <Td>
                  <div className="w-[240px] max-w-[28vw]">
                    <div className="truncate text-[13px] font-medium text-fg">{item.name}</div>
                    <div className="truncate text-[11px] text-fg-subtle" title={item.model_path}>
                      {item.model_path}
                    </div>
                    {/* 部署是跨项目共享的推理端点，这里记的是「谁起的」 */}
                    {item.project_id && <ProjectTag projectId={item.project_id} className="mt-0.5" />}
                  </div>
                </Td>
                <Td>
                  <StatusDot tone={meta.tone} label={meta.label} pulse={item.status === 'starting'} />
                  {item.error && (
                    <div className="mt-0.5 line-clamp-2 text-[11px] text-danger" title={item.error}>
                      {item.error}
                    </div>
                  )}
                </Td>
                <Td>
                  <div className="flex items-center gap-1.5">
                    <Mono>{`:${item.port}/v1`}</Mono>
                    <button
                      type="button"
                      aria-label="复制本机端点地址"
                      className="text-fg-subtle transition-colors hover:text-primary"
                      title="复制本机地址（curl 验证用）"
                      onClick={async () => {
                        const ok = await copyText(`http://127.0.0.1:${item.port}/v1`)
                        toast[ok ? 'success' : 'error'](ok ? '已复制本机地址' : '复制失败')
                      }}
                    >
                      <Copy className="h-3 w-3" aria-hidden />
                    </button>
                    {sandbox.get(item.id) && (
                      <button
                        type="button"
                        className="text-[11px] text-primary hover:underline"
                        title={`容器内可达地址：${sandbox.get(item.id)}`}
                        onClick={async () => {
                          const ok = await copyText(sandbox.get(item.id)!)
                          toast[ok ? 'success' : 'error'](ok ? '已复制容器内可达地址' : '复制失败')
                        }}
                      >
                        容器内地址
                      </button>
                    )}
                  </div>
                  <div className="mt-0.5 truncate text-[11px] text-fg-subtle">
                    model: {item.served_model_name}
                  </div>
                </Td>
                <Td className="text-xs text-fg-muted">
                  {item.gpu_ids.length ? `GPU ${item.gpu_ids.join(', ')}` : 'CPU / 不记账'}
                  <div className="text-[11px] text-fg-subtle">
                    TP {item.config.tensor_parallel_size}
                    {item.config.pipeline_parallel_size > 1 && ` · PP ${item.config.pipeline_parallel_size}`}
                  </div>
                </Td>
                <Td className="truncate text-xs text-fg-muted">{item.owner_email}</Td>
                <Td className="text-xs text-fg-subtle">
                  {uptime ?? (item.stopped_at ? formatRelative(item.stopped_at) : '—')}
                </Td>
                <Td>
                  <div className="flex items-center gap-3 whitespace-nowrap">
                    <LinkButton onClick={() => onShowLog(item.id, item.name)}>
                      <ScrollText className="mr-0.5 inline h-3 w-3" aria-hidden />
                      日志
                    </LinkButton>
                    <LinkButton
                      onClick={() => stop.mutate(item.id)}
                      disabled={!canControl(item) || !['starting', 'serving'].includes(item.status)}
                    >
                      <Square className="mr-0.5 inline h-3 w-3" aria-hidden />
                      停止
                    </LinkButton>
                    <LinkButton
                      tone="danger"
                      disabled={!canControl(item) || ['starting', 'serving'].includes(item.status)}
                      onClick={() => {
                        if (window.confirm(`删除部署记录「${item.name}」？`)) remove.mutate(item.id)
                      }}
                    >
                      <Trash2 className="mr-0.5 inline h-3 w-3" aria-hidden />
                      删除
                    </LinkButton>
                  </div>
                </Td>
              </Tr>
            )
          })}
        </tbody>
      </Table>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
/* 合并导出列表                                                                */
/* -------------------------------------------------------------------------- */
function ExportList({
  onShowLog,
  onCreate,
}: {
  onShowLog: (id: string, name: string) => void
  onCreate: () => void
}) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const exports = useQuery({
    queryKey: ['exports'],
    queryFn: () => api.exports(),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((item) => item.status === 'running') ? 3000 : 20000,
  })

  const remove = useMutation({
    mutationFn: ({ id, purge }: { id: string; purge: boolean }) => api.deleteExport(id, purge),
    onSuccess: () => {
      toast.success('已删除')
      queryClient.invalidateQueries({ queryKey: ['exports'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelExport(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['exports'] }),
    onError: (error: ApiError) => toast.error(error.message),
  })

  const rows = exports.data ?? []

  if (exports.isLoading) {
    return (
      <Card className="space-y-2 p-4">
        <Skeleton className="h-14 w-full" />
      </Card>
    )
  }

  if (!rows.length) {
    return (
      <Card>
        <EmptyState
          icon={<Package className="h-8 w-8" aria-hidden />}
          title="还没有导出任务"
          description="LoRA 训练出来的是 adapter，vLLM 不能直接加载；合并导出会把它并回底座，得到可直接部署的完整权重。"
          action={
            <Button variant="primary" onClick={onCreate}>
              <Package className="h-4 w-4" aria-hidden />
              合并导出
            </Button>
          }
        />
      </Card>
    )
  }

  return (
    <Card>
      <Table>
        <thead>
          <tr>
            <Th>导出</Th>
            <Th className="w-[96px]">状态</Th>
            <Th>产物目录</Th>
            <Th className="w-[90px] text-right">大小</Th>
            <Th className="w-[130px]">发起人</Th>
            <Th className="w-[150px]">操作</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const meta = EXPORT_STATUS[item.status]
            const mine = user?.role === 'admin' || item.owner_id === user?.id
            return (
              <Tr key={item.id}>
                <Td>
                  <div className="w-[200px] max-w-[22vw]">
                    <div className="truncate text-[13px] font-medium text-fg">{item.name}</div>
                    <div className="truncate text-[11px] text-fg-subtle" title={item.adapter_path}>
                      adapter: {item.adapter_path.split('/').slice(-2).join('/')}
                    </div>
                  </div>
                </Td>
                <Td>
                  <StatusDot tone={meta.tone} label={meta.label} pulse={item.status === 'running'} />
                  {item.error && (
                    <div className="mt-0.5 line-clamp-2 text-[11px] text-danger" title={item.error}>
                      {item.error}
                    </div>
                  )}
                </Td>
                <Td>
                  <div className="flex w-[280px] max-w-[30vw] items-center gap-1.5">
                    <Mono className="truncate">{item.export_dir}</Mono>
                    <button
                      type="button"
                      aria-label="复制产物目录"
                      className="shrink-0 text-fg-subtle transition-colors hover:text-primary"
                      onClick={async () => {
                        const ok = await copyText(item.export_dir)
                        toast[ok ? 'success' : 'error'](ok ? '已复制路径' : '复制失败')
                      }}
                    >
                      <Copy className="h-3 w-3" aria-hidden />
                    </button>
                  </div>
                </Td>
                <Td className="num text-right text-xs text-fg-muted">
                  {item.output_bytes ? formatBytes(item.output_bytes) : '—'}
                </Td>
                <Td className="truncate text-xs text-fg-muted">{item.owner_email}</Td>
                <Td>
                  <div className="flex items-center gap-3 whitespace-nowrap">
                    <LinkButton onClick={() => onShowLog(item.id, item.name)}>
                      <ScrollText className="mr-0.5 inline h-3 w-3" aria-hidden />
                      日志
                    </LinkButton>
                    <LinkButton onClick={() => cancel.mutate(item.id)} disabled={!mine || item.status !== 'running'}>
                      取消
                    </LinkButton>
                    <LinkButton
                      tone="danger"
                      disabled={!mine || item.status === 'running'}
                      onClick={() => {
                        if (window.confirm(`删除导出记录「${item.name}」及其产物文件？`)) {
                          remove.mutate({ id: item.id, purge: true })
                        }
                      }}
                    >
                      <Trash2 className="mr-0.5 inline h-3 w-3" aria-hidden />
                      删除
                    </LinkButton>
                  </div>
                </Td>
              </Tr>
            )
          })}
        </tbody>
      </Table>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
/* 新建部署                                                                    */
/* -------------------------------------------------------------------------- */
const DEFAULT_VLLM: VLLMConfig = {
  tensor_parallel_size: 1,
  pipeline_parallel_size: 1,
  gpu_memory_utilization: 0.9,
  max_model_len: null,
  max_num_seqs: 256,
  dtype: 'auto',
  quantization: '',
  trust_remote_code: true,
  enable_prefix_caching: true,
  enforce_eager: false,
  api_key: '',
  chat_template: '',
  extra_args: {},
}

function CreateDeploymentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  const [source, setSource] = React.useState<'model' | 'export'>('model')
  const [name, setName] = React.useState('')
  const [note, setNote] = React.useState('')
  const [model, setModel] = React.useState<ModelEntry | null>(null)
  const [exportId, setExportId] = React.useState('')
  const [served, setServed] = React.useState('')
  const [port, setPort] = React.useState<number | null>(null)
  const [nGpus, setNGpus] = React.useState<number | null>(1)
  const [config, setConfig] = React.useState<VLLMConfig>(DEFAULT_VLLM)

  const finished = useQuery({
    queryKey: ['exports', 'succeeded'],
    queryFn: () => api.exports(),
    enabled: open,
    select: (rows) => rows.filter((row) => row.status === 'succeeded'),
  })

  const create = useMutation({
    mutationFn: () => {
      const chosen = finished.data?.find((row) => row.id === exportId)
      return api.createDeployment({
        name: name.trim(),
        note,
        source: source === 'model' ? 'model' : 'path',
        model_id: source === 'model' ? model?.id ?? '' : '',
        model_path: source === 'model' ? '' : chosen?.export_dir ?? '',
        served_model_name: served.trim(),
        port,
        n_gpus: nGpus ?? 1,
        project_id: projectId,
        config: { ...config, max_model_len: config.max_model_len || null },
      })
    },
    onSuccess: (deployment) => {
      toast.success(`已启动部署「${deployment.name}」，正在加载模型…`)
      onOpenChange(false)
      setName('')
      setModel(null)
      setExportId('')
      setServed('')
      setConfig(DEFAULT_VLLM)
      queryClient.invalidateQueries({ queryKey: ['deployments'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const ready = name.trim() && (source === 'model' ? Boolean(model) : Boolean(exportId))
  const patch = (partial: Partial<VLLMConfig>) => setConfig((current) => ({ ...current, ...partial }))

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="部署模型"
      description="默认使用 vLLM 启动 OpenAI 兼容服务。占用的 GPU 会计入平台的 GPU 账本，训练任务不会再拿到同一张卡。"
      width="max-w-3xl"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!ready} onClick={() => create.mutate()}>
            <Rocket className="h-4 w-4" aria-hidden />
            启动
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="服务名称" required hint="仅用于平台内识别">
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="qwen3-4b-对齐版" />
          </Field>
          <Field label="对外模型名" hint="留空则用目录名；评测时 --model 填这个">
            <Input value={served} onChange={(event) => setServed(event.target.value)} placeholder="自动" />
          </Field>
        </div>

        <Field label="模型来源" required>
          <div className="flex gap-2">
            <Button
              variant={source === 'model' ? 'primary' : 'secondary'}
              size="sm"
              onClick={() => setSource('model')}
            >
              <Server className="h-3.5 w-3.5" aria-hidden />
              模型仓库
            </Button>
            <Button
              variant={source === 'export' ? 'primary' : 'secondary'}
              size="sm"
              onClick={() => setSource('export')}
            >
              <FileDown className="h-3.5 w-3.5" aria-hidden />
              已合并导出的产物
            </Button>
          </div>
        </Field>

        {source === 'model' ? (
          <ModelPicker value={model?.id ?? ''} onChange={setModel} />
        ) : finished.data?.length ? (
          <Field label="选择导出产物" required>
            <Select
              value={exportId}
              onValueChange={setExportId}
              placeholder="选择一个已完成的合并导出"
              options={finished.data.map((row) => ({
                value: row.id,
                label: row.name,
                description: row.export_dir,
              }))}
            />
          </Field>
        ) : (
          <InlineHint>
            还没有已完成的合并导出。LoRA 训练产物需要先「合并导出」成完整权重，vLLM 才能加载。
          </InlineHint>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="GPU 数量" hint="0 = 不参与 GPU 记账">
            <NumberInput value={nGpus} onChange={setNGpus} min={0} max={16} step={1} />
          </Field>
          <Field label="端口" hint="留空自动分配 8100-8199">
            <NumberInput value={port} onChange={setPort} min={1024} max={65535} step={1} />
          </Field>
          <Field label="张量并行 TP" hint="必须 ≤ GPU 数量">
            <NumberInput
              value={config.tensor_parallel_size}
              onChange={(value) => patch({ tensor_parallel_size: value ?? 1 })}
              min={1}
              max={16}
              step={1}
            />
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="显存占用比例" hint="vLLM 预分配的 KV cache 比例">
            <NumberInput
              value={config.gpu_memory_utilization}
              onChange={(value) => patch({ gpu_memory_utilization: value ?? 0.9 })}
              min={0.1}
              max={1}
              step={0.05}
            />
          </Field>
          <Field label="最大上下文" hint="留空用模型自带的 max_position_embeddings">
            <NumberInput
              value={config.max_model_len}
              onChange={(value) => patch({ max_model_len: value })}
              min={512}
              step={512}
            />
          </Field>
          <Field label="最大并发序列">
            <NumberInput
              value={config.max_num_seqs}
              onChange={(value) => patch({ max_num_seqs: value ?? 256 })}
              min={1}
              step={1}
            />
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="精度 dtype">
            <Select
              value={config.dtype}
              onValueChange={(value) => patch({ dtype: value })}
              options={[
                { value: 'auto', label: 'auto（按模型配置）' },
                { value: 'bfloat16', label: 'bfloat16' },
                { value: 'float16', label: 'float16' },
                { value: 'float32', label: 'float32' },
              ]}
            />
          </Field>
          <Field label="量化" hint="模型本身没量化就留空">
            <Select
              value={config.quantization || 'none'}
              onValueChange={(value) => patch({ quantization: value === 'none' ? '' : value })}
              options={[
                { value: 'none', label: '不量化' },
                { value: 'awq', label: 'awq' },
                { value: 'gptq', label: 'gptq' },
                { value: 'fp8', label: 'fp8' },
              ]}
            />
          </Field>
          <Field label="API Key" hint="留空表示不校验；填了会在日志里被打码">
            <Input
              type="password"
              value={config.api_key}
              onChange={(event) => patch({ api_key: event.target.value })}
              placeholder="选填"
            />
          </Field>
        </div>

        <div className="grid gap-2 sm:grid-cols-3">
          <ToggleLine
            label="trust_remote_code"
            checked={config.trust_remote_code}
            onChange={(checked) => patch({ trust_remote_code: checked })}
          />
          <ToggleLine
            label="prefix caching"
            checked={config.enable_prefix_caching}
            onChange={(checked) => patch({ enable_prefix_caching: checked })}
          />
          <ToggleLine
            label="enforce eager"
            checked={config.enforce_eager}
            onChange={(checked) => patch({ enforce_eager: checked })}
          />
        </div>

        <Field label="备注">
          <Textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="选填" />
        </Field>
      </div>
    </Dialog>
  )
}

function ToggleLine({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  const id = React.useId()
  return (
    <div className="flex items-center justify-between gap-2 rounded-md border border-border px-2.5 py-2">
      <label htmlFor={id} className="num text-[11px] text-fg-muted">
        {label}
      </label>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* 新建合并导出                                                                */
/* -------------------------------------------------------------------------- */
function CreateExportDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  const [jobId, setJobId] = React.useState('')
  const [adapterPath, setAdapterPath] = React.useState('')
  const [name, setName] = React.useState('')
  const [sizeGb, setSizeGb] = React.useState<number | null>(5)
  const [device, setDevice] = React.useState('cpu')

  // 只有 LoRA/freeze 的 SFT 任务能合并，所以候选列表从已结束的训练任务里挑
  const jobs = useQuery({
    queryKey: ['jobs', 'exportable', projectId],
    queryFn: () => api.jobs({ type: ['sft'], project_id: projectId, limit: 200 }),
    enabled: open,
    select: (payload) =>
      payload.items.filter(
        (job) => job.sft?.finetuning_type !== 'full' && ['succeeded', 'cancelled', 'failed'].includes(job.status),
      ),
  })

  const candidates = useQuery({
    queryKey: ['export-candidates', jobId],
    queryFn: () => api.exportCandidates(jobId),
    enabled: Boolean(jobId),
  })

  React.useEffect(() => {
    setAdapterPath('')
  }, [jobId])

  const create = useMutation({
    mutationFn: () =>
      api.createExport({
        job_id: jobId,
        name: name.trim(),
        adapter_path: adapterPath,
        export_size_gb: sizeGb ?? 5,
        export_device: device,
      }),
    onSuccess: (task) => {
      toast.success(`已开始导出「${task.name}」`)
      onOpenChange(false)
      setJobId('')
      setName('')
      queryClient.invalidateQueries({ queryKey: ['exports'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="合并导出 checkpoint"
      description="把 LoRA adapter 合并回底座模型，导出成可直接部署 / 分发的完整权重。用的是 LLaMA-Factory 自己的 export 流程。"
      width="max-w-2xl"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!jobId || candidates.data?.exportable === false}
            onClick={() => create.mutate()}
          >
            <Package className="h-4 w-4" aria-hidden />
            开始导出
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="训练任务" required hint="只列出当前项目里已结束的 LoRA / freeze 训练任务">
          <Select
            value={jobId}
            onValueChange={setJobId}
            placeholder={jobs.data?.length ? '选择一个训练任务' : '当前项目没有可导出的训练任务'}
            options={(jobs.data ?? []).map((job) => ({
              value: job.id,
              label: job.name,
              description: `${job.sft?.stage ?? ''} · ${job.owner_email}`,
            }))}
          />
        </Field>

        {candidates.data && !candidates.data.exportable && (
          <InlineHint>{candidates.data.reason}</InlineHint>
        )}

        {candidates.data?.exportable && (
          <>
            <Field label="选择 checkpoint" hint="留空取最终 adapter">
              <Select
                value={adapterPath || candidates.data.checkpoints[0]?.path || ''}
                onValueChange={setAdapterPath}
                options={candidates.data.checkpoints.map((item) => ({
                  value: item.path,
                  label: item.label,
                  description: item.path,
                }))}
              />
            </Field>
            <div className="rounded-md border border-border bg-bg-elevated px-3 py-2">
              <KeyValue label="底座模型">{candidates.data.base_model_path}</KeyValue>
              <KeyValue label="对话模板">{candidates.data.template}</KeyValue>
            </div>
          </>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="导出名称" hint="留空自动命名">
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="自动" />
          </Field>
          <Field label="分片大小 (GB)" hint="export_size">
            <NumberInput value={sizeGb} onChange={setSizeGb} min={1} max={50} step={1} />
          </Field>
          <Field label="合并设备" hint="cpu 更稳且不抢显存">
            <Select
              value={device}
              onValueChange={setDevice}
              options={[
                { value: 'cpu', label: 'cpu（推荐）' },
                { value: 'auto', label: 'auto（用 GPU 加速）' },
              ]}
            />
          </Field>
        </div>
      </div>
    </Dialog>
  )
}

/* -------------------------------------------------------------------------- */
/* 日志                                                                        */
/* -------------------------------------------------------------------------- */
function LogDialog({
  target,
  onClose,
}: {
  target: { kind: 'deployment' | 'export'; id: string; name: string } | null
  onClose: () => void
}) {
  const log = useQuery({
    queryKey: ['deploy-log', target?.kind, target?.id],
    queryFn: () =>
      target!.kind === 'deployment' ? api.deploymentLog(target!.id) : api.exportLog(target!.id),
    enabled: Boolean(target),
    refetchInterval: (query) => (query.state.data?.eof ? false : 3000),
  })

  return (
    <Dialog
      open={Boolean(target)}
      onOpenChange={(open) => !open && onClose()}
      title={target?.name ?? '日志'}
      description={target?.kind === 'deployment' ? 'vLLM 服务输出' : '合并导出输出'}
      width="max-w-4xl"
    >
      <pre className="num max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md bg-bg-elevated p-3 text-[11px] leading-relaxed text-fg-muted">
        {log.data?.content || '（暂无输出）'}
      </pre>
      {!log.data?.eof && (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-fg-subtle">
          <Badge tone="info">实时</Badge>
          每 3 秒刷新
        </p>
      )}
    </Dialog>
  )
}

export { DEPLOY_STATUS }
