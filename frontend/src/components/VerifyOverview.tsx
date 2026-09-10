/**
 * 评测任务概览里的「小屏」：两张圆环图——各阶段有多少 trial、reward 怎么分布。
 * 数据来自 /trials 的 counts（阶段）与 metrics 的 reward_distribution（分数）。
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { TrialPhase } from '@/lib/types'
import { DonutChart, type DonutSlice } from './charts'
import { PHASE_META } from './TrialBoard'
import { Card, CardBody, CardHeader, Skeleton } from './ui'

const PHASE_COLORS: Record<TrialPhase, string> = {
  pending: 'var(--fg-subtle)',
  setup_environment: 'var(--info)',
  running_agent: 'var(--primary)',
  running_verifier: 'var(--warning)',
  success: 'var(--success)',
  failed: 'var(--danger)',
}
const PHASE_ORDER: TrialPhase[] = ['success', 'failed', 'running_agent', 'running_verifier', 'setup_environment', 'pending']

export function VerifyOverview({ jobId, live }: { jobId: string; live: boolean }) {
  const trials = useQuery({
    queryKey: ['job-trials', jobId, 1, '', ''],
    queryFn: () => api.jobTrials(jobId, { page: 1, page_size: 1 }),
    refetchInterval: live ? 5000 : false,
  })
  const metrics = useQuery({
    queryKey: ['job-metrics', jobId],
    queryFn: () => api.jobMetrics(jobId),
    refetchInterval: live ? 8000 : false,
  })

  if (trials.isLoading || metrics.isLoading) return <Skeleton className="h-64 w-full" />

  const counts = trials.data?.counts ?? {}
  const total = counts.total ?? 0
  const phaseSlices: DonutSlice[] = PHASE_ORDER.map((phase) => ({
    key: phase,
    label: PHASE_META[phase].label,
    value: counts[phase] ?? 0,
    color: PHASE_COLORS[phase],
  }))

  // 分数分布：reward 各取值的 trial 数 + 崩掉没打分的一档
  const distribution = metrics.data?.series?.reward_distribution ?? []
  const rewardSlices: DonutSlice[] = distribution
    .slice()
    .sort((a, b) => b[0] - a[0])
    .map(([reward, n], index) => ({
      key: `r${reward}`,
      label: `reward ${reward.toFixed(2)}${reward >= 1 ? '（通过）' : ''}`,
      value: n,
      color: reward >= 1 ? 'var(--success)' : reward > 0 ? 'var(--warning)' : index === 0 ? 'var(--chart-4)' : 'var(--chart-2)',
    }))
  // 失败里有 reward 的（agent 超时但 verifier 打了分）已经算进分数分布，别再记一次
  const unscored = Math.max(0, total - (counts.scored ?? distribution.reduce((sum, [, n]) => sum + n, 0)) - (counts.pending ?? 0)
    - (counts.setup_environment ?? 0) - (counts.running_agent ?? 0) - (counts.running_verifier ?? 0))
  if (unscored) rewardSlices.push({ key: 'errored', label: '异常（未打分）', value: unscored, color: 'var(--danger)' })
  const scored = rewardSlices.reduce((sum, s) => sum + s.value, 0)
  const passed = counts.passed ?? 0

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card>
        <CardHeader title="任务状态" description={`${total} 个 trial 现在各在哪一步`} />
        <CardBody>
          <DonutChart slices={phaseSlices} centerLabel="trial" centerValue={total} />
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="分数分布" description={scored ? `已出结果 ${scored} 个 · 通过 ${passed}` : '还没有 trial 出结果'} />
        <CardBody>
          <DonutChart
            slices={rewardSlices}
            centerLabel="通过率"
            centerValue={scored ? `${((passed / scored) * 100).toFixed(0)}%` : '—'}
          />
        </CardBody>
      </Card>
    </div>
  )
}
