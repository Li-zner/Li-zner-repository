#!/usr/bin/env python3
"""Apply all fixes to container's v2.py"""
import re

with open('/app/app/routes/v2.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add import re
if 'import re' not in content.split('\n', 1)[1][:10]:
    content = content.replace(
        'from ..core.semantic_cache import SemanticCache',
        'from ..core.semantic_cache import SemanticCache\nimport re'
    )

# 2. Add _clean_reasoning function
if '_clean_reasoning' not in content:
    filter_code = r"""
# ---------- 推理内容过滤器：屏蔽内部提示词泄漏 ----------
_INTERNAL_PATTERNS = [
    r'工具定义.*?(?:query_weather|query_hotel|query_route|query_food)',
    r'(?:query_weather|query_hotel|query_route|query_food).*?(?:工具|函数|tool)',
    r'(?:parameters|type|description|required).*?(?:object|string)',
    r'你是旅行规划总控助手',
    r'判断用户意图.*?天气.*?酒店.*?路线.*?美食',
    r'根据意图调用对应的工具',
    r'如果用户问题涉及多个方面',
    r'收到工具返回的JSON数据后',
    r'你必须记住用户之前提到的',
    r'如果工具返回的JSON中包含',
    r'语气稍微热情即可',
    r'思考.*?过程请使用中文进行推理',
    r'保留专业术语的英文原名',
    r'当用户询问出行路线时',
    r'除非用户明确说.*?驾车.*?开车.*?自驾',
    r'默认使用公共交通',
    r'工具 query_route 返回的 mode',
    r'当用户说.*?我在XX.*?或告知位置',
    r'这仅用于确定出发地和天气查询',
    r'不要擅自将此位置用于酒店推荐',
    r'如果用户问天气但没有说城市',
    r'用户告知位置后，不要反问用户位置',
    r'role.*?tool.*?tool_call_id',
    r'## 基本信息',
    r'## 角色定位',
    r'## 交通出行规则',
    r'## 定位与位置处理规则',
    r'【用户当前位置】',
    r'【用户画像】',
    r'用户上传了以下文件',
    r'【用户上传文件:',
    r'【文件结束】',
]

def _clean_reasoning(text: str) -> str:
    if not text:
        return text
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        skip = False
        for pat in _INTERNAL_PATTERNS:
            if re.search(pat, stripped, re.IGNORECASE):
                skip = True
                break
        if skip:
            continue
        if re.match(r'^\s*[{}\[\],]\s*$', stripped):
            continue
        if re.match(r'^\s*"[^"]*"\s*:\s*\{', stripped):
            continue
        if re.match(r'^\s*"[^"]*"\s*:\s*\[', stripped):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)

"""
    content = content.replace('logger = setup_logging()\n', filter_code + '\nlogger = setup_logging()\n')

# 3. Replace system prompt construction
old_sys = '''            # 构建 System Prompt（含画像注入）
            system_parts = [
                "你是旅行规划总控助手。你的职责是：",
                "1. 判断用户意图（天气/酒店/路线/美食）。",
                "2. 根据意图调用对应的工具（query_weather/query_hotel/query_route/query_food）。",
                "3. 如果用户问题涉及多个方面（如'北京三日游'），可以同时调用多个工具。",
                "4. 收到工具返回的JSON数据后，整理成自然语言回复用户。",
                "5. **重要：你必须记住用户之前提到的目的地、人数等上下文信息。**",
                "6. **重要：如果工具返回的JSON中包含 'error' 或 'success': false，请根据 'fallback_message' 给用户友好回应，不要让用户看到技术错误详情。**",
                "7. 语气稍微热情即可，不必生成表情和颜文字。",
                "8. **重要：思考（reasoning）过程请使用中文进行推理，保留专业术语的英文原名。**"
            ]
            if user_profile:
                system_parts.insert(1, user_profile)
            system_content = "\\n".join(system_parts)'''

new_sys = '''            # 构建 System Prompt（含日期、画像、用户位置）
            today_str = datetime.now().strftime("%Y年%m月%d日 %A")
            system_parts = [
                f"## 基本信息",
                f"今天是 {today_str}。你应该用这个日期回答任何关于日期/时间的问题，以及查询天气时的当日参考。",
                "",
                "## 角色定位",
                "你是旅行规划总控助手。你的职责是：",
                "1. 判断用户意图（天气/酒店/路线/美食/通用问答）。",
                "2. 根据意图调用对应的工具（query_weather/query_hotel/query_route/query_food）。",
                "3. 如果用户问题涉及多个方面（如'北京三日游'），可以同时调用多个工具。",
                "4. 收到工具返回的JSON数据后，整理成自然语言回复用户。",
                "5. **重要：你必须记住用户之前提到的目的地、人数等上下文信息。**",
                "6. **重要：如果工具返回的JSON中包含 'error' 或 'success': false，请根据 'fallback_message' 给用户友好回应，不要让用户看到技术错误详情。**",
                "7. 语气稍微热情即可，不必生成表情和颜文字。",
                "8. **重要：思考（reasoning）过程请使用中文进行推理，保留专业术语的英文原名。**",
                "",
                "## 交通出行规则",
                "【重要】当用户询问出行路线时，除非用户明确说「驾车/开车/自驾」，否则**默认使用公共交通**（公交/地铁/高铁）规划路线。不要默认推荐驾车。",
                "【重要】工具 query_route 返回的 mode 字段应优先设为"公交"，仅在用户明确要求驾车时才设为"驾车"。",
                "",
                "## 定位与位置处理规则",
                "【重要】当用户说「我在XX」或告知位置时，这仅用于确定出发地和天气查询，不要擅自将此位置用于酒店推荐或驾车路线规划，除非用户明确要求。",
                "【重要】如果用户问天气但没有说城市，优先使用用户的定位城市。如果定位不可用，则主动询问用户所在城市。",
                "【重要】用户告知位置后，不要反问用户位置，直接使用用户提供的信息。"
            ]
            if user_profile:
                system_parts.insert(1, user_profile)
            # 注入用户定位城市（如果前端提供了），直接紧跟在日期后面
            if req.user_location:
                system_parts[1] = f"今天是 {today_str}。用户当前所在城市：{req.user_location}。如果用户问天气但没有说城市，直接使用该城市查询天气，不要再反问用户。你应该用这个日期回答任何关于日期/时间的问题，以及查询天气时的当日参考。"
            system_content = "\\n".join(system_parts)'''

content = content.replace(old_sys, new_sys)

# 4. Replace reasoning streaming - buffer instead of immediate send
old_reasoning_yield = '''                                        if delta.get("reasoning_content"):
                                            chunk = delta["reasoning_content"]
                                            full_reasoning += chunk
                                            yield f"data: {json.dumps({'type': 'reasoning', 'content': chunk})}\\n\\n"
                                        '''
new_reasoning_yield = '''                                        if delta.get("reasoning_content"):
                                            chunk = delta["reasoning_content"]
                                            full_reasoning += chunk
                                            # 不再逐块流式发送原始推理内容，避免内部提示词泄漏
                                        '''

content = content.replace(old_reasoning_yield, new_reasoning_yield)

# 5. Replace reasoning_done to use _clean_reasoning
old_done = '''                    # 流式响应结束，发送完整的思考内容（仅中文，已在 system prompt 要求用中文推理）
                    if full_reasoning:
                        yield f"data: {json.dumps({'type': 'reasoning_done', 'content': full_reasoning})}\\n\\n"
                        await asyncio.sleep(0)'''

new_done = '''                    # 流式响应结束，发送过滤后的思考内容（移除内部提示词泄漏）
                    if full_reasoning:
                        clean_reasoning = _clean_reasoning(full_reasoning)
                        # 分段流式发送，让前端逐步展示
                        chunk_size_r = 80
                        for i in range(0, len(clean_reasoning), chunk_size_r):
                            chunk = clean_reasoning[i:i+chunk_size_r]
                            yield f"data: {json.dumps({'type': 'reasoning', 'content': chunk})}\\n\\n"
                            await asyncio.sleep(0.015)
                        yield f"data: {json.dumps({'type': 'reasoning_done', 'content': clean_reasoning})}\\n\\n"
                        await asyncio.sleep(0)'''

content = content.replace(old_done, new_done)

# 6. Replace user query injection
old_query = '''            for msg in history_dicts:
                messages.append(msg)
            messages.append({"role": "user", "content": req.query})'''

new_query = '''            for msg in history_dicts:
                messages.append(msg)
            # 如果用户有定位城市且消息中没提城市，自动将定位注入到查询中
            user_query = req.query
            if req.user_location:
                loc_name = req.user_location.replace("市", "")
                if loc_name not in user_query:
                    intent_keywords = ["天气", "酒店", "路线", "美食", "餐厅", "怎么去", "到", "旅游", "玩"]
                    if any(kw in user_query for kw in intent_keywords):
                        user_query = f"{user_query}（我在{req.user_location}）"
            messages.append({"role": "user", "content": user_query})'''

content = content.replace(old_query, new_query)

with open('/app/app/routes/v2.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('All fixes applied successfully')
print(f'Total lines: {len(content.splitlines())}')
