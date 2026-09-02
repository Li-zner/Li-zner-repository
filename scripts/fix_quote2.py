#!/usr/bin/env python3
"""Fix the problematic Chinese quote characters in v2.py"""
with open('/app/app/routes/v2.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find and fix the problematic line (around line 318)
for i, line in enumerate(lines):
    # Check if this line has the issue: ASCII quotes around Chinese text
    if '优先设为' in line and '驾车' in line and 'mode' in line:
        print(f'Fixing line {i+1}')
        print(f'Before: {repr(line[:80])}')
        # Replace the entire problematic string to use single quotes
        lines[i] = "                '【重要】工具 query_route 返回的 mode 字段应优先设为「公交」，仅在用户明确要求驾车时才设为「驾车」。',\n"
        print(f'After: {repr(lines[i][:80])}')
        break

with open('/app/app/routes/v2.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('Done')
