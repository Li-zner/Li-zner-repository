#!/usr/bin/env python3
with open('/app/app/routes/v2.py', 'r', encoding='utf-8') as f:
    content = f.read()

# The problematic line has ASCII double quotes inside a double-quoted Python string
# Replace with 「」 brackets instead
old = (
    '                "【重要】工具 query_route 返回的 mode 字段应优先设为'
    '\u201c公交\u201d'
    '，仅在用户明确要求驾车时才设为'
    '\u201c驾车\u201d'
    '。",'
)
new = (
    "                '【重要】工具 query_route 返回的 mode 字段应优先设为「公交」，"
    "仅在用户明确要求驾车时才设为「驾车」。',"
)
content = content.replace(old, new)

with open('/app/app/routes/v2.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Quote fix applied')
