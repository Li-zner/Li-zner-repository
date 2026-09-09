from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
import httpx
import os
import json
import re
from datetime import datetime
import time
from ..middleware.auth import get_current_user
from ..core.config import HTTP_TIMEOUT_MEDIUM, MAP_API_TIMEOUT
from ..core.logging import setup_logging

logger = setup_logging()

class RouteRequest(BaseModel):
    departure: str
    destination: str

router = APIRouter(prefix="/api/map", tags=["map"])


# ===== 高频城市缓存（2026-09-10 主人定稿）=====
# 点击计数按日（zset，两天 TTL）；当日点击 Top-N 的城市才写入缓存：
#   天气：TTL 1 天（每日惰性刷新；高德免费接口无逐小时数据，8-22 点"最长天气段"
#         无法计算，按主人指示维持实况原样——即缓存写入时刻的实况）
#   美食景点：TTL 15 天（短期内不会变，无需频繁重生成）
# force=1（刷新按钮）：跳过读缓存重新生成，且无视 Top-N 一律覆盖写入。
# Top-N 只是"多少城市享受缓存秒出"的产品策略，单条 ~1-2KB，Top200 亦无压力；
# 可用 CITY_CACHE_TOP_N 环境变量调整。
CITY_HIT_ZSET = "map:city_hits:{date}"
CITY_CACHE_PREFIX = "map:city_cache:"
CITY_TOP_N = int(os.getenv("CITY_CACHE_TOP_N", "200"))
WEATHER_TTL_SECONDS = 86400        # 天气：每天刷新一次
RECOMMEND_TTL_SECONDS = 15 * 86400  # 美食景点：15 天刷新一次
# recommend 失败兜底数据（模块级单例：handler 用同一性判断"是兜底就不写缓存"）
_DEFAULT_REC = {"foods": ["当地特色小吃", "地道家常菜", "招牌美食"],
                "spots": ["城市地标", "历史文化街区", "自然公园"]}


