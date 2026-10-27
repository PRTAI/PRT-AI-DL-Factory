const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/lib/billing.ts'), 'utf8')
const context = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, context)
const { money, jobCostState, spendRangeError, spendChartRows, supplementPrices } = context.exports

test('missing server support differs from pending usage and missing tariffs', () => {
  assert.equal(jobCostState({ status: 'succeeded' }).label, '费用服务未启用')
  const billing = { amount_cny: null, warnings: [], requests: 0, updated_at: '2026-10-22', status: 'pending' }
  assert.equal(jobCostState({ status: 'running', billing }).label, '等待用量记录')
  assert.equal(jobCostState({ status: 'succeeded', billing }).label, '未记录用量')
  assert.equal(jobCostState({ status: 'succeeded', billing: { ...billing, requests: 2 } }).label, '费用待定价')
  assert.equal(jobCostState({ status: 'succeeded', billing: { ...billing, warnings: ['部分用量记录无法读取'] } }).label, '用量读取失败')
})

test('known zero and small positive costs remain distinct from unknown amounts', () => {
  assert.equal(money(null), '—')
  assert.equal(money(0), '¥0.00')
  assert.equal(money(0.001), '¥0.001')
  assert.equal(money(0.00000001), '<¥0.000001')
  const state = jobCostState({ status: 'succeeded', billing: { amount_cny: 0, status: 'estimated', warnings: [] } })
  assert.equal(state.label, '¥0.00 · 预估')
})

test('date range accepts open ends and rejects a reversed range', () => {
  assert.equal(spendRangeError('', ''), '')
  assert.equal(spendRangeError('2026-10-01', ''), '')
  assert.equal(spendRangeError('', '2026-10-31'), '')
  assert.equal(spendRangeError('2026-10-01', '2026-10-01'), '')
  assert.equal(spendRangeError('2026-10-02', '2026-10-01'), '开始日期不能晚于结束日期')
})

test('spend rankings exclude unknown costs, preserve partial estimates and do not mutate rows', () => {
  const rows = [
    { id: 'missing', amount_cny: null }, { id: 'free', amount_cny: 0 },
    { id: 'partial', amount_cny: 50, incomplete_jobs: 2 }, { id: 'complete', amount_cny: 10 },
    { id: 'largest', amount_cny: 100 },
  ]
  const chart = spendChartRows(rows, 2)
  assert.deepEqual(Array.from(chart, row => row.id), ['largest', 'partial'])
  assert.equal(chart[1].incomplete_jobs, 2)
  assert.equal(rows[0].id, 'missing')
  assert.equal(spendChartRows([{ amount_cny: null }]).length, 0)
})

test('catalog supplementation preserves custom rates and discounts and avoids alias conflicts', () => {
  const prices = [
    { vendor: 'deepseek', model: 'deepseek-flash', aliases: [], region: '', discount: .5, tiers: [{ input: 99 }] },
    { vendor: 'alibaba', model: 'custom-qwen', aliases: ['qwen3.7-max'], region: 'cn-beijing' },
  ]
  const catalog = [
    { vendor: 'deepseek', model: 'deepseek-flash', aliases: ['deepseek-flash[1m]'], region: '', discount: 1, tiers: [{ input: 2 }] },
    { vendor: 'alibaba', model: 'qwen3.7-max', aliases: [], region: 'cn-beijing' },
    { vendor: 'tencent', model: 'hy4-preview', aliases: [], region: 'guangzhou' },
  ]
  const result = supplementPrices(prices, catalog)
  assert.equal(result.length, 3)
  assert.equal(result[0].discount, .5)
  assert.equal(result[0].tiers[0].input, 99)
  assert.deepEqual(Array.from(result[0].aliases), ['deepseek-flash[1m]'])
  assert.equal(prices[0].aliases.length, 0)
  assert.equal(JSON.stringify(supplementPrices(result, catalog)), JSON.stringify(result))
  const foreign = [{ ...catalog[0], region: 'other', aliases: [] }]
  assert.equal(supplementPrices(foreign, catalog)[0].aliases.length, 0)
})
