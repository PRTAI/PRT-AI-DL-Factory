"""评测任务的逐 trial 状态：从 Harbor 的 trial 目录推断阶段。

Harbor 的进度条是 rich Live，在非终端里一个字都不打，所以「每个任务现在到哪一步了」
不能靠 stdout。好在 Docker 环境把每个 trial 的 ``agent/`` 与 ``verifier/`` **bind-mount**
进容器，文件是实时出现的：

    <trial>/config.json + lock.json     trial 开始（容器启动中）
    <trial>/agent/setup/                容器起来了，正在安装 agent
    <trial>/agent/<agent>.txt、sessions/ agent 开始干活
    <trial>/verifier/*                  verifier 开始跑测试
    <trial>/result.json                 结束：有 exception_info = 失败，否则 = 完成（附 reward）

trial 结果损坏时，优先从 job 级 result.json 的 trial_results 或统计桶恢复；
任务已经结束时，无法恢复的 trial 显示读取错误，不能继续推断为运行中。

尚未在磁盘上出现的 trial 从 job 级 ``lock.json`` 的 trials 清单里补成 pending。
全部是防御式读取：任何异常都当成「读不到」，绝不能影响正在跑的任务。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PHASES = ["pending", "setup_environment", "running_agent", "running_verifier", "success", "failed"]
_RUNNING = {"setup_environment", "running_agent", "running_verifier"}
_SAFE_NAME = re.compile(r"[^/\\\x00-\x1f\x7f]+")
_TERMINAL = {"succeeded", "failed", "cancelled", "rejected"}
_TAIL_BYTES = 64_000


@dataclass
class TrialState:
    trial_name: str
    task_name: str
    phase: str
    detail: str = ""  # 子状态：启动容器 / 安装 agent / …
    reward: float | None = None
    rewards: dict[str, Any] = field(default_factory=dict)
    passed: bool | None = None
    exception_type: str | None = None
    exception_message: str = ""
    agent: str = ""
    model: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    # 各阶段耗时（秒）；跑完的 trial 用 result.json 的精确时间，跑着的用文件 mtime 估
    phase_seconds: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    path: str = ""


def _result_file(trial_dir: Path) -> Path | None:
    """Harbor 写的是 result.json；它自己的 TrialPaths 文档写成 results.json，两种都认。"""
    for name in ("result.json", "results.json"):
        if (trial_dir / name).is_file():
            return trial_dir / name
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(block: dict[str, Any] | None) -> float | None:
    if not isinstance(block, dict) or not block:
        return None
    start, end = _parse_iso(block.get("started_at")), _parse_iso(block.get("finished_at"))
    if start is None:
        return None
    return ((end or datetime.now(timezone.utc)) - start).total_seconds()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _primary_reward(rewards: dict[str, Any]) -> float | None:
    if "reward" in rewards and isinstance(rewards["reward"], (int, float)):
        return float(rewards["reward"])
    numeric = [v for v in rewards.values() if isinstance(v, (int, float))]
    return sum(numeric) / len(numeric) if numeric else None


def _from_result(trial_dir: Path, payload: dict[str, Any]) -> TrialState:
    rewards = ((payload.get("verifier_result") or {}).get("rewards")) or {}
    reward = _primary_reward(rewards)
    exception = payload.get("exception_info") or {}
    agent_info = payload.get("agent_info") or {}
    agent_result = payload.get("agent_result") or {}
    started, finished = payload.get("started_at"), payload.get("finished_at")
    verifier_error = bool(rewards.get("verifier_error"))
    phase = "failed" if exception or verifier_error else "success"
    if verifier_error and not exception:
        exception = {"exception_type": "VerifierError", "exception_message": "判分流程失败，reward 是失败兜底值，并非有效评分"}
    tokens = {
        key: int(agent_result[src])
        for key, src in (("input", "n_input_tokens"), ("output", "n_output_tokens"), ("cache", "n_cache_tokens"))
        if isinstance(agent_result.get(src), int)
    }
    phase_seconds = {
        name: seconds
        for name, key in (
            ("setup_environment", "environment_setup"),
            ("agent_setup", "agent_setup"),
            ("running_agent", "agent_execution"),
            ("running_verifier", "verifier"),
        )
        if (seconds := _seconds(payload.get(key))) is not None
    }
    return TrialState(
        # 详情路径必须使用真实目录名，不能用可能被 Harbor 脱敏改写的名称。
        trial_name=trial_dir.name,
        task_name=(payload.get("task_name") if payload.get("task_name") and isinstance(payload.get("task_name"), str)
                   and "[REDACTED]" not in payload["task_name"] else trial_dir.name.split("__", 1)[0]),
        phase=phase,
        detail=("通过" if reward is not None and reward >= 1 else "未通过") if phase == "success" else (exception.get("exception_type") or "失败"),
        reward=reward,
        rewards=rewards,
        passed=(reward >= 1) if reward is not None else None,
        exception_type=exception.get("exception_type"),
        exception_message=(exception.get("exception_message") or "")[:600],
        agent=agent_info.get("name", ""),
        model=(agent_info.get("model_info") or {}).get("name", "") or "",
        started_at=started,
        finished_at=finished,
        duration_seconds=_seconds({"started_at": started, "finished_at": finished}),
        phase_seconds=phase_seconds,
        tokens=tokens,
        path=str(trial_dir),
    )


def _from_live_dir(trial_dir: Path, *, finished_at: str | None = None) -> TrialState:
    config = _read_json(trial_dir / "config.json") or {}
    agent_cfg = config.get("agent") or {}
    task_cfg = config.get("task") or {}
    started_ts = _mtime(trial_dir / "config.json") or _mtime(trial_dir)
    now = (_parse_iso(finished_at) or datetime.now(timezone.utc)).timestamp()

    agent_dir = trial_dir / "agent"
    verifier_dir = trial_dir / "verifier"
    setup_dir = agent_dir / "setup"

    def entries(path: Path, skip: str = "") -> list[Path]:
        try:
            return [p for p in path.iterdir() if p.name != skip]
        except OSError:
            return []

    verifier_files = entries(verifier_dir)
    agent_files = entries(agent_dir, skip="setup")

    phase, detail = "setup_environment", "启动容器"
    phase_seconds: dict[str, float] = {}
    env_ready = _mtime(setup_dir)
    agent_start = min((m for p in agent_files if (m := _mtime(p)) is not None), default=None)
    verifier_start = min((m for p in verifier_files if (m := _mtime(p)) is not None), default=None)

    if verifier_files:
        phase, detail = "running_verifier", "运行测试"
        exit_message = _read_json(verifier_dir / "reward_exit_message.json") or {}
        if "placeholder" in str(exit_message.get("exit_reason", "")).lower():
            detail = "判分执行中（已有失败占位文件，等待最终结果）"
    elif agent_files:
        phase, detail = "running_agent", "agent 执行中"
    elif setup_dir.exists():
        phase, detail = "setup_environment", "安装 agent"

    if started_ts is not None:
        if env_ready is not None:
            phase_seconds["setup_environment"] = max(0.0, env_ready - started_ts)
            if agent_start is not None:
                phase_seconds["agent_setup"] = max(0.0, agent_start - env_ready)
            else:
                phase_seconds["agent_setup"] = max(0.0, now - env_ready)
        else:
            phase_seconds["setup_environment"] = max(0.0, now - started_ts)
        if agent_start is not None:
            phase_seconds["running_agent"] = max(0.0, (verifier_start or now) - agent_start)
        if verifier_start is not None:
            phase_seconds["running_verifier"] = max(0.0, now - verifier_start)

    observation = _read_json(trial_dir / "prt-phase.json") or {}
    observed_phase, observed_start = observation.get("phase"), observation.get("started_at")
    if observed_phase in _RUNNING and isinstance(observed_start, (int, float)) and 0 < observed_start <= now:
        phase = observed_phase
        if phase == "running_agent":
            detail = "agent 执行中"
        elif phase == "running_verifier" and "占位" not in detail:
            detail = "运行测试"
        phase_seconds[phase] = max(0.0, now - observed_start)

    task_name = task_cfg.get("name") or trial_dir.name.split("__", 1)[0]
    return TrialState(
        trial_name=trial_dir.name,
        task_name=str(task_name) if "[REDACTED]" not in str(task_name) else trial_dir.name.split("__", 1)[0],
        phase=phase,
        detail=detail,
        agent=agent_cfg.get("name", ""),
        model=agent_cfg.get("model_name") or "",
        started_at=_iso(started_ts) if started_ts else None,
        finished_at=finished_at,
        duration_seconds=max(0.0, now - started_ts) if started_ts else None,
        phase_seconds=phase_seconds,
        path=str(trial_dir),
    )


@dataclass
class _JobContext:
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminal: bool = False
    finished_at: str | None = None


def _job_context(job_dir: Path, job_status: str, finished_at: str | None) -> _JobContext:
    """任务级结果不在 Harbor 的 trial 脱敏范围内，可用于恢复损坏的 trial。"""
    payload = _read_json(job_dir / "result.json") or {}
    end = payload.get("finished_at") or finished_at
    context = _JobContext(terminal=job_status in _TERMINAL or _parse_iso(end) is not None,
                          finished_at=end if _parse_iso(end) is not None else None)
    results = payload.get("trial_results")
    for result in results if isinstance(results, list) else []:
        if isinstance(result, dict) and isinstance(result.get("trial_name"), str):
            context.results[result["trial_name"]] = result
    stats = payload.get("stats")
    evaluations = stats.get("evals") if isinstance(stats, dict) else None
    for evaluation in (evaluations if isinstance(evaluations, dict) else {}).values():
        if not isinstance(evaluation, dict):
            continue
        rewards = evaluation.get("reward_stats")
        for metric, buckets in (rewards if isinstance(rewards, dict) else {}).items():
            for value, names in (buckets if isinstance(buckets, dict) else {}).items():
                try:
                    reward = float(value)
                except (TypeError, ValueError):
                    continue
                for name in names if isinstance(names, list) else []:
                    if not isinstance(name, str):
                        continue
                    summary = context.summaries.setdefault(name, {})
                    summary.setdefault("verifier_result", {}).setdefault("rewards", {})[metric] = reward
        exceptions = evaluation.get("exception_stats")
        for exception_type, names in (exceptions if isinstance(exceptions, dict) else {}).items():
            for name in names if isinstance(names, list) else []:
                if not isinstance(name, str):
                    continue
                context.summaries.setdefault(name, {})["exception_info"] = {
                    "exception_type": exception_type,
                    "exception_message": "异常类型来自任务级汇总；trial 结果文件无法读取。",
                }
    return context


def _state_for(trial_dir: Path, context: _JobContext) -> TrialState:
    result_path = _result_file(trial_dir)
    result = _read_json(result_path) if result_path else None
    if result:
        try:
            return _from_result(trial_dir, result)
        except (AttributeError, TypeError, ValueError):
            pass
    # 先恢复完整结果，再尝试按真实 trial 名匹配汇总的 reward/exception 桶。
    recovered = context.results.get(trial_dir.name)
    if not recovered and (result_path is not None or context.terminal):
        summary = context.summaries.get(trial_dir.name)
        if summary:
            result_ts = _mtime(result_path) if result_path else None
            recovered = {**summary, "finished_at": context.finished_at or (_iso(result_ts) if result_ts else None)}
    if recovered:
        try:
            state = _from_result(trial_dir, recovered)
            state.detail += "（从任务汇总恢复）"
            return state
        except (AttributeError, TypeError, ValueError):
            pass
    end = context.finished_at
    if context.terminal and not end:
        end_ts = _mtime(result_path or trial_dir / "config.json") or _mtime(trial_dir)
        end = _iso(end_ts) if end_ts is not None else None
    try:
        state = _from_live_dir(trial_dir, finished_at=end)
    except (AttributeError, TypeError, ValueError):
        state = TrialState(trial_name=trial_dir.name, task_name=trial_dir.name.split("__", 1)[0],
                           phase="setup_environment", detail="解析中", finished_at=end, path=str(trial_dir))
    if context.terminal:
        state.phase = "failed"
        state.detail = "结果文件无法读取" if result_path else "任务已结束，trial 未生成结果"
        state.exception_type = "TrialResultReadError" if result_path else "TrialInterrupted"
        state.exception_message = "无法从任务汇总恢复结果；请查看 trial 日志。"
    return state


def _planned_trials(job_dir: Path) -> list[dict[str, Any]]:
    """job 级 lock.json 里的 trials 清单（一开始就写好），用来补出还没开始的。"""
    lock = _read_json(job_dir / "lock.json") or {}
    rows: list[dict[str, Any]] = []
    for entry in lock.get("trials") or []:
        task = (entry or {}).get("task") or {}
        rows.append({"task_name": task.get("name") or "", "agent": ((entry or {}).get("agent") or {}).get("name", ""),
                     "model": ((entry or {}).get("agent") or {}).get("model_name") or ""})
    return rows


def scan(job_dir: Path, *, job_status: str = "", finished_at: str | None = None) -> list[TrialState]:
    """所有 trial 的当前状态（磁盘上有的 + 清单里还没开始的）。"""
    states: list[TrialState] = []
    context = _job_context(job_dir, job_status, finished_at)
    try:
        dirs = [
            p for p in job_dir.iterdir()
            if p.is_dir() and not p.is_symlink() and ((p / "config.json").is_file() or _result_file(p) is not None)
        ]
    except OSError:
        return states
    for trial_dir in sorted(dirs):
        try:
            states.append(_state_for(trial_dir, context))
        except Exception:  # noqa: BLE001 - 解析永远不能拖垮任务页
            states.append(TrialState(trial_name=trial_dir.name, task_name=trial_dir.name.split("__", 1)[0],
                                     phase="failed" if context.terminal else "setup_environment",
                                     detail="结果解析失败" if context.terminal else "解析中",
                                     finished_at=context.finished_at, path=str(trial_dir)))

    # 还没落盘的：按任务名把清单里多出来的次数补成 pending。
    # result.json 里的 task_name 可能带数据集前缀（pc-fullstack/xxx），lock 里是裸名，
    # 按最后一段比。
    planned = _planned_trials(job_dir)
    if planned:
        seen: dict[str, int] = {}
        for state in states:
            key = state.task_name.rsplit("/", 1)[-1]
            seen[key] = seen.get(key, 0) + 1
        for row in planned:
            name = row["task_name"].rsplit("/", 1)[-1]
            if seen.get(name, 0) > 0:
                seen[name] -= 1
                continue
            states.append(TrialState(trial_name=f"{name} (待开始)", task_name=name,
                                     phase="failed" if context.terminal else "pending",
                                     detail="任务已结束，trial 未执行" if context.terminal else "排队中",
                                     exception_type="TrialNotStarted" if context.terminal else None,
                                     finished_at=context.finished_at, agent=row["agent"], model=row["model"]))
    return states


def counts(states: list[TrialState]) -> dict[str, int]:
    out = {phase: 0 for phase in PHASES}
    for state in states:
        out[state.phase] = out.get(state.phase, 0) + 1
    out["passed"] = sum(1 for s in states if s.passed)
    # 有 reward 的 trial 数。Harbor 允许「有 reward 也有异常」（agent 超时但 verifier 仍打了分），
    # 这种算作已打分，分数分布里不能再记一次「异常」。
    out["scored"] = sum(1 for s in states if s.reward is not None)
    out["total"] = len(states)
    return out


def progress_message(c: dict[str, int]) -> str:
    done = c["success"] + c["failed"]
    parts = [f"完成 {done}/{c['total']}"]
    if c["running_agent"]:
        parts.append(f"agent {c['running_agent']}")
    if c["running_verifier"]:
        parts.append(f"验证 {c['running_verifier']}")
    if c["setup_environment"]:
        parts.append(f"环境 {c['setup_environment']}")
    if c["failed"]:
        parts.append(f"失败 {c['failed']}")
    return " · ".join(parts)


_ORDER = {"running_verifier": 0, "running_agent": 1, "setup_environment": 2, "failed": 3, "success": 4, "pending": 5}


def page(
    job_dir: Path,
    *,
    page_no: int = 1,
    page_size: int = 10,
    phase: str = "",
    query: str = "",
    job_status: str = "",
    finished_at: str | None = None,
) -> dict[str, Any]:
    states = scan(job_dir, job_status=job_status, finished_at=finished_at)
    total_counts = counts(states)
    rows = states
    if phase:
        wanted = _RUNNING if phase == "running" else {phase}
        rows = [s for s in rows if s.phase in wanted]
    if query:
        needle = query.lower()
        rows = [s for s in rows if needle in s.task_name.lower() or needle in s.trial_name.lower()
                or needle in (s.exception_type or "").lower()]
    rows.sort(key=lambda s: (_ORDER.get(s.phase, 9), s.started_at or "", s.task_name))
    page_size = max(1, min(page_size, 100))
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page_no = max(1, min(page_no, pages))
    start = (page_no - 1) * page_size
    return {
        "items": [asdict(s) for s in rows[start : start + page_size]],
        "total": total,
        "page": page_no,
        "page_size": page_size,
        "pages": pages,
        "counts": total_counts,
        "job_dir": str(job_dir),
    }


def _tail(path: Path, limit: int = _TAIL_BYTES) -> str:
    try:
        if path.is_symlink():
            return ""
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _nested_logs(root: Path) -> list[dict[str, str]]:
    """Bound both traversal and output; never follow directory symlinks."""
    import os
    if root.is_symlink():
        return []
    candidates: list[Path] = []
    visited = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        visited += 1
        if visited >= 1000:
            break
        directories[:] = sorted(d for d in directories if not (Path(current) / d).is_symlink())[:50]
        if len(Path(current).relative_to(root).parts) >= 6:
            directories[:] = []
        for name in sorted(filenames):
            visited += 1
            path = Path(current) / name
            if not path.is_symlink() and (path.suffix in {".txt", ".log", ".jsonl"} or name in {"reward.json", "reward_exit_message.json", "trajectory.json"}):
                candidates.append(path)
            if visited >= 1000:
                break
        if visited >= 1000:
            break
    # Show lifecycle/score files as well as the most recently updated nested sessions.
    primary = [p for p in candidates if p.parent == root and p.name in {"test-stdout.txt", "test-stderr.txt", "reward.json", "reward.txt", "reward_exit_message.json"}]
    recent = sorted((p for p in candidates if p not in primary), key=lambda p: _mtime(p) or 0, reverse=True)
    return [{"name": str(p.relative_to(root)), "content": _tail(p, 24000)} for p in (primary + recent)[:12]]


def detail(job_dir: Path, trial_name: str, *, job_status: str = "", finished_at: str | None = None) -> dict[str, Any] | None:
    """单个 trial 的详细日志：trial.log、agent 输出、verifier 输出、异常栈。"""
    if trial_name in {".", ".."} or not _SAFE_NAME.fullmatch(trial_name):
        return None
    trial_path = job_dir / trial_name
    if trial_path.is_symlink():
        return None
    try:
        trial_dir = trial_path.resolve()
    except (OSError, RuntimeError):
        return None
    if trial_dir.parent != job_dir.resolve():
        return None
    if not (trial_dir / "config.json").is_file() and _result_file(trial_dir) is None:
        return None
    state = _state_for(trial_dir, _job_context(job_dir, job_status, finished_at))

    agent_logs = _nested_logs(trial_dir / "agent")
    verifier_logs = _nested_logs(trial_dir / "verifier")

    files: list[dict[str, Any]] = []
    try:
        for path in sorted(trial_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                rel = str(path.relative_to(trial_dir))
                files.append({"path": rel, "size_bytes": path.stat().st_size})
                if len(files) >= 200:
                    break
    except OSError:
        pass

    return {
        "trial": asdict(state),
        "trial_log": _tail(trial_dir / "trial.log"),
        "exception": _tail(trial_dir / "exception.txt", 20_000) if (trial_dir / "exception.txt").is_file() else "",
        "agent_logs": agent_logs,
        "verifier_logs": verifier_logs,
        "files": files,
    }
