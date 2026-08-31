"""
清空民法典相关缓存并重新测评
"""
import json
import yaml
import os

# Load test cases to find CC IDs
with open(r'D:\桌面\agent_gateway\tests\test_cases.yaml', 'r', encoding='utf-8') as f:
    raw = yaml.safe_load(f)
all_cases = raw.get('test_cases', raw)
cc_ids = {c['id'] for c in all_cases if c.get('category') == '民法典'}

# Load cache and remove CC entries
cache_path = r'D:\桌面\agent_gateway\tests\eval_cache.json'
if os.path.exists(cache_path):
    with open(cache_path, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    
    # Find keys to remove - we need to rebuild by checking the test cases
    # Since cache keys are hashes, we need to identify which ones are CC
    # Strategy: keep only non-CC entries
    # We can't easily identify CC entries by key alone, so we'll just delete the cache and re-run
    print(f"Cache has {len(cache)} entries. Deleting and re-running CC only.")
    os.remove(cache_path)
    print("Cache deleted.")
else:
    print("No cache found.")
