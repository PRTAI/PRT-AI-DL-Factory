// Mirrors backend/app/models — keep in sync when the API changes.

export type Role = 'admin' | 'user'
export type JobType = 'sft' | 'rl' | 'verify'
export type Framework = 'llamafactory' | 'verl' | 'trl' | 'harbor'
export type JobStatus =
  | 'draft'
  | 'pending_review'
  | 'rejected'
  | 'queued'
  | 'preparing'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
export type TrainStage =
  | 'sft'
  | 'dpo'
  | 'kto'
  | 'orpo'
  | 'simpo'
  | 'ppo'
  | 'grpo'
  | 'dapo'
  | 'gspo'
  | 'rloo'
  | 'reinforce_plus_plus'
  | 'remax'
export type DatasetKind =
  | 'sft_alpaca'
  | 'sft_sharegpt'
  | 'dpo_pairs'
  | 'kto_pairs'
  | 'harbor_tasks'
  | 'verl_parquet'
  | 'raw'
export type DatasetStatus = 'uploading' | 'extracting' | 'validating' | 'ready' | 'invalid' | 'deleted'
export type DownloadStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface User {
  id: string
  email: string
  display_name: string
  role: Role
  active: boolean
  must_change_password: boolean
  created_at: string
  last_login_at?: string | null
  max_concurrent_jobs?: number | null
  max_gpus_per_job?: number | null
  note: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export interface DatasetStats {
  n_records: number
  n_files: number
  n_tasks: number
  total_bytes: number
  columns: string[]
  sample: Record<string, unknown>[]
  warnings: string[]
}

export interface DatasetFileInfo {
  path: string
  size_bytes: number
  records?: number | null
  role: string
}

export interface Dataset {
  id: string
  name: string
  kind: DatasetKind
  description: string
  status: DatasetStatus
  owner_id: string
  owner_email: string
  created_at: string
  updated_at: string
  root_path: string
  archive_name: string
  archive_bytes: number
  stats: DatasetStats
  files: DatasetFileInfo[]
  error: string
  lf_dataset_names: string[]
  tags: string[]
}

export interface ModelEntry {
  id: string
  vendor: string
  name: string
  path: string
  size_bytes: number
  model_type: string
  architectures: string[]
  num_parameters_b?: number | null
  hidden_size?: number | null
  num_layers?: number | null
  max_position_embeddings?: number | null
  torch_dtype: string
  has_tokenizer: boolean
  is_multimodal: boolean
  modified_at?: string | null
  llamafactory_template?: string | null
  supported_frameworks: string[]
  notes: string[]
}

export interface ModelDownload {
  id: string
  model_id: string
  source: string
  revision: string
  target_path: string
  status: DownloadStatus
  requested_by_email: string
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  exit_code?: number | null
  error: string
  downloaded_bytes: number
}

export interface ResourceSpec {
  n_gpus: number
  gpu_ids: number[]
  cpu_cores?: number | null
  memory_gb?: number | null
  priority: number
  env: Record<string, string>
}

export interface LoraSpec {
  rank: number
  alpha: number
  dropout: number
  target: string
  create_new_adapter: boolean
}

export interface SFTConfig {
  framework: 'llamafactory'
  stage: TrainStage
  model_path: string
  model_id: string
  template: string
  trust_remote_code: boolean
  dataset_ids: string[]
  eval_dataset_ids: string[]
  val_size: number
  finetuning_type: 'lora' | 'full' | 'freeze'
  lora: LoraSpec
  freeze_trainable_layers: number
  cutoff_len: number
  max_samples?: number | null
  learning_rate: number
  num_train_epochs: number
  max_steps?: number | null
  per_device_train_batch_size: number
  gradient_accumulation_steps: number
  lr_scheduler_type: string
  warmup_ratio: number
  weight_decay: number
  max_grad_norm: number
  logging_steps: number
  save_steps: number
  eval_steps?: number | null
  save_only_model: boolean
  resume_from_checkpoint?: string | null
  compute_type: 'bf16' | 'fp16' | 'fp32' | 'pure_bf16'
  quantization_bit?: number | null
  flash_attn: 'auto' | 'disabled' | 'sdpa' | 'fa2'
  gradient_checkpointing: boolean
  deepspeed_stage: 'none' | 'zero2' | 'zero3' | 'zero3_offload'
  liger_kernel: boolean
  pref_beta: number
  pref_loss: string
  pref_ftx: number
  report_to: string
  plot_loss: boolean
  extra_args: Record<string, unknown>
}

export interface RolloutSpec {
  backend: 'vllm' | 'sglang' | 'hf'
  n: number
  temperature: number
  top_p: number
  top_k: number
  gpu_memory_utilization: number
  tensor_model_parallel_size: number
  max_num_batched_tokens: number
}

export interface HarborEnvSpec {
  enabled: boolean
  harness: string
  environment_type: string
  num_tasks?: number | null
  max_tool_calling_iterations: number
  include_metadata: boolean
}

export interface RLConfig {
  framework: 'verl' | 'trl'
  algorithm: TrainStage
  model_path: string
  model_id: string
  dataset_id: string
  eval_dataset_id: string
  max_prompt_length: number
  max_response_length: number
  train_batch_size: number
  mini_batch_size: number
  micro_batch_size_per_gpu: number
  prompt_key: string
  truncation: 'error' | 'left' | 'right' | 'middle'
  filter_overlong_prompts: boolean
  learning_rate: number
  total_epochs: number
  total_training_steps?: number | null
  warmup_ratio: number
  weight_decay: number
  clip_ratio: number
  clip_ratio_low: number
  clip_ratio_high: number
  loss_agg_mode: string
  entropy_coeff: number
  use_kl_loss: boolean
  kl_loss_coef: number
  use_kl_in_reward: boolean
  norm_adv_by_std_in_grpo: boolean
  filter_groups_enable: boolean
  filter_groups_metric: string
  filter_groups_max_num_gen_batches: number
  enable_overlong_buffer: boolean
  overlong_buffer_len: number
  overlong_penalty_factor: number
  rollout: RolloutSpec
  harbor: HarborEnvSpec
  strategy: 'fsdp' | 'fsdp2' | 'megatron'
  param_offload: boolean
  optimizer_offload: boolean
  use_dynamic_bsz: boolean
  gradient_checkpointing: boolean
  reward_fn_key: string
  custom_reward_function_path: string
  custom_reward_function_name: string
  save_freq: number
  test_freq: number
  val_before_train: boolean
  logger: string[]
  extra_args: Record<string, unknown>
}

export interface VerifyConfig {
  framework: 'harbor'
  dataset_id: string
  tasks_path: string
  registry_dataset: string
  agent: string
  model: string
  agent_import_path: string
  n_concurrent: number
  n_attempts: number
  max_retries: number
  environment_type: string
  task_names: string[]
  exclude_task_names: string[]
  n_tasks?: number | null
  agent_env: Record<string, string>
  agent_kwargs: Record<string, string>
  verifier_env: Record<string, string>
  environment_kwargs: Record<string, string>
  verifier_kwargs: Record<string, string>
  environment_cpus?: number | null
  environment_memory_mb?: number | null
  environment_gpus?: number | null
  timeout_multiplier: number
  agent_timeout_multiplier?: number | null
  verifier_timeout_multiplier?: number | null
  disable_verification: boolean
  force_build: boolean
  export_traces: boolean
  sharegpt: boolean
  extra_args: Record<string, unknown>
}

export interface JobProgress {
  percent: number
  current_step: number
  total_steps: number
  current_epoch: number
  total_epochs: number
  eta_seconds?: number | null
  throughput?: number | null
  message: string
  completed_trials: number
  total_trials: number
}

export interface JobMetricsSummary {
  last_loss?: number | null
  best_loss?: number | null
  last_reward?: number | null
  best_reward?: number | null
  learning_rate?: number | null
  grad_norm?: number | null
  accuracy?: number | null
  pass_at_k: Record<string, number>
  n_trials?: number | null
  n_errors?: number | null
  resolved_rate?: number | null
  cost_usd?: number | null
  tokens: Record<string, number>
  updated_at?: string | null
}

export interface JobRuntime {
  work_dir: string
  output_dir: string
  log_file: string
  script_path: string
  command: string
  pid?: number | null
  pgid?: number | null
  exit_code?: number | null
  started_at?: string | null
  finished_at?: string | null
  cancel_requested: boolean
  host: string
  error: string
}

export interface JobReview {
  required: boolean
  reviewer_id?: string | null
  reviewer_email?: string | null
  reviewed_at?: string | null
  comment: string
}

export interface Job {
  id: string
  name: string
  note: string
  type: JobType
  framework: Framework
  status: JobStatus
  owner_id: string
  owner_email: string
  created_at: string
  updated_at: string
  queued_at?: string | null
  tags: string[]
  resources: ResourceSpec
  review: JobReview
  runtime: JobRuntime
  progress: JobProgress
  metrics: JobMetricsSummary
  sft?: SFTConfig | null
  rl?: RLConfig | null
  verify?: VerifyConfig | null
  parent_job_id?: string | null
}

export interface Notification {
  id: string
  at: string
  user_id: string
  level: 'info' | 'success' | 'warning' | 'error'
  title: string
  body: string
  link: string
  read: boolean
}

export interface AuditEvent {
  id: string
  at: string
  actor_id: string
  actor_email: string
  action: string
  target_type: string
  target_id: string
  summary: string
  ip: string
  detail: Record<string, unknown>
}

export interface SystemSettings {
  require_approval: boolean
  max_concurrent_jobs: number
  default_max_gpus_per_job: number
  allow_self_service_verify: boolean
  banner: string
  updated_at: string
  updated_by: string
}

export interface GpuDevice {
  index: number
  name: string
  utilization: number | null
  memory_utilization: number | null
  temperature: number | null
  memory_used: number
  memory_total: number
  memory_percent: number
  power_watts: number | null
  power_limit_watts: number | null
  processes: { pid: number; memory: number }[]
}

export interface SystemMetrics {
  at: number
  cpu: { percent: number; per_core: number[]; count: number; physical: number; load: number[] }
  memory: { percent: number; used: number; total: number; available: number; swap_percent: number }
  disk: { percent: number; used: number; total: number; free: number; read_bps: number; write_bps: number }
  network: { sent_bps: number; recv_bps: number }
  gpu: {
    count: number
    devices: GpuDevice[]
    avg_utilization: number
    avg_memory_percent: number
  }
}

export interface MetricPoint {
  at: number
  cpu: number
  memory: number
  gpu: number
  gpu_memory: number
  net_recv: number
  net_sent: number
  disk_read: number
  disk_write: number
  gpus: { index: number; utilization: number; memory: number }[]
}

export interface GpuSnapshot {
  total: number
  busy: number[]
  free: number[]
  assignments: Record<string, string>
}

export interface DashboardPayload {
  at: string
  metrics: SystemMetrics
  history: MetricPoint[]
  gpu: GpuSnapshot
  active_jobs: Job[]
  recent_jobs: Job[]
  counters: {
    running: number
    queued: number
    pending_review: number
    succeeded: number
    failed: number
    total: number
    users: number | null
    datasets: number
  }
}

export interface ToolchainRow {
  key: string
  label: string
  version: string
  root: string
  root_exists: boolean
  python: string
  python_ok: boolean
  /** framework module importable by the configured interpreter */
  importable: boolean | null
  /** hard dependency (torch) present — null when not applicable */
  deps_ok: boolean | null
  status: 'ready' | 'missing_deps' | 'missing_python' | 'unknown'
  detail: string
}

export interface AlgorithmInfo {
  id: string
  label: string
  job_type: JobType
  frameworks: Framework[]
  dataset_kinds: DatasetKind[]
  summary: string
  doc: string
}

export interface Preset {
  id: string
  label: string
  description: string
  framework?: Framework
  algorithm?: TrainStage
  recommended_gpus?: number
  config: Record<string, unknown>
}

export interface HarborAgentInfo {
  name: string
  description: string
  env_keys: string[]
  needs_model: boolean
  recommended: boolean
}

export interface DatasetSpec {
  kind: DatasetKind
  title: string
  frameworks: string[]
  stages: string[]
  summary: string
  layout: string[]
  required_fields: string[]
  optional_fields: string[]
  example: unknown
  notes: string[]
}

export interface CompatSection {
  framework: Framework
  title: string
  headline: string
  bullets: string[]
  table: { columns: string[]; rows: string[][] } | null
}

export interface MetricsPayload {
  series: Record<string, [number, number][]>
  summary: Partial<JobMetricsSummary>
  progress: Partial<JobProgress>
  status?: JobStatus
  framework?: Framework
}

export interface CommandPreview {
  command: string
  cwd: string
  env: Record<string, string>
  notes: string[]
  files: Record<string, string>
  output_dir: string
}
