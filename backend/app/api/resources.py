"""Static catalogs the submission wizards need (presets, agents, templates)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..deps import CurrentUser
from ..services import resources_service

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/frameworks", summary="框架版本与能力")
def frameworks(_: CurrentUser) -> dict[str, Any]:
    support = resources_service.framework_support()
    # The full LLaMA-Factory model table is large; it has its own endpoint.
    trimmed = {k: v for k, v in support.items() if k != "llamafactory"}
    lf = dict(support.get("llamafactory", {}))
    lf.pop("models", None)
    trimmed["llamafactory"] = lf
    return trimmed


@router.get("/algorithms", summary="可选算法")
def algorithms(_: CurrentUser) -> list[dict[str, Any]]:
    return resources_service.algorithms()


@router.get("/presets", summary="训练/评测预设")
def presets(_: CurrentUser) -> dict[str, Any]:
    payload = resources_service.presets()
    return {
        "sft_presets": payload.get("sft_presets", []),
        "rl_presets": payload.get("rl_presets", []),
        "verify_presets": payload.get("verify_presets", []),
    }


@router.get("/templates", summary="LLaMA-Factory 对话模板")
def templates(_: CurrentUser) -> list[str]:
    return resources_service.llamafactory_templates()


@router.get("/harbor-agents", summary="Harbor 可用 agent")
def harbor_agents(_: CurrentUser) -> list[dict[str, Any]]:
    return resources_service.harbor_agents()


@router.get("/harbor-environments", summary="Harbor 沙箱后端")
def harbor_environments(_: CurrentUser) -> list[str]:
    return resources_service.harbor_environments()


@router.get("/verl-estimators", summary="verl 优势估计器")
def verl_estimators(_: CurrentUser) -> list[str]:
    return resources_service.verl_advantage_estimators()
