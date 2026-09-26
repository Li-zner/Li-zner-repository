"""RAG 请求内容侧读取：问题、回答、召回运行和知识片段。"""


def _as_json(value, fallback):
    """兼容 asyncpg 返回的 JSON 字符串和已解析对象。"""
    import json

    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return fallback


def _as_text_list(value) -> list[str]:
    """把 JSONB 字符串/列表统一归一为字符串列表，禁止按字符拆包。"""
    parsed = _as_json(value, [])
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, (str, int))]


async def fetch_request_content(conn, request_uid) -> tuple:
    """读取请求关联的问题、回答、召回运行和知识片段。"""
    answer = await conn.fetchrow(
        "SELECT query, answer, answer_len, cited_numbers, "
        "retrieval_returned_count, retrieval_keys, retrieval_method, "
        "retrieval_latency_ms, ungrounded_citations, feedback "
        "FROM answer_traces WHERE request_uid = $1 "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        request_uid)
    retrievals = await conn.fetch(
        "SELECT query, method, trgm_hits, vec_hits, max_trgm_sim, "
        "max_vec_sim, returned_count, rerank_used, latency_ms, "
        "returned_chunk_keys, created_at "
        "FROM retrieval_traces WHERE request_uid = $1 "
        "ORDER BY created_at, id LIMIT 50",
        request_uid)
    keys = []
    for row in retrievals:
        keys.extend(_as_text_list(row["returned_chunk_keys"])[:50])
    if answer:
        keys.extend(_as_text_list(answer["retrieval_keys"])[:50])
    unique_keys = list(dict.fromkeys(key for key in keys if key))[:20]
    chunks = []
    if unique_keys:
        rows = await conn.fetch(
            "SELECT chunk_key, source, heading, content "
            "FROM knowledge_chunks WHERE chunk_key = ANY($1::text[])",
            unique_keys)
        by_key = {row["chunk_key"]: row for row in rows}
        chunks = [by_key[key] for key in unique_keys if key in by_key]
    return answer, retrievals, chunks


def request_content_payload(answer, retrievals, chunks) -> dict:
    """把内容侧记录转换为稳定 API 结构。"""
    runs = [
        {
            "query": (row["query"] or "")[:1000],
            "method": row["method"],
            "trgm_hits": row["trgm_hits"],
            "vec_hits": row["vec_hits"],
            "max_trgm_sim": row["max_trgm_sim"],
            "max_vec_sim": row["max_vec_sim"],
            "returned_count": row["returned_count"],
            "rerank_used": row["rerank_used"],
            "latency_ms": row["latency_ms"],
            "chunk_keys": _as_text_list(row["returned_chunk_keys"])[:50],
            "created_at": str(row["created_at"]),
        }
        for row in retrievals
    ]
    first_query = runs[0]["query"] if runs else ""
    answer_value = answer["answer"] if answer else ""
    answer_len = (
        answer["answer_len"]
        if answer and answer["answer_len"] is not None
        else len(answer_value or "")
    )
    return {
        "query": (answer["query"] if answer else first_query) or first_query,
        "answer": (answer_value or "")[:4000],
        "answer_len": answer_len,
        "cited_numbers": (
            _as_json(answer["cited_numbers"], []) if answer else []),
        "ungrounded_citations": (
            _as_json(answer["ungrounded_citations"], []) if answer else []),
        "retrieval_returned_count": (
            answer["retrieval_returned_count"] if answer else None),
        "retrieval_method": answer["retrieval_method"] if answer else None,
        "retrieval_latency_ms": (
            answer["retrieval_latency_ms"] if answer else None),
        "feedback": answer["feedback"] if answer else None,
        "retrieval_runs": runs,
        "chunks": [
            {
                "chunk_key": row["chunk_key"],
                "source": row["source"],
                "heading": row["heading"],
                "content": (row["content"] or "")[:3000],
            }
            for row in chunks
        ],
    }
