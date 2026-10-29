import type {
  AgentConversation,
  AgentMessage,
  AgentModelOption,
  AgentSkill,
  AgentTool,
  AlgorithmInfo,
  AuditEvent,
  CommandPreview,
  CompatSection,
  DashboardPayload,
  Dataset,
  DatasetKind,
  DatasetSpec,
  Deployment,
  DeploymentEndpoint,
  ExportCandidates,
  GitHubBinding,
  Issue,
  ExportTask,
  GpuSnapshot,
  HarborAgentInfo,
  Job,
  JobStatus,
  JobType,
  MetricPoint,
  MetricsPayload,
  ModelDownload,
  ModelEntry,
  Notification,
  ModelApi,
  ModelProviderInfo,
  ModelPlatform,
  PlatformCatalog,
  PricingSettings,
  SpendSummary,
  UserApiKey,
  RegistryDataset,
  ReviewFinding,
  ReviewTask,
  TrialDetail,
  TrialPage,
  Project,
  ProjectWithStats,
  SystemMetrics,
  TeamMember,
  SystemSettings,
  TokenResponse,
  ToolchainRow,
  User,
  VerifierEnvInfo,
} from './types'

const TOKEN_KEY = 'prt.token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })
  if (response.status === 401) {
    onUnauthorized?.()
    throw new ApiError(401, '登录状态已失效，请重新登录')
  }
  if (response.status === 204) return undefined as T
  const text = await response.text()
  const payload = text ? safeParse(text) : null
  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(payload) ?? `请求失败 (${response.status})`)
  }
  return payload as T
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function extractDetail(payload: unknown): string | null {
  if (!payload) return null
  if (typeof payload === 'string') return payload
  const detail = (payload as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    // FastAPI 的校验错误形如 {loc: ['body','email'], msg: 'Value error, 邮箱格式不正确'}
    // —— 这是给开发看的，用户只需要那句人话。
    const clean = (msg: string) => String(msg).replace(/^(Value error|Assertion failed),\s*/i, '')
    if (detail.length === 1) return clean(detail[0]?.msg ?? '')
    return detail
      .map((item) => {
        const location = Array.isArray(item?.loc) ? item.loc.slice(1).join('.') : ''
        return location ? `${location}: ${clean(item.msg)}` : clean(item.msg)
      })
      .join('；')
  }
  return null
}

const qs = (params: Record<string, unknown>) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)))
    else search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ''
}

/**
 * 带 token 的附件下载。文件名优先用后端 `Content-Disposition` 里的
 * `filename*=UTF-8''…`（返修报告的名字是中文，走 `filename=` 会乱码）。
 */
