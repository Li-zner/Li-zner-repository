"""测试手机号完整流程"""
import httpx, asyncio

API = "http://localhost:10092"

async def main():
    phone = "13800003333"
    
    # 1. 发送验证码
    r = await httpx.AsyncClient().post(f"{API}/api/phone/send-code",
        json={"phone": phone})
    print(f"1. send-code: {r.status_code}")
    d = r.json()
    print(f"   response: {d}")
    code = d.get("debug_code", "")
    
    # 2. 注册
    r2 = await httpx.AsyncClient().post(f"{API}/api/phone/register",
        json={"phone": phone, "code": code, "password": "Test1234", "agree": True})
    print(f"2. register: {r2.status_code}")
    print(f"   response: {r2.text[:300]}")
    
    # 3. 再次发验证码
    r3 = await httpx.AsyncClient().post(f"{API}/api/phone/send-code",
        json={"phone": phone})
    print(f"3. send-code: {r3.status_code}")
    code2 = r3.json().get("debug_code", "")
    
    # 4. 验证码登录
    r4 = await httpx.AsyncClient().post(f"{API}/api/phone/login",
        json={"phone": phone, "code": code2})
    print(f"4. phone-login: {r4.status_code}")
    print(f"   response: {r4.text[:300]}")
    
    if r4.status_code == 200:
        token = r4.json().get("access_token", "")
        # 5. 用token访问profile
        r5 = await httpx.AsyncClient().get(f"{API}/api/user/profile",
            headers={"Authorization": f"Bearer {token}"})
        print(f"5. profile: {r5.status_code}")
        print(f"   response: {r5.text[:300]}")
    
    # 6. 测试真实手机发验证码
    print("\n--- 测试真实手机 ---")
    r6 = await httpx.AsyncClient().post(f"{API}/api/phone/send-code",
        json={"phone": "13357308241"})
    print(f"6. real-phone send-code: {r6.status_code}")
    print(f"   response: {r6.json()}")

asyncio.run(main())
