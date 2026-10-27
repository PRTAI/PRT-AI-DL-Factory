import type { Job, ModelPrice, SpendRow } from './types'

export const BILLING_UNAVAILABLE = '后端尚未提供费用数据，请同步新版后端并重启服务后刷新页面。'

export function money(value?: number | null) {
  if (value === null || value === undefined) return '—'
  if (value > 0 && value < 0.000001) return '<¥0.000001'
  return `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: value > 0 && value < 0.01 ? 6 : 2 })}`
}

export function jobCostState(job: Pick<Job, 'billing' | 'status'>) {
  const billing = job.billing
  if (!billing) return { label: '费用服务未启用', hint: BILLING_UNAVAILABLE }
  const hint = billing.warnings.join('\n')
  if (billing.amount_cny != null) return {
    label: `${money(billing.amount_cny)} · ${billing.status === 'partial' ? '部分估算' : '预估'}`, hint,
  }
  if (!billing.updated_at) return { label: '费用尚未统计', hint }
  if (billing.requests > 0) return { label: '费用待定价', hint }
  if (billing.warnings.some(note => note.includes('无法读取'))) return { label: '用量读取失败', hint }
  return { label: ['pending_review', 'queued', 'preparing', 'running'].includes(job.status) ? '等待用量记录' : '未记录用量', hint }
}

export function spendRangeError(start?: string | null, end?: string | null) {
  return start && end && start > end ? '开始日期不能晚于结束日期' : ''
}

export function spendChartRows(rows: SpendRow[], limit = 8) {
  // Missing amounts are unknown, not zero-cost usage.
  return rows.filter(row => row.amount_cny != null && row.amount_cny > 0)
    .sort((a, b) => b.amount_cny! - a.amount_cny!)
    .slice(0, limit)
}

export function supplementPrices(prices: ModelPrice[], catalog: ModelPrice[]) {
  const key = (vendor: string, model: string) => `${vendor}/${model.toLowerCase()}`
  const occupied = new Set(prices.flatMap(row => [row.model, ...row.aliases].map(name => key(row.vendor, name))))
  const result = prices.map(row => ({ ...row, aliases: [...row.aliases] }))
  for (const price of catalog) {
    const existing = result.find(row => key(row.vendor, row.model) === key(price.vendor, price.model))
    // Preserve admin tariffs, discounts and region; only enrich a matching region's aliases.
    if (existing && existing.region !== price.region) continue
    if (!existing && occupied.has(key(price.vendor, price.model))) continue
    const aliases = price.aliases.filter(alias => !occupied.has(key(price.vendor, alias)))
    if (existing) existing.aliases.push(...aliases)
    else result.push({ ...price, aliases })
    occupied.add(key(price.vendor, price.model))
    aliases.forEach(alias => occupied.add(key(price.vendor, alias)))
  }
  return result
}
