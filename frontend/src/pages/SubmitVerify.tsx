import * as React from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Bot, Brain, ChevronDown, Container, Database, Eraser, FlaskConical, Globe, KeyRound, Rocket, Settings2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import type { Prebuild } from '@/lib/api'
import type { ApiProtocol, Dataset, DeploymentEndpoint, ModelApi, RegistryDataset, VerifyConfig } from '@/lib/types'
import { useProject } from '@/lib/store'
import { clearDraft, useDraftState } from '@/lib/draft'
import { cn } from '@/lib/utils'
import { supportsProtocol } from '@/lib/model-api'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ErrorBox,
  Field,
  Input,
  NumberInput,
  Select,
  Textarea,
  ToggleRow,
} from '@/components/ui'
import { InlineHint, Mono, PageHeader, WarnHint } from '@/components/common'
import { ProjectBar } from '@/components/ProjectBar'
import { CompatDocButton } from '@/components/CompatDoc'
import { DatasetPicker, KeyValueEditor } from '@/components/pickers'

const DEFAULT_VERIFY: VerifyConfig = {
  framework: 'harbor',
  dataset_id: '',
  tasks_path: '',
  registry_dataset: '',
  agent: 'oracle',
  model_api_id: '',
  agent_api_protocol: '',
  model: '',
  reasoning_effort: '',
  agent_import_path: '',
  n_concurrent: 4,
  n_attempts: 1,
  max_retries: 0,
  environment_type: 'docker',
  task_names: [],
  exclude_task_names: [],
  n_tasks: null,
  agent_env: {},
  agent_kwargs: {},
  verifier_env: {},
  environment_kwargs: {},
  verifier_kwargs: {},
  judge_model_api_id: '',
  judge_api_protocol: 'anthropic',
  environment_cpus: null,
  environment_memory_mb: null,
  environment_gpus: null,
  timeout_multiplier: 1,
  agent_timeout_multiplier: null,
  verifier_timeout_multiplier: null,
  disable_verification: false,
  use_platform_image: false,
  force_build: false,
  export_traces: false,
  sharegpt: false,
  verbose_log: true,
  extra_args: {},
}

const EFFORT_OPTIONS = [
  { value: 'default', label: '默认（不指定）', description: '交给 agent 自己的默认值' },
  { value: 'low', label: 'low', description: '最少思考，最快最省' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high', description: '多数 agent 的默认档' },
  { value: 'xhigh', label: 'xhigh' },
  { value: 'max', label: 'max', description: '最深思考，最慢最贵' },
]

/** 协议已确定后，按平台、模型逐级选择；密钥由后台注入。 */
function ModelApiPicker({ value, onChange, protocol, allowManual = true }: {
  value: string
  onChange: (item: ModelApi | null) => void
  protocol: ApiProtocol
  allowManual?: boolean
}) {
  const models = useQuery({ queryKey: ['model-apis'], queryFn: () => api.modelApis() })
  const rows = (models.data ?? []).filter((m) => m.enabled && supportsProtocol(m, protocol))
  const current = rows.find((m) => m.id === value)
  const [vendor, setVendor] = React.useState('')
  const previousProtocol = React.useRef(protocol)
  React.useEffect(() => {
    if (current) setVendor(current.vendor)
    else if (previousProtocol.current !== protocol) setVendor('')
    previousProtocol.current = protocol
  }, [protocol, current?.vendor])
  const vendors = [...new Map(rows.map((m) => [m.vendor, m.vendor_label])).entries()]
  const shown = rows.filter((m) => m.vendor === vendor)
  return <div className="space-y-2">
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="平台" required>
        <Select value={vendor} placeholder="先选择平台" onValueChange={(next) => { setVendor(next); onChange(null) }} options={[
          ...vendors.map(([id, label]) => ({ value: id, label, disabled: !rows.some((m) => m.vendor === id && m.ready) })),
          ...(allowManual ? [{ value: 'manual', label: '手动填写', description: '使用下方模型标识和环境变量' }] : []),
        ]} />
      </Field>
      <Field label="模型" required>
        <Select value={current?.id ?? ''} disabled={!vendor || vendor === 'manual'} placeholder={vendor ? '再选择模型' : '请先选择平台'} onValueChange={(next) => onChange(rows.find((m) => m.id === next) ?? null)} options={shown.map((m) => ({ value: m.id, label: m.name, description: `${m.platform_name}${m.ready ? '' : ' · 未配置密钥'}`, disabled: !m.ready }))} />
      </Field>
    </div>
    {current && <div className="flex items-center justify-between gap-2"><p className="min-w-0 break-all text-[11px] text-fg-subtle">共用「{current.platform_name}」的平台密钥 · {current.endpoints[protocol]}</p><Button size="sm" variant="ghost" onClick={() => onChange(null)}>清除选择</Button></div>}
    {!rows.length && !models.isLoading && <p className="text-[11px] text-fg-subtle">暂无符合此接口的模型，请在「平台管理 → 模型 API」配置。</p>}
    {models.error && <p className="text-xs text-danger">{models.error.message}</p>}
  </div>
}

/** Harbor 官方注册表里的数据集（terminal-bench 等），运行时需要联网 clone。 */
function RegistryPicker({ value, onChange }: { value: string; onChange: (ref: string) => void }) {
  const [keyword, setKeyword] = React.useState('')
  const registry = useQuery({
    queryKey: ['harbor-registry'],
    queryFn: () => api.harborRegistry(),
    staleTime: 60_000,
    // 后端在后台拉 13 MB 的 registry.json；拉的时候先给空/旧列表，这里盯着它
    refetchInterval: (query) => (query.state.data?.source === 'loading' ? 4000 : false),
  })
  const rows: RegistryDataset[] = registry.data?.datasets ?? []
  const needle = keyword.trim().toLowerCase()
  const shown = needle ? rows.filter((r) => `${r.name} ${r.description}`.toLowerCase().includes(needle)) : rows
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder={`搜索 ${rows.length} 个官方数据集`} className="h-8 text-xs" />
        <Button
          variant="ghost"
          size="sm"
          loading={registry.data?.source === 'loading'}
          onClick={async () => {
            await api.harborRegistry(true)
            registry.refetch()
          }}
        >
          刷新
        </Button>
      </div>
      {registry.data?.source === 'loading' && (
        <p className="text-[11px] text-fg-subtle">正在从 GitHub 拉取注册表（约 13 MB，首次要十几秒）…</p>
      )}
      {registry.data?.error && <WarnHint>{registry.data.error}</WarnHint>}
      {registry.data?.source === 'cache' && !registry.data.error && (
        <p className="text-[11px] text-fg-subtle">列表来自本地缓存（6 小时内不重复拉取）。</p>
      )}
      <div className="max-h-64 divide-y divide-border/60 overflow-y-auto rounded-md border border-border">
        {shown.map((row) => {
          const selected = row.ref === value
          return (
            <button
              key={row.ref}
              type="button"
              aria-pressed={selected}
              onClick={() => onChange(selected ? '' : row.ref)}
              className={cn('flex w-full items-start gap-3 px-3 py-2 text-left transition-colors', selected ? 'bg-primary-soft' : 'hover:bg-card-hover')}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className={cn('text-sm font-medium', selected ? 'text-primary' : 'text-fg')}>{row.name}</span>
                  <Badge>{row.version || 'latest'}</Badge>
                  <Badge tone="info">{row.task_count} 任务</Badge>
                </div>
                <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-fg-subtle">{row.description}</p>
              </div>
            </button>
          )
        })}
        {!shown.length && !registry.isLoading && (
          <p className="px-3 py-6 text-center text-xs text-fg-subtle">{rows.length ? '没有匹配的数据集' : '拿不到注册表（无网络？）'}</p>
        )}
      </div>
      <p className="text-[11px] leading-relaxed text-fg-subtle">
        <Globe className="mr-1 inline h-3 w-3" aria-hidden />
        官方数据集运行时由 Harbor 从 GitHub 拉取任务仓库，机器需要能访问外网。
      </p>
    </div>
  )
}

