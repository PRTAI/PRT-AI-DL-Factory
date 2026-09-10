/**
 * 平台管理 → 模型 API：厂家 / 中转站的模型端点。全员可见、管理员可改。
 * 密钥只回显尾号；编辑时不动密钥栏就表示保留原值。
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Pencil, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import type { ModelApi, ModelProvider } from '@/lib/types'
import { formatRelative } from '@/lib/utils'
import { Mono } from './common'
import { KeyValueEditor } from './pickers'
import { Badge, Button, Card, CardBody, CardHeader, Dialog, EmptyState, Field, Input, LinkButton, Select, Skeleton, StatusDot, Table, Td, Textarea, Th, ToggleRow, Tr } from './ui'

export function ModelApiPanel() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const queryClient = useQueryClient()
  const models = useQuery({ queryKey: ['model-apis'], queryFn: () => api.modelApis() })
  const [editing, setEditing] = React.useState<ModelApi | null | 'new'>(null)

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteModelApi(id),
    onSuccess: () => {
      toast.success('已删除')
      queryClient.invalidateQueries({ queryKey: ['model-apis'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const rows = models.data ?? []
  return (
    <Card>
      <CardHeader
        title="模型 API 配置"
        description="评测时可选的模型。运行前平台按 Harbor 的标准环境变量（ANTHROPIC_API_KEY / OPENAI_BASE_URL …）注入密钥与地址，用户不需要接触密钥。"
        icon={<KeyRound className="h-4 w-4" aria-hidden />}
        actions={
          isAdmin && (
            <Button variant="primary" size="sm" onClick={() => setEditing('new')}>
              <Plus className="h-3.5 w-3.5" aria-hidden />
              添加模型
            </Button>
          )
        }
      />
      {models.isLoading ? (
        <CardBody>
          <Skeleton className="h-40 w-full" />
        </CardBody>
      ) : !rows.length ? (
        <EmptyState title="还没有模型配置" description="添加后所有成员都能在提交评测时选择。" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>模型</Th>
              <Th className="w-[150px]">厂家 / 协议</Th>
              <Th className="w-[260px]">地址</Th>
              <Th className="w-[120px]">密钥</Th>
              <Th className="w-[100px]">状态</Th>
              <Th className="w-[130px]">更新</Th>
              {isAdmin && <Th className="w-[110px]">操作</Th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <Tr key={m.id}>
                <Td>
                  <div className="text-[13px] font-medium text-fg">{m.name}</div>
                  {m.model && m.model !== m.name && <div className="num text-[11px] text-fg-subtle">--model {m.model}</div>}
                  {m.note && <div className="mt-0.5 line-clamp-1 text-[11px] text-fg-muted">{m.note}</div>}
                </Td>
                <Td>
                  <Badge tone="primary">{m.provider}</Badge>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {m.env_names.map((n) => (
                      <Mono key={n}>{n}</Mono>
                    ))}
                  </div>
                </Td>
                <Td className="num truncate text-[11px] text-fg-muted" title={m.base_url}>
                  {m.base_url || <span className="text-fg-subtle">厂家默认</span>}
                </Td>
                <Td className="num text-xs">{m.has_api_key ? m.api_key_masked : <span className="text-warning">未填</span>}</Td>
                <Td>
                  {!m.enabled ? (
                    <StatusDot tone="neutral" label="已停用" />
                  ) : m.ready ? (
                    <StatusDot tone="success" label="可用" />
                  ) : (
                    <StatusDot tone="warning" label="缺密钥" />
                  )}
                </Td>
                <Td className="text-[11px] text-fg-subtle">
                  {m.updated_by}
                  <div>{formatRelative(m.updated_at)}</div>
                </Td>
                {isAdmin && (
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <LinkButton onClick={() => setEditing(m)}>
                        <Pencil className="mr-0.5 inline h-3 w-3" aria-hidden />
                        编辑
                      </LinkButton>
                      <LinkButton
                        tone="danger"
                        onClick={() => {
                          if (window.confirm(`删除模型配置「${m.name}」？`)) remove.mutate(m.id)
                        }}
                      >
                        <Trash2 className="mr-0.5 inline h-3 w-3" aria-hidden />
                        删除
                      </LinkButton>
                    </div>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      <EditDialog target={editing} onClose={() => setEditing(null)} />
    </Card>
  )
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
