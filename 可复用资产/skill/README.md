# 可复用 Skill 资产库

> 从 agent_gateway wiki + docs 沉淀的可复用技能。每个技能一个目录，`SKILL.md` 为
> 标准技能文件（YAML frontmatter 含 name/description），可放入任意 AI 编码工具的
> skills 目录（Claude Code / Cursor / OpenCode 等）。

## 技能清单（按作用命名）

| # | 技能 | 何时用 |
|---|------|--------|
| 001 | `mcp-server开发五步法` | 任务要求「把 X 能力封装成 MCP / 新建 MCP Server」 |
| 002 | `生产机制自查` | 上线前 / 生产化 / 查漏补缺 / 机制评审 |
| 003 | `避坑检查清单` | 排障 / 环境问题 / Windows+Docker / 中文编码 / 部署 |
| 004 | `密钥与配置治理` | 涉及密钥 / 配置 / .env / 安全目录 / 轮换 |
| 005 | `面试手写八段训练` | 面试准备 / 手写代码训练 |
| 006 | `知识库笔记写作` | 记录 / 总结 / 整理到知识库 / 写 wiki 笔记 |

## 使用方式

- **直接使用**：把 `skill/<技能名>/` 目录（或其中的 SKILL.md）复制到你的工具 skills 目录
  （如 `.claude/skills/`、`.cursor/skills/`、`.opencode/skills/`）
- **按需调用**：任务命中「何时用」时，先读 SKILL.md 再执行
- **持续演进**：新踩坑 → 追加到 `避坑检查清单`；新机制 → 追加到 `生产机制自查`

## 与 MCP sever/ 的关系

- skill = 教 AI 怎么做的**方法**（过程性知识）
- MCP sever = 可直接调用的**能力**（工具化封装）
- 例：写面试八段训练时，对应能力已封装成 MCP（分布式锁/限流 → 001，语义缓存 → 003，JWT → 004），
  训练时可现场调用演示。
- **组合实例**：`../AI应用验收工具/` 把「验收流程 skill + 5 个 MCP 能力」合并成一个工具
  （SKILL.md + verify_all.py 一键执行），是 MCP+skill 组合的落地样板。
