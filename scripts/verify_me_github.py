# -*- coding: utf-8 -*-
"""验证求职助手 /v2/chat/me 已加载新 me.json（含 GitHub 个人主页）。"""
import json
import requests

BASE = "http://localhost:10092"

# 1) 登录
r = requests.post(f"{BASE}/api/login", json={"username": "admin", "password": "Admin@2026"}, timeout=10)
print("login:", r.status_code)
data = r.json()
token = data.get("access_token") or data.get("token") or data.get("data", {}).get("access_token")
if not token:
    print("登录失败:", data)
    raise SystemExit(1)
print("token ok")

# 2) 调 /v2/chat/me 问 GitHub
headers = {"Authorization": f"Bearer {token}"}
body = {"query": "你的公网演示网站是什么？稳定吗？", "persona_id": "me"}
resp = requests.post(f"{BASE}/v2/chat/me", json=body, headers=headers, stream=True, timeout=60)
print("chat status:", resp.status_code)

text = ""
for line in resp.iter_lines(decode_unicode=True):
    if not line:
        continue
    line = line.strip()
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        # 兼容多种字段
        piece = obj.get("delta") or obj.get("content") or obj.get("text") or obj.get("answer") or ""
        if isinstance(piece, str):
            text += piece

print("---- 回答 ----")
print(text[:600])
print("---- 校验 ----")
print("包含 github.com/Li-zner :", "github.com/Li-zner" in text)
