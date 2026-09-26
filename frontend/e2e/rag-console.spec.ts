/** RAG 管理端 E2E：管理员登录后加载四个运营视图。 */
import { expect, test } from '@playwright/test'

const TEST_USER = process.env.E2E_USER ?? 'admin'
const TEST_PASS = process.env.E2E_PASS ?? ''
const CONSOLE_BASE_URL = (
  process.env.E2E_CONSOLE_BASE_URL ?? 'http://127.0.0.1:13150'
).replace(/\/$/, '')

test.use({
  baseURL: CONSOLE_BASE_URL,
})

function console_url(route: string): string {
  return `${CONSOLE_BASE_URL}${route}`
}

test('管理员可打开 RAG 运营控制台', async ({ page }) => {
  test.skip(!TEST_PASS, '未配置 E2E_PASS，跳过')
  await page.goto(console_url('/#/admin/rag'))
  await expect(page).toHaveURL(/\/login/)
  await page.getByPlaceholder('管理员账号').fill(TEST_USER)
  await page.getByPlaceholder('密码').fill(TEST_PASS)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/admin\/rag$/)
  await expect(page.getByRole('heading', { name: 'RAG 运营控制台' })).toBeVisible()
  await expect.poll(
    () => page.evaluate(() => localStorage.getItem('rag_console_authenticated')),
  ).toBe('1')
  const storage = await page.evaluate(() => ({
    access: localStorage.getItem('gw_access_token'),
    refresh: localStorage.getItem('gw_refresh_token'),
    session: localStorage.getItem('rag_console_authenticated'),
  }))
  expect(storage.access).toBeNull()
  expect(storage.refresh).toBeNull()
  expect(storage.session).toBe('1')

  for (const label of ['概览', '请求', '事件', '动作']) {
    await page.getByRole('button', { name: new RegExp(`^${label}`) }).click()
  }
  await expect(page.getByText('Incident', { exact: true })).toHaveCount(0)
  await expect(page.getByText('当前账号没有 RAG 管理权限')).toHaveCount(0)

  await page.goto(console_url('/#/admin/rag/chat'))
  await expect(page.getByRole('heading', { name: 'RAG 运维助手' })).toBeVisible()
})

test('登录后回到原本要访问的运维会话', async ({ page }) => {
  test.skip(!TEST_PASS, '未配置 E2E_PASS，跳过')
  await page.goto(console_url('/#/admin/rag/chat'))
  await expect(page).toHaveURL(/\/login/)
  await page.getByPlaceholder('管理员账号').fill(TEST_USER)
  await page.getByPlaceholder('密码').fill(TEST_PASS)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/admin\/rag\/chat/)
  await expect(page.getByRole('heading', { name: 'RAG 运维助手' })).toBeVisible()
  await expect(page.getByText('RAG 运维', { exact: true })).toBeVisible()
  await expect(page.getByText('RAG Ops', { exact: true })).toHaveCount(0)
})

