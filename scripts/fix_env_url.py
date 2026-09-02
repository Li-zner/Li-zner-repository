#!/usr/bin/env python3
"""Fix DATABASE_URL password encoding in /etc/agent_gateway/.env"""
import os, sys
from urllib.parse import quote_plus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

# 从根目录 .env 读取原始密码，动态完成 URL 编码修复（不硬编码密钥）
raw_pwd = get("POSTGRES_PASSWORD")
enc_pwd = quote_plus(raw_pwd)

with open('/etc/agent_gateway/.env', 'r') as f:
    content = f.read()

content = content.replace(raw_pwd, enc_pwd)

with open('/etc/agent_gateway/.env', 'w') as f:
    f.write(content)

print("DATABASE_URL fixed")
