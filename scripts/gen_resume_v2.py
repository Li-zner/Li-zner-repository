"""生成简历 v2：黎忠南简历-AI应用工程师-v2.docx（一页，三故事结构）

用法：python scripts/gen_resume_v2.py
产出：黎忠南简历-AI应用工程师-v2.docx（项目根目录）
"""
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

OUT = "黎忠南简历-AI应用工程师-v2.docx"

doc = Document()

# 全局字体：微软雅黑（中文）
style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(10.5)
style._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")


def para(text, size=10.5, bold=False, color=None, align=None, space_after=2):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)
    if align:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    return p


def heading(text):
    p = para(text, size=11, bold=True, color=(0x1F, 0x4E, 0x79), space_after=3)
    p.paragraph_format.space_before = Pt(6)
    return p


def bullet(text, size=10.5):
    p = doc.add_paragraph(style="List Bullet")
    run = p.add_run(text)
    run.font.size = Pt(size)
    p.paragraph_format.space_after = Pt(1)
    return p


# ===== 头部 =====
para("黎忠南", size=16, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
para("求职意向：AI 应用工程师（后端向）｜广州 / 深圳｜电话 13357308241｜微信 CXKSBLi",
     size=10, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=4)

para("独立交付生产级 Multi-Agent AI 网关（已上线公网，日常 800 并发稳定运行）："
     "从架构设计、Agent 引擎、RAG 检索到并发压测、CI/CD 上线，全链路闭环。"
     "毕业以来全职投入该项目，具备 AI 应用从模型选型到生产运维的完整交付能力。",
     size=10.5, space_after=4)

# ===== 核心优势 =====
heading("核心优势")
bullet("AI 应用全链路落地：自研 Agent 引擎（意图路由 / 多 Agent 圆桌协作 / 流式输出），"
       "RAG 双通道检索 + 重排精排 + 跨法域引导；112 条自动化用例，民法典专项 60 条综合 0.925 分、"
       "回答忠诚度 99.92%；极限压测 1600 并发可用性 99.28%。")
bullet("生产级工程能力：FastAPI + PostgreSQL（pgvector/pg_trgm）+ Redis 全栈；JWT 认证与令牌轮换、"
       "Redis 分布式限流、熔断降级、三级语义缓存、冷热分层记忆；支付体系（分布式锁 + 乐观锁 + WAL 流水 + 对账）；"
       "OpenTelemetry + Prometheus/Grafana/Loki 全链路可观测；CI/CD 滚动发布与回滚；WAF 与密钥治理。")
bullet("缺陷发现与修复：封装 RAG 过程中定位知识库构建脚本正则缺陷（千位以上条号漏导），"
       "修复后法条覆盖从 861 条补全至 1260 条；曾定位并修复配置漂移导致的线上全链路故障。")
bullet("模型认知 + AI 辅助开发：本地量化部署（Ollama/GGUF，实测 Qwen 2.5B/3.5B、DeepSeek 7B）；"
       "阿里云百炼 LoRA 微调全流程（数据集→训练→评估）；理解微调与 RAG 适用边界；"
       "日常以 AI 辅助编码（vibe coding）+ 人工审查验证闭环工作。")

# ===== 项目经历 =====
heading("项目经历")
para("AI Agent 网关｜独立设计 / 开发 / 运维｜2026.06 - 至今", bold=True, space_after=1)
para("公网演示：https://the-world-agent.cloud｜代码仓库：github.com/Li-zner", size=9.5, space_after=2)
bullet("架构：一核双用可插拔——引擎与业务解耦，人设/工具/模型策略外置配置包，"
       "一套引擎承载旅游规划、民法典咨询、求职助手三个场景，新增场景零核心改动。")
bullet("Agent 与 RAG：关键词+LLM 两级意图路由（简单/复杂/全推荐分路）；多 Agent 三阶段圆桌并行协作；"
       "关键词+向量双通道召回 + Rerank 精排，回答附法条出处；法律映射表以架构手段解决模型顽固错误。")
bullet("性能与稳定性：三级语义缓存（进程 LRU→精确哈希→语义相似度）将重复查询成本降至约六成；"
       "冷热分层记忆 + 异步落盘限流；Redis 四维限流 + 熔断三态状态机；800 并发平均响应 1.1s。")
bullet("商业化与并发安全：钱包/订单/交易流水（WAL）/对账完整支付体系；分布式锁 + 乐观锁双保险，"
       "并发扣费无超扣、无丢流水，幂等机制防重复回调。")
bullet("工程化：CI/CD 五阶段滚动发布（失败自动回滚）；Alembic 数据库版本化迁移；SLO 告警体系；"
       "备份恢复演练；弹性伸缩（2-4 实例）；Nginx WAF；Cloudflare Tunnel 公网暴露、源站零端口；"
       "密钥治理（清除 41 个文件硬编码密钥）。")

# ===== 技术栈 =====
heading("技术栈")
bullet("熟练：Python / FastAPI / asyncio、PostgreSQL（pgvector、pg_trgm）、Redis、Docker Compose、Nginx、SSE 流式")
bullet("熟悉：Prometheus / Grafana / Tempo / Loki、Alembic、JWT 认证与权限、CI/CD、分布式锁与乐观锁、MCP（工具协议化封装）")
bullet("了解：LangChain / LangGraph 设计理念、Dify、Kubernetes 基础概念")
bullet("开发方式：AI 辅助编码（vibe coding）+ 代码审查 / 测试验证闭环")

# ===== 模型认知 =====
heading("模型认知")
bullet("微调：阿里云百炼平台自有数据集 LoRA 微调全流程，理解过拟合、学习率、loss 收敛等训练指标。")
bullet("量化部署：Ollama + GGUF 本地部署 Qwen 2.5B / 3.5B、DeepSeek 7B，理解量化精度（Q4_K_M）与显存/速度/质量取舍。")
bullet("技术边界：知识时效性与私有数据优先 RAG；领域风格与特定能力优先微调；复杂场景两者组合。")

doc.save(OUT)
print(f"已生成: {OUT}")
