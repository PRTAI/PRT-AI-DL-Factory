import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, ScrollText, ShieldCheck, Trash2, UserPlus, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import type { SystemSettings, User } from '@/lib/types'
import { formatDateTime, formatRelative } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Dialog,
  ErrorBox,
  Field,
  Input,
  NumberInput,
  Select,
  Skeleton,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Textarea,
  Th,
  ToggleRow,
  Tr,
} from '@/components/ui'
import { PageHeader } from '@/components/common'
import { HarborImagePanel } from '@/components/HarborImagePanel'
import { ModelApiPanel } from '@/components/ModelApiPanel'
import { PricingPanel } from '@/components/PricingPanel'

export default function Admin() {
  // /admin?tab=models 可以直接分享给要维护模型平台的人
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') ?? 'users'
  const setTab = (next: string) => {
    const params = new URLSearchParams(searchParams)
    if (next === 'users') params.delete('tab')
    else params.set('tab', next)
    setSearchParams(params, { replace: true })
  }
  return (
    <div className="space-y-4">
      <PageHeader title="平台管理" description="账号、审核、模型 API、系统参数与审计日志" />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="users">账号管理</TabsTrigger>
          <TabsTrigger value="review">待审核</TabsTrigger>
          <TabsTrigger value="models">模型 API</TabsTrigger>
          <TabsTrigger value="pricing">价格与折扣</TabsTrigger>
          <TabsTrigger value="settings">系统设置</TabsTrigger>
          <TabsTrigger value="harbor-image">Harbor 镜像</TabsTrigger>
          <TabsTrigger value="toolchain">工具链</TabsTrigger>
          <TabsTrigger value="audit">审计日志</TabsTrigger>
        </TabsList>
        <TabsContent value="harbor-image" className="mt-3"><HarborImagePanel /></TabsContent>
        <TabsContent value="users" className="mt-3">
          <UsersPanel />
        </TabsContent>
        <TabsContent value="review" className="mt-3">
          <ReviewPanel />
        </TabsContent>
        <TabsContent value="models" className="mt-3">
          <ModelApiPanel />
        </TabsContent>
        <TabsContent value="pricing" className="mt-3"><PricingPanel /></TabsContent>
        <TabsContent value="settings" className="mt-3">
          <SettingsPanel />
        </TabsContent>
        <TabsContent value="toolchain" className="mt-3">
          <ToolchainPanel />
        </TabsContent>
        <TabsContent value="audit" className="mt-3">
          <AuditPanel />
        </TabsContent>
      </Tabs>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
function UsersPanel() {
  const queryClient = useQueryClient()
  const { user: me } = useAuth()
  const users = useQuery({ queryKey: ['users'], queryFn: () => api.users() })
  const [createOpen, setCreateOpen] = React.useState(false)
  const [resetTarget, setResetTarget] = React.useState<User | null>(null)

  const update = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: Partial<User> }) => api.updateUser(id, payload),
    onSuccess: () => {
      toast.success('已更新')
      queryClient.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '更新失败'),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteUser(id),
    onSuccess: () => {
      toast.success('账号已删除')
      queryClient.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '删除失败'),
  })

  return (
    <Card>
      <CardHeader
        title="账号"
        description="平台不开放自助注册，账号由管理员创建并分发初始密码"
        icon={<ShieldCheck className="h-4 w-4" aria-hidden />}
        actions={
          <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>
            <UserPlus className="h-3.5 w-3.5" aria-hidden />
            新建账号
          </Button>
        }
      />
      {users.isLoading ? (
        <CardBody>
          <Skeleton className="h-32 w-full" />
        </CardBody>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>成员</Th>
              <Th className="w-28">角色</Th>
              <Th className="w-24">状态</Th>
              <Th className="w-44">配额</Th>
              <Th className="w-40">最近登录</Th>
              <Th className="w-44">操作</Th>
            </tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((user) => (
              <Tr key={user.id}>
                <Td>
                  <div className="font-medium text-fg">{user.display_name}</div>
                  <div className="text-[11px] text-fg-muted">{user.email}</div>
                  {user.note && <div className="mt-0.5 text-[11px] text-fg-subtle">{user.note}</div>}
                </Td>
                <Td>
                  <Select
                    value={user.role}
                    onValueChange={(value) =>
                      update.mutate({ id: user.id, payload: { role: value as User['role'] } })
                    }
                    className="w-24"
                    options={[
                      { value: 'user', label: '成员' },
                      { value: 'admin', label: '管理员' },
                    ]}
                  />
                </Td>
                <Td>
                  <Badge tone={user.active ? 'success' : 'neutral'}>{user.active ? '启用' : '停用'}</Badge>
                  {user.must_change_password && (
                    <div className="mt-1">
                      <Badge tone="warning">待改密</Badge>
                    </div>
                  )}
                </Td>
                <Td className="num text-[11px] text-fg-muted">
                  <div>并发任务：{user.max_concurrent_jobs ?? '默认'}</div>
                  <div>单任务 GPU：{user.max_gpus_per_job ?? '默认'}</div>
                </Td>
                <Td className="text-[11px] text-fg-muted">
                  {user.last_login_at ? formatRelative(user.last_login_at) : '从未登录'}
                  <div className="text-fg-subtle">创建于 {formatDateTime(user.created_at)}</div>
                </Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    <Button variant="ghost" size="sm" onClick={() => setResetTarget(user)}>
                      <KeyRound className="h-3.5 w-3.5" aria-hidden />
                      重置密码
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => update.mutate({ id: user.id, payload: { active: !user.active } })}
                      disabled={user.id === me?.id}
                    >
                      {user.active ? '停用' : '启用'}
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`删除账号 ${user.email}`}
                      disabled={user.id === me?.id}
                      onClick={() => {
                        if (confirm(`确认删除账号 ${user.email}？其历史任务记录会保留。`)) remove.mutate(user.id)
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                  </div>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      <CreateUserDialog open={createOpen} onOpenChange={setCreateOpen} />
      <ResetPasswordDialog user={resetTarget} onClose={() => setResetTarget(null)} />
    </Card>
  )
}

function CreateUserDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient()
  const [email, setEmail] = React.useState('')
  const [displayName, setDisplayName] = React.useState('')
  const [password, setPassword] = React.useState('')
  const [role, setRole] = React.useState<'admin' | 'user'>('user')
  const [maxJobs, setMaxJobs] = React.useState<number | null>(null)
  const [maxGpus, setMaxGpus] = React.useState<number | null>(null)
  const [note, setNote] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)

  const create = useMutation({
    mutationFn: () =>
      api.createUser({
        email: email.trim(),
        display_name: displayName.trim(),
        password,
        role,
        must_change_password: true,
        max_concurrent_jobs: maxJobs,
        max_gpus_per_job: maxGpus,
        note,
      }),
    onSuccess: (user) => {
      toast.success(`已创建账号 ${user.email}，请把初始密码发给对方`)
      queryClient.invalidateQueries({ queryKey: ['users'] })
      onOpenChange(false)
      setEmail('')
      setDisplayName('')
      setPassword('')
      setNote('')
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '创建失败'),
  })

  const randomPassword = () => {
    const alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%'
    const values = crypto.getRandomValues(new Uint32Array(14))
    setPassword([...values].map((value) => alphabet[value % alphabet.length]).join(''))
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="新建账号"
      description="创建后请把邮箱与初始密码告知成员，对方首次登录会被提示修改密码。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!email.includes('@') || password.length < 8}
            loading={create.isPending}
            onClick={() => {
              setError(null)
              create.mutate()
            }}
          >
            创建
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="邮箱" required>
            <Input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="member@team.com" />
          </Field>
          <Field label="显示名称" hint="留空则取邮箱前缀">
            <Input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          </Field>
        </div>
        <Field label="初始密码" required hint="至少 8 位">
          <div className="flex gap-2">
            <Input value={password} onChange={(event) => setPassword(event.target.value)} className="num" />
            <Button variant="outline" onClick={randomPassword} type="button">
              随机生成
            </Button>
          </div>
        </Field>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="角色">
            <Select
              value={role}
              onValueChange={(value) => setRole(value as 'admin' | 'user')}
              options={[
                { value: 'user', label: '成员', description: '提交任务、查看自己的日志' },
                { value: 'admin', label: '管理员', description: '审核任务、管理成员与系统' },
              ]}
            />
          </Field>
          <Field label="并发任务上限" hint="留空使用系统默认">
            <NumberInput value={maxJobs} min={1} onChange={setMaxJobs} />
          </Field>
          <Field label="单任务 GPU 上限" hint="留空使用系统默认">
            <NumberInput value={maxGpus} min={1} onChange={setMaxGpus} />
          </Field>
        </div>
        <Field label="备注">
          <Textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
        </Field>
        {error && <ErrorBox message={error} />}
      </div>
    </Dialog>
  )
}

function ResetPasswordDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const [password, setPassword] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)

  const reset = useMutation({
    mutationFn: () => api.resetPassword(user!.id, password, true),
    onSuccess: () => {
      toast.success('密码已重置，请把新密码告知成员')
      onClose()
      setPassword('')
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '重置失败'),
  })

  return (
    <Dialog
      open={Boolean(user)}
      onOpenChange={(open) => !open && onClose()}
      title={`重置密码：${user?.email ?? ''}`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="danger" disabled={password.length < 8} loading={reset.isPending} onClick={() => reset.mutate()}>
            确认重置
          </Button>
        </>
      }
    >
      <Field label="新密码" required hint="至少 8 位；重置后对方登录时会被要求再次修改">
        <Input value={password} onChange={(event) => setPassword(event.target.value)} className="num" />
      </Field>
      {error && <ErrorBox message={error} className="mt-3" />}
    </Dialog>
  )
}

