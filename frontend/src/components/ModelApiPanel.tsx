import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Pencil, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import type { ApiProtocol, ModelApi, ModelPlatform, ModelProvider, PlatformCatalog, PlatformVendor } from '@/lib/types'
import { cn } from '@/lib/utils'
import { protocolLabels } from '@/lib/model-api'
import { KeyValueEditor } from './pickers'
import { Badge, Button, Card, CardBody, CardHeader, Checkbox, Dialog, EmptyState, ErrorBox, Field, Input, Select, Skeleton, Table, Td, Textarea, Th, ToggleRow, Tr } from './ui'

export function ModelApiPanel() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const qc = useQueryClient()
  const models = useQuery({ queryKey: ['model-apis'], queryFn: api.modelApis })
  const platforms = useQuery({ queryKey: ['model-platforms'], queryFn: api.modelPlatforms })
  const catalog = useQuery({ queryKey: ['platform-catalog'], queryFn: api.platformCatalog })
  const [vendor, setVendor] = React.useState<PlatformVendor>('alibaba')
  const [accountId, setAccountId] = React.useState('')
  const [platformTarget, setPlatformTarget] = React.useState<ModelPlatform | 'new' | null>(null)
  const [modelTarget, setModelTarget] = React.useState<ModelApi | 'new' | null>(null)
  const accounts = (platforms.data ?? []).filter((p) => p.vendor === vendor)
  const account = accounts.find((p) => p.id === accountId) ?? accounts[0]
  const spec = catalog.data?.find((p) => p.id === vendor)
  const rows = (models.data ?? []).filter((m) => (account && m.platform_id === account.id) || (!m.platform_id && vendor === 'custom'))
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['model-apis'] })
    qc.invalidateQueries({ queryKey: ['model-platforms'] })
  }
  const remove = useMutation({ mutationFn: api.deleteModelApi, onSuccess: invalidate, onError: (e: ApiError) => toast.error(e.message) })
  const restore = useMutation({
    mutationFn: async () => {
      const existing = new Set(rows.map((m) => m.model_id))
      for (const preset of spec?.models ?? []) {
        if (!existing.has(preset.model)) await api.createModelApi({ name: preset.model, platform_id: account!.id })
      }
    }, onSuccess: () => { invalidate(); toast.success('已添加常见模型') }, onError: (e: ApiError) => { invalidate(); toast.error(e.message) },
  })
  return <Card>
    <CardHeader title="模型 API 平台" icon={<KeyRound className="h-4 w-4" />} description="先配置平台密钥，再选择或添加模型。同一平台配置下的模型共用 API Key；默认使用普通 API。" />
    <CardBody className="space-y-5">
      {(platforms.error || catalog.error || models.error) && <ErrorBox message={(platforms.error || catalog.error || models.error)!.message} />}
      {catalog.isLoading ? <Skeleton className="h-24" /> : <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-6">
        {(catalog.data ?? []).map((p) => {
          const configured = (platforms.data ?? []).some((a) => a.vendor === p.id && a.enabled && a.has_api_key)
          return <button key={p.id} type="button" aria-pressed={vendor === p.id} onClick={() => { setVendor(p.id); setAccountId('') }} className={cn('rounded-lg border p-3 text-left transition-colors', vendor === p.id ? 'border-primary bg-primary-soft' : 'border-border hover:bg-card-hover')}>
            <div className="text-sm font-medium text-fg">{p.label}</div>
            <div className="mt-2 text-xs text-fg-muted">{configured ? '已配置密钥' : '待配置'}</div>
          </button>
        })}
      </div>}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-muted/40 p-4">
        <div className="min-w-0 space-y-1">
          <div className="font-medium">{account?.name ?? spec?.label}</div>
          <p className="text-xs text-fg-muted">{spec?.description} · {account?.has_api_key ? `共享密钥 ${account.api_key_masked}` : '配置 API Key 后，平台下的模型即可使用'}{account && !account.enabled && ' · 已停用'}</p>
          {spec?.sources[0] && <a href={spec.sources[0]} target="_blank" rel="noreferrer" className="text-xs text-primary">官方接入文档</a>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {accounts.length > 1 && <Select className="w-60" value={account?.id ?? ''} onValueChange={setAccountId} options={accounts.map((p) => ({ value: p.id, label: p.name, description: `${p.api_key_masked || '未填密钥'} · ${Object.values(p.endpoints)[0] ?? ''}` }))} />}
          {isAdmin && <>
            <Button size="sm" variant="primary" onClick={() => setPlatformTarget(account ?? 'new')}><KeyRound className="h-3.5 w-3.5" />{account ? '配置平台' : '连接平台'}</Button>
            <Button size="sm" onClick={() => setPlatformTarget('new')}><Plus className="h-3.5 w-3.5" />新增配置</Button>
          </>}
        </div>
      </div>
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-medium">模型 <span className="text-fg-muted">({rows.length})</span></div>
        {isAdmin && account && <div className="flex gap-2">
          {!!spec?.models.length && <Button size="sm" loading={restore.isPending} onClick={() => restore.mutate()}>添加常见模型</Button>}
          <Button size="sm" onClick={() => setModelTarget('new')}><Plus className="h-3.5 w-3.5" />自定义模型</Button>
        </div>}
      </div>
      {models.isLoading ? <Skeleton className="h-32" /> : !rows.length ? <EmptyState title="暂无模型" description="连接平台后自动添加常见模型，也可以输入模型名称添加。" /> : <Table>
        <thead><tr><Th>模型名称</Th><Th>接口</Th><Th>状态</Th>{isAdmin && <Th className="w-44">操作</Th>}</tr></thead>
        <tbody>{rows.map((m) => <Tr key={m.id}>
          <Td><div className="font-medium">{m.name}</div>{m.model_id !== m.name && <div className="num text-xs text-fg-muted">{m.model_id}</div>}{m.note && <div className="text-xs text-fg-subtle">{m.note}</div>}</Td>
          <Td><Badge tone="primary">{protocolLabels(m.supported_protocols ?? [])}</Badge></Td>
          <Td className="text-xs text-fg-muted">{!m.enabled ? '已停用' : m.ready ? '可用' : '平台未配置或已停用'}</Td>
          {isAdmin && <Td><div className="flex gap-2"><Button size="sm" onClick={() => setModelTarget(m)}><Pencil className="h-3 w-3" />编辑</Button><Button size="sm" variant="outline" onClick={() => { if (window.confirm(`删除模型「${m.name}」？`)) remove.mutate(m.id) }}><Trash2 className="h-3 w-3" />删除</Button></div></Td>}
        </Tr>)}</tbody>
      </Table>}
    </CardBody>
    {platformTarget && spec && <PlatformDialog target={platformTarget} spec={spec} onClose={() => setPlatformTarget(null)} onSaved={(p) => { invalidate(); setAccountId(p.id); setPlatformTarget(null) }} />}
    {modelTarget && (modelTarget === 'new' || modelTarget.platform_id) && account && <ModelDialog target={modelTarget} platform={account} onClose={() => setModelTarget(null)} onSaved={() => { invalidate(); setModelTarget(null) }} />}
    <EditDialog target={modelTarget && modelTarget !== 'new' && !modelTarget.platform_id ? modelTarget : null} onClose={() => setModelTarget(null)} />
  </Card>
}

