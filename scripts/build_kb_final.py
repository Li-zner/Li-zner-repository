"""
Final KB builder - runs inside container, no external model downloads
Uses pg_trgm trigram similarity for fuzzy search as embedding-free semantic search
"""
import json, hashlib, asyncio, asyncpg, os, sys
from docx import Document

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

DOCX = "/app/tests/中华人民共和国民法典.docx"
DB_URL = os.environ.get("DATABASE_URL") or db_url("postgres")

def extract_chunks(docx_path):
    doc = Document(docx_path)
    text = "\n".join(p.text for p in doc.paragraphs)
    
    chunks = []
    part, chapter, arts, cur = "", "", [], ""
    
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("第") and "编" in line and len(line) < 10:
            _flush(part, chapter, arts, chunks)
            arts, cur = [], ""
            part = line
            chapter = ""
        elif line.startswith("第") and "章" in line and len(line) < 10:
            _flush(part, chapter, arts, chunks)
            arts, cur = [], ""
            chapter = line
        elif line.startswith("第") and "条" in line:
            if cur:
                arts.append(cur.strip())
            cur = line
        else:
            cur += "\n" + line if cur else line
    
    if cur:
        arts.append(cur.strip())
    _flush(part, chapter, arts, chunks)
    return chunks

def _flush(part, chapter, arts, chunks):
    for a in arts:
        if len(a) < 10:
            continue
        key = hashlib.md5(a.encode()).hexdigest()[:16]
        chunks.append({
            "chunk_key": f"civil_{key}",
            "source": "civil_code",
            "heading": f"{part} / {chapter}" if chapter else part,
            "content": a,
        })

async def store(pool, chunks):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM knowledge_chunks WHERE source = 'civil_code'")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_trgm ON knowledge_chunks USING gin (content gin_trgm_ops)")
        
        for i, c in enumerate(chunks):
            await conn.execute(
                "INSERT INTO knowledge_chunks (chunk_key, source, heading, content) VALUES ($1,$2,$3,$4) ON CONFLICT (chunk_key) DO UPDATE SET content=$4",
                c["chunk_key"], c["source"], c["heading"], c["content"]
            )
            if (i+1) % 100 == 0:
                print(f"  {i+1}/{len(chunks)}")
        
        print(f"  ALL {len(chunks)}/{len(chunks)} - COMPLETE")
        # Verify
        row = await conn.fetchrow("SELECT count(*) FROM knowledge_chunks WHERE source='civil_code'")
        print(f"  Verified: {row['count']} chunks in DB")

async def main():
    print("Extracting chunks...")
    chunks = extract_chunks(DOCX)
    print(f"Extracted {len(chunks)} semantic chunks")
    print("Connecting to DB...")
    pool = await asyncpg.create_pool(DB_URL)
    print("Storing...")
    await store(pool, chunks)
    await pool.close()
    print("DONE")

if __name__ == "__main__":
    asyncio.run(main())
