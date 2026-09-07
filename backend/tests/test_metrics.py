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
