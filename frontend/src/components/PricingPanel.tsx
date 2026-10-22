import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/lib/api'
import { supplementPrices } from '@/lib/billing'
import type { ModelPrice, PricingSettings, RateTier } from '@/lib/types'
import { Button, Card, CardBody, CardHeader, Dialog, ErrorBox, Field, Input, NumberInput, Select, Skeleton, Table, Td, Th, Tr } from '@/components/ui'

export function PricingPanel() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['pricing'], queryFn: api.pricing })
  const [draft, setDraft] = React.useState<PricingSettings | null>(null)
  const [edit, setEdit] = React.useState<{ index: number; price: ModelPrice } | null>(null)
  const [search, setSearch] = React.useState('')
  const value = draft ?? query.data
  const save = useMutation({
    mutationFn: api.updatePricing,
    onSuccess: (next) => { queryClient.setQueryData(['pricing'], next); setDraft(null); toast.success('已保存；现有任务保留原价格快照') },
    onError: (error) => toast.error(error.message),
  })
  const supplement = useMutation({
    mutationFn: api.priceCatalog,
    onSuccess: (catalog) => {
      const current = draft ?? query.data
      if (!current) return
      const next = supplementPrices(current.prices ?? [], catalog)
      setDraft({ ...current, prices: next })
      toast.success('已补充缺失型号和别名；现有单价与折扣保留，请检查后保存')
    },
    onError: (error) => toast.error(error.message),
  })
  if (query.isError) return <ErrorBox message={query.error.message} />
  if (!value) return <Skeleton className="h-48 w-full" />
  const prices = value.prices ?? []
  const updatePrice = (index: number, price: ModelPrice) => setDraft({ ...value, prices: prices.map((row, i) => i === index ? price : row) })
  return <>
    <Card>
      <CardHeader title="模型价格与折扣" description="单价均为每百万 Token 的官方原价，费用再乘折扣及人民币汇率。仅影响新任务，现有任务保留快照。" actions={<Button variant="primary" loading={save.isPending} onClick={() => save.mutate(value)}>保存价格设置</Button>} />
      <CardBody className="space-y-4">
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="美元兑人民币"><NumberInput min={0.0001} step={0.0001} value={value.usd_cny} onChange={(next) => setDraft({ ...value, usd_cny: next ?? value.usd_cny })} /></Field>
          <Field label="汇率日期"><Input type="date" value={value.exchange_date} onChange={(e) => setDraft({ ...value, exchange_date: e.target.value })} /></Field>
          <Field label="汇率来源"><Input value={value.exchange_source} onChange={(e) => setDraft({ ...value, exchange_source: e.target.value })} /></Field>
        </div>
        <p className="text-xs text-fg-muted">Atlas 别名明确对应官方基础型号；ccmax 不自动按 Fast 加价。百炼默认北京、6 折；其他地域需配置相应价格。Judge 的 batched 模式不自动享受供应商 Batch API 折扣。缺少单价或用量会提示不完整。</p>
        <div className="flex flex-wrap items-center gap-3">
          <Button loading={supplement.isPending} onClick={() => supplement.mutate()}>补充内置价格</Button>
          <span className="text-xs text-fg-muted">补充新版目录中的缺失型号与别名，保留已设置的单价和折扣。</span>
        </div>
        <div className="flex gap-2"><Input placeholder="搜索平台或模型" value={search} onChange={(e) => setSearch(e.target.value)} /><Button onClick={() => setEdit({ index: -1, price: { vendor: 'custom', model: '', official_model: '', aliases: [], currency: 'CNY', discount: 1, tiers: [{ input: 0, output: 0 }], region: '', source: '管理员定价', verified_on: new Date().toLocaleDateString('sv-SE'), valid_until: '', note: '' } })}>添加单价</Button></div>
        <Table><thead><tr><Th>平台 / 模型</Th><Th>官方型号</Th><Th>输入 / 输出</Th><Th>折扣系数</Th><Th>核验日期</Th><Th>操作</Th></tr></thead><tbody>
          {prices.map((price, index) => ({ price, index })).filter(({ price }) => `${price.vendor} ${price.model} ${price.official_model}`.toLowerCase().includes(search.toLowerCase())).map(({ price, index }) => <Tr key={`${price.vendor}/${price.model}`}>
            <Td className="break-all text-xs">{price.vendor}<div className="font-medium">{price.model}</div></Td><Td className="text-xs">{price.official_model || price.model}</Td>
            <Td className="num text-xs">{price.tiers[0].input} / {price.tiers[0].output} {price.currency}{price.tiers.length > 1 && <div>共 {price.tiers.length} 个输入阶梯</div>}</Td>
            <Td><NumberInput className="w-24" min={0} max={1} step={0.01} value={price.discount} onChange={(next) => updatePrice(index, { ...price, discount: next ?? 1 })} /></Td>
            <Td className="text-xs">{price.verified_on}{price.valid_until && <div>有效至 {price.valid_until}</div>}</Td>
            <Td><Button size="sm" onClick={() => setEdit({ index, price: structuredClone(price) })}>编辑</Button></Td>
          </Tr>)}
        </tbody></Table>
      </CardBody>
    </Card>
    {edit && <PriceEditor price={edit.price} onClose={() => setEdit(null)} onSave={(price) => { setDraft({ ...value, prices: edit.index < 0 ? [...prices, price] : prices.map((row, i) => i === edit.index ? price : row) }); setEdit(null) }} />}
  </>
}

