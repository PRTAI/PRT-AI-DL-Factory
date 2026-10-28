const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/lib/project-scope.ts'), 'utf8')
const context = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, context)
const { ALL_PROJECTS, projectScopeValue, projectIdFromScope } = context.exports

test('dataset filter defaults to the globally selected project', () => {
  assert.equal(projectScopeValue('proj-123'), 'proj-123')
  assert.equal(projectScopeValue(''), ALL_PROJECTS)
})

test('choosing all projects clears the global selection, choosing one selects it', () => {
  assert.equal(projectIdFromScope(ALL_PROJECTS), '')
  assert.equal(projectIdFromScope('proj-456'), 'proj-456')
})

test('scope conversions round-trip for a selected project', () => {
  assert.equal(projectIdFromScope(projectScopeValue('proj-789')), 'proj-789')
})
