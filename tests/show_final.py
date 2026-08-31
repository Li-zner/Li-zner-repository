"""查看最新修复结果"""
import json, openpyxl

with open(r'D:\桌面\agent_gateway\tests\eval_cache.json', encoding='utf-8') as f:
    c = json.load(f)
print(f'Cache: {len(c)}/60')

wb = openpyxl.load_workbook(r'D:\桌面\agent_gateway\tests\ragas\civil_evaluation_report.xlsx')
ws = wb.active
header = [cell.value for cell in ws[1]]
id_col = 0
sc = header.index('评分(10分制)')
rc = header.index('Agent完整回复')
ac = header.index('扣分说明与综合评价')

rows = list(ws.iter_rows(min_row=2, values_only=True))
scores = [r[sc] for r in rows]
print(f'Avg: {sum(scores)/len(scores):.2f}/10')
print(f'High>=8: {sum(1 for s in scores if s>=8)} Mid: {sum(1 for s in scores if 6<=s<8)} Low<6: {sum(1 for s in scores if s<6)}')

targets = ['CC001','CC032','CC035','CC057','CC058','CC060','CC019']
print('\n===焦点用例（缓存全清后首次真实调用）===')
for r in rows:
    if r[id_col] in targets:
        resp = str(r[rc])
        print(f'\n{r[id_col]}: {r[sc]}/10 len={len(resp)}')
        print(f'  resp: {resp[:200]}')
        print(f'  analysis: {str(r[ac])[:150]}')
