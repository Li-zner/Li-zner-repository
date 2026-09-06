"""测试轮询函数"""
import os
import random


def _get_deepseek_key():
    keys = [
        os.getenv("DEEPSEEK_API_KEY", ""),
        os.getenv("DEEPSEEK_API_KEY_2", ""),
        os.getenv("DEEPSEEK_API_KEY_3", ""),
    ]
    valid = [k for k in keys if k]
    return random.choice(valid) if valid else ""


for i in range(20):
    k = _get_deepseek_key()
    which = "KEY1" if k == os.getenv("DEEPSEEK_API_KEY") else "KEY2"
    print(f"{i}: ...{k[-4:]} ({which})")