function PriceEditor({ price, onClose, onSave }: { price: ModelPrice; onClose: () => void; onSave: (value: ModelPrice) => void }) {
  const [value, setValue] = React.useState(price)
  const [aliasesText, setAliasesText] = React.useState(price.aliases.join(', '))
  const update = <K extends keyof ModelPrice>(key: K, next: ModelPrice[K]) => setValue({ ...value, [key]: next })
  const tierFields: (keyof RateTier)[] = ['max_input', 'input', 'output', 'cache_read', 'explicit_cache_read', 'cache_write', 'cache_write_long']
  const labels = ['输入上限（空=不限）', '普通输入', '输出', '缓存读取（默认/隐式）', '百炼显式缓存读取', '缓存写入（短）', '缓存写入（长）']
  return <Dialog open onOpenChange={(open) => { if (!open) onClose() }} title="编辑模型单价" description="缓存单价未知时留空；0 表示确认免费。保存后还需点击「保存价格设置」。" width="max-w-3xl">
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <Field label="平台标识" hint="alibaba / atlascloud / openai 等；专属配置用 platform:平台ID"><Input value={value.vendor} onChange={(e) => update('vendor', e.target.value)} /></Field>
        <Field label="模型 ID"><Input value={value.model} onChange={(e) => update('model', e.target.value)} /></Field>
        <Field label="官方基础型号"><Input value={value.official_model} onChange={(e) => update('official_model', e.target.value)} /></Field>
        <Field label="明确的别名（逗号分隔）"><Input value={aliasesText} onChange={(e) => setAliasesText(e.target.value)} /></Field>
        <Field label="币种"><Select value={value.currency} onValueChange={(next) => update('currency', next as 'USD' | 'CNY')} options={[{ value: 'CNY', label: '人民币' }, { value: 'USD', label: '美元' }]} /></Field>
        <Field label="地区（空=通用）"><Input value={value.region} onChange={(e) => update('region', e.target.value)} /></Field>
        <Field label="官方来源"><Input value={value.source} onChange={(e) => update('source', e.target.value)} /></Field>
        <Field label="有效截止日期（可空）"><Input type="date" value={value.valid_until} onChange={(e) => update('valid_until', e.target.value)} /></Field>
      </div>
      {value.tiers.map((tier, index) => <div key={index} className="rounded border border-border p-2">
        <div className="mb-2 flex justify-between"><span className="text-xs">输入阶梯 {index + 1}</span>{value.tiers.length > 1 && <Button size="sm" variant="ghost" onClick={() => update('tiers', value.tiers.filter((_, i) => i !== index))}>移除</Button>}</div>
        <div className="grid grid-cols-3 gap-2">{tierFields.map((key, i) => <Field key={key} label={labels[i]}><NumberInput min={0} step={key === 'max_input' ? 1 : 0.001} value={tier[key] ?? undefined} onChange={(next) => update('tiers', value.tiers.map((row, j) => j === index ? { ...row, [key]: next ?? null } : row))} /></Field>)}</div>
      </div>)}
      <Button size="sm" onClick={() => update('tiers', [...value.tiers, { input: 0, output: 0 }])}>添加输入阶梯</Button>
      <Field label="定价说明"><Input value={value.note} onChange={(e) => update('note', e.target.value)} /></Field>
      <div className="flex justify-end gap-2"><Button onClick={onClose}>取消</Button><Button variant="primary" disabled={!value.model.trim() || !value.vendor.trim() || !value.source.trim()} onClick={() => onSave({ ...value, aliases: aliasesText.split(',').map((item) => item.trim()).filter(Boolean), model: value.model.trim(), vendor: value.vendor.trim(), verified_on: new Date().toLocaleDateString('sv-SE') })}>应用编辑</Button></div>
    </div>
  </Dialog>
}
