import json
import re
import os
import httpx
from ..core.logging import setup_logging
from ..core.config import DEEPSEEK_API_BASE, DEEPSEEK_MODEL, HTTP_TIMEOUT_MEDIUM

logger = setup_logging()


def _extract_json(text: str) -> dict:
    """
    从 LLM 回复中提取 JSON。
    处理以下情况：
    1. 纯 JSON
    2. 包裹在 ```json ... ``` markdown 代码块中
    3. 包裹在 ``` ... ``` 代码块中
    4. 前后有额外文字
    """
    if not text:
        raise json.JSONDecodeError("空响应", text, 0)

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 markdown 代码块
    block_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if block_match:
        try:
            return json.loads(block_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 尝试提取第一个 { } 包裹的内容
    brace_match = re.search(r'\{.*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    # 最后尝试：修复常见问题（未引用的 key）
    fixed = re.sub(r'(?<!")(\b\w+\b)(?=\s*:)', r'"\1"', text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        raise



SUB_AGENT_PROMPTS = {
    "query_hotel": '''你是酒店推荐专家。根据用户需求调用酒店API，只返回JSON。

【输出格式】
{"hotels": [{"name": "名称", "price": 价格, "rating": 评分, "address": "地址", "comment": "对酒店位置/服务的简短评价"}]}
【约束】只输出JSON，不输出任何解释性文字。''',

    "query_route": '''你是路线规划专家。根据出发地和目的地规划路线，只返回JSON。

【核心规则】
- 根据两地距离**智能选择**交通方式：
  - 如果在同一城市内（<30km）：公交/地铁/打车
  - 如果跨城市但距离较近（30-200km）：高铁/动车/大巴，优先推荐高铁
  - 如果跨城市且距离较远（>200km）：高铁/飞机，优先推荐高铁
  - 仅当用户明确说「驾车/开车/自驾」时才用驾车
- **禁止**使用市内公交（如"坐公交"）跨城市长途出行
- 如果用户没指定出发地，默认从用户当前位置出发

【输出格式】
{"route": {"distance_km": 数字, "duration_min": 数字, "mode": "高铁"}}
- mode 字段：高铁/动车/公交/驾车/飞机/大巴，根据距离智能选择
- distance_km：路线距离（公里），客观估算
- duration_min：预计耗时（分钟），客观估算
【约束】只输出JSON，不输出任何解释性文字。''',

    "query_food": '''你是美食推荐专家。根据目的地调用餐厅API，只返回JSON。

【输出格式】
{"restaurants": [{"name": "名称", "cuisine": "菜系", "avg_price": 价格, "rating": 评分, "feature": "特色推荐"}]}
【约束】只输出JSON，不输出任何解释性文字。''',
}

async def call_sub_agent(agent_name: str, args: dict, user_query: str, retry: bool = True):
    """调用子Agent，如果返回非法JSON则自动重试一次"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"error": "Missing DeepSeek Key"}

    system_prompt = SUB_AGENT_PROMPTS.get(agent_name, "")
    if not system_prompt:
        return {"error": f"Unknown agent: {agent_name}"}
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户问题：{user_query}\n参数：{json.dumps(args)}"}
    ]
    
    async def _call():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1
                }
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # ---- Token 精细计量 ----
            from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
            usage = data.get("usage", {})
            prompt_tk = usage.get('prompt_tokens', 0) or 0
            completion_tk = usage.get('completion_tokens', 0) or 0
            llm_tokens_total.labels(type='input').inc(prompt_tk)
            llm_tokens_total.labels(type='output').inc(completion_tk)
            llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='sub_agent', type='input').inc(prompt_tk)
            llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='sub_agent', type='output').inc(completion_tk)
            llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='sub_agent', status='success').inc()

            return _extract_json(content)
    
    try:
        return await _call()
    except (json.JSONDecodeError, Exception) as e:
        if retry:
            logger.warning(f"子Agent {agent_name} 首次返回非法JSON，自动重试")
            messages[0]["content"] = system_prompt + "\n【重要】只输出纯JSON，不要添加任何解释。"
            try:
                return await _call()
            except Exception as e2:
                logger.error(f"子Agent {agent_name} 重试仍失败: {e2}")
                return {"error": f"重试失败: {str(e2)}"}
        return {"error": f"JSON解析失败: {str(e)}"}