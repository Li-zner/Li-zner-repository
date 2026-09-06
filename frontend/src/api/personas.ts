/** 人格列表 API（GET /api/personas） */
import { get } from './http'

export interface Persona {
  id: string
  name: string
  icon: string
  model: string
  tools_enabled: string[]
  knowledge_base: string
}

export interface PersonaListResponse {
  personas: Persona[]
  current: string
}

export function fetchPersonas(): Promise<PersonaListResponse> {
  return get<PersonaListResponse>('/api/personas')
}
