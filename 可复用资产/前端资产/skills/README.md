# skills——前端相关 Agent 技能（本地拷贝）

> **用途**：从 Anthropic 官方技能库下载的前端相关技能原件，供本库 agent 直接加载使用。
> **来源**：https://github.com/anthropics/skills （2026-09-05 克隆 main 分支拷贝）。
> **何时用**：见下表按任务类型选用；每个技能目录的 SKILL.md 即完整说明。

## 技能索引

| 目录 | 干什么 | 什么时候用 |
|---|---|---|
| `frontend-design/` | 独特、有意图的视觉设计指导：审美方向、排版、避免"模板感"默认样式 | 从零构建新 UI 或重塑现有 UI 的观感时 |
| `web-artifacts-builder/` | 用 React + Tailwind + shadcn/ui 搭建复杂多组件 Web 应用（含初始化/打包脚本） | 需要状态管理、路由、组件库的复杂前端，而非单文件页面 |
| `webapp-testing/` | 基于 Playwright 的本地 Web 应用测试工具包：功能验证、UI 调试、截图、浏览器日志 | 前端写完后的功能验证与回归（对应本库 vbcoding 流程的"提交测试"阶段） |
| `theme-factory/` | 主题化工具：10 套预置主题（配色/字体）可套用到任何产物，也可即时生成新主题 | 给页面/报告/落地页统一换肤时（含 themes/ 十套主题文件） |

## 使用方式

1. 单技能使用：把对应目录的 SKILL.md 内容作为指令上下文投喂给 agent（或按所用客户端的技能机制挂载）。
2. web-artifacts-builder 与 webapp-testing 带可执行脚本（scripts/），使用前先读其 SKILL.md 的前置要求。
3. theme-factory 的 themes/ 下每套主题是独立 md，可单独取用。

## 本地修改留痕（与上游的差异）

按本库规范（全库禁 emoji、LF 换行），对上游原件做了两处机械化处理：

1. 换行符 CRLF 统一转为 LF；
2. 剥离 emoji 共 45 处：`web-artifacts-builder/SKILL.md`（7）、`webapp-testing/SKILL.md`（2）、
   `scripts/bundle-artifact.sh`（10）、`scripts/init-artifact.sh`（26）。

除上述两类的机械清理外未改动任何语义内容；升级上游版本后需重做同样处理。

## 许可

各目录内 LICENSE.txt 为上游附带许可，随目录原样保留；再分发时连同许可文件一并携带。
