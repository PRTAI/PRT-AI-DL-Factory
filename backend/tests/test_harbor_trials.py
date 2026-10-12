"""逐 trial 阶段推断：按 Harbor 在 Docker bind-mount 目录里留下的文件判断。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _result(name, reward=0.0):
    return {
        "trial_name": name, "task_name": name.split("__", 1)[0],
        "started_at": "2026-10-10T10:00:00Z", "finished_at": "2026-10-10T10:10:00Z",
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_info": {"name": "nop"}, "timeout_multiplier": 1.0,
    }


def test_scrubbed_trials_recover_from_job_results_without_rewriting(tmp_path):
    results = []
    originals = {}
    for index in range(1, 4):
        name = f"PRT-医疗-{index:03d}-20261003-1__trial{index}"
        trial = _trial(tmp_path, name, name.split("__", 1)[0])
        result = _result(name)
        corrupted = json.dumps(result, ensure_ascii=False).replace("1", "[REDACTED]")
        (trial / "result.json").write_text(corrupted)
        (trial / "config.json").write_text('{"attempt": [REDACTED]}')
        (trial / "verifier" / "test-stdout.txt").write_text("done")
        results.append(result)
        originals[trial / "result.json"] = corrupted
    (tmp_path / "result.json").write_text(json.dumps({
        "finished_at": "2026-10-10T10:11:00Z", "trial_results": results,
    }))
    (tmp_path / "lock.json").write_text(json.dumps({
        "trials": [{"task": {"name": result["task_name"]}} for result in results],
    }))

    page = harbor_trials.page(tmp_path)
    assert page["counts"]["success"] == page["counts"]["scored"] == 3
    assert page["counts"]["pending"] == page["counts"]["running_verifier"] == 0
    assert page["counts"]["passed"] == 0
    assert harbor_trials.page(tmp_path, phase="running")["total"] == 0
    for row in page["items"]:
        assert row["duration_seconds"] == 600
        assert row["finished_at"] == "2026-10-10T10:10:00Z"
        assert row["reward"] == 0.0 and row["passed"] is False
        detail = harbor_trials.detail(tmp_path, row["trial_name"])
        assert detail["trial"] == row
        assert detail["verifier_logs"][0]["content"] == "done"
    assert all(path.read_text() == original for path, original in originals.items())


def test_corrupt_result_recovers_reward_and_exception_buckets(tmp_path):
    name = "医疗任务__summary"
    trial = _trial(tmp_path, name, "医疗任务")
    (trial / "result.json").write_text('{"multiplier": [REDACTED].0}')
    (tmp_path / "result.json").write_text(json.dumps({
        "finished_at": "2026-10-10T10:10:00Z",
        "stats": {"evals": {"nop__adhoc": {
            "reward_stats": {"reward": {"0.5": [name]}},
            "exception_stats": {"AgentTimeoutError": [name]},
        }}},
    }))
    state = harbor_trials.scan(tmp_path)[0]
    assert state.phase == "failed" and state.exception_type == "AgentTimeoutError"
    assert state.reward == 0.5 and state.passed is False
    assert state.finished_at == "2026-10-10T10:10:00Z"
    assert harbor_trials.counts([state])["scored"] == 1


def test_unrecoverable_terminal_trial_stops_running_and_freezes_time(tmp_path):
    trial = _trial(tmp_path, "医疗任务__broken", "医疗任务")
    (trial / "verifier" / "test-stdout.txt").write_text("done")
    (trial / "result.json").write_text('{"reward": [REDACTED]}')
    live = harbor_trials.scan(tmp_path)[0]
    assert live.phase == "running_verifier" and live.finished_at is None

    end = "2026-10-10T10:10:00Z"
    stopped = harbor_trials.page(tmp_path, job_status="succeeded", finished_at=end)["items"][0]
    assert stopped["phase"] == "failed" and stopped["exception_type"] == "TrialResultReadError"
    assert stopped["reward"] is None and stopped["passed"] is None
    assert stopped["finished_at"] == end
    assert harbor_trials.detail(tmp_path, trial.name, job_status="succeeded", finished_at=end)["trial"] == stopped


def test_terminal_job_marks_unstarted_trials_as_finished(tmp_path):
    (tmp_path / "lock.json").write_text(json.dumps({"trials": [{"task": {"name": "never-started"}}]}))
    state = harbor_trials.scan(tmp_path, job_status="cancelled", finished_at="2026-10-10T10:10:00Z")[0]
    assert state.phase == "failed" and state.exception_type == "TrialNotStarted"
    assert state.reward is None


def test_unicode_trial_name_and_scrubbed_name_use_real_directory(tmp_path):
    name = "PRT-医疗 001-20261003-1__abc"
    trial = _trial(tmp_path, name, "医疗")
    result = _result(name, 1.0)
    result["trial_name"] = name.replace("1", "[REDACTED]")
    result["task_name"] = result["trial_name"]
    (trial / "result.json").write_text(json.dumps(result))
    detail = harbor_trials.detail(tmp_path, name)
    assert detail["trial"]["trial_name"] == name
    assert detail["trial"]["task_name"] == name.split("__", 1)[0]
    assert detail["trial"]["passed"] is True


@pytest.mark.parametrize("name", ["", ".", "..", "../other", "/other", "a/b", "a\\b", "医疗\n", "医疗\x00"])
def test_trial_detail_rejects_invalid_components(tmp_path, name):
    assert harbor_trials.detail(tmp_path, name) is None


def test_trial_detail_and_scan_reject_directory_symlinks(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    outside = _trial(tmp_path, "outside", "outside")
    (root / "医疗__link").symlink_to(outside, target_is_directory=True)
    assert harbor_trials.detail(root, "医疗__link") is None
    assert harbor_trials.scan(root) == []


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


@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('config_readable', [False, True])
def test_local_names_and_package_result_names_do_not_duplicate_trials(tmp_path, terminal, config_readable):
    planned = []
    for index, reward in enumerate((.83, .66, .82), 1):
        name = f'MED-T2-{index:03d}'
        planned.append({'task': {'name': name}, 'agent': {'name': 'codex', 'model_name': 'm'}})
        trial = _trial(tmp_path, f'{name}__abc{index}', name)
        if not config_readable:
            (trial / 'config.json').write_text('{broken')
        result = _result(trial.name, reward)
        result['task_name'] = f'prt/{name.lower()}'
        result['agent_info'] = {'name': 'codex', 'model_info': {'name': 'm'}}
        (trial / 'result.json').write_text(json.dumps(result))
    (tmp_path / 'lock.json').write_text(json.dumps({'trials': planned}))
    page = harbor_trials.page(tmp_path, job_status='succeeded' if terminal else 'running')
    assert page['total'] == page['counts']['success'] == page['counts']['scored'] == 3
    assert page['counts']['failed'] == page['counts']['pending'] == 0
    assert {row['reward'] for row in page['items']} == {.83, .66, .82}
    from backend.app.services import metrics_parser
    parsed = metrics_parser.parse_harbor(tmp_path.parent, tmp_path.name,
                                        job_status='succeeded' if terminal else 'running')
    assert parsed.progress.total_trials == parsed.progress.completed_trials == 3
    assert parsed.summary.n_errors == 0


def test_matching_consumes_each_attempt_once_and_keeps_unstarted_attempt(tmp_path):
    name = 'MED-T2-001'
    (tmp_path / 'lock.json').write_text(json.dumps({'trials': [
        {'task': {'name': name}} for _ in range(3)
    ]}))
    for attempt in ('first', 'second'):
        trial = _trial(tmp_path, f'{name}__{attempt}', name)
        result = _result(trial.name, .8)
        result['task_name'] = 'prt/med-t2-001'
        (trial / 'result.json').write_text(json.dumps(result))
    states = harbor_trials.scan(tmp_path, job_status='failed')
    assert len(states) == 3
    assert sum(state.exception_type == 'TrialNotStarted' for state in states) == 1
    assert sum(state.reward == .8 for state in states) == 2


def test_matching_does_not_consume_different_model_or_namespace(tmp_path):
    (tmp_path / 'lock.json').write_text(json.dumps({'trials': [
        {'task': {'name': 'other/task'}, 'agent': {'name': 'codex', 'model_name': 'm'}},
        {'task': {'name': 'prt/task'}, 'agent': {'name': 'codex', 'model_name': 'not-m'}},
        {'task': {'name': 'prt/task'}, 'agent': {'name': 'codex', 'model_name': 'm'}},
    ]}))
    trial = _trial(tmp_path, 'task__real', 'prt/task')
    (trial / 'agent' / 'codex.txt').write_text('running')
    states = harbor_trials.scan(tmp_path)
    assert len(states) == 3
    pending = [state for state in states if state.phase == 'pending']
    assert len(pending) == 2
    assert {state.model for state in pending} == {'m', 'not-m'}


def test_trial_lock_name_recognizes_task_despite_directory_truncation(tmp_path):
    name = 'MED-' + 'long-task-name-' * 5
    (tmp_path / 'lock.json').write_text(json.dumps({'trials': [{'task': {'name': name}}]}))
    trial = _trial(tmp_path, name[:32] + '__abc', 'prt/renamed-package')
    (trial / 'lock.json').write_text(json.dumps({'task': {'name': name}}))
    result = _result(trial.name, 1)
    result['task_name'] = 'prt/renamed-package'
    (trial / 'result.json').write_text(json.dumps(result))
    assert len(harbor_trials.scan(tmp_path, job_status='succeeded')) == 1
