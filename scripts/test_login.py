"""测试登录"""
import asyncio
import httpx
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

async def main():
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "http://localhost:10092/api/login",
            json={"username": "admin", "password": get("ADMIN_PASSWORD")}
        )
        print(f"Status: {r.status_code}")
        print(f"Response: {r.text[:200]}")

        if r.status_code == 200:
            token = r.json()["access_token"]
            print(f"Token: {token[:30]}...")

            # Test a simple query
            r2 = await client.post(
                "http://localhost:10092/v2/chat/tasks",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"conversation_id": "test", "message": "北京天气怎么样", "user_location": "北京"}
            )
            print(f"Task create: {r2.status_code}")
            print(f"Response: {r2.text[:200]}")

asyncio.run(main())
