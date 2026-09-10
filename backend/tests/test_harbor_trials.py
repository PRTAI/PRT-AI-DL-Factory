"""逐 trial 阶段推断：按 Harbor 在 Docker bind-mount 目录里留下的文件判断。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app.services import harbor_registry_service, harbor_trials


def _trial(job_dir: Path, name: str, task: str) -> Path:
    trial = job_dir / name
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "config.json").write_text(
        json.dumps({"trial_name": name, "task": {"name": task}, "agent": {"name": "codex", "model_name": "m"}}),
        encoding="utf-8",
    )
    return trial


def test_phases_from_disk(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    # 清单里 5 个 trial：1 个还没开始
    (job_dir / "lock.json").write_text(
        json.dumps({"trials": [{"task": {"name": f"t{i}"}, "agent": {"name": "codex"}} for i in range(5)]}),
        encoding="utf-8",
    )
    _trial(job_dir, "t0__a", "t0")  # 只有 config：启动容器
    t1 = _trial(job_dir, "t1__b", "t1")
    (t1 / "agent" / "setup").mkdir()  # 安装 agent
    t2 = _trial(job_dir, "t2__c", "t2")
    (t2 / "agent" / "setup").mkdir()
    (t2 / "agent" / "codex.txt").write_text("thinking...", encoding="utf-8")  # agent 在跑
    t3 = _trial(job_dir, "t3__d", "t3")
    (t3 / "agent" / "codex.txt").write_text("done", encoding="utf-8")
    (t3 / "verifier" / "test-stdout.txt").write_text("running tests", encoding="utf-8")  # 验证中

    states = {s.task_name: s for s in harbor_trials.scan(job_dir)}
    assert states["t0"].phase == "setup_environment" and states["t0"].detail == "启动容器"
    assert states["t1"].phase == "setup_environment" and states["t1"].detail == "安装 agent"
    assert states["t2"].phase == "running_agent"
    assert states["t3"].phase == "running_verifier"
    assert states["t4"].phase == "pending"

    counts = harbor_trials.counts(list(states.values()))
    assert counts["total"] == 5 and counts["pending"] == 1 and counts["running_agent"] == 1
    assert counts["scored"] == 0
    assert "agent 1" in harbor_trials.progress_message(counts)

    # 完成：写 result.json 后变 success/failed，带 reward 与阶段耗时
    (t3 / "result.json").write_text(json.dumps({
        "trial_name": "t3__d", "task_name": "ds/t3",
        "agent_info": {"name": "codex", "model_info": {"name": "m"}},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:10:00Z",
        "environment_setup": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:30Z"},
        "agent_execution": {"started_at": "2026-01-01T00:01:00Z", "finished_at": "2026-01-01T00:08:00Z"},
        "verifier": {"started_at": "2026-01-01T00:08:00Z", "finished_at": "2026-01-01T00:10:00Z"},
    }), encoding="utf-8")
    done = {s.task_name: s for s in harbor_trials.scan(job_dir)}["ds/t3"]
    assert done.phase == "success" and done.passed is True and done.reward == 1.0
    assert harbor_trials.counts(harbor_trials.scan(job_dir))["scored"] == 1
    assert done.phase_seconds["running_agent"] == 420.0
    assert done.duration_seconds == 600.0
    # 带前缀的 task_name 也要能和清单对上，pending 仍只有 t4
    assert harbor_trials.counts(harbor_trials.scan(job_dir))["pending"] == 1

    page = harbor_trials.page(job_dir, page_size=2, phase="running")
    assert page["total"] == 3 and page["pages"] == 2
    assert page["items"][0]["phase"] == "running_agent"  # 跑着的里 agent 最靠前（verifier 已完成）

    detail = harbor_trials.detail(job_dir, "t2__c")
    assert detail and detail["agent_logs"][0]["content"] == "thinking..."
    assert harbor_trials.detail(job_dir, "../job") is None


def test_registry_summary_and_offline_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(harbor_registry_service, "cache_path", lambda: tmp_path / "reg.json")
    raw = [{"name": "terminal-bench", "version": "2.0", "description": "tb", "tasks": [{}, {}, {}]},
           {"name": "aime", "version": "1.0", "description": "math", "tasks": [{}]}]
    monkeypatch.setattr(harbor_registry_service, "_fetch", lambda: harbor_registry_service._summarize(raw))
    payload = harbor_registry_service.list_datasets(force_refresh=True, wait=True)
    assert payload["source"] == "network"
    assert [d["ref"] for d in payload["datasets"]] == ["aime@1.0", "terminal-bench@2.0"]
    assert payload["datasets"][1]["task_count"] == 3

    def boom():
        raise OSError("offline")

    monkeypatch.setattr(harbor_registry_service, "_fetch", boom)
    cached = harbor_registry_service.list_datasets(force_refresh=True, wait=True)
    assert cached["source"] == "cache" and len(cached["datasets"]) == 2 and "offline" in cached["error"]


def test_registry_url_matches_harbor_constant():
    import glob

    hits = glob.glob("/home/user/.conda/envs/harbor/lib/python3*/site-packages/harbor/constants.py")
    if not hits:
        return
    text = Path(hits[0]).read_text(encoding="utf-8")
    assert harbor_registry_service.REGISTRY_URL in text.replace("\n", "").replace(" ", "").replace('""', "") or \
        harbor_registry_service.REGISTRY_URL.split("//")[1] in text
