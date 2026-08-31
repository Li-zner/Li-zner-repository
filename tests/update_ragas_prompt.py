"""更新RAGAS评估提示词 - 处理非民法典CC用例"""
import sys
sys.path.insert(0, r'D:\桌面\agent_gateway')

path = r'D:\桌面\agent_gateway\tests\ragas\ragas_evaluation.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix answer_relevancy prompt - add domain awareness hint
old = '### 通用评分标准（0-1 分）'
new = '''### ⚠️ 特别提示（法律领域评估）
对于编号以 CC 开头的案例（民法典评估集），助手可能正确指出问题**不属于民法典调整范围**，而是属于其他法律领域。如果助手正确识别了法律领域并给出引导，这是正确行为，不应扣分。不要因为助手没有给出民法典条文而扣分。

### 通用评分标准（0-1 分）'''

content = content.replace(old, new, 1)

# Also fix the answer_correctness prompt
old2 = '### 通用评分标准（0-1 分）'
# Only replace the SECOND occurrence (in correctness prompt)
parts = content.split(old2)
if len(parts) >= 3:
    # Rejoin first two parts, then replace second occurrence
    content = parts[0] + old2 + parts[1] + new + ''.join(parts[2:])

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('Updated RAGAS prompts')
print(f'Added domain awareness hint to answer_relevancy and answer_correctness')
