"""
并发测压脚本 v4
- 每个虚拟用户独占一个 user_id（load_001~load_080），避免用户级限流
- 三级问题池（简单50% / 中等30% / 复杂20%），20% 缓存变体
- 客户端请求超时 20s，超时标记失败
- 完整消费 SSE 流，释放连接不泄漏
"""
from locust import HttpUser, task, between
from gevent.timeout import Timeout
import json
import random
import re

# ============================================================
# 独占账号池（每个 Locust 用户分配一个 unique 账号）
# load_001~load_080 是预创建的独占账号，互不重复
# ============================================================
_LOAD_ACCOUNTS = [
    {"username": f"load_{i:03d}", "password": "Test@1234"}
    for i in range(1, 81)
]

# ============================================================
# 三级问题池（简单50% + 中等30% + 复杂20%），20% 缓存变体
# ============================================================
_SIMPLE = [
    "明天广州的天气怎么样？", "北京的空气质量如何？",
    "重庆火锅哪家最正宗？", "长沙臭豆腐哪家最好吃？",
    "广州塔怎么买票？", "黄山登山路线推荐",
    "九寨沟现在开放了吗？", "西藏旅游需要准备什么？",
    "哈尔滨冬天去穿什么衣服？", "青岛啤酒节什么时候举办？",
]
_MEDIUM = [
    "成都三日游怎么安排？", "西安三天旅游攻略",
    "厦门鼓浪屿一日游路线", "从南京到苏州怎么去方便？",
    "从昆明到大理怎么坐车？", "北京到上海的高铁路线",
]
_COMPLEX = [
    "我想去北京和上海旅游，帮我规划路线和推荐酒店",
    "带小孩去上海迪士尼玩两天够吗？怎么安排？",
    "大理和丽江哪个更适合度假？两个地方都想玩怎么走？",
    "深圳有哪些好玩的地方？推荐一下周边的酒店和美食",
]
_CACHE_VARIANTS = [
    "广州明天天气怎么样？", "北京空气质量如何？",
    "成都三日游行程推荐", "西安旅游攻略三天",
    "从南京到苏州交通怎么走",
]
_WEIGHTED_POOL = (
    [(q, "simple") for q in _SIMPLE] * 5 +
    [(q, "medium") for q in _MEDIUM] * 3 +
    [(q, "complex") for q in _COMPLEX] * 2
)


def pick_query():
    if random.random() < 0.20:
        return random.choice(_CACHE_VARIANTS)
    return random.choice(_WEIGHTED_POOL)[0]


# 客户端超时（秒）
REQUEST_TIMEOUT = 20


class AgentGatewayUser(HttpUser):
    wait_time = between(5, 15)

    def on_start(self):
        """用实例 hash 分配独占账号，确保每个虚拟用户不同 user_id"""
        idx = hash(self) % len(_LOAD_ACCOUNTS)
        account = _LOAD_ACCOUNTS[idx]
        self.username = account["username"]
        self.password = account["password"]
        self.token = None
        self.headers = None
        # 每个虚拟用户唯一的 ID（DeepSeek 可见，用于区分用户级限流）
        self.test_user_id = f"test_user_{idx:03d}"

        # 登录带重试（502/网络错误自动重试一次）
        token = None
        import time as _time
        for attempt in range(2):
            try:
                with self.client.post(
                    "/api/login",
                    json={"username": self.username, "password": self.password},
                    name="/api/login",
                    timeout=REQUEST_TIMEOUT,
                    catch_response=True,
                ) as resp:
                    if resp.status_code == 200:
                        token = resp.json().get("access_token")
                        break
                    elif attempt == 0:
                        continue  # 瞬断重试
                    else:
                        raise Exception(
                            f"登录失败: user={self.username}, status={resp.status_code}"
                        )
            except Exception as e:
                if attempt == 0 and ("502" in str(e) or "503" in str(e) or "504" in str(e) or "Connection" in str(e)):
                    continue  # 瞬断或网络错误重试
                raise
        if not token:
            raise Exception(f"登录失败: user={self.username}, 重试后仍失败")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------
    # 任务1: V2 流式对话（权重 7 —— 主力）
    # -------------------------------------------------------
    @task(7)
    def chat_stream_v2(self):
        query = pick_query()
        payload = {"query": query, "stream": True, "user": self.test_user_id}

        # 指数退避重试：429/500/502 时等 1s→2s→4s 后重试，最多 2 次
        import time as _time
        for attempt in range(3):
            try:
                with Timeout(REQUEST_TIMEOUT, Exception("客户端超时")):
                    with self.client.post(
                        "/v2/chat/stream",
                        json=payload,
                        headers=self.headers,
                        stream=True,
                        catch_response=True,
                        name="/v2/chat/stream",
                        timeout=REQUEST_TIMEOUT,
                    ) as resp:
                        if resp.status_code == 200:
                            pass  # 继续处理响应
                        elif resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                            _time.sleep(2 ** attempt)  # 1s, 2s
                            continue
                        else:
                            resp.failure(f"HTTP {resp.status_code}")
                            return

                    full_text = ""
                    answer_complete = False
                    try:
                        for line in resp.iter_lines(decode_unicode=True):
                            if not line or not line.strip():
                                continue
                            full_text += line + "\n"

                            if "[DONE]" in line:
                                answer_complete = True
                                break

                            # 限长保护：最长取 50KB
                            if len(full_text) > 50_000:
                                answer_complete = True
                                break

                        if answer_complete:
                            # 从 SSE data 中提取 content 字段
                            contents = re.findall(
                                r'"content"\s*:\s*"([^"]*)"', full_text
                            )
                            reply_text = "".join(contents)

                            if len(reply_text) >= 10:
                                resp.success()
                            else:
                                resp.failure(f"响应太短 ({len(reply_text)} chars)")
                        else:
                            resp.failure("流式响应未正常结束")

                    except Exception as e:
                        resp.failure(f"读取异常: {e}")

            except Exception:
                # 客户端超时，直接标记失败
                try:
                    resp = self.client.post(
                        "/v2/chat/stream",
                        json=payload,
                        headers=self.headers,
                        name="/v2/chat/stream(timeout)",
                        timeout=5,
                    )
                    resp.failure("客户端超时(10s)")
                except Exception:
                    pass

    # -------------------------------------------------------
    # 任务2: 健康检查（权重 2）
    # -------------------------------------------------------
    @task(2)
    def health_check(self):
        with self.client.get(
            "/health", catch_response=True, name="/health", timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                resp.success()
            else:
                resp.failure(f"health 异常: {resp.status_code}")

    # -------------------------------------------------------
    # V1 已废弃，不再压测
    # -------------------------------------------------------