"""FastAPI 应用入口：CORS、路由注册、启动初始化（建表 + 默认数据 + 调度器）。"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import (audit, auth, cache, chat, datasources, dicts, doc_chat_api,
                  eval as eval_api, knowledge, lineage, models_config, org,
                  permissions, scenes, scheduled_tasks)
from .config import get_settings
from .database import init_db
from .engine.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(title="AI 问数系统 API", version="1.0.0",
              description="NL2SQL + RAG + 字段级权限 + 定时任务的问数服务")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(datasources.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)
app.include_router(models_config.router, prefix=API_PREFIX)
app.include_router(chat.router, prefix=API_PREFIX)
app.include_router(doc_chat_api.router, prefix=API_PREFIX)
app.include_router(scheduled_tasks.router, prefix=API_PREFIX)
app.include_router(permissions.router, prefix=API_PREFIX)
app.include_router(dicts.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)
app.include_router(org.router, prefix=API_PREFIX)
# 能力补建路由（C1/C4/C8/C14）
app.include_router(cache.router, prefix=API_PREFIX)
app.include_router(eval_api.router, prefix=API_PREFIX)
app.include_router(lineage.router, prefix=API_PREFIX)
app.include_router(scenes.router, prefix=API_PREFIX)


@app.on_event("startup")
def on_startup():
    init_db()
    from .database import SessionLocal
    db = SessionLocal()
    try:
        auth.seed_default_user(db)
        org.seed_default_menus(db)
        # C14 预置六大场景模板（幂等）
        from .engine.scene_router import seed_scenes
        seed_scenes(db)
    finally:
        db.close()
    start_scheduler()
    logger.info("AI 问数系统后端已启动")


@app.on_event("shutdown")
def on_shutdown():
    """优雅停止：先停定时任务调度线程，再退出进程。"""
    stop_scheduler()
    logger.info("AI 问数系统后端已停止")


@app.get("/health")
def health():
    from .database import engine
    return {"status": "ok", "meta_db": engine.url.drivername}
