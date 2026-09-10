"""Log/metric parsers for each framework."""

from __future__ import annotations

import json

from backend.app.services import metrics_parser


def test_llamafactory_progress(tmp_path):
    log = tmp_path / "trainer_log.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"current_steps": 10, "total_steps": 100, "loss": 2.5, "lr": 1e-4, "epoch": 0.3,
                 "percentage": 10.0, "remaining_time": "0:09:00"},
                {"current_steps": 20, "total_steps": 100, "loss": 1.8, "lr": 9e-5, "epoch": 0.6,
                 "percentage": 20.0, "remaining_time": "0:08:00", "throughput": 1200.0},
            ]
        ),
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_llamafactory(tmp_path)
    assert parsed.progress.current_step == 20
    assert parsed.progress.total_steps == 100
    assert parsed.progress.percent == 20.0
    assert parsed.progress.eta_seconds == 480
    assert parsed.summary.last_loss == 1.8
    assert parsed.summary.best_loss == 1.8
    assert parsed.series["loss"] == [[10.0, 2.5], [20.0, 1.8]]


def test_verl_console_parsing(tmp_path):
    log = tmp_path / "stdout.log"
    log.write_text(
        "some noise\n"
        "step:1 - critic/rewards/mean:0.10 - actor/pg_loss:0.55 - actor/grad_norm:1.2\n"
        "step:2 - critic/rewards/mean:0.42 - actor/pg_loss:0.31 - actor/grad_norm:0.9\n",
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_verl(log, total_steps_hint=10)
    assert parsed.progress.current_step == 2
    assert parsed.progress.percent == 20.0
    assert parsed.summary.last_reward == 0.42
    assert parsed.summary.best_reward == 0.42
    assert parsed.series["reward"] == [[1.0, 0.10], [2.0, 0.42]]


def test_trl_trainer_state(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "trainer_state.json").write_text(
        json.dumps(
            {
                "global_step": 30,
                "max_steps": 60,
                "epoch": 1.5,
                "log_history": [
                    {"step": 10, "loss": 1.0, "learning_rate": 1e-6, "reward": 0.2},
                    {"step": 20, "loss": 0.7, "learning_rate": 9e-7, "reward": 0.5},
                ],
            }
        ),
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_trl(output, tmp_path / "missing.log")
    assert parsed.progress.percent == 50.0
    assert parsed.summary.best_reward == 0.5
    assert parsed.summary.best_loss == 0.7


def test_harbor_result_parsing(tmp_path):
    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / "job_abc"
    job_dir.mkdir(parents=True)
    (job_dir / "config.json").write_text("{}", encoding="utf-8")
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 8,
                    "n_errored_trials": 1,
                    "n_running_trials": 1,
                    "n_pending_trials": 2,
                    "cost_usd": 1.25,
                    "n_input_tokens": 1000,
                    "n_output_tokens": 500,
                    "evals": {
                        "claude-code__sonnet__demo": {
                            "n_trials": 8,
                            "pass_at_k": {"1": 0.5, "4": 0.75},
                            "reward_stats": {"reward": {"1.0": ["a", "b"], "0.0": ["c", "d"]}},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_harbor(jobs_dir, "job_abc")
    # completed(8) already contains the errored one, so the total is 8 + 1 + 2
    assert parsed.progress.total_trials == 11
    assert parsed.progress.completed_trials == 8
    assert parsed.summary.pass_at_k == {"pass@1": 0.5, "pass@4": 0.75}
    # 2 of the 8 finished trials scored 1.0
    assert parsed.summary.resolved_rate == 0.25
    assert parsed.summary.n_errors == 1
    assert parsed.summary.cost_usd == 1.25
    assert parsed.summary.tokens == {"input": 1000, "output": 500}


def test_parsers_survive_garbage(tmp_path):
    (tmp_path / "trainer_log.jsonl").write_text("not json\n{broken", encoding="utf-8")
    parsed = metrics_parser.parse_llamafactory(tmp_path)
    assert parsed.progress.current_step == 0
    assert parsed.series == {}


def test_job_trials_parsing(tmp_path, monkeypatch):
    """Per-trial results are read from the Harbor job dir, worst reward first."""
    from backend.app.models import Framework, Job, JobType, ResourceSpec, User, VerifyConfig, Role
    from backend.app.services import job_service
    from backend.app.store import store

    jobs_dir = tmp_path / "outputs"
    job = Job(
        name="verify-parse",
        type=JobType.VERIFY,
        framework=Framework.HARBOR,
        owner_id="usr_metrics",
        resources=ResourceSpec(n_gpus=0),
        verify=VerifyConfig(tasks_path=str(tmp_path), agent="oracle"),
    )
    job.runtime.output_dir = str(jobs_dir)
    store.jobs.put(job)

    harbor_dir = jobs_dir / job.id
    for name, reward, exc in (("task-a", 1.0, None), ("task-b", 0.0, None), ("task-c", None, "AgentTimeoutError")):
        trial_dir = harbor_dir / f"{name}.1"
        trial_dir.mkdir(parents=True)
        payload = {
            "task_name": name,
            "trial_name": f"{name}.1",
            "agent_info": {"name": "oracle", "version": "1", "model_info": {"name": "n/a"}},
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:30+00:00",
        }
        if reward is not None:
            payload["verifier_result"] = {"rewards": {"reward": reward}}
        if exc:
            payload["exception_info"] = {
                "exception_type": exc,
                "exception_message": "timed out",
                "exception_traceback": "",
                "occurred_at": "2026-01-01T00:01:30+00:00",
            }
        (trial_dir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    (harbor_dir / "config.json").write_text("{}", encoding="utf-8")

    actor = User(id="usr_metrics", email="m@prt.local", display_name="m", role=Role.ADMIN, password_hash="x")
    result = job_service.job_trials(job.id, actor=actor)
    assert result["total"] == 3
    assert result["counts"]["success"] == 2
    assert result["counts"]["failed"] == 1
    assert result["counts"]["passed"] == 1
    # 失败的排在完成的前面
    assert result["items"][0]["exception_type"] == "AgentTimeoutError"
    assert result["items"][0]["phase"] == "failed"
    assert {i["reward"] for i in result["items"][1:]} == {0.0, 1.0}
    assert result["items"][1]["duration_seconds"] == 90.0

    # 分页 + 按阶段筛
    page2 = job_service.job_trials(job.id, actor=actor, page=2, page_size=2)
    assert page2["pages"] == 2 and len(page2["items"]) == 1
    only_ok = job_service.job_trials(job.id, actor=actor, phase="success")
    assert only_ok["total"] == 2 and all(i["phase"] == "success" for i in only_ok["items"])

    # 单个 trial 的详情（路径穿越要被拒）
    detail = job_service.job_trial_detail(job.id, "task-c.1", actor=actor)
    assert detail["trial"]["exception_type"] == "AgentTimeoutError"
    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException
    with _pytest.raises(_HTTPException):
        job_service.job_trial_detail(job.id, "../config.json", actor=actor)


def test_llamafactory_final_progress_record_keeps_last_values(tmp_path):
    """LLaMA-Factory appends a progress-only record at train end.

    Regression: taking "the last record" made last_loss/lr vanish the moment a
    real run finished (observed on a live Qwen3.5-4B LoRA run).
    """
    (tmp_path / "trainer_log.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"current_steps": 1, "total_steps": 3, "loss": 3.59, "lr": 0.0, "percentage": 33.33},
                {"current_steps": 2, "total_steps": 3, "loss": 3.05, "lr": 1e-4, "percentage": 66.67},
                {"current_steps": 3, "total_steps": 3, "loss": 2.85, "lr": 5e-5, "percentage": 100.0},
                # final record: progress only, no loss / lr
                {"current_steps": 3, "total_steps": 3, "epoch": 0.375, "percentage": 100.0},
            ]
        ),
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_llamafactory(tmp_path)
    assert parsed.summary.last_loss == 2.85
    assert parsed.summary.best_loss == 2.85
    assert parsed.summary.learning_rate == 5e-5
    assert parsed.progress.percent == 100.0


def test_trl_final_runtime_record_keeps_last_values(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "trainer_state.json").write_text(
        json.dumps(
            {
                "global_step": 20,
                "max_steps": 20,
                "log_history": [
                    {"step": 10, "loss": 1.0, "learning_rate": 1e-6},
                    {"step": 20, "loss": 0.6, "learning_rate": 5e-7},
                    {"train_runtime": 12.5, "train_samples_per_second": 3.2},
                ],
            }
        ),
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_trl(output, tmp_path / "missing.log")
    assert parsed.summary.last_loss == 0.6
    assert parsed.summary.learning_rate == 5e-7


def test_harbor_errored_trial_counts_as_failure(tmp_path):
    """Regression from a real Harbor run (oracle on harbor's own examples).

    Harbor's JobStats bumps n_completed_trials for every finished trial and
    additionally n_errored_trials when it raised, so errored ⊆ completed.
    Summing them invented a third trial, and reward_stats — which only holds
    trials that produced a reward — made a 1-of-2 run look like 100%.
    """
    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / "job_x"
    job_dir.mkdir(parents=True)
    (job_dir / "config.json").write_text("{}", encoding="utf-8")
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 2,     # includes the errored one
                    "n_errored_trials": 1,
                    "n_running_trials": 0,
                    "n_pending_trials": 0,
                    "evals": {
                        "oracle__ds": {
                            "n_trials": 1,
                            "pass_at_k": {},
                            "reward_stats": {"reward": {"1.0": ["hello-workdir__abc"]}},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    parsed = metrics_parser.parse_harbor(jobs_dir, "job_x")
    assert parsed.progress.total_trials == 2       # not 3
    assert parsed.progress.completed_trials == 2
    assert parsed.summary.n_errors == 1
    assert parsed.summary.resolved_rate == 0.5     # not 1.0
    assert parsed.summary.n_trials == 2


def test_job_trials_reads_singular_result_json(tmp_path):
    """Harbor writes <trial>/result.json even though its docstring says results.json."""
    from backend.app.models import Framework, Job, JobType, ResourceSpec, Role, User, VerifyConfig
    from backend.app.services import job_service
    from backend.app.store import store

    jobs_dir = tmp_path / "out"
    job = Job(
        name="verify-singular",
        type=JobType.VERIFY,
        framework=Framework.HARBOR,
        owner_id="usr_singular",
        resources=ResourceSpec(n_gpus=0),
        verify=VerifyConfig(tasks_path=str(tmp_path), agent="oracle"),
    )
    job.runtime.output_dir = str(jobs_dir)
    store.jobs.put(job)

    harbor_dir = jobs_dir / job.id
    (harbor_dir / "hello-world__aaa").mkdir(parents=True)
    (harbor_dir / "config.json").write_text("{}", encoding="utf-8")
    (harbor_dir / "hello-world__aaa" / "result.json").write_text(
        json.dumps(
            {
                "task_name": "harbor/hello-world",
                "trial_name": "hello-world__aaa",
                "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
                "exception_info": {
                    "exception_type": "VerifierTimeoutError",
                    "exception_message": "Verifier execution timed out after 240.0 seconds",
                    "exception_traceback": "",
                    "occurred_at": "2026-09-07T13:24:39Z",
                },
                "started_at": "2026-09-07T13:19:57Z",
                "finished_at": "2026-09-07T13:24:39Z",
            }
        ),
        encoding="utf-8",
    )

    actor = User(id="usr_singular", email="s@prt.local", display_name="s", role=Role.ADMIN, password_hash="x")
    result = job_service.job_trials(job.id, actor=actor)
    assert result["total"] == 1
    assert result["counts"]["failed"] == 1
    assert result["items"][0]["exception_type"] == "VerifierTimeoutError"
    assert result["items"][0]["agent"] == "oracle"
