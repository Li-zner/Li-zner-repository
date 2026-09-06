with open('/app/app/routes/v2.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: Replace old user_query pattern with simple loc_name prefix approach
old_query = """            user_query = req.query
            if req.user_location:
                loc_name = req.user_location.replace('市', '')
                if loc_name not in user_query:
                    intent_keywords = ['天气', '酒店', '路线', '美食', '餐厅', '怎么去', '到', '旅游', '玩']
                    if any(kw in user_query for kw in intent_keywords):
                        user_query = f"{user_query}（我在{req.user_location}）"
            messages.append({"role": "user", "content": user_query})"""

new_query = """            user_query = req.query
            if req.user_location:
                loc_name = req.user_location.replace('市', '')
                if loc_name not in user_query:
                    user_query = f"{loc_name}{req.query}"
            messages.append({"role": "user", "content": user_query})"""

content = content.replace(old_query, new_query)

with open('/app/app/routes/v2.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Applied!')
