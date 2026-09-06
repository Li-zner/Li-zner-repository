"""检索管线纯函数单测（2026-09-06 AI 层补强）：RRF 融合 + 权限片段 + 用户决策常量"""
from app.agents.tools import RECALL_LIMIT, RERANK_TOP, _perm_clause, _rrf_merge


def _c(key: str, sim: float = 0.5) -> dict:
    return {"chunk_key": key, "heading": key, "content": "", "similarity": sim}


def test_rrf_merge_fuses_and_dedups():
    """双路都命中的块排最前；单路命中按相对位置融合；无重复输出"""
    trgm = [_c("a"), _c("b"), _c("c")]
    vector = [_c("b"), _c("d"), _c("a")]
    merged = _rrf_merge(trgm, vector)
    keys = [m["chunk_key"] for m in merged]
    assert len(keys) == 4, "按 chunk_key 去重"
    # b 双路命中（1/(60+2)+1/(60+1)）应高于单路第一 a（1/61）
    assert keys[0] == "b"
    assert all(m["similarity"] for m in merged), "保留原始属性"


def test_rrf_merge_single_and_empty():
    """单路退化为原排序；全空返回空（向量路 Ollama 不可用时自然退化）"""
    assert [m["chunk_key"] for m in _rrf_merge([_c("x"), _c("y")])] == ["x", "y"]
    assert _rrf_merge([], []) == []


def test_perm_clause_modes():
    """三态权限片段（P0 #29/#41 语义保持）：None 不过滤 / [] 仅公开 / 非空 公开+权限组"""
    assert _perm_clause(None) == ""
    assert "$3" not in _perm_clause([])
    assert "COALESCE(permission, '{}') = '{}'" in _perm_clause([])
    clause = _perm_clause(["vip"])
    assert "permission && $3" in clause and "COALESCE" in clause


def test_user_decision_constants():
    """钉住 2026-09-06 用户决策：召回窗 15 / 重排 5——防止无声回退到旧默认"""
    assert RECALL_LIMIT == 15
    assert RERANK_TOP == 5
