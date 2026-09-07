import * as React from 'react'
import {
  Ban,
  CheckCircle2,
  CircleDashed,
  Clock,
  FlaskConical,
  GraduationCap,
  Hourglass,
  Loader2,
  ShieldQuestion,
  Sparkles,
  XCircle,
} from 'lucide-react'
import { Badge } from './ui'
import type { Framework, JobStatus, JobType } from '@/lib/types'
import { cn, copyText } from '@/lib/utils'
import { toast } from 'sonner'

/* -------------------------------------------------------------------------- */
/* Status                                                                      */
/* -------------------------------------------------------------------------- */
const STATUS_META: Record<
  JobStatus,
  { label: string; tone: 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'info'; Icon: typeof Clock }
> = {
  draft: { label: '草稿', tone: 'neutral', Icon: CircleDashed },
  pending_review: { label: '待审核', tone: 'warning', Icon: ShieldQuestion },
  rejected: { label: '已驳回', tone: 'danger', Icon: Ban },
  queued: { label: '排队中', tone: 'info', Icon: Hourglass },
  preparing: { label: '准备中', tone: 'info', Icon: Loader2 },
  running: { label: '运行中', tone: 'success', Icon: Loader2 },
  succeeded: { label: '已完成', tone: 'success', Icon: CheckCircle2 },
  failed: { label: '失败', tone: 'danger', Icon: XCircle },
  cancelled: { label: '已终止', tone: 'neutral', Icon: Ban },
}

export function StatusBadge({ status, className }: { status: JobStatus; className?: string }) {
  const meta = STATUS_META[status] ?? STATUS_META.draft
  const spinning = status === 'running' || status === 'preparing'
  return (
    <Badge tone={meta.tone} className={className}>
      <meta.Icon className={cn('h-3 w-3', spinning && 'animate-spin')} aria-hidden />
      {meta.label}
    </Badge>
  )
}

export function statusLabel(status: JobStatus): string {
  return STATUS_META[status]?.label ?? status
}

export const JOB_STATUS_OPTIONS: { value: JobStatus; label: string }[] = (
  Object.keys(STATUS_META) as JobStatus[]
).map((status) => ({ value: status, label: STATUS_META[status].label }))

/* -------------------------------------------------------------------------- */
/* Job type / framework                                                        */
/* -------------------------------------------------------------------------- */
const TYPE_META: Record<JobType, { label: string; Icon: typeof GraduationCap; tone: 'primary' | 'info' | 'success' }> = {
  sft: { label: '监督训练', Icon: GraduationCap, tone: 'primary' },
  rl: { label: '强化学习', Icon: Sparkles, tone: 'info' },
  verify: { label: 'Agent 评测', Icon: FlaskConical, tone: 'success' },
}

export function JobTypeBadge({ type, framework }: { type: JobType; framework?: Framework }) {
  const meta = TYPE_META[type]
  return (
    <Badge tone={meta.tone}>
      <meta.Icon className="h-3 w-3" aria-hidden />
      {meta.label}
      {framework && <span className="text-fg-subtle">· {FRAMEWORK_LABEL[framework]}</span>}
    </Badge>
  )
}

export const FRAMEWORK_LABEL: Record<Framework, string> = {
  llamafactory: 'LLaMA-Factory',
  verl: 'verl',
  trl: 'TRL',
  harbor: 'Harbor',
}

export const JOB_TYPE_LABEL: Record<JobType, string> = {
  sft: '监督训练',
  rl: '强化学习',
  verify: 'Agent 评测',
}

/* -------------------------------------------------------------------------- */
/* Misc                                                                        */
/* -------------------------------------------------------------------------- */
export function CopyButton({ value, label = '复制', className }: { value: string; label?: string; className?: string }) {
  return (
    <button
      type="button"
      className={cn(
        'rounded-md border border-border px-2 py-1 text-[11px] text-fg-muted transition-colors hover:bg-muted hover:text-fg',
        className,
      )}
      onClick={async () => {
        const ok = await copyText(value)
        if (ok) toast.success('已复制到剪贴板')
        else toast.error('复制失败，请手动选择文本')
      }}
    >
      {label}
    </button>
  )
}

export function Mono({ children, className }: { children: React.ReactNode; className?: string }) {
  return <code className={cn('num rounded bg-muted px-1 py-0.5 text-[11px] text-fg', className)}>{children}</code>
}

export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: string
  description?: React.ReactNode
  actions?: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-lg font-semibold tracking-tight text-fg">{title}</h1>
        {description && <p className="mt-0.5 text-xs text-fg-muted">{description}</p>}
        {children}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

export function InlineHint({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <p className={cn('rounded-lg border border-info/30 bg-info-soft px-3 py-2 text-xs leading-relaxed text-info', className)}>
      {children}
    </p>
  )
}

export function WarnHint({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <p
      className={cn(
        'rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-xs leading-relaxed text-warning',
        className,
      )}
    >
      {children}
    </p>
  )
}
