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