function PlatformDialog({ target, spec, onClose, onSaved }: { target: ModelPlatform | 'new'; spec: PlatformCatalog; onClose: () => void; onSaved: (p: ModelPlatform) => void }) {
  const editing = target === 'new' ? null : target
  const [name, setName] = React.useState(editing?.name ?? spec.label)
  const [key, setKey] = React.useState('')
  const [endpoints, setEndpoints] = React.useState<Partial<Record<ApiProtocol, string>>>(editing?.endpoints ?? spec.endpoints)
  const [enabled, setEnabled] = React.useState(editing?.enabled ?? true)
  const save = useMutation({
    mutationFn: () => {
      const payload = { name, endpoints, enabled, ...(key || !editing ? { api_key: key } : {}) }
      return editing ? api.updateModelPlatform(editing.id, payload) : api.createModelPlatform({ ...payload, vendor: spec.id })
    }, onSuccess: onSaved, onError: (e: ApiError) => toast.error(e.message),
  })
  const clear = useMutation({ mutationFn: () => api.updateModelPlatform(editing!.id, { api_key: '' }), onSuccess: onSaved, onError: (e: ApiError) => toast.error(e.message) })
  const remove = useMutation({ mutationFn: () => api.deleteModelPlatform(editing!.id), onSuccess: () => { onSaved(editing!); onClose() }, onError: (e: ApiError) => toast.error(e.message) })
  return <Dialog open onOpenChange={(open) => !open && onClose()} title={`${editing ? '配置' : '连接'}平台 · ${spec.label}`} description="该配置下的所有模型共用一个 API Key。编辑时留空保留原密钥。" footer={<>
    {editing && <Button variant="ghost" size="sm" loading={remove.isPending} onClick={() => { if (window.confirm('删除此平台配置？请先删除其下的模型。')) remove.mutate() }}>删除配置</Button>}
    {editing?.has_api_key && <Button variant="ghost" onClick={() => clear.mutate()} loading={clear.isPending}>清空密钥</Button>}
    <Button onClick={onClose}>取消</Button><Button variant="primary" onClick={() => save.mutate()} loading={save.isPending} disabled={!name.trim() || !Object.values(endpoints).some((v) => v?.trim())}>保存平台</Button>
  </>}>
    <div className="space-y-4">
      <Field label="配置名称"><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="平台 API Key" hint={editing?.has_api_key ? `当前 ${editing.api_key_masked} · 留空保留` : '使用普通 API 的密钥；保存后预置模型和自定义模型共用'}><Input type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="输入 API Key" /></Field>
      <details open={spec.id === 'custom'} className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm text-fg-muted">高级设置：接口地址</summary>
        <div className="mt-3 space-y-3">
          <p className="text-xs text-fg-muted">默认使用官方地址。业务空间、地域或中转地址可在此覆盖；留空停用对应接口。</p>
          {(['openai', 'anthropic', 'responses'] as ApiProtocol[]).map((p) => <Field key={p} label={p === 'anthropic' ? 'Anthropic' : p === 'responses' ? 'OpenAI · Codex 地址' : 'OpenAI'}><Input value={endpoints[p] ?? ''} onChange={(e) => setEndpoints((prev) => ({ ...prev, [p]: e.target.value }))} placeholder={spec.endpoints[p] ?? 'https://…'} className="num text-xs" /></Field>)}
          {spec.id === 'custom' && <Button size="sm" onClick={() => setEndpoints((p) => ({ ...p, responses: p.openai }))}>Codex 复用 OpenAI 地址</Button>}
        </div>
      </details>
      <ToggleRow label="启用平台" hint="停用后该配置下的所有模型不可用于新任务" checked={enabled} onCheckedChange={setEnabled} />
    </div>
  </Dialog>
}