async function downloadAttachment(path: string, fallback: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${getToken() ?? ''}`)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(safeParse(await response.text())) ?? '下载失败')
  }
  const disposition = response.headers.get('content-disposition') ?? ''
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = encoded ? decodeURIComponent(encoded) : fallback
  anchor.click()
  URL.revokeObjectURL(url)
}

function uploadWithProgress<T>(
  method: string,
  path: string,
  form: FormData,
  onProgress?: (percent: number) => void,
) {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open(method, path)
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }
    xhr.onload = () => {
      const payload = safeParse(xhr.responseText)
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload as T)
      else reject(new ApiError(xhr.status, extractDetail(payload) ?? `上传失败 (${xhr.status})`))
    }
    xhr.onerror = () => reject(new ApiError(0, '网络错误，上传失败'))
    xhr.send(form)
  })
}

export interface Prebuild {
  id: string
  dataset_id: string
  task: string
  command: string
  status: 'running' | 'succeeded' | 'failed'
  return_code: number | null
  created_at: string
  finished_at: string | null
  log?: string
}

export const api = {
  // -------------------------------------------------------------- auth ----
  login: (email: string, password: string) =>
    request<TokenResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<User>('/api/auth/me'),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  changePassword: (current_password: string, new_password: string) =>
    request<User>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password }),
    }),

  // ------------------------------------------------------------- users ----
  users: () => request<User[]>('/api/users'),
  createUser: (payload: {
    email: string
    display_name?: string
    password: string
    role: 'admin' | 'user'
    must_change_password?: boolean
    max_concurrent_jobs?: number | null
    max_gpus_per_job?: number | null
    note?: string
  }) => request<User>('/api/users', { method: 'POST', body: JSON.stringify(payload) }),
  updateUser: (id: string, payload: Partial<User>) =>
    request<User>(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  resetPassword: (id: string, password: string, must_change_password = true) =>
    request<User>(`/api/users/${id}/password`, {
      method: 'POST',
      body: JSON.stringify({ password, must_change_password }),
    }),
  deleteUser: (id: string) => request<void>(`/api/users/${id}`, { method: 'DELETE' }),

  // ----------------------------------------------------- notifications ----
  notifications: (unreadOnly = false) =>
    request<Notification[]>(`/api/notifications${qs({ unread_only: unreadOnly })}`),
  markRead: (ids?: string[]) =>
    request<{ updated: number }>('/api/notifications/read', {
      method: 'POST',
      body: JSON.stringify(ids ?? null),
    }),
  audit: (params: { limit?: number; action_prefix?: string; actor_id?: string; target_id?: string } = {}) =>
    request<AuditEvent[]>(`/api/audit${qs(params)}`),

  // ----------------------------------------------------------- projects ----
  // 项目在团队内共享：任何登录用户都能列出/搜索，改和删只有负责人与管理员能做。
  // q 同时匹配项目名与**项目内的任务名/负责人**，三个条件是联合的
  projects: (
    params: {
      q?: string
      owner_id?: string
      job_type?: JobType
      include_archived?: boolean
    } = {},
  ) => request<ProjectWithStats[]>(`/api/projects${qs(params)}`),
  /** 负责人下拉框数据（`/api/users` 是管理员专属，普通成员用这个） */
  teamMembers: () => request<TeamMember[]>('/api/projects/members'),
  project: (id: string) => request<ProjectWithStats>(`/api/projects/${id}`),
  projectJobs: (id: string, limit = 20) =>
    request<Job[]>(`/api/projects/${id}/jobs${qs({ limit })}`),
  createProject: (payload: { name: string; description?: string; tags?: string[] }) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  updateProject: (
    id: string,
    payload: { name?: string; description?: string; tags?: string[]; archived?: boolean; owner_id?: string },
  ) => request<Project>(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: 'DELETE' }),

  // -------------------------------------------------------------- jobs ----
  jobs: (params: {
    mine_only?: boolean
    status?: JobStatus[]
    type?: JobType[]
    project_id?: string
    owner_id?: string
    q?: string
    limit?: number
    offset?: number
  } = {}, signal?: AbortSignal) =>
    request<{ items: Job[]; total: number; offset: number; limit: number; billing_pending?: string[] }>(`/api/jobs${qs(params)}`, { signal }),
  jobOverview: (projectId = '') =>
    request<{
      total: number
      mine: number
      by_status: Record<string, number>
      by_type: Record<string, number>
      pending_review: number
      running: number
      queued: number
      gpu: GpuSnapshot
    }>(`/api/jobs/overview${qs({ project_id: projectId })}`),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  renameJob: (id: string, name: string) =>
    request<Job>(`/api/jobs/${id}/name`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  createJob: (payload: unknown) =>
    request<Job>('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  createVerifyBatch: (payload: unknown) =>
    request<Job[]>('/api/jobs/verify-batch', { method: 'POST', body: JSON.stringify(payload) }),
  jobLog: (id: string, offset = 0) =>
    request<{ content: string; size: number; offset: number; eof: boolean }>(
      `/api/jobs/${id}/log${qs({ offset })}`,
    ),
  jobMetrics: (id: string) => request<MetricsPayload>(`/api/jobs/${id}/metrics`),
  jobArtifacts: (id: string) =>
    request<{ name: string; path: string; is_dir: boolean; size_bytes: number | null; modified_at: number }[]>(
      `/api/jobs/${id}/artifacts`,
    ),
  jobCommand: (id: string) => request<CommandPreview>(`/api/jobs/${id}/command`),
  jobTrials: (id: string, params: { page?: number; page_size?: number; phase?: string; q?: string } = {}) =>
    request<TrialPage>(`/api/jobs/${id}/trials${qs(params)}`),
  jobTrialDetail: (id: string, trialName: string) =>
    request<TrialDetail>(`/api/jobs/${id}/trials/${encodeURIComponent(trialName)}`),
  // Downloads go through fetch + blob because the REST API only accepts the
  // bearer header (a token in the query string would leak into access logs).
  downloadArtifact: async (id: string, path: string) => {
    const response = await fetch(`/api/jobs/${id}/artifacts/download?path=${encodeURIComponent(path)}`, {
      headers: { Authorization: `Bearer ${getToken() ?? ''}` },
    })
    if (!response.ok) throw new ApiError(response.status, extractDetail(safeParse(await response.text())) ?? '下载失败')
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = path.split('/').pop() ?? 'artifact'
    anchor.click()
    URL.revokeObjectURL(url)
  },
  downloadJob: (id: string) => downloadAttachment(`/api/jobs/${id}/export`, `${id}.zip`),
  batchDownloadJobs: (jobIds: string[]) => downloadAttachment('/api/jobs/batch-export', 'jobs.zip', {
    method: 'POST', body: JSON.stringify({ job_ids: jobIds }),
  }),
  reviewJob: (id: string, approve: boolean, comment = '') =>
    request<Job>(`/api/jobs/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({ approve, comment }),
    }),
  cancelJob: (id: string, reason = '') =>
    request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST', body: JSON.stringify({ reason }) }),
  batchResubmitJobs: (jobIds: string[]) => request<{ results: { source_id: string; job_id: string; status: string; error: string }[]; succeeded: number; failed: number }>('/api/jobs/batch-resubmit', { method: 'POST', body: JSON.stringify({ job_ids: jobIds }) }),
  resubmitJob: (id: string) => request<Job>(`/api/jobs/${id}/resubmit`, { method: 'POST' }),
  harborActions: (id: string) => request<{
    recorded_trials: number; unfinished_trials: number; error_types: Record<string, number>; regrade_error: string
  }>(`/api/jobs/${id}/harbor-actions`),
  createTrialActions: (id: string, payload: { mode: 'rerun' | 'regrade' | 'resume'; trial_names: string[] }) =>
    request<Job[]>(`/api/jobs/${id}/trial-actions`, { method: 'POST', body: JSON.stringify(payload) }),
  createHarborAction: (id: string, payload: {
    mode: 'resume' | 'regrade'; filter_error_types?: string[]; dataset_id?: string
    judge_model_api_id?: string; judge_api_protocol?: 'anthropic' | 'openai'
    batch_judge?: boolean
  }) => request<Job>(`/api/jobs/${id}/harbor-actions`, { method: 'POST', body: JSON.stringify(payload) }),
  verifySuggestion: (id: string) =>
    request<{
      parent_job_id: string
      parent_job_name: string
      /** 训练任务所属项目 —— 评测应该落在同一个项目里 */
      project_id: string
      project_name: string
      model_path: string
      checkpoints: string[]
      hint: string
    }>(`/api/jobs/${id}/verify-suggestion`),
  deleteJob: (id: string, purgeOutputs = false) =>
    request<void>(`/api/jobs/${id}${qs({ purge_outputs: purgeOutputs })}`, { method: 'DELETE' }),

  // ---------------------------------------------------------- datasets ----
  datasets: (params: { kind?: DatasetKind; mine_only?: boolean; project_id?: string } = {}) =>
    request<Dataset[]>(`/api/datasets${qs(params)}`),
  dataset: (id: string) => request<Dataset>(`/api/datasets/${id}`),
  datasetPreview: (id: string) =>
    request<{ kind: string; file?: string; records?: unknown[]; tasks?: unknown[] }>(
      `/api/datasets/${id}/preview`,
    ),
  datasetSpecs: () => request<{ version: number; specs: DatasetSpec[] }>('/api/datasets/specs'),
  uploadDataset: (form: FormData, onProgress?: (percent: number) => void) =>
    uploadWithProgress<Dataset>('POST', '/api/datasets', form, onProgress),
  /** 重新上传压缩包替换数据集内容，id 不变 */
  replaceDatasetFile: (id: string, form: FormData, onProgress?: (percent: number) => void) =>
    uploadWithProgress<Dataset>('PUT', `/api/datasets/${id}/file`, form, onProgress),
  startPrebuild: (datasetId: string, task: string) =>
    request<Prebuild>(`/api/datasets/${datasetId}/prebuild`, { method: 'POST', body: JSON.stringify({ task }) }),
  prebuild: (buildId: string) => request<Prebuild>(`/api/datasets/prebuilds/${buildId}`),
  updateDataset: (
    id: string,
    payload: { name?: string; project_id?: string; description?: string; tags?: string[]; rewardkit?: boolean },
  ) =>
    request<Dataset>(`/api/datasets/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteDataset: (id: string) => request<void>(`/api/datasets/${id}`, { method: 'DELETE' }),

  // ------------------------------------------------------------ models ----
  models: (params: { q?: string; framework?: string; limit?: number } = {}) =>
    request<ModelEntry[]>(`/api/models${qs(params)}`),
  refreshModels: () => request<ModelEntry[]>('/api/models/refresh', { method: 'POST' }),
  modelCompat: () => request<{ generated_at: string; sections: CompatSection[] }>('/api/models/compat'),
  modelsRoot: () =>
    request<{ models_root: string; exists: boolean; count: number }>('/api/models/root'),
  downloads: () => request<ModelDownload[]>('/api/models/downloads'),
  startDownload: (model_id: string, revision = '') =>
    request<ModelDownload>('/api/models/downloads', {
      method: 'POST',
      body: JSON.stringify({ model_id, revision, source: 'modelscope' }),
    }),
  downloadLog: (id: string) => request<{ content: string }>(`/api/models/downloads/${id}/log`),
  cancelDownload: (id: string) =>
    request<ModelDownload>(`/api/models/downloads/${id}/cancel`, { method: 'POST' }),

  // -------------------------------------------------------- deployments ----
  deployments: (params: { active_only?: boolean; project_id?: string } = {}) =>
    request<Deployment[]>(`/api/deployments${qs(params)}`),
  deployment: (id: string) => request<Deployment>(`/api/deployments/${id}`),
  deploymentEndpoints: () => request<DeploymentEndpoint[]>('/api/deployments/endpoints'),
  createDeployment: (payload: unknown) =>
    request<Deployment>('/api/deployments', { method: 'POST', body: JSON.stringify(payload) }),
  deploymentLog: (id: string, offset = 0) =>
    request<{ content: string; size: number; offset: number; eof: boolean }>(
      `/api/deployments/${id}/log${qs({ offset })}`,
    ),
  stopDeployment: (id: string) =>
    request<Deployment>(`/api/deployments/${id}/stop`, { method: 'POST' }),
  deleteDeployment: (id: string) => request<void>(`/api/deployments/${id}`, { method: 'DELETE' }),

  // ------------------------------------------------------------ exports ----
  exports: (jobId = '') => request<ExportTask[]>(`/api/exports${qs({ job_id: jobId })}`),
  exportCandidates: (jobId: string) =>
    request<ExportCandidates>(`/api/exports/candidates/${jobId}`),
  createExport: (payload: {
    job_id: string
    name?: string
    adapter_path?: string
    export_size_gb?: number
    export_device?: string
    export_legacy_format?: boolean
  }) => request<ExportTask>('/api/exports', { method: 'POST', body: JSON.stringify(payload) }),
  exportLog: (id: string) => request<{ content: string; size: number; eof: boolean }>(`/api/exports/${id}/log`),
  cancelExport: (id: string) => request<ExportTask>(`/api/exports/${id}/cancel`, { method: 'POST' }),
  deleteExport: (id: string, purgeFiles = false) =>
    request<void>(`/api/exports/${id}${qs({ purge_files: purgeFiles })}`, { method: 'DELETE' }),

  // ------------------------------------------------------------ reviews ----
  reviews: (params: { mine_only?: boolean; project_id?: string } = {}) =>
    request<ReviewTask[]>(`/api/reviews${qs(params)}`),
  review: (id: string) => request<ReviewTask>(`/api/reviews/${id}`),
  reviewFindings: (id: string) => request<ReviewFinding[]>(`/api/reviews/${id}/findings`),
  reviewAgentLog: (id: string) => request<{ text: string; running: boolean }>(`/api/reviews/${id}/log`),
  // 压缩包可能很大，照 uploadDataset 用 XHR 才能拿到上传进度
  createReview: (form: FormData, onProgress?: (percent: number) => void) =>
    new Promise<ReviewTask>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/reviews')
      const token = getToken()
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      }
      xhr.onload = () => {
        const payload = safeParse(xhr.responseText)
        if (xhr.status >= 200 && xhr.status < 300) resolve(payload as ReviewTask)
        else reject(new ApiError(xhr.status, extractDetail(payload) ?? `提交失败 (${xhr.status})`))
      }
      xhr.onerror = () => reject(new ApiError(0, '网络错误，提交失败'))
      xhr.send(form)
    }),
  downloadReviewReport: (id: string) =>
    downloadAttachment(`/api/reviews/${id}/report.docx`, `${id}.docx`),
  downloadReviewArchive: (id: string) =>
    downloadAttachment(`/api/reviews/${id}/archive`, `${id}.zip`),
  cancelReview: (id: string) => request<ReviewTask>(`/api/reviews/${id}/cancel`, { method: 'POST' }),
  deleteReview: (id: string, purgeFiles = false) =>
    request<void>(`/api/reviews/${id}${qs({ purge_files: purgeFiles })}`, { method: 'DELETE' }),

  // -------------------------------------------------------------- issues ----
  issues: (status = '') => request<Issue[]>(`/api/issues${qs({ status })}`),
  issue: (id: string) => request<Issue>(`/api/issues/${id}`),
  issueAgentLog: (id: string) => request<{ text: string; running: boolean }>(`/api/issues/${id}/log`),
  createIssue: (payload: { title: string; description?: string; page?: string }) =>
    request<Issue>('/api/issues', { method: 'POST', body: JSON.stringify(payload) }),
  closeIssue: (id: string) => request<Issue>(`/api/issues/${id}/close`, { method: 'POST' }),
  startFix: (id: string) => request<Issue>(`/api/issues/${id}/fix`, { method: 'POST' }),
  mergeIssue: (id: string) => request<Issue>(`/api/issues/${id}/merge`, { method: 'POST' }),

  // -------------------------------------------------------------- github ----
  githubBinding: () => request<GitHubBinding>('/api/github/me'),
  setGitIdentity: (git_name: string, git_email: string) =>
    request<User>('/api/auth/git-identity', { method: 'PUT', body: JSON.stringify({ git_name, git_email }) }),
  bindGithubToken: (token: string) =>
    request<GitHubBinding>('/api/github/me/token', { method: 'PUT', body: JSON.stringify({ token }) }),
  unbindGithub: () => request<void>('/api/github/me', { method: 'DELETE' }),
  githubOauthStart: () =>
    request<{ url: string }>(`/api/github/oauth/start${qs({ origin: window.location.origin })}`, { method: 'POST' }),

  // ------------------------------------------------------------ system ----
  systemMetrics: () => request<SystemMetrics>('/api/system/metrics'),
  systemHistory: (limit = 300) =>
    request<{ points: MetricPoint[]; interval_seconds: number }>(
      `/api/system/metrics/history${qs({ limit })}`,
    ),
  dashboard: () => request<DashboardPayload>('/api/system/dashboard?include_billing=false'),
  spendSummary: (params: { start?: string; end?: string } = {}) =>
    request<SpendSummary>(`/api/system/billing${qs(params)}`),
  toolchain: () => request<ToolchainRow[]>('/api/system/toolchain'),
  harborImage: () => request<{ image: string; ready: boolean; image_id: string; status: string; error: string; log: string }>('/api/system/harbor-image'),
  harborImageRecipe: () => request<{ dockerfile: string }>('/api/system/harbor-image/recipe'),
  buildHarborImage: () => request('/api/system/harbor-image/build', { method: 'POST' }),
  settings: () => request<SystemSettings>('/api/system/settings'),
  pricing: () => request<PricingSettings>('/api/system/pricing'),
  priceCatalog: () => request<NonNullable<PricingSettings['prices']>>('/api/system/pricing/catalog'),
  updatePricing: (payload: PricingSettings) =>
    request<PricingSettings>('/api/system/pricing', { method: 'PUT', body: JSON.stringify(payload) }),
  updateSettings: (payload: SystemSettings) =>
    request<SystemSettings>('/api/system/settings', { method: 'PUT', body: JSON.stringify(payload) }),
  info: () =>
    request<{
      app_name: string
      version: string
      models_root: string
      data_root: string
      gpu_total: number
      monitor_interval_seconds: number
      max_upload_mb: number
    }>('/api/system/info'),

  // -------------------------------------------------------------- docs ----
  docs: () =>
    request<{ slug: string; title: string; category: string; summary: string; order: number; updated_at: number }[]>(
      '/api/handbook',
    ),
  doc: (slug: string) =>
    request<{ slug: string; title: string; category: string; summary: string; body: string }>(`/api/handbook/${slug}`),
  /** 上游框架的仓库地址 + 本机装的版本 */
  docLinks: () =>
    request<{ key: string; label: string; url: string; note: string; version: string }[]>(
      '/api/handbook/links',
    ),

  // ----------------------------------------------------------- catalog ----
  algorithms: () => request<AlgorithmInfo[]>('/api/catalog/algorithms'),
  templates: () => request<string[]>('/api/catalog/templates'),
  harborAgents: () => request<HarborAgentInfo[]>('/api/catalog/harbor-agents'),
  harborEnvironments: () => request<string[]>('/api/catalog/harbor-environments'),
  harborVerifierEnv: (params: { dataset_id?: string; tasks_path?: string }) =>
    request<VerifierEnvInfo>(`/api/catalog/harbor-verifier-env${qs(params)}`),
  harborRegistry: (refresh = false) =>
    request<{ datasets: RegistryDataset[]; fetched_at: number | null; source: 'network' | 'cache' | 'loading' | 'none'; error: string; refreshing: boolean }>(
      `/api/catalog/harbor-registry${qs({ refresh: refresh || undefined })}`,
    ),

  // --------------------------------------------------------- model apis ----
  modelApis: () => request<ModelApi[]>('/api/model-apis'),
  modelPlatforms: () => request<ModelPlatform[]>('/api/model-apis/platforms'),
  platformCatalog: () => request<PlatformCatalog[]>('/api/model-apis/platform-catalog'),
  createModelPlatform: (payload: Record<string, unknown>) => request<ModelPlatform>('/api/model-apis/platforms', { method: 'POST', body: JSON.stringify(payload) }),
  updateModelPlatform: (id: string, payload: Record<string, unknown>) => request<ModelPlatform>(`/api/model-apis/platforms/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteModelPlatform: (id: string) => request<void>(`/api/model-apis/platforms/${id}`, { method: 'DELETE' }),
  modelProviders: () => request<ModelProviderInfo[]>('/api/model-apis/providers'),
  myApiKeys: () => request<UserApiKey[]>('/api/model-apis/my-keys'),
  setMyApiKey: (targetId: string, api_key: string) =>
    request<UserApiKey>(`/api/model-apis/my-keys/${encodeURIComponent(targetId)}`, { method: 'PUT', body: JSON.stringify({ api_key }) }),
  deleteMyApiKey: (targetId: string) =>
    request<void>(`/api/model-apis/my-keys/${encodeURIComponent(targetId)}`, { method: 'DELETE' }),
  createModelApi: (payload: Record<string, unknown>) =>
    request<ModelApi>('/api/model-apis', { method: 'POST', body: JSON.stringify(payload) }),
  updateModelApi: (id: string, payload: Record<string, unknown>) =>
    request<ModelApi>(`/api/model-apis/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteModelApi: (id: string) => request<void>(`/api/model-apis/${id}`, { method: 'DELETE' }),
  frameworks: () => request<Record<string, Record<string, unknown>>>('/api/catalog/frameworks'),

  // -------------------------------------------------------------- agent ----
  agentConversations: () => request<AgentConversation[]>('/api/agent/conversations'),
  agentConversation: (id: string) => request<AgentConversation>(`/api/agent/conversations/${id}`),
  createAgentConversation: (payload: { title?: string; model_api_id?: string } = {}) =>
    request<AgentConversation>('/api/agent/conversations', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateAgentConversation: (id: string, payload: { title?: string; model_api_id?: string }) =>
    request<AgentConversation>(`/api/agent/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteAgentConversation: (id: string) =>
    request<void>(`/api/agent/conversations/${id}`, { method: 'DELETE' }),
  agentMessages: (id: string) =>
    request<AgentMessage[]>(`/api/agent/conversations/${id}/messages`),
  sendAgentMessage: (id: string, content: string) =>
    request<AgentConversation>(`/api/agent/conversations/${id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  stopAgentConversation: (id: string) =>
    request<AgentConversation>(`/api/agent/conversations/${id}/stop`, { method: 'POST' }),
  uploadAgentAttachment: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<AgentConversation>(`/api/agent/conversations/${id}/attachments`, {
      method: 'POST',
      body: form,
    })
  },
  deleteAgentAttachment: (id: string, attachmentId: string) =>
    request<AgentConversation>(`/api/agent/conversations/${id}/attachments/${attachmentId}`, {
      method: 'DELETE',
    }),
  agentModels: () => request<AgentModelOption[]>('/api/agent/models'),
  agentSkills: () => request<AgentSkill[]>('/api/agent/skills'),
  agentTools: () => request<AgentTool[]>('/api/agent/tools'),
}

export function wsUrl(path: string): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getToken()
  return `${protocol}//${location.host}${path}${token ? `?token=${encodeURIComponent(token)}` : ''}`
}
