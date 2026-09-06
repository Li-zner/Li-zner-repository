"""
民法典知识库构建脚本
按语义结构（编/章/条）切分 → 生成嵌入 → 存入 pgvector
"""
import os, re, json, sys, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from docx import Document
except ImportError:
    os.system(f"{sys.executable} -m pip install python-docx -q")
    from docx import Document

import asyncio
import httpx
from app.core.config import EMBEDDING_API_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL
from app.core.db import init_pool, get_pool, close_pool

DOCX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "中华人民共和国民法典.docx")
EMBED_URL = EMBEDDING_API_URL or "https://api.deepseek.com/v1/embeddings"
EMBED_KEY = EMBEDDING_API_KEY or os.getenv("DEEPSEEK_API_KEY", "")
EMBED_MODEL = EMBEDDING_MODEL or "text-embedding-3-small"


def extract_articles(docx_path):
    """按民法典语义结构切分：编 → 章 → 条"""
    doc = Document(docx_path)
    full_text = "\n".join([p.text for p in doc.paragraphs])

    chunks = []
    current_part = ""   # 编
    current_chapter = ""  # 章
    current_articles = []
    article_text = ""

    for line in full_text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 匹配"第X编"
        m = re.match(r'^第[一二三四五六七八九十百]+编', line)
        if m:
            _flush_article(chunks, current_part, current_chapter, current_articles, article_text)
            current_articles = []
            article_text = ""
            current_part = line
            current_chapter = ""
            continue

        # 匹配"第X章"
        m = re.match(r'^第[一二三四五六七八九十百]+章', line)
        if m:
            _flush_article(chunks, current_part, current_chapter, current_articles, article_text)
            current_articles = []
            article_text = ""
            current_chapter = line
            continue

        # 匹配"第X条"
        m = re.match(r'^第[一二三四五六七八九十百]+条', line)
        if m:
            if article_text:
                current_articles.append(article_text.strip())
            article_text = line
        else:
            if article_text:
                article_text += "\n" + line

    if article_text:
        current_articles.append(article_text.strip())
    _flush_article(chunks, current_part, current_chapter, current_articles, article_text)

    print(f"提取 {len(chunks)} 条知识片段")
    return chunks


def _flush_article(chunks, part, chapter, articles, last_text):
    """按条合并，每条作为一个独立 chunk"""
    for art in articles:
        if len(art) < 10:
            continue
        key = hashlib.md5(art.encode()).hexdigest()[:16]
        chunks.append({
            "chunk_key": f"civil_{key}",
            "source": "civil_code",
            "heading": f"{part} / {chapter}" if chapter else part,
            "content": art,
        })


async def generate_embedding(text):
    """使用本地 Ollama embedding 模型生成向量（768维）"""
    import logging
    logger = logging.getLogger(__name__)
    # 优先尝试本地 Ollama（容器内→host.docker.internal，宿主机→localhost）
    urls = [
        "http://host.docker.internal:11434/api/embeddings",
        "http://localhost:11434/api/embeddings",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    json={"model": "shaw/dmeta-embedding-zh", "prompt": text}
                )
                resp.raise_for_status()
                data = resp.json()
                return data["embedding"]
        except Exception:
            continue
    logger.warning(f"Embedding 生成失败（所有端点均不可达）")
    return None


async def store_chunks(chunks):
    """存入 knowledge_chunks 表"""
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 清空旧数据
        await conn.execute("DELETE FROM knowledge_chunks WHERE source = 'civil_code'")
        
        for i, chunk in enumerate(chunks):
            # 生成嵌入
            emb = await generate_embedding(chunk["content"])
            
            if emb:
                await conn.execute(
                    "INSERT INTO knowledge_chunks (chunk_key, source, heading, content, embedding) "
                    "VALUES ($1, $2, $3, $4, $5::vector) "
                    "ON CONFLICT (chunk_key) DO UPDATE SET content = $4, embedding = $5",
                    chunk["chunk_key"], chunk["source"], chunk["heading"],
                    chunk["content"], json.dumps(emb)
                )
            else:
                await conn.execute(
                    "INSERT INTO knowledge_chunks (chunk_key, source, heading, content) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (chunk_key) DO UPDATE SET content = $4",
                    chunk["chunk_key"], chunk["source"], chunk["heading"], chunk["content"]
                )
            
            if (i + 1) % 20 == 0:
                print(f"  已存储 {i+1}/{len(chunks)}")

    await close_pool()
    print(f"✅ 完成，共存储 {len(chunks)} 条")


async def main():
    print("=" * 50)
    print("民法典知识库构建")
    print("=" * 50)
    chunks = extract_articles(DOCX_PATH)
    await store_chunks(chunks)

    # 创建向量索引（如不存在）
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_embedding ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)")
        print("✅ 向量索引已确保")

if __name__ == "__main__":
    asyncio.run(main())
