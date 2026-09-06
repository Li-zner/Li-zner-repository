"""生成企业版简历 docx（黎忠南 - AI 应用开发 / Python 后端）"""
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

OUT = "黎忠南简历-AI后端开发.docx"

doc = Document()

# ---------- 全局样式 ----------
style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(10.5)
style._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

# 页边距
for sec in doc.sections:
    sec.top_margin = Cm(1.8)
    sec.bottom_margin = Cm(1.8)
    sec.left_margin = Cm(2.2)
    sec.right_margin = Cm(2.2)


def set_cn(run, name="微软雅黑"):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def add_name(text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(18)
    set_cn(r)
    p.paragraph_format.space_after = Pt(2)


def add_center(text, size=10.5, bold=False, color=None):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(text)
    r.bold = bold
    r.font.size = Pt(size)
    if color:
        r.font.color.rgb = RGBColor(*color)
    set_cn(r)
    p.paragraph_format.space_after = Pt(2)
    return p


def add_heading(text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(13)
    r.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
    set_cn(r)
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    # 下划线边框
    pPr = p._p.get_or_add_pPr()
    from docx.oxml import OxmlElement
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:color"), "1F4E79")
    pBdr.append(bottom)
    pPr.append(pBdr)
    return p


def add_line(text, bold_prefix=None, size=10.5):
    p = doc.add_paragraph()
    if bold_prefix:
        r0 = p.add_run(bold_prefix)
        r0.bold = True
        set_cn(r0)
        r0.font.size = Pt(size)
    r = p.add_run(text)
    r.font.size = Pt(size)
    set_cn(r)
    p.paragraph_format.space_after = Pt(2)
    return p


def add_bullet(text, bold_prefix=None, size=10.5):
    p = doc.add_paragraph(style="List Bullet")
    if bold_prefix:
        r0 = p.add_run(bold_prefix)
        r0.bold = True
        set_cn(r0)
        r0.font.size = Pt(size)
    r = p.add_run(text)
    r.font.size = Pt(size)
    set_cn(r)
    p.paragraph_format.space_after = Pt(3)
    return p


# ================= 头部 =================
add_name("黎忠南")
add_center("男  |  电话 13357308241  |  微信 CXKSBLi  |  广州、深圳", 10.5)
add_center("求职意向：AI 应用开发 / Python 后端", 10.5, bold=True)

# ================= 个人简介 =================
add_heading("个人简介")
p = doc.add_paragraph()
r = p.add_run(
    "独立设计并交付企业级 Multi-Agent 网关（FastAPI + PostgreSQL + Redis + Docker），"
    "一套引擎承载旅游规划、民法典咨询、求职助手三个业务场景，已上线公网稳定运行。"
    "熟悉 AI 应用层全链路：RAG 检索、Agent 编排、流式输出、语义缓存；"
    "同时具备生产级工程能力：并发安全、可观测性、CI/CD、密钥治理与线上排障。"
    "具备从需求拆解、架构落地到生产运维的完整闭环能力。"
    "在大模型侧具备从量化部署到微调训练的实操认知：本地完成过量化模型部署，"
    "在阿里云百炼平台用自有数据集完成过微调训练，理解微调与 RAG 的适用边界。"
)
r.font.size = Pt(10.5)
set_cn(r)
p.paragraph_format.space_after = Pt(3)

# ================= 核心优势 =================
add_heading("核心优势")
add_bullet(
    "引擎与业务解耦的可插拔架构：人设、工具、模型策略全部外置为配置包，三个业务场景仅通过新增配置接入、核心代码零改动，具备清晰的产品化意识。",
    bold_prefix="架构与产品化：",
)
add_bullet(
    "按生产标准交付工程体系：Git 密钥治理（清除 40+ 文件硬编码密钥）、CI/CD 滚动发布与失败回滚、数据库版本化迁移、SLO 告警、备份与恢复演练。",
    bold_prefix="工程规范：",
)
add_bullet(
    "分布式限流、熔断降级、三级语义缓存、冷热分层记忆；800 并发近零错误稳定服务，极限 1600 并发可用性 99.28%。",
    bold_prefix="性能与稳定性：",
)
add_bullet(
    "OpenTelemetry 全链路追踪 + Prometheus/Grafana/Loki 日志关联，线上问题分钟级定位；曾独立定位并修复配置漂移导致的线上全链路故障。",
    bold_prefix="可观测与排障：",
)
add_bullet(
    "系统学习大模型微调、量化、部署：本地实操量化模型部署（Ollama/GGUF），在阿里云百炼平台用自有数据集完成微调训练，能清晰阐述微调与 RAG 的适用边界与组合策略。",
    bold_prefix="模型认知：",
)

# ================= 项目经历 =================
add_heading("项目经历")

p = doc.add_paragraph()
r = p.add_run("AI Agent 网关")
r.bold = True
r.font.size = Pt(11)
set_cn(r)
r2 = p.add_run("   |   架构设计 / 核心开发   |   2026.06 - 至今")
r2.font.size = Pt(10.5)
set_cn(r2)
p.paragraph_format.space_after = Pt(2)

add_line("GitHub：github.com/Li-zner ｜ 源码仓库：github.com/Li-zner/Li-zner-repository", size=9.5)
add_line("公网演示：https://the-world-agent.cloud", size=9.5)

p = doc.add_paragraph()
r = p.add_run(
    "面向企业知识服务的 AI 网关，带完整支付体系，已上线公网稳定运行，日常 800 并发稳定服务，极限压测 1600 并发可用性 99.28%。"
)
r.font.size = Pt(10.5)
set_cn(r)
p.paragraph_format.space_before = Pt(2)
p.paragraph_format.space_after = Pt(3)

add_bullet(
    "独立完成从需求拆解、架构设计到部署上线的全流程。Agent 内核只负责调度决策与流式输出，业务能力通过配置包接入，支撑多场景扩展。",
    bold_prefix="架构设计：",
)
add_bullet(
    "关键词与向量双通道召回 + 重排精排，回答附法条出处并支持跨法域正确引导；60 条专业用例评测综合得分 0.925，回答忠诚度 99.92%。",
    bold_prefix="RAG 知识问答：",
)
add_bullet(
    "Redis 分布式锁与乐观锁保障资金一致性（并发扣费无超扣、无丢流水），幂等机制防重复回调；WAL 交易流水实现不可篡改对账。",
    bold_prefix="支付与并发安全：",
)
add_bullet(
    "三级语义缓存将命中查询成本降至纯调用的约六成；冷热分层记忆 + 断连自动保存；800 并发平均响应 1.1s。",
    bold_prefix="性能优化：",
)
add_bullet(
    "Docker Compose 编排 10+ 服务、Nginx 负载均衡 + 按 QPS 自动伸缩（2-4 实例）；Cloudflare Tunnel 公网暴露、源站零端口暴露；慢查询监控与异常聚合告警。",
    bold_prefix="可观测与运维：",
)

# ================= 技术栈 =================
add_heading("技术栈")
add_bullet("Python / FastAPI / asyncio / PostgreSQL（pgvector、pg_trgm）/ Redis / Docker Compose / Nginx / SSE 流式。", bold_prefix="熟练：")
add_bullet("Prometheus / Grafana / Tempo / Loki / Alembic / JWT 认证与权限体系 / CI-CD 流程 / 分布式锁与乐观锁。", bold_prefix="熟悉：")
add_bullet("LangChain / LangGraph 设计理念、Dify 平台、Kubernetes 基础概念。", bold_prefix="了解：")
add_bullet("大模型微调与部署：阿里云百炼/PAI 微调流程（数据集准备→训练→评估）、量化部署（Ollama/GGUF，实测 Qwen2.5B/3.5B、DeepSeek-7B）、RAG 与微调适用边界。", bold_prefix="模型认知：")

# ================= 大模型认知与实操 =================
add_heading("大模型认知与实操")
add_bullet(
    "在阿里云百炼平台使用自有数据集完成过模型微调训练（数据集构建→训练任务→效果评估全流程，部署环节未实操），理解 LoRA 等参数高效微调思路，掌握过拟合、学习率、loss 收敛等训练指标的基础概念。",
    bold_prefix="微调实操：",
)
add_bullet(
    "本地部署量化模型：Ollama + GGUF 量化格式，实测运行 Qwen 2.5B / 3.5B 与 DeepSeek 7B，理解量化精度（如 Q4_K_M）对显存占用、推理速度与生成质量的取舍。",
    bold_prefix="量化部署：",
)
add_bullet(
    "能结合业务场景决策：知识时效性与私有数据优先 RAG，领域风格与特定能力优先微调，复杂场景两者组合（如知识库检索 + 领域术语风格输出）。",
    bold_prefix="技术边界：",
)
add_bullet(
    "备考阿里云大模型 ACP 认证，系统学习了大模型原理、提示工程、微调、RAG、Agent 等核心知识体系。",
    bold_prefix="持续学习：",
)

doc.save(OUT)
print(f"已生成: {OUT}")
