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
    assert parsed.progress.total_trials == 12
    assert parsed.progress.completed_trials == 8
    assert parsed.summary.pass_at_k == {"pass@1": 0.5, "pass@4": 0.75}
    assert parsed.summary.resolved_rate == 0.5
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
    assert result["resolved"] == 1
    assert result["errored"] == 1
    assert result["trials"][0]["reward"] == 0.0          # worst first
    assert result["trials"][-1]["exception_type"] == "AgentTimeoutError"
    assert result["trials"][0]["duration_seconds"] == 90.0
