"""Embedding 模型对比：本地 (dmeta-embedding-zh, 768维)"""
import os, sys, time, numpy as np, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

same_pairs = [
    ("离婚冷静期是多久", "民法典第1077条规定的离婚冷静期为三十日"),
    ("高利贷合法吗", "民法典第680条禁止高利放贷"),
    ("偷拍算侵权吗", "民法典第1032条保护隐私权"),
    ("未成年人打赏能退吗", "限制民事行为能力人实施的民事法律行为需监护人同意"),
    ("彩礼能退吗", "民法典婚姻家庭编关于彩礼返还的规定"),
    ("被狗咬伤找谁赔", "民法典第1245条饲养动物损害责任"),
    ("遗嘱怎么写有效", "民法典第1134条自书遗嘱需亲笔签名注明年月日"),
    ("夫妻共同债务怎么认定", "民法典第1064条夫妻共同债务认定规则"),
    ("定金和订金区别", "民法典第586条定金罚则"),
    ("遗产继承顺序", "民法典第1127条法定继承顺序"),
]
diff_pairs = [
    ("离婚冷静期是多久", "今天天气真好"),
    ("高利贷合法吗", "附近有什么好吃的餐厅"),
    ("偷拍算侵权吗", "明天会下雨吗"),
    ("未成年人打赏能退吗", "这件衣服多少钱"),
    ("彩礼能退吗", "怎么去火车站"),
    ("被狗咬伤找谁赔", "帮我订一间大床房"),
    ("遗嘱怎么写有效", "明天上午开会"),
    ("夫妻共同债务怎么认定", "这首歌叫什么名字"),
    ("定金和订金区别", "北京到上海的高铁"),
    ("遗产继承顺序", "今天星期几"),
]

def embed(model, texts):
    if isinstance(texts, str):
        texts = [texts]
    embs = []
    for t in texts:
        r = requests.post("http://localhost:11434/api/embeddings",
            json={"model": model, "prompt": t}, timeout=30)
        r.raise_for_status()
        embs.append(r.json()["embedding"])
    return np.array(embs)

def cos_sim(a, b):
    an = a / np.linalg.norm(a, axis=1, keepdims=True)
    bn = b / np.linalg.norm(b, axis=1, keepdims=True)
    return (an * bn).sum(axis=1)

print("=" * 60)
print("本地模型: shaw/dmeta-embedding-zh (768维)")
print("=" * 60)

# 测试1
print("\n【测试1】句对相似度")
for name, pairs, expect_high in [
    ("同类句对（应>0.5）", same_pairs, True),
    ("异类句对（应<0.5）", diff_pairs, False),
]:
    t0 = time.time()
    ea = embed("shaw/dmeta-embedding-zh", [p[0] for p in pairs])
    eb = embed("shaw/dmeta-embedding-zh", [p[1] for p in pairs])
    elapsed = time.time() - t0
    sims = cos_sim(ea, eb)
    ok = sum(1 for s in sims if (s > 0.5) == expect_high)
    print(f"\n  {name} (耗时{elapsed:.1f}s):")
    for i, s in enumerate(sims):
        print(f"  #{i+1}: {s:.4f}")
    print(f"  均值={sims.mean():.4f} 准确率={ok/len(pairs)*100:.0f}%")

# 测试2: API对比（如果可用）
print("\n【测试2】API模型对比")
api_key = get("DEEPSEEK_API_KEY")
try:
    import httpx
    from sklearn.metrics.pairwise import cosine_similarity as sk_cos
    texts = [p[0] for p in same_pairs] + [p[1] for p in same_pairs]
    resp = httpx.post("https://api.deepseek.com/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "text-embedding-3-small", "input": texts}, timeout=30)
    resp.raise_for_status()
    api_embs = np.array([d["embedding"] for d in resp.json()["data"]])
    # 对比同类句对的相似度排序一致性
    local_embs = embed("shaw/dmeta-embedding-zh", texts)
    api_same = sk_cos(api_embs[:10], api_embs[10:]).diagonal()
    local_same = cos_sim(local_embs[:10], local_embs[10:])
    from scipy.stats import spearmanr
    corr, p = spearmanr(api_same, local_same)
    print(f"\n  API模型(text-embedding-3-small) 维度: {api_embs.shape[1]}")
    print(f"  Spearman相关: {corr:.4f} (p={p:.6f})")
    print(f"  {'✅ 强相关，可替换' if corr > 0.7 else '⚠️ 弱相关，需谨慎'}")
except Exception as e:
    print(f"\n  API对比跳过: {e}")
    print(f"  （不影响本地模型评估）")

print("\n" + "=" * 60)
print("结论")
print("-" * 60)
print("模型: shaw/dmeta-embedding-zh")
print("维度: 768")
print("大小: 408MB")
print("本地推理：无需网络，无API费用，零延迟")
print("=" * 60)