/* -------------------------------------------------------------------------- */
function ReviewPanel() {
  const queryClient = useQueryClient()
  const jobs = useQuery({
    queryKey: ['jobs', 'pending_review'],
    queryFn: () => api.jobs({ status: ['pending_review'], limit: 100 }),
    refetchInterval: 10000,
  })

  const review = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) => api.reviewJob(id, approve, ''),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      queryClient.invalidateQueries({ queryKey: ['job-overview'] })
    },
  })

  const items = jobs.data?.items ?? []

  return (
    <Card>
      <CardHeader title="待审核任务" description="通过后任务才会占用 GPU；建议先在详情页查看生成的命令" />
      <Table>
        <thead>
          <tr>
            <Th>任务</Th>
            <Th className="w-28">类型</Th>
            <Th className="w-24">GPU</Th>
            <Th className="w-44">提交人</Th>
            <Th className="w-56">操作</Th>
          </tr>
        </thead>
        <tbody>
          {items.map((job) => (
            <Tr key={job.id}>
              <Td>
                <Link to={`/jobs/${job.id}`} className="font-medium text-fg hover:text-primary">
                  {job.name}
                </Link>
                {job.note && <div className="mt-0.5 line-clamp-2 text-[11px] text-fg-muted">{job.note}</div>}
              </Td>
              <Td>
                <Badge tone="primary">{job.framework}</Badge>
              </Td>
              <Td className="num text-xs">{job.resources.n_gpus}</Td>
              <Td className="text-xs text-fg-muted">
                {job.owner_email}
                <div className="text-fg-subtle">{formatRelative(job.created_at)}</div>
              </Td>
              <Td>
                <div className="flex gap-1">
                  <Link to={`/jobs/${job.id}`}>
                    <Button variant="outline" size="sm">
                      查看命令
                    </Button>
                  </Link>
                  <Button variant="success" size="sm" onClick={() => review.mutate({ id: job.id, approve: true })}>
                    通过
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => review.mutate({ id: job.id, approve: false })}>
                    驳回
                  </Button>
                </div>
              </Td>
            </Tr>
          ))}
          {!items.length && (
            <tr>
              <Td colSpan={5}>
                <div className="py-8 text-center text-xs text-fg-subtle">没有待审核的任务</div>
              </Td>
            </tr>
          )}
        </tbody>
      </Table>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
