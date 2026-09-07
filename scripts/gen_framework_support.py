#!/usr/bin/env python3
"""Generate ``backend/app/resources/framework_support.json`` from the real repos.

The platform shows users a "which model works with which framework" cheat sheet
next to the model picker. Rather than hand-maintaining that list (which rots the
moment a framework is upgraded), we parse the checked-out framework sources:

* LlamaFactory  -> ``src/llamafactory/extras/constants.py`` (AST, no imports)
* verl          -> model registry + advantage estimators
* TRL           -> trainer export table
* Harbor        -> agent name / environment type enums

Run it after upgrading any framework:

    python scripts/gen_framework_support.py
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "backend" / "app" / "resources" / "framework_support.json"

DEFAULT_ROOTS = {
    "llamafactory": Path("/workspace/LiHanlin_Workspace/github/LlamaFactory"),
    "verl": Path("/workspace/LiHanlin_Workspace/github/verl"),
    "trl": Path("/workspace/LiHanlin_Workspace/github/trl"),
    "harbor": Path("/workspace/LiHanlin_Workspace/github/harbor"),
}


# --------------------------------------------------------------------------- #
# LlamaFactory
# --------------------------------------------------------------------------- #
def parse_llamafactory(root: Path) -> dict:
    constants = root / "src" / "llamafactory" / "extras" / "constants.py"
    out: dict = {
        "name": "LLaMA-Factory",
        "root": str(root),
        "version": _grep_version(root / "src" / "llamafactory" / "extras" / "env.py"),
        "available": constants.exists(),
        "models": [],
        "templates": [],
        "stages": ["pt", "sft", "rm", "ppo", "dpo", "kto"],
        "finetuning_types": ["lora", "full", "freeze"],
    }
    if not constants.exists():
        return out

    tree = ast.parse(constants.read_text(encoding="utf-8"))
    models: list[dict] = []
    templates: set[str] = set()

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "register_model_group"):
            continue
        group_models: dict | None = None
        template = None
        multimodal = False
        for kw in node.keywords:
            if kw.arg == "models" and isinstance(kw.value, ast.Dict):
                group_models = kw.value
            elif kw.arg == "template":
                template = _literal(kw.value)
            elif kw.arg == "multimodal":
                multimodal = bool(_literal(kw.value))
        if group_models is None:
            continue
        if template:
            templates.add(template)
        for key, value in zip(group_models.keys, group_models.values, strict=False):
            name = _literal(key)
            if not isinstance(name, str):
                continue
            sources: dict[str, str] = {}
            if isinstance(value, ast.Dict):
                for skey, svalue in zip(value.keys, value.values, strict=False):
                    source = _download_source(skey)
                    path = _literal(svalue)
                    if source and isinstance(path, str):
                        sources[source] = path
            models.append(
                {
                    "name": name,
                    "template": template,
                    "multimodal": multimodal,
                    "hf": sources.get("DEFAULT", ""),
                    "modelscope": sources.get("MODELSCOPE", ""),
                    "openmind": sources.get("OPENMIND", ""),
                }
            )

    out["models"] = sorted(models, key=lambda m: m["name"].lower())
    out["templates"] = sorted(templates)
    return out


def _download_source(node: ast.expr) -> str | None:
    # ``DownloadSource.MODELSCOPE`` -> "MODELSCOPE"
    if isinstance(node, ast.Attribute):
        return node.attr
    value = _literal(node)
    return value if isinstance(value, str) else None


def _literal(node: ast.expr):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


# --------------------------------------------------------------------------- #
# verl
# --------------------------------------------------------------------------- #
def parse_verl(root: Path) -> dict:
    out: dict = {
        "name": "verl",
        "root": str(root),
        "version": _read_text(root / "verl" / "version" / "version"),
        "available": (root / "verl").is_dir(),
        "advantage_estimators": [],
        "megatron_models": [],
        "transformers_patches": [],
        "rollout_backends": ["vllm", "sglang", "hf"],
        "strategies": ["fsdp", "fsdp2", "megatron"],
    }
    core_algos = root / "verl" / "trainer" / "ppo" / "core_algos.py"
    if core_algos.exists():
        text = core_algos.read_text(encoding="utf-8")
        block = re.search(r"class AdvantageEstimator\(str, Enum\):(.*?)\n\n\n", text, re.S)
        if block:
            out["advantage_estimators"] = re.findall(r'=\s*"([a-z0-9_]+)"', block.group(1))

    registry = root / "verl" / "models" / "registry.py"
    if registry.exists():
        out["megatron_models"] = sorted(set(re.findall(r'"([A-Za-z0-9_]+ForCausalLM)"', registry.read_text(encoding="utf-8"))))

    patches = root / "verl" / "models" / "transformers"
    if patches.is_dir():
        out["transformers_patches"] = sorted(
            p.stem for p in patches.glob("*.py") if p.stem not in {"__init__", "monkey_patch", "npu_patch"}
        )
    return out


# --------------------------------------------------------------------------- #
# TRL
# --------------------------------------------------------------------------- #
def parse_trl(root: Path) -> dict:
    out: dict = {
        "name": "TRL",
        "root": str(root),
        "version": _read_text(root / "VERSION"),
        "available": (root / "trl").is_dir(),
        "trainers": [],
        "has_harbor_integration": (root / "trl" / "experimental" / "harbor").is_dir(),
        "harbor_harnesses": [],
    }
    init = root / "trl" / "trainer" / "__init__.py"
    if init.exists():
        out["trainers"] = sorted(set(re.findall(r'"([A-Za-z0-9_]+Trainer)"', init.read_text(encoding="utf-8"))))
    harness_dir = root / "examples" / "scripts" / "harbor" / "harnesses"
    if harness_dir.is_dir():
        out["harbor_harnesses"] = sorted(p.name for p in harness_dir.iterdir() if p.is_dir())
    return out


# --------------------------------------------------------------------------- #
# Harbor
# --------------------------------------------------------------------------- #
def parse_harbor(root: Path) -> dict:
    out: dict = {
        "name": "Harbor",
        "root": str(root),
        "version": _grep_pyproject_version(root / "pyproject.toml"),
        "available": (root / "src" / "harbor").is_dir(),
        "agents": [],
        "environments": [],
        "installed_agents": [],
    }
    names = root / "src" / "harbor" / "models" / "agent" / "name.py"
    if names.exists():
        out["agents"] = sorted(set(re.findall(r'=\s*"([a-z0-9\-\._]+)"', names.read_text(encoding="utf-8"))))
    envs = root / "src" / "harbor" / "models" / "environment_type.py"
    if envs.exists():
        out["environments"] = sorted(set(re.findall(r'=\s*"([a-z0-9\-_]+)"', envs.read_text(encoding="utf-8"))))
    installed = root / "src" / "harbor" / "agents" / "installed"
    if installed.is_dir():
        out["installed_agents"] = sorted(p.stem for p in installed.glob("*.py") if p.stem != "__init__")
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _grep_version(path: Path) -> str:
    if not path.exists():
        return ""
    match = re.search(r'VERSION\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def _grep_pyproject_version(path: Path) -> str:
    if not path.exists():
        return ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def build(roots: dict[str, Path]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llamafactory": parse_llamafactory(roots["llamafactory"]),
        "verl": parse_verl(roots["verl"]),
        "trl": parse_trl(roots["trl"]),
        "harbor": parse_harbor(roots["harbor"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    for key, value in DEFAULT_ROOTS.items():
        parser.add_argument(f"--{key}-root", type=Path, default=value)
    args = parser.parse_args()

    roots = {key: getattr(args, f"{key}_root") for key in DEFAULT_ROOTS}
    payload = build(roots)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {args.out}")
    print(f"  llamafactory: {len(payload['llamafactory']['models'])} models, "
          f"{len(payload['llamafactory']['templates'])} templates")
    print(f"  verl: {len(payload['verl']['advantage_estimators'])} advantage estimators")
    print(f"  trl: {len(payload['trl']['trainers'])} trainers, harbor={payload['trl']['has_harbor_integration']}")
    print(f"  harbor: {len(payload['harbor']['agents'])} agents, {len(payload['harbor']['environments'])} envs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
