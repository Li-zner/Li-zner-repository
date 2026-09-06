"""
civil_code 知识块 embedding 回填 — 向量路上线配套（2026-09-06）

库中 1260 块约 399 块缺向量（历史导入批次遗漏）；向量召回对 embedding IS NULL
的块天然不可见，回填后全库可被语义召回。需 Ollama（shaw/dmeta-embedding-zh）
在本地运行：curl http://localhost:11434/api/tags 可验证。

刻意独立于 app.* 导入：应用配置对密钥 fail-fast，运维脚本只需 DATABASE_URL。
用法：
    DATABASE_URL=postgresql://... python scripts/backfill_civil_embeddings.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import httpx

BASE = Path(__file__).resolve().parent.parent
# 从 .env 加载（与 run_civil_eval.py 同一模式；DATABASE_URL 也可直接从环境传入）
_env_path = BASE / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "shaw/dmeta-embedding-zh"


async def embed(client: httpx.AsyncClient, text: str) -> list | None:
    """调 Ollama 生成 embedding；失败返回 None（与 app 内 _generate_embedding 同款语义）"""
    try:
        resp = await client.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text[:512]})
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        print(f"embedding 失败: {type(e).__name__}: {str(e)[:80]}")
        return None


async def main():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("缺少 DATABASE_URL 环境变量")

    conn = await asyncpg.connect(dsn, timeout=10)
    done = 0
    try:
        rows = await conn.fetch(
            "SELECT chunk_key, content FROM knowledge_chunks "
            "WHERE source = 'civil_code' AND embedding IS NULL")
        print(f"待回填: {len(rows)} 块")
        async with httpx.AsyncClient(timeout=60) as client:
            for r in rows:
                emb = await embed(client, r["content"])
                if emb is None:
                    # 连续失败基本等于 Ollama 没起：中止而非空转全表
                    print(f"已中止。完成 {done}/{len(rows)}")
                    return
                await conn.execute(
                    "UPDATE knowledge_chunks SET embedding = $1::vector WHERE chunk_key = $2",
                    json.dumps(emb), r["chunk_key"])
                done += 1
                if done % 50 == 0:
                    print(f"  已回填 {done}/{len(rows)}")
        print(f"回填完成: {done}/{len(rows)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
