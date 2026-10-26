"""Run exact TrialConfigs in their original directory through Harbor's queue."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from uuid import uuid4


def _preflight_agent(factory, config) -> None:
    """Validate an agent before old results are moved.

    Older Harbor exposed ``AgentFactory.run_preflight``; current releases
    resolve agents in ``Trial.create`` only, so build the agent instance here
    instead (same class/env resolution, no container or run).
    """
    run_preflight = getattr(factory, "run_preflight", None)
    if callable(run_preflight):
        run_preflight(config)
        return
    logs_dir = Path(tempfile.gettempdir()) / "prt-agent-preflight"
    factory.create_agent_from_config(config, logs_dir=logs_dir)


def write_json(path: Path, value: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def read_results(root: Path):
    from harbor.models.trial.result import TrialResult
    results = []
    for path in sorted(root.glob("*/result.json")):
        try:
            results.append(TrialResult.model_validate_json(path.read_text()))
        except (ValueError, OSError):
            # An interrupted, unselected record must remain untouched.
            continue
    return results


def refresh_result(root, original, total, metrics, *, finished=False):
    from harbor.models.job.result import JobResult, JobStats
    from harbor.utils.pass_at_k import compute_pass_at_k_by_evals

    results = read_results(root)
    stats = JobStats.from_trial_results(results, n_total_trials=total)
    rewards = defaultdict(list)
    sources = {}
    for result in results:
        model = result.agent_info.model_info
        source = result.source or "adhoc"
        key = JobStats.format_agent_evals_key(result.agent_info.name, model.name if model else None, source)
        sources[key] = source
        rewards[key].append(result.verifier_result.rewards if result.verifier_result else None)
    for key, values in rewards.items():
        stats.evals[key].metrics = [metric.compute(values) for metric in metrics[sources[key]]]
    for key, values in compute_pass_at_k_by_evals(results).items():
        stats.evals[key].pass_at_k = values
    now = datetime.now()
    result = JobResult(id=original.id if original else uuid4(),
                       started_at=original.started_at if original else now,
                       updated_at=now, finished_at=now if finished else None,
                       n_total_trials=total, stats=stats)
    write_json(root / "result.json", result.model_dump_json(indent=4, exclude={"trial_results"}))


async def run(plan: dict) -> None:
    from harbor.agents.factory import AgentFactory
    from harbor.environments.factory import EnvironmentFactory
    from harbor.job import Job
    from harbor.models.job.config import JobConfig
    from harbor.models.job.result import JobResult, JobStats
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.queue import TrialQueue

    root, backup = Path(plan["root"]), Path(plan["backup"])
    config = JobConfig.model_validate_json((root / "config.json").read_text())
    trials = [TrialConfig.model_validate(row) for row in plan["trials"]]
    names = [trial.trial_name for trial in trials]
    if len(set(names)) != len(names) or any(not name or name in {".", ".."}
                                            or "/" in name or "\\" in name for name in names):
        raise ValueError("Invalid or duplicate trial name")
    if any(trial.trials_dir.resolve() != root.resolve() for trial in trials):
        raise ValueError("Trial directory must be the original job directory")
    if backup.exists():
        raise ValueError("This rerun operation already started; select the records again to retry")
    # All preflight and metric resolution happen before any old result is moved.
    for trial in trials:
        _preflight_agent(AgentFactory, trial.agent)
        EnvironmentFactory.validate_resource_policies(trial.environment)
    records = read_results(root)
    metrics = await Job._resolve_metrics(config, [r.config.task for r in records] + [t.task for t in trials])
    original = None
    if (root / "result.json").is_file():
        try:
            original = JobResult.model_validate_json((root / "result.json").read_text())
        except ValueError:
            pass
    lock = json.loads((root / "lock.json").read_text()) if (root / "lock.json").exists() else {}
    physical = {p.name for p in root.iterdir() if p.is_dir()}
    total = max(len(lock.get("trials", [])), original.n_total_trials if original else 0,
                len(physical | set(names)))
    if original is None:
        original = JobResult(id=uuid4(), started_at=datetime.now(), n_total_trials=total, stats=JobStats())
    backup.mkdir(parents=True)
    if (root / "result.json").exists():
        shutil.copy2(root / "result.json", backup / "result.json")
    moved, created = [], []
    try:
        for trial in trials:
            path = root / trial.trial_name
            if path.exists():
                shutil.move(str(path), str(backup / trial.trial_name))
                moved.append(trial.trial_name)
            path.mkdir()
            created.append(path)
            (path / "config.json").write_text(trial.model_dump_json())
    except BaseException:
        for path in created:
            shutil.rmtree(path)
        for name in moved:
            shutil.move(str(backup / name), str(root / name))
        shutil.rmtree(backup)
        raise

    def persist_recorded_configs(completed_name: str | None = None):
        # Credentials are injected only for execution. Keep recorded config equality
        # with the original job plan so later native whole-job resume still works.
        for name, canonical in plan.get("recorded_configs", {}).items():
            if completed_name is not None and name != completed_name:
                continue
            path = root / name
            if not path.is_dir():
                continue
            write_json(path / "config.json", json.dumps(canonical))
            result_path = path / "result.json"
            if result_path.is_file():
                try:
                    data = json.loads(result_path.read_text())
                    data["config"] = canonical
                    write_json(result_path, json.dumps(data))
                except (ValueError, OSError):
                    continue

    async def refresh(event):
        persist_recorded_configs(event.trial_name)
        refresh_result(root, original, total, metrics)

    queue = TrialQueue(n_concurrent=config.n_concurrent_trials, retry_config=config.retry)
    queue.add_hook(TrialEvent.END, refresh)
    refresh_result(root, original, total, metrics)
    print(f"PRT: 原地重跑 {len(trials)} 条数据；原结果备份于 {backup}", flush=True)
    finished = False
    try:
        tasks = []
        async with asyncio.TaskGroup() as group:
            for coroutine in queue.submit_batch(trials):
                tasks.append(group.create_task(coroutine))
        for task in tasks:
            task.result()  # Propagate cancellation even if the group itself was not cancelled.
        finished = True
    finally:
        persist_recorded_configs()
        refresh_result(root, original, total, metrics, finished=finished)


async def entry(plan: dict) -> None:
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    await run(plan)


if __name__ == "__main__":
    from harbor_runtime import install_guards
    install_guards()
    asyncio.run(entry(json.loads(Path(sys.argv[1]).read_text())))
