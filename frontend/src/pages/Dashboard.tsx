import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  CircleCheck,
  CircleX,
  Cpu,
  HardDrive,
  Hourglass,
  MemoryStick,
  Network,
  Pause,
  Play,
  ShieldQuestion,
  Zap,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { useSystemStream } from '@/lib/ws'
import type { Job } from '@/lib/types'
import { formatBytes, formatDuration, formatNumber, formatRate, elapsedSeconds } from '@/lib/utils'
import { Badge, Button, Card, CardBody, CardHeader, EmptyState, Progress, Stat, Table, Td, Th, Tr } from '@/components/ui'
import { JobTypeBadge, PageHeader, StatusBadge } from '@/components/common'
import { ProjectTag } from '@/components/ProjectTag'
import { JobCost, SpendDashboard } from '@/components/Billing'
import { Meter, UtilizationChart } from '@/components/charts'

const timeLabel = (value: string | number) =>
  new Date(Number(value) * 1000).toLocaleTimeString('zh-CN', { hour12: false })

export default function Dashboard() {
  const { user } = useAuth()
  const stream = useSystemStream()
  const dashboard = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.dashboard(),
    refetchInterval: 10_000,
  })

  // Seed the chart from the REST snapshot until the socket delivers history.
  const points = stream.points.length ? stream.points : (dashboard.data?.history ?? [])
  const metrics = stream.latest ?? dashboard.data?.metrics ?? null
  const gpuSnapshot = stream.gpu ?? dashboard.data?.gpu ?? null
  const counters = dashboard.data?.counters

  const chartData = React.useMemo(
    () =>
      points.map((point) => ({
        x: point.at,
        cpu: point.cpu,
        memory: point.memory,
        gpu: point.gpu,
        gpu_memory: point.gpu_memory,
      })),
    [points],
  )

  const activeJobs = dashboard.data?.active_jobs ?? []
  const recentJobs = dashboard.data?.recent_jobs ?? []

  return (
    <div className="space-y-4">
      <PageHeader
        title="数据大屏"
        description="集群实时负载与任务运行态势"
        actions={
          <div className="flex items-center gap-2">
            <Badge tone={stream.connected ? 'success' : 'warning'}>
              <span
                className={`h-1.5 w-1.5 rounded-full ${stream.connected ? 'bg-success pulse-live' : 'bg-warning'}`}
                aria-hidden
              />
              {stream.connected ? '实时连接' : '轮询模式'}
            </Badge>
            <Button variant="outline" size="sm" onClick={() => stream.setPaused(!stream.paused)}>
              {stream.paused ? (
                <>
                  <Play className="h-3.5 w-3.5" aria-hidden />
                  继续采样
                </>
              ) : (
                <>
                  <Pause className="h-3.5 w-3.5" aria-hidden />
                  暂停曲线
                </>
              )}
            </Button>
          </div>
        }
      />

      {/* ------------------------------------------------------- counters -- */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat
          label="运行中"
          value={counters?.running ?? '—'}
          tone={counters?.running ? 'success' : 'neutral'}
          icon={<Activity className="h-4 w-4" aria-hidden />}
          hint={<Link className="link" to="/jobs?status=running">查看任务</Link>}
        />
        <Stat label="排队中" value={counters?.queued ?? '—'} icon={<Hourglass className="h-4 w-4" aria-hidden />} />
        <Stat
          label="待审核"
          value={counters?.pending_review ?? '—'}
          tone={counters?.pending_review ? 'warning' : 'neutral'}
          icon={<ShieldQuestion className="h-4 w-4" aria-hidden />}
          hint={user?.role === 'admin' ? '需要管理员处理' : '等待管理员审核'}
        />
        <Stat label="已完成" value={counters?.succeeded ?? '—'} icon={<CircleCheck className="h-4 w-4" aria-hidden />} />
        <Stat label="失败" value={counters?.failed ?? '—'} tone={counters?.failed ? 'danger' : 'neutral'} icon={<CircleX className="h-4 w-4" aria-hidden />} />
        <Stat
          label="GPU 占用"
          value={gpuSnapshot ? `${gpuSnapshot.busy.length}/${gpuSnapshot.total}` : '—'}
          icon={<Zap className="h-4 w-4" aria-hidden />}
          hint={gpuSnapshot ? `空闲卡: ${gpuSnapshot.free.join(', ') || '无'}` : undefined}
        />
      </div>

      <SpendDashboard data={dashboard.data?.billing} loading={dashboard.isPending} error={dashboard.isError} />
      {/* ---------------------------------------------------- utilisation -- */}
      <div className="grid gap-3 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="CPU / 内存"
            icon={<Cpu className="h-4 w-4" aria-hidden />}
            description={
              metrics
                ? `${metrics.cpu.count} 逻辑核 · 负载 ${metrics.cpu.load.join(' / ')} · 内存 ${formatBytes(metrics.memory.used)} / ${formatBytes(metrics.memory.total)}`
                : '采集中…'
            }
            actions={
              metrics && (
                <div className="flex gap-3 text-right">
                  <div>
                    <div className="num text-lg font-semibold leading-none text-chart-1">{metrics.cpu.percent.toFixed(0)}%</div>
                    <div className="text-[10px] text-fg-subtle">CPU</div>
                  </div>
                  <div>
                    <div className="num text-lg font-semibold leading-none text-chart-2">{metrics.memory.percent.toFixed(0)}%</div>
                    <div className="text-[10px] text-fg-subtle">内存</div>
                  </div>
                </div>
              )
            }
          />
          <CardBody className="pb-2 pt-3">
            <UtilizationChart
              data={chartData}
              xFormatter={timeLabel}
              height={150}
              series={[
                { key: 'cpu', label: 'CPU %', color: 'var(--chart-1)' },
                { key: 'memory', label: '内存 %', color: 'var(--chart-2)' },
              ]}
            />
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="GPU 利用率 / 显存"
            icon={<Zap className="h-4 w-4" aria-hidden />}
            description={
              metrics?.gpu.count
                ? `${metrics.gpu.count} 张显卡 · 平均利用率 ${metrics.gpu.avg_utilization}%`
                : '未检测到 NVIDIA GPU'
            }
            actions={
              metrics?.gpu.count ? (
                <div className="flex gap-3 text-right">
                  <div>
                    <div className="num text-lg font-semibold leading-none text-chart-3">
                      {metrics.gpu.avg_utilization.toFixed(0)}%
                    </div>
                    <div className="text-[10px] text-fg-subtle">利用率</div>
                  </div>
                  <div>
                    <div className="num text-lg font-semibold leading-none text-chart-4">
                      {metrics.gpu.avg_memory_percent.toFixed(0)}%
                    </div>
                    <div className="text-[10px] text-fg-subtle">显存</div>
                  </div>
                </div>
              ) : null
            }
          />
          <CardBody className="pb-2 pt-3">
            <UtilizationChart
              data={chartData}
              xFormatter={timeLabel}
              height={150}
              series={[
                { key: 'gpu', label: 'GPU %', color: 'var(--chart-3)' },
                { key: 'gpu_memory', label: '显存 %', color: 'var(--chart-4)' },
              ]}
            />
          </CardBody>
        </Card>
      </div>

      {/* ------------------------------------------------------ gpu cards -- */}
      {metrics?.gpu.devices.length ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {metrics.gpu.devices.map((device) => {
            const owner = gpuSnapshot?.assignments?.[String(device.index)]
            return (
              <Card key={device.index}>
                <CardBody className="space-y-2.5">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">
                        GPU {device.index} · {device.name}
                      </div>
                      <div className="text-[11px] text-fg-subtle">
                        {device.temperature !== null ? `${device.temperature}℃` : '温度未知'}
                        {device.power_watts !== null &&
                          ` · ${device.power_watts}W / ${device.power_limit_watts ?? '—'}W`}
                      </div>
                    </div>
                    {owner ? (
                      <Link to={`/jobs/${owner}`}>
                        <Badge tone="primary">已分配</Badge>
                      </Link>
                    ) : (
                      <Badge tone="success">空闲</Badge>
                    )}
                  </div>
                  <Meter label="利用率" value={device.utilization ?? 0} detail={`${device.utilization ?? 0}%`} tone="var(--chart-3)" />
                  <Meter
                    label="显存"
                    value={device.memory_percent}
                    detail={`${formatBytes(device.memory_used)} / ${formatBytes(device.memory_total)}`}
                    tone="var(--chart-4)"
                  />
                  {device.processes.length > 0 && (
                    <div className="text-[11px] text-fg-subtle">
                      {device.processes.length} 个进程占用显存
                    </div>
                  )}
                </CardBody>
              </Card>
            )
          })}
        </div>
      ) : null}

      {/* ---------------------------------------------------- io + active -- */}
      <div className="grid gap-3 xl:grid-cols-[1fr_1.6fr]">
        <Card>
          <CardHeader title="磁盘与网络" icon={<HardDrive className="h-4 w-4" aria-hidden />} />
          <CardBody className="space-y-3">
            {metrics ? (
              <>
                <Meter
                  label="数据盘使用"
                  value={metrics.disk.percent}
                  detail={`${formatBytes(metrics.disk.used)} / ${formatBytes(metrics.disk.total)}`}
                  tone={metrics.disk.percent > 90 ? 'var(--danger)' : 'var(--chart-6)'}
                />
                {metrics.disk.percent > 90 && (
                  <p className="rounded-md border border-danger/30 bg-danger-soft px-2 py-1.5 text-[11px] text-danger">
                    数据盘剩余空间不足 10%，训练产物可能写入失败。
                  </p>
                )}
                <div className="grid grid-cols-2 gap-3 pt-1">
                  <div className="rounded-lg border border-border px-3 py-2">
                    <div className="flex items-center gap-1.5 text-[11px] text-fg-muted">
                      <HardDrive className="h-3 w-3" aria-hidden /> 磁盘 IO
                    </div>
                    <div className="num mt-1 text-xs">读 {formatRate(metrics.disk.read_bps)}</div>
                    <div className="num text-xs">写 {formatRate(metrics.disk.write_bps)}</div>
                  </div>
                  <div className="rounded-lg border border-border px-3 py-2">
                    <div className="flex items-center gap-1.5 text-[11px] text-fg-muted">
                      <Network className="h-3 w-3" aria-hidden /> 网络
                    </div>
                    <div className="num mt-1 text-xs">↓ {formatRate(metrics.network.recv_bps)}</div>
                    <div className="num text-xs">↑ {formatRate(metrics.network.sent_bps)}</div>
                  </div>
                </div>
                <div className="flex items-center gap-2 pt-1 text-[11px] text-fg-subtle">
                  <MemoryStick className="h-3 w-3" aria-hidden />
                  可用内存 {formatBytes(metrics.memory.available)} · Swap {metrics.memory.swap_percent}%
                </div>
              </>
            ) : (
              <div className="py-8 text-center text-xs text-fg-subtle">采集中…</div>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="正在运行的任务"
            description={`${activeJobs.length} 个任务处于排队 / 运行状态`}
            actions={
              <Link to="/jobs">
                <Button variant="ghost" size="sm">
                  全部任务
                </Button>
              </Link>
            }
          />
          {activeJobs.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>任务</Th>
                  <Th className="w-28">状态</Th>
                  <Th className="w-48">进度</Th>
                  <Th className="w-24">GPU</Th>
                  <Th className="w-24">已运行</Th>
                </tr>
              </thead>
              <tbody>
                {activeJobs.map((job) => (
                  <ActiveJobRow key={job.id} job={job} />
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              icon={<Activity className="h-6 w-6" aria-hidden />}
              title="当前没有运行中的任务"
              description="提交训练或评测任务后，这里会实时显示进度。"
              action={
                <Link to="/submit/train">
                  <Button variant="primary" size="sm">
                    提交训练任务
                  </Button>
                </Link>
              }
            />
          )}
        </Card>
      </div>

      {/* ----------------------------------------------------- recent ----- */}
      <Card>
        <CardHeader title="最近更新" description="按更新时间排序的最新 12 个任务" />
        <Table>
          <thead>
            <tr>
              <Th>任务</Th>
              <Th className="w-32">类型</Th>
              <Th className="w-24">状态</Th>
              <Th className="w-40">关键指标</Th>
              <Th className="w-40">提交人</Th>
            </tr>
          </thead>
          <tbody>
            {recentJobs.map((job) => (
              <Tr key={job.id}>
                <Td>
                  <Link to={`/jobs/${job.id}`} className="font-medium text-fg hover:text-primary">
                    {job.name}
                  </Link>
                  <ProjectTag projectId={job.project_id} linked={false} className="mt-0.5 flex" />
                  <JobCost job={job} />
                </Td>
                <Td>
                  <JobTypeBadge type={job.type} framework={job.framework} />
                </Td>
                <Td>
                  <StatusBadge status={job.status} />
                </Td>
                <Td className="num text-xs text-fg-muted">{keyMetric(job)}</Td>
                <Td className="text-xs text-fg-muted">{job.owner_email}</Td>
              </Tr>
            ))}
            {!recentJobs.length && (
              <tr>
                <Td colSpan={5}>
                  <div className="py-6 text-center text-xs text-fg-subtle">暂无任务</div>
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      </Card>
    </div>
  )
}

function ActiveJobRow({ job }: { job: Job }) {
  const elapsed = elapsedSeconds(job.runtime.started_at, job.runtime.finished_at)
  return (
    <Tr>
      <Td>
        <Link to={`/jobs/${job.id}`} className="block min-w-0">
          <div className="truncate text-sm font-medium text-fg hover:text-primary">{job.name}</div>
          <ProjectTag projectId={job.project_id} linked={false} className="mt-0.5" />
          <JobCost job={job} />
          <div className="mt-0.5">
            <JobTypeBadge type={job.type} framework={job.framework} />
          </div>
        </Link>
      </Td>
      <Td>
        <StatusBadge status={job.status} />
      </Td>
      <Td>
        <Progress value={job.progress.percent} showLabel tone={job.status === 'running' ? 'success' : 'info'} />
        <div className="mt-1 truncate text-[11px] text-fg-subtle">{job.progress.message || '—'}</div>
      </Td>
      <Td className="num text-xs">{job.resources.gpu_ids.length ? job.resources.gpu_ids.join(', ') : '—'}</Td>
      <Td className="num text-xs text-fg-muted">{formatDuration(elapsed)}</Td>
    </Tr>
  )
}

function keyMetric(job: Job): string {
  const metrics = job.metrics
  if (job.type === 'verify') {
    const entries = Object.entries(metrics.pass_at_k ?? {})
    if (entries.length) return entries.map(([key, value]) => `${key}=${(value * 100).toFixed(1)}%`).join(' · ')
    if (metrics.n_trials) return `${formatNumber(metrics.n_trials)} trials`
    return '—'
  }
  const parts: string[] = []
  if (metrics.last_loss !== null && metrics.last_loss !== undefined) parts.push(`loss ${metrics.last_loss.toFixed(4)}`)
  if (metrics.last_reward !== null && metrics.last_reward !== undefined)
    parts.push(`reward ${metrics.last_reward.toFixed(3)}`)
  return parts.join(' · ') || '—'
}
