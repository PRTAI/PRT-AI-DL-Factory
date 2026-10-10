"""API router aggregation."""

from fastapi import APIRouter

from . import (
    auth,
    datasets,
    deployments,
    docs,
    jobs,
    model_apis,
    models,
    projects,
    resources,
    reviews,
    system,
    users,
    ws,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(jobs.router)
api_router.include_router(datasets.router)
api_router.include_router(models.router)
api_router.include_router(model_apis.router)
api_router.include_router(deployments.router)
api_router.include_router(deployments.exports_router)
api_router.include_router(reviews.router)
api_router.include_router(system.router)
api_router.include_router(resources.router)
api_router.include_router(docs.router)

ws_router = ws.router

__all__ = ["api_router", "ws_router"]
