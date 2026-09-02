"""
项目知识库种子脚本
将简历信息、项目架构、技术细节写入 knowledge_chunks (source='project')
供求职Agent通过 search_project_knowledge 工具检索回答HR提问。

用法: python scripts/seed_project_knowledge.py
"""
import os, sys, json, asyncio, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from app.core.db import init_pool, get_pool, close_pool

KNOWLEDGE_CHUNKS = [
    {
        "heading": "个人基本信息",
        "content": "姓名：黎忠南\n年龄：23岁（2003年生）\n教育背景：广西科技大学 本科，土木工程（土木工程建造与管理），2021-2025\n在校职务：校组织运营部副部长、班级心理委员\n求职意向：AI应用开发\n期望城市：广州、深圳\n联系方式：电话 13357308241 | 微信 CXKSBLi | QQ 1270345165@qq.com\n期望薪资：10-12K",
    },
    {
        "heading": "从土木工程转行AI的经历",
        "content": "黎忠南大学学的是土木工程，毕业后自学编程转型AI应用开发。从零开始学习Python、FastAPI、前端、Docker、PostgreSQL、Redis、Nginx、Prometheus、Grafana等全栈技术。用时3个月，从第一行代码到自研Multi-Agent网关上线公网。项目全程一人独立完成，展现极强的自主学习能力和全栈工程能力。技术选型和技术决策均有独立判断和理由。",
    },
    {
        "heading": "核心项目：自研Multi-Agent网关",
        "content": "自研Multi-Agent智能网关，基于FastAPI + DeepSeek API构建。采用对等协作架构，各Agent领域垂直独立（天气、酒店、路线、美食），语义路由自动分发任务。主链路全自研，Dify仅作为熔断降级备胎。已上线公网（https://the-world-agent.cloud），具备完整的服务治理、可观测性与安全合规体系。",
    },
    {
        "heading": "Agent架构设计详情",
        "content": "多Agent对等协作架构：四个领域Agent（天气、酒店、路线、美食）通过讨论板协议进行圆桌协作。每个Agent独立执行专业查询后发表见解，支持交叉审阅和补充。自研ReAct循环引擎实现思考-行动-观察闭环。可插拔设计：业务属性（人设、工具、模型）剥离为独立JSON配置包，切换场景不改核心代码。asyncio.gather并行调用多工具降低响应延迟。",
    },
    {
        "heading": "RAG多路召回与知识管理",
        "content": "多路召回架构：SQL关键词检索（pg_trgm）+ Embedding向量检索（pgvector，768维）并行执行。Cross-Encoder Rerank精排提升相关性。冷热分层记忆：Redis热缓存（L1）+ PostgreSQL长期归档（L2）+ 用户画像自动提取。pg_trgm语义缓存，高频问题毫秒级命中。RAGAS评测结果：旅游40条用例综合得分0.97（Faithfulness 0.94），民法60条用例综合得分0.925。",
    },
    {
        "heading": "服务治理体系",
        "content": "Redis分布式限流（IP+Token级别，角色分级）。熔断降级机制，自动切换备胎模型。SSE流式输出，元数据过滤不暴露推理过程。4实例+Nginx负载均衡，健康检查自动恢复。Opentelemetry手写埋点，Trace导出Tempo。Prometheus指标+Grafana可视化面板。Loki日志聚合，trace_id精准检索，问题2分钟定位。QQ邮箱告警通知。",
    },
    {
        "heading": "安全体系",
        "content": "JWT认证（python-jose）+ GitHub OAuth + 手机号短信验证码注册。DFA敏感词过滤器（确定性有穷自动机，O(n)时间复杂度，微秒级响应）。安全审计修复20+项风险，包括：默认JWT密钥加固、CORS白名单、文件上传类型校验、SQL注入防护、LLM提示词注入过滤、上传内容安全过滤等。",
    },
    {
        "heading": "性能压测数据",
        "content": "Locust压测结果：500并发用户时可用性100%，平均响应时间1.1秒，P95 2.9秒，吞吐量43 RPS。800并发用户时可用性99.28%，峰值吞吐量49.2 RPS。语义缓存命中率约20%，API调用成本约为纯调用的60%。数据库连接池配置：最小20连接，最大50连接每个实例。",
    },
    {
        "heading": "部署架构",
        "content": "Docker Compose编排10个服务：4个Gateway实例、Nginx负载均衡、PostgreSQL 16、Redis 7、Grafana、Prometheus、Tempo、Loki、Alertmanager。Nginx做四实例流量分发，健康检查自动剔除故障节点。Cloudflare Tunnel公网暴露，源站零端口暴露。可直接作为K8s部署基础单元。",
    },
    {
        "heading": "技术栈清单",
        "content": "AI与后端：Python 3.11、FastAPI、DeepSeek API、Multi-Agent编排、ReAct循环、RAG多路召回（SQL+Embedding）、Cross-Encoder Rerank、语义路由、可插拔架构、冷热分层记忆、RAGAS评测。数据库：PostgreSQL 16（pgvector + pg_trgm）、Redis 7。运维监控：Docker Compose、Nginx、Prometheus、Grafana、Tempo、Loki、OpenTelemetry。安全：JWT、OAuth2、DFA敏感词过滤、阿里云SMS。前端：原生HTML/CSS/JavaScript。",
    },
    {
        "heading": "模拟支付体系",
        "content": "2026年7月28日实现的完全模拟支付环境，仅数字为模拟、货币为人民币。完整支付流程：创建订单、选择渠道、支付确认、成功回调。三种模拟渠道：余额支付（100%成功率）、模拟支付宝（95%）、模拟微信支付（95%），带1-3秒模拟延迟。Token扣费机制：每万token自动扣费10元，后台异步执行，余额不足不阻塞对话。并发安全：Redis分布式锁 + PostgreSQL乐观锁（version字段）+ WAL交易流水（只追加不修改）。数据库6张新表。",
    },
    {
        "heading": "求职助手人格设计",
        "content": "第三人称人格「黎忠南」，独立端点/v2/chat/me，使用DeepSeek Flash模型，纯中文回答。沟通风格：适中长度、偏随意像朋友聊天、不用表情符号、低调谦虚。薪资规则：报10-12K范围，具体引导找本人详谈。知识库检索：当访客问技术细节时，自动调用search_project_knowledge工具从项目知识库获取准确信息。",
    },
    {
        "heading": "学习能力与自我评价",
        "content": "从土木工程零基础自学转型AI应用开发，3个月从第一行代码到项目上线公网。学习路径：Python基础、FastAPI后端、前端、Docker、PostgreSQL、Redis、Nginx、Prometheus/Grafana监控、OpenTelemetry链路追踪。项目全程一人完成，具备全栈工程能力、独立技术决策能力和快速学习能力。工作理念：团队协作中完成任务并学习，先解决问题再复盘。",
    },
    {
        "heading": "与AI应用开发岗位的匹配度",
        "content": "直接相关的技能：Python/FastAPI后端开发、Multi-Agent编排、RAG检索增强生成、LLM API调用与优化、Docker容器化部署、PostgreSQL+Redis数据库、Nginx负载均衡、Prometheus/Grafana监控。核心竞争力：从零到一构建完整生产级项目的经验、全栈工程能力、独立技术决策能力、超强学习能力（3个月转行上线）。虽为应届生，但已有公网上线的真实项目经得起拷问。",
    },
]