async def _city_cache_get(city: str, kind: str):
    from ..core.redis import get_redis
    r = await get_redis()
    raw = await r.get(f"{CITY_CACHE_PREFIX}{kind}:{city[:64]}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _city_cache_put(city: str, kind: str, payload, force: bool = False) -> None:
    """点击计数（按日 zset）+ 当日 Top-N（或 force 刷新）才写缓存。
    TTL：天气 1 天 / 美食景点 15 天（2026-09-10 主人定稿）"""
    from ..core.redis import get_redis
    r = await get_redis()
    member = f"{kind}:{city[:64]}"
    zkey = CITY_HIT_ZSET.format(date=datetime.now().strftime("%Y%m%d"))
    await r.zincrby(zkey, 1, member)
    await r.expire(zkey, 172800)  # 两天，跨自然日仍能判定"当日 Top-N"
    if not force:
        rank = await r.zrevrank(zkey, member)
        if rank is None or rank >= CITY_TOP_N:
            return  # 不在当日 Top-N：不写，保持既有缓存（若有）继续服役
    ttl = WEATHER_TTL_SECONDS if kind == "weather" else RECOMMEND_TTL_SECONDS
    await r.set(f"{CITY_CACHE_PREFIX}{member}",
                json.dumps(payload, ensure_ascii=False), ex=ttl)


@router.get("/regeo")
async def reverse_geocode(lat: float, lng: float, current_user: dict = Depends(get_current_user)):
    """经纬度→城市名（高德逆地理编码）"""
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        return {"city": ""}
    try:
        url = "https://restapi.amap.com/v3/geocode/regeo"
        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            resp = await client.get(url, params={
                "key": amap_key,
                "location": f"{lng},{lat}",
                "radius": 1000,
                "extensions": "base"
            })
            if resp.status_code != 200:
                return {"city": ""}
            data = resp.json()
            if data.get("status") != "1":
                return {"city": ""}
            regeo = data.get("regeocode", {})
            addr_comp = regeo.get("addressComponent", {})
            # 优先返回城市名，如果城市为空则用省份名
            city = addr_comp.get("city", "") or addr_comp.get("province", "")
            # 高德可能在直辖市返回 []，此时取 province
            if isinstance(city, list):
                city = addr_comp.get("province", "")
            province = addr_comp.get("province", "")
            if isinstance(province, list):
                province = province[0] if province else city
            return {"city": city, "province": province}
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
        return {"city": ""}


def _is_reserved_ip(ip: str) -> bool:
    """判断 IP 是否为私有/保留地址（10.x / 172.16-31.x / 192.168.x / 127.x 等）。

    这类 IP 无法在高德 IP 定位里解析出真实城市，直接视为"无定位"，
    避免前端拿高德返回的"局域网"占位词当真实出发地（如"我从局域网出发…"）。
    """
    if not ip:
        return True
    import ipaddress as _ipa
    try:
        return _ipa.ip_address(ip).is_private or _ipa.ip_address(ip).is_loopback or _ipa.ip_address(ip).is_reserved
    except ValueError:
        return True  # 非合法 IP


@router.get("/iploc")
async def ip_location(request: Request, current_user: dict = Depends(get_current_user)):
    """IP 近似定位兜底：浏览器 geolocation 不可用时（国内 Chrome 走 Google 定位常被墙），
    用请求 IP 经高德 IP 定位接口拿城市。CF-Connecting-IP 是 Cloudflare 传的真实用户 IP。

    私有/保留 IP（如本地测试经 docker/局域网访问）或高德无法解析时返回空——
    定位未知应让前端改问出发地，而非把"局域网"当真实城市。
    """
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        return {"city": "", "province": ""}
    # 真实用户 IP：CF-Connecting-IP > X-Forwarded-For 首个 > X-Real-IP > 客户端 IP
    ip = (request.headers.get("cf-connecting-ip")
          or request.headers.get("x-forwarded-for")
          or request.headers.get("x-real-ip")
          or (request.client.host if request.client else ""))
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    if not ip or _is_reserved_ip(ip):
        return {"city": "", "province": ""}
    try:
        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            resp = await client.get("https://restapi.amap.com/v3/ip", params={"key": amap_key, "ip": ip})
            data = resp.json()
            if data.get("status") == "1":
                province = data.get("province", "") or ""
                city = data.get("city", "") or ""
                # 高德对无法精确归属的 IP 可能把 province/city 返回为空数组 []
                if isinstance(city, list):
                    city = city[0] if city else ""
                if isinstance(province, list):
                    province = province[0] if province else ""
                # 高德对私有/未知 IP 常返回"局域网"，无实际定位价值 → 视为空
                if city in ("局域网", "") or province in ("局域网", ""):
                    return {"city": "", "province": ""}
                # 高德直辖市的 city 可能为空/等于 province，兜底保持一致
                if not city:
                    city = province
                return {"city": city, "province": province, "ip": ip}
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        logger.debug(f"IP 定位服务不可用: {type(e).__name__}")
    return {"city": "", "province": ""}

def _weather_candidates(name: str):
    """生成高德天气可用的候选名称列表（原样 → 自治州核心名 → 去市/州后缀）"""
    if not name:
        return []
    candidates = [name]
    # 自治州：保留核心地名（如 海西蒙古族藏族自治州 → 海西）
    m = re.search(r'^(.*?)(?:维吾尔|壮|回|蒙古|藏|苗|彝|土家|侗|布依|瑶|白|哈尼|哈萨克|傣|黎|傈僳|佤|畲|高山|拉祜|水|东乡|纳西|景颇|柯尔克孜|土|达斡尔|仫佬|羌|布朗|撒拉|保安|仡佬|锡伯|阿昌|普米|朝鲜|满|鄂温克|鄂伦春|赫哲|门巴|珞巴|基诺)*族*自治州$', name)
    if m and m.start() > 0:
        core = name[:m.start()]
        candidates.append(core)
        candidates.append(core + '市')
    # 去掉末尾 市/州/地区/省
    cleaned = re.sub(r'(市|州|地区|特别行政区|省)$', '', name)
    if cleaned != name:
        candidates.append(cleaned)
    # 去重保序
    seen = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen

async def _resolve_adcode(amap_key: str, name: str, client):
    """通过高德地理编码把城市名解析为 adcode"""
    try:
        resp = await client.get("https://restapi.amap.com/v3/geocode/geo", params={
            "key": amap_key, "address": name
        })
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "1" and data.get("geocodes"):
                return data["geocodes"][0].get("adcode")
    except Exception as e:
        logger.debug(f"adcode 查询失败: {type(e).__name__}")
    return None

@router.get("/weather")
async def get_weather(city: str, force: int = 0, current_user: dict = Depends(get_current_user)):
    import time as _time
    _t0 = _time.perf_counter()
    result = await _weather_impl(city, force)
    logger.info(f"[map-perf] weather city={city} total={_time.perf_counter() - _t0:.2f}s")
    return result


async def _weather_impl(city: str, force: int):
    # 天气走高德预报（extensions=all）：出行查的是今天/明天/后天的白天天气，
    # 不是当下实况（2026-09-10 主人定稿）。高频城市当日缓存同前。
    if not force:
        cached = await _city_cache_get(city, "weather")
        if cached is not None:
            logger.info(f"[map-perf] weather city={city} cache=hit")
            return cached
    amap_key = os.getenv("AMAP_API_KEY")
    fallback = {"status": "1", "days": []}
    if not amap_key:
        return fallback

    try:
        # ---- 港澳特殊处理：用临近城市代替 ----
        fallback_map = {
            "香港": {"city": "深圳", "note": "（以下为深圳天气，香港临近仅供参考）"},
            "澳门": {"city": "珠海", "note": "（以下为珠海天气，澳门临近仅供参考）"},
        }

        candidates = _weather_candidates(city)
        note = ""
        if city in fallback_map:
            fb = fallback_map[city]
            candidates = [fb["city"]]
            note = fb["note"]

        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            # 1) 依次尝试候选名称
            for cand in candidates:
                if not cand:
                    continue
                resp = await client.get(
                    "https://restapi.amap.com/v3/weather/weatherInfo",
                    params={"city": cand, "key": amap_key, "extensions": "all"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out = _format_forecast(data, note)
                    if out is not None:
                        await _city_cache_put(city, "weather", out, force=bool(force))
                        return out

            # 2) 兜底：地理编码解析 adcode 后再查一次
            adcode = await _resolve_adcode(amap_key, city, client)
            if adcode:
                resp = await client.get(
                    "https://restapi.amap.com/v3/weather/weatherInfo",
                    params={"city": adcode, "key": amap_key, "extensions": "all"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out = _format_forecast(data, note)
                    if out is not None:
                        await _city_cache_put(city, "weather", out, force=bool(force))
                        return out
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
        # 网络超时或无法连接时返回兜底数据
        return fallback
    except Exception:
        return fallback
    # 全部失败 → 兜底数据（不抛 502，前端可正常展示美食/景点）
    return fallback


def _format_forecast(data: dict, note: str):
    """把高德预报（extensions=all）压成 今天/明天/后天 三天白天天气；无有效预报返回 None"""
    if not (data.get("status") == "1" and data.get("forecasts")):
        return None
    casts = (data["forecasts"][0].get("casts") or [])[:3]
    if not casts:
        return None
    labels = ["今天", "明天", "后天"]
    days = []
    for i, c in enumerate(casts):
        days.append({
            "label": labels[i] if i < len(labels) else c.get("date", f"第{i+1}天"),
            "weather": c.get("dayweather") or c.get("nightweather") or "--",
            "temp": f"{c.get('nighttemp', '--')}~{c.get('daytemp', '--')}℃",
        })
    out = {"status": "1", "days": days}
    if note:
        out["_note"] = note
    return out

@router.post("/recommend")
async def get_recommend(city: str, force: int = 0, current_user: dict = Depends(get_current_user)):
    import time as _time
    _t0 = _time.perf_counter()
    # 高频城市当日缓存：命中直接返回（不走 LLM 也不计费）；force=1 强制重新生成并覆盖
    if not force:
        cached = await _city_cache_get(city, "recommend")
        if cached is not None:
            logger.info(f"[map-perf] recommend city={city} cache=hit")
            return cached
    # 配额门禁（2026-09-07 审查 P1）：原先完全绕过 ensure_chat_allowed/日 token 限额，
    # 登录用户可刷 LLM 配额；LLM 调用期间占用并发槽位，finally 释放
    from ..services.chat_stream_ctx import ensure_chat_allowed
    from ..middleware.rate_limit import release_concurrent
    await ensure_chat_allowed(current_user)
    try:
        result = await _recommend_llm(city, username=current_user["username"])
        if result is not _DEFAULT_REC:
            await _city_cache_put(city, "recommend", result, force=bool(force))
        logger.info(f"[map-perf] recommend city={city} total={_time.perf_counter() - _t0:.2f}s cache=generate")
        return result
    finally:
        await release_concurrent(current_user["username"])


async def _recommend_llm(city: str, username: str = "") -> dict:
    """推荐正文（LLM 调用与解析），由 /recommend 在配额门禁内调用

    username 供计量扣费（2026-09-09 审查 P1：原先只过门禁不扣费不计日 token）。
    """
    # 与主链路同款 Key 轮询（统一 chat_support 取 Key，杜绝多 Key 白名单不一致的 400）
    import time
    from ..services.chat_support import get_deepseek_key
    _t0 = time.perf_counter()
    deepseek_key = await get_deepseek_key()
    _t_key = time.perf_counter()

    # ===== 通用兜底数据（API不可用时返回；模块级单例 _DEFAULT_REC，handler 靠同一性判断是否写缓存）=====

    if not deepseek_key:
        return _DEFAULT_REC

    import random
    # 随机即可（2026-09-10 主人定稿）：不强制风格差异，用 temperature 抖动自然变化；
    # temperature 仅 DeepSeek 侧携带（qwen 兜底时由 post_chat_completion 剥离防 400）
    prompt = f"""
你是一个本地美食和旅游专家。请为「{city}」推荐 3 道特色美食和 3 个必去景点。
要求：
1. 每次推荐的内容要多样化，避免千篇一律。
2. 只返回 JSON 格式，不要有其他文字。
格式：{{"foods":["美食1","美食2","美食3"], "spots":["景点1","景点2","景点3"]}}
"""
    from ..core.config import DEEPSEEK_MODEL
    # payload 对齐主链路成功形态：无 max_tokens（部分网关拒绝这些参数返回 400）
    payload = {
        "model": DEEPSEEK_MODEL,
        "temperature": round(random.uniform(0.9, 1.3), 2),
        "messages": [{"role": "user", "content": prompt}],
    }
    # 带降级（flash 优先：轻量结构化任务生成快 3 倍+，主模型兜底——2026-09-09 埋点结论）
    from ..services.chat_support import post_chat_completion
    try:
        ok, data = await post_chat_completion(payload, timeout=HTTP_TIMEOUT_MEDIUM,
                                              prefer_flash=True, preferred_timeout=8)
        _t_llm = time.perf_counter()
        if not ok:
            logger.warning(f"[map-perf] recommend city={city} 上游失败 耗时={_t_llm - _t0:.2f}s (key={_t_key - _t0:.2f}s)")
            return _DEFAULT_REC
        content = data["choices"][0]["message"]["content"]
        # 计量扣费（2026-09-09 审查 P1）：/recommend 原先只过配额门禁，不扣费不计日 token
        _usage = data.get("usage") or {}
        if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
            from ..services.llm_streaming import record_token_usage
            record_token_usage(_usage, username, "")
        # LLM 可能返回 ```json 包裹：走 _extract_json 容错解析
        from ..agents.sub_agents import _extract_json
        result = _extract_json(content)
        logger.info(
            f"[map-perf] recommend city={city} total={time.perf_counter() - _t0:.2f}s "
            f"(key={_t_key - _t0:.2f}s llm={_t_llm - _t_key:.2f}s parse={time.perf_counter() - _t_llm:.2f}s) "
            f"model={data.get('model', '?')} tokens={(_usage.get('total_tokens') or 0)}")
        return result
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError, httpx.HTTPStatusError):
        logger.warning(f"[map-perf] recommend city={city} 上游异常，返回兜底")
        return _DEFAULT_REC
    except Exception as e:
        logger.warning(f"recommend 解析失败（返回兜底）: {str(e)[:120]}")
        return _DEFAULT_REC


@router.post("/route")
async def plan_route(req: RouteRequest, current_user: dict = Depends(get_current_user)):
    """规划多段路线（支持高铁/飞机/火车/公交组合），返回每段的模式、起止城市、耗时"""
    # 配额门禁（2026-09-07 审查 P1）：同 /recommend，原先绕过全部限额直烧 LLM token
    from ..services.chat_stream_ctx import ensure_chat_allowed
    from ..middleware.rate_limit import release_concurrent
    await ensure_chat_allowed(current_user)
    try:
        return await _plan_route_llm(req.departure, req.destination, username=current_user["username"])
    finally:
        await release_concurrent(current_user["username"])


async def _plan_route_llm(departure: str, destination: str, username: str = "") -> dict:
    """路线规划正文（LLM 调用与解析），由 /route 在配额门禁内调用

    username 供计量扣费（2026-09-09 审查 P1：原先只过门禁不扣费不计日 token）。
    """
    # Key 取法统一（2026-09-07 审查 P2）：与 /recommend 一致走池化轮询，不再裸读 env
    from ..services.chat_support import get_deepseek_key
    deepseek_key = await get_deepseek_key()
    if not deepseek_key:
        raise HTTPException(503, "路线规划服务暂不可用")
    prompt = f"""你是交通路线规划专家。请为从「{departure}」到「{destination}」规划合理的交通路线。

要求：
1. 根据两地距离选择合适的主要交通方式：
   - < 200km: 高铁或自驾
   - 200-800km: 高铁优先
   - 800-1500km: 飞机+高铁组合
   - > 1500km: 飞机
2. 如果距离适中，可以规划多段路线（如飞机到某城市再转高铁到目的地）
3. 每段路线必须包含：出发城市、到达城市、交通方式、预计耗时（分钟）、距离（公里）
4. 交通方式用：高铁/飞机/火车/公交/自驾

只返回JSON，格式：
{{
  "legs": [
    {{"from": "北京", "to": "广州", "mode": "飞机", "duration_min": 180, "distance_km": 1967}},
    {{"from": "广州", "to": "珠海", "mode": "高铁", "duration_min": 60, "distance_km": 116}}
  ],
  "total_duration_min": 240,
  "total_distance_km": 2083,
  "recommendation": "建议先飞广州，再转高铁到珠海，全程约4小时"
}}"""
    from ..core.config import DEEPSEEK_API_BASE, DEEPSEEK_MODEL
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
        resp = await client.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"},
            json={"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 500}
        )
        if resp.status_code != 200:
            raise HTTPException(502, "DeepSeek API error")
        data = resp.json()
        # 计量扣费（2026-09-09 审查 P1）：/route 原先只过配额门禁，不扣费不计日 token
        _usage = data.get("usage") or {}
        if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
            from ..services.llm_streaming import record_token_usage
            record_token_usage(_usage, username, "")
        try:
            import json as _json
            content = data["choices"][0]["message"]["content"]
            return _json.loads(content)
        except Exception:  # noqa: BLE001 — 解析失败回退直答路线（不泄露内部错误）
            return {"legs": [{"from": departure, "to": destination, "mode": "高铁", "duration_min": 120, "distance_km": 500}], "total_duration_min": 120, "total_distance_km": 500, "recommendation": f"从{departure}到{destination}建议乘坐高铁"}


@router.post("/amap-route")
async def amap_route(req: RouteRequest, current_user: dict = Depends(get_current_user)):  # noqa: func-length 豁免（主人决策 2026-09-05）：高德 API 集成层，与外部服务端点一一对应，拆分破坏内聚
    """使用高德地图 API 规划真实路线（驾车/公交/步行）"""
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        raise HTTPException(503, "路线规划服务暂不可用")

    dep = req.departure
    dest = req.destination

    # 1. 先通过高德地理编码获取起终点经纬度
    # 显式超时（2026-09-07 审查 P2）：默认 5s×5 次连外重试，无超时上限
    async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
        # 地理编码（地点→坐标）
        geo_dep_resp = await client.get("https://restapi.amap.com/v3/geocode/geo", params={
            "key": amap_key, "address": dep, "city": dep
        })
        geo_dest_resp = await client.get("https://restapi.amap.com/v3/geocode/geo", params={
            "key": amap_key, "address": dest, "city": dest
        })
        geo_dep = geo_dep_resp.json()
        geo_dest = geo_dest_resp.json()

        if geo_dep.get("status") != "1" or geo_dest.get("status") != "1":
            raise HTTPException(502, "地理编码失败")

        dep_loc = geo_dep["geocodes"][0]["location"] if geo_dep.get("geocodes") else None
        dest_loc = geo_dest["geocodes"][0]["location"] if geo_dest.get("geocodes") else None
        if not dep_loc or not dest_loc:
            raise HTTPException(502, "无法获取起终点坐标")

        # 2. 获取驾车路线
        driving_resp = await client.get("https://restapi.amap.com/v3/direction/driving", params={
            "key": amap_key, "origin": dep_loc, "destination": dest_loc,
            "strategy": 0, "extensions": "all"
        })
        driving_data = driving_resp.json()

        # 3. 获取公交/火车/飞机综合路线
        transit_resp = await client.get("https://restapi.amap.com/v3/direction/transit/integrated", params={
            "key": amap_key, "origin": dep_loc, "destination": dest_loc,
            "city": dep, "cityd": dest, "strategy": 0, "extensions": "all"
        })
        transit_data = transit_resp.json()

        # 4. 获取骑行路线
        bicycling_resp = await client.get("https://restapi.amap.com/v3/direction/bicycling", params={
            "key": amap_key, "origin": dep_loc, "destination": dest_loc
        })
        bicycling_data = bicycling_resp.json()

        # 5. 获取步行路线
        walking_resp = await client.get("https://restapi.amap.com/v3/direction/walking", params={
            "key": amap_key, "origin": dep_loc, "destination": dest_loc
        })
        walking_data = walking_resp.json()

    # 解析结果
    result = {"departure": dep, "destination": dest, "departure_location": dep_loc, "destination_location": dest_loc}

    # 驾车路线
    if driving_data.get("status") == "1" and driving_data.get("route", {}).get("paths"):
        path = driving_data["route"]["paths"][0]
        # steps 为空数组时 [0] 会 IndexError（2026-09-07 审查 P2），or 兜底空对象
        result["driving"] = {
            "distance_km": round(int(path.get("distance", 0)) / 1000, 1),
            "duration_min": round(int(path.get("duration", 0)) / 60),
            "tolls": path.get("tolls", "0"),
            "polyline": (path.get("steps") or [{}])[0].get("polyline", "")
        }

    # 公交/火车路线
    if transit_data.get("status") == "1" and transit_data.get("route", {}).get("transits"):
        transits = transit_data["route"]["transits"][:3]  # 最多3条
        result["transit"] = []
        for t in transits:
            segments = t.get("segments", [])
            legs = []
            for seg in segments:
                mode = None
                if "railway" in seg:
                    mode = "火车" if "火车" in str(seg.get("railway", {}).get("name", "")) else "高铁"
                    r = seg["railway"]
                    legs.append({
                        "mode": mode or "高铁",
                        "from": r.get("departure_station", {}).get("name", dep),
                        "to": r.get("arrival_station", {}).get("name", dest),
                        "duration_min": round(int(r.get("time", 0)) / 60) if r.get("time") else 0,
                        "distance_km": round(int(r.get("distance", 0)) / 1000, 1) if r.get("distance") else 0
                    })
                elif "bus" in seg:
                    bus_info = seg["bus"]
                    bus_lines = bus_info.get("buslines", [{}])[0]
                    legs.append({
                        "mode": "公交",
                        "from": bus_lines.get("departure_stop", {}).get("name", ""),
                        "to": bus_lines.get("arrival_stop", {}).get("name", ""),
                        "duration_min": round(int(seg.get("duration", 0)) / 60) if seg.get("duration") else 0,
                        "distance_km": round(int(seg.get("distance", 0)) / 1000, 1) if seg.get("distance") else 0
                    })
                elif "walking" in seg:
                    legs.append({
                        "mode": "步行",
                        "duration_min": round(int(seg.get("duration", 0)) / 60) if seg.get("duration") else 0,
                        "distance_km": round(int(seg.get("distance", 0)) / 1000, 1) if seg.get("distance") else 0
                    })
            total_time = sum(l.get("duration_min", 0) for l in legs)
            total_dist = sum(l.get("distance_km", 0) for l in legs)
            result["transit"].append({
                "legs": legs,
                "total_duration_min": total_time,
                "total_distance_km": round(total_dist, 1)
            })

    # 骑行路线（短途）
    if bicycling_data.get("status") == "1" and bicycling_data.get("route", {}).get("paths"):
        path = bicycling_data["route"]["paths"][0]
        result["bicycling"] = {
            "distance_km": round(int(path.get("distance", 0)) / 1000, 1),
            "duration_min": round(int(path.get("duration", 0)) / 60)
        }

    # 步行路线（短途）
    if walking_data.get("status") == "1" and walking_data.get("route", {}).get("paths"):
        path = walking_data["route"]["paths"][0]
        result["walking"] = {
            "distance_km": round(int(path.get("distance", 0)) / 1000, 1),
            "duration_min": round(int(path.get("duration", 0)) / 60)
        }

    return result


@router.get("/geojson")
async def get_geojson(adcode: str = "100000", current_user: dict = Depends(get_current_user)):
    """代理 GeoJSON 地图数据，绕过阿里云 DataV 的 Referer 防盗链（需登录）"""
    # P3 修复：adcode 只允许 6 位数字，防 URL 路径拼接
    if not re.fullmatch(r"\d{6}", adcode):
        raise HTTPException(400, "adcode 必须为 6 位数字")
    if adcode == "100000":
        url = "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json"
    else:
        url = f"https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
    async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
        resp = await client.get(url, headers={"User-Agent": "AgentGateway/1.0"})
        if resp.status_code != 200:
            raise HTTPException(502, f"GeoJSON data fetch failed (HTTP {resp.status_code})")
        return resp.json()
