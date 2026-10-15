import type { ApiProtocol, DeploymentApiProtocol, ModelApi } from './types'

export function supportsProtocol(model: ModelApi, protocol: ApiProtocol) {
  return model.supported_protocols?.includes(protocol) && Boolean(model.endpoints?.[protocol])
}

export const DEPLOYMENT_PROTOCOL_LABELS: Record<DeploymentApiProtocol, string> = {
  openai: 'OpenAI Chat',
  responses: 'OpenAI Responses',
  anthropic: 'Anthropic Messages',
}

export function protocolLabels(protocols: ApiProtocol[]) {
  const labels = []
  if (protocols.includes('openai') || protocols.includes('responses')) labels.push('OpenAI')
  if (protocols.includes('anthropic')) labels.push('Anthropic')
  return labels.join(' / ') || '原有接口'
}
