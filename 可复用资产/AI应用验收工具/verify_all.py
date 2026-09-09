# ============================================================
# 可复用资产：verify_all.py
# 来源：agent_gateway 生产机制验收链
# 实战验证：实战验证（五阶段一键验收）
# 依赖：mcp sdk；阶段3/4 需 PostgreSQL
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""
AI 应用验收工具 — 一键验收执行器（MCP + skill 组合的可运行检查）

按 SKILL.md 的五阶段流程，拉起 ../MCP sever/ 下 5 个 Server 并逐项断言：
  阶段 1 认证     → JWT令牌签发与校验
  阶段 2 并发限流 → 分布式锁与限流
  阶段 3 语义缓存 → 语义缓存（需 PostgreSQL）
  阶段 4 知识检索 → 知识库双通道检索（需 PostgreSQL）
  阶段 5 纠错映射 → 模型归因纠错映射

用法：
    python verify_all.py              全部阶段
    python verify_all.py --phase 1    只跑阶段 1
    python verify_all.py --skip-pg    跳过依赖 PostgreSQL 的阶段 3/4

退出码：0 = 全部通过；1 = 存在失败；2 = 全部跳过（无基础设施）。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

BASE = Path(__file__).resolve().parent.parent / "MCP sever"

PHASES = [
    {"n": 1, "name": "认证", "server": "JWT令牌签发与校验/jwt_token_service_server.py", "needs_pg": False},
    {"n": 2, "name": "并发与限流", "server": "分布式锁与限流/redis_lock_ratelimit_server.py", "needs_pg": False},
    {"n": 3, "name": "语义缓存", "server": "语义缓存/semantic_cache_server.py", "needs_pg": True},
    {"n": 4, "name": "知识检索", "server": "知识库双通道检索/pg_rag_search_server.py", "needs_pg": True},
    {"n": 5, "name": "纠错映射", "server": "模型归因纠错映射/correction_map_server.py", "needs_pg": False},
]


# ---------- 各阶段断言（失败抛 AssertionError，消息即失败原因） ----------

async def phase_auth(session):
    notes = []
    r = await session.call_tool("create_access_token", {"subject": "__accept__user"})
    t = r.content[0].text
    assert '"token": "' in t, f"签发 access 失败: {t[:120]}"
    token = t.split('"token": "')[1].split('"')[0]
    notes.append("签发 access OK")

    r = await session.call_tool("verify_token", {"token": token})
    assert '"valid": true' in r.content[0].text, f"验证往返失败: {r.content[0].text[:120]}"
    notes.append("验证往返 OK")

    r = await session.call_tool("verify_token", {"token": token[:-4] + "AAAA"})
    assert '"error"' in r.content[0].text, "篡改 token 未被拒绝"
    notes.append("篡改拒绝 OK")

    r = await session.call_tool("create_refresh_token", {"subject": "__accept__user"})
    ref = r.content[0].text.split('"token": "')[1].split('"')[0]
    r = await session.call_tool("refresh_access_token", {"refresh_token": ref})
    assert '"access_token"' in r.content[0].text, f"刷新轮换失败: {r.content[0].text[:120]}"
    notes.append("刷新轮换 OK")

    r = await session.call_tool("revoke_token", {"token": token})
    assert '"revoked": true' in r.content[0].text, f"吊销失败: {r.content[0].text[:120]}"
    r = await session.call_tool("verify_token", {"token": token})
    assert '"error"' in r.content[0].text, "吊销后仍验证通过"
    notes.append("吊销生效 OK")
    return notes


async def phase_lock(session):
    notes = []
    r = await session.call_tool("acquire_lock", {"key": "__accept__:lock", "ttl": 10})
    t = r.content[0].text
    assert '"acquired": true' in t, f"加锁失败: {t[:120]}"
    token = t.split('"token": "')[1].split('"')[0]
    notes.append("加锁 OK")

    r = await session.call_tool("release_lock", {"key": "__accept__:lock", "token": "wrong-token"})
    assert '"released": false' in r.content[0].text, "错误 token 释放被放行（Lua 持有者校验失效）"
    notes.append("他人 token 释放被拒 OK")

    r = await session.call_tool("release_lock", {"key": "__accept__:lock", "token": token})
    assert '"released": true' in r.content[0].text, "正确 token 释放失败"
    notes.append("本人释放 OK")

    for i in range(3):
        r = await session.call_tool("fixed_window_limit", {"key": "__accept__:qps", "limit": 2, "window_sec": 1})
    assert '"allowed": false' in r.content[0].text, "固定窗口超限未被拒绝"
    notes.append("限流超限拒绝 OK")
    return notes


