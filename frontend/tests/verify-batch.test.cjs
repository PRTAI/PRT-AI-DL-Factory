const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/lib/verify-batch.ts'), 'utf8')
const context = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, context)
const { batchJobNames, agentConfig } = context.exports
const plain = value => JSON.parse(JSON.stringify(value))

test('batch preview uses final model segment and Oracle agent name', () => {
  assert.deepEqual(plain(batchJobNames(' 金融评测 ', [
    { agent: 'oracle', model: 'stale/model' },
    { agent: 'claude-code', model: 'claude/claude-opus-5' },
    { agent: 'codex', model_api_id: 'api1' },
  ], [{ id: 'api1', name: 'Friendly name', model_id: 'openai/gpt-test' }])), [
    '金融评测-oracle', '金融评测-claude-opus-5', '金融评测-gpt-test',
  ])
})

test('batch preview resolves collisions including names with numeric suffix', () => {
  assert.deepEqual(plain(batchJobNames('test', [
    { agent: 'oracle' }, { agent: 'oracle' }, { agent: 'codex', model: 'oracle-2' },
  ], [])), ['test-oracle', 'test-oracle-2', 'test-oracle-2-2'])
})

test('per-agent overrides exclude dataset, Judge and resources', () => {
  const result = agentConfig({ agent: 'oracle', agent_env: {}, agent_kwargs: {}, dataset_id: 'dataset', judge_model_api_id: 'judge', n_concurrent: 8 })
  assert.equal(result.agent, 'oracle')
  assert.equal('dataset_id' in result, false)
  assert.equal('judge_model_api_id' in result, false)
  assert.equal('n_concurrent' in result, false)
})

test('adding agents works without crypto in a LAN HTTP context and preserves existing rows', () => {
  const config = { agent: 'claude-code', model: '', agent_env: {}, agent_kwargs: {} }
  const { appendVerifyAgent } = context.exports
  const initial = [{ id: 'agent-1', config: { ...config, model: 'existing-model' } }]
  const added = appendVerifyAgent(initial, config)
  assert.equal(initial.length, 1)
  assert.equal(added.length, 2)
  assert.equal(added[0].config.model, 'existing-model')
  assert.equal(added[1].id, 'agent-2')
  assert.deepEqual(plain(appendVerifyAgent(initial, config)), plain(added))
  added[1].config.agent_env.API_KEY = 'new-row-only'
  assert.equal(config.agent_env.API_KEY, undefined)
  assert.equal(added[0].config.agent_env.API_KEY, undefined)
  const removed = added.filter(row => row.id !== 'agent-1')
  const readded = appendVerifyAgent(removed, config)
  assert.equal(new Set(readded.map(row => row.id)).size, 2)
})

test('bridge mode is per agent and requires Responses without changing native Claude', () => {
  const { agentWireProtocol } = context.exports
  assert.equal(agentWireProtocol({ agent: 'claude-code', use_openai_bridge: true }), 'responses')
  assert.equal(agentWireProtocol({ agent: 'claude-code' }), 'anthropic')
  assert.equal(agentWireProtocol({ agent: 'codex' }), 'responses')
  const rows = context.exports.appendVerifyAgent([], { agent: 'claude-code', use_openai_bridge: true, agent_env: {}, agent_kwargs: {} })
  assert.equal(rows[0].config.use_openai_bridge, true)
  assert.equal(agentConfig({ agent: 'oracle', agent_env: {}, agent_kwargs: {} }).use_openai_bridge, false)
})

test('single and batch submissions normalize saved protocol using the bridge switch', () => {
  const saved = { agent: 'claude-code', agent_api_protocol: 'anthropic', use_openai_bridge: true, agent_env: {}, agent_kwargs: {} }
  const single = agentConfig(saved)
  assert.equal(single.use_openai_bridge, true)
  assert.equal(single.agent_api_protocol, 'openai')
  const added = context.exports.appendVerifyAgent([], saved)
  assert.equal(added[0].config.agent_api_protocol, 'openai')
  assert.equal(agentConfig({ ...single, use_openai_bridge: false }).agent_api_protocol, 'anthropic')
  assert.equal(agentConfig({ ...saved, agent: 'codex', use_openai_bridge: false }).agent_api_protocol, 'openai')
})
