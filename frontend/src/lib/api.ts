import type {
  AlgorithmInfo,
  AuditEvent,
  CommandPreview,
  CompatSection,
  DashboardPayload,
  Dataset,
  DatasetKind,
  DatasetSpec,
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
  Preset,
  SystemMetrics,
  SystemSettings,
  TokenResponse,
  ToolchainRow,
  User,
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
    return detail
      .map((item) => {
        const location = Array.isArray(item?.loc) ? item.loc.slice(1).join('.') : ''
        return location ? `${location}: ${item.msg}` : item.msg
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

  // -------------------------------------------------------------- jobs ----
  jobs: (params: {
    mine_only?: boolean
    status?: JobStatus[]
    type?: JobType[]
    q?: string
    limit?: number
    offset?: number
  } = {}) =>
    request<{ items: Job[]; total: number; offset: number; limit: number }>(`/api/jobs${qs(params)}`),
  jobOverview: () =>
    request<{
      total: number
      by_status: Record<string, number>
      by_type: Record<string, number>
      pending_review: number
      running: number
      queued: number
      gpu: GpuSnapshot
    }>('/api/jobs/overview'),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  createJob: (payload: unknown) =>
    request<Job>('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),
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
  jobTrials: (id: string) =>
    request<{
      trials: {
        task_name: string
        trial_name: string
        reward: number | null
        rewards: Record<string, number>
        exception_type: string | null
        exception_message: string
        agent: string
        model: string
        duration_seconds: number | null
        path: string
      }[]
      job_dir: string
      total: number
      resolved: number
      errored: number
    }>(`/api/jobs/${id}/trials`),
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
  reviewJob: (id: string, approve: boolean, comment = '') =>
    request<Job>(`/api/jobs/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({ approve, comment }),
    }),
  cancelJob: (id: string, reason = '') =>
    request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST', body: JSON.stringify({ reason }) }),
  resubmitJob: (id: string) => request<Job>(`/api/jobs/${id}/resubmit`, { method: 'POST' }),
  verifySuggestion: (id: string) =>
    request<{ parent_job_id: string; model_path: string; checkpoints: string[]; hint: string }>(
      `/api/jobs/${id}/verify-suggestion`,
    ),
  deleteJob: (id: string, purgeOutputs = false) =>
    request<void>(`/api/jobs/${id}${qs({ purge_outputs: purgeOutputs })}`, { method: 'DELETE' }),

  // ---------------------------------------------------------- datasets ----
  datasets: (params: { kind?: DatasetKind; mine_only?: boolean } = {}) =>
    request<Dataset[]>(`/api/datasets${qs(params)}`),
  dataset: (id: string) => request<Dataset>(`/api/datasets/${id}`),
  datasetPreview: (id: string) =>
    request<{ kind: string; file?: string; records?: unknown[]; tasks?: unknown[] }>(
      `/api/datasets/${id}/preview`,
    ),
  datasetSpecs: () => request<{ version: number; specs: DatasetSpec[] }>('/api/datasets/specs'),
  uploadDataset: (form: FormData, onProgress?: (percent: number) => void) =>
    new Promise<Dataset>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/datasets')
      const token = getToken()
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      }
      xhr.onload = () => {
        const payload = safeParse(xhr.responseText)
        if (xhr.status >= 200 && xhr.status < 300) resolve(payload as Dataset)
        else reject(new ApiError(xhr.status, extractDetail(payload) ?? `上传失败 (${xhr.status})`))
      }
      xhr.onerror = () => reject(new ApiError(0, '网络错误，上传失败'))
      xhr.send(form)
    }),
  updateDataset: (id: string, payload: { name?: string; description?: string; tags?: string[] }) =>
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

  // ------------------------------------------------------------ system ----
  systemMetrics: () => request<SystemMetrics>('/api/system/metrics'),
  systemHistory: (limit = 300) =>
    request<{ points: MetricPoint[]; interval_seconds: number }>(
      `/api/system/metrics/history${qs({ limit })}`,
    ),
  dashboard: () => request<DashboardPayload>('/api/system/dashboard'),
  toolchain: () => request<ToolchainRow[]>('/api/system/toolchain'),
  settings: () => request<SystemSettings>('/api/system/settings'),
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

  // ----------------------------------------------------------- catalog ----
  algorithms: () => request<AlgorithmInfo[]>('/api/catalog/algorithms'),
  presets: () =>
    request<{ sft_presets: Preset[]; rl_presets: Preset[]; verify_presets: Preset[] }>(
      '/api/catalog/presets',
    ),
  templates: () => request<string[]>('/api/catalog/templates'),
  harborAgents: () => request<HarborAgentInfo[]>('/api/catalog/harbor-agents'),
  harborEnvironments: () => request<string[]>('/api/catalog/harbor-environments'),
  frameworks: () => request<Record<string, Record<string, unknown>>>('/api/catalog/frameworks'),
}

export function wsUrl(path: string): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = getToken()
  return `${protocol}//${location.host}${path}${token ? `?token=${encodeURIComponent(token)}` : ''}`
}
