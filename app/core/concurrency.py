"""全局并发控制

LLM 调用限流：每个实例最多同时进行 LLM_MAX_CONCURRENCY 个流式请求。
防止高并发下所有请求同时解析 LLM 返回的大 JSON，导致 CPU 飙高、P99 延迟失控。

用法（在 LLM 调用处）：
    from ..core.concurrency import llm_semaphore
    async with llm_semaphore:
        async with httpx.AsyncClient(...) as client:
            ...
"""
import asyncio
import os

# 每实例最大并发 LLM 调用数（默认 200）
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "200"))

# 校验：负数会导致 Semaphore 创建失败且报错信息不明确（P1）
if LLM_MAX_CONCURRENCY < 1:
    raise ValueError(f"LLM_MAX_CONCURRENCY 必须 >= 1，当前为 {LLM_MAX_CONCURRENCY}")

# Python 3.10+ 允许在无运行循环时创建 Semaphore（惰性绑定）
llm_semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)
