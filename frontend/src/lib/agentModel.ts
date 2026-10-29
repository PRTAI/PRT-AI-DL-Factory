/**
 * 内置 Agent 的模型由管理员在「系统设置」里统一配置（SystemSettings.agent_model）。
 * 设置值与后端 agent_service._match_platform_model 同一口径：可以是模型配置的
 * ID、名称或模型标识，模型标识可以带 anthropic/ 前缀。
 */
import type { AgentModelOption } from './types'

const bare = (value: string) => value.trim().replace(/^anthropic\//, '')

export function matchAgentModel(models: AgentModelOption[], value: string): AgentModelOption | undefined {
  const target = value.trim()
  if (!target) return undefined
  return (
    models.find((model) => model.id === target) ??
    models.find((model) =>
      [model.name.trim(), model.model_id, bare(model.model_id)].some(
        (name) => name === target || name === bare(target),
      ),
    )
  )
}

/** Select 不接受空值，「自动选择」用这个占位。 */
export const AUTO_AGENT_MODEL = '__auto__'

export function agentModelOptions(models: AgentModelOption[], value: string) {
  const options = [
    { value: AUTO_AGENT_MODEL, label: '自动选择', description: '每个账号用第一个可用的 Anthropic 协议模型' },
    ...models.map((model) => ({ value: model.name, label: model.name, description: model.model_id })),
  ]
  // 设置里的模型在模型配置中找不到（未添加或已停用）时也要显示出来，提醒管理员处理
  if (value.trim() && !matchAgentModel(models, value)) {
    options.push({
      value: value.trim(),
      label: `${value.trim()}（未找到）`,
      description: '模型配置中没有启用的同名 Anthropic 模型',
    })
  }
  return options
}

export function agentModelSelectValue(models: AgentModelOption[], value: string): string {
  if (!value.trim()) return AUTO_AGENT_MODEL
  return matchAgentModel(models, value)?.name ?? value.trim()
}

/** 拖进来的是不是文件（拖文字、链接时不显示上传遮罩）。 */
export function isFileDrag(types: ArrayLike<string> | undefined): boolean {
  return Boolean(types && Array.from(types).includes('Files'))
}
