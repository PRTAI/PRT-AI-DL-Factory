import type { DeploymentEndpoint, ModelApi, VerifyAgentConfig } from './types'

export function agentConfig(config: VerifyAgentConfig): VerifyAgentConfig {
  return {
    agent: config.agent, model_api_id: config.model_api_id, deployment_id: config.deployment_id ?? '',
    agent_api_protocol: config.agent_api_protocol, model: config.model,
    reasoning_effort: config.reasoning_effort, agent_import_path: config.agent_import_path,
    agent_env: config.agent_env, agent_kwargs: config.agent_kwargs,
  }
}

export function batchJobNames(name: string, agents: VerifyAgentConfig[], models: ModelApi[], endpoints: DeploymentEndpoint[] = []): string[] {
  const used = new Set<string>()
  return agents.map(agent => {
    const model = agent.deployment_id ? endpoints.find(item => item.id === agent.deployment_id)?.model ?? ''
      : agent.model_api_id ? models.find(item => item.id === agent.model_api_id)?.model_id ?? '' : agent.model
    const suffix = ['oracle', 'nop'].includes(agent.agent) ? agent.agent : model.trim().replace(/\/+$/, '').split('/').pop() || agent.agent
    const base = `${name.trim()}-${suffix}`
    let result = base
    let counter = 2
    while (used.has(result)) result = `${base}-${counter++}`
    used.add(result)
    return result
  })
}

export interface VerifyAgentRow {
  id: string
  config: VerifyAgentConfig
}

// These IDs only identify draft rows. Keep this updater pure and usable over LAN HTTP.
export function appendVerifyAgent(rows: VerifyAgentRow[], config: VerifyAgentConfig): VerifyAgentRow[] {
  const used = new Set(rows.map(row => row.id))
  let index = 1
  while (used.has(`agent-${index}`)) index++
  return [...rows, {
    id: `agent-${index}`,
    config: { ...agentConfig(config), agent_env: { ...config.agent_env }, agent_kwargs: { ...config.agent_kwargs } },
  }]
}
