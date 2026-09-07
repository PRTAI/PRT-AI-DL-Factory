import * as React from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Bot, ChevronDown, Database, FlaskConical, KeyRound, Settings2, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import type { Preset, VerifyConfig } from '@/lib/types'
import { cn } from '@/lib/utils'
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
import { CompatDocButton } from '@/components/CompatDoc'
import { DatasetPicker, KeyValueEditor } from '@/components/pickers'

const DEFAULT_VERIFY: VerifyConfig = {
  framework: 'harbor',
  dataset_id: '',
  tasks_path: '',
  registry_dataset: '',
  agent: 'oracle',
  model: '',
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
  environment_cpus: null,
  environment_memory_mb: null,
  environment_gpus: null,
  timeout_multiplier: 1,
  agent_timeout_multiplier: null,
  verifier_timeout_multiplier: null,
  disable_verification: false,
  force_build: false,
  export_traces: false,
  sharegpt: false,
  extra_args: {},
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

export default function SubmitVerify() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const fromJobId = searchParams.get('from')

  const agents = useQuery({ queryKey: ['harbor-agents'], queryFn: () => api.harborAgents() })
  const environments = useQuery({ queryKey: ['harbor-envs'], queryFn: () => api.harborEnvironments() })
  const presets = useQuery({ queryKey: ['presets'], queryFn: () => api.presets() })
  const toolchain = useQuery({ queryKey: ['toolchain'], queryFn: () => api.toolchain() })
  const suggestion = useQuery({
    queryKey: ['verify-suggestion', fromJobId],
    queryFn: () => api.verifySuggestion(fromJobId!),
    enabled: Boolean(fromJobId),
    retry: false,
  })

  const [name, setName] = React.useState('')
  const [note, setNote] = React.useState('')
  const [tags, setTags] = React.useState('')
  const [nGpus, setNGpus] = React.useState(0)
  const [config, setConfig] = React.useState<VerifyConfig>(DEFAULT_VERIFY)
  const [taskFilter, setTaskFilter] = React.useState('')
  const [excludeFilter, setExcludeFilter] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)

  const patch = (values: Partial<VerifyConfig>) => setConfig((previous) => ({ ...previous, ...values }))
  const agentInfo = agents.data?.find((item) => item.name === config.agent)
  const harborReady = toolchain.data?.find((row) => row.key === 'harbor-cli')?.python_ok ?? true
  const dockerRow = toolchain.data?.find((row) => row.key === 'docker')
  const dockerBlocked = config.environment_type === 'docker' && dockerRow ? dockerRow.status !== 'ready' : false

  React.useEffect(() => {
    if (suggestion.data && !note) {
      setNote(`评测训练任务 ${suggestion.data.parent_job_id} 的产物：${suggestion.data.model_path}`)
    }
  }, [suggestion.data, note])

  // Pre-fill the env keys an agent typically needs so the user just pastes values.
  React.useEffect(() => {
    if (!agentInfo) return
    setConfig((previous) => {
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

  const applyPreset = (preset: Preset) => {
    setConfig((previous) => ({ ...previous, ...(preset.config as Partial<VerifyConfig>) }))
    toast.success(`已应用预设：${preset.label}`)
  }

  const submit = useMutation({
    mutationFn: () =>
      api.createJob({
        name: name.trim(),
        note,
        type: 'verify',
        tags: tags.split(',').map((tag) => tag.trim()).filter(Boolean),
        resources: { n_gpus: nGpus, priority: 0, env: {} },
        verify: {
          ...config,
          task_names: splitList(taskFilter),
          exclude_task_names: splitList(excludeFilter),
          agent_env: cleanKv(config.agent_env),
          verifier_env: cleanKv(config.verifier_env),
          agent_kwargs: cleanKv(config.agent_kwargs),
          environment_kwargs: cleanKv(config.environment_kwargs),
          verifier_kwargs: cleanKv(config.verifier_kwargs),
        },
        parent_job_id: fromJobId ?? null,
      }),
    onSuccess: (job) => {
      toast.success(job.status === 'pending_review' ? '已提交，等待管理员审核' : '已提交并进入排队')
      navigate(`/jobs/${job.id}`)
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '提交失败'),
  })

  const problems = React.useMemo(() => {
    const out: string[] = []
    if (!name.trim()) out.push('请填写任务名称')
    if (!config.dataset_id && !config.tasks_path && !config.registry_dataset) out.push('请选择要评测的任务集')
    if (agentInfo?.needs_model && !config.model) out.push(`agent ${config.agent} 需要指定模型`)
    const missingKeys = Object.entries(config.agent_env)
      .filter(([key, value]) => agentInfo?.env_keys.includes(key) && !value)
      .map(([key]) => key)
    if (missingKeys.length) out.push(`请填写 ${missingKeys.join(' / ')}`)
    return out
  }, [name, config, agentInfo])

  return (
    <div className="space-y-4">
      <PageHeader
        title="提交评测任务"
        description="基于 Harbor framework 在沙箱中运行 agent 并按任务 verifier 打分"
        actions={<CompatDocButton defaultFramework="harbor" label="Harbor 说明" />}
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

      <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
        <div className="space-y-4">
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
              <DatasetPicker
                value={config.dataset_id ? [config.dataset_id] : []}
                onChange={(ids) => patch({ dataset_id: ids[0] ?? '' })}
                kinds={['harbor_tasks']}
                emptyHint="请在「数据集」页上传 harbor_tasks 类型的压缩包（tasks/<task>/task.toml）。"
              />
              <Advanced title="其他任务来源（高级）">
                <Grid cols={2}>
                  <Field label="服务器上的任务目录" hint="绝对路径，适合已经放在机器上的大任务集">
                    <Input
                      value={config.tasks_path}
                      onChange={(event) => patch({ tasks_path: event.target.value })}
                      placeholder="/workspace/LiHanlin_Workspace/datasets/PRT-Terminal-Bench-Data4k"
                    />
                  </Field>
                  <Field label="Harbor registry 数据集" hint="例如 terminal-bench@2.0，需要网络访问">
                    <Input
                      value={config.registry_dataset}
                      onChange={(event) => patch({ registry_dataset: event.target.value })}
                      placeholder="terminal-bench@2.0"
                    />
                  </Field>
                </Grid>
              </Advanced>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Agent 与模型" icon={<Bot className="h-4 w-4" aria-hidden />} />
            <CardBody className="space-y-4">
              <Field label="Agent" required hint={agentInfo?.description}>
                <Select
                  value={config.agent}
                  onValueChange={(value) => patch({ agent: value })}
                  options={(agents.data ?? []).map((agent) => ({
                    value: agent.name,
                    label: agent.recommended ? `${agent.name} ★` : agent.name,
                    description: agent.description || undefined,
                  }))}
                />
              </Field>

              {agentInfo?.needs_model && (
                <Field
                  label="模型"
                  required
                  hint="litellm 格式，例如 anthropic/claude-sonnet-4-5、openai/gpt-5.1、hosted_vllm/local-model"
                >
                  <Input
                    value={config.model}
                    onChange={(event) => patch({ model: event.target.value })}
                    placeholder="anthropic/claude-sonnet-4-5"
                  />
                </Field>
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
              {(presets.data?.verify_presets ?? []).length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {(presets.data?.verify_presets ?? []).map((preset) => (
                    <button
                      key={preset.id}
                      type="button"
                      onClick={() => applyPreset(preset)}
                      className="rounded-lg border border-border px-3 py-2 text-left transition-colors hover:border-primary hover:bg-primary-soft"
                    >
                      <div className="flex items-center gap-1.5 text-xs font-medium text-fg">
                        <Sparkles className="h-3 w-3 text-primary" aria-hidden />
                        {preset.label}
                      </div>
                      <p className="mt-0.5 max-w-64 text-[11px] leading-relaxed text-fg-subtle">{preset.description}</p>
                    </button>
                  ))}
                </div>
              )}

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
                  onCheckedChange={(checked) => patch({ disable_verification: checked })}
                />
                <ToggleRow
                  label="强制重建镜像"
                  hint="忽略已构建的任务镜像重新 build，较慢"
                  checked={config.force_build}
                  onCheckedChange={(checked) => patch({ force_build: checked })}
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
                <Field
                  label="Verifier 环境变量 (--verifier-env)"
                  hint="reward-kit / LLM judge 打分时需要的密钥写在这里；同时会写入 .env 供 task.toml 的 ${VAR} 插值使用"
                >
                  <KeyValueEditor
                    value={config.verifier_env}
                    onChange={(next) => patch({ verifier_env: next })}
                    keyPlaceholder="ANTHROPIC_API_KEY"
                    valuePlaceholder="sk-..."
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
                  <dt className="text-fg-muted">任务集</dt>
                  <dd className="truncate text-right text-fg">
                    {config.dataset_id ? '已选择' : config.tasks_path || config.registry_dataset || '未选择'}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-muted">模型</dt>
                  <dd className="truncate text-right text-fg">{config.model || '（无需模型）'}</dd>
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