/** 让评测直接指向平台里跑着的 vLLM 服务，省得手抄 base_url 和模型名。 */
function LocalEndpointPicker({ onPick }: { onPick: (endpoint: DeploymentEndpoint) => void }) {
  const endpoints = useQuery({
    queryKey: ['deployment-endpoints'],
    queryFn: () => api.deploymentEndpoints(),
    refetchInterval: 15000,
  })
  const serving = (endpoints.data ?? []).filter((item) => item.status === 'serving')

  if (!serving.length) {
    return (
      <p className="text-[11px] leading-relaxed text-fg-subtle">
        想评测本地训练出来的模型？先到「模型部署」用 vLLM 起一个服务，这里就会出现一键填充的入口。
      </p>
    )
  }

  return (
    <div className="rounded-md border border-border bg-bg-elevated px-3 py-2.5">
      <p className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-fg-muted">
        <Rocket className="h-3.5 w-3.5" aria-hidden />
        使用本地部署的模型
      </p>
      <div className="flex flex-wrap gap-1.5">
        {serving.map((endpoint) => (
          <Button key={endpoint.id} variant="outline" size="sm" onClick={() => onPick(endpoint)}>
            {endpoint.name}
            <span className="text-fg-subtle">· {endpoint.model}</span>
          </Button>
        ))}
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-fg-subtle">
        点击会把模型填成 <Mono>hosted_vllm/&lt;模型名&gt;</Mono>，并把 <Mono>OPENAI_BASE_URL</Mono> 写进 agent
        环境变量。地址用的是**宿主机 IP** 而不是 <Mono>127.0.0.1</Mono>——agent 跑在容器里，回环地址连不上。
      </p>
    </div>
  )
}

function Advanced({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false)
  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-fg-muted hover:text-fg"
        aria-expanded={open}
      >
        {title}
        <ChevronDown className={cn('h-4 w-4 transition-transform', open && 'rotate-180')} aria-hidden />
      </button>
      {open && <div className="space-y-4 border-t border-border p-3">{children}</div>}
    </div>
  )
}

const Grid = ({ children, cols = 3 }: { children: React.ReactNode; cols?: 2 | 3 }) => (
  <div className={cn('grid gap-3', cols === 2 ? 'sm:grid-cols-2' : 'sm:grid-cols-2 lg:grid-cols-3')}>{children}</div>
)

const DRAFT = 'submit-verify'

