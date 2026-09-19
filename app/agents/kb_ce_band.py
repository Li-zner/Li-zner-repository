"""高分 CE 保护带子系统 —— 从 tools.py 拆出（2026-09-16，防熵检查点）。

tools.py 已达 726 行（硬限 600），按 web_search / project_kb / kb_expand 同例整块
搬迁。本模块只做「CE 分与融合序的校准」：把 CE 明确认为更相关、且在该条单腿召回
里排名靠前的候选提回最终窗口。召回、RRF 融合与 CE 推理仍归 tools.py。

动因（ragclosure/reports/召回率提升分析.md §十六实测，text-embedding-v4@768 +
TOP=15）：CC061 金标 CE 0.9985 却被融合序压到第 7（Top5 最低 CE 仅 0.9180）、
CC099 金标 CE 0.9981 排第 6（Top5 最高 CE 0.9931）。全局调 RRF/blend 权重已证伪：
降 trgm 权重指标完全不动，抬 blend 到 0.8 只换来 MRR 0.8850 且 recall@5 掉 6.7pp。
"""

import os

# 默认关闭，验收通过后再改默认值（与邻接/章扩召的处置惯例一致）。
CE_BAND_ENABLED = os.getenv("KB_CE_BAND_ENABLED", "0") == "1"
# 单腿召回的头部宽度：候选须在 trgm 或 vector 任一路的前 N 名内
CE_BAND_LEG_TOP = int(os.getenv("KB_CE_BAND_LEG_TOP", "5"))
# CE 分须高出 TopK 内最低分多少才允许动窗口（防 bge-reranker 噪声分插队）
CE_BAND_MARGIN = float(os.getenv("KB_CE_BAND_MARGIN", "0.002"))


def _best_leg_rank(recall_lists: list[list], leg_top: int) -> dict:
    """每条候选在 trgm / 向量两路里的最好位次（只记前 leg_top 名之内的）"""
    best: dict = {}
    for leg in recall_lists:
        for rank, item in enumerate(leg[:leg_top], 1):
            key = item.get("chunk_key") if isinstance(item, dict) else str(item)
            if key and rank < best.get(key, 1 << 30):
                best[key] = rank
    return best


def _promote_high_ce(ranked: list, recall_lists: list[list], top_k: int,
                     leg_top: int, margin: float) -> list:
    """高分 CE 保护带：把「CE 更高 + 单腿靠前」的候选提进 TopK，返回新窗口。

    2026-09-16 新增（ragclosure/reports/召回率提升分析.md §十六）。真正的问题不是
    融合权重整体偏了，而是少数条目的 CE 分明确高于 TopK 内条目却被压出窗口——
    保护带只针对这一类，其余条目的融合序保持不变。

    规则（都可由环境变量配）：
      1. 候选须在某一路召回（trgm 或 vector）的前 leg_top 名内 —— 单腿排名靠前说明
         它不是噪声，而是被权重稀释的确定性信号；
      2. 候选 CE 分须高于当前 TopK 里最低的 CE 分（+margin）—— 只有 CE 明确认为它
         更相关才动窗口。

    插入位置取它自己的融合分位次，因此挤出去的必然是 TopK 内融合分最低的那条；
    ranked 必须已按融合分降序（_blend_rrf_ce 的输出）。CE 未启用时调用方把
    rerank_score 传 inf，规则自动退化成「单腿靠前 + 融合分高于窗口底线」。

    ponytail: 只在候选窗（KB_RERANK_TOP）内生效，深于候选窗的条目不会被拉回；
    要覆盖它们需先扩大候选窗或接更强的重排模型。
    """
    if top_k <= 0 or len(ranked) <= top_k:
        return ranked
    in_window = ranked[:top_k]
    floor = min(float(c.get("rerank_score", 0.0)) for c in in_window)
    leg_rank = _best_leg_rank(recall_lists, leg_top)
    boosted = list(ranked)
    for cand in ranked[top_k:]:
        if cand.get("chunk_key") not in leg_rank:
            continue
        if float(cand.get("rerank_score", 0.0)) <= floor + margin:
            continue
        # 找第一个融合分更低的条目作为插入点（ranked 已按融合分降序）
        insert_at = next((i for i, c in enumerate(boosted[:top_k])
                          if c.get("blend_score", 0.0) < cand.get("blend_score", 0.0)),
                         max(0, top_k - 1))
        boosted.insert(insert_at, cand)
        boosted.pop(top_k + 1)
    return boosted
