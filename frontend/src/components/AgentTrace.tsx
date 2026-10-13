import * as React from 'react'
import { parseAgentTrace } from '@/lib/agentTrace'
import type { AgentTraceEntry, AgentTraceKind } from '@/lib/agentTrace'
import { cn } from '@/lib/utils'
import { Dialog } from './ui'

const KINDS: Record<AgentTraceKind, { label: string; className: string }> = {
  user: { label: '用户输入', className: 'border-danger/50 bg-danger-soft' },
  assistant: { label: '模型回复', className: 'border-yellow-500/50 bg-yellow-500/10' },
  tool: { label: '工具调用 / 返回', className: 'border-primary/50 bg-primary-soft' },
  other: { label: '其他', className: 'border-border-strong bg-muted' },
}

export function AgentTrace({ logs, live }: { logs: { name: string; content: string }[]; live: boolean }) {
  const entries = React.useMemo(() => parseAgentTrace(logs), [logs])
  const [selected, setSelected] = React.useState<AgentTraceEntry | null>(null)
  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-fg-muted">
        {Object.entries(KINDS).map(([kind, meta]) => (
          <span key={kind} className={cn('rounded border px-2 py-0.5', meta.className)}>{meta.label}</span>
        ))}
        <span className="ml-auto">最近 {entries.length} 条 · 点击查看详情</span>
      </div>
      <div className="max-h-[50vh] min-w-0 space-y-2 overflow-y-auto overflow-x-hidden rounded-md border border-border p-2" aria-label="Agent 输出轨迹">
        {entries.map((entry, index) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => setSelected(entry)}
            className={cn('flex h-28 w-full min-w-0 flex-col gap-2 overflow-hidden rounded-lg border border-l-4 p-3 text-left transition-shadow hover:shadow-sm focus-visible:outline-2 focus-visible:outline-primary', KINDS[entry.kind].className)}
            aria-label={`${KINDS[entry.kind].label} ${index + 1}：${entry.title}`}
          >
            <div className="flex w-full min-w-0 shrink-0 items-center gap-2 text-[11px]">
              <span className="num shrink-0 text-fg-subtle">{index + 1}</span>
              <span className="shrink-0 font-semibold text-fg">{KINDS[entry.kind].label}</span>
              <span className="min-w-0 flex-1 truncate text-fg-muted">{entry.title !== KINDS[entry.kind].label ? entry.title : ''}</span>
              <span className="hidden max-w-[25%] truncate text-fg-subtle sm:block" title={entry.source}>{entry.source.split('/').pop()}</span>
              {entry.timestamp && <time className="num shrink-0 text-fg-subtle" dateTime={entry.timestamp}>{Number.isFinite(Date.parse(entry.timestamp)) ? new Date(entry.timestamp).toLocaleTimeString('zh-CN', { hour12: false }) : ''}</time>}
            </div>
            <div className="num line-clamp-2 w-full min-w-0 whitespace-pre-wrap break-words text-xs leading-5 text-fg-muted [overflow-wrap:anywhere]">{entry.content}</div>
          </button>
        ))}
        {!entries.length && <p className="p-3 text-xs text-fg-muted">{live ? '暂未产生 Agent 输出，等待下一次刷新。' : '没有可显示的 Agent 输出。'}</p>}
      </div>
      <Dialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)} title={selected ? selected.title === KINDS[selected.kind].label ? selected.title : `${KINDS[selected.kind].label} · ${selected.title}` : '轨迹详情'} width="max-w-4xl">
        <p className="mb-2 break-all text-[11px] text-fg-subtle">{selected?.source}</p>
        <pre className="num max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 text-xs text-fg-muted [overflow-wrap:anywhere]">{selected?.content}</pre>
      </Dialog>
    </div>
  )
}
