import { Link } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useAuth } from '@/lib/store'
import type { Job, SpendRow, SpendSummary } from '@/lib/types'
import { BILLING_UNAVAILABLE, jobCostState, money, spendChartRows } from '@/lib/billing'
import { Card, CardBody, CardHeader, Table, Tabs, TabsContent, TabsList, TabsTrigger, Td, Th, Tr } from '@/components/ui'

export function JobCost({ job, updating = false }: { job: Job; updating?: boolean }) {
  if (job.type !== 'verify') return null
  const { label, hint } = jobCostState(job)
  return <span className="mt-0.5 block text-[11px] text-fg-muted" title={hint}>{label}{updating && ' · 更新中'}</span>
}

function SourceLink({ source }: { source: string }) {
  return /^https?:\/\//i.test(source) ? <a className="link" href={source} target="_blank" rel="noreferrer">官方来源</a> : <span>{source}</span>
}

export function BillingDetail({ job }: { job: Job }) {
  if (job.type !== 'verify') return null
  const billing = job.billing
  const snapshot = billing?.snapshot
  return <Card>
    <CardHeader title="API 预估费用" description="只计算已记录的模型 Token；不含工具、缓存存储、订阅与本地 GPU 费用。" />
    <CardBody className="space-y-3 text-xs">
      {!billing && <p role="status" className="text-warning">{BILLING_UNAVAILABLE}</p>}
      <div className="flex flex-wrap gap-6">
        <span>已记录费用 <strong className="num text-lg">{money(billing?.amount_cny)}</strong></span>
        <span>Agent {money(billing?.agent_cny)}</span><span>Judge {money(billing?.judge_cny)}</span>
        <span>有用量记录 {billing?.requests ?? 0} 项</span>
      </div>
      {snapshot && <>
        <p className="text-fg-muted">固定汇率：1 USD = {snapshot.usd_cny} CNY（{snapshot.exchange_date}，<SourceLink source={snapshot.exchange_source} />）。{snapshot.historical ? '历史任务按首次统计时价格估算。' : '已保存任务价格快照。'}</p>
        <div className="grid gap-2 md:grid-cols-2">
          {(['agent', 'judge'] as const).map((phase) => {
            const component = snapshot[phase]
            const rate = component.price
            return <div key={phase} className="rounded border border-border p-2">
              <div className="font-medium">{phase === 'agent' ? 'Agent' : 'Judge'} · {component.model || '题包内配置'}</div>
              {component.free ? <p className="text-fg-muted">无外部 API Token 费用</p> : rate ? <>
                <p className="text-fg-muted">{rate.vendor} · {rate.official_model || rate.model} · 折扣系数 {rate.discount} · {rate.region || '默认地区'}</p>
                <p>以下单价为 {rate.currency} / 百万 Token，折扣另乘。</p>
                {rate.tiers.map((tier, i) => <p key={i}>输入 ≤ {tier.max_input?.toLocaleString() ?? '不限'}：输入 {tier.input} / 输出 {tier.output} / 缓存读 {(component.cache_mode === 'explicit' && rate.vendor === 'alibaba' ? tier.explicit_cache_read : tier.cache_read) ?? '待配置'} / 缓存写 {tier.cache_write ?? '待配置'}</p>)}
                <p><SourceLink source={rate.source} /> · 核验于 {rate.verified_on}{rate.valid_until && ` · 有效至 ${rate.valid_until}`}</p>
                {rate.note && <p className="text-fg-muted">{rate.note}</p>}
              </> : <p className="text-warning">价格待配置</p>}
            </div>
          })}
        </div>
      </>}
      {!!billing?.warnings.length && <ul className="list-disc space-y-1 pl-4 text-fg-muted">{billing.warnings.map((note) => <li key={note}>{note}</li>)}</ul>}
    </CardBody>
  </Card>
}

