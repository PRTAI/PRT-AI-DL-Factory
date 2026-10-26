import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { GitCommitHorizontal, Github, KeyRound, Moon, Sun, UserCircle2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useUi } from '@/lib/store'
import type { UserApiKey } from '@/lib/types'
import { formatDateTime, formatRelative } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ErrorBox,
  Field,
  Input,
  KeyValue,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import { PageHeader } from '@/components/common'

export default function ProfileSettings() {
  const { user, setUser } = useAuth()
  const { theme, toggleTheme } = useUi()
  const [current, setCurrent] = React.useState('')
  const [next, setNext] = React.useState('')
  const [confirm, setConfirm] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)

  const activity = useQuery({
    queryKey: ['my-audit'],
    queryFn: () => api.audit({ limit: 60, actor_id: user?.id }),
    enabled: Boolean(user),
  })

  const change = useMutation({
    mutationFn: () => api.changePassword(current, next),
    onSuccess: (updated) => {
      toast.success('密码已更新')
      setUser(updated)
      setCurrent('')
      setNext('')
      setConfirm('')
    },
    onError: (exception) => setError(exception instanceof ApiError ? exception.message : '修改失败'),
  })

  if (!user) return null

  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit = current.length > 0 && next.length >= 8 && !mismatch

  return (
    <div className="space-y-4">
      <PageHeader title="个人设置" description="账号信息、密码、模型 API Key 与界面偏好" />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="账号信息" icon={<UserCircle2 className="h-4 w-4" aria-hidden />} />
          <CardBody className="divide-y divide-border/50">
            <KeyValue label="显示名称">{user.display_name}</KeyValue>
            <KeyValue label="邮箱">{user.email}</KeyValue>
            <KeyValue label="角色">
              <Badge tone={user.role === 'admin' ? 'primary' : 'neutral'}>
                {user.role === 'admin' ? '管理员' : '成员'}
              </Badge>
            </KeyValue>
            <KeyValue label="并发任务上限">{user.max_concurrent_jobs ?? '使用系统默认'}</KeyValue>
            <KeyValue label="单任务 GPU 上限">{user.max_gpus_per_job ?? '使用系统默认'}</KeyValue>
            <KeyValue label="创建时间">{formatDateTime(user.created_at)}</KeyValue>
            <KeyValue label="最近登录">{formatDateTime(user.last_login_at)}</KeyValue>
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="修改密码"
            icon={<KeyRound className="h-4 w-4" aria-hidden />}
            description={user.must_change_password ? '你还在使用管理员分发的初始密码，请立即修改' : undefined}
          />
          <CardBody className="space-y-4">
            <Field label="当前密码" required htmlFor="current-password">
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
              />
            </Field>
            <Field label="新密码" required htmlFor="new-password" hint="至少 8 位，建议包含大小写与数字">
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(event) => setNext(event.target.value)}
              />
            </Field>
            <Field
              label="确认新密码"
              required
              htmlFor="confirm-password"
              error={mismatch ? '两次输入的新密码不一致' : null}
            >
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
              />
            </Field>
            {error && <ErrorBox message={error} />}
            <Button
              variant="primary"
              disabled={!canSubmit}
              loading={change.isPending}
              onClick={() => {
                setError(null)
                change.mutate()
              }}
            >
              更新密码
            </Button>
          </CardBody>
        </Card>
      </div>

      <MyGitIdentity />

      <MyGitHub />

      <MyApiKeys />

      <Card>
        <CardHeader
          title="界面偏好"
          actions={
            <Button variant="outline" size="sm" onClick={toggleTheme}>
              {theme === 'dark' ? <Sun className="h-3.5 w-3.5" aria-hidden /> : <Moon className="h-3.5 w-3.5" aria-hidden />}
              切换到{theme === 'dark' ? '浅色' : '深色'}主题
            </Button>
          }
        />
        <CardBody className="text-xs text-fg-muted">
          当前主题：{theme === 'dark' ? '深色（默认，适合长时间盯盘）' : '浅色'}。主题偏好保存在本地浏览器中。
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="我的操作记录" description="最近 60 条" />
        <Table>
          <thead>
            <tr>
              <Th className="w-44">时间</Th>
              <Th className="w-40">动作</Th>
              <Th>摘要</Th>
            </tr>
          </thead>
          <tbody>
            {(activity.data ?? []).map((event) => (
              <Tr key={event.id}>
                <Td className="num text-[11px] text-fg-muted">{formatRelative(event.at)}</Td>
                <Td>
                  <Badge>{event.action}</Badge>
                </Td>
                <Td className="text-xs text-fg-muted">{event.summary}</Td>
              </Tr>
            ))}
            {!activity.data?.length && (
              <tr>
                <Td colSpan={3}>
                  <div className="py-8 text-center text-xs text-fg-subtle">暂无记录</div>
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      </Card>
    </div>
  )
}

