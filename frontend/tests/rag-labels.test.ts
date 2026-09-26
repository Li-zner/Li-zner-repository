/** RAG 控制台中文标签映射回归。 */
import { describe, expect, it } from 'vitest'
import {
  label_action,
  label_persona,
  label_retrieval_method,
  label_risk,
  label_route,
  label_severity,
  label_stage,
  label_status,
} from '../src/utils/ragLabels'

describe('RAG 中文标签', () => {
  it('状态、级别和动作统一转中文', () => {
    expect(label_status('waiting_approval')).toBe('待审批')
    expect(label_status('succeeded')).toBe('成功')
    expect(label_severity('medium')).toBe('中')
    expect(label_risk('L1')).toBe('可逆（L1）')
    expect(label_action('rerun_diagnosis')).toBe('重跑诊断')
  })

  it('路径、阶段和召回方法统一转中文', () => {
    expect(label_route('simple_fast_path')).toBe('简单快速路径')
    expect(label_stage('law_mapping')).toBe('法律术语映射')
    expect(label_persona('civil_code')).toBe('民法典')
    expect(label_retrieval_method('trgm+vector+rrf+local_rerank'))
      .toBe('关键词 + 向量 + 融合 + 本地重排')
  })
})