function SettingsPanel() {
  const queryClient = useQueryClient()
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => api.settings() })
  const [draft, setDraft] = React.useState<SystemSettings | null>(null)
  const value = draft ?? settings.data ?? null

  const save = useMutation({
    mutationFn: (payload: SystemSettings) => api.updateSettings(payload),
    onSuccess: () => {
      toast.success('系统设置已更新')
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      setDraft(null)
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '保存失败'),
  })

  if (!value) return <Skeleton className="h-48 w-full" />

  return (
    <Card>
      <CardHeader
        title="系统设置"
        description={value.updated_by ? `最近由 ${value.updated_by} 修改于 ${formatDateTime(value.updated_at)}` : '尚未修改过'}
        actions={
          <Button variant="primary" size="sm" loading={save.isPending} onClick={() => save.mutate(value)}>
            保存
          </Button>
        }
      />
      <CardBody className="space-y-4">
        <ToggleRow
          label="任务需要管理员审核"
          hint="关闭后成员提交的任务会直接进入排队队列，请谨慎"
          checked={value.require_approval}
          onCheckedChange={(checked) => setDraft({ ...value, require_approval: checked })}
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="全局并发任务数" hint="同时运行的任务上限">
            <NumberInput
              value={value.max_concurrent_jobs}
              min={1}
              onChange={(next) => setDraft({ ...value, max_concurrent_jobs: next ?? 1 })}
            />
          </Field>
          <Field label="单任务默认 GPU 上限" hint="成员未单独设置配额时生效">
            <NumberInput
              value={value.default_max_gpus_per_job}
              min={1}
              onChange={(next) => setDraft({ ...value, default_max_gpus_per_job: next ?? 1 })}
            />
          </Field>
        </div>
        <Field label="全局公告" hint="填写后会显示在所有页面顶部">
          <Textarea
            rows={2}
            value={value.banner}
            onChange={(event) => setDraft({ ...value, banner: event.target.value })}
            placeholder="例如：本周六 20:00 停机维护，请提前保存 checkpoint。"
          />
        </Field>
      </CardBody>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
function ToolchainPanel() {
  const toolchain = useQuery({ queryKey: ['toolchain'], queryFn: () => api.toolchain() })
  const info = useQuery({ queryKey: ['info'], queryFn: () => api.info() })

  return (
    <div className="space-y-3">
      <Card>
        <CardHeader title="框架与命令行工具" icon={<Wrench className="h-4 w-4" aria-hidden />} description="平台执行任务时依赖的外部程序" />
        <Table>
          <thead>
            <tr>
              <Th>组件</Th>
              <Th className="w-28">版本</Th>
              <Th>路径</Th>
              <Th className="w-28">状态</Th>
              <Th>说明</Th>
            </tr>
          </thead>
          <tbody>
            {(toolchain.data ?? []).map((row) => {
              const tone =
                row.status === 'ready' ? 'success' : row.status === 'missing_deps' ? 'warning' : 'danger'
              const label =
                row.status === 'ready'
                  ? '可用'
                  : row.status === 'missing_deps'
                    ? '依赖缺失'
                    : row.status === 'missing_python'
                      ? '未安装'
                      : '未知'
              return (
                <Tr key={row.key}>
                  <Td className="text-xs font-medium">{row.label}</Td>
                  <Td className="num text-xs">{row.version || '—'}</Td>
                  <Td className="num truncate text-[11px] text-fg-muted">
                    {row.root && <div>{row.root}</div>}
                    <div>{row.python}</div>
                  </Td>
                  <Td>
                    <Badge tone={tone}>{label}</Badge>
                    {row.flash_attn === false && (
                      <Badge tone="neutral" className="mt-1">
                        无 flash-attn
                      </Badge>
                    )}
                    {row.flash_attn === true && (
                      <Badge tone="info" className="mt-1">
                        flash-attn
                      </Badge>
                    )}
                  </Td>
                  <Td className="text-[11px] leading-relaxed text-fg-muted">{row.detail}</Td>
                </Tr>
              )
            })}
          </tbody>
        </Table>
      </Card>

      {info.data && (
        <Card>
          <CardHeader title="部署信息" />
          <CardBody className="grid gap-2 text-xs sm:grid-cols-2">
            <div className="flex justify-between gap-3">
              <span className="text-fg-muted">版本</span>
              <span className="num">{info.data.version}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-fg-muted">GPU 总数</span>
              <span className="num">{info.data.gpu_total}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-fg-muted">模型目录</span>
              <span className="num truncate">{info.data.models_root}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-fg-muted">数据目录</span>
              <span className="num truncate">{info.data.data_root}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-fg-muted">上传大小上限</span>
              <span className="num">{info.data.max_upload_mb > 0 ? `${info.data.max_upload_mb} MB` : '不限'}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-fg-muted">采样间隔</span>
              <span className="num">{info.data.monitor_interval_seconds}s</span>
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
function AuditPanel() {
  const [prefix, setPrefix] = React.useState('')
  const events = useQuery({
    queryKey: ['audit', prefix],
    queryFn: () => api.audit({ limit: 300, action_prefix: prefix || undefined }),
  })

  return (
    <Card>
      <CardHeader
        title="审计日志"
        icon={<ScrollText className="h-4 w-4" aria-hidden />}
        description="记录登录、任务提交/审核、账号变更等关键操作"
        actions={
          <Select
            value={prefix}
            onValueChange={setPrefix}
            className="w-40"
            options={[
              { value: '', label: '全部操作' },
              { value: 'auth', label: '登录相关' },
              { value: 'job', label: '任务相关' },
              { value: 'user', label: '账号相关' },
              { value: 'dataset', label: '数据集相关' },
              { value: 'model', label: '模型相关' },
            ]}
          />
        }
      />
      <Table>
        <thead>
          <tr>
            <Th className="w-44">时间</Th>
            <Th className="w-40">操作人</Th>
            <Th className="w-36">动作</Th>
            <Th>摘要</Th>
          </tr>
        </thead>
        <tbody>
          {(events.data ?? []).map((event) => (
            <Tr key={event.id}>
              <Td className="num text-[11px] text-fg-muted">{formatDateTime(event.at)}</Td>
              <Td className="text-xs">{event.actor_email}</Td>
              <Td>
                <Badge tone={event.action.includes('fail') || event.action.includes('reject') ? 'danger' : 'neutral'}>
                  {event.action}
                </Badge>
              </Td>
              <Td className="text-xs text-fg-muted">
                {event.summary}
                {event.target_type === 'job' && event.target_id && (
                  <Link to={`/jobs/${event.target_id}`} className="ml-2 text-primary underline underline-offset-2">
                    查看
                  </Link>
                )}
              </Td>
            </Tr>
          ))}
          {!events.data?.length && (
            <tr>
              <Td colSpan={4}>
                <div className="py-8 text-center text-xs text-fg-subtle">暂无记录</div>
              </Td>
            </tr>
          )}
        </tbody>
      </Table>
    </Card>
  )
}
