export type AgentTraceKind = 'user' | 'assistant' | 'tool' | 'other'

export interface AgentTraceEntry {
  id: string
  kind: AgentTraceKind
  title: string
  content: string
  source: string
  timestamp: string
}

type RecordValue = Record<string, unknown>
const record = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const text = (value: unknown): string => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2)
const timestampText = (value: unknown): string => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : ''
const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

export function formatAgentTraceTime(timestamp: string): string {
  if (!timestampText(timestamp)) return '时间未知'
  const parts = Object.fromEntries(timeFormatter.formatToParts(new Date(timestamp)).map(part => [part.type, part.value]))
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
}

/** Normalize bounded log tails; tolerate partial JSON records from running agents. */
export function parseAgentTrace(logs: { name: string; content: string }[]): AgentTraceEntry[] {
  const entries: AgentTraceEntry[] = []
  const seen = new Map<string, AgentTraceEntry>()
  let source = ''
  let timestamp = ''
  let eventId = ''
  let blockIndex = 0
  let hasStructuredEvents = false
  const add = (kind: AgentTraceKind, title: string, content: unknown) => {
    const body = text(content).trim()
    if (!body) return
    const key = eventId ? `${eventId}:${blockIndex++}` : ''
    const existing = key ? seen.get(key) : undefined
    if (existing) {
      if (!existing.timestamp && timestamp) existing.timestamp = timestamp
      return
    }
    const entry = { id: key || `${source}:${entries.length}`, kind, title: title.slice(0, 120), content: body, source, timestamp }
    entries.push(entry)
    if (key) seen.set(key, entry)
  }
  const blocks = (value: unknown, role: string) => {
    const kind = role === 'user' ? 'user' : role === 'assistant' || role === 'agent' ? 'assistant' : role === 'tool' ? 'tool' : 'other'
    const title = kind === 'user' ? '用户输入' : kind === 'assistant' ? '模型回复' : kind === 'tool' ? '工具返回' : '日志'
    if (!Array.isArray(value)) { add(kind, title, value); return }
    for (const item of value) {
      if (typeof item === 'string') { add(kind, title, item); continue }
      const block = record(item)
      const parentTimestamp = timestamp
      timestamp = timestampText(block.timestamp) || parentTimestamp
      const type = String(block.type ?? '')
      if (type === 'tool_use' || type === 'function_call') {
        add('tool', String(block.name ?? '工具调用'), block.input ?? block.arguments)
      } else if (type === 'tool_result' || type === 'function_call_output') {
        add('tool', block.is_error ? '工具错误' : '工具返回', block.content ?? block.output)
      } else if (type === 'thinking' || type === 'reasoning') {
        add('assistant', '模型思考', block.thinking ?? block.text ?? block.summary)
      } else if (block.text !== undefined) {
        add(kind, title, block.text)
      } else {
        add('other', String(block.type ?? '日志'), block)
      }
      timestamp = parentTimestamp
    }
  }
  const parse = (value: unknown) => {
    const event = record(value)
    if (!Object.keys(event).length) { add('other', '日志', value); return }
    const message = record(event.message)
    timestamp = timestampText(event.timestamp) || timestampText(message.timestamp) || timestamp
    if (Array.isArray(event.steps)) {
      for (const item of event.steps) {
        const step = record(item)
        eventId = `${source}:step:${step.step_id ?? entries.length}`
        const stepTimestamp = timestampText(step.timestamp)
        timestamp = stepTimestamp
        blockIndex = 0
        blocks(step.message, String(step.source ?? 'other'))
        for (const item of Array.isArray(step.tool_calls) ? step.tool_calls : []) {
          const call = record(item)
          const fn = record(call.function)
          timestamp = timestampText(call.timestamp) || stepTimestamp
          add('tool', String(call.function_name ?? fn.name ?? '工具调用'), call.arguments ?? fn.arguments)
        }
        const observation = record(step.observation)
        for (const item of Array.isArray(observation.results) ? observation.results : []) {
          const result = record(item)
          timestamp = timestampText(result.timestamp) || timestampText(observation.timestamp) || stepTimestamp
          add('tool', '工具返回', result.content ?? result)
        }
      }
      return
    }
    const type = String(event.type ?? '')
    if (type === 'response_item' || type === 'event_msg') {
      parse(event.payload)
      return
    }
    if (message.role || event.role || ['user', 'assistant'].includes(type)) {
      blocks(message.content ?? event.content ?? event.message, String(message.role ?? event.role ?? type))
    } else if (type === 'user_message' || type === 'agent_message') {
      blocks(event.message ?? event.text, type === 'user_message' ? 'user' : 'assistant')
    } else if (type === 'function_call' || type === 'function_call_output') {
      blocks([event], 'tool')
    } else if (type.startsWith('item.')) {
      const item = record(event.item)
      timestamp = timestampText(item.timestamp) || timestamp
      if (item.type === 'agent_message' || item.type === 'reasoning') {
        add('assistant', item.type === 'reasoning' ? '模型思考' : '模型回复', item.text)
      } else if (['command_execution', 'mcp_tool_call', 'file_change', 'web_search'].includes(String(item.type))) {
        add('tool', String(item.command ?? item.tool ?? item.type), item.aggregated_output || item.arguments || item.changes || item)
      } else {
        add('other', String(item.type ?? type), item)
      }
    } else if (type === 'result') {
      add(event.is_error ? 'other' : 'assistant', event.is_error ? '执行错误' : '模型回复', event.result || event.errors || event)
    } else {
      add('other', String(event.status ?? event.subtype ?? event.type ?? '日志'), event)
    }
  }
  for (const log of logs) {
    source = log.name
    const content = log.content.trim()
    if (!content) continue
    if (log.name.includes('tool-results/')) {
      timestamp = ''
      eventId = ''
      add('tool', '日志片段', content)
      continue
    }
    const lines = content.split('\n')
    let values: unknown[]
    try { values = [JSON.parse(content)] } catch { values = lines }
    for (const value of values) {
      timestamp = ''
      eventId = ''
      blockIndex = 0
      let event = value
      if (typeof value === 'string') {
        try { event = JSON.parse(value) } catch {
          add(log.name.includes('tool-results/') ? 'tool' : 'other', '日志片段', value)
          continue
        }
      }
      const object = record(event)
      if (object.type || object.role || object.steps || object.message) hasStructuredEvents = true
      timestamp = timestampText(object.timestamp)
      eventId = typeof object.uuid === 'string' ? object.uuid : ''
      parse(event)
    }
  }
  // Only reorder a fully timestamped stream; untimed CLI events retain their order.
  if (entries.every((entry) => entry.timestamp && Number.isFinite(Date.parse(entry.timestamp)))) {
    entries.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
  }
  // Raw tool-result files have no per-event time; their session records carry it.
  return entries.filter(entry => !hasStructuredEvents || !entry.source.includes('tool-results/')).slice(-300)
}
