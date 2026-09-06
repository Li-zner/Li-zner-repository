// 主链路 E2E：登录 → 发送消息 → 断言流式渲染出现回答（需要后端与测试账号）
import { expect, test } from '@playwright/test'

const TEST_USER = process.env.E2E_USER ?? 'admin'
const TEST_PASS = process.env.E2E_PASS ?? ''

test('登录后完成一轮流式对话', async ({ page }) => {
  test.skip(!TEST_PASS, '未配置 E2E_PASS，跳过（CI 中由环境变量注入）')
  await page.goto('/login')
  await page.getByPlaceholder('用户名').fill(TEST_USER)
  await page.getByPlaceholder('密码').fill(TEST_PASS)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/chat/)
  await page.getByPlaceholder(/输入你的旅行问题/).fill('你好，请做个自我介绍')
  await page.getByRole('button', { name: '发送' }).click()
  await expect(page.locator('.message.assistant .answer-box').last()).toContainText(/./, { timeout: 30_000 })
})
