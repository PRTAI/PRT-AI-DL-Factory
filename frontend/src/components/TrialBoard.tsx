/**
 * 评测任务的逐 trial 进展：分页、按阶段筛选、点开看单个 trial 的完整日志。
 *
 * 阶段来自 Harbor 在 trial 目录里留下的文件（Docker bind-mount，实时出现），
 * 而不是 stdout —— Harbor 的进度条在非终端里一个字都不打。
 * 跑着的排最前，其次失败，再是完成，最后是还没开始的。
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Container,
  FlaskConical,
  Search,
} from 'lucide-react'
import { api } from '@/lib/api'
import type { TrialPhase, TrialState } from '@/lib/types'
import { cn, formatDuration, formatNumber } from '@/lib/utils'
import { Badge, Button, Card, CardBody, CardHeader, Dialog, EmptyState, Input, Skeleton, StatusDot, Table, Td, Th, Tr } from './ui'
import { CopyButton, Mono } from './common'

export const PHASE_META: Record<
  TrialPhase,
  { label: string; tone: 'neutral' | 'info' | 'primary' | 'warning' | 'success' | 'danger'; Icon: typeof Bot; live: boolean }
> = {
  pending: { label: '待开始', tone: 'neutral', Icon: CircleDashed, live: false },
  setup_environment: { label: '环境准备', tone: 'info', Icon: Container, live: true },
  running_agent: { label: 'Agent 运行', tone: 'primary', Icon: Bot, live: true },
  running_verifier: { label: '验证中', tone: 'warning', Icon: FlaskConical, live: true },
  success: { label: '完成', tone: 'success', Icon: CheckCircle2, live: false },
  failed: { label: '失败', tone: 'danger', Icon: AlertTriangle, live: false },
}

const FILTERS: { value: string; label: string; keys: string[] }[] = [
  { value: '', label: '全部', keys: ['total'] },
  { value: 'running', label: '进行中', keys: ['setup_environment', 'running_agent', 'running_verifier'] },
  { value: 'setup_environment', label: '环境准备', keys: ['setup_environment'] },
  { value: 'running_agent', label: 'Agent 运行', keys: ['running_agent'] },
  { value: 'running_verifier', label: '验证中', keys: ['running_verifier'] },
  { value: 'success', label: '完成', keys: ['success'] },
  { value: 'failed', label: '失败', keys: ['failed'] },
  { value: 'pending', label: '待开始', keys: ['pending'] },
]

const PAGE_SIZE = 10

export function TrialBoard({ jobId, live }: { jobId: string; live: boolean }) {
  const [page, setPage] = React.useState(1)
  const [phase, setPhase] = React.useState('')
  const [keyword, setKeyword] = React.useState('')
  const [debounced, setDebounced] = React.useState('')
  const [openTrial, setOpenTrial] = React.useState<string | null>(null)

  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(keyword.trim()), 250)
    return () => clearTimeout(timer)
  }, [keyword])
  React.useEffect(() => setPage(1), [phase, debounced])

  const trials = useQuery({
    queryKey: ['job-trials', jobId, page, phase, debounced],
    queryFn: () => api.jobTrials(jobId, { page, page_size: PAGE_SIZE, phase, q: debounced }),
    refetchInterval: live ? 5000 : false,
    placeholderData: (previous) => previous,
  })
  const data = trials.data
  const counts = data?.counts ?? {}
  const countOf = (keys: string[]) => keys.reduce((sum, key) => sum + (counts[key] ?? 0), 0)

  return (
    <Card>
      <CardHeader
        title="任务进展"
        description={
          data
            ? `${formatNumber(counts.total ?? 0)} 个 trial · 通过 ${formatNumber(counts.passed ?? 0)} · 失败 ${formatNumber(counts.failed ?? 0)}${live ? ' · 每 5 秒刷新' : ''}`
            : '按 trial 目录里的文件推断每个任务到了哪一步'
        }
        actions={data && <CopyButton value={data.job_dir} label="复制结果目录" />}
      />
      <CardBody className="space-y-3">
        {/* 阶段筛选 —— 数字就是各阶段的 trial 数，一眼看出瓶颈在哪 */}
        <div className="flex flex-wrap items-center gap-1.5">
          {FILTERS.map((filter) => {
            const n = countOf(filter.keys)
            const active = phase === filter.value
            return (
              <button
                key={filter.value}
                type="button"
                aria-pressed={active}
                onClick={() => setPhase(filter.value)}
                className={cn(
                  'flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors',
                  active ? 'border-primary bg-primary-soft font-medium text-primary' : 'border-border text-fg-muted hover:border-border-strong hover:text-fg',
                )}
              >
                {filter.label}
                <span className="num text-[11px] text-fg-subtle">{n}</span>
              </button>
            )
          })}
          <div className="relative ml-auto w-56">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
            <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="任务名 / 异常类型" className="h-8 pl-8 text-xs" aria-label="搜索 trial" />
          </div>
        </div>

        {trials.isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : !data?.items.length ? (
          <EmptyState
            title={counts.total ? '这个筛选下没有 trial' : '还没有 trial 开始'}
            description={counts.total ? '换个阶段看看。' : 'Harbor 启动并写入第一个 trial 目录后，这里会自动出现。'}
          />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>任务</Th>
                  <Th className="w-[150px]">阶段</Th>
                  <Th className="w-[90px]">reward</Th>
                  <Th className="w-[260px]">阶段耗时</Th>
                  <Th className="w-[90px]">总耗时</Th>
                  <Th>说明</Th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((trial) => (
                  <TrialRow key={trial.trial_name} trial={trial} onOpen={() => setOpenTrial(trial.trial_name)} />
                ))}
              </tbody>
            </Table>
            <div className="flex items-center justify-between text-xs text-fg-muted">
              <span>
                第 {data.page} / {data.pages} 页 · 共 {data.total} 条
              </span>
              <div className="flex items-center gap-1">
                <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                  <ChevronLeft className="h-4 w-4" aria-hidden />
                  上一页
                </Button>
                <Button variant="ghost" size="sm" disabled={page >= data.pages} onClick={() => setPage((p) => p + 1)}>
                  下一页
                  <ChevronRight className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            </div>
          </>
        )}
      </CardBody>
      <TrialDetailDialog jobId={jobId} trialName={openTrial} live={live} onClose={() => setOpenTrial(null)} />
    </Card>
  )
}

