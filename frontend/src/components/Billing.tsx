import { Link } from 'react-router-dom'
import { useAuth } from '@/lib/store'
import type { Job, SpendSummary } from '@/lib/types'
import { Card, CardBody, CardHeader, Table, Td, Th, Tr } from '@/components/ui'

export function money(value?: number | null) {
  if (value === null || value === undefined) return '—'
  if (value > 0 && value < 0.000001) return '<¥0.000001'
  return `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: value > 0 && value < 0.01 ? 6 : 2 })}`
}

export function JobCost({ job }: { job: Job }) {
  if (job.type !== 'verify') return null
  const billing = job.billing
  const label = billing?.amount_cny == null ? '费用待统计' : `${money(billing.amount_cny)} · ${billing.status === 'partial' ? '部分估算' : '预估'}`
  return <span className="mt-0.5 block text-[11px] text-fg-muted" title={billing?.warnings.join('\n')}>{label}</span>
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

export function SpendDashboard({ data }: { data?: SpendSummary }) {
  const { user } = useAuth()
  if (!data) return null
  return <Card>
    <CardHeader title="API 费用统计" description={data.note} actions={user?.role === 'admin' && <Link className="link text-xs" to="/admin?tab=pricing">价格与折扣</Link>} />
    <CardBody className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-4">
        <strong className="num text-2xl">{money(data.amount_cny)}</strong>
        <span className="text-xs text-fg-muted">{data.jobs} 个评测任务 · {data.incomplete_jobs} 个费用不完整或待统计</span>
      </div>
      <div className="grid gap-3 xl:grid-cols-3">
        {([{ key: 'projects', title: '按项目' }, { key: 'people', title: '按人员' }, { key: 'models', title: '按模型' }] as const).map(({ key, title }) => <div key={key}>
          <h3 className="mb-2 text-sm font-medium">{title}</h3>
          <div className="max-h-80 overflow-auto rounded-md border border-border"><Table>
            <thead><tr><Th>名称</Th><Th>费用</Th><Th>任务</Th></tr></thead>
            <tbody>{data[key].map((row) => <Tr key={row.id}>
              <Td className="max-w-40 break-all text-xs">{key === 'projects' ? <Link className="link" to={`/jobs?project=${row.id}`}>{row.name}</Link> : <span title={row.id}>{row.name}</span>}</Td>
              <Td className="num whitespace-nowrap text-xs">{money(row.amount_cny)}{row.incomplete_jobs > 0 && <div className="text-[10px] text-warning">{row.incomplete_jobs} 项不完整</div>}</Td>
              <Td className="num text-xs">{row.jobs}</Td>
            </Tr>)}{!data[key].length && <tr><Td colSpan={3}>暂无记录</Td></tr>}</tbody>
          </Table></div>
        </div>)}
      </div>
    </CardBody>
  </Card>
}
