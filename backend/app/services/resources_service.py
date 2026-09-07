"""Static catalogs served to the UI: framework support, presets, dataset specs.

``framework_support.json`` is generated from the checked-out framework sources by
``scripts/gen_framework_support.py`` — see that script when a framework is
upgraded. Everything here is read-only and cached by file mtime.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings

_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, Any]] = {}


def _load(name: str) -> dict[str, Any]:
    path = settings.resources_dir / name
    with _LOCK:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return {}
        cached = _CACHE.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[name] = (mtime, data)
        return data


def framework_support() -> dict[str, Any]:
    return _load("framework_support.json")


def presets() -> dict[str, Any]:
    return _load("presets.json")


def dataset_specs() -> dict[str, Any]:
    return _load("dataset_specs.json")


# --------------------------------------------------------------------------- #
# Derived catalogs
# --------------------------------------------------------------------------- #
def llamafactory_templates() -> list[str]:
    return framework_support().get("llamafactory", {}).get("templates", [])


def harbor_agents() -> list[dict[str, Any]]:
    """Agent list with the credentials each one typically needs."""
    support = framework_support().get("harbor", {})
    hints = {
        "oracle": ("参考解 agent，执行 solution/solve.sh，用于任务自检", []),
        "nop": ("空 agent，什么都不做，用于确认任务不可被空解通过", []),
        "claude-code": ("Anthropic Claude Code CLI", ["ANTHROPIC_API_KEY"]),
        "codex": ("OpenAI Codex CLI", ["OPENAI_API_KEY"]),
        "cursor-cli": ("Cursor CLI", ["CURSOR_API_KEY"]),
        "gemini-cli": ("Google Gemini CLI", ["GEMINI_API_KEY", "GOOGLE_API_KEY"]),
        "aider": ("Aider 编码 agent", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]),
        "goose": ("Block Goose，模型格式 provider/model_name", ["OPENAI_API_KEY"]),
        "opencode": ("OpenCode CLI", ["OPENAI_API_KEY"]),
        "openhands": ("OpenHands，需 LLM_API_KEY 与 LLM_MODEL", ["LLM_API_KEY", "LLM_MODEL"]),
        "openhands-sdk": ("OpenHands SDK 版本", ["LLM_API_KEY", "LLM_MODEL"]),
        "mini-swe-agent": ("Mini SWE-agent", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]),
        "swe-agent": ("SWE-agent", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]),
        "terminus": ("Harbor 内置终端 agent，可指向任意 OpenAI 兼容服务", ["OPENAI_API_KEY"]),
        "terminus-1": ("Terminus v1", ["OPENAI_API_KEY"]),
        "terminus-2": ("Terminus v2", ["OPENAI_API_KEY"]),
        "qwen-coder": ("Qwen Coder CLI", ["DASHSCOPE_API_KEY", "OPENAI_API_KEY"]),
        "cline-cli": ("Cline CLI，模型格式 provider:model-id", ["OPENROUTER_API_KEY"]),
        "copilot-cli": ("GitHub Copilot CLI", ["GITHUB_TOKEN"]),
        "kimi-cli": ("Kimi CLI", ["MOONSHOT_API_KEY"]),
        "kimi-code": ("Kimi Code", ["MOONSHOT_API_KEY"]),
    }
    items: list[dict[str, Any]] = []
    for name in support.get("agents", []):
        description, env = hints.get(name, ("", []))
        items.append(
            {
                "name": name,
                "description": description,
                "env_keys": env,
                "needs_model": name not in {"oracle", "nop"},
                "recommended": name in {"oracle", "nop", "claude-code", "codex", "terminus"},
            }
        )
    items.sort(key=lambda a: (not a["recommended"], a["name"]))
    return items


def harbor_environments() -> list[str]:
    return framework_support().get("harbor", {}).get("environments", ["docker"])


def verl_advantage_estimators() -> list[str]:
    return framework_support().get("verl", {}).get("advantage_estimators", [])


def algorithms() -> list[dict[str, Any]]:
    return presets().get("algorithms", [])


_PROBE_TTL = 60.0
_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# (import name of the framework itself, PYTHONPATH suffix, hard dependency to look for)
_FRAMEWORK_PROBES: dict[str, tuple[str, str, str]] = {
    "llamafactory": ("llamafactory", "src", "torch"),
    "verl": ("verl", "", "torch"),
    "trl": ("trl", "", "torch"),
    "harbor": ("harbor", "src", ""),
}


def _probe_python(python_bin: str, module: str, pythonpath: str, dependency: str) -> dict[str, Any]:
    """Ask the configured interpreter whether it can actually import the framework.

    Checking that the interpreter *file exists* is not enough — the common failure
    mode is a valid python that simply has no torch/transformers installed, and
    reporting that as "可用" sends admins chasing ghosts at run time.
    """
    cache_key = f"{python_bin}|{module}|{pythonpath}|{dependency}"
    cached = _PROBE_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < _PROBE_TTL:
        return cached[1]

    result: dict[str, Any] = {"importable": None, "deps_ok": None, "error": ""}
    resolved = shutil.which(python_bin) or (python_bin if Path(python_bin).is_file() else None)
    if resolved is None:
        result["error"] = "解释器不存在"
        _PROBE_CACHE[cache_key] = (now, result)
        return result

    script = (
        "import importlib.util as u, json, sys;"
        f"print(json.dumps({{'m': u.find_spec({module!r}) is not None,"
        f"'d': (u.find_spec({dependency!r}) is not None) if {bool(dependency)!r} else None}}))"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    try:
        completed = subprocess.run(  # noqa: S603 - argv is built from settings
            [resolved, "-c", script],
            capture_output=True,
            text=True,
            timeout=25,
            env=env,
            check=False,
        )
        payload = json.loads(completed.stdout.strip() or "{}")
        result["importable"] = bool(payload.get("m"))
        result["deps_ok"] = payload.get("d")
        if completed.returncode != 0:
            result["error"] = (completed.stderr or "").strip()[:200]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]

    _PROBE_CACHE[cache_key] = (now, result)
    return result


def _docker_status() -> dict[str, Any]:
    """Harbor's default sandbox is Docker — check the daemon is actually reachable.

    A present ``docker`` binary means nothing if the service user is not in the
    ``docker`` group; without this check the first evaluation only fails minutes
    later, inside the environment build.
    """
    cached = _PROBE_CACHE.get("__docker__")
    now = time.time()
    if cached and now - cached[0] < _PROBE_TTL:
        return cached[1]

    binary = shutil.which("docker")
    row: dict[str, Any] = {
        "key": "docker",
        "label": "Docker 守护进程",
        "version": "",
        "root": "",
        "root_exists": True,
        "python": binary or "docker",
        "python_ok": bool(binary),
        "deps_ok": None,
    }
    if not binary:
        row.update(
            importable=False,
            status="missing_python",
            detail="未安装 docker；Harbor 评测默认使用 docker 沙箱",
        )
    else:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv
                [binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            completed = None
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = (completed.stderr or "").strip()

        if completed is not None and completed.returncode == 0:
            row.update(
                version=completed.stdout.strip(),
                importable=True,
                status="ready",
                detail="沙箱可用",
            )
        elif "permission denied" in error.lower():
            import getpass

            row.update(
                importable=False,
                status="missing_deps",
                detail=(
                    f"守护进程存在但当前用户（{getpass.getuser()}）无权访问 docker.sock。"
                    "执行 `sudo usermod -aG docker $USER` 后重新登录，或改用 e2b / daytona 等云端沙箱。"
                ),
            )
        else:
            row.update(
                importable=False,
                status="missing_deps",
                detail=f"无法连接 docker 守护进程：{error[:160] or '未知错误'}",
            )

    _PROBE_CACHE["__docker__"] = (now, row)
    return row


def toolchain_status() -> list[dict[str, Any]]:
    """Is each framework actually usable from this deployment?"""
    support = framework_support()
    rows: list[dict[str, Any]] = []

    for key, label in (
        ("llamafactory", "LLaMA-Factory"),
        ("verl", "verl"),
        ("trl", "TRL"),
        ("harbor", "Harbor"),
    ):
        info = support.get(key, {})
        root = settings.root_for(key)
        root_exists = bool(root and root.is_dir())
        python_bin = settings.python_for(key)
        python_ok = bool(shutil.which(python_bin) or Path(python_bin).is_file())

        module, path_suffix, dependency = _FRAMEWORK_PROBES[key]
        pythonpath = str(root / path_suffix) if root and path_suffix else (str(root) if root else "")
        probe = _probe_python(python_bin, module, pythonpath, dependency) if python_ok else {
            "importable": None,
            "deps_ok": None,
            "error": "解释器不存在",
        }

        if not python_ok:
            status_value, detail = "missing_python", f"找不到解释器 {python_bin}"
        elif probe["deps_ok"] is False:
            status_value = "missing_deps"
            detail = f"{python_bin} 里没有 {dependency}，请指定装好依赖的环境（PRT_{key.upper()}_PYTHON）"
        elif probe["importable"] is False:
            status_value = "missing_deps"
            detail = (
                f"{python_bin} 无法导入 {module}"
                + (f"（源码目录 {root} 不存在）" if not root_exists else "，请检查源码目录或安装该框架")
            )
        elif probe["importable"]:
            status_value, detail = "ready", "可直接启动任务"
        else:
            status_value, detail = "unknown", probe.get("error", "")

        rows.append(
            {
                "key": key,
                "label": label,
                "version": info.get("version", ""),
                "root": str(root) if root else "",
                "root_exists": root_exists,
                "python": python_bin,
                "python_ok": python_ok,
                "importable": probe["importable"],
                "deps_ok": probe["deps_ok"],
                "status": status_value,
                "detail": detail,
            }
        )

    harbor_cli = settings.harbor_bin
    cli_ok = bool(shutil.which(harbor_cli) or Path(harbor_cli).is_file())
    rows.append(
        {
            "key": "harbor-cli",
            "label": "harbor CLI",
            "version": support.get("harbor", {}).get("version", ""),
            "root": "",
            "root_exists": True,
            "python": harbor_cli,
            "python_ok": cli_ok,
            "importable": cli_ok,
            "deps_ok": None,
            "status": "ready" if cli_ok else "missing_python",
            "detail": "评测任务可执行" if cli_ok else "未安装 Harbor（pip install harbor）",
        }
    )

    rows.append(_docker_status())

    modelscope = settings.modelscope_bin
    ms_ok = bool(
        shutil.which(modelscope)
        or Path(modelscope).is_file()
        or Path("/opt/miniconda3/envs/modelscope/bin/modelscope").is_file()
    )
    rows.append(
        {
            "key": "modelscope",
            "label": "ModelScope CLI",
            "version": "",
            "root": "",
            "root_exists": True,
            "python": modelscope,
            "python_ok": ms_ok,
            "importable": ms_ok,
            "deps_ok": None,
            "status": "ready" if ms_ok else "missing_python",
            "detail": "可从 ModelScope 下载模型" if ms_ok else "未找到 modelscope 命令",
        }
    )
    return rows


def model_compat_doc() -> dict[str, Any]:
    """The cheat sheet rendered next to the model picker."""
    support = framework_support()
    lf = support.get("llamafactory", {})
    verl = support.get("verl", {})
    trl = support.get("trl", {})
    harbor = support.get("harbor", {})

    return {
        "generated_at": support.get("generated_at", ""),
        "sections": [
            {
                "framework": "llamafactory",
                "title": f"LLaMA-Factory {lf.get('version', '')}",
                "headline": f"内置 {len(lf.get('models', []))} 个模型条目 / {len(lf.get('templates', []))} 套对话模板",
                "bullets": [
                    "只要模型出现在下方列表里，平台会自动带出正确的 template，无需手填。",
                    "本地模型目录名与 HF/ModelScope 路径都会参与匹配；改过名字时请手动确认 template。",
                    "支持的阶段：" + " / ".join(lf.get("stages", [])),
                    "微调方式：" + " / ".join(lf.get("finetuning_types", [])),
                    "多模态模型需要选择带 _vl / _omni 后缀的模板。",
                ],
                "table": {
                    "columns": ["模型", "template", "多模态", "ModelScope"],
                    "rows": [
                        [m["name"], m.get("template") or "-", "是" if m.get("multimodal") else "否", m.get("modelscope") or "-"]
                        for m in lf.get("models", [])
                    ],
                },
            },
            {
                "framework": "verl",
                "title": f"verl {verl.get('version', '')}",
                "headline": "FSDP/Megatron + vLLM rollout 的大规模 RL 框架",
                "bullets": [
                    "FSDP 路径支持绝大多数 HuggingFace CausalLM；Megatron 路径仅支持已注册模型。",
                    "已注册 Megatron 模型：" + (", ".join(verl.get("megatron_models", [])) or "无"),
                    "专用加速补丁：" + (", ".join(verl.get("transformers_patches", [])) or "无"),
                    "可选优势估计器：" + ", ".join(verl.get("advantage_estimators", [])),
                    "rollout 后端：" + " / ".join(verl.get("rollout_backends", [])),
                ],
                "table": None,
            },
            {
                "framework": "trl",
                "title": f"TRL {trl.get('version', '')}",
                "headline": "HuggingFace 官方后训练库，与 transformers 生态完全一致",
                "bullets": [
                    "可用 Trainer：" + ", ".join(trl.get("trainers", [])),
                    "任何 transformers 支持的 CausalLM 都可以直接训练。",
                    ("已检测到 trl.experimental.harbor：可直接把 Harbor 任务树当作 RL 环境做 agentic RL。"
                     if trl.get("has_harbor_integration")
                     else "当前 TRL 版本未包含 Harbor 集成。"),
                    "可用 harness：" + (", ".join(trl.get("harbor_harnesses", [])) or "无"),
                    "GSPO = GRPOTrainer + importance_sampling_level=sequence。",
                ],
                "table": None,
            },
            {
                "framework": "harbor",
                "title": f"Harbor {harbor.get('version', '')}",
                "headline": f"{len(harbor.get('agents', []))} 个 agent / {len(harbor.get('environments', []))} 种沙箱后端",
                "bullets": [
                    "评测数据必须是 Harbor 任务树（task.toml + instruction.md + environment/ + tests/）。",
                    "oracle 用于任务自检，nop 用于确认任务不可被空解通过，正式评测请选真实 agent。",
                    "pass@k 由 --n-attempts 控制，并发由 --n-concurrent 控制。",
                    "reward-kit judge：在 tests/ 提供 reward.toml + criteria.py，judge 用的 API Key 通过环境变量注入。",
                    "沙箱后端：" + ", ".join(harbor.get("environments", [])),
                ],
                "table": None,
            },
        ],
    }