const PHASE_SECONDS_ORDER: [string, string][] = [
  ['setup_environment', '环境'],
  ['agent_setup', '安装'],
  ['running_agent', 'agent'],
  ['running_verifier', '验证'],
]

function TrialRow({ trial, onOpen }: { trial: TrialState; onOpen: () => void }) {
  const meta = PHASE_META[trial.phase] ?? PHASE_META.pending
  const clickable = trial.phase !== 'pending'
  return (
    <Tr className={cn(clickable && 'cursor-pointer')} onClick={clickable ? onOpen : undefined}>
      <Td>
        <div className="num text-xs font-medium text-fg">{trial.task_name}</div>
        <div className="num truncate text-[11px] text-fg-subtle">{trial.trial_name}</div>
      </Td>
      <Td>
        <StatusDot tone={meta.tone} label={meta.label} pulse={meta.live} />
        {trial.detail && trial.detail !== meta.label && <div className="mt-0.5 text-[11px] text-fg-subtle">{trial.detail}</div>}
      </Td>
      <Td>
        {trial.reward === null ? (
          <span className="text-xs text-fg-subtle">—</span>
        ) : (
          <Badge tone={trial.reward >= 1 ? 'success' : trial.reward > 0 ? 'warning' : 'danger'}>{trial.reward.toFixed(2)}</Badge>
        )}
      </Td>
      <Td>
        <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[11px] text-fg-muted">
          {PHASE_SECONDS_ORDER.filter(([key]) => trial.phase_seconds[key] !== undefined).map(([key, label]) => (
            <span key={key} className={cn(trial.phase === key && 'font-medium text-fg')}>
              {label} <span className="num">{formatDuration(trial.phase_seconds[key])}</span>
            </span>
          ))}
          {!Object.keys(trial.phase_seconds).length && <span className="text-fg-subtle">—</span>}
        </div>
      </Td>
      <Td className="num text-xs text-fg-muted">{formatDuration(trial.duration_seconds)}</Td>
      <Td className="text-[11px]">
        {trial.exception_type ? (
          <>
            <Badge tone="danger">{trial.exception_type}</Badge>
            <div className="mt-0.5 line-clamp-2 text-fg-muted">{trial.exception_message}</div>
          </>
        ) : trial.phase === 'success' ? (
          <span className={trial.passed ? 'text-success' : 'text-fg-muted'}>{trial.passed ? '通过' : '未通过'}</span>
        ) : (
          <span className="text-fg-subtle">{trial.model || '—'}</span>
        )}
      </Td>
    </Tr>
  )
}

