"""高性能 JSON 工具（orjson 加速，带标准库回退）

高收益点替换：LLM 返回的大 JSON 解析、日志格式化等 CPU 热点。
SSE 流式小对象仍用标准库（见 v2.py），此处只覆盖大 JSON 场景。

用法：
    from ..core.jfast import dumps, loads
    text = dumps(obj)   # 返回 str（orjson 输出 bytes 后 decode）
    obj  = loads(text)  # 与 json.loads 兼容
"""
try:
    import orjson
    import json as _json

    def dumps(obj, ensure_ascii=False, **kwargs):
        """序列化为 str。orjson 默认 UTF-8，ensure_ascii 参数仅作兼容保留（无效）。
        注意：orjson 不支持 default 回调（P2 #76），含不可序列化对象时请先处理。"""
        return orjson.dumps(obj).decode("utf-8")

    def loads(s):
        """解析 JSON，异常统一转成 json.JSONDecodeError（与标准库兼容）。"""
        try:
            return orjson.loads(s)
        except orjson.JSONDecodeError as e:
            raise _json.JSONDecodeError(
                e.args[0] if e.args else "Invalid JSON",
                getattr(e, "doc", s or ""),
                getattr(e, "pos", 0),
            ) from e

    _HAS_ORJSON = True
except ImportError:
    import json as _json
    import logging
    # 降级提示：orjson 缺失时大 JSON 性能下降（P2 #74），仅提示一次
    logging.warning("orjson 不可用，jfast 降级到标准库 json（大 JSON 解析性能下降）")

    def dumps(obj, ensure_ascii=False, **kwargs):
        return _json.dumps(obj, ensure_ascii=ensure_ascii, **kwargs)

    def loads(s):
        return _json.loads(s)

    _HAS_ORJSON = False
