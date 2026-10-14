const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/lib/trial-selection.ts'), 'utf8')
const context = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, context)
const { trialSelectionId, toggleTrialSelection } = context.exports

test('selection preserves other pages and distinguishes attempts of one task', () => {
  const first = { trial_name: 'task__one' }
  const second = { trial_name: 'task__two' }
  const third = { trial_name: 'other__one' }
  let selected = toggleTrialSelection([], [first], true)
  selected = toggleTrialSelection(selected, [second, third], true)
  assert.equal(selected.length, 3)
  selected = toggleTrialSelection(selected, [second, third], false)
  assert.equal(selected.length, 1)
  assert.equal(selected[0], first)
  assert.equal(toggleTrialSelection(selected, [first], true).length, 1)
})

test('unstarted attempts with identical display names have separate stable identities', () => {
  const rows = [{ trial_name: 'task (待开始)', selection_id: '@planned:1' }, { trial_name: 'task (待开始)', selection_id: '@planned:2' }]
  assert.notEqual(trialSelectionId(rows[0]), trialSelectionId(rows[1]))
  assert.equal(toggleTrialSelection([], rows, true).length, 2)
})
