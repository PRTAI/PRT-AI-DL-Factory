/** Model / dataset selection widgets shared by the submission wizards. */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { Boxes, Database, Download, RefreshCw, Search, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import type { Dataset, DatasetKind, ModelEntry } from '@/lib/types'
import { cn, formatBytes, formatNumber } from '@/lib/utils'
import { Badge, Button, EmptyState, Input, Select, Spinner } from './ui'
import { FRAMEWORK_LABEL, Mono } from './common'
import { useProjectNames } from './ProjectTag'

/* -------------------------------------------------------------------------- */
/* Model picker                                                                */
/* -------------------------------------------------------------------------- */
export function ModelPicker({
  value,
  onChange,
  framework,
  onRequestDownload,
}: {
  value: string
  onChange: (model: ModelEntry | null) => void
  framework?: string
  onRequestDownload?: () => void
}) {
  const [query, setQuery] = React.useState('')
  const [debounced, setDebounced] = React.useState('')

  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 250)
    return () => clearTimeout(timer)
  }, [query])

  const models = useQuery({
    queryKey: ['models', debounced, framework],
    queryFn: () => api.models({ q: debounced, framework, limit: 300 }),
  })

  const refresh = async () => {
    await api.refreshModels()
    await models.refetch()
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索模型：厂家 / 名称 / 架构，例如 qwen3 4b"
            className="pl-8"
            aria-label="搜索模型"
          />
        </div>
        <Button variant="outline" size="icon" onClick={refresh} title="重新扫描模型目录" aria-label="重新扫描模型目录">
          <RefreshCw className={cn('h-4 w-4', models.isFetching && 'animate-spin')} aria-hidden />
        </Button>
        {onRequestDownload && (
          <Button variant="outline" onClick={onRequestDownload}>
            <Download className="h-3.5 w-3.5" aria-hidden />
            下载新模型
          </Button>
        )}
      </div>

      <div className="max-h-72 overflow-y-auto rounded-lg border border-border">
        {models.isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Spinner />
          </div>
        ) : !models.data?.length ? (
          <EmptyState
            icon={<Boxes className="h-6 w-6" aria-hidden />}
            title="没有匹配的模型"
            description={
              onRequestDownload
                ? '可以用「下载新模型」从 ModelScope 拉取，例如 Qwen/Qwen3-8B。'
                : '请调整搜索关键词。'
            }
          />
        ) : (
          <ul className="divide-y divide-border/60">
            {models.data.map((model) => {
              const selected = model.id === value
              return (
                <li key={model.id}>
                  <button
                    type="button"
                    onClick={() => onChange(selected ? null : model)}
                    aria-pressed={selected}
                    className={cn(
                      'flex w-full items-start gap-3 px-3 py-2.5 text-left transition-colors',
                      selected ? 'bg-primary-soft' : 'hover:bg-card-hover',
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className={cn('truncate text-sm font-medium', selected ? 'text-primary' : 'text-fg')}>
                          {model.id}
                        </span>
                        {model.num_parameters_b ? <Badge>{model.num_parameters_b}B</Badge> : null}
                        {model.is_multimodal && <Badge tone="info">多模态</Badge>}
                        {model.llamafactory_template && (
                          <Badge tone="primary">template: {model.llamafactory_template}</Badge>
                        )}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-fg-subtle">
                        <span>{model.model_type || '未知架构'}</span>
                        <span>{formatBytes(model.size_bytes)}</span>
                        {model.max_position_embeddings ? (
                          <span>上下文 {formatNumber(model.max_position_embeddings)}</span>
                        ) : null}
                        <span className="truncate">{model.path}</span>
                      </div>
                      {model.supported_frameworks.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {model.supported_frameworks.map((item) => (
                            <span key={item} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-fg-muted">
                              {FRAMEWORK_LABEL[item as keyof typeof FRAMEWORK_LABEL] ?? item}
                            </span>
                          ))}
                        </div>
                      )}
                      {selected && model.notes.length > 0 && (
                        <ul className="mt-1.5 space-y-0.5">
                          {model.notes.map((note) => (
                            <li key={note} className="text-[11px] leading-relaxed text-warning">
                              · {note}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Dataset picker                                                              */
/* -------------------------------------------------------------------------- */
export function DatasetPicker({
  value,
  onChange,
  kinds,
  projectId,
  multiple = false,
  emptyHint,
}: {
  value: string[]
  onChange: (ids: string[]) => void
  kinds: DatasetKind[]
  projectId: string
  multiple?: boolean
  emptyHint?: React.ReactNode
}) {
  const [keyword, setKeyword] = React.useState('')
  const [scope, setScope] = React.useState<'project' | 'external' | 'all'>('project')
  const [externalProject, setExternalProject] = React.useState('all')
  const projectNames = useProjectNames()
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api.datasets() })
  const matching = (datasets.data ?? []).filter(
    (dataset) => dataset.status === 'ready' && kinds.includes(dataset.kind),
  )
  const needle = keyword.trim().toLowerCase()
  const scoped = matching.filter((dataset) => {
    if (scope === 'project') return dataset.project_id === projectId
    if (scope === 'external') {
      return dataset.project_id !== projectId
        && (externalProject === 'all' || dataset.project_id === externalProject)
    }
    return true
  })
  const eligible = needle
    ? scoped.filter((dataset) =>
        `${projectNames.get(dataset.project_id) ?? ''} ${dataset.name} ${dataset.description} ${dataset.tags.join(' ')} ${dataset.owner_email}`
          .toLowerCase()
          .includes(needle),
      )
    : scoped
  const projectCount = matching.filter((dataset) => dataset.project_id === projectId).length
  const selectedDatasets = (datasets.data ?? []).filter((dataset) => value.includes(dataset.id))

  const toggle = (dataset: Dataset) => {
    if (multiple) {
      onChange(value.includes(dataset.id) ? value.filter((id) => id !== dataset.id) : [...value, dataset.id])
    } else {
      onChange(value.includes(dataset.id) ? [] : [dataset.id])
    }
  }

  if (datasets.isLoading) {
    return (
      <div className="flex items-center justify-center rounded-lg border border-border py-8">
        <Spinner />
      </div>
    )
  }

  const search = (
    <div className="relative">
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle"
        aria-hidden
      />
      <Input
        value={keyword}
        onChange={(event) => setKeyword(event.target.value)}
        placeholder={`按项目、名称搜索（共 ${scoped.length} 个可用数据集）`}
        className="pl-8"
        aria-label="搜索数据集"
      />
    </div>
  )

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap gap-1" role="group" aria-label="数据集项目范围">
        {([
          ['project', '本项目数据', projectCount],
          ['external', '外部数据', matching.length - projectCount],
          ['all', '全部数据', matching.length],
        ] as const).map(([key, label, count]) => (
          <button
            key={key}
            type="button"
            aria-pressed={scope === key}
            onClick={() => setScope(key)}
            className={cn(
              'rounded-md px-3 py-1.5 text-xs transition-colors',
              scope === key ? 'bg-primary-soft font-medium text-primary' : 'text-fg-muted hover:bg-card-hover',
            )}
          >
            {label}（{count}）
          </button>
        ))}
      </div>
      {scope === 'external' && (
        <Select
          value={externalProject}
          onValueChange={setExternalProject}
          options={[
            { value: 'all', label: '全部外部项目' },
            ...Array.from(projectNames)
              .filter(([id]) => id !== projectId)
              .map(([id, name]) => ({ value: id, label: name })),
          ]}
        />
      )}
      {search}
      {selectedDatasets.length > 0 && (
        <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-[11px] text-fg-muted">
          <span>已选 {selectedDatasets.length} 个：</span>
          {selectedDatasets.map((dataset) => (
            <button
              key={dataset.id}
              type="button"
              onClick={() => onChange(value.filter((id) => id !== dataset.id))}
              title="点击取消选择"
              className="max-w-full truncate rounded bg-primary-soft px-2 py-1 text-primary"
            >
              {projectNames.get(dataset.project_id) ?? dataset.project_id} / {dataset.name}
              {dataset.project_id !== projectId ? '（外部）' : ''} ×
            </button>
          ))}
        </div>
      )}
      {!eligible.length ? (
        <EmptyState
          icon={<Database className="h-6 w-6" aria-hidden />}
          title={needle ? '没有匹配的数据集' : scope === 'project' ? '本项目暂无可用数据' : '暂无符合条件的数据集'}
          description={scope === 'project'
            ? '可切换「外部数据」选择其他项目的数据，或上传数据到本项目。'
            : emptyHint ?? `请上传 ${kinds.join(' / ')} 类型的数据，或调整筛选。`}
        />
      ) : (
      <div className="min-w-0 max-h-64 space-y-1.5 overflow-y-auto rounded-md border border-border p-1.5">
      {eligible.map((dataset) => {
        const selected = value.includes(dataset.id)
        return (
          <button
            key={dataset.id}
            type="button"
            onClick={() => toggle(dataset)}
            aria-pressed={selected}
            className={cn(
              'flex w-full items-start justify-between gap-3 rounded-lg border px-3 py-2 text-left transition-colors',
              selected ? 'border-primary/50 bg-primary-soft' : 'border-transparent hover:bg-card-hover',
            )}
          >
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className={cn('truncate text-sm font-medium', selected ? 'text-primary' : 'text-fg')}>
                  {projectNames.get(dataset.project_id) ?? dataset.project_id} / {dataset.name}
                </span>
                <Badge>{dataset.kind}</Badge>
                {dataset.rewardkit && <Badge tone="primary">reward-kit</Badge>}
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-fg-subtle">
                {dataset.stats.n_tasks > 0 && <span>{formatNumber(dataset.stats.n_tasks)} 个任务</span>}
                {dataset.stats.n_records > 0 && <span>{formatNumber(dataset.stats.n_records)} 条样本</span>}
                <span>{formatBytes(dataset.stats.total_bytes)}</span>
                <span>{dataset.owner_email}</span>
              </div>
              {dataset.stats.warnings.length > 0 && (
                <p className="mt-1 break-words text-[11px] text-warning">⚠ {dataset.stats.warnings[0]}</p>
              )}
            </div>
              {selected && <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />}
            </button>
          )
        })}
      </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Key/value editor (env vars, agent kwargs, hydra overrides…)                 */
/* -------------------------------------------------------------------------- */
export function KeyValueEditor({
  value,
  onChange,
  keyPlaceholder = 'KEY',
  valuePlaceholder = 'value',
  secretKeys = true,
  addLabel = '添加一项',
}: {
  value: Record<string, string>
  onChange: (next: Record<string, string>) => void
  keyPlaceholder?: string
  valuePlaceholder?: string
  secretKeys?: boolean
  addLabel?: string
}) {
  const entries = Object.entries(value)

  const update = (index: number, key: string, entryValue: string) => {
    const next: Record<string, string> = {}
    entries.forEach(([existingKey, existingValue], position) => {
      if (position === index) next[key] = entryValue
      else next[existingKey] = existingValue
    })
    onChange(next)
  }

  const remove = (index: number) => {
    const next: Record<string, string> = {}
    entries.forEach(([key, entryValue], position) => {
      if (position !== index) next[key] = entryValue
    })
    onChange(next)
  }

  const isSecret = (key: string) =>
    secretKeys && /KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(key)

  return (
    <div className="space-y-1.5">
      {entries.map(([key, entryValue], index) => (
        <div key={index} className="flex items-center gap-1.5">
          <Input
            value={key}
            placeholder={keyPlaceholder}
            onChange={(event) => update(index, event.target.value, entryValue)}
            className="num flex-1"
            aria-label={`键 ${index + 1}`}
          />
          <Input
            value={entryValue}
            type={isSecret(key) ? 'password' : 'text'}
            placeholder={valuePlaceholder}
            onChange={(event) => update(index, key, event.target.value)}
            className="num flex-[1.4]"
            aria-label={`值 ${index + 1}`}
          />
          <Button variant="ghost" size="icon" onClick={() => remove(index)} aria-label="删除该项">
            ×
          </Button>
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        onClick={() => onChange({ ...value, '': '' })}
        disabled={Object.hasOwn(value, '')}
      >
        + {addLabel}
      </Button>
      {entries.some(([key]) => isSecret(key)) && (
        <p className="text-[11px] text-fg-subtle">
          含密钥的字段以密码形式显示，落库与日志中都会被替换为 <Mono>***</Mono>。
        </p>
      )}
    </div>
  )
}