function SpendChart({ rows, title, color }: { rows: SpendRow[]; title: string; color: string }) {
  const chart = spendChartRows(rows)
  if (!chart.length) return <div className="flex h-80 items-center justify-center rounded-md bg-bg-elevated p-4 text-center text-xs text-fg-muted">
    {rows.some(row => row.amount_cny != null) ? '已统计的费用为 ¥0.00' : '暂无可计价的用量，收到费用记录后显示图表'}
  </div>
  return <div role="img" aria-label={`${title}费用排行，单位人民币，最多显示前 8 项`} className="h-80 min-w-0">
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={chart} layout="vertical" margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={{ fill: 'var(--fg-subtle)', fontSize: 10 }} tickFormatter={value => money(Number(value))} axisLine={false} tickLine={false} />
        <YAxis type="category" dataKey="id" width={32} tick={{ fill: 'var(--fg-muted)', fontSize: 11 }} axisLine={false} tickLine={false}
          tickFormatter={id => `#${chart.findIndex(row => row.id === id) + 1}`} />
        <Tooltip cursor={{ fill: 'var(--bg-elevated)' }} content={({ active, payload }) => {
          if (!active || !payload?.length) return null
          const row = payload[0].payload as SpendRow
          return <div className="max-w-80 rounded-lg border border-border bg-card p-3 text-xs shadow-xl">
            <p className="break-all font-medium">{row.name}</p>
            <p className="num mt-1">{money(row.amount_cny)} · {row.jobs} 个任务</p>
            {row.incomplete_jobs > 0 && <p className="mt-1 text-warning">{row.incomplete_jobs} 个任务费用不完整</p>}
          </div>
        }} />
        <Bar dataKey="amount_cny" name="人民币费用" fill={color} radius={[0, 4, 4, 0]} maxBarSize={20} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  </div>
}

export function SpendDashboard({ data, loading = false, error = false }: { data?: SpendSummary; loading?: boolean; error?: boolean }) {
  const { user } = useAuth()
  if (!data) return <Card>
    <CardHeader title="API 费用统计" description="按项目、人员和模型汇总人民币费用" />
    <CardBody><p role="status" className={`text-sm ${loading ? 'text-fg-muted' : 'text-warning'}`}>
      {loading ? '正在加载费用统计…' : error ? '费用数据加载失败，请重试或检查后端连接。' : BILLING_UNAVAILABLE}
    </p></CardBody>
  </Card>
  return <Card>
    <CardHeader title="API 费用统计" description={data.note} actions={user?.role === 'admin' && <Link className="link text-xs" to="/admin?tab=pricing">价格与折扣</Link>} />
    <CardBody className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-4">
        <strong className="num text-2xl">{money(data.amount_cny)}</strong>
        <span className="text-xs text-fg-muted">{data.jobs} 个评测任务 · {data.incomplete_jobs} 个费用不完整或待统计</span>
        {!!data.updating_jobs && <span role="status" className="text-xs text-primary">正在后台更新 {data.updating_jobs} 个任务的费用，结果将逐步显示</span>}
      </div>
      <Tabs defaultValue="projects">
        <TabsList aria-label="费用统计维度">
          <TabsTrigger value="projects">按项目</TabsTrigger><TabsTrigger value="people">按人员</TabsTrigger><TabsTrigger value="models">按模型</TabsTrigger>
        </TabsList>
        {([{ key: 'projects', title: '按项目' }, { key: 'people', title: '按人员' }, { key: 'models', title: '按模型' }] as const).map(({ key, title }, index) => {
          const ranked = spendChartRows(data[key])
          const rows = [...data[key]].sort((a, b) => (b.amount_cny ?? -1) - (a.amount_cny ?? -1))
          return <TabsContent key={key} value={key} className="pt-4">
            <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
              <div className="min-w-0">
                <h3 className="mb-2 text-xs font-medium">费用排行 · 前 8 项</h3>
                <SpendChart rows={data[key]} title={title} color={`var(--chart-${index + 1})`} />
              </div>
              <div className="min-w-0">
                <h3 className="mb-2 text-xs font-medium">完整明细 · {rows.length} 项</h3>
                <div className="h-80 overflow-auto rounded-md border border-border"><Table>
                  <thead className="sticky top-0 z-10 bg-card"><tr><Th>名称</Th><Th className="whitespace-nowrap">费用</Th><Th className="whitespace-nowrap">任务</Th></tr></thead>
                  <tbody>{rows.map((row) => {
                    const rank = ranked.findIndex(item => item.id === row.id)
                    return <Tr key={row.id}>
                      <Td className="w-full min-w-32 break-all text-xs">
                        {rank >= 0 && <span className="num mr-2 text-fg-subtle">#{rank + 1}</span>}
                        {key === 'projects' ? <Link className="link" to={`/jobs?project=${row.id}`}>{row.name}</Link> : <span title={row.id}>{row.name}</span>}
                      </Td>
                      <Td className="num whitespace-nowrap text-xs">{money(row.amount_cny)}{row.incomplete_jobs > 0 && <div className="text-[10px] text-warning">{row.incomplete_jobs} 项不完整</div>}</Td>
                      <Td className="num text-xs">{row.jobs}</Td>
                    </Tr>
                  })}{!rows.length && <tr><Td colSpan={3}>暂无记录</Td></tr>}</tbody>
                </Table></div>
              </div>
            </div>
            <p className="mt-3 text-xs text-fg-muted">图中序号对应明细排名，悬停可查看完整名称；未定价的用量不按零费用计算。</p>
          </TabsContent>
        })}
      </Tabs>
    </CardBody>
  </Card>
}
