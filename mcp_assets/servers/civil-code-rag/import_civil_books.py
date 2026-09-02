"""
知识库数据补全脚本 — 补齐 knowledge_chunks 缺失法条（全量幂等补缺）

背景（2026-08 发现）：原构建脚本条号正则 `第[一二三四五六七八九十百]+条` 缺少
"千""零"字符，导致第一千条及以后的法条（人格权编后段、婚姻家庭、继承、侵权责任）
全部漏导。修正正则后，本脚本把 docx 中 DB 里不存在的法条全部补齐。

- 复用 build_knowledge_base.py 的编/章/条解析逻辑（自包含内联，正则已修复）
- 按现有格式：heading=编/章，content=法条原文，chunk_key=civil_md5[:16]
- 幂等：已存在的 chunk_key 自动跳过，可重复运行
- embedding：一次性探测本地 Ollama，不可用则写 NULL（检索逻辑不使用向量，无影响）

用法（项目根目录）：
    python mcp_assets/servers/civil-code-rag/import_civil_books.py
"""
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))
from _env import db_url  # noqa: E402

DOCX_PATH = _ROOT / "tests" / "中华人民共和国民法典.docx"
# 全量补缺：导入 docx 中 DB 不存在的所有法条（靠 chunk_key 幂等去重）
TARGET_PARTS = ()

try:
    from docx import Document
except ImportError:
    os.system(f"{sys.executable} -m pip install python-docx -q")
    from docx import Document


def extract_articles(docx_path):
    """按编/章/条切分（逻辑同 scripts/build_knowledge_base.py）"""
    doc = Document(docx_path)
    full_text = "\n".join(p.text for p in doc.paragraphs)

    chunks = []
    current_part, current_chapter = "", ""
    article_text = ""

    def flush():
        nonlocal article_text
        if article_text and len(article_text.strip()) >= 10:
            key = hashlib.md5(article_text.strip().encode()).hexdigest()[:16]
            chunks.append({
                "chunk_key": f"civil_{key}",
                "source": "civil_code",
                "heading": f"{current_part} / {current_chapter}" if current_chapter else current_part,
                "content": article_text.strip(),
            })
        article_text = ""

    for line in full_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^第[一二三四五六七八九十百]+编", line)
        if m:
            flush(); current_part, current_chapter = line, ""
            continue
        m = re.match(r"^第[一二三四五六七八九十百]+章", line)
        if m:
            flush(); current_chapter = line
            continue
        m = re.match(r"^第[一二三四五六七八九十百千万零]+条", line)
        if m:
            flush(); article_text = line
        elif article_text:
            article_text += "\n" + line
    flush()
    return chunks


async def try_embedding(text: str) -> list | None:
    """尝试本地 Ollama embedding；失败返回 None（不阻塞导入）"""
    if not _OLLAMA_OK[0]:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_OLLAMA_OK[1],
                                  json={"model": "nomic-embed-text", "prompt": text[:1000]})
            if r.status_code == 200:
                return r.json().get("embedding")
    except Exception:
        pass
    return None


async def probe_ollama() -> None:
    """一次性探测 Ollama 可用性（3 秒超时），不可用则全部 embedding 置 NULL"""
    for url in ("http://localhost:11434/api/embeddings",
                "http://host.docker.internal:11434/api/embeddings"):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.post(url, json={"model": "nomic-embed-text", "prompt": "探"})
                if r.status_code == 200:
                    _OLLAMA_OK[0] = True
                    _OLLAMA_OK[1] = url
                    print(f"    Ollama embedding 可用: {url}")
                    return
        except Exception:
            continue
    print("    Ollama 不可用，embedding 置 NULL（检索不使用向量，无影响）")


_OLLAMA_OK = [False, ""]


async def main() -> None:
    print(f"[1] 解析 {DOCX_PATH.name} ...")
    all_chunks = extract_articles(DOCX_PATH)
    # TARGET_PARTS 为空 = 全量补缺（幂等去重由 chunk_key 保证）
    target = all_chunks if not TARGET_PARTS else [
        c for c in all_chunks if c["heading"].startswith(TARGET_PARTS)]
    print(f"    全库 {len(all_chunks)} 条，本次目标 {len(target)} 条")

    await probe_ollama()

    import asyncpg
    conn = await asyncpg.connect(db_url("localhost"), timeout=10)
    try:
        existing = set(r["chunk_key"] for r in await conn.fetch(
            "SELECT chunk_key FROM knowledge_chunks WHERE source='civil_code'"))
        todo = [c for c in target if c["chunk_key"] not in existing]
        print(f"[2] 已存在 {len(target) - len(todo)} 条（跳过），待导入 {len(todo)} 条")

        # 批量插入（每批 100 条，embedding 生成失败自动降级为 NULL）
        BATCH = 100
        inserted = 0
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            rows = []
            for c in batch:
                emb = await try_embedding(c["content"]) if _OLLAMA_OK[0] else None
                rows.append((c["chunk_key"], c["source"], c["heading"], c["content"], emb))
            await conn.executemany(
                "INSERT INTO knowledge_chunks (chunk_key, source, heading, content, embedding) "
                "VALUES ($1, $2, $3, $4, $5)",
                rows,
            )
            inserted += len(batch)
            print(f"    已导入 {inserted}/{len(todo)}")
        print(f"[3] 本次导入 {inserted} 条")

        total = await conn.fetchval(
            "SELECT count(*) FROM knowledge_chunks WHERE source='civil_code'")
        print(f"[4] 完成后 civil_code 总量 = {total}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
