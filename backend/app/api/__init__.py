"""API router aggregation."""

from fastapi import APIRouter

from . import auth, datasets, jobs, models, resources, system, users, ws

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(jobs.router)
api_router.include_router(datasets.router)
api_router.include_router(models.router)
api_router.include_router(system.router)
api_router.include_router(resources.router)

ws_router = ws.router

__all__ = ["api_router", "ws_router"]
