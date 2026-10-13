import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import type { Job } from '@/lib/types'
import { Button, Checkbox, Dialog, ErrorBox, Field, Select } from '@/components/ui'

export function HarborActions({ job }: { job: Job }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [mode, setMode] = React.useState<'resume' | 'regrade' | null>(null)
  const [filters, setFilters] = React.useState(['CancelledError'])
  const [dataset, setDataset] = React.useState('keep')
  const [judge, setJudge] = React.useState('keep')
  const [protocol, setProtocol] = React.useState<'anthropic' | 'openai'>(job.verify?.judge_api_protocol ?? 'anthropic')
  const info = useQuery({
    queryKey: ['harbor-actions', job.id], queryFn: () => api.harborActions(job.id),
    enabled: mode !== null, retry: false,
  })
  const datasets = useQuery({
    queryKey: ['datasets', 'harbor-actions'], queryFn: () => api.datasets({ kind: 'harbor_tasks' }),
    enabled: mode === 'regrade',
  })
  const models = useQuery({
    queryKey: ['model-apis'], queryFn: api.modelApis, enabled: mode === 'regrade',
  })
  const create = useMutation({
    mutationFn: () => api.createHarborAction(job.id, mode === 'resume' ? {
      mode, filter_error_types: filters,
    } : {
      mode: 'regrade',
      ...(dataset !== 'keep' ? { dataset_id: dataset } : {}),
      ...(judge !== 'keep' ? { judge_model_api_id: judge, judge_api_protocol: protocol } : {}),
    }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      toast.success(created.status === 'pending_review' ? '已提交，等待审核' : '已提交到调度队列')
      setMode(null)
      navigate(`/jobs/${created.id}`)
    },
  })
  const open = (next: 'resume' | 'regrade') => {
    create.reset()
    setFilters(['CancelledError'])
    setDataset('keep')
    setJudge('keep')
    setProtocol(job.verify?.judge_api_protocol ?? 'anthropic')
    setMode(next)
  }
  const errorTypes = Array.from(new Set(['CancelledError', ...Object.keys(info.data?.error_types ?? {})]))
  const blocked = !info.data || info.isError || (mode === 'resume' ? !filters.length : Boolean(info.data.regrade_error))

  return <>
    <Button variant="secondary" size="sm" onClick={() => open('resume')}>中断恢复</Button>
    <Button variant="secondary" size="sm" onClick={() => open('regrade')}>重新判分</Button>
    <Dialog open={mode !== null} onOpenChange={(value) => { if (!value && !create.isPending) setMode(null) }}
      title={mode === 'resume' ? '恢复中断的评测' : '使用已有产物重新判分'}
      description="创建新任务，沿用平台审核与调度流程，保留原任务记录。"
      footer={<>
        <Button variant="ghost" disabled={create.isPending} onClick={() => setMode(null)}>取消</Button>
        <Button loading={create.isPending} disabled={blocked} onClick={() => create.mutate()}>
          {mode === 'resume' ? '提交恢复任务' : '提交判分任务'}
        </Button>
      </>}>
      <div className="space-y-4 text-xs">
        {info.isLoading && <p className="text-fg-muted">正在检查 Harbor 原始记录…</p>}
        {info.isError && <ErrorBox message={info.error instanceof ApiError ? info.error.message : '无法检查原始记录'} />}
        {info.data && <p className="text-fg-muted">
          已记录 {info.data.recorded_trials} 个 trial，另有 {info.data.unfinished_trials} 个目录尚无有效结果。
        </p>}
        {mode === 'resume' ? <>
          <p className="leading-relaxed text-fg-muted">保留已完成结果，补跑尚未开始、未完成及下方选定错误类型的 trial。中断的 trial 会从头执行，不会恢复 Agent 对话、容器文件或进程。</p>
          <Field label="需要重新执行的错误类型">
            <div className="space-y-2">
              {errorTypes.map((value) => <label key={value} className="flex items-center gap-2">
                <Checkbox checked={filters.includes(value)} onCheckedChange={(checked) => setFilters((current) =>
                  checked ? [...current, value] : current.filter((item) => item !== value))} />
                <span>{value}（{info.data?.error_types[value] ?? 0} 个）</span>
              </label>)}
            </div>
          </Field>
        </> : <>
          <p className="leading-relaxed text-fg-muted">仅运行评分器。需要 trial 的 result.json、产物清单及实际产物；评分题包必须使用独立 verifier，并从保存的产物读取输入。只有散装产物文件的目录暂不支持。</p>
          {info.data?.regrade_error && <ErrorBox message={info.data.regrade_error} />}
          <Field label="评分任务集" hint="任务名称须与原记录一致。可选择更新后的评分题包。">
            <Select value={dataset} onValueChange={setDataset} options={[
              { value: 'keep', label: '沿用原任务集' },
              ...(datasets.data ?? []).filter((item) => item.status === 'ready').map((item) => ({ value: item.id, label: item.name })),
            ]} />
          </Field>
          <Field label="Judge 模型">
            <Select value={judge} onValueChange={setJudge} options={[
              { value: 'keep', label: '沿用原 Judge 配置' },
              ...(models.data ?? []).filter((item) => item.ready).map((item) => ({ value: item.id, label: item.name })),
            ]} />
          </Field>
          {judge !== 'keep' && <Field label="Judge 协议">
            <Select value={protocol} onValueChange={(value) => setProtocol(value as typeof protocol)} options={[
              { value: 'anthropic', label: 'Anthropic Messages' }, { value: 'openai', label: 'OpenAI Chat Completions' },
            ]} />
          </Field>}
          <p className="text-fg-subtle">并发数和沙箱类型沿用原配置；评分资源和超时取评分题包配置。</p>
        </>}
        {create.isError && <ErrorBox message={create.error instanceof ApiError ? create.error.message : '提交失败'} />}
      </div>
    </Dialog>
  </>
}
