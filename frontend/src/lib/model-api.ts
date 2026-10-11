import type { ApiProtocol, ModelApi } from './types'

export function supportsProtocol(model: ModelApi, protocol: ApiProtocol) {
  return model.supported_protocols?.includes(protocol) && Boolean(model.endpoints?.[protocol])
}

export function protocolLabels(protocols: ApiProtocol[]) {
  const labels = []
  if (protocols.includes('openai') || protocols.includes('responses')) labels.push('OpenAI')
  if (protocols.includes('anthropic')) labels.push('Anthropic')
  return labels.join(' / ') || '原有接口'
}
