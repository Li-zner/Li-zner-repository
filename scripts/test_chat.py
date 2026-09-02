"""Test chat API flow"""
import httpx, json, sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

BASE = "http://localhost:10092"

# 1. Login（admin 密码从 .env 读取）
r = httpx.post(f"{BASE}/api/login", json={"username": get("ADMIN_USERNAME", "admin"), "password": get("ADMIN_PASSWORD")})
if r.status_code != 200:
    print(f"Login failed: {r.status_code} {r.text[:200]}")
    sys.exit(1)
token = r.json()["access_token"]
print(f"Login OK, token: {token[:20]}...")

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# 2. Create a chat task
r = httpx.post(f"{BASE}/v2/chat/tasks", 
    json={"query": "你好，用一句话介绍自己", "stream": False},
    headers=headers, timeout=30)
print(f"Chat task: {r.status_code}")
if r.status_code == 201:
    task = r.json()
    tid = task.get("task_id")
    print(f"  task_id: {tid}, status: {task.get('status')}")
    
    # 3. Poll for result
    import time
    for i in range(10):
        r2 = httpx.get(f"{BASE}/v2/chat/tasks/{tid}/result?wait=1", headers=headers, timeout=10)
        if r2.status_code == 200:
            result = r2.json()
            status = result.get("status")
            print(f"  Poll {i}: {status}")
            if status == "completed":
                content = result.get("content", "")
                print(f"  ✅ 回复: {content[:200]}")
                break
            elif status == "error":
                print(f"  ❌ 错误: {result.get('error', 'unknown')}")
                break
        else:
            print(f"  Poll {i}: HTTP {r2.status_code}")
        time.sleep(1)
    else:
        print("  ⏰ 超时")
else:
    print(f"  Error: {r.text[:500]}")
