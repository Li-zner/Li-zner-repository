"""应用入口骨架 — 复制后按项目填充
功能：lifespan（初始化连接池/迁移/缓存预热）+ 路由注册 + /health
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import config

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：初始化 DB/Redis 连接池（fail-fast），注册后关闭"""
    # TODO(owner): 初始化数据库连接池、Redis、缓存预热
    logger.info("应用启动: env=%s", config.ENV)
    yield
    # TODO(owner): 关闭连接池
    logger.info("应用关闭")


app = FastAPI(title=config.APP_NAME, version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    """存活探针：供 Nginx/容器健康检查使用，无鉴权"""
    return {"status": "ok"}


# TODO(owner): 注册业务路由
# from app.routes import v1
# app.include_router(v1.router, prefix="/api/v1")