export default function SubmitVerify() {
  const navigate = useNavigate()
  const { projectId, setProject } = useProject()
  const [searchParams] = useSearchParams()
  const fromJobId = searchParams.get('from')
  // 任务详情页「重跑」带过来的源评测任务：预填它的配置，改完再提交
  const rerunId = searchParams.get('rerun')
  const rerunSource = useQuery({
    queryKey: ['job', rerunId],
    queryFn: () => api.job(rerunId!),
    enabled: Boolean(rerunId),
    retry: false,
  })

  const agents = useQuery({ queryKey: ['harbor-agents'], queryFn: () => api.harborAgents() })
  const environments = useQuery({ queryKey: ['harbor-envs'], queryFn: () => api.harborEnvironments() })
  const toolchain = useQuery({ queryKey: ['toolchain'], queryFn: () => api.toolchain() })
  const platformImage = useQuery({ queryKey: ['harbor-image'], queryFn: api.harborImage })
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.projects() })
  const projectName = projects.data?.find((row) => row.project.id === projectId)?.project.name
  const suggestion = useQuery({
    queryKey: ['verify-suggestion', fromJobId],
    queryFn: () => api.verifySuggestion(fromJobId!),
    enabled: Boolean(fromJobId),
    retry: false,
  })

  // 草稿缓存：切页面再回来不用重填（提交成功后清掉）
  const [name, setName] = useDraftState(DRAFT, 'name', '')
  const [note, setNote] = useDraftState(DRAFT, 'note', '')
  const [tags, setTags] = useDraftState(DRAFT, 'tags', '')
  const [nGpus, setNGpus] = useDraftState(DRAFT, 'nGpus', 0)
  const [config, setConfig] = useDraftState<VerifyConfig>(DRAFT, 'config', DEFAULT_VERIFY)
  const [source, setSource] = useDraftState<'uploaded' | 'registry' | 'path'>(DRAFT, 'source', 'uploaded')
  const [taskFilter, setTaskFilter] = useDraftState(DRAFT, 'taskFilter', '')
  const [excludeFilter, setExcludeFilter] = useDraftState(DRAFT, 'excludeFilter', '')
  const [parentJobId, setParentJobId] = useDraftState<string | null>(DRAFT, 'parentJobId', null)
  const [error, setError] = React.useState<string | null>(null)

  const resetForm = () => {
    setName('')
    setNote('')
    setTags('')
    setNGpus(0)
    setConfig(DEFAULT_VERIFY)
    setSource('uploaded')
    setTaskFilter('')
    setExcludeFilter('')
    setParentJobId(null)
    setError(null)
  }

  const appliedRerun = React.useRef<string | null>(null)
  React.useEffect(() => {
    const job = rerunSource.data
    if (!job?.verify || appliedRerun.current === job.id) return
    appliedRerun.current = job.id
    const verify = job.verify
    setName(`${job.name.replace(/ \(重跑\)/g, '').trim()} (重跑)`)
    setNote(job.note ?? '')
    setTags(job.tags.join(','))
    setNGpus(job.resources.n_gpus)
    setConfig({ ...DEFAULT_VERIFY, ...verify })
    setSource(verify.registry_dataset ? 'registry' : verify.tasks_path && !verify.dataset_id ? 'path' : 'uploaded')
    setTaskFilter((verify.task_names ?? []).join(','))
    setExcludeFilter((verify.exclude_task_names ?? []).join(','))
    setParentJobId(job.id)
    if (job.project_id && job.project_id !== projectId) setProject(job.project_id)
    toast.info(`已载入评测「${job.name}」的配置，修改后提交即可重跑`)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rerunSource.data])

  const patch = (values: Partial<VerifyConfig>) => setConfig((previous) => ({ ...previous, ...values }))
  const agentInfo = agents.data?.find((item) => item.name === config.agent)
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: () => api.modelApis() })
  const agentProtocol = config.agent === 'claude-code' ? 'anthropic' : config.agent === 'codex' ? 'openai' : config.agent_api_protocol || 'openai'
  const agentWireProtocol: ApiProtocol = config.agent === 'codex' ? 'responses' : agentProtocol
  React.useEffect(() => {
    if (!modelApis.data) return
    setConfig((prev) => {
      const agent = modelApis.data!.find((m) => m.id === prev.model_api_id)
      const judge = modelApis.data!.find((m) => m.id === prev.judge_model_api_id)
      const clearAgent = Boolean(agent && !supportsProtocol(agent, agentWireProtocol))
      const clearJudge = Boolean(judge && !supportsProtocol(judge, prev.judge_api_protocol))
      return clearAgent || clearJudge ? { ...prev, ...(clearAgent ? { model_api_id: '', model: '' } : {}), ...(clearJudge ? { judge_model_api_id: '' } : {}) } : prev
    })
  }, [agentWireProtocol, config.judge_api_protocol, modelApis.data, setConfig])
  const modelApiName = modelApis.data?.find((m) => m.id === config.model_api_id)?.name
  // DatasetPicker（components/pickers.tsx）用同一个 queryKey，直接复用缓存
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api.datasets() })
  const selectedDataset = datasets.data?.find((d) => d.id === config.dataset_id)
  const [envTasksPath, setEnvTasksPath] = React.useState('')
  React.useEffect(() => {
    const timer = window.setTimeout(() => setEnvTasksPath(source === 'path' ? config.tasks_path.trim() : ''), 400)
    return () => window.clearTimeout(timer)
  }, [source, config.tasks_path])
  const envDatasetId = source === 'uploaded' ? config.dataset_id : ''
  const canDetectEnv = Boolean(envDatasetId || (source === 'path' && envTasksPath))
  const verifierEnv = useQuery({
    queryKey: ['harbor-verifier-env', envDatasetId, source === 'path' ? envTasksPath : '', selectedDataset?.updated_at],
    queryFn: () => api.harborVerifierEnv(envDatasetId ? { dataset_id: envDatasetId } : { tasks_path: envTasksPath }),
    enabled: canDetectEnv,
    retry: false,
  })
  const autoEnvKeys = React.useRef(new Set<string>())
  React.useEffect(() => {
    const fields = canDetectEnv ? verifierEnv.data?.variables : []
    if (!fields) return
    const keys = new Set(fields.map((field) => field.key))
    const previousAutoKeys = autoEnvKeys.current
    const nextAutoKeys = new Set<string>()
    setConfig((previous) => {
      const next = { ...previous.verifier_env }
      let changed = false
      for (const key of previousAutoKeys) {
        if (!keys.has(key) && next[key] === '') {
          delete next[key]
          changed = true
        } else if (keys.has(key)) nextAutoKeys.add(key)
      }
      for (const key of keys) {
        if (!(key in next)) {
          next[key] = ''
          nextAutoKeys.add(key)
          changed = true
        }
      }
      autoEnvKeys.current = nextAutoKeys
      return changed ? { ...previous, verifier_env: next } : previous
    })
  }, [verifierEnv.data, verifierEnv.dataUpdatedAt, canDetectEnv, setConfig])
  const selectedJudge = modelApis.data?.find((model) => model.id === config.judge_model_api_id)
  const managedVerifierKeys = new Set(
    config.judge_model_api_id && !config.disable_verification
      ? ['JUDGE_API_KEY', 'JUDGE_MODEL', 'JUDGE_PROVIDER', 'JUDGE_API_PROTOCOL',
        ...(selectedJudge?.endpoints?.[config.judge_api_protocol] ? ['JUDGE_BASE_URL'] : [])]
      : [],
  )
  const verifierEnvHints: Record<string, string> = {}
  const verifierEnvPlaceholders: Record<string, string> = {}
  for (const field of verifierEnv.data?.variables ?? []) {
    const managed = managedVerifierKeys.has(field.key) || managedVerifierKeys.has(field.source_variable ?? '')
    const hint = managed ? '由所选 Judge 模型自动注入；填写后覆盖模型配置'
      : field.required ? '必填：任务没有默认值，请填写实际值'
      : field.conflicting_defaults ? '可选：各任务默认值不同，留空沿用各自配置'
      : field.secret && field.has_task_value ? '可选：任务已设置密钥，留空沿用；密钥不回显'
      : field.default ? `可选：默认 ${field.default}，填写后覆盖`
      : '可选：默认空值，可留空，无需用 1 占位'
    verifierEnvHints[field.key] = `${hint} · 来源 ${field.tasks.length} 个 task.toml`
    verifierEnvPlaceholders[field.key] = managed ? '由 Judge 模型自动注入'
      : field.required ? '请填写实际值'
      : field.default ? `默认：${field.default}` : '可留空'
  }
  const judgeRequired =
    source === 'uploaded' && Boolean(selectedDataset?.rewardkit) && !config.disable_verification
  const harborReady = toolchain.data?.find((row) => row.key === 'harbor-cli')?.python_ok ?? true
  const dockerRow = toolchain.data?.find((row) => row.key === 'docker')
  const dockerBlocked = config.environment_type === 'docker' && dockerRow ? dockerRow.status !== 'ready' : false

  React.useEffect(() => {
    if (suggestion.data && !note) {
      setNote(`评测训练任务 ${suggestion.data.parent_job_id} 的产物：${suggestion.data.model_path}`)
    }
  }, [suggestion.data, note])

  // 从「评测该产物」进来时，跟随那次训练所属的项目——
  // 否则评测会落在用户当前恰好选中的项目里，和被评测的训练对不上。
  const suggestedProject = suggestion.data?.project_id
  React.useEffect(() => {
    if (!suggestedProject || suggestedProject === projectId) return
    setProject(suggestedProject)
    toast.info(`已切换到该训练任务所属的项目「${suggestion.data?.project_name ?? suggestedProject}」`)
  }, [suggestedProject, projectId, setProject, suggestion.data?.project_name])

  // Pre-fill the env keys an agent typically needs so the user just pastes values.
  React.useEffect(() => {
    if (!agentInfo) return
    setConfig((previous) => {
      if (previous.model_api_id) return previous  // 密钥由模型配置注入，不用手填
      const next = { ...previous.agent_env }
      let changed = false
      for (const key of agentInfo.env_keys) {
        if (!(key in next)) {
          next[key] = ''
          changed = true
        }
      }
      return changed ? { ...previous, agent_env: next } : previous
    })
  }, [agentInfo])

  const submit = useMutation({
    mutationFn: () =>
      api.createJob({
        name: name.trim(),
        note,
        type: 'verify',
        project_id: projectId,
        tags: tags.split(',').map((tag) => tag.trim()).filter(Boolean),
        resources: { n_gpus: nGpus, priority: 0, env: {} },
        verify: {
          ...config,
          agent_api_protocol: agentProtocol,
          task_names: splitList(taskFilter),
          exclude_task_names: splitList(excludeFilter),
          agent_env: cleanKv(config.agent_env),
          verifier_env: cleanKv(config.verifier_env),
          agent_kwargs: cleanKv(config.agent_kwargs),
          environment_kwargs: cleanKv(config.environment_kwargs),
          verifier_kwargs: cleanKv(config.verifier_kwargs),
        },
        parent_job_id: fromJobId ?? parentJobId,
      }),
    onSuccess: (job) => {
      toast.success(job.status === 'pending_review' ? '已提交，等待管理员审核' : '已提交并进入排队')
      clearDraft(DRAFT)
      navigate(`/jobs/${job.id}`)
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '提交失败'),
  })

  const problems = React.useMemo(() => {
    const out: string[] = []
    if (!name.trim()) out.push('请填写任务名称')
    if (config.use_platform_image) {
      if (!platformImage.data?.ready) out.push('平台 Harbor 镜像未就绪，请先在平台管理构建')
      if (config.environment_type !== 'docker' || config.registry_dataset) out.push('平台镜像仅支持 Docker 环境和上传或服务器本地任务集')
    }
    if (!config.dataset_id && !config.tasks_path && !config.registry_dataset) out.push('请选择要评测的任务集')
    if (agentInfo?.needs_model && !config.model && !config.model_api_id) out.push(`agent ${config.agent} 需要选择或填写模型`)
    if (judgeRequired && !config.judge_model_api_id) out.push('该任务集是 reward-kit 格式，请选择 judge 模型')
    if (!config.disable_verification) {
      const missing = (verifierEnv.data?.variables ?? [])
        .filter((field) => field.required && !managedVerifierKeys.has(field.key)
          && !managedVerifierKeys.has(field.source_variable ?? '')
          && !config.verifier_env[field.key]?.trim() && !config.agent_env[field.key]?.trim()
          && !(field.source_variable && (config.verifier_env[field.source_variable]?.trim()
            || config.agent_env[field.source_variable]?.trim())))
        .map((field) => field.key)
      if (missing.length) out.push(`请填写任务要求的 Verifier 环境变量：${missing.join(' / ')}`)
    }
    if (!config.model_api_id) {
      const missingKeys = Object.entries(config.agent_env)
        .filter(([key, value]) => agentInfo?.env_keys.includes(key) && !value)
        .map(([key]) => key)
      if (missingKeys.length) out.push(`请填写 ${missingKeys.join(' / ')}（或改为选择平台的模型配置）`)
    }
    return out
  }, [name, config, agentInfo, judgeRequired, verifierEnv.data, modelApis.data, platformImage.data])

  return (
    <div className="space-y-4">
      <PageHeader
        title={rerunId ? '编辑配置并重跑' : '提交评测任务'}
        description="基于 Harbor framework 在沙箱中运行 agent 并按任务 verifier 打分"
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={resetForm} title="表单内容会自动缓存，点这里恢复成默认值">
              <Eraser className="h-3.5 w-3.5" aria-hidden />
              清空草稿
            </Button>
            <CompatDocButton defaultFramework="harbor" label="Harbor 说明" />
          </>
        }
      />

      <ProjectBar
        hint={
          fromJobId && suggestion.data?.project_name
            ? `已跟随训练任务落在同一项目`
            : undefined
        }
      />

      {!harborReady && (
        <WarnHint>
          当前部署环境没有检测到 <Mono>harbor</Mono> 可执行文件，任务可以提交但会在启动时失败。
          请先安装 Harbor（<Mono>uv tool install harbor</Mono>）或在系统设置中配置 <Mono>PRT_HARBOR_BIN</Mono>。
        </WarnHint>
      )}

      {dockerBlocked && (
        <WarnHint>
          沙箱选择了 docker，但本机 docker 不可用：{dockerRow?.detail}
        </WarnHint>
      )}

      {suggestion.data && (
        <InlineHint>
          来自训练任务 <Mono>{suggestion.data.parent_job_id}</Mono>：产物目录 <Mono>{suggestion.data.model_path}</Mono>
          {suggestion.data.checkpoints.length > 0 && ` · ${suggestion.data.checkpoints.length} 个 checkpoint`}。
          {suggestion.data.hint}
        </InlineHint>
      )}

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader title="基本信息" icon={<FlaskConical className="h-4 w-4" aria-hidden />} />
            <CardBody className="space-y-4">
              <Grid cols={2}>
                <Field label="任务名称" required htmlFor="verify-name">
                  <Input
                    id="verify-name"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="例如：terminal-bench-4k-claude-code-pass4"
                  />
                </Field>
                <Field label="标签" htmlFor="verify-tags" hint="逗号分隔">
                  <Input id="verify-tags" value={tags} onChange={(event) => setTags(event.target.value)} />
                </Field>
              </Grid>
              <Field label="备注" htmlFor="verify-note">
                <Textarea id="verify-note" rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
              </Field>
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="任务集"
              description="Harbor 任务树：每个任务包含 task.toml / instruction.md / environment / tests"
              icon={<Database className="h-4 w-4" aria-hidden />}
              actions={
                <Link to="/datasets">
                  <Button variant="ghost" size="sm">
                    上传任务集
                  </Button>
                </Link>
              }
            />
            <CardBody className="space-y-4">
              <div className="flex h-9 w-fit items-center gap-0.5 rounded-md border border-border bg-bg-elevated p-0.5" role="group" aria-label="任务集来源">
                {(
                  [
                    ['uploaded', '已上传的任务集'],
                    ['registry', 'Harbor 官方数据集'],
                    ['path', '服务器目录'],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    aria-pressed={source === key}
                    onClick={() => {
                      setSource(key)
                      // 三个来源互斥：切换时清掉其他两个，后端也只接受一个
                      patch({ dataset_id: '', registry_dataset: '', tasks_path: '' })
                    }}
                    className={cn('h-full rounded px-3 text-[13px] transition-colors', source === key ? 'bg-card font-medium text-primary shadow-sm' : 'text-fg-muted hover:text-fg')}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {source === 'uploaded' && (
                <DatasetPicker
                  projectId={projectId}
                  value={config.dataset_id ? [config.dataset_id] : []}
                  onChange={(ids) => patch({ dataset_id: ids[0] ?? '' })}
                  kinds={['harbor_tasks']}
                  emptyHint="请在「数据集」页上传 harbor_tasks 类型的压缩包（tasks/<task>/task.toml）。"
                />
              )}
              {!config.use_platform_image && source === 'uploaded' && selectedDataset && <PrebuildPanel dataset={selectedDataset} />}
              {source === 'registry' && (
                <RegistryPicker value={config.registry_dataset} onChange={(ref) => patch({ registry_dataset: ref })} />
              )}
              {source === 'path' && (
                <Field label="服务器上的任务目录" hint="绝对路径，适合已经放在机器上的大任务集">
                  <Input
                    value={config.tasks_path}
                    onChange={(event) => patch({ tasks_path: event.target.value })}
                    placeholder="/workspace/LiHanlin_Workspace/datasets/PRT-Terminal-Bench-Data4k"
                  />
                </Field>
              )}
            </CardBody>
          </Card>

          {!config.disable_verification && (
            <Card>
              <CardHeader
                title={judgeRequired ? 'Judge 模型（reward-kit 打分）' : 'Judge 模型（可选）'}
                description="任务使用 LLM 打分时，平台按所选接口注入 JUDGE_API_KEY / JUDGE_BASE_URL / JUDGE_MODEL / JUDGE_PROVIDER / JUDGE_API_PROTOCOL；reward-kit 任务需要选择 Judge。"
                icon={<Bot className="h-4 w-4" aria-hidden />}
              />
              <CardBody className="space-y-4">
                <Field label="Judge 接口" required hint="先选择接口，再选择支持该接口的平台与模型">
                  <Select value={config.judge_api_protocol} onValueChange={(value) => patch({ judge_api_protocol: value as VerifyConfig['judge_api_protocol'] })} options={[{ value: 'anthropic', label: 'Anthropic' }, { value: 'openai', label: 'OpenAI' }]} />
                </Field>
                <ModelApiPicker value={config.judge_model_api_id} protocol={config.judge_api_protocol} allowManual={false} onChange={(item) => patch({ judge_model_api_id: item?.id ?? '' })} />
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader title="Agent 与模型" icon={<Bot className="h-4 w-4" aria-hidden />} />
            <CardBody className="space-y-4">
              <Field label="Agent" required hint={agentInfo?.description}>
                <Select
                  value={config.agent}
                  onValueChange={(value) => patch({ agent: value, agent_api_protocol: value === 'claude-code' ? 'anthropic' : 'openai' })}
                  options={(agents.data ?? []).map((agent) => ({
                    value: agent.name,
                    label: agent.recommended ? `${agent.name} ★` : agent.name,
                    description: agent.description || undefined,
                  }))}
                />
              </Field>

              {agentInfo?.needs_model && (
                <>
                  <Field label="模型接口" required hint={config.agent === 'claude-code' || config.agent === 'codex' ? `${config.agent} 已自动选择对应接口` : '先选择接口，再选择平台与模型'}>
                    <Select value={agentProtocol} disabled={config.agent === 'claude-code' || config.agent === 'codex'} onValueChange={(value) => patch({ agent_api_protocol: value as VerifyConfig['agent_api_protocol'] })} options={[{ value: 'openai', label: 'OpenAI' }, { value: 'anthropic', label: 'Anthropic' }]} />
                  </Field>
                  <ModelApiPicker value={config.model_api_id} protocol={agentWireProtocol} onChange={(item) => patch({ model_api_id: item?.id ?? '', model: item ? '' : config.model })} />

                  <Field
                    label={
                      <span className="flex items-center gap-1.5">
                        <Brain className="h-3.5 w-3.5" aria-hidden />
                        思考模式
                      </span>
                    }
                    hint={
                      agentInfo?.effort_levels?.length
                        ? `${config.agent} 支持：${agentInfo.effort_levels.join(' / ')}；选了不支持的档位会自动收敛到最近一档`
                        : `${config.agent} 不支持思考模式参数`
                    }
                  >
                    <Select
                      value={config.reasoning_effort || 'default'}
                      onValueChange={(v) => patch({ reasoning_effort: (v === 'default' ? '' : v) as VerifyConfig['reasoning_effort'] })}
                      options={EFFORT_OPTIONS}
                      disabled={!agentInfo?.effort_levels?.length}
                    />
                  </Field>

                  {!config.model_api_id && (
                    <Field
                      label="手动指定模型"
                      required
                      hint="litellm 格式，例如 anthropic/claude-sonnet-4-5、openai/gpt-5.1、hosted_vllm/local-model；密钥填在下方环境变量里"
                    >
                      <Input
                        value={config.model}
                        onChange={(event) => patch({ model: event.target.value })}
                        placeholder="anthropic/claude-sonnet-4-5"
                      />
                    </Field>
                  )}

                  <LocalEndpointPicker
                    onPick={(endpoint) => {
                      // litellm 用 hosted_vllm/ 前缀走 OpenAI 兼容协议。
                      // 地址必须用 sandbox_base_url（宿主机 IP）：agent 跑在容器里，
                      // 容器内的 127.0.0.1 是容器自己，回环地址一定连不上。
                      // OPENAI_BASE_URL 是 Harbor 的首选键，OPENAI_API_BASE 是它的回退键，
                      // 两个都给以兼容不同 agent。
                      patch({
                        model_api_id: '',
                        model: `hosted_vllm/${endpoint.model}`,
                        agent_env: {
                          ...config.agent_env,
                          OPENAI_BASE_URL: endpoint.sandbox_base_url,
                          OPENAI_API_BASE: endpoint.sandbox_base_url,
                          OPENAI_API_KEY: config.agent_env.OPENAI_API_KEY || 'EMPTY',
                        },
                      })
                      toast.success(`已指向本地部署「${endpoint.name}」`)
                    }}
                  />
                </>
              )}

              {!agentInfo?.needs_model && (
                <InlineHint>
                  oracle / nop 属于自检 agent：oracle 执行任务自带的 solution，reward 应接近 1.0；
                  nop 什么都不做，reward 应为 0。正式评测前先跑这两个可以确认任务集本身是可靠的。
                </InlineHint>
              )}

              {agentInfo?.env_keys.length ? (
                <Field
                  label={
                    <span className="flex items-center gap-1.5">
                      <KeyRound className="h-3.5 w-3.5" aria-hidden />
                      Agent 环境变量
                    </span>
                  }
                  hint="这些值会通过 --agent-env 注入到沙箱中的 agent；含 KEY/TOKEN 的字段不会出现在日志与命令记录里"
                >
                  <KeyValueEditor
                    value={config.agent_env}
                    onChange={(next) => patch({ agent_env: next })}
                    keyPlaceholder="ANTHROPIC_API_KEY"
                    valuePlaceholder="sk-..."
                  />
                </Field>
              ) : (
                <Advanced title="Agent 环境变量（可选）">
                  <KeyValueEditor value={config.agent_env} onChange={(next) => patch({ agent_env: next })} />
                </Advanced>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="评测超参数"
              description="pass@k 与并发是最影响耗时与成本的两个参数"
              icon={<Settings2 className="h-4 w-4" aria-hidden />}
            />
            <CardBody className="space-y-4">
              <Grid cols={3}>
                <Field label="pass@k（每题尝试次数）" required hint="k>1 时平台会统计 pass@1..pass@k">
                  <NumberInput
                    value={config.n_attempts}
                    min={1}
                    max={64}
                    onChange={(value) => patch({ n_attempts: Math.max(1, value ?? 1) })}
                  />
                </Field>
                <Field label="并发数" required hint="同时运行的 trial 数；Docker 后端受本机 CPU/内存限制">
                  <NumberInput
                    value={config.n_concurrent}
                    min={1}
                    max={256}
                    onChange={(value) => patch({ n_concurrent: Math.max(1, value ?? 1) })}
                  />
                </Field>
                <Field label="沙箱后端">
                  <Select
                    value={config.environment_type}
                    onValueChange={(value) => patch({ environment_type: value })}
                    options={(environments.data ?? ['docker']).map((item) => ({ value: item, label: item }))}
                  />
                </Field>
                <Field label="失败重试次数" hint="只对可重试的异常生效">
                  <NumberInput value={config.max_retries} min={0} max={10} onChange={(value) => patch({ max_retries: value ?? 0 })} />
                </Field>
                <Field label="任务数量上限" hint="留空表示跑完整个任务集">
                  <NumberInput value={config.n_tasks} min={1} onChange={(value) => patch({ n_tasks: value })} />
                </Field>
                <Field label="超时倍数" hint="1.0 表示使用任务自身的超时设置">
                  <NumberInput
                    value={config.timeout_multiplier}
                    step="0.5"
                    min={0.1}
                    onChange={(value) => patch({ timeout_multiplier: value ?? 1 })}
                  />
                </Field>
              </Grid>

              <Grid cols={2}>
                <Field label="只跑匹配的任务" hint="glob，多个用逗号分隔，例如 bash-*,net-*">
                  <Input value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)} placeholder="bash-*" />
                </Field>
                <Field label="排除任务" hint="glob，多个用逗号分隔">
                  <Input value={excludeFilter} onChange={(event) => setExcludeFilter(event.target.value)} placeholder="flaky-*" />
                </Field>
              </Grid>

              <div className="grid gap-2 sm:grid-cols-2">
                <ToggleRow
                  label="导出轨迹"
                  hint="评测结束后导出 ATIF 轨迹，可用于后续 SFT / 蒸馏"
                  checked={config.export_traces}
                  onCheckedChange={(checked) => patch({ export_traces: checked })}
                />
                <ToggleRow
                  label="跳过 verifier"
                  hint="只跑 agent 不打分，用于调试环境"
                  checked={config.disable_verification}
                  onCheckedChange={(checked) =>
                    patch({ disable_verification: checked, ...(checked ? { judge_model_api_id: '' } : {}) })
                  }
                />
                <ToggleRow
                  label="使用平台现有 Harbor 镜像"
                  hint={platformImage.data?.ready ? '镜像已就绪；复用公共依赖，只构建题目文件和初始化层。仅支持本地标准任务模板。' : '镜像未就绪或状态未知，请到平台管理 → Harbor 镜像检查并构建。'}
                  checked={config.use_platform_image}
                  onCheckedChange={(checked) => patch({ use_platform_image: checked, ...(checked ? { force_build: false, environment_type: 'docker' } : {}) })}
                />
                <ToggleRow
                  label="强制重建镜像"
                  hint="忽略已构建的任务镜像重新 build，较慢"
                  checked={config.force_build}
                  onCheckedChange={(checked) => patch({ force_build: checked, ...(checked ? { use_platform_image: false } : {}) })}
                />
              </div>

              <Advanced title="进阶：沙箱资源与超时">
                <Grid cols={3}>
                  <Field label="容器 CPU 数">
                    <NumberInput value={config.environment_cpus} min={1} onChange={(value) => patch({ environment_cpus: value })} />
                  </Field>
                  <Field label="容器内存 (MB)">
                    <NumberInput
                      value={config.environment_memory_mb}
                      min={256}
                      onChange={(value) => patch({ environment_memory_mb: value })}
                    />
                  </Field>
                  <Field label="容器 GPU 数">
                    <NumberInput value={config.environment_gpus} min={0} onChange={(value) => patch({ environment_gpus: value })} />
                  </Field>
                  <Field label="agent 超时倍数">
                    <NumberInput
                      value={config.agent_timeout_multiplier}
                      step="0.5"
                      onChange={(value) => patch({ agent_timeout_multiplier: value })}
                    />
                  </Field>
                  <Field label="verifier 超时倍数">
                    <NumberInput
                      value={config.verifier_timeout_multiplier}
                      step="0.5"
                      onChange={(value) => patch({ verifier_timeout_multiplier: value })}
                    />
                  </Field>
                </Grid>
              </Advanced>

              <Field
                label="Verifier 环境变量 (--verifier-env)"
                hint="选择任务集后自动读取 task.toml 的 [verifier.env]；填写实际值，密钥不要用 1 等短值占位"
              >
                <div className="mb-2 flex items-center justify-between gap-2 text-xs text-fg-muted">
                  <span>
                    {verifierEnv.isFetching ? '正在检测任务环境变量…'
                      : verifierEnv.data ? `已检测 ${verifierEnv.data.n_tasks} 个任务 · ${verifierEnv.data.variables.length} 个变量`
                      : source === 'registry' ? '注册表任务下载前可手动添加变量'
                      : '选择任务集或填写服务器目录后自动检测'}
                  </span>
                  {canDetectEnv && <Button variant="ghost" size="sm" onClick={() => verifierEnv.refetch()}>重新检测</Button>}
                </div>
                {verifierEnv.error && <p role="alert" className="mb-2 text-xs text-danger">
                  {verifierEnv.error instanceof ApiError ? verifierEnv.error.message : '环境变量检测失败，请重新检测'}
                </p>}
                {!!verifierEnv.data?.warnings.length && <p className="mb-2 text-xs text-warning">{verifierEnv.data.warnings.join('；')}</p>}
                <KeyValueEditor
                  value={config.verifier_env}
                  onChange={(next) => patch({ verifier_env: next })}
                  keyPlaceholder="EVAL_API_KEY"
                  valuePlaceholder="实际值"
                  rowHints={verifierEnvHints}
                  valuePlaceholders={verifierEnvPlaceholders}
                  secretNames={verifierEnv.data?.variables.filter((field) => field.secret).map((field) => field.key)}
                />
              </Field>

              <Advanced title="进阶：kwargs 与容器环境变量">
                <Field label="Agent kwargs (--agent-kwarg)">
                  <KeyValueEditor
                    value={config.agent_kwargs}
                    onChange={(next) => patch({ agent_kwargs: next })}
                    secretKeys={false}
                    keyPlaceholder="max_turns"
                    valuePlaceholder="40"
                  />
                </Field>
                <Field label="环境 kwargs (--environment-kwarg)">
                  <KeyValueEditor
                    value={config.environment_kwargs}
                    onChange={(next) => patch({ environment_kwargs: next })}
                    secretKeys={false}
                  />
                </Field>
                <Field label="Verifier kwargs (--verifier-kwarg)" hint="reward-kit judge 的参数可以写在这里">
                  <KeyValueEditor
                    value={config.verifier_kwargs}
                    onChange={(next) => patch({ verifier_kwargs: next })}
                    secretKeys={false}
                  />
                </Field>
              </Advanced>
            </CardBody>
          </Card>
        </div>

        <aside className="xl:sticky xl:top-20 xl:self-start">
          <Card>
            <CardHeader title="提交摘要" />
            <CardBody className="space-y-3">
              <div className="flex flex-wrap gap-1.5">
                <Badge tone="primary">Harbor</Badge>
                <Badge>{config.agent}</Badge>
                <Badge tone="info">pass@{config.n_attempts}</Badge>
                <Badge tone="success">并发 {config.n_concurrent}</Badge>
                <Badge>{config.environment_type}</Badge>
              </div>

              <dl className="space-y-1.5 text-xs">
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-muted">项目</dt>
                  <dd className="truncate text-right text-fg">{projectName ?? '未选择'}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-muted">任务集</dt>
                  <dd className="truncate text-right text-fg">
                    {config.dataset_id ? '已选择' : config.tasks_path || config.registry_dataset || '未选择'}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-muted">模型</dt>
                  <dd className="truncate text-right text-fg">
                    {config.model_api_id ? modelApiName ?? '模型配置' : config.model || '（无需模型）'}
                    {config.reasoning_effort && ` · ${config.reasoning_effort}`}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-muted">密钥</dt>
                  <dd className="text-right text-fg">
                    {Object.values(config.agent_env).filter(Boolean).length} 项已填写
                  </dd>
                </div>
              </dl>

              {problems.length > 0 && (
                <ul className="space-y-1 rounded-lg border border-warning/30 bg-warning-soft px-2.5 py-2">
                  {problems.map((problem) => (
                    <li key={problem} className="text-[11px] text-warning">
                      · {problem}
                    </li>
                  ))}
                </ul>
              )}

              {error && <ErrorBox message={error} />}

              <Field label="GPU 数量" hint="评测通常在容器中调用外部模型，一般填 0">
                <NumberInput value={nGpus} min={0} onChange={(value) => setNGpus(Math.max(0, value ?? 0))} />
              </Field>

              <Button
                variant="primary"
                size="lg"
                className="w-full justify-center"
                disabled={problems.length > 0}
                loading={submit.isPending}
                onClick={() => {
                  setError(null)
                  submit.mutate()
                }}
              >
                提交评测
              </Button>
              <p className="text-[11px] leading-relaxed text-fg-subtle">
                评测同样需要管理员审核。审核通过后平台会执行 <Mono>harbor run</Mono>，
                并把 pass@k、reward 分布与花费回填到任务详情。
              </p>
            </CardBody>
          </Card>
        </aside>
      </div>
    </div>
  )
}

function splitList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function cleanKv(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([key, value]) => key.trim() && value !== ''))
}

/* -------------------------------------------------------------------------- */
/** 挑一个任务先把它的 docker 环境 build 一遍：提前暴露 Dockerfile 问题，并预热层缓存。 */
function PrebuildPanel({ dataset }: { dataset: Dataset }) {
  const tasks = dataset.files.filter((file) => file.role === 'tasks').map((file) => file.path)
  const [task, setTask] = React.useState('')
  const [buildId, setBuildId] = React.useState<string | null>(null)
  React.useEffect(() => {
    setTask(tasks[0] ?? '')
    setBuildId(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataset.id])

  const start = useMutation({
    mutationFn: () => api.startPrebuild(dataset.id, task),
    onSuccess: (build) => {
      setBuildId(build.id)
      toast.info(`开始预构建 ${build.task}`)
    },
    onError: (exception) => toast.error(exception instanceof ApiError ? exception.message : '预构建启动失败'),
  })
  const status = useQuery({
    queryKey: ['prebuild', buildId],
    queryFn: () => api.prebuild(buildId!),
    enabled: Boolean(buildId),
    refetchInterval: (query) => ((query.state.data as Prebuild | undefined)?.status === 'running' ? 2000 : false),
  })
  const build = status.data
  const logRef = React.useRef<HTMLPreElement>(null)
  React.useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [build?.log])

  if (!tasks.length) return null
  return (
    <div className="min-w-0 w-full max-w-full overflow-hidden space-y-2 rounded-lg border border-border bg-bg-elevated p-3">
      <div className="flex items-center gap-1.5 text-[13px] font-medium text-fg">
        <Container className="h-3.5 w-3.5" aria-hidden />
        预构建容器
      </div>
      <p className="text-[11px] text-fg-muted">
        挑一个任务先 build 它的 environment（Dockerfile / docker-compose / docker_image），提前发现镜像问题，
        正式评测时也能复用 docker 层缓存。不占 GPU、不走审核。
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1 basis-60">
          <Select value={task} onValueChange={setTask} options={tasks.map((path) => ({ value: path, label: path }))} />
        </div>
        <Button
          variant="secondary"
          size="sm"
          disabled={!task || build?.status === 'running'}
          loading={start.isPending}
          onClick={() => start.mutate()}
        >
          <Container className="h-3.5 w-3.5" aria-hidden />
          预构建
        </Button>
      </div>
      {build && (
        <div className="min-w-0 max-w-full space-y-1.5">
          <div className="flex min-w-0 items-center gap-2 text-[11px]">
            <Badge tone={build.status === 'succeeded' ? 'success' : build.status === 'failed' ? 'danger' : 'info'}>
              {build.status === 'succeeded' ? '构建成功' : build.status === 'failed' ? `构建失败 (${build.return_code})` : '构建中…'}
            </Badge>
            <code className="num min-w-0 flex-1 truncate text-fg-subtle" title={build.command}>{build.command}</code>
          </div>
          <pre
            ref={logRef}
            className="num min-w-0 w-full max-w-full max-h-56 overflow-auto rounded bg-card p-2 text-[11px] leading-relaxed text-fg-muted"
          >
            {build.log || '（暂无输出）'}
          </pre>
        </div>
      )}
    </div>
  )
}