test('点击请求只预览，不自动调用运维助手', async ({ page }) => {
  test.skip(!TEST_PASS, '未配置 E2E_PASS，跳过')
  await page.route('**/api/admin/rag/requests?limit=**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{
          request_uid: 'req-e2e',
          created_at: '2026-09-14T10:00:00',
          completed_at: '2026-09-14T10:00:01',
          persona: 'civil_code',
          route: 'simple_fast_path',
          status: 'succeeded',
          error_code: '',
          selected_model: 'deepseek-chat',
          provider: 'deepseek',
          config_version: 'e2e',
          first_token_ms: 20,
          total_ms: 100,
          input_tokens: 10,
          output_tokens: 20,
          instance_id: 'gateway',
        }],
      }),
    })
  })
  await page.route(/\/api\/admin\/rag\/requests\/[^/?]+$/, async (route) => {
    const requestUid = route.request().url().split('/').pop() ?? 'req-e2e'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        request_uid: requestUid,
        created_at: '2026-09-14T10:00:00',
        completed_at: '2026-09-14T10:00:01',
        persona: 'civil_code',
        route: 'simple_fast_path',
        status: 'succeeded',
        error_code: '',
        selected_model: 'deepseek-chat',
        provider: 'deepseek',
        config_version: 'e2e',
        first_token_ms: 20,
        total_ms: 100,
        input_tokens: 10,
        output_tokens: 20,
        instance_id: 'gateway',
        spans: [{
          span_uid: 'span-e2e',
          stage: 'retrieve',
          round_no: 1,
          status: 'ok',
          started_at: null,
          ended_at: null,
          latency_ms: 80,
          result_count: 1,
          error_code: '',
          attributes: {},
        }],
        content: {
          query: '定金不退合法吗',
          answer: '根据民法典第五百八十七条，收受定金的一方不履行债务的，应当双倍返还定金。',
          answer_len: 31,
          cited_numbers: [587],
          ungrounded_citations: [],
          retrieval_returned_count: 1,
          retrieval_method: 'trgm+vector+rrf',
          retrieval_latency_ms: 80,
          feedback: null,
          retrieval_runs: [{
            query: '定金不退合法吗',
            method: 'trgm+vector+rrf',
            trgm_hits: 1,
            vec_hits: 1,
            max_trgm_sim: 0.8,
            max_vec_sim: 0.7,
            returned_count: 1,
            rerank_used: true,
            latency_ms: 80,
            chunk_keys: ['civil-587'],
            created_at: '2026-09-14T10:00:00',
          }],
          chunks: [{
            chunk_key: 'civil-587',
            source: 'civil_code',
            heading: '第五百八十七条',
            content: '债务人履行债务的，定金应当抵作价款或者收回。',
          }],
        },
      }),
    })
  })
  await page.goto(console_url('/#/admin/rag/chat'))
  await page.getByPlaceholder('管理员账号').fill(TEST_USER)
  await page.getByPlaceholder('密码').fill(TEST_PASS)
  await page.getByRole('button', { name: '登录', exact: true }).click()

  const request = page.locator('.request-item').first()
  await expect(request).toBeVisible()
  const userMessagesBefore = await page.locator('.ops-message.user').count()
  await request.click()

  await expect(page.locator('.request-context')).toBeVisible()
  await expect(page.getByRole('button', { name: '分析此请求' })).toBeVisible()
  await expect(page.getByText('用户问题', { exact: true })).toBeVisible()
  await expect(page.getByText('定金不退合法吗', { exact: true })).toBeVisible()
  await expect(page.getByText(/最终回答/)).toBeVisible()
  await expect(page.getByText(/召回内容/)).toBeVisible()
  await expect(page.getByText(/执行阶段/)).toBeVisible()
  await page.getByText(/召回内容/).click()
  await expect(page.getByText('第五百八十七条', { exact: true })).toBeVisible()
  await expect(page.locator('.ops-message.user')).toHaveCount(userMessagesBefore)
})

test('长回答区域可以独立滚动', async ({ page }) => {
  test.skip(!TEST_PASS, '未配置 E2E_PASS，跳过')
  const longAnswer = Array.from(
    { length: 220 },
    (_, index) => `第 ${index + 1} 行：长回答滚动验证。`,
  ).join('\n\n')
  await page.route('**/api/admin/rag/ask/stream', async (route) => {
    // 前端走 streamRagOpsAsk（POST-SSE），必须按 SSE 协议喂事件；
    // 原先拦的是已下线的非流式 /ask，mock 根本命不中（2026-09-19 审查 P2-2）
    const event = (payload: unknown) => `data: ${JSON.stringify(payload)}\n\n`
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: [
        event({ type: 'answer_chunk', content: longAnswer }),
        event({ type: 'answer_complete', content: longAnswer }),
        'data: [DONE]\n\n',
      ].join(''),
    })
  })
  await page.goto(console_url('/#/admin/rag/chat'))
  await page.getByPlaceholder('管理员账号').fill(TEST_USER)
  await page.getByPlaceholder('密码').fill(TEST_PASS)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.transcript')).toBeVisible()

  await page.locator('.composer textarea').fill('请输出长回答用于滚动验证')
  await page.getByRole('button', { name: '发送' }).click()
  const transcript = page.locator('.transcript')
  await expect(transcript.locator('.message-body.markdown').last()).toContainText('第 220 行')
  const metrics = await transcript.evaluate((element) => {
    element.scrollTop = 0
    const top = element.scrollTop
    element.scrollTop = element.scrollHeight
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      top,
      bottom: element.scrollTop,
    }
  })
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight)
  expect(metrics.top).toBe(0)
  expect(metrics.bottom).toBeGreaterThan(0)
})
