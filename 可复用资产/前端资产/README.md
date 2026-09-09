# 前端资产

> **用途**：前端开发的可复用资产入口——开源项目选型清单、官方前端技能原件、前端 MCP 接入文档。
> **何时用**：写/改/测前端，或做前端技术选型时进入本目录；先读本文件，再按路由取用。

## 结论先行：前端可复用吗？

可复用，且是前端比后端复用度更高的领域，但要分三层取用：
**整应用**（AI 对话台直接改造 open-webui 等）、**组件/模板**（shadcn/ui 复制进项目、tabler 模板）、
**原语**（Tailwind/Radix/react-hook-form 自由组装）。业务页面本身不复用。明细见 `开源项目清单.md`。

## 目录路由

| 位置 | 是什么 | 何时用 |
|---|---|---|
| `开源项目清单.md` | 三层复用清单（星数实测 + 取用建议） | 选型、给项目换装、agent 选 UI 方案时 |
| `skills/` | Anthropic 官方前端技能原件 4 件（含 LICENSE 与脚本） | 见 `skills/README.md` 索引 |
| `mcp/前端MCP清单.md` | 6 个前端 MCP 的定位、场景、接入 JSON | 需要浏览器实测/组件生成/设计稿转码时 |
| `自产代码/` | 从 agent_gateway 前端 v2 萃取的可复用原语（SSE 分帧器/401 单飞刷新/消毒链等 7 件，零 UI） | 写流式接口/认证客户端/PWA/地图时直接 import 取用 |

## 技能分工速记（写一个"精美前端"的完整链路）

1. **观感**：`skills/frontend-design`——审美方向、排版、去模板感；
2. **搭建**：`skills/web-artifacts-builder`——React + Tailwind + shadcn/ui 工程化搭建；
3. **取件**：shadcn MCP 直查组件注册表（可选 Magic MCP 取现成精美组件）；
4. **皮肤**：`skills/theme-factory`——10 套预置主题一键换肤；
5. **验证**：Playwright MCP + `skills/webapp-testing`——agent 自己点页面、看日志、截图回归。

## 维护约定

- 本目录上游拷贝件升级后需重做两件机械处理：CRLF 转 LF、剥离 emoji（留痕见 `skills/README.md`）；
- 选型数据须实测（GitHub API），禁止凭印象更新；
- 新增 MCP 接入文档时必须含：仓库地址、适用场景、接入 JSON、密钥走 .env 的提醒。

## 图谱入口

- 主题图谱：[[图谱/MOC-前端|MOC-前端]]（本目录选型 / 技能 / MCP 的归拢）
- 相邻主题：[[图谱/MOC-MCP生态|MOC-MCP 生态]]（前端 MCP 与自研 MCP 的共同纪律）
- 全库总览：[[图谱/MOC-总览|MOC-总览]]
