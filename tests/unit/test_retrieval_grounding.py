"""检索评测资产与引用溯源的回归测试（2026-09-06 AI 层补强）

1. 标注文件与用例集一致：id 对得上、法条号格式合法、无"既无标注也未排除"的漏洞
2. find_ungrounded_citations： grounded 引用不报、编造条文号报、content 依据不误报
"""
import re

import yaml

_ARTICLE_RE = re.compile(r"^第[一二三四五六七八九十百千零0-9]+条$")


def _load_label_doc():
    import json
    from pathlib import Path
    base = Path(__file__).resolve().parents[2]
    return json.loads((base / "tests" / "civil_retrieval_labels.json").read_text(encoding="utf-8"))


def _civil_ids():
    from pathlib import Path
    base = Path(__file__).resolve().parents[2]
    raw = yaml.safe_load((base / "tests" / "test_cases.yaml").read_text(encoding="utf-8"))
    return {c["id"] for c in raw.get("test_cases", raw) if c.get("category") == "民法典"}


def test_labels_cover_all_civil_cases():
    """标注完整性：60 条用例必须"已标注"或"已排除"二选一，标注 id 与法条号格式合法"""
    doc = _load_label_doc()
    ids = _civil_ids()
    assert len(ids) == 60, f"用例集应为 60 条, 实际 {len(ids)}"
    labeled, excluded = set(doc["labels"]), set(doc["excluded"])
    assert not (labeled & excluded), "同一用例不得既标注又排除"
    assert labeled | excluded == ids, f"未覆盖: {ids - labeled - excluded}"
    for cid, arts in doc["labels"].items():
        assert arts, f"{cid} 标注为空（应移入 excluded）"
        for a in arts:
            assert _ARTICLE_RE.match(a), f"{cid} 法条号格式非法: {a}"


def test_find_ungrounded_citations():
    """引用校验三分支：全部有据不报 / 编造条文号报 / 依据在 content 中不误报"""
    from app.services.chat_fast_paths import find_ungrounded_citations

    contexts = [
        "第一千零七十七条　离婚冷静期",
        "第一千零八十四条　离婚后子女抚养",  # heading 不含条文号的编/章名时, content 是依据
    ]
    # 全部有据：不报
    assert find_ungrounded_citations(
        "依据第一千零七十七条……依据: 第一千零七十七条", contexts) == []
    # 编造条文号：报出且去重排序
    assert find_ungrounded_citations(
        "依据第九千九百九十九条和第九千九百九十九条", contexts) == ["第九千九百九十九条"]
    # content 是依据语料：正文与 content 重叠的条文号不误报
    content_grounded = ["第六编　婚姻家庭\n……第一千零八十四条……"]
    assert find_ungrounded_citations("第一千零八十四条", content_grounded) == []


def test_article_regex_includes_zero_numeral():
    """回归：法条正则字符类必须含"零"——"第一千零七十七条"曾因缺零匹配不上"""
    from app.agents.routing_table import _CIVIL_CODE_ARTICLE_PATTERN

    m = _CIVIL_CODE_ARTICLE_PATTERN.search("民法典第一千零七十七条")
    assert m, "含零法条号必须能命中 article_ref 正则"
    r = None
    from app.agents import routing_table as rt
    r = rt.route_query("民法典第一千零七十七条规定了什么")
    assert r.match_type == "article_ref"
