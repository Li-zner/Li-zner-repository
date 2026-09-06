# 前端 MCP 清单（编写/调试精美前端用）

> **用途**：与 agent 编码客户端（ZCode / Claude Code / Cursor 等）对接的前端类 MCP Server 清单与接入配置。
> **何时用**：需要浏览器实测、组件生成、设计稿转码时，按需选装；装前先读每节的"适用场景"。
> **约定**：所有 MCP 均为外部进程，密钥一律走 .env 或客户端密钥管理，禁止硬编码进配置文件入库。

## 1. Playwright MCP（浏览器自动化，首选）

- 仓库：https://github.com/microsoft/playwright-mcp （约 3.7 万星，微软官方）
- 干什么：让 agent 真实打开浏览器做可访问性驱动的页面操作、截图、读控制台日志——
  前端功能验证、UI 调试、E2E 回归的主力工具。
- 适用场景：写完页面让 agent 自己点一遍；抓 console 报错；对比设计稿截图。
- 接入（客户端 MCP 配置 JSON）：

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

- 备注：首次使用 `npx playwright install chromium` 装浏览器内核；无头模式默认开启。

## 2. Chrome DevTools MCP（性能与网络诊断）

- 仓库：https://github.com/ChromeDevTools/chrome-devtools-mcp （Google Chrome 团队出品）
- 干什么：暴露 Chrome DevTools 能力给 agent——性能追踪、网络请求审查、CPU/内存画像。
- 适用场景：页面"能跑但慢"、需要看哪一步渲染/接口拖了后腿；与 Playwright MCP 互补
  （Playwright 管"操作与功能"，DevTools 管"性能与网络"）。
- 接入：

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "command": "npx",
      "args": ["chrome-devtools-mcp@latest"]
    }
  }
}
```

## 3. shadcn MCP（组件注册表直达）

- 仓库：https://github.com/shadcn-ui/ui （shadcn/ui 自 v3 起内置 MCP，约 12.3 万星）
- 干什么：agent 直接检索/拉取 shadcn 组件注册表（demo、代码、安装方式），生成即用的是
  "放进你项目"的真实组件而非臆造的 API。
- 适用场景：技术栈为 React + Tailwind（web-artifacts-builder 技能的标配）时挂上它，
  组件准确率显著提升。
- 接入（客户端配置里指向 shadcn 自带 MCP）：

```json
{
  "mcpServers": {
    "shadcn": {
      "command": "npx",
      "args": ["shadcn@latest", "mcp"]
    }
  }
}
```

## 4. 21st MCP（原 Magic MCP，自然语言生成现代组件；商业服务，需 Key）

- 仓库：https://github.com/21st-dev/magic-mcp （约 5.8 千星；**已更名 21st MCP，旧 Magic Key 全部作废**）
- 干什么：检索/生成 21st.dev 组件库的现代 UI 组件（动效卡片、落地页区块等）。
- 费用现实：云端按额度计费——免费档有限额，重度使用需付费；下载代码 ≠ 随意调用。
- 适用场景：想要 21st.dev 风格动效组件且愿意付费时。
- 接入（远程服务模式）：

```json
{
  "mcpServers": {
    "21st": {
      "url": "https://21st.dev/api/mcp",
      "headers": { "x-api-key": "从 https://21st.dev/mcp 申请，走密钥管理，禁入库" }
    }
  }
}
```

- **零 Key 替代路径（本库推荐优先）**：
  1. shadcn MCP + `skills/frontend-design` + `skills/theme-factory`——精美观感不靠云（本库已配齐）；
  2. 开源动效组件库直接抄代码（copy-paste 模式，无 Key 无限额）：MagicUI（magicui.design，MIT）、
     Aceternity UI（免费组件区）、Origin UI、HyperUI、Cult UI；
  3. 设计稿/截图转代码：abi/screenshot-to-code（约 7.8 万星，开源可自托管，
     LLM 走你自己的网关——agent_gateway 本身就是，零新增充值）。

## 5. Figma MCP（设计稿转代码；需 Figma 付费 Dev 席位）

- 仓库：https://github.com/figma/figma-mcp （Figma 官方 Dev Mode MCP）
- 干什么：直接读 Figma 设计稿的结构/样式/变量生成前端代码，减少"照着截图猜"的还原误差。
- 费用现实：Figma Dev Mode MCP 需要付费席位，个人轻度使用不划算。
- 适用场景：有设计师产出 Figma 稿的团队协作流。
- 零费用替代：abi/screenshot-to-code（https://github.com/abi/screenshot-to-code ，约 7.8 万星，
  开源自托管；LLM 可接你自己的 agent_gateway 网关，零新增充值）；
  更轻量：直接截图 + `skills/frontend-design` 让 agent 照图还原。
- 接入：Figma 桌面端开启 Dev Mode MCP Server（本地 SSE 服务），客户端指向其地址；
  具体端口以 Figma 官方文档为准（本库不复制会过时的端口号）。

## 6. Browser Tools MCP（页面审计三件套）

- 仓库：https://github.com/AgentDeskAI/browser-tools-mcp （约 7 千星）
- 干什么：运行中页面的 console 日志、网络请求、DOM 元素三类数据喂给 agent，附带 Lighthouse 式审计。
- 适用场景：调试"我这里好好的、用户那里报错"类问题；做无障碍/SEO/性能体检。
- 接入：需三件套——MCP Server + 本地 Node 中间层 + Chrome 扩展，步骤较多，见其仓库 README；
  只需要"操作+截图"时优先用 Playwright MCP，不必上这套。

## 选装建议（按需，不贪多）

| 需求 | 装 |
|---|---|
| 功能验证 / 回归 / 截图 | Playwright MCP（首选，一个就够开工） |
| 性能 / 网络诊断 | Chrome DevTools MCP |
| React + Tailwind 组件准确率 | shadcn MCP |
| 快速取精美组件 | 零 Key：开源组件库抄码（见第 4 节替代路径）；付费：21st MCP |
| 有 Figma 设计稿 | Figma MCP（需付费 Dev 席位；免费替代：screenshot-to-code 自托管） |
| 深度页面审计 | Browser Tools MCP |

## 本库已有能力的分工

- `可复用资产/skills/`（本目录上级）：frontend-design 管"设计观感"，web-artifacts-builder 管"工程搭建"，
  webapp-testing 管"验证回归"，theme-factory 管"主题皮肤"——与上面 MCP 的分工：技能管方法论，MCP 管手和眼睛。
- `harness/05-执行流程/03-验收清单.md`：验收阶段建议挂 Playwright MCP 后执行 webapp-testing 技能。

## 库内关联

- 技能侧分工：[[前端资产/skills/README|官方前端技能索引]]（技能管方法论，MCP 管手和眼睛）
- 自研 MCP 时对照：[[AI应用资产/skills/mcp-builder/SKILL|mcp-builder 官方技能]]、[[skill/mcp-server开发五步法/SKILL|mcp-server 开发五步法]]
- 验收流程：[[harness/05-执行流程/03-验收清单|验收清单]]
- 图谱：[[图谱/MOC-前端|MOC-前端]]、[[图谱/MOC-MCP生态|MOC-MCP 生态]]
