const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')

function load(relative) {
  const source = fs.readFileSync(path.join(__dirname, '../src', relative), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const context = { exports: {}, require: () => ({}) }
  vm.runInNewContext(compiled, context)
  return context.exports
}
const { matchAgentModel, agentModelOptions, agentModelSelectValue, isFileDrag, AUTO_AGENT_MODEL } = load('lib/agentModel.ts')

const models = [
  { id: 'mdl_1', name: 'claude-opus-5', provider: 'anthropic', model_id: 'claude-opus-5', ready: true, note: '' },
  { id: 'mdl_2', name: 'claude-opus-5.5-ccmax', provider: 'anthropic', model_id: 'anthropic/claude-opus-5.5-ccmax', ready: false, note: '' },
]

test('agent model setting matches id, name or model id with anthropic/ prefix', () => {
  assert.equal(matchAgentModel(models, 'mdl_1').id, 'mdl_1')
  assert.equal(matchAgentModel(models, 'claude-opus-5.5-ccmax').id, 'mdl_2')
  assert.equal(matchAgentModel(models, 'anthropic/claude-opus-5.5-ccmax').id, 'mdl_2')
  assert.equal(matchAgentModel(models, ''), undefined)
  assert.equal(matchAgentModel(models, 'nope'), undefined)
})

test('select value maps empty to auto and ids to names', () => {
  assert.equal(agentModelSelectValue(models, ''), AUTO_AGENT_MODEL)
  assert.equal(agentModelSelectValue(models, 'mdl_2'), 'claude-opus-5.5-ccmax')
})

test('unknown configured model is still listed so admins can see it', () => {
  const values = Array.from(agentModelOptions(models, 'claude-opus-9'), (option) => option.value)
  assert.deepEqual(values, [AUTO_AGENT_MODEL, 'claude-opus-5', 'claude-opus-5.5-ccmax', 'claude-opus-9'])
  assert.equal(agentModelOptions(models, 'mdl_1').length, 3)
})

test('only file drags trigger the upload overlay', () => {
  assert.equal(isFileDrag(['Files']), true)
  assert.equal(isFileDrag(['text/plain']), false)
  assert.equal(isFileDrag(undefined), false)
})
