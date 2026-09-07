"""Turn framework-specific training/eval output into progress + metric curves.

Each parser is defensive: frameworks change their log shapes between releases and
a monitoring feature must never take down a running job.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from ..models import Framework, Job, JobMetricsSummary, JobProgress, utcnow

MAX_POINTS = 1500
_TAIL_BYTES = 4_000_000

# key -> friendly series name for verl's `step:N - a:1 - b:2` console format
_VERL_SERIES = {
    "critic/score/mean": "score",
    "critic/rewards/mean": "reward",
    "actor/pg_loss": "pg_loss",
    "actor/entropy": "entropy",
    "actor/kl_loss": "kl_loss",
    "actor/grad_norm": "grad_norm",
    "actor/lr": "lr",
    "response_length/mean": "response_length",
    "perf/throughput": "throughput",
    "val-core/acc": "val_acc",
}


class ParsedMetrics:
    __slots__ = ("progress", "summary", "series")

    def __init__(self) -> None:
        self.progress = JobProgress()
        self.summary = JobMetricsSummary(updated_at=utcnow())
        self.series: dict[str, list[list[float]]] = {}

    def add(self, name: str, step: float, value: Any) -> None:
        if value is None:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if numeric != numeric:  # NaN
            return
        self.series.setdefault(name, []).append([float(step), numeric])

    def finish(self) -> "ParsedMetrics":
        for name, points in self.series.items():
            if len(points) > MAX_POINTS:
                stride = len(points) // MAX_POINTS + 1
                # always keep the last point so the curve ends where the job is
                self.series[name] = points[::stride] + [points[-1]]
        return self


def _read_tail(path: Path, limit: int = _TAIL_BYTES) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
            handle.readline()  # drop the partial first line
        return handle.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# LLaMA-Factory
# --------------------------------------------------------------------------- #
def parse_llamafactory(output_dir: Path) -> ParsedMetrics:
    parsed = ParsedMetrics()
    log_path = output_dir / "trainer_log.jsonl"
    if not log_path.is_file():
        return parsed

    last: dict[str, Any] = {}
    # LLaMA-Factory appends a final progress-only record (no loss/lr) when the
    # run ends, so "the last record" is not the last *value* of each metric.
    latest: dict[str, float] = {}
    losses: list[float] = []
    for line in _read_tail(log_path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        step = record.get("current_steps", 0)
        for key in ("loss", "eval_loss", "lr", "reward", "accuracy", "throughput"):
            value = record.get(key)
            parsed.add(key, step, value)
            if isinstance(value, (int, float)):
                latest[key] = float(value)
        if isinstance(record.get("loss"), (int, float)):
            losses.append(float(record["loss"]))
        last = record

    if last:
        total = int(last.get("total_steps") or 0)
        current = int(last.get("current_steps") or 0)
        parsed.progress = JobProgress(
            percent=float(last.get("percentage") or (current / total * 100 if total else 0.0)),
            current_step=current,
            total_steps=total,
            current_epoch=float(last.get("epoch") or 0.0),
            eta_seconds=_parse_hms(last.get("remaining_time")),
            throughput=last.get("throughput"),
            message=f"step {current}/{total}" if total else f"step {current}",
        )
        parsed.summary = JobMetricsSummary(
            last_loss=latest.get("loss"),
            best_loss=min(losses) if losses else None,
            last_reward=latest.get("reward"),
            learning_rate=latest.get("lr"),
            accuracy=latest.get("accuracy"),
            tokens={"total": int(last["total_tokens"])} if last.get("total_tokens") else {},
            updated_at=utcnow(),
        )
    return parsed.finish()


def _parse_hms(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(?:(\d+) days?, )?(\d+):(\d{2}):(\d{2})", value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    return (
        int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    )


# --------------------------------------------------------------------------- #
# verl
# --------------------------------------------------------------------------- #
_VERL_LINE = re.compile(r"step:(\d+)\s+-\s+(.*)$")


def parse_verl(log_file: Path, total_steps_hint: int = 0) -> ParsedMetrics:
    parsed = ParsedMetrics()
    last_step = 0
    last_values: dict[str, float] = {}
    rewards: list[float] = []

    for line in _read_tail(log_file).splitlines():
        match = _VERL_LINE.search(line)
        if not match:
            continue
        step = int(match.group(1))
        last_step = max(last_step, step)
        for chunk in match.group(2).split(" - "):
            if ":" not in chunk:
                continue
            key, _, raw = chunk.rpartition(":")
            key, raw = key.strip(), raw.strip()
            name = _VERL_SERIES.get(key)
            if name is None:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            parsed.add(name, step, value)
            last_values[name] = value
            if name == "reward":
                rewards.append(value)

    if last_step:
        parsed.progress = JobProgress(
            percent=(last_step / total_steps_hint * 100) if total_steps_hint else 0.0,
            current_step=last_step,
            total_steps=total_steps_hint,
            message=f"global step {last_step}" + (f"/{total_steps_hint}" if total_steps_hint else ""),
        )
        parsed.summary = JobMetricsSummary(
            last_reward=last_values.get("reward"),
            best_reward=max(rewards) if rewards else None,
            last_loss=last_values.get("pg_loss"),
            learning_rate=last_values.get("lr"),
            grad_norm=last_values.get("grad_norm"),
            accuracy=last_values.get("val_acc"),
            updated_at=utcnow(),
        )
    return parsed.finish()


# --------------------------------------------------------------------------- #
# TRL (HuggingFace Trainer)
# --------------------------------------------------------------------------- #
_HF_DICT = re.compile(r"\{'loss':.*?\}|\{'train_runtime':.*?\}|\{'reward':.*?\}")


def parse_trl(output_dir: Path, log_file: Path) -> ParsedMetrics:
    parsed = ParsedMetrics()
    history: list[dict[str, Any]] = []
    global_step = 0
    max_steps = 0
    epoch = 0.0

    state_files = sorted(output_dir.glob("**/trainer_state.json"), key=lambda p: p.stat().st_mtime)
    if state_files:
        try:
            state = json.loads(state_files[-1].read_text(encoding="utf-8"))
            history = state.get("log_history", [])
            global_step = int(state.get("global_step") or 0)
            max_steps = int(state.get("max_steps") or 0)
            epoch = float(state.get("epoch") or 0.0)
        except (json.JSONDecodeError, OSError):
            history = []

    if not history:  # fall back to the console stream
        for match in _HF_DICT.finditer(_read_tail(log_file)):
            try:
                history.append(ast.literal_eval(match.group(0)))
            except (ValueError, SyntaxError):
                continue
        global_step = len(history)

    losses: list[float] = []
    rewards: list[float] = []
    last: dict[str, Any] = {}
    # HF's final log entry only carries train_runtime/samples_per_second, so keep
    # the last *observed* value of every metric rather than the last record's.
    latest: dict[str, float] = {}
    for index, record in enumerate(history, start=1):
        step = record.get("step", index)
        for key, name in (
            ("loss", "loss"),
            ("eval_loss", "eval_loss"),
            ("learning_rate", "lr"),
            ("grad_norm", "grad_norm"),
            ("reward", "reward"),
            ("rewards/outcome_reward", "reward"),
            ("kl", "kl"),
            ("completions/mean_length", "response_length"),
        ):
            if key in record:
                parsed.add(name, step, record[key])
                if isinstance(record[key], (int, float)):
                    latest[key] = float(record[key])
        if isinstance(record.get("loss"), (int, float)):
            losses.append(float(record["loss"]))
        for reward_key in ("reward", "rewards/outcome_reward"):
            if isinstance(record.get(reward_key), (int, float)):
                rewards.append(float(record[reward_key]))
        last = record

    if last:
        parsed.progress = JobProgress(
            percent=(global_step / max_steps * 100) if max_steps else 0.0,
            current_step=global_step,
            total_steps=max_steps,
            current_epoch=float(last.get("epoch", epoch) or 0.0),
            message=f"step {global_step}" + (f"/{max_steps}" if max_steps else ""),
        )
        parsed.summary = JobMetricsSummary(
            last_loss=latest.get("loss"),
            best_loss=min(losses) if losses else None,
            last_reward=rewards[-1] if rewards else None,
            best_reward=max(rewards) if rewards else None,
            learning_rate=latest.get("learning_rate"),
            grad_norm=latest.get("grad_norm"),
            updated_at=utcnow(),
        )
    return parsed.finish()


# --------------------------------------------------------------------------- #
# Harbor
# --------------------------------------------------------------------------- #
def _trial_result_files(job_dir: Path, limit: int = 5000) -> list[Path]:
    """Harbor writes ``<trial>/result.json``.

    Its own ``TrialPaths`` docstring says ``results.json``; the runtime writes the
    singular form. Accept both so a future rename does not silently empty the
    trial table.
    """
    files: list[Path] = []
    for pattern in ("*/result.json", "*/results.json", "*/*/result.json", "*/*/results.json"):
        for path in job_dir.glob(pattern):
            files.append(path)
            if len(files) >= limit:
                return files
    return files


def harbor_job_dir(output_dir: Path, job_id: str) -> Path | None:
    candidate = output_dir / job_id
    if candidate.is_dir():
        return candidate
    if not output_dir.is_dir():
        return None
    dirs = [p for p in output_dir.iterdir() if p.is_dir() and (p / "config.json").is_file()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def parse_harbor(output_dir: Path, job_id: str) -> ParsedMetrics:
    parsed = ParsedMetrics()
    job_dir = harbor_job_dir(output_dir, job_id)
    if job_dir is None:
        return parsed

    result_path = job_dir / "result.json"
    stats: dict[str, Any] = {}
    if result_path.is_file():
        try:
            stats = (json.loads(result_path.read_text(encoding="utf-8")) or {}).get("stats", {}) or {}
        except (json.JSONDecodeError, OSError):
            stats = {}

    # Harbor's JobStats.increment() bumps n_completed_trials for *every* finished
    # trial and additionally bumps n_errored_trials when it raised — errored is a
    # subset of completed, so adding them together would double count.
    completed = int(stats.get("n_completed_trials") or 0)
    errored = int(stats.get("n_errored_trials") or 0)
    running = int(stats.get("n_running_trials") or 0)
    pending = int(stats.get("n_pending_trials") or 0)
    total = completed + running + pending

    if total == 0:  # result.json not written yet — count trial dirs on disk
        completed = len(_trial_result_files(job_dir))
        total = completed

    pass_at_k: dict[str, float] = {}
    rewards: list[float] = []
    for eval_stats in (stats.get("evals") or {}).values():
        if not isinstance(eval_stats, dict):
            continue
        for k, value in (eval_stats.get("pass_at_k") or {}).items():
            pass_at_k[f"pass@{k}"] = float(value)
        # reward_stats only holds trials that produced a reward; trials that blew
        # up (timeout, container failure) never land here.
        for _metric, buckets in (eval_stats.get("reward_stats") or {}).items():
            for reward_value, trial_ids in (buckets or {}).items():
                try:
                    rewards.extend([float(reward_value)] * len(trial_ids or []))
                except (TypeError, ValueError):
                    continue

    resolved = None
    if rewards or completed:
        # A trial that errored is a failure, not a missing sample: divide by the
        # number of finished trials, otherwise a run where 9/10 tasks crashed
        # would proudly report 100%.
        denominator = max(completed, len(rewards))
        if denominator:
            resolved = sum(1 for r in rewards if r >= 1.0) / denominator
    if rewards:
        distribution: dict[float, int] = {}
        for reward in rewards:
            distribution[reward] = distribution.get(reward, 0) + 1
        for reward_value, count in sorted(distribution.items()):
            parsed.add("reward_distribution", reward_value, count)

    parsed.progress = JobProgress(
        # errored trials are already inside `completed`
        percent=completed / total * 100 if total else 0.0,
        completed_trials=completed,
        total_trials=total,
        message=f"trials {completed}/{total}" if total else "准备评测环境…",
    )
    parsed.summary = JobMetricsSummary(
        pass_at_k=pass_at_k,
        n_trials=completed,
        n_errors=errored,
        resolved_rate=resolved,
        last_reward=rewards[-1] if rewards else None,
        best_reward=max(rewards) if rewards else None,
        cost_usd=stats.get("cost_usd"),
        tokens={
            key: int(stats[field])
            for key, field in (
                ("input", "n_input_tokens"),
                ("output", "n_output_tokens"),
                ("cache", "n_cache_tokens"),
            )
            if isinstance(stats.get(field), int)
        },
        updated_at=utcnow(),
    )
    return parsed.finish()


# --------------------------------------------------------------------------- #
def parse_job(job: Job) -> ParsedMetrics:
    output_dir = Path(job.runtime.output_dir or "")
    log_file = Path(job.runtime.log_file or "")
    try:
        if job.framework == Framework.LLAMAFACTORY:
            return parse_llamafactory(output_dir)
        if job.framework == Framework.VERL:
            hint = job.rl.total_training_steps if job.rl and job.rl.total_training_steps else 0
            return parse_verl(log_file, hint or 0)
        if job.framework == Framework.TRL:
            return parse_trl(output_dir, log_file)
        if job.framework == Framework.HARBOR:
            return parse_harbor(output_dir, job.id)
    except Exception:  # pragma: no cover - monitoring must never break a job
        return ParsedMetrics()
    return ParsedMetrics()


def write_series(work_dir: Path, parsed: ParsedMetrics) -> None:
    if not parsed.series:
        return
    payload = {
        "series": parsed.series,
        "summary": parsed.summary.model_dump(mode="json"),
        "progress": parsed.progress.model_dump(mode="json"),
    }
    target = work_dir / "metrics.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def read_series(work_dir: Path) -> dict[str, Any]:
    path = work_dir / "metrics.json"
    if not path.is_file():
        return {"series": {}, "summary": {}, "progress": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"series": {}, "summary": {}, "progress": {}}
