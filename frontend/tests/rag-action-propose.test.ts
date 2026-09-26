/**
 * 业务动作提议表单回归：参数拼装、提交前门禁与提交后的中文提示口径。
 *
 * 这里只挂载动作分区组件，真实执行发生在 worker 侧，本文件锁住的是
 * "操作者点提交时到底往控制面写了什么"。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RagActionWorkspace from '../src/components/RagActionWorkspace.vue'
import { proposeRagAction } from '../src/api/ragAdmin'

vi.mock('../src/api/ragAdmin', () => ({
  proposeRagAction: vi.fn(),
}))

/** 构造一条提议响应：status 决定前端提示走待审批还是仅提案。 */
function proposed(status: string, actionId = 7) {
  return {
    action_id: actionId,
    incident_id: null,
    request_uid: null,
    action_key: 'set_user_active',
    risk_level: 'L2',
    status,
    params: {},
    reason: '风控处置',
    result: {},
    verification: {},
    rollback_plan: '',
    rollback_result: {},
    rollback_available: false,
    policy_decision_id: null,
    environment: 'local',
    config_version: 'test',
    requested_by: 'admin',
    approved_by: null,
    executed_by: null,
    attempt_count: 0,
    created_at: '2026-09-24T10:00:00',
    started_at: null,
    ended_at: null,
    verifications: [],
  }
}

function factory() {
  return mount(RagActionWorkspace, {
    props: { actions: [], selected: null },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(proposeRagAction).mockResolvedValue(proposed('waiting_approval'))
})

describe('RagActionWorkspace 提议表单', () => {
  it('停用账号提交的是 set_user_active 且 active 为 false', async () => {
    const wrapper = factory()
    await wrapper.get('.propose-panel select').setValue('user_disable')
    await wrapper.get('.propose-panel input').setValue('  u_1  ')
    await wrapper.get('textarea').setValue('多次恶意刷接口，先停用')

    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(proposeRagAction).toHaveBeenCalledWith(
      'set_user_active',
      { username: 'u_1', active: false },
      '多次恶意刷接口，先停用',
    )
    expect(wrapper.text()).toContain('批准后台才会执行')
    expect(wrapper.emitted('refresh')).toHaveLength(1)
    // 提交成功后清空输入，避免误把上一个目标当成新目标重复提交
    const targetInput = wrapper.get('.propose-panel input').element as HTMLInputElement
    expect(targetInput.value).toBe('')
  })

  it('恢复账号复用同一动作键但目标态为 true', async () => {
    const wrapper = factory()
    await wrapper.get('.propose-panel select').setValue('user_enable')
    await wrapper.get('.propose-panel input').setValue('u_1')
    await wrapper.get('textarea').setValue('申诉通过，恢复登录')

    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(proposeRagAction).toHaveBeenCalledWith(
      'set_user_active',
      { username: 'u_1', active: true },
      '申诉通过，恢复登录',
    )
  })

  it('渠道动作传的是 channel_code 而不是账号', async () => {
    const wrapper = factory()
    await wrapper.get('.propose-panel select').setValue('channel_off')
    await wrapper.get('.propose-panel input').setValue('wechat_jsapi')
    await wrapper.get('textarea').setValue('渠道回调异常，先关闭')

    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(proposeRagAction).toHaveBeenCalledWith(
      'set_channel_active',
      { channel_code: 'wechat_jsapi', active: false },
      '渠道回调异常，先关闭',
    )
  })

  it('理由不足 4 个字时提交按钮保持禁用', () => {
    const wrapper = factory()
    wrapper.get('.propose-panel input').setValue('u_1')
    wrapper.get('textarea').setValue('太长')

    expect(wrapper.get('button.primary').attributes('disabled')).toBeDefined()
    expect(proposeRagAction).not.toHaveBeenCalled()
  })

  it('余额调整只登记提案，无金额时前端先拦住', async () => {
    const wrapper = factory()
    await wrapper.get('.propose-panel select').setValue('wallet')
    await wrapper.get('.propose-panel input').setValue('u_1')
    await wrapper.get('textarea').setValue('客服核对后建议补发差额')

    // 钱包选项才会出现的第二个输入框（金额）
    const inputs = wrapper.findAll('.propose-panel input')
    expect(inputs).toHaveLength(2)
    // 金额留空时按钮直接禁用（canSubmit 拦在更前面）
    expect(wrapper.get('button.primary').attributes('disabled')).toBeDefined()
    expect(proposeRagAction).not.toHaveBeenCalled()

    // 填了但金额无效（0 元 = 无变化）时，由 submit 的兜底校验拦住
    await inputs[1].setValue('0')
    await wrapper.get('button.primary').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('请填写非零的调整金额')
    expect(proposeRagAction).not.toHaveBeenCalled()

    await inputs[1].setValue('-50')
    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(proposeRagAction).toHaveBeenCalledWith(
      'propose_wallet_adjustment',
      { username: 'u_1', amount: -50 },
      '客服核对后建议补发差额',
    )
  })

  it('后端返回 proposed 时提示不会自动执行', async () => {
    vi.mocked(proposeRagAction).mockResolvedValue(proposed('proposed'))
    const wrapper = factory()
    await wrapper.get('.propose-panel select').setValue('wallet')
    await wrapper.get('.propose-panel input').setValue('u_1')
    await wrapper.findAll('.propose-panel input')[1].setValue('30')
    await wrapper.get('textarea').setValue('补发优惠券差额')

    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('等待工程评审')
    expect(wrapper.text()).toContain('控制面不会执行它')
  })

  it('技术明细默认收起，展开后才显示动作键与参数', async () => {
    const wrapper = factory()
    expect(wrapper.find('.tech').exists()).toBe(false)

    await wrapper.get('.propose-panel .text-button').trigger('click')
    await wrapper.get('.propose-panel input').setValue('u_1')

    const tech = wrapper.get('.tech').text()
    expect(tech).toContain('set_user_active')
    expect(tech).toContain('"active":false')
  })

  it('接口报错时只提示错误文案，不吞掉表单', async () => {
    vi.mocked(proposeRagAction).mockRejectedValue(new Error('动作状态校验未通过'))
    const wrapper = factory()
    await wrapper.get('.propose-panel input').setValue('u_1')
    await wrapper.get('textarea').setValue('风控处置')

    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(wrapper.get('.hint.danger').text()).toBe('动作状态校验未通过')
    expect(wrapper.get('textarea').element.value).toBe('风控处置')
    expect(wrapper.emitted('refresh')).toBeUndefined()
  })
})
