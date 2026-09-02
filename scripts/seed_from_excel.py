import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

import pandas as pd
from app.core.semantic_cache import SemanticCache
from app.core.logging import setup_logging

logger = setup_logging()

EXCEL_PATH = project_root / "tests" / "evaluation_report.xlsx"
SCORE_THRESHOLD = 9.5

async def warmup_from_excel():
    if not EXCEL_PATH.exists():
        logger.error(f"❌ 未找到测评文件: {EXCEL_PATH}")
        return

    logger.info(f"📂 正在读取人工测评报告: {EXCEL_PATH}")
    df = pd.read_excel(EXCEL_PATH, sheet_name=0, engine='openpyxl')
    
    # 转换评分列为数值，无法转换的变成 NaN
    df['评分(10分制)'] = pd.to_numeric(df['评分(10分制)'], errors='coerce')
    
    # 过滤：评分 >= 阈值 且 回复不为空
    df_filtered = df[
        (df['评分(10分制)'] >= SCORE_THRESHOLD) & 
        (df['Agent完整回复'].notna()) & 
        (df['Agent完整回复'].astype(str).str.strip() != '')
    ]
    
    df_filtered = df_filtered.sort_values(by='评分(10分制)', ascending=False)
    total = len(df_filtered)
    logger.info(f"✅ 筛选出 {total} 条高质量人工标注问答 (评分 >= {SCORE_THRESHOLD})")
    if total == 0:
        return

    success_count = 0
    for _, row in df_filtered.iterrows():
        query = row['用户问题']
        response = row['Agent完整回复']
        if not query or not response or not isinstance(response, str):
            continue
        try:
            await SemanticCache.set(query, response)
            success_count += 1
            logger.info(f"✅ 预热成功 [{success_count}/{total}]: {query[:20]}...")
        except Exception as e:
            logger.error(f"❌ 预热失败: {query[:20]}..., 错误: {e}")
    
    logger.info(f"🎉 语义缓存预热完成！共写入 {success_count} 条记录。")

if __name__ == "__main__":
    asyncio.run(warmup_from_excel())