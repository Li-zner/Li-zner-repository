#!/usr/bin/env python3
"""Fix user query injection to be more direct"""
with open('/app/app/routes/v2.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''            # 如果用户有定位城市且消息中没提城市，自动将定位注入到查询中
            user_query = req.query
            if req.user_location:
                loc_name = req.user_location.replace("市", "")
                if loc_name not in user_query:
                    intent_keywords = ["天气", "酒店", "路线", "美食", "餐厅", "怎么去", "到", "旅游", "玩"]
                    if any(kw in user_query for kw in intent_keywords):
                        user_query = f"{user_query}（我在{req.user_location}）"
            messages.append({"role": "user", "content": user_query})'''

new = '''            # 如果用户有定位城市且消息中没提城市，自动将城市名注入查询
            user_query = req.query
            if req.user_location and req.user_location not in user_query and req.user_location.replace("市","") not in user_query:
                # 将定位城市名嵌入查询，让模型直接看到城市信息
                user_query = f"{req.user_location}{req.query}"
            messages.append({"role": "user", "content": user_query})'''

content = content.replace(old, new)

with open('/app/app/routes/v2.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Query injection fixed')
