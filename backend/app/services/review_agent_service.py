"""在 Harbor 容器中执行真正的审核 Agent，保存工具轨迹并读取结构化结论。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from pathlib import Path

import psutil

from ..builders import review as review_builder
from ..config import settings
from ..models import ReviewTask
from . import proc_env

MAX_RESULT_BYTES = 8 * 1024 * 1024


class ReviewAgentError(RuntimeError):
    pass


def _mask(text: str, secrets: list[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        # Harbor catches SIGTERM, cancels trials and brings down their containers.
        await asyncio.wait_for(process.wait(), timeout=30)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def read_result(call_dir: Path) -> tuple[str, dict[str, int]]:
    """CLI 的零退出码不代表所有 trial 成功，必须同时检查 native result。"""
    native_results = list((call_dir / "runs" / call_dir.name).glob("*/result.json"))
    if len(native_results) != 1:
        raise ReviewAgentError("Agent 未产生唯一的 Harbor trial 结果，请查看执行日志")
    native_path = native_results[0]
    if native_path.is_symlink() or not native_path.resolve().is_relative_to(call_dir.resolve()):
        raise ReviewAgentError("Agent 结果路径越界")
    native = json.loads(native_path.read_text(encoding="utf-8"))
    error = native.get("exception_info")
    if error:
        raise ReviewAgentError(
            f"Agent 执行失败：{error.get('exception_type', '')} {error.get('exception_message', '')}"[:1200]
        )
    result_path = native_path.parent / "agent" / "review-result.json"
    if result_path.is_symlink() or not result_path.resolve().is_relative_to(call_dir.resolve()):
        raise ReviewAgentError("Agent 结论文件路径越界")
    if not result_path.is_file():
        raise ReviewAgentError("Agent 已退出，但未写入 /logs/agent/review-result.json，请查看工具轨迹")
    if result_path.stat().st_size > MAX_RESULT_BYTES:
        raise ReviewAgentError("Agent 结论文件超过 8 MB 上限")
    text = result_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ReviewAgentError("Agent 结论文件为空")
    result = native.get("agent_result") or {}
    usage = {
        name: result[key] for name, key in (("prompt_tokens", "n_input_tokens"), ("completion_tokens", "n_output_tokens"))
        if isinstance(result.get(key), int)
    }
    return text, usage


async def run(
    item: ReviewTask, *, system: str, user: str, tag: str, attempt: int,
) -> tuple[str, dict[str, int]]:
    base = settings.reviews_dir / item.workspace
    (base / "agents").mkdir(parents=True, exist_ok=True, mode=0o700)
    call_dir = base / "agents" / f"review-{uuid.uuid4().hex[:16]}"
    call_dir.mkdir(parents=True, mode=0o700)
    secrets: list[str] = []
    try:
        # Builder 只构造 argv / 文件，不在后台线程中启动进程；取消时不存在漏登记的子进程。
        build = review_builder.build(item, call_dir, base, system=system, user=user)
        secrets = build.secrets
        work = Path(build.cwd)
        work.mkdir(parents=True, exist_ok=True)
        Path(build.output_dir).mkdir(parents=True, exist_ok=True)
        for name, content in build.files.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (call_dir / "command.json").write_text(json.dumps({
            "agent": item.agent, "tag": tag, "attempt": attempt,
            "command": [_mask(part, secrets) for part in build.command], "env": build.masked_env(), "notes": build.notes,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        env = {**os.environ, **build.env, "CUDA_VISIBLE_DEVICES": "", "PYTHONUNBUFFERED": "1"}
        proc_env.with_interpreter_on_path(env, build.command[0])
        log_path = call_dir / "stdout.log"
        with log_path.open("w", encoding="utf-8") as log:
            log_path.chmod(0o600)
            log.write(f"Agent: {item.agent} · {tag} · attempt {attempt}\n")
            for note in build.notes:
                log.write(_mask(note, secrets) + "\n")
            log.flush()
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *build.command, cwd=build.cwd, env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=10 * 1024 * 1024,
            ))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # subprocess creation can finish just as the request is cancelled.
                process = await spawn
                await _stop(process)
                raise

            async def supervise() -> int:
                assert process.stdout is not None
                while line := await process.stdout.readline():
                    log.write(_mask(line.decode("utf-8", errors="replace"), secrets))
                    log.flush()
                return await process.wait()

            try:
                try:
                    created = psutil.Process(process.pid).create_time()
                except psutil.NoSuchProcess:
                    created = 0  # 极短命的失败进程已退出，无需重启回收。
                (call_dir / "process.json").write_text(json.dumps({
                    "pid": process.pid, "create_time": created,
                }), encoding="utf-8")
                code = await asyncio.wait_for(supervise(), timeout=settings.review_agent_timeout_seconds + 1260)
            except BaseException:
                await _stop(process)
                raise
        if code:
            raise ReviewAgentError(f"{item.agent} / Harbor 退出码 {code}，请查看 Agent 执行日志")
        return read_result(call_dir)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ReviewAgentError(_mask(f"{type(exc).__name__}: {exc}", secrets)) from exc


async def reap_orphans(item: ReviewTask) -> None:
    """重启后仅清理此审核曾启动且 PID/创建时间/命令都匹配的进程，避免后台继续花额度。"""
    base = settings.reviews_dir / item.workspace / "agents"
    for path in base.glob("review-*/process.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            process = psutil.Process(data["pid"])
            if abs(process.create_time() - data["create_time"]) > 0.01:
                continue
            runtime = str(path.parent / "work" / "harbor_runtime.py")
            if runtime not in process.cmdline() or os.getpgid(process.pid) != process.pid:
                continue
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, timeout=30)
            except psutil.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        except (psutil.NoSuchProcess, ProcessLookupError, FileNotFoundError):
            continue


def log_tail(item: ReviewTask, tail_bytes: int) -> str:
    directory = settings.reviews_dir / item.workspace / "agents"
    if not directory.is_dir():
        return ""
    paths = sorted(directory.glob("review-*/stdout.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    parts: list[str] = []
    remaining = tail_bytes
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - remaining))
            data = handle.read(remaining)
        parts.append(data.decode("utf-8", errors="replace"))
        remaining -= len(data)
        if remaining <= 0:
            break
    return "\n".join(reversed(parts))
