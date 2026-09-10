import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ChevronDown, Cpu, Database, Rocket, Server, Settings2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import type { AlgorithmInfo, DatasetKind, ModelEntry, RLConfig, SFTConfig, TrainStage } from '@/lib/types'
import { useProject } from '@/lib/store'
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
import { InlineHint, PageHeader, WarnHint } from '@/components/common'
import { ProjectBar } from '@/components/ProjectBar'
import { CompatDocButton } from '@/components/CompatDoc'
import { DatasetPicker, KeyValueEditor, ModelPicker } from '@/components/pickers'

/* -------------------------------------------------------------------------- */
/* Defaults (mirror the pydantic defaults so the payload is always complete)   */
/* -------------------------------------------------------------------------- */
const DEFAULT_SFT: SFTConfig = {
  framework: 'llamafactory',
  stage: 'sft',
  model_path: '',
  model_id: '',
  template: 'auto',
  trust_remote_code: true,
  dataset_ids: [],
  eval_dataset_ids: [],
  val_size: 0,
  finetuning_type: 'lora',
  lora: { rank: 8, alpha: 16, dropout: 0.05, target: 'all', create_new_adapter: false },
  freeze_trainable_layers: 2,
  cutoff_len: 2048,
  max_samples: null,
  learning_rate: 1e-4,
  num_train_epochs: 3,
  max_steps: null,
  per_device_train_batch_size: 1,
  gradient_accumulation_steps: 8,
  lr_scheduler_type: 'cosine',
  warmup_ratio: 0.1,
  weight_decay: 0,
  max_grad_norm: 1,
  logging_steps: 10,
  save_steps: 500,
  eval_steps: null,
  save_only_model: false,
  resume_from_checkpoint: null,
  compute_type: 'bf16',
  quantization_bit: null,
  flash_attn: 'auto',
  gradient_checkpointing: true,
  deepspeed_stage: 'none',
  liger_kernel: false,
  pref_beta: 0.1,
  pref_loss: 'sigmoid',
  pref_ftx: 0,
  report_to: 'none',
  plot_loss: true,
  extra_args: {},
}

const DEFAULT_RL: RLConfig = {
  framework: 'verl',
  algorithm: 'grpo',
  model_path: '',
  model_id: '',
  dataset_id: '',
  eval_dataset_id: '',
  max_prompt_length: 1024,
  max_response_length: 2048,
  train_batch_size: 64,
  mini_batch_size: 32,
  micro_batch_size_per_gpu: 1,
  prompt_key: 'prompt',
  truncation: 'error',
  filter_overlong_prompts: true,
  learning_rate: 1e-6,
  total_epochs: 1,
  total_training_steps: null,
  warmup_ratio: 0,
  weight_decay: 0.01,
  clip_ratio: 0.2,
  clip_ratio_low: 0.2,
  clip_ratio_high: 0.28,
  loss_agg_mode: 'token-mean',
  entropy_coeff: 0,
  use_kl_loss: false,
  kl_loss_coef: 0.001,
  use_kl_in_reward: false,
  norm_adv_by_std_in_grpo: true,
  filter_groups_enable: false,
  filter_groups_metric: 'acc',
  filter_groups_max_num_gen_batches: 10,
  enable_overlong_buffer: false,
  overlong_buffer_len: 512,
  overlong_penalty_factor: 1,
  rollout: {
    backend: 'vllm',
    n: 8,
    temperature: 1,
    top_p: 1,
    top_k: -1,
    gpu_memory_utilization: 0.6,
    tensor_model_parallel_size: 1,
    max_num_batched_tokens: 8192,
  },
  harbor: {
    enabled: false,
    harness: 'bash',
    environment_type: 'docker',
    num_tasks: 64,
    max_tool_calling_iterations: 25,
    include_metadata: true,
  },
  attn_implementation: 'auto',
  strategy: 'fsdp2',
  param_offload: false,
  optimizer_offload: false,
  use_dynamic_bsz: true,
  gradient_checkpointing: true,
  reward_fn_key: 'data_source',
  custom_reward_function_path: '',
  custom_reward_function_name: '',
  save_freq: 20,
  test_freq: 20,
  val_before_train: false,
  logger: ['console'],
  extra_args: {},
}

