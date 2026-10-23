"""Job-local Harbor guards; loaded in Harbor's interpreter, never patch its install."""
from __future__ import annotations

import asyncio
import functools
import json
import os
from pathlib import Path
import shlex
import tempfile
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


# Run as root inside the container, only on verified writable log bind mounts.
# Keep the creating UID (the agent may resume) and grant the host process's group
# access. Never follow links into task inputs, the workspace, or other mounts.
LOG_PERMISSIONS = r"""
import os, stat, sys
gid = int(sys.argv[1])
def repair(fd, device):
    info = os.fstat(fd)
    if info.st_dev != device:
        return
    directory = stat.S_ISDIR(info.st_mode)
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        return
    mode = stat.S_IMODE(info.st_mode) | (0o770 if directory else 0o660)
    if info.st_gid != gid:
        os.fchown(fd, -1, gid)
    os.fchmod(fd, mode & ~(stat.S_ISUID | stat.S_ISGID))
    if directory:
        for name in os.listdir(fd):
            child = None
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                repair(child, device)
            except FileNotFoundError:
                pass
            finally:
                if child is not None:
                    os.close(child)
for path in sys.argv[2:]:
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.strip('/').split('/'):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        repair(fd, os.fstat(fd).st_dev)
    except FileNotFoundError:
        pass
    finally:
        os.close(fd)
"""


def log_mount_targets(environment):
    paths = getattr(environment, 'trial_paths', None)
    if paths is None:
        return []
    allowed = {str(Path(value).resolve()) for name in ('agent_dir', 'verifier_dir')
               if (value := getattr(paths, name, None)) is not None}
    targets = []
    for mount in getattr(environment, '_mounts', []):
        source, target = mount.get('source'), mount.get('target')
        if (mount.get('type') == 'bind' and not mount.get('read_only') and source and target
                and str(Path(source).resolve()) in allowed
                and target in ('/logs/agent', '/logs/verifier')):
            targets.append(target)
    return sorted(set(targets))


async def repair_log_permissions(original, environment):
    targets = log_mount_targets(environment)
    if not targets or not hasattr(os, 'getgid'):
        return
    command = shlex.join(['python3', '-c', LOG_PERMISSIONS, str(os.getgid()), *targets])
    task = asyncio.create_task(original(environment, command=command, user='root', timeout_sec=8))
    done, _ = await asyncio.wait({task}, timeout=10)
    if task in done:
        try:
            if task.result().return_code == 0:
                return
        except Exception:
            pass
    else:
        task.cancel()
        task.add_done_callback(consume)
    environment.logger.warning('PRT: 日志目录权限修复失败，宿主机可能无法读取或归档日志；请检查容器权限')


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
        finally:
            # Must run before Harbor reads rewards or archives a completed step,
            # including when wait_for cancelled the verifier after its timeout.
            try:
                await repair_log_permissions(original, self)
            except Exception:
                self.logger.warning('PRT: 无法修复日志目录权限，请检查 Docker 状态')
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


def wait_for_codex_mcp(original):
    @functools.wraps(original)
    def build_config(self, *args, **kwargs):
        config = original(self, *args, **kwargs)
        # Codex 0.155.1 otherwise gives optional MCP servers only 1 second.
        # Zero waits for their startup results (with each server's own timeout).
        # Apply after Harbor's merge so resumed records benefit without rewriting
        # their config/lock/results or changing providers and MCP definitions.
        config.setdefault("mcp_optional_startup_grace_ms", 0)
        return config
    build_config._prt_mcp_startup_guard = True
    return build_config


PLATFORM_BASE = "prt-ai-factory-harbor-eval-base-image:sha-"
TASK_IMAGE_NAME = "prt-platform-task"


def uses_platform_base(dockerfile: Path) -> bool:
    try:
        first = dockerfile.read_text(encoding="utf-8").lstrip().split("\n", 1)[0]
    except OSError:
        return False
    return first.startswith("FROM " + PLATFORM_BASE)


async def image_exists(image: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", image,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await process.wait() == 0


async def build_task_image(env_dir: Path, dockerfile: Path, logger) -> str:
    from filelock import AsyncFileLock
    from harbor.utils.container_cache import docker_build_context_hash
    # Plain `docker build` (not buildx) so FROM resolves the locally pinned platform image.
    image = f"{TASK_IMAGE_NAME}:{docker_build_context_hash(context=env_dir, dockerfile_path=dockerfile)}"
    if await image_exists(image):
        return image
    lock_dir = Path(tempfile.gettempdir()) / "prt-platform-task-images"
    lock_dir.mkdir(parents=True, exist_ok=True)
    async with AsyncFileLock(str(lock_dir / (image.split(":", 1)[1] + ".lock"))):
        if await image_exists(image):
            return image
        logger.info("PRT: 首次构建平台题目镜像 %s（之后的评测直接复用）", image)
        process = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", image, "-f", str(dockerfile), str(env_dir),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            raise
        if process.returncode:
            raise RuntimeError(f"平台题目镜像构建失败: {output.decode(errors='replace')[-4000:]}")
    return image


def reuse_task_image(original):
    """Platform-image tasks: build each task's thin layer once, then run it as a prebuilt image.

    The tag is content-addressed (Dockerfile incl. pinned base + context), so later
    trials and jobs skip `compose build`, and `down --rmi local` leaves it alone.
    Resume/rerun reuse the recorded task dirs, so detection is by the Dockerfile.
    """
    @functools.wraps(original)
    async def start(self, force_build):
        env_dir = Path(self.environment_dir)
        dockerfile = env_dir / "Dockerfile"
        if (not force_build and not self.task_env_config.docker_image and uses_platform_base(dockerfile)
                and not (env_dir / "docker-compose.yaml").exists()
                and not getattr(self, "_is_windows_container", False)):
            image = await build_task_image(env_dir, dockerfile, self.logger)
            self.task_env_config.docker_image = image
            self._env_vars.prebuilt_image_name = image
            self.logger.info("PRT: 使用平台题目镜像 %s，跳过 compose build", image)
        return await original(self, force_build)
    start._prt_task_image = True
    return start


def install_guards() -> None:
    if Path(__file__).with_name("prt_cc_bridge_runtime.py").is_file():
        from prt_cc_bridge_runtime import install
        install()
    from harbor.agents.installed.codex import Codex
    from harbor.trial.trial import Trial
    from harbor.environments.docker.docker import DockerEnvironment
    original = getattr(Codex, "_build_effective_config", None)
    if original is not None and not getattr(original, "_prt_mcp_startup_guard", False):
        Codex._build_effective_config = wait_for_codex_mcp(original)
    if hasattr(Trial, "_emit"):
        Trial._emit = record_phase(Trial._emit)
    for name in ("_collect_buffered_output", "_collect_streamed_output"):
        original = getattr(DockerEnvironment, name, None)
        if original is not None:
            setattr(DockerEnvironment, name, staticmethod(reap_on_cancel(original)))
    DockerEnvironment.exec = guarded_exec(DockerEnvironment.exec)
    original = getattr(DockerEnvironment, "start", None)
    if original is not None and not getattr(original, "_prt_task_image", False):
        DockerEnvironment.start = reuse_task_image(original)


def main() -> None:
    install_guards()
    print("PRT: 平台不设评测时限；按 Harbor 任务配置执行；超时或取消时清理容器内进程组", flush=True)
    from harbor.cli.main import app
    app()


if __name__ == '__main__':
    main()