function TrialDetailDialog({
  jobId,
  trialName,
  live,
  onClose,
}: {
  jobId: string
  trialName: string | null
  live: boolean
  onClose: () => void
}) {
  const detail = useQuery({
    queryKey: ['job-trial', jobId, trialName],
    queryFn: () => api.jobTrialDetail(jobId, trialName!),
    enabled: Boolean(trialName),
    refetchInterval: live ? 5000 : false,
  })
  const [tab, setTab] = React.useState<'trial' | 'agent' | 'verifier' | 'exception'>('trial')
  React.useEffect(() => setTab('trial'), [trialName])
  const d = detail.data
  const meta = d ? PHASE_META[d.trial.phase] : null

  const sections: { key: typeof tab; label: string; content: string; available: boolean }[] = d
    ? [
        { key: 'trial', label: 'trial.log', content: d.trial_log, available: Boolean(d.trial_log) },
        {
          key: 'agent',
          label: `agent 输出${d.agent_logs.length ? ` (${d.agent_logs.map((l) => l.name).join(', ')})` : ''}`,
          content: d.agent_logs.map((l) => `===== ${l.name} =====\n${l.content}`).join('\n\n'),
          available: d.agent_logs.length > 0,
        },
        {
          key: 'verifier',
          label: 'verifier 输出',
          content: d.verifier_logs.map((l) => `===== ${l.name} =====\n${l.content}`).join('\n\n'),
          available: d.verifier_logs.length > 0,
        },
        { key: 'exception', label: '异常栈', content: d.exception, available: Boolean(d.exception) },
      ]
    : []
  const current = sections.find((s) => s.key === tab)

  return (
    <Dialog
      open={Boolean(trialName)}
      onOpenChange={(open) => !open && onClose()}
      title={d ? d.trial.task_name : trialName ?? ''}
      description={
        d && meta ? (
          <span className="flex flex-wrap items-center gap-2">
            <StatusDot tone={meta.tone} label={meta.label} pulse={meta.live} />
            {d.trial.reward !== null && <Badge tone={d.trial.reward >= 1 ? 'success' : 'danger'}>reward {d.trial.reward.toFixed(2)}</Badge>}
            {d.trial.exception_type && <Badge tone="danger">{d.trial.exception_type}</Badge>}
            <Mono>{d.trial.trial_name}</Mono>
            <span className="text-fg-subtle">{d.trial.agent}{d.trial.model && ` · ${d.trial.model}`}</span>
          </span>
        ) : undefined
      }
      width="max-w-5xl"
    >
      {detail.isLoading || !d ? (
        <Skeleton className="h-64 w-full" />
      ) : (
        <div className="space-y-3">
          <div className="grid gap-2 text-xs sm:grid-cols-4">
            {PHASE_SECONDS_ORDER.map(([key, label]) => (
              <div key={key} className="rounded-md border border-border bg-bg-elevated px-3 py-2">
                <div className="text-[11px] text-fg-subtle">{label}</div>
                <div className="num text-sm text-fg">{d.trial.phase_seconds[key] !== undefined ? formatDuration(d.trial.phase_seconds[key]) : '—'}</div>
              </div>
            ))}
          </div>
          {Object.keys(d.trial.tokens).length > 0 && (
            <p className="text-[11px] text-fg-muted">
              tokens：{Object.entries(d.trial.tokens).map(([k, v]) => `${k} ${formatNumber(v)}`).join(' · ')}
            </p>
          )}
          <div className="flex flex-wrap gap-1">
            {sections.map((s) => (
              <button
                key={s.key}
                type="button"
                disabled={!s.available}
                aria-pressed={tab === s.key}
                onClick={() => setTab(s.key)}
                className={cn(
                  'rounded-md border px-2.5 py-1 text-xs transition-colors disabled:opacity-40',
                  tab === s.key ? 'border-primary bg-primary-soft text-primary' : 'border-border text-fg-muted hover:text-fg',
                )}
              >
                {s.label}
              </button>
            ))}
            <span className="ml-auto text-[11px] text-fg-subtle">目录 {d.files.length} 个文件</span>
          </div>
          <pre className="num max-h-[50vh] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg-elevated p-3 text-[11px] leading-relaxed text-fg-muted">
            {current?.content || '（暂无内容）'}
          </pre>
          <p className="text-[11px] text-fg-subtle">
            <Mono>{d.trial.path}</Mono>
          </p>
        </div>
      )}
    </Dialog>
  )
}
