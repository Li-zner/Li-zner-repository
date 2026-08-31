"""DeepSeek direct test for CC019"""
import json, httpx, sys, os
sys.path.insert(0, '/app')
os.environ.setdefault('DEEPSEEK_API_KEY', '')

from app.core.constants import clear_persona_cache, get_persona_prompt
from app.core.config import DEEPSEEK_API_BASE, DEEPSEEK_MODEL
clear_persona_cache()
data = get_persona_prompt("civil_code")
sp = data["prompt"].format(today="2026年7月22日", name="民法典助手")
print("Sys prompt len:", len(sp))

query = "【提示：此问题受《消费者权益保护法》调整，不属于民法典。请引用《消费者权益保护法》回答，不要引用民法典。】\n网购商品七天无理由退货的法律依据是什么？哪些商品不适用？"

api_key = open("/app/.env").read().split("DEEPSEEK_API_KEY=")[1].split("\n")[0].strip()
messages = [{"role": "system", "content": sp}, {"role": "user", "content": query}]
with httpx.Client(timeout=30) as client:
    resp = client.post(f"{DEEPSEEK_API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "messages": messages, "temperature": 0.3, "max_tokens": 2048})
    c = resp.json()["choices"][0]["message"]["content"]
    print("=" * 60)
    print("DeepSeek Response:")
    print(c[:500])
    print("=" * 60)
    print("Has consumer:", "消费者权益保护法" in c)
    print("Has civil code:", "民法典" in c)
    if "消费者权益保护法" in c and "民法典" not in c[:100]:
        print("GOOD: Correctly identifies Consumer Protection Law")
    else:
        print("BAD: Still attributing to Civil Code")
