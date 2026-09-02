"""模拟 Locust 的 on_start 流程，测试登录"""
import httpx
import random
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

# admin / Fengfeng 密码从 .env 读取，测试账号为开发环境固定测试数据
ACCOUNTS = [
    {"username": "Fengfeng",    "password": get("FENGFENG_PASSWORD")},
    {"username": "admin",       "password": get("ADMIN_PASSWORD")},
    {"username": "test01",      "password": "Test@1234"},
    {"username": "test02",      "password": "Test@1234"},
    {"username": "test03",      "password": "Test@1234"},
    {"username": "demo",        "password": "Demo@2026"},
    {"username": "guest",       "password": "Guest@2026"},
]

HOST = "http://127.0.0.1:10092"

for i in range(20):
    account = random.choice(ACCOUNTS)
    try:
        resp = httpx.post(
            f"{HOST}/api/login",
            json={"username": account["username"], "password": account["password"]},
            timeout=10,
        )
        status = resp.status_code
        if status == 200:
            token = resp.json().get("access_token", "")[:20]
            print(f"[{i:02d}] {account['username']}: 200 OK token={token}...")
        else:
            print(f"[{i:02d}] {account['username']}: {status} {resp.text[:80]}")
    except Exception as e:
        print(f"[{i:02d}] {account['username']}: ERROR {e}")
