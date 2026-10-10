"""FastAPI application entry point.

    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000

Serves the JSON API under ``/api``, WebSocket streams under ``/ws``, and — when
``frontend/dist`` exists — the built React SPA at ``/``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router, ws_router
from .config import REPO_ROOT, settings
from .services import (
    dataset_service,
    deployment_service,
    export_service,
    model_api_service,
    project_service,
    review_service,
    user_service,
)
from .services.monitor import monitor
from .services.scheduler import scheduler

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("prt")

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    admin = user_service.ensure_bootstrap_admin()
    if admin is not None:
        logger.warning(
            "created bootstrap administrator %s (password from PRT_BOOTSTRAP_ADMIN_PASSWORD) — change it now",
            admin.email,
        )
    seeded = model_api_service.seed_defaults()
    if seeded:
        logger.info("seeded %d default model API config(s); admins fill in the keys", seeded)
    adopted = project_service.adopt_orphan_jobs()
    if adopted:
        logger.info("moved %d pre-existing job(s) into the default project", adopted)
    adopted_datasets = dataset_service.adopt_orphan_datasets()
    if adopted_datasets:
        logger.info("moved %d pre-existing dataset(s) into the default project", adopted_datasets)

    await monitor.start()
    await scheduler.start()
    # 部署和导出的进程不归调度器管，重启后要各自对账一次，
    # 否则会一直挂在 serving/running 上并且白占着 GPU 账。
    deployment_service.reconcile()
    export_service.reconcile()
    # 样例审核跑在事件循环里，没有子进程可以接管，重启就等于中断。
    review_service.reconcile()
    logger.info("%s %s ready", settings.app_name, settings.app_version)
    try:
        yield
    finally:
        await scheduler.stop()
        await monitor.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="团队内模型后训练（SFT / DPO / GRPO / DAPO）与 Agent 评测（Harbor）平台",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(ws_router)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    del request
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health", tags=["system"], summary="健康检查")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "gpu_total": scheduler.total_gpus,
    }


# --------------------------------------------------------------------------- #
# Static SPA (only mounted when the frontend has been built)
# --------------------------------------------------------------------------- #
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith(("api/", "ws/")):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

else:  # pragma: no cover - developer convenience

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "message": f"{settings.app_name} API 已启动，前端尚未构建。",
            "hint": "cd frontend && npm install && npm run build，或用 npm run dev 启动开发服务器。",
            "docs": "/api/docs",
        }


def _dist_hint() -> Path:
    return FRONTEND_DIST