/* -------------------------------------------------------------------------- */
function Section({
  title,
  description,
  icon,
  children,
  actions,
}: {
  title: string
  description?: string
  icon?: React.ReactNode
  children: React.ReactNode
  actions?: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader title={title} description={description} icon={icon} actions={actions} />
      <CardBody className="space-y-4">{children}</CardBody>
    </Card>
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

const Grid = ({ children, cols = 3 }: { children: React.ReactNode; cols?: 2 | 3 | 4 }) => (
  <div
    className={cn(
      'grid gap-3',
      cols === 2 && 'sm:grid-cols-2',
      cols === 3 && 'sm:grid-cols-2 lg:grid-cols-3',
      cols === 4 && 'sm:grid-cols-2 lg:grid-cols-4',
    )}
  >
    {children}
  </div>
)

/* -------------------------------------------------------------------------- */
export default function SubmitTraining() {
  const navigate = useNavigate()
  const { projectId } = useProject()

  const algorithms = useQuery({ queryKey: ['algorithms'], queryFn: () => api.algorithms() })
  const templates = useQuery({ queryKey: ['templates'], queryFn: () => api.templates() })
  const info = useQuery({ queryKey: ['info'], queryFn: () => api.info() })
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.projects() })
  const projectName = projects.data?.find((row) => row.project.id === projectId)?.project.name

  const [name, setName] = React.useState('')
  const [note, setNote] = React.useState('')
  const [tags, setTags] = React.useState('')
  const [algorithm, setAlgorithm] = React.useState<TrainStage>('sft')
  const [nGpus, setNGpus] = React.useState(1)
  const [priority, setPriority] = React.useState(0)
  const [envVars, setEnvVars] = React.useState<Record<string, string>>({})
  const [sft, setSft] = React.useState<SFTConfig>(DEFAULT_SFT)
  const [rl, setRl] = React.useState<RLConfig>(DEFAULT_RL)
  const [selectedModel, setSelectedModel] = React.useState<ModelEntry | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  const trainingAlgorithms = (algorithms.data ?? []).filter((item) => item.job_type !== 'verify')
  const current: AlgorithmInfo | undefined = trainingAlgorithms.find((item) => item.id === algorithm)
  const jobType = current?.job_type ?? 'sft'
  const isRl = jobType === 'rl'

  // Keep the algorithm in sync with the per-type config blocks.
  React.useEffect(() => {
    if (isRl) {
      setRl((previous) => {
        const nextFramework = current?.frameworks.includes(previous.framework)
          ? previous.framework
          : ((current?.frameworks[0] as RLConfig['framework']) ?? 'verl')
        return { ...previous, algorithm, framework: nextFramework }
      })
    } else {
      setSft((previous) => ({ ...previous, stage: algorithm }))
    }
  }, [algorithm, isRl, current])

  const datasetKinds: DatasetKind[] = (current?.dataset_kinds ?? ['sft_alpaca']) as DatasetKind[]

  const applyModel = (model: ModelEntry | null) => {
    setSelectedModel(model)
    if (isRl) {
      setRl((previous) => ({ ...previous, model_id: model?.id ?? '', model_path: model?.path ?? '' }))
    } else {
      setSft((previous) => ({
        ...previous,
        model_id: model?.id ?? '',
        model_path: model?.path ?? '',
        template: model?.llamafactory_template ?? previous.template,
      }))
    }
  }

  const submit = useMutation({
    mutationFn: () => {
      const payload = {
        name: name.trim(),
        note,
        type: jobType,
        project_id: projectId,
        tags: tags.split(',').map((tag) => tag.trim()).filter(Boolean),
        resources: { n_gpus: nGpus, priority, env: cleanKv(envVars) },
        sft: isRl ? null : sft,
        rl: isRl ? rl : null,
      }
      return api.createJob(payload)
    },
    onSuccess: (job) => {
      toast.success(job.status === 'pending_review' ? '已提交，等待管理员审核' : '已提交并进入排队')
      navigate(`/jobs/${job.id}`)
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '提交失败'),
  })

  const validation = React.useMemo(() => {
    const problems: string[] = []
    if (!name.trim()) problems.push('请填写任务名称')
    if (isRl) {
      if (!rl.model_id && !rl.model_path) problems.push('请选择基础模型')
      if (!rl.dataset_id) problems.push('请选择训练数据集')
      if (rl.rollout.tensor_model_parallel_size > nGpus) problems.push('rollout 张量并行度不能大于申请的 GPU 数')
    } else {
      if (!sft.model_id && !sft.model_path) problems.push('请选择基础模型')
      if (!sft.dataset_ids.length) problems.push('请至少选择一个训练数据集')
    }
    return problems
  }, [name, isRl, rl, sft, nGpus])

  return (
    <div className="space-y-4">
      <PageHeader
        title="提交训练任务"
        description="SFT / 偏好对齐 / 强化学习，平台会按框架生成命令并在审核通过后自动排队执行"
        actions={<CompatDocButton defaultFramework={isRl ? (rl.framework as 'verl') : 'llamafactory'} />}
      />

      <ProjectBar />

      <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
        <div className="space-y-4">
          {/* --------------------------------------------------- basics -- */}
          <Section title="基本信息" icon={<Rocket className="h-4 w-4" aria-hidden />}>
            <Grid cols={2}>
              <Field label="任务名称" required htmlFor="job-name" hint="用于在列表与大屏中识别，建议包含模型与数据集">
                <Input
                  id="job-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="例如：qwen3-4b-sft-terminal-v1"
                />
              </Field>
              <Field label="标签" htmlFor="job-tags" hint="逗号分隔，便于筛选，例如 baseline,v1">
                <Input id="job-tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="baseline,v1" />
              </Field>
            </Grid>
            <Field label="备注" htmlFor="job-note" hint="写清楚这次训练想验证什么，审核人会看到">
              <Textarea
                id="job-note"
                rows={2}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="例如：在 4k 终端任务上做 LoRA SFT，验证 cutoff 提到 4096 后是否收敛更好。"
              />
            </Field>

            <Field label="训练算法" required hint={current?.summary}>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {trainingAlgorithms.map((item) => {
                  const selected = item.id === algorithm
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setAlgorithm(item.id as TrainStage)}
                      aria-pressed={selected}
                      className={cn(
                        'rounded-lg border px-3 py-2 text-left transition-colors',
                        selected ? 'border-primary bg-primary-soft' : 'border-border hover:bg-card-hover',
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className={cn('text-sm font-medium', selected && 'text-primary')}>{item.label}</span>
                        <Badge tone={item.job_type === 'rl' ? 'info' : 'primary'}>
                          {item.job_type === 'rl' ? 'RL' : '训练'}
                        </Badge>
                      </div>
                      <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-fg-subtle">{item.summary}</p>
                    </button>
                  )
                })}
              </div>
            </Field>

            {current && <InlineHint>{current.doc}</InlineHint>}

            {isRl && (
              <Field label="RL 框架" required hint="Harbor 任务树只能用 TRL 做 agentic RL；parquet 数据两者都支持">
                <div className="flex gap-2">
                  {(current?.frameworks ?? []).map((framework) => (
                    <button
                      key={framework}
                      type="button"
                      onClick={() => setRl((previous) => ({ ...previous, framework: framework as RLConfig['framework'] }))}
                      aria-pressed={rl.framework === framework}
                      className={cn(
                        'flex-1 rounded-lg border px-3 py-2 text-sm transition-colors',
                        rl.framework === framework
                          ? 'border-primary bg-primary-soft text-primary'
                          : 'border-border hover:bg-card-hover',
                      )}
                    >
                      {framework === 'verl' ? 'verl（大规模 / Megatron）' : 'TRL（HF 生态 / Harbor 环境）'}
                    </button>
                  ))}
                </div>
              </Field>
            )}
          </Section>

          {/* ---------------------------------------------------- model -- */}
          <Section
            title="基础模型"
            description="从模型仓库中选择；未收录的模型可以在「模型仓库」页从 ModelScope 下载"
            icon={<Server className="h-4 w-4" aria-hidden />}
            actions={
              <Link to="/models">
                <Button variant="ghost" size="sm">
                  去模型仓库
                </Button>
              </Link>
            }
          >
            <ModelPicker
              value={isRl ? rl.model_id : sft.model_id}
              onChange={applyModel}
              framework={isRl ? rl.framework : 'llamafactory'}
              onRequestDownload={() => navigate('/models?download=1')}
            />
            {selectedModel && !isRl && (
              <Grid cols={2}>
                <Field
                  label="对话模板 (template)"
                  hint={
                    selectedModel.llamafactory_template
                      ? '已根据模型自动匹配，确认无误即可'
                      : '该模型未在 LLaMA-Factory 模型表中，请手动选择'
                  }
                >
                  <Select
                    value={sft.template}
                    onValueChange={(value) => setSft((previous) => ({ ...previous, template: value }))}
                    options={[
                      { value: 'auto', label: 'auto（由平台推断）' },
                      ...(templates.data ?? []).map((template) => ({ value: template, label: template })),
                    ]}
                  />
                </Field>
                <Field label="信任远端代码 (trust_remote_code)" hint="自定义架构的模型通常需要开启">
                  <ToggleRow
                    label="trust_remote_code"
                    checked={sft.trust_remote_code}
                    onCheckedChange={(checked) => setSft((previous) => ({ ...previous, trust_remote_code: checked }))}
                  />
                </Field>
              </Grid>
            )}
            {selectedModel?.notes.length ? <WarnHint>{selectedModel.notes.join('；')}</WarnHint> : null}
          </Section>

          {/* -------------------------------------------------- dataset -- */}
          <Section
            title="数据集"
            description={`当前算法可用类型：${datasetKinds.join(' / ')}`}
            icon={<Database className="h-4 w-4" aria-hidden />}
            actions={
              <Link to="/datasets">
                <Button variant="ghost" size="sm">
                  上传数据集
                </Button>
              </Link>
            }
          >
            {isRl ? (
              <>
                <Field label="训练数据集" required>
                  <DatasetPicker
                    value={rl.dataset_id ? [rl.dataset_id] : []}
                    onChange={(ids) => {
                      const id = ids[0] ?? ''
                      setRl((previous) => ({ ...previous, dataset_id: id }))
                    }}
                    kinds={datasetKinds}
                  />
                </Field>
                <HarborDatasetHint rl={rl} setRl={setRl} />
              </>
            ) : (
              <>
                <Field label="训练数据集" required hint="可多选，平台会自动合并生成 LLaMA-Factory dataset_info.json">
                  <DatasetPicker
                    value={sft.dataset_ids}
                    onChange={(ids) => setSft((previous) => ({ ...previous, dataset_ids: ids }))}
                    kinds={datasetKinds}
                    multiple
                  />
                </Field>
                <Advanced title="验证集设置（可选）">
                  <Field label="独立验证集" hint="不选则可用下面的比例从训练集中切分">
                    <DatasetPicker
                      value={sft.eval_dataset_ids}
                      onChange={(ids) => setSft((previous) => ({ ...previous, eval_dataset_ids: ids }))}
                      kinds={datasetKinds}
                      multiple
                    />
                  </Field>
                  <Grid cols={2}>
                    <Field label="切分比例 val_size" hint="0 表示不切分；0.1 表示 10% 作为验证集">
                      <NumberInput
                        value={sft.val_size}
                        step="0.01"
                        min={0}
                        max={0.9}
                        onChange={(value) => setSft((previous) => ({ ...previous, val_size: value ?? 0 }))}
                      />
                    </Field>
                    <Field label="评估间隔 eval_steps">
                      <NumberInput
                        value={sft.eval_steps}
                        onChange={(value) => setSft((previous) => ({ ...previous, eval_steps: value }))}
                        placeholder="默认与 save_steps 相同"
                      />
                    </Field>
                  </Grid>
                </Advanced>
              </>
            )}
          </Section>

          {/* ----------------------------------------------- parameters -- */}
          <Section
            title="训练参数"
            description="默认值已按常见配置给好，按需修改即可"
            icon={<Settings2 className="h-4 w-4" aria-hidden />}
          >
            {isRl ? <RlParams rl={rl} setRl={setRl} /> : <SftParams sft={sft} setSft={setSft} />}
          </Section>

          {/* ------------------------------------------------- resources -- */}
          <Section title="资源与调度" icon={<Cpu className="h-4 w-4" aria-hidden />}>
            <Grid cols={3}>
              <Field
                label="GPU 数量"
                required
                hint={info.data ? `本机共 ${info.data.gpu_total} 张卡；0 表示纯 CPU 任务` : undefined}
              >
                <NumberInput
                  value={nGpus}
                  min={0}
                  max={info.data?.gpu_total ?? 8}
                  onChange={(value) => setNGpus(Math.max(0, value ?? 0))}
                />
              </Field>
              <Field label="优先级" hint="-10 ~ 10，数值越大越先出队">
                <NumberInput value={priority} min={-10} max={10} onChange={(value) => setPriority(value ?? 0)} />
              </Field>
            </Grid>
            <Advanced title="附加环境变量（可选）">
              <KeyValueEditor
                value={envVars}
                onChange={setEnvVars}
                keyPlaceholder="WANDB_API_KEY"
                valuePlaceholder="值"
              />
            </Advanced>
          </Section>
        </div>

        {/* ------------------------------------------------------ summary -- */}
        <aside className="xl:sticky xl:top-20 xl:self-start">
          <Card>
            <CardHeader title="提交摘要" />
            <CardBody className="space-y-3">
              <dl className="space-y-1.5 text-xs">
                <SummaryRow label="项目" value={projectName ?? '未选择'} />
                <SummaryRow label="任务类型" value={isRl ? '强化学习' : '监督 / 偏好训练'} />
                <SummaryRow label="算法" value={current?.label ?? algorithm} />
                <SummaryRow label="框架" value={isRl ? rl.framework : 'LLaMA-Factory'} />
                <SummaryRow label="模型" value={(isRl ? rl.model_id : sft.model_id) || '未选择'} />
                <SummaryRow
                  label="数据集"
                  value={isRl ? (rl.dataset_id ? '已选择 1 个' : '未选择') : `${sft.dataset_ids.length} 个`}
                />
                <SummaryRow label="GPU" value={`${nGpus} 张`} />
                {isRl ? (
                  <>
                    <SummaryRow label="rollout n" value={String(rl.rollout.n)} />
                    <SummaryRow label="学习率" value={String(rl.learning_rate)} />
                  </>
                ) : (
                  <>
                    <SummaryRow label="微调方式" value={sft.finetuning_type} />
                    <SummaryRow label="学习率" value={String(sft.learning_rate)} />
                  </>
                )}
              </dl>

              {validation.length > 0 && (
                <ul className="space-y-1 rounded-lg border border-warning/30 bg-warning-soft px-2.5 py-2">
                  {validation.map((problem) => (
                    <li key={problem} className="text-[11px] text-warning">
                      · {problem}
                    </li>
                  ))}
                </ul>
              )}

              {error && <ErrorBox message={error} />}

              <Button
                variant="primary"
                size="lg"
                className="w-full justify-center"
                disabled={validation.length > 0}
                loading={submit.isPending}
                onClick={() => {
                  setError(null)
                  submit.mutate()
                }}
              >
                提交任务
              </Button>
              <p className="text-[11px] leading-relaxed text-fg-subtle">
                提交后进入「待审核」，管理员通过后才会占用 GPU 开始训练。审核人可以在任务详情里查看平台生成的完整命令。
              </p>
            </CardBody>
          </Card>
        </aside>
      </div>
    </div>
  )
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="shrink-0 text-fg-muted">{label}</dt>
      <dd className="min-w-0 truncate text-right text-fg">{value}</dd>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* 注意力加速：取值对应 LLaMA-Factory 的 AttentionFunction 枚举                 */
/* -------------------------------------------------------------------------- */
const FLASH_ATTN_OPTIONS = [
  { value: 'auto', label: 'auto（自动探测）', description: '装了 flash-attn 就用 FA2，否则回落' },
  { value: 'fa2', label: 'FlashAttention-2', description: '需要 flash-attn，长序列提速最明显' },
  { value: 'fa3', label: 'FlashAttention-3', description: '仅 Hopper (H100/H800) 及更新架构' },
  { value: 'sdpa', label: 'SDPA', description: 'PyTorch 自带，无需额外依赖' },
  { value: 'disabled', label: '关闭（eager）', description: '最慢，调试用' },
]

const FLASH_ATTN_HINT: Record<SFTConfig['flash_attn'], string> = {
  auto: '让框架自己选：有 flash-attn 用 FA2，没有则退回 SDPA',
  fa2: 'FlashAttention-2：显存更省、长上下文更快；环境里没装 flash-attn 会启动失败',
  fa3: 'FlashAttention-3 只支持 Hopper 及更新的卡，老卡请选 FA2',
  sdpa: 'PyTorch 原生高效实现，不需要 flash-attn，兼容性最好',
  disabled: '退回 eager 实现，只在排查数值问题时用',
}

function SftParams({ sft, setSft }: { sft: SFTConfig; setSft: React.Dispatch<React.SetStateAction<SFTConfig>> }) {
  const patch = (values: Partial<SFTConfig>) => setSft((previous) => ({ ...previous, ...values }))
  const isPreference = ['dpo', 'orpo', 'simpo', 'kto'].includes(sft.stage)
  // 环境里没装 flash-attn 却选了 fa2/fa3，会在加载模型时才失败——提交前就说清楚
  const toolchain = useQuery({ queryKey: ['toolchain'], queryFn: () => api.toolchain() })
  const noFlashAttn = toolchain.data?.find((row) => row.key === 'llamafactory')?.flash_attn === false
  const flashAttnWarning =
    noFlashAttn && (sft.flash_attn === 'fa2' || sft.flash_attn === 'fa3')
      ? 'LLaMA-Factory 环境里没有装 flash-attn，该选项会在加载模型时失败；请改用 sdpa 或 auto'
      : null

  return (
    <div className="space-y-4">
      <Grid cols={3}>
        <Field label="微调方式" hint="LoRA 显存友好；full 需要多卡 + ZeRO">
          <Select
            value={sft.finetuning_type}
            onValueChange={(value) => patch({ finetuning_type: value as SFTConfig['finetuning_type'] })}
            options={[
              { value: 'lora', label: 'LoRA', description: '只训练低秩适配器' },
              { value: 'full', label: '全参数', description: '显存占用最高' },
              { value: 'freeze', label: '冻结部分层', description: '只训练最后若干层' },
            ]}
          />
        </Field>
        <Field label="学习率" hint={isPreference ? '偏好对齐建议 5e-6 量级' : 'LoRA 1e-4，全参 1e-5'}>
          <NumberInput value={sft.learning_rate} onChange={(value) => patch({ learning_rate: value ?? 0 })} />
        </Field>
        <Field label="训练轮数 epochs">
          <NumberInput value={sft.num_train_epochs} step="0.5" onChange={(value) => patch({ num_train_epochs: value ?? 1 })} />
        </Field>
        <Field label="序列长度 cutoff_len" hint="超过部分会按 truncation 策略截断">
          <NumberInput value={sft.cutoff_len} onChange={(value) => patch({ cutoff_len: value ?? 2048 })} />
        </Field>
        <Field label="单卡 batch size">
          <NumberInput
            value={sft.per_device_train_batch_size}
            min={1}
            onChange={(value) => patch({ per_device_train_batch_size: value ?? 1 })}
          />
        </Field>
        <Field label="梯度累积" hint={`等效 batch = ${sft.per_device_train_batch_size * sft.gradient_accumulation_steps} × GPU 数`}>
          <NumberInput
            value={sft.gradient_accumulation_steps}
            min={1}
            onChange={(value) => patch({ gradient_accumulation_steps: value ?? 1 })}
          />
        </Field>
        <Field
          label="加速 flash_attn"
          hint={FLASH_ATTN_HINT[sft.flash_attn]}
          error={flashAttnWarning}
          className="sm:col-span-2 lg:col-span-1"
        >
          <Select
            value={sft.flash_attn}
            onValueChange={(value) => patch({ flash_attn: value as SFTConfig['flash_attn'] })}
            options={FLASH_ATTN_OPTIONS}
          />
        </Field>
      </Grid>

      {sft.finetuning_type === 'lora' && (
        <Grid cols={4}>
          <Field label="LoRA rank">
            <NumberInput
              value={sft.lora.rank}
              min={1}
              onChange={(value) => setSft((p) => ({ ...p, lora: { ...p.lora, rank: value ?? 8 } }))}
            />
          </Field>
          <Field label="LoRA alpha" hint="经验值：alpha = 2 × rank">
            <NumberInput
              value={sft.lora.alpha}
              min={1}
              onChange={(value) => setSft((p) => ({ ...p, lora: { ...p.lora, alpha: value ?? 16 } }))}
            />
          </Field>
          <Field label="LoRA dropout">
            <NumberInput
              value={sft.lora.dropout}
              step="0.01"
              onChange={(value) => setSft((p) => ({ ...p, lora: { ...p.lora, dropout: value ?? 0 } }))}
            />
          </Field>
          <Field label="作用模块 lora_target" hint="all 表示所有线性层">
            <Input
              value={sft.lora.target}
              onChange={(event) => setSft((p) => ({ ...p, lora: { ...p.lora, target: event.target.value } }))}
            />
          </Field>
        </Grid>
      )}

      {isPreference && (
        <Grid cols={3}>
          <Field label="pref_beta" hint="越大越保守，DPO 常用 0.1，SimPO 常用 2.0">
            <NumberInput value={sft.pref_beta} step="0.05" onChange={(value) => patch({ pref_beta: value ?? 0.1 })} />
          </Field>
          <Field label="损失类型 pref_loss">
            <Select
              value={sft.pref_loss}
              onValueChange={(value) => patch({ pref_loss: value })}
              options={[
                { value: 'sigmoid', label: 'sigmoid（标准 DPO）' },
                { value: 'hinge', label: 'hinge' },
                { value: 'ipo', label: 'ipo' },
                { value: 'orpo', label: 'orpo' },
                { value: 'simpo', label: 'simpo' },
              ]}
            />
          </Field>
          <Field label="pref_ftx" hint="混合 SFT 损失的权重，0 表示纯偏好损失">
            <NumberInput value={sft.pref_ftx} step="0.1" onChange={(value) => patch({ pref_ftx: value ?? 0 })} />
          </Field>
        </Grid>
      )}

      <Advanced title="进阶：调度、精度与显存">
        <Grid cols={3}>
          <Field label="学习率调度">
            <Select
              value={sft.lr_scheduler_type}
              onValueChange={(value) => patch({ lr_scheduler_type: value })}
              options={['cosine', 'linear', 'constant', 'constant_with_warmup', 'polynomial'].map((item) => ({
                value: item,
                label: item,
              }))}
            />
          </Field>
          <Field label="warmup 比例">
            <NumberInput value={sft.warmup_ratio} step="0.01" onChange={(value) => patch({ warmup_ratio: value ?? 0 })} />
          </Field>
          <Field label="weight decay">
            <NumberInput value={sft.weight_decay} step="0.01" onChange={(value) => patch({ weight_decay: value ?? 0 })} />
          </Field>
          <Field label="计算精度">
            <Select
              value={sft.compute_type}
              onValueChange={(value) => patch({ compute_type: value as SFTConfig['compute_type'] })}
              options={[
                { value: 'bf16', label: 'bf16（推荐）' },
                { value: 'fp16', label: 'fp16' },
                { value: 'pure_bf16', label: 'pure_bf16' },
                { value: 'fp32', label: 'fp32' },
              ]}
            />
          </Field>
          <Field label="DeepSpeed" hint="多卡全参训练建议 ZeRO-3">
            <Select
              value={sft.deepspeed_stage}
              onValueChange={(value) => patch({ deepspeed_stage: value as SFTConfig['deepspeed_stage'] })}
              options={[
                { value: 'none', label: '不使用' },
                { value: 'zero2', label: 'ZeRO-2' },
                { value: 'zero3', label: 'ZeRO-3' },
                { value: 'zero3_offload', label: 'ZeRO-3 + Offload' },
              ]}
            />
          </Field>
          <Field label="量化 (QLoRA)" hint="4 / 8 bit，仅 LoRA 有效">
            <Select
              value={String(sft.quantization_bit ?? '')}
              onValueChange={(value) => patch({ quantization_bit: value ? Number(value) : null })}
              options={[
                { value: '', label: '不量化' },
                { value: '8', label: '8 bit' },
                { value: '4', label: '4 bit (QLoRA)' },
              ]}
            />
          </Field>
          <Field label="日志间隔 logging_steps">
            <NumberInput value={sft.logging_steps} min={1} onChange={(value) => patch({ logging_steps: value ?? 10 })} />
          </Field>
          <Field label="保存间隔 save_steps">
            <NumberInput value={sft.save_steps} min={1} onChange={(value) => patch({ save_steps: value ?? 500 })} />
          </Field>
          <Field label="最大样本数 max_samples" hint="留空表示使用全部数据">
            <NumberInput value={sft.max_samples} onChange={(value) => patch({ max_samples: value })} />
          </Field>
          <Field label="最大步数 max_steps" hint="设置后会覆盖 epochs">
            <NumberInput value={sft.max_steps} onChange={(value) => patch({ max_steps: value })} />
          </Field>
          <Field label="实验跟踪 report_to">
            <Select
              value={sft.report_to}
              onValueChange={(value) => patch({ report_to: value })}
              options={['none', 'wandb', 'tensorboard', 'swanlab', 'mlflow'].map((item) => ({ value: item, label: item }))}
            />
          </Field>
        </Grid>
        <div className="grid gap-2 sm:grid-cols-2">
          <ToggleRow
            label="梯度检查点"
            hint="省显存、慢约 20%"
            checked={sft.gradient_checkpointing}
            onCheckedChange={(checked) => patch({ gradient_checkpointing: checked })}
          />
          <ToggleRow
            label="Liger Kernel"
            hint="部分模型可提速并省显存"
            checked={sft.liger_kernel}
            onCheckedChange={(checked) => patch({ liger_kernel: checked })}
          />
          <ToggleRow
            label="只保存模型权重"
            hint="不保存优化器状态，checkpoint 更小但无法续训"
            checked={sft.save_only_model}
            onCheckedChange={(checked) => patch({ save_only_model: checked })}
          />
          <ToggleRow
            label="绘制 loss 曲线"
            checked={sft.plot_loss}
            onCheckedChange={(checked) => patch({ plot_loss: checked })}
          />
        </div>
      </Advanced>

      <Advanced title="进阶：透传给 LLaMA-Factory 的额外参数">
        <KeyValueEditor
          value={Object.fromEntries(Object.entries(sft.extra_args).map(([key, value]) => [key, String(value)]))}
          onChange={(next) => setSft((previous) => ({ ...previous, extra_args: next }))}
          keyPlaceholder="例如 neat_packing"
          valuePlaceholder="true"
          secretKeys={false}
        />
        <p className="text-[11px] text-fg-subtle">这些键值会直接写入 train_config.yaml，并覆盖上面表单生成的同名字段。</p>
      </Advanced>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* verl 走 model.override_config.attn_implementation，TRL 走 model_init_kwargs   */
/* -------------------------------------------------------------------------- */
const RL_ATTN_OPTIONS = [
  {
    value: 'auto',
    label: 'auto（推荐）',
    description: '装了 flash-attn 就用 FA2，否则回落到 SDPA',
  },
  {
    value: 'flash_attention_2',
    label: 'FlashAttention-2',
    description: '需要环境里装了 flash-attn',
  },
  { value: 'sdpa', label: 'SDPA', description: 'PyTorch 自带，无需额外依赖' },
  { value: 'eager', label: 'eager', description: '最慢，调试用' },
  { value: 'default', label: '不指定', description: '交给框架自己的默认值' },
]

const RL_ATTN_HINT: Record<RLConfig['attn_implementation'], string> = {
  auto: '提交时探测目标环境：有 flash-attn 用 FA2，没有则回落到 SDPA',
  flash_attention_2: 'FlashAttention-2：长 response 的 RL 提速最明显；环境里没装 flash-attn 会在加载模型时失败',
  sdpa: 'PyTorch 原生高效实现，兼容性最好',
  eager: '退回逐步实现，只在排查数值问题时用',
  default: '平台不下发这个参数，用框架自己的默认值',
}

function RlParams({ rl, setRl }: { rl: RLConfig; setRl: React.Dispatch<React.SetStateAction<RLConfig>> }) {
  const patch = (values: Partial<RLConfig>) => setRl((previous) => ({ ...previous, ...values }))
  const toolchain = useQuery({ queryKey: ['toolchain'], queryFn: () => api.toolchain() })
  const noFlashAttn = toolchain.data?.find((row) => row.key === rl.framework)?.flash_attn === false
  const attnWarning =
    noFlashAttn && rl.attn_implementation === 'flash_attention_2'
      ? `${rl.framework} 环境里没有装 flash-attn，该选项会在加载模型时失败；请改用 auto 或 sdpa`
      : null
  const patchRollout = (values: Partial<RLConfig['rollout']>) =>
    setRl((previous) => ({ ...previous, rollout: { ...previous.rollout, ...values } }))
  const isGroupAlgo = ['grpo', 'dapo', 'gspo'].includes(rl.algorithm)

  return (
    <div className="space-y-4">
      <Grid cols={3}>
        <Field label="学习率" hint="RL 学习率远小于 SFT，常用 1e-6">
          <NumberInput value={rl.learning_rate} onChange={(value) => patch({ learning_rate: value ?? 1e-6 })} />
        </Field>
        <Field label="训练 batch size" hint="每次迭代采样的 prompt 数">
          <NumberInput value={rl.train_batch_size} min={1} onChange={(value) => patch({ train_batch_size: value ?? 64 })} />
        </Field>
        <Field label="mini batch size" hint="每次参数更新使用的样本数">
          <NumberInput value={rl.mini_batch_size} min={1} onChange={(value) => patch({ mini_batch_size: value ?? 32 })} />
        </Field>
        <Field label="prompt 最大长度">
          <NumberInput value={rl.max_prompt_length} min={1} onChange={(value) => patch({ max_prompt_length: value ?? 1024 })} />
        </Field>
        <Field label="response 最大长度" hint="长 CoT 任务建议 8192 以上">
          <NumberInput
            value={rl.max_response_length}
            min={1}
            onChange={(value) => patch({ max_response_length: value ?? 2048 })}
          />
        </Field>
        <Field label="训练轮数 epochs">
          <NumberInput value={rl.total_epochs} min={1} onChange={(value) => patch({ total_epochs: value ?? 1 })} />
        </Field>
        <Field
          label="加速 attn_implementation"
          hint={
            rl.framework === 'verl'
              ? RL_ATTN_HINT[rl.attn_implementation]
              : `${RL_ATTN_HINT[rl.attn_implementation]}（TRL 通过 model_init_kwargs 透传）`
          }
          error={attnWarning}
        >
          <Select
            value={rl.attn_implementation}
            onValueChange={(value) => patch({ attn_implementation: value as RLConfig['attn_implementation'] })}
            options={RL_ATTN_OPTIONS}
          />
        </Field>
      </Grid>

      <div>
        <h4 className="mb-2 text-xs font-semibold text-fg-muted">采样 (rollout)</h4>
        <Grid cols={3}>
          <Field label="推理后端">
            <Select
              value={rl.rollout.backend}
              onValueChange={(value) => patchRollout({ backend: value as RLConfig['rollout']['backend'] })}
              options={[
                { value: 'vllm', label: 'vLLM（推荐）' },
                { value: 'sglang', label: 'SGLang' },
                { value: 'hf', label: 'HuggingFace generate' },
              ]}
            />
          </Field>
          <Field
            label={isGroupAlgo ? '组大小 n' : '采样数 n'}
            hint={isGroupAlgo ? '同一 prompt 的采样条数，组相对算法建议 8~16' : undefined}
          >
            <NumberInput value={rl.rollout.n} min={1} onChange={(value) => patchRollout({ n: value ?? 8 })} />
          </Field>
          <Field label="温度">
            <NumberInput value={rl.rollout.temperature} step="0.05" onChange={(value) => patchRollout({ temperature: value ?? 1 })} />
          </Field>
          <Field label="显存占用比例" hint="留给推理引擎的显存，训练显存紧张时调低">
            <NumberInput
              value={rl.rollout.gpu_memory_utilization}
              step="0.05"
              min={0.1}
              max={0.95}
              onChange={(value) => patchRollout({ gpu_memory_utilization: value ?? 0.6 })}
            />
          </Field>
          <Field label="张量并行度" hint="不能大于申请的 GPU 数">
            <NumberInput
              value={rl.rollout.tensor_model_parallel_size}
              min={1}
              onChange={(value) => patchRollout({ tensor_model_parallel_size: value ?? 1 })}
            />
          </Field>
          <Field label="top_p">
            <NumberInput value={rl.rollout.top_p} step="0.05" min={0} max={1} onChange={(value) => patchRollout({ top_p: value ?? 1 })} />
          </Field>
        </Grid>
      </div>

      {rl.harbor.enabled && (
        <div>
          <h4 className="mb-2 text-xs font-semibold text-fg-muted">Harbor 环境（agentic RL）</h4>
          <Grid cols={3}>
            <Field label="Harness" hint="agent 可用的工具面">
              <Select
                value={rl.harbor.harness}
                onValueChange={(value) => setRl((p) => ({ ...p, harbor: { ...p.harbor, harness: value } }))}
                options={[
                  { value: 'bash', label: 'bash', description: '单一 shell 工具（内置）' },
                  {
                    value: 'examples/scripts/harbor/harnesses/jupyter/env.py:JupyterEnv',
                    label: 'jupyter',
                    description: '有状态 Python 内核',
                  },
                  {
                    value: 'examples/scripts/harbor/harnesses/terminal_notes/env.py:TerminalNotesEnv',
                    label: 'terminal_notes',
                    description: 'shell + 持久笔记工具',
                  },
                ]}
              />
            </Field>
            <Field label="沙箱后端" hint="docker 需要本机 Docker 守护进程">
              <Select
                value={rl.harbor.environment_type}
                onValueChange={(value) => setRl((p) => ({ ...p, harbor: { ...p.harbor, environment_type: value } }))}
                options={['docker', 'e2b', 'daytona', 'modal', 'runloop'].map((item) => ({ value: item, label: item }))}
              />
            </Field>
            <Field label="使用任务数" hint="留空表示使用任务树中的全部任务">
              <NumberInput
                value={rl.harbor.num_tasks}
                onChange={(value) => setRl((p) => ({ ...p, harbor: { ...p.harbor, num_tasks: value } }))}
              />
            </Field>
            <Field label="最大工具调用轮数">
              <NumberInput
                value={rl.harbor.max_tool_calling_iterations}
                min={1}
                onChange={(value) =>
                  setRl((p) => ({ ...p, harbor: { ...p.harbor, max_tool_calling_iterations: value ?? 25 } }))
                }
              />
            </Field>
          </Grid>
        </div>
      )}

      <Advanced title="进阶：策略优化细节">
        <Grid cols={3}>
          {isGroupAlgo ? (
            <>
              <Field label="clip ratio low" hint="DAPO 解耦裁剪的下界">
                <NumberInput value={rl.clip_ratio_low} step="0.01" onChange={(value) => patch({ clip_ratio_low: value ?? 0.2 })} />
              </Field>
              <Field label="clip ratio high" hint="上界放大可提升探索（DAPO 论文用 0.28）">
                <NumberInput value={rl.clip_ratio_high} step="0.01" onChange={(value) => patch({ clip_ratio_high: value ?? 0.28 })} />
              </Field>
            </>
          ) : (
            <Field label="clip ratio">
              <NumberInput value={rl.clip_ratio} step="0.01" onChange={(value) => patch({ clip_ratio: value ?? 0.2 })} />
            </Field>
          )}
          <Field label="损失聚合方式">
            <Select
              value={rl.loss_agg_mode}
              onValueChange={(value) => patch({ loss_agg_mode: value })}
              options={[
                { value: 'token-mean', label: 'token-mean（DAPO 推荐）' },
                { value: 'seq-mean-token-sum', label: 'seq-mean-token-sum' },
                { value: 'seq-mean-token-mean', label: 'seq-mean-token-mean' },
              ]}
            />
          </Field>
          <Field label="KL 系数 kl_loss_coef">
            <NumberInput value={rl.kl_loss_coef} onChange={(value) => patch({ kl_loss_coef: value ?? 0.001 })} />
          </Field>
          <Field label="熵系数 entropy_coeff">
            <NumberInput value={rl.entropy_coeff} onChange={(value) => patch({ entropy_coeff: value ?? 0 })} />
          </Field>
          <Field label="并行策略">
            <Select
              value={rl.strategy}
              onValueChange={(value) => patch({ strategy: value as RLConfig['strategy'] })}
              options={[
                { value: 'fsdp2', label: 'FSDP2（推荐）' },
                { value: 'fsdp', label: 'FSDP' },
                { value: 'megatron', label: 'Megatron（仅已注册模型）' },
              ]}
            />
          </Field>
          <Field label="保存间隔 save_freq">
            <NumberInput value={rl.save_freq} onChange={(value) => patch({ save_freq: value ?? 20 })} />
          </Field>
          <Field label="验证间隔 test_freq">
            <NumberInput value={rl.test_freq} onChange={(value) => patch({ test_freq: value ?? 20 })} />
          </Field>
          <Field label="总训练步数" hint="留空表示按 epochs 计算">
            <NumberInput value={rl.total_training_steps} onChange={(value) => patch({ total_training_steps: value })} />
          </Field>
        </Grid>

        <div className="grid gap-2 sm:grid-cols-2">
          <ToggleRow
            label="使用 KL 损失"
            hint="约束策略不要偏离参考模型太远"
            checked={rl.use_kl_loss}
            onCheckedChange={(checked) => patch({ use_kl_loss: checked })}
          />
          <ToggleRow
            label="奖励中加入 KL 惩罚"
            checked={rl.use_kl_in_reward}
            onCheckedChange={(checked) => patch({ use_kl_in_reward: checked })}
          />
          <ToggleRow
            label="动态采样 filter_groups"
            hint="丢弃全对/全错的组并重采（DAPO 关键技巧）"
            checked={rl.filter_groups_enable}
            onCheckedChange={(checked) => patch({ filter_groups_enable: checked })}
          />
          <ToggleRow
            label="参数 offload"
            hint="显存不足时把参数换出到 CPU，速度更慢"
            checked={rl.param_offload}
            onCheckedChange={(checked) => patch({ param_offload: checked })}
          />
          <ToggleRow
            label="梯度检查点"
            checked={rl.gradient_checkpointing}
            onCheckedChange={(checked) => patch({ gradient_checkpointing: checked })}
          />
          <ToggleRow
            label="训练前先验证"
            checked={rl.val_before_train}
            onCheckedChange={(checked) => patch({ val_before_train: checked })}
          />
        </div>
      </Advanced>

      <Advanced title="进阶：自定义奖励与透传参数">
        <Grid cols={2}>
          <Field label="自定义奖励函数文件" hint="服务器上的 .py 绝对路径">
            <Input
              value={rl.custom_reward_function_path}
              onChange={(event) => patch({ custom_reward_function_path: event.target.value })}
              placeholder="/workspace/.../reward.py"
            />
          </Field>
          <Field label="函数名">
            <Input
              value={rl.custom_reward_function_name}
              onChange={(event) => patch({ custom_reward_function_name: event.target.value })}
              placeholder="compute_score"
            />
          </Field>
        </Grid>
        <KeyValueEditor
          value={Object.fromEntries(Object.entries(rl.extra_args).map(([key, value]) => [key, String(value)]))}
          onChange={(next) => setRl((previous) => ({ ...previous, extra_args: next }))}
          keyPlaceholder={rl.framework === 'verl' ? 'actor_rollout_ref.actor.ppo_epochs' : 'beta'}
          valuePlaceholder={rl.framework === 'verl' ? '2' : '0.04'}
          secretKeys={false}
        />
        <p className="text-[11px] text-fg-subtle">
          {rl.framework === 'verl'
            ? 'verl 使用 Hydra，键值会以 key=value 形式追加到命令行末尾。'
            : 'TRL 的键值会合并进生成脚本里的 Config 参数。'}
        </p>
      </Advanced>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
function HarborDatasetHint({
  rl,
  setRl,
}: {
  rl: RLConfig
  setRl: React.Dispatch<React.SetStateAction<RLConfig>>
}) {
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api.datasets() })
  const selected = datasets.data?.find((dataset) => dataset.id === rl.dataset_id)

  React.useEffect(() => {
    if (!selected) return
    const isHarbor = selected.kind === 'harbor_tasks'
    setRl((previous) => {
      if (previous.harbor.enabled === isHarbor && (!isHarbor || previous.framework === 'trl')) return previous
      return {
        ...previous,
        framework: isHarbor ? 'trl' : previous.framework,
        harbor: { ...previous.harbor, enabled: isHarbor },
      }
    })
  }, [selected, setRl])

  if (!selected) return null
  if (selected.kind === 'harbor_tasks') {
    return (
      <InlineHint>
        已选择 Harbor 任务树（{selected.stats.n_tasks} 个任务）：框架自动切换为 TRL，
        奖励直接取自每个任务 verifier 的 reward，无需再配置奖励函数。
      </InlineHint>
    )
  }
  return (
    <InlineHint>
      已选择 parquet 数据集：verl 会读取 train/test parquet，reward 由 data_source 对应的规则打分函数计算。
    </InlineHint>
  )
}

function cleanKv(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([key]) => key.trim()))
}
