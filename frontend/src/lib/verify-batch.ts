import type { ModelApi, VerifyAgentConfig } from './types'

export function agentConfig(config: VerifyAgentConfig): VerifyAgentConfig {
  return {
    agent: config.agent, model_api_id: config.model_api_id,
    agent_api_protocol: config.agent_api_protocol, model: config.model,
    reasoning_effort: config.reasoning_effort, agent_import_path: config.agent_import_path,
    agent_env: config.agent_env, agent_kwargs: config.agent_kwargs,
  }
}

export function batchJobNames(name: string, agents: VerifyAgentConfig[], models: ModelApi[]): string[] {
  const used = new Set<string>()
  return agents.map(agent => {
    const model = agent.model_api_id ? models.find(item => item.id === agent.model_api_id)?.model_id ?? '' : agent.model
    const suffix = ['oracle', 'nop'].includes(agent.agent) ? agent.agent : model.trim().replace(/\/+$/, '').split('/').pop() || agent.agent
    const base = `${name.trim()}-${suffix}`
    let result = base
    let counter = 2
    while (used.has(result)) result = `${base}-${counter++}`
    used.add(result)
    return result
  })
}