async def generate_embedding(text: str):
    """调用 Ollama Embedding 生成 768 维向量"""
    urls = [
        "http://host.docker.internal:11434/api/embeddings",
        "http://localhost:11434/api/embeddings",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json={
                    "model": "shaw/dmeta-embedding-zh",
                    "prompt": text[:512]
                })
                resp.raise_for_status()
                return resp.json()["embedding"]
        except Exception:
            continue
    print("Embedding 不可用，将使用 pg_trgm 兜底")
    return None


async def main():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM knowledge_chunks WHERE source = 'project'")
        print("已清理旧的项目知识库")

        for i, chunk in enumerate(KNOWLEDGE_CHUNKS):
            key = hashlib.md5(chunk["content"].encode()).hexdigest()[:16]
            emb = await generate_embedding(chunk["content"])

            if emb:
                await conn.execute(
                    "INSERT INTO knowledge_chunks (chunk_key, source, heading, content, embedding) "
                    "VALUES ($1, 'project', $2, $3, $4::vector) "
                    "ON CONFLICT (chunk_key) DO UPDATE SET content = $3, embedding = $4",
                    f"proj_{key}", chunk["heading"], chunk["content"], json.dumps(emb)
                )
            else:
                await conn.execute(
                    "INSERT INTO knowledge_chunks (chunk_key, source, heading, content) "
                    "VALUES ($1, 'project', $2, $3) "
                    "ON CONFLICT (chunk_key) DO UPDATE SET content = $3",
                    f"proj_{key}", chunk["heading"], chunk["content"]
                )
            print(f"  [{i+1}/{len(KNOWLEDGE_CHUNKS)}] {chunk['heading']}")

        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_project_knowledge_embedding
                ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 10)
            """)
            print("向量索引已创建")
        except Exception as e:
            print(f"向量索引创建失败（pg_trgm 兜底可用）: {e}")

    await close_pool()
    print(f"\n完成！共写入 {len(KNOWLEDGE_CHUNKS)} 条项目知识")


if __name__ == "__main__":
    asyncio.run(main())
