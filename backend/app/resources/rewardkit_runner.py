"""Reward Kit CLI with an independent-copy fallback for unprivileged containers.

Copied into Harbor tests/ as _prt_rewardkit_runner.py; stdlib only until main().
"""

from __future__ import annotations

import logging
import runpy
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path


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
    runpy.run_module("rewardkit", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
