import * as React from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { KeyRound, Moon, Sun, UserCircle2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useUi } from '@/lib/store'
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
      <PageHeader title="个人设置" description="账号信息、密码与界面偏好" />

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