function MyGitIdentity() {
  const { user, setUser } = useAuth()
  const [name, setName] = React.useState(user?.git_name ?? '')
  const [email, setEmail] = React.useState(user?.git_email ?? '')
  const save = useMutation({
    mutationFn: (values: { name: string; email: string }) => api.setGitIdentity(values.name, values.email),
    onSuccess: (updated) => {
      setUser(updated)
      setName(updated.git_name ?? '')
      setEmail(updated.git_email ?? '')
      toast.success(updated.git_email ? 'git 署名已保存' : '已清除 git 署名')
    },
    onError: (exception) => toast.error(exception instanceof ApiError ? exception.message : '保存失败'),
  })
  if (!user) return null
  const trimmedName = name.trim()
  const trimmedEmail = email.trim()
  const partial = Boolean(trimmedName) !== Boolean(trimmedEmail)
  const unchanged = trimmedName === (user.git_name ?? '') && trimmedEmail === (user.git_email ?? '')
  return (
    <Card>
      <CardHeader
        title="Git 署名"
        icon={<GitCommitHorizontal className="h-4 w-4" aria-hidden />}
        description="把问题修复合并到 main 时，合并提交用这里的 user.name / user.email；留空则用绑定的 GitHub 账号，再没有就用平台账号的名称和邮箱"
      />
      <CardBody>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            if (!partial && !unchanged) save.mutate({ name: trimmedName, email: trimmedEmail })
          }}
        >
          <Field label="user.name" htmlFor="git-name" className="min-w-[14rem] flex-1">
            <Input id="git-name" autoComplete="name" placeholder={user.display_name} value={name}
              onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="user.email" htmlFor="git-email" className="min-w-[14rem] flex-1"
            error={partial ? '名称和邮箱需要同时填写，或同时留空' : null}>
            <Input id="git-email" type="email" autoComplete="email" placeholder={user.email} value={email}
              onChange={(event) => setEmail(event.target.value)} />
          </Field>
          <Button type="submit" variant="primary" disabled={partial || unchanged} loading={save.isPending}>
            保存
          </Button>
          {user.git_email && (
            <Button type="button" variant="ghost" loading={save.isPending} onClick={() => save.mutate({ name: '', email: '' })}>
              清除
            </Button>
          )}
        </form>
      </CardBody>
    </Card>
  )
}

function MyGitHub() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [token, setToken] = React.useState('')
  const binding = useQuery({ queryKey: ['github-binding'], queryFn: api.githubBinding })
  const onError = (exception: unknown) => toast.error(exception instanceof ApiError ? exception.message : '操作失败')
  const refresh = () => qc.invalidateQueries({ queryKey: ['github-binding'] })

  // OAuth 回调跳回本页时带 ?github=ok|error
  React.useEffect(() => {
    const result = params.get('github')
    if (!result) return
    if (result === 'ok') toast.success('GitHub 账号已绑定')
    else toast.error(`GitHub 绑定失败：${params.get('message') ?? '未知错误'}`)
    setParams({}, { replace: true })
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const bindToken = useMutation({
    mutationFn: () => api.bindGithubToken(token.trim()),
    onSuccess: () => { toast.success('GitHub 账号已绑定'); setToken(''); refresh() },
    onError,
  })
  const oauth = useMutation({
    mutationFn: api.githubOauthStart,
    onSuccess: ({ url }) => { window.location.href = url },
    onError,
  })
  const unbind = useMutation({
    mutationFn: api.unbindGithub,
    onSuccess: () => { toast.success('已解绑 GitHub'); refresh() },
    onError,
  })

  const account = binding.data?.account
  return (
    <Card>
      <CardHeader
        title="GitHub 账号"
        icon={<Github className="h-4 w-4" aria-hidden />}
        description="管理员把问题修复合并到 main 时，用合并人自己绑定的 GitHub 账号推送（未单独设置 Git 署名时也用它署名）；未绑定则用平台本机的 git 凭据推送"
      />
      <CardBody className="space-y-4">
        {binding.error && <ErrorBox message={binding.error instanceof ApiError ? binding.error.message : '加载失败'} />}
        {account ? (
          <div className="flex flex-wrap items-center gap-3">
            {account.avatar_url && <img src={account.avatar_url} alt="" className="h-9 w-9 rounded-full border border-border" />}
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-fg">
                {account.name || account.login} <span className="text-fg-muted">@{account.login}</span>
              </div>
              <div className="text-[11px] text-fg-subtle">
                {account.email} · {account.source === 'oauth' ? 'OAuth 授权' : `Token ${account.token_masked}`}
                {account.scopes && ` · 权限 ${account.scopes}`} · 绑定于 {formatDateTime(account.updated_at)}
              </div>
            </div>
            <Button variant="ghost" size="sm" loading={unbind.isPending} onClick={() => unbind.mutate()}>
              解绑
            </Button>
          </div>
        ) : (
          !binding.isLoading && <Badge tone="warning">未绑定</Badge>
        )}
        <div className="flex flex-wrap items-end gap-3">
          {binding.data?.oauth_enabled && (
            <Button variant={account ? 'outline' : 'primary'} loading={oauth.isPending} onClick={() => oauth.mutate()}>
              <Github className="h-4 w-4" aria-hidden />
              {account ? '重新授权' : '使用 GitHub 授权绑定'}
            </Button>
          )}
          <form
            className="flex min-w-[20rem] flex-1 items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              if (token.trim()) bindToken.mutate()
            }}
          >
            <Input
              type="password"
              autoComplete="off"
              className="h-8 flex-1 text-xs"
              placeholder={account ? '粘贴新的 Personal Access Token 以替换' : '粘贴 Personal Access Token（需要 repo 写权限）'}
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
            <Button type="submit" size="sm" variant={binding.data?.oauth_enabled ? 'outline' : 'primary'} disabled={!token.trim()} loading={bindToken.isPending}>
              用 Token 绑定
            </Button>
          </form>
        </div>
      </CardBody>
    </Card>
  )
}

