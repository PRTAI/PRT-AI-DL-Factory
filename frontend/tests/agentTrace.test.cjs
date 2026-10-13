const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')

function load(relative, modules = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../src', relative), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText
  const context = { exports: {}, require: name => modules[name] }
  vm.runInNewContext(compiled, context)
  return context.exports
}
const { parseAgentTrace } = load('lib/agentTrace.ts')
const log = (events, name = 'claude-code.txt') => ({ name, content: events.map(event => JSON.stringify(event)).join('\n') })
const json = value => JSON.parse(JSON.stringify(value))

test('Claude inputs, replies, thinking, calls, returns and status have separate categories', () => {
  const entries = parseAgentTrace([log([
    { type: 'user', message: { role: 'user', content: '请生成报告' } },
    { type: 'assistant', message: { content: [{ type: 'thinking', thinking: '先分析' }, { type: 'text', text: '开始生成' }, { type: 'tool_use', name: 'Bash', input: { command: 'python build.py' } }] } },
    { type: 'user', message: { role: 'user', content: [{ type: 'tool_result', content: '完成', is_error: false }] } },
    { type: 'system', subtype: 'status', status: 'compacting' },
    { type: 'result', is_error: true, result: 'Prompt is too long' },
  ])])
  assert.deepEqual(json(entries.map(entry => entry.kind)), ['user', 'assistant', 'assistant', 'tool', 'tool', 'other', 'other'])
  assert.equal(entries[3].title, 'Bash')
  assert.match(entries[3].content, /python build.py/)
  assert.equal(entries[4].title, '工具返回')
  assert.equal(entries[6].content, 'Prompt is too long')
})

test('duplicate Claude session and stream UUIDs merge and retain timestamps', () => {
  const event = { type: 'assistant', uuid: 'reply-1', message: { role: 'assistant', content: [{ type: 'text', text: '你好' }] } }
  const entries = parseAgentTrace([log([event]), log([{ ...event, timestamp: '2026-10-13T02:00:00Z' }], 'sessions/main.jsonl')])
  assert.equal(entries.length, 1)
  assert.equal(entries[0].timestamp, '2026-10-13T02:00:00Z')
})

test('Codex rollouts and exec streams distinguish users from tool calls', () => {
  const entries = parseAgentTrace([log([
    { type: 'event_msg', payload: { type: 'user_message', message: '检查数据' } },
    { type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: '读取中' }] } },
    { type: 'response_item', payload: { type: 'function_call', name: 'exec_command', arguments: '{"cmd":"ls"}' } },
    { type: 'item.completed', item: { type: 'command_execution', command: 'ls', aggregated_output: 'input.csv' } },
    { type: 'item.completed', item: { type: 'agent_message', text: '已完成' } },
  ], 'codex.txt')])
  assert.deepEqual(json(entries.map(entry => entry.kind)), ['user', 'assistant', 'tool', 'tool', 'assistant'])
  assert.equal(entries[3].content, 'input.csv')
})

test('Harbor trajectory steps preserve messages, calls and observations', () => {
  const entries = parseAgentTrace([{ name: 'trajectory.json', content: JSON.stringify({ steps: [
    { step_id: 1, source: 'user', message: '原始任务' },
    { step_id: 2, source: 'agent', message: '执行查询', tool_calls: [{ function_name: 'Bash', arguments: { command: 'ls' } }], observation: { results: [{ content: '文件列表' }] } },
  ] }) }])
  assert.deepEqual(json(entries.map(entry => entry.kind)), ['user', 'assistant', 'tool', 'tool'])
})

test('partial JSON, plain text and empty files do not break parsing', () => {
  const entries = parseAgentTrace([
    { name: 'empty.jsonl', content: '' },
    { name: 'claude-code.txt', content: 'fragment of truncated record\n{"type":"system","status":"compacting"}\n{"type":"assistant",' },
    { name: 'tool-results/output.txt', content: '工具输出' },
  ])
  assert.equal(entries.length, 4)
  assert.deepEqual(json(entries.map(entry => entry.kind)), ['other', 'other', 'other', 'tool'])
})

test('long content survives for details but titles and displayed history are bounded', () => {
  const content = '超长输出'.repeat(5000)
  const entries = parseAgentTrace([log([{ type: 'item.completed', item: { type: 'command_execution', command: 'x'.repeat(1000), aggregated_output: content } }])])
  assert.equal(entries[0].content, content)
  assert.equal(entries[0].title.length, 120)
  assert.equal(parseAgentTrace([{ name: 'stdout.txt', content: Array.from({ length: 400 }, (_, i) => `line ${i}`).join('\n') }]).length, 300)
})

test('cards use identical fixed dimensions, role colors and clipped previews', () => {
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const { AgentTrace } = load('components/AgentTrace.tsx', {
    react: React,
    '@/lib/agentTrace': { parseAgentTrace },
    '@/lib/utils': { cn: (...values) => values.filter(Boolean).join(' ') },
    './ui': { Dialog: () => null },
  })
  const html = renderToStaticMarkup(React.createElement(AgentTrace, { live: true, logs: [log([
    { type: 'user', message: { role: 'user', content: 'x'.repeat(5000) } },
    { type: 'assistant', message: { content: [{ type: 'text', text: '回复' }, { type: 'tool_use', name: 'Bash', input: {} }] } },
    { type: 'system', status: 'compacting' },
  ])] }))
  assert.equal((html.match(/h-28 w-full min-w-0/g) || []).length, 4)
  assert.equal((html.match(/line-clamp-2/g) || []).length, 4)
  for (const color of ['border-danger/50', 'border-yellow-500/50', 'border-primary/50', 'border-border-strong']) assert.ok(html.includes(color))
})