async def phase_cache(session):
    notes = []
    r = await session.call_tool("cache_store", {"query": "__accept__问题", "response": "验收答案"})
    assert '"stored": true' in r.content[0].text, f"缓存写入失败: {r.content[0].text[:120]}"
    notes.append("写入 OK")

    r = await session.call_tool("cache_lookup", {"query": "__accept__问题"})
    assert '"hit": true' in r.content[0].text and '"level": "L0"' in r.content[0].text, "L0 命中失败"
    notes.append("L0 命中 OK")

    r = await session.call_tool("cache_lookup", {"query": "__accept__的问题哦", "threshold": 0.1})
    assert '"hit": true' in r.content[0].text, "近似问法语义命中失败"
    notes.append("语义命中 OK")

    r = await session.call_tool("cache_lookup", {"query": "完全不相关的天气问题"})
    assert '"hit": false' in r.content[0].text, "无关问法误命中"
    notes.append("无关问法 miss OK")
    return notes


async def phase_search(session):
    notes = []
    r = await session.call_tool("search_knowledge", {"query": "测试查询", "top_k": 3})
    assert '"total"' in r.content[0].text, f"检索失败: {r.content[0].text[:120]}"
    notes.append("召回通道可用 OK")

    r = await session.call_tool("search_knowledge", {"query": "测试查询", "top_k": 0})
    assert '"error"' in r.content[0].text, "top_k=0 未报错"
    notes.append("非法入参守卫 OK")
    return notes


async def phase_map(session):
    notes = []
    r = await session.call_tool(
        "add_mapping", {"trigger": "__accept__trigger", "answer": "验收答案", "mode": "contains"})
    assert '"added": true' in r.content[0].text, f"添加映射失败: {r.content[0].text[:120]}"
    notes.append("添加 OK")

    r = await session.call_tool("lookup_redirect", {"text": "前缀__accept__trigger后缀"})
    assert '"hit": true' in r.content[0].text, "contains 命中失败"
    notes.append("contains 命中 OK")

    r = await session.call_tool(
        "add_mapping", {"trigger": "__accept__trigger", "answer": "验收答案2", "mode": "contains"})
    assert '"updated": true' in r.content[0].text, "幂等更新失败"
    r = await session.call_tool("lookup_redirect", {"text": "__accept__trigger"})
    assert "验收答案2" in r.content[0].text, "更新未生效"
    notes.append("幂等更新 OK")

    r = await session.call_tool("lookup_redirect", {"text": "完全无关内容"})
    assert '"hit": false' in r.content[0].text, "无关文本误命中"
    notes.append("无关文本 miss OK")

    r = await session.call_tool("remove_mapping", {"trigger": "__accept__trigger", "mode": "contains"})
    assert '"removed": true' in r.content[0].text, "清理失败"
    notes.append("清理无残留 OK")
    return notes


PHASE_FNS = {1: phase_auth, 2: phase_lock, 3: phase_cache, 4: phase_search, 5: phase_map}


# ---------- 执行与报告 ----------

async def run_phase(phase: dict, skip_pg: bool) -> tuple:
    """返回 (状态, 说明)。状态: PASS / FAIL / SKIP

    注意：阶段断言失败后，stdio 上下文清理可能再抛 ExceptionGroup（SDK TaskGroup 噪音），
    必须在上下文内部先捕获阶段异常，避免真正的失败原因被清理期异常顶掉。
    """
    name = f"阶段 {phase['n']} {phase['name']}"
    if skip_pg and phase["needs_pg"]:
        return (name, "SKIP", "无 PostgreSQL 环境（--skip-pg）")
    srv = BASE / phase["server"]
    if not srv.exists():
        return (name, "SKIP", f"Server 不存在: {srv}")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(srv)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    captured = None
    notes = None
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                try:
                    notes = await PHASE_FNS[phase["n"]](session)
                except Exception as e:  # 阶段断言失败：先记录，正常退出上下文
                    captured = e
    except Exception as e:
        if captured is None:  # 清理期异常且无阶段异常 → 这才是真失败原因
            captured = e
    if captured is not None:
        if isinstance(captured, AssertionError):
            return (name, "FAIL", str(captured))
        return (name, "FAIL", f"执行异常 {type(captured).__name__}: {captured}")
    return (name, "PASS", "; ".join(notes))


async def main():
    args = sys.argv[1:]
    skip_pg = "--skip-pg" in args
    only = None
    if "--phase" in args:
        only = int(args[args.index("--phase") + 1])

    report = []
    for phase in PHASES:
        if only and phase["n"] != only:
            continue
        report.append(await run_phase(phase, skip_pg))

    print("\n=== AI 应用验收报告 ===")
    for name, status, detail in report:
        print(f"{status:5s}  {name:12s}  {detail}")
    print("=======================")

    fails = [r for r in report if r[1] == "FAIL"]
    skips = [r for r in report if r[1] == "SKIP"]
    if fails:
        sys.exit(1)
    if len(report) > 0 and len(skips) == len(report):
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