function MyApiKeys() {
  const keys = useQuery({ queryKey: ['my-api-keys'], queryFn: api.myApiKeys })
  return (
    <Card>
      <CardHeader
        title="我的 API Key"
        icon={<KeyRound className="h-4 w-4" aria-hidden />}
        description="平台不提供统一密钥：评测、样例审核调用模型时使用你自己在这里填写的密钥（同一平台下的模型共用一份）"
      />
      {keys.error && <ErrorBox message={keys.error instanceof ApiError ? keys.error.message : '加载失败'} />}
      <Table>
        <thead>
          <tr>
            <Th className="w-56">平台</Th>
            <Th>模型</Th>
            <Th className="w-[26rem]">密钥</Th>
          </tr>
        </thead>
        <tbody>
          {(keys.data ?? []).map((row) => (
            <ApiKeyRow key={row.target_id} row={row} />
          ))}
          {!keys.isLoading && !keys.data?.length && (
            <tr>
              <Td colSpan={3}>
                <div className="py-8 text-center text-xs text-fg-subtle">管理员还没有添加模型平台</div>
              </Td>
            </tr>
          )}
        </tbody>
      </Table>
    </Card>
  )
}

function ApiKeyRow({ row }: { row: UserApiKey }) {
  const qc = useQueryClient()
  const [value, setValue] = React.useState('')
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['my-api-keys'] })
    qc.invalidateQueries({ queryKey: ['model-apis'] })
    qc.invalidateQueries({ queryKey: ['model-platforms'] })
  }
  const onError = (exception: unknown) => toast.error(exception instanceof ApiError ? exception.message : '操作失败')
  const save = useMutation({
    mutationFn: () => api.setMyApiKey(row.target_id, value.trim()),
    onSuccess: () => {
      toast.success(`已保存「${row.name}」的密钥`)
      setValue('')
      refresh()
    },
    onError,
  })
  const clear = useMutation({
    mutationFn: () => api.deleteMyApiKey(row.target_id),
    onSuccess: () => {
      toast.success(`已清除「${row.name}」的密钥`)
      refresh()
    },
    onError,
  })
  return (
    <Tr>
      <Td>
        <div className="flex items-center gap-1.5 text-xs font-medium">
          {row.name}
          {!row.enabled && <Badge>已停用</Badge>}
        </div>
        <div className="text-[11px] text-fg-subtle">{row.vendor_label}</div>
      </Td>
      <Td className="text-[11px] text-fg-muted">
        {row.models.length ? row.models.slice(0, 6).join('、') + (row.models.length > 6 ? ` 等 ${row.models.length} 个` : '') : '—'}
      </Td>
      <Td>
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (value.trim()) save.mutate()
          }}
        >
          {row.has_api_key ? <Badge tone="success">{row.api_key_masked}</Badge> : <Badge tone="warning">未配置</Badge>}
          <Input
            type="password"
            autoComplete="off"
            className="h-7 flex-1 text-xs"
            placeholder={row.has_api_key ? '输入新密钥以替换' : '粘贴你的 API Key'}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <Button type="submit" size="sm" variant="primary" disabled={!value.trim()} loading={save.isPending}>
            保存
          </Button>
          {row.has_api_key && (
            <Button type="button" size="sm" variant="ghost" loading={clear.isPending} onClick={() => clear.mutate()}>
              清除
            </Button>
          )}
        </form>
      </Td>
    </Tr>
  )
}
