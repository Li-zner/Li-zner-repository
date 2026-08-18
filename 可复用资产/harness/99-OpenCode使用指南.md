# OpenCode 使用指南（结合本 harness）

> 目标：用 OpenCode（终端 AI 代理，已配置 DeepSeek）配合本 harness 启动新项目。
> 前提：`opencode.exe` 已装好，环境变量已配（DEEPSEEK_API_KEY / OPENCODE_MODELS_PATH /
> OPENCODE_DISABLE_MODELS_FETCH）。

## 一、新项目启动三步

1. **人类**：复制本 harness 到新项目旁，填写 `04-待人类填写/` 四个文件。
2. **人类**：把 harness 路径 + 下面这句「开场指令」交给 OpenCode。
3. **AI**：OpenCode 会自动读取 `AGENTS.md`，并按 `05-执行流程/01-启动流程.md` 执行。

## 二、开场指令（直接粘贴给 OpenCode）

```
你正在使用一套新项目启动套件（harness），路径：<harness 绝对路径>。
1) 先读 harness/AGENTS.md 与 harness/00-主人意图与使用方法.md；
2) 读 harness/04-待人类填写/ 全部内容（项目事实，唯一准绳）；
3) 按 harness/05-执行流程/01-启动流程.md 从 Step 0 开始执行；
4) 遇到 [待定] 或协议矛盾，停下来问我，禁止猜测；
5) 每个阶段完成按 harness/05-执行流程/04-交付报告模板.md 输出报告。
```

## 三、命令行示例

```bash
# 在项目目录启动交互式会话（推荐）
cd <项目目录>
opencode

# 单次执行（把开场指令写进 prompt.txt）
opencode run "$(cat prompt.txt)"

# 指定模型（本机已配 DeepSeek）
opencode run -m deepseek/deepseek-v4-flash "用一句话介绍你自己"
```

## 四、常用动作

| 目标 | 命令 |
|---|---|
| 查看可用模型 | `opencode models` |
| 交互式开发 | `opencode` |
| 单次跑指令 | `opencode run "指令"` |
| 查看帮助 | `opencode --help` |

## 五、与 GitHub Copilot 的分工建议

- **Copilot（本会话）**：日常开发、运维、本项目修复（已熟系本项目）。
- **OpenCode**：新项目按 harness 从零搭建（长链自主执行），以及需要终端代理并行做的批处理。
- 两者共用同一套 `01-规则/`，产出物格式一致，方便互相交接。