function ModelDialog({ target, platform, onClose, onSaved }: { target: ModelApi | 'new'; platform: ModelPlatform; onClose: () => void; onSaved: () => void }) {
  const editing = target === 'new' ? null : target
  const [name, setName] = React.useState(editing?.name ?? '')
  const [model, setModel] = React.useState(editing?.model_id ?? '')
  const [enabled, setEnabled] = React.useState(editing?.enabled ?? true)
  const [protocols, setProtocols] = React.useState<ApiProtocol[]>(editing?.supported_protocols ?? Object.keys(platform.endpoints) as ApiProtocol[])
  const save = useMutation({
    mutationFn: () => editing ? api.updateModelApi(editing.id, { name: name.trim(), model: model.trim(), enabled, supported_protocols: protocols }) : api.createModelApi({ name: name.trim(), platform_id: platform.id }),
    onSuccess: onSaved, onError: (e: ApiError) => toast.error(e.message),
  })
  return <Dialog open onOpenChange={(open) => !open && onClose()} title={editing ? '编辑模型' : '添加自定义模型'} description={`复用「${platform.name}」的密钥和接口地址，无需重复填写。`} footer={<><Button onClick={onClose}>取消</Button><Button variant="primary" disabled={!name.trim()} loading={save.isPending} onClick={() => save.mutate()}>保存模型</Button></>}>
    <div className="space-y-4">
      <Field label={editing ? '显示名称' : 'Model name'} required hint="填写平台实际支持的模型标识；添加后可编辑该模型支持的接口"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 qwen3.8-max" /></Field>
      {editing && <>
        <Field label="模型标识"><Input value={model} onChange={(e) => setModel(e.target.value)} className="num" /></Field>
        <Field label="模型支持的接口" hint="请按该模型的官方能力设置；OpenAI 同时用于 Judge 与做题模型">
          <div className="flex flex-wrap gap-4">{(['openai', 'anthropic', 'responses'] as ApiProtocol[]).filter((p) => platform.endpoints[p]).map((p) => <label key={p} className="flex items-center gap-2 text-sm"><Checkbox checked={protocols.includes(p)} onCheckedChange={(checked) => setProtocols((prev) => checked ? [...prev, p] : prev.filter((v) => v !== p))} />{p === 'responses' ? '支持 Codex' : p === 'anthropic' ? 'Anthropic' : 'OpenAI'}</label>)}</div>
        </Field>
        <ToggleRow label="启用模型" checked={enabled} onCheckedChange={setEnabled} />
      </>}
    </div>
  </Dialog>
}

