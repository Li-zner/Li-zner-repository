# 模型归因纠错映射（MCP Server 005）

## 作用

模型有**顽固错误归因**时（如 DeepSeek 总把「七天无理由退货」归到民法典），改 prompt
三版无效——用**架构手段绕过**：建一张「触发词 → 权威答案/引导」映射表，在调用 LLM
**之前**先查表，命中直接返回引导，不花钱、不赌模型。实战结果：CC019 用例从 2 分提到
7 分（踩坑清单 #27）。

## 能力

| 工具 | 作用 |
|------|------|
| `lookup_redirect` | 文本命中映射则返回权威答案（exact 优先 → contains 按注册顺序） |
| `add_mapping` | 新增/更新映射（同 trigger+mode 覆盖，幂等） |
| `list_mappings` | 列出全部映射（排查/审计） |
| `remove_mapping` | 删除映射 |

## 为什么这样设计

- **exact 优先于 contains**：完全一致的输入给完整答案；包含关系的给引导话术。
- **映射表不是 prompt**：prompt 靠模型自觉，映射表是确定性拦截——这就是「模型解决
  不了的问题，用架构手段绕过」的面试故事。
- **零依赖 + JSON 持久化**：数据文件原子写（临时文件 + replace，防写一半损坏）；
  重启不丢；可人工编辑。
- **模式化应用**：不止法律——任何「模型顽固错、规则可判定」的场景都适用
  （政策引导、口径统一、敏感话题转向）。

## 运行与验证

```bash
pip install mcp

python correction_map_server.py            # 拉起 Server（stdio）
python correction_map_server.py --self-check   # 纯逻辑自检（隔离临时文件）
python test_client.py                      # 完整验证（自动清理测试条目）
```

## 接入姿势

```
用户输入 → lookup_redirect(text)
  ├─ hit  → 直接返回 answer（不调 LLM）
  └─ miss → 正常走意图路由/LLM
```

## 维护纪律

- 触发词要短而稳（用户口语变体多时用 contains，避免把整句当触发词）。
- 命中率低/误命中多 → 调整 trigger 或模式；定期用 `list_mappings` 审计。

## 库内关联

- 踩坑来源：[[skill/避坑检查清单/SKILL|避坑检查清单]] #27（模型级问题硬改 prompt 无效，架构手段绕过）
- 链路位置：[[AI应用资产/agent全链路|agent 全链路]]护栏与纠错环节
- 验收：[[AI应用验收工具/README|AI 应用验收工具·阶段 5]]（contains 命中 / 幂等更新 / 清理不残留）
- 图谱：[[图谱/MOC-RAG与知识工程|MOC-RAG 与知识工程]]、[[图谱/MOC-MCP生态|MOC-MCP 生态]]
