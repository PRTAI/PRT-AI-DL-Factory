"""Reward Kit CLI with an independent-copy fallback for unprivileged containers.

Copied into Harbor tests/ as _prt_rewardkit_runner.py; stdlib only until main().
"""

from __future__ import annotations

import logging
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from contextlib import ExitStack, contextmanager
from pathlib import Path

USAGE_PATH = Path("/logs/verifier/prt-model-usage.jsonl")


def record_usage(kwargs, response, start_time, end_time) -> None:
    """Store counters only: no prompts, responses, endpoint credentials or keys."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict) or usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
            return
        details = usage.get("prompt_tokens_details") or {}
        creation = usage.get("cache_creation_input_tokens") or details.get("cache_creation_tokens") or 0
        model = kwargs.get("model") or ""
        request_id = (response.get("id") if isinstance(response, dict) else getattr(response, "id", None)) or kwargs.get("litellm_call_id")
        if not request_id:
            return
        payload = {
            "schema_version": "prt-usage-v1", "request_id": str(request_id), "model": model,
            "timestamp": (start_time if isinstance(start_time, datetime) else datetime.now(timezone.utc)).isoformat(),
            "metrics": {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
                        "cached_tokens": details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0,
                        "extra": {"cache_creation_input_tokens": creation,
                                  "cache_creation": details.get("cache_creation_token_details") or usage.get("cache_creation") or {}}},
        }
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # One append syscall per response also works across concurrent CLI processes.
        fd = os.open(USAGE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        logging.getLogger(__name__).warning("Could not record Judge Token usage", exc_info=True)


def install_usage_recorder() -> None:
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger

        if getattr(litellm, "_prt_usage_recorder", False):
            return

        class UsageRecorder(CustomLogger):
            def log_success_event(self, kwargs, response_obj, start_time, end_time):
                record_usage(kwargs, response_obj, start_time, end_time)

            async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
                record_usage(kwargs, response_obj, start_time, end_time)

        litellm.callbacks.append(UsageRecorder())
        litellm._prt_usage_recorder = True
    except ImportError:
        logging.getLogger(__name__).warning("LiteLLM unavailable; Judge usage recording skipped")


def _can_mount() -> bool:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << 21))  # CAP_SYS_ADMIN
    except (OSError, ValueError, IndexError):
        pass
    return False


def install_fallback() -> None:
    import rewardkit.isolation as isolation

    if getattr(isolation, "_prt_copy_fallback", False):
        return
    original = isolation.isolate
    overlay_available = _can_mount()
    warned = False

    @contextmanager
    def compatible_isolate(path: Path):
        nonlocal overlay_available, warned
        with ExitStack() as stack:
            workspace = None
            if overlay_available:
                try:
                    workspace = stack.enter_context(original(path))
                except (OSError, subprocess.CalledProcessError):
                    overlay_available = False
            if workspace is None:
                if not warned:
                    logging.getLogger("rewardkit.isolation").warning(
                        "Overlay mount unavailable; using an independent workspace copy for each judge."
                    )
                    warned = True
                temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="prt-judge-"))
                workspace = Path(temporary) / "workspace"
                # Dereference links so writes in a snapshot cannot reach the original workspace.
                shutil.copytree(path, workspace, symlinks=False, ignore_dangling_symlinks=True)
            yield workspace

    # __init__ imports reward.py first; update its already-imported synchronous alias too.
    for module in list(sys.modules.values()):
        if module and getattr(module, "__name__", "").startswith("rewardkit"):
            for name, value in list(vars(module).items()):
                if value is original:
                    setattr(module, name, compatible_isolate)
    isolation._prt_copy_fallback = True


def main() -> None:
    install_fallback()
    install_usage_recorder()
    runpy.run_module("rewardkit", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
