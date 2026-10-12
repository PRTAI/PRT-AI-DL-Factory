"""Job-local Harbor guards; loaded in Harbor's interpreter, never patch its install."""
from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
import shlex
import time
import uuid


def grouped_command(command: str, pidfile: str) -> str:
    # The PID recorded after setsid is also the process group ID. Never use pkill
    # by executable name: agent and verifier may share a container.
    child = f"echo $$ > {shlex.quote(pidfile)}; exec bash -c {shlex.quote(command)}"
    fallback = "import os,sys; os.setsid(); os.execvp('bash',['bash','-c',sys.argv[1]])"
    return (
        f"if command -v setsid >/dev/null 2>&1; then setsid --wait bash -c {shlex.quote(child)}; "
        f"elif command -v python3 >/dev/null 2>&1; then python3 -c {shlex.quote(fallback)} {shlex.quote(child)}; "
        "else echo 'PRT: timeout protection requires setsid or python3' >&2; exit 125; fi; "
        f"rc=$?; rm -f {shlex.quote(pidfile)}; exit $rc"
    )


# Executed inside the Linux container. Capture start times to avoid signalling a
# recycled PID, and include descendants that created their own sessions (judges).
TREE_CLEANUP = r"""
import os, signal, sys, time
from pathlib import Path
root = int(sys.argv[1])
if root <= 1:
    raise SystemExit(1)
def snapshot():
    result = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            result[int(path.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            pass
    return result
all_processes = snapshot()
selected = {root} if root in all_processes else set()
while True:
    found = {pid for pid, (parent, _) in all_processes.items() if parent in selected}
    expanded = selected | found
    if expanded == selected:
        break
    selected = expanded
for sig in (signal.SIGTERM, signal.SIGKILL):
    current = snapshot()
    for pid in sorted(selected, reverse=True):
        if pid not in current or current[pid][1] != all_processes[pid][1]:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    if sig == signal.SIGTERM:
        time.sleep(2)
"""


def cleanup_command(pidfile: str) -> str:
    quoted = shlex.quote(pidfile)
    return (
        f"if [ -f {quoted} ]; then read -r p < {quoted}; "
        "case $p in ''|*[!0-9]*) exit 1;; esac; "
        "if [ -d /proc ] && command -v python3 >/dev/null 2>&1; then "
        f"python3 -c {shlex.quote(TREE_CLEANUP)} \"$p\" || exit $?; "
        "elif [ \"$p\" -gt 1 ]; then kill -TERM -- -\"$p\" 2>/dev/null || true; "
        "sleep 2; kill -KILL -- -\"$p\" 2>/dev/null || true; fi; "
        f"rm -f {quoted}; else exit 3; fi"
    )


def consume(task):
    if not task.cancelled():
        task.exception()


async def bounded_cleanup(original, environment, pidfile):
    # Do not let a broken Docker daemon hold asyncio cancellation indefinitely.
    task = asyncio.create_task(original(environment, command=cleanup_command(pidfile), user="root", timeout_sec=8))
    done, _ = await asyncio.wait({task}, timeout=10)
    if task in done:
        try:
            result = task.result()
            if result.return_code:
                environment.logger.warning("PRT: 容器进程组清理失败，请检查 Docker 状态")
        except Exception:
            environment.logger.warning("PRT: 容器进程组清理失败，请检查 Docker 状态")
    else:
        task.cancel()
        task.add_done_callback(consume)
        environment.logger.warning("PRT: 容器进程组清理超时，请检查 Docker 状态")


def guarded_exec(original):
    @functools.wraps(original)
    async def execute(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        if getattr(self, '_is_windows_container', False):
            return await original(self, command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)
        pidfile = f"/tmp/prt-exec-{uuid.uuid4().hex}.pid"
        started = time.monotonic()
        limit = timeout_sec if timeout_sec is not None and timeout_sec > 0 else None
        task = asyncio.create_task(original(
            self, grouped_command(command, pidfile), cwd=cwd,
            env={**(env or {}), "PYTHONUNBUFFERED": "1"}, timeout_sec=None, user=user,
        ))
        try:
            while True:
                remaining = limit - (time.monotonic() - started) if limit is not None else None
                if remaining is not None and remaining <= 0:
                    raise asyncio.TimeoutError("PRT: Docker command exceeded execution limit")
                done, _ = await asyncio.wait({task}, timeout=min(60, remaining) if remaining is not None else 60)
                if task in done:
                    return task.result()
                self.logger.info("PRT: 容器命令仍在执行，已等待 %.0f 秒；可能正在等待模型，详见 agent/verifier 日志", time.monotonic() - started)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            self.logger.warning("PRT: 执行超时或取消，正在回收容器内当前命令及其子进程")
            task.cancel()
            await bounded_cleanup(original, self, pidfile)
            task.add_done_callback(consume)
            raise
    return execute


def reap_on_cancel(original):
    @functools.wraps(original)
    async def collect(process, *args, **kwargs):
        try:
            return await original(process, *args, **kwargs)
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            waiter = asyncio.create_task(process.wait())
            done, _ = await asyncio.wait({waiter}, timeout=2)
            if not done:
                waiter.cancel()
                waiter.add_done_callback(consume)
            raise
    return collect


def record_phase(original):
    @functools.wraps(original)
    async def emit(self, event):
        name = getattr(event, "value", str(event))
        phases = {"environment-start": "setup_environment", "agent-start": "running_agent", "verification-start": "running_verifier"}
        if name in phases:
            try:
                path = Path(self.paths.trial_dir) / "prt-phase.json"
                state = {"phase": phases[name], "started_at": time.time()}
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(state), encoding="utf-8")
                temporary.replace(path)
                self.logger.info("PRT: 阶段开始 %s", phases[name])
            except OSError:
                self.logger.warning("PRT: 无法写入阶段时间记录")
        return await original(self, event)
    return emit


def install_guards() -> None:
    from harbor.trial.trial import Trial
    from harbor.environments.docker.docker import DockerEnvironment
    if hasattr(Trial, "_emit"):
        Trial._emit = record_phase(Trial._emit)
    for name in ("_collect_buffered_output", "_collect_streamed_output"):
        original = getattr(DockerEnvironment, name, None)
        if original is not None:
            setattr(DockerEnvironment, name, staticmethod(reap_on_cancel(original)))
    DockerEnvironment.exec = guarded_exec(DockerEnvironment.exec)


def main() -> None:
    install_guards()
    print("PRT: 平台不设评测时限；按 Harbor 任务配置执行；超时或取消时清理容器内进程组", flush=True)
    from harbor.cli.main import app
    app()


if __name__ == '__main__':
    main()
