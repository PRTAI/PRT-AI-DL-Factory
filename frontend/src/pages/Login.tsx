import * as React from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Cpu, Layers, LineChart, LockKeyhole, Mail } from 'lucide-react'
import { ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { Button, ErrorBox, Field, Input } from '@/components/ui'

export default function Login() {
  const { user, loading, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = React.useState('')
  const [password, setPassword] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const [submitting, setSubmitting] = React.useState(false)

  if (!loading && user) {
    const from = (location.state as { from?: string } | null)?.from
    return <Navigate to={from && from !== '/login' ? from : '/'} replace />
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email.trim(), password)
      navigate('/', { replace: true })
    } catch (exception) {
      setError(exception instanceof ApiError ? exception.message : '登录失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-bg px-4 py-10">
      <div className="surface-grid pointer-events-none absolute inset-0" aria-hidden />
      <div
        className="pointer-events-none absolute -left-40 top-0 h-[420px] w-[420px] rounded-full blur-[120px]"
        style={{ background: 'var(--primary-soft)' }}
        aria-hidden
      />
      <div
        className="pointer-events-none absolute -right-32 bottom-0 h-[360px] w-[360px] rounded-full blur-[120px]"
        style={{ background: 'var(--info-soft)' }}
        aria-hidden
      />

      <div className="relative grid w-full max-w-4xl overflow-hidden rounded-2xl border border-border bg-card shadow-2xl md:grid-cols-[1.1fr_1fr]">
        {/* --------------------------------------------------- brand pane -- */}
        <div className="hidden flex-col justify-between gap-8 border-r border-border bg-bg-elevated p-8 md:flex">
          <div>
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary text-sm font-bold text-primary-fg">
                PF
              </div>
              <div>
                <div className="text-sm font-semibold">PRT AI DL Factory</div>
                <div className="text-[11px] text-fg-subtle">模型后训练 · Agent 评测一体化平台</div>
              </div>
            </div>
            <p className="mt-6 text-sm leading-relaxed text-fg-muted">
              统一提交 SFT / DPO / GRPO / DAPO 训练与 Harbor Agent 评测，
              自动生成框架命令、排队调度 GPU，并实时回传日志与指标。
            </p>
          </div>

          <ul className="space-y-3 text-xs text-fg-muted">
            <li className="flex items-start gap-2.5">
              <Layers className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
              <span>
                <b className="text-fg">多框架适配</b> —— LLaMA-Factory / verl / TRL / Harbor，
                模型与算法的兼容性直接在提交页给出提示。
              </span>
            </li>
            <li className="flex items-start gap-2.5">
              <LineChart className="mt-0.5 h-4 w-4 shrink-0 text-info" aria-hidden />
              <span>
                <b className="text-fg">全过程可观测</b> —— loss / reward / pass@k 曲线与实时日志，
                失败原因一眼可见。
              </span>
            </li>
            <li className="flex items-start gap-2.5">
              <Cpu className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden />
              <span>
                <b className="text-fg">资源可控</b> —— 管理员审核后自动排队，GPU 按卡分配，
                系统负载像任务管理器一样可视化。
              </span>
            </li>
          </ul>
        </div>

        {/* --------------------------------------------------- form pane --- */}
        <div className="p-8">
          <h1 className="text-lg font-semibold">登录</h1>
          <p className="mt-1 text-xs text-fg-muted">账号由管理员统一创建，使用邮箱登录。</p>

          <form className="mt-6 space-y-4" onSubmit={submit}>
            <Field label="邮箱" htmlFor="email" required>
              <div className="relative">
                <Mail className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
                <Input
                  id="email"
                  type="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@team.com"
                  className="pl-8"
                />
              </div>
            </Field>

            <Field label="密码" htmlFor="password" required>
              <div className="relative">
                <LockKeyhole className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="••••••••"
                  className="pl-8"
                />
              </div>
            </Field>

            {error && <ErrorBox message={error} />}

            <Button type="submit" variant="primary" size="lg" className="w-full justify-center" loading={submitting}>
              登录
            </Button>
          </form>

          <p className="mt-6 text-[11px] leading-relaxed text-fg-subtle">
            忘记密码或需要开通账号，请联系平台管理员。首次登录后请立即修改初始密码。
          </p>
        </div>
      </div>
    </div>
  )
}
