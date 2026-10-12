import type { TaskTimeoutValue, VerifierEnvInfo, VerifyConfig } from '@/lib/types'
import { Button } from '@/components/ui'

const sources = { task: 'task.toml', step: '步骤配置', harbor_default: 'Harbor 默认', step_default: 'Harbor 步骤默认', unavailable: '无法读取步骤默认值', invalid: '配置无效' }
const secondsText = (seconds: number) => `${seconds.toLocaleString()} 秒（${Number((seconds / 60).toFixed(1))} 分钟）`

export function TaskTimeoutPanel({ data, loading, error, registry, config, onRefresh, canDetect }: {
  data?: VerifierEnvInfo
  loading: boolean
  error?: string
  registry: boolean
  config: VerifyConfig
  onRefresh: () => void
  canDetect: boolean
}) {
  const describe = (value: TaskTimeoutValue, phase: 'agent' | 'verifier') => {
    const defaults = data?.timeout_defaults
    const unknown = value.source === 'invalid' || value.source === 'unavailable' || (value.source === 'harbor_default' && !defaults?.available)
    const seconds = value.source === 'harbor_default' ? defaults?.[phase] : value.seconds
    const multiplier = (phase === 'agent' ? config.agent_timeout_multiplier : config.verifier_timeout_multiplier) ?? config.timeout_multiplier
    const cap = phase === 'agent' ? config.agent_timeout_seconds : config.verifier_timeout_seconds
    const effective = unknown || seconds === undefined ? null : seconds === null ? cap : Math.min(seconds * multiplier, cap)
    return <div className="space-y-1">
      <div>{unknown ? '无法确定' : seconds == null ? '未设时限' : secondsText(seconds)}</div>
      <div className="text-[11px] text-fg-subtle">{sources[value.source]} · 倍率 ×{multiplier}</div>
      <div className="text-primary">{phase === 'verifier' && config.disable_verification ? '已跳过判分' : `生效：${effective == null ? '待确认' : secondsText(effective)}`}</div>
    </div>
  }
  return <div className="min-w-0 space-y-2 rounded-lg border border-border p-3 text-xs">
    <div className="flex items-center justify-between gap-2">
      <span className="font-medium">任务超时（自动读取 task.toml）</span>
      <Button size="sm" variant="ghost" disabled={!canDetect || loading} onClick={onRefresh}>重新检测</Button>
    </div>
    <p className="text-fg-muted">读取 [agent].timeout_sec 和 [verifier].timeout_sec（秒）。生效时限为任务时限 × 阶段倍率与平台上限中的较小值；阶段倍率未填时使用全局倍率。Verifier 时限包含本题全部判分调用。</p>
    {Object.keys(config.extra_args).some((key) => /timeout|config/.test(key)) && <p className="text-warning">额外 CLI 参数可能覆盖超时设置；以下按表单配置计算，最终以 Harbor 执行配置为准。</p>}
    {data?.timeout_defaults?.version && <p className="text-fg-subtle">服务器 Harbor {data.timeout_defaults.version} · 平台上限：Agent {config.agent_timeout_seconds} 秒 / Verifier {config.verifier_timeout_seconds} 秒</p>}
    {loading && <p>正在读取任务配置…</p>}
    {error && <p className="text-danger">{error}</p>}
    {!canDetect && <p className="text-fg-subtle">{registry ? '注册表任务下载前无法读取超时配置；可导入本地任务集后检测。' : '选择任务集或填写服务器任务目录后自动检测。'}</p>}
    {data && !data.task_timeouts && <p className="text-warning">后端尚未提供超时检测，请更新后端服务。</p>}
    {data?.task_timeouts?.length === 0 && <p className="text-fg-subtle">未找到可解析的 task.toml。</p>}
    {!!data?.task_timeouts?.length && <>
      <p className="text-fg-subtle">共 {data.n_tasks} 个任务；下表显示整个任务集，实际执行以任务筛选为准。多步骤任务分别列出。</p>
      <div className="max-h-72 overflow-auto">
        <table className="w-full min-w-[540px] text-left">
          <thead><tr className="border-b border-border"><th className="p-2">任务 / 步骤</th><th className="p-2">Agent</th><th className="p-2">Verifier</th></tr></thead>
          <tbody>{data.task_timeouts.map((row, index) => <tr key={`${row.task}/${row.step}/${index}`} className="border-b border-border last:border-0">
            <td className="max-w-56 break-all p-2 align-top">{row.task === '.' ? '当前任务' : row.task}{row.step && <div className="text-fg-subtle">步骤：{row.step}</div>}</td>
            <td className="p-2 align-top">{describe(row.agent, 'agent')}</td>
            <td className="p-2 align-top">{describe(row.verifier, 'verifier')}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
    {data?.warnings.map((warning, index) => <p key={index} className="text-warning">{warning}</p>)}
  </div>
}
