/**
 * 中控台「问数」分区回归（P1 只读观测）。
 *
 * 钉的是界面契约而不是样式：白名单表要显示成人话、结果行数与截断标记不能吞、
 * SQL 默认收进技术明细（操作者不该先读 SQL），以及追问必须把上一轮问答带回去——
 * 少带一轮，后端就会重新猜表与口径。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import RagDataAskPanel from '../src/components/RagDataAskPanel.vue'
import { askData, listDataTables } from '../src/api/ragAdmin'

vi.mock('../src/api/ragAdmin', () => ({
  listDataTables: vi.fn(),
  askData: vi.fn(),
}))

const TABLES = [
  { name: 'payment_orders', usage: '支付订单主表，主键是 order_no' },
  { name: 'requests', usage: '一次问答的请求流水' },
]

function result(overrides: Record<string, unknown> = {}) {
  return {
    sql: 'SELECT count(*) AS c FROM payment_orders',
    columns: ['c'],
    rows: [{ c: 1956 }],
    row_count: 1,
    truncated: false,
    elapsed_ms: 1234,
    answer: '共有 1956 笔支付订单。',
    ...overrides,
  }
}

async function mounted() {
  const wrapper = mount(RagDataAskPanel)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listDataTables).mockResolvedValue({ tables: TABLES })
  vi.mocked(askData).mockResolvedValue(result() as never)
})

it('把只读白名单表与用途说明显示出来', async () => {
  const wrapper = await mounted()
  expect(listDataTables).toHaveBeenCalledTimes(1)
  const text = wrapper.text()
  expect(text).toContain('payment_orders')
  expect(text).toContain('支付订单主表，主键是 order_no')
})

it('提问走只读端点，并把结论与数据行渲染出来', async () => {
  const wrapper = await mounted()
  await wrapper.find('textarea').setValue('一共有多少笔支付订单？')
  await wrapper.find('button.primary').trigger('click')
  await flushPromises()

  expect(askData).toHaveBeenCalledWith('一共有多少笔支付订单？', [])
  const text = wrapper.text()
  expect(text).toContain('共有 1956 笔支付订单。')
  expect(wrapper.find('th').text()).toBe('c')
  expect(wrapper.find('tbody td').text()).toBe('1956')
})

it('结果被行数上限截断时必须显式告警', async () => {
  vi.mocked(askData).mockResolvedValue(
    result({ truncated: true, row_count: 200 }) as never,
  )
  const wrapper = await mounted()
  await wrapper.find('textarea').setValue('列出所有订单')
  await wrapper.find('button.primary').trigger('click')
  await flushPromises()

  expect(wrapper.find('.warn').text()).toContain('截断')
})

it('SQL 默认折叠，展开技术明细才看得到', async () => {
  const wrapper = await mounted()
  await wrapper.find('textarea').setValue('一共有多少笔支付订单？')
  await wrapper.find('button.primary').trigger('click')
  await flushPromises()

  expect(wrapper.find('pre.sql').exists()).toBe(false)
  await wrapper.find('.tech button').trigger('click')
  expect(wrapper.find('pre.sql').text()).toContain('SELECT count(*)')
})

it('追问带上上一轮问答，口径不必让模型重新猜', async () => {
  const wrapper = await mounted()
  await wrapper.find('textarea').setValue('第一问')
  await wrapper.find('button.primary').trigger('click')
  await flushPromises()

  await wrapper.find('textarea').setValue('第二问')
  await wrapper.find('button.primary').trigger('click')
  await flushPromises()

  expect(askData).toHaveBeenLastCalledWith('第二问', [
    { role: 'user', content: '第一问' },
    { role: 'assistant', content: '共有 1956 笔支付订单。' },
  ])
})

it('守卫拒绝或执行失败要留在界面上，且不产生一条空回答', async () => {
  vi.mocked(askData).mockRejectedValue(new Error('查询被只读守卫拒绝: 表 users 不在只读白名单内'))
  const wrapper = await mounted()
  await wrapper.find('textarea').setValue('查一下用户密码')
  await wrapper.find('button.primary').trigger('click')
  await flushPromises()

  expect(wrapper.find('.hint.danger').text()).toContain('不在只读白名单内')
  expect(wrapper.findAll('article.turn')).toHaveLength(0)
})