function EditDialog({ target, onClose }: { target: ModelApi | null | 'new'; onClose: () => void }) {
  const queryClient = useQueryClient()
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: () => api.modelProviders(), enabled: Boolean(target) })
  const editing = target && target !== 'new' ? target : null

  const [name, setName] = React.useState('')
  const [provider, setProvider] = React.useState<ModelProvider>('openai')
  const [model, setModel] = React.useState('')
  const [baseUrl, setBaseUrl] = React.useState('')
  const [apiKey, setApiKey] = React.useState('')
  const [extraEnv, setExtraEnv] = React.useState<Record<string, string>>({})
  const [note, setNote] = React.useState('')
  const [enabled, setEnabled] = React.useState(true)
  const [supportsEffort, setSupportsEffort] = React.useState(true)

  React.useEffect(() => {
    if (!target) return
    setName(editing?.name ?? '')
    setProvider(editing?.provider ?? 'openai')
    setModel(editing?.model ?? '')
    setBaseUrl(editing?.base_url ?? '')
    setApiKey('')
    setExtraEnv({})
    setNote(editing?.note ?? '')
    setEnabled(editing?.enabled ?? true)
    setSupportsEffort(editing?.supports_reasoning_effort ?? true)
  }, [target, editing])

  const spec = providers.data?.find((p) => p.id === provider)

  const save = useMutation({
    mutationFn: () => {
      const payload: Record<string, unknown> = {
        name: name.trim(),
        provider,
        model: model.trim(),
        base_url: baseUrl.trim(),
        note,
        enabled,
        supports_reasoning_effort: supportsEffort,
      }
      if (Object.keys(extraEnv).length) payload.extra_env = extraEnv
      // 编辑时留空 = 保留原密钥；新增时留空 = 先不填
      if (!editing || apiKey) payload.api_key = apiKey
      return editing ? api.updateModelApi(editing.id, payload) : api.createModelApi(payload)
    },
    onSuccess: () => {
      toast.success(editing ? '模型配置已更新' : '模型配置已添加')
      queryClient.invalidateQueries({ queryKey: ['model-apis'] })
      onClose()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const clearKey = useMutation({
    mutationFn: () => api.updateModelApi(editing!.id, { api_key: '' }),
    onSuccess: () => {
      toast.success('已清空密钥')
      queryClient.invalidateQueries({ queryKey: ['model-apis'] })
      onClose()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <Dialog
      open={Boolean(target)}
      onOpenChange={(open) => !open && onClose()}
      title={editing ? `编辑模型配置：${editing.name}` : '添加模型配置'}
      description="厂家/协议决定注入哪些环境变量；中转站一般走 OpenAI 兼容，Claude 系走 Anthropic 协议。"
      width="max-w-2xl"
      footer={
        <>
          {editing?.has_api_key && (
            <Button variant="ghost" onClick={() => clearKey.mutate()} loading={clearKey.isPending}>
              清空密钥
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!name.trim()} onClick={() => save.mutate()}>
            保存
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="显示名称" required hint="提交评测时看到的名字；留空「模型标识」时也用它当 --model">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="gpt-5.6-sol" />
          </Field>
          <Field label="厂家 / 协议" required hint={spec?.hint || undefined}>
            <Select
              value={provider}
              onValueChange={(v) => {
                setProvider(v as ModelProvider)
                const next = providers.data?.find((p) => p.id === v)
                if (next && !baseUrl) setBaseUrl(next.default_base_url === 'https://api.openai.com/v1' || next.default_base_url === 'https://api.anthropic.com/v1' ? '' : next.default_base_url)
              }}
              options={(providers.data ?? []).map((p) => ({ value: p.id, label: p.label, description: p.api_key_envs.join(' / ') }))}
            />
          </Field>
          <Field label="模型标识" hint="真正发给 agent 的模型名；留空用显示名。litellm 系 agent 会自动加 provider/ 前缀">
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={name || '同显示名'} className="num" />
          </Field>
          <Field label="Base URL" hint={spec ? `注入 ${spec.base_url_envs.join(' / ')}；留空用厂家默认 ${spec.default_base_url}` : undefined}>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={spec?.default_base_url ?? 'https://…/v1'} className="num" />
          </Field>
        </div>
        <Field
          label="API Key"
          hint={
            editing?.has_api_key
              ? `当前 ${editing.api_key_masked}；留空表示不改。注入 ${spec?.api_key_envs.join(' / ')}`
              : `注入 ${spec?.api_key_envs.join(' / ') ?? ''}；不填的话这条配置先不可选`
          }
        >
          <Input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={editing?.has_api_key ? '（保留原密钥）' : 'sk-…'} className="num" autoComplete="new-password" />
        </Field>
        <Field label="额外环境变量" hint="少数 agent 需要的其他变量，例如 AZURE_API_VERSION">
          <KeyValueEditor value={extraEnv} onChange={setExtraEnv} addLabel="添加变量" />
        </Field>
        <Field label="备注">
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="选填" className="min-h-14" />
        </Field>
        <div className="grid gap-2 sm:grid-cols-2">
          <ToggleRow label="启用" hint="停用后提交页里不再出现" checked={enabled} onCheckedChange={setEnabled} />
          <ToggleRow label="支持思考模式" hint="该模型接受 reasoning_effort" checked={supportsEffort} onCheckedChange={setSupportsEffort} />
        </div>
      </div>
    </Dialog>
  )
}
