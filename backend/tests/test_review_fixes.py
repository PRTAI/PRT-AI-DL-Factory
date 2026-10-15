"""Regression coverage for the Harbor and training review findings."""
import json
import os
from pathlib import Path

import pytest

from backend.app.builders import harbor, harbor_actions, trl, verl
from backend.app.builders.base import BuildError
from backend.app.config import settings
from backend.app.models import Framework, Job, JobStatus, JobType, ResourceSpec, RLConfig, TrainStage, VerifyConfig
from backend.app.models.job import HarborAction
from backend.app.services import harbor_trials, metrics_parser
from backend.app.store import store
from .test_builders import harbor_dataset, parquet_dataset, _fake_harbor_bin


def verify_job(dataset, **options):
    return Job(name="review regression", owner_id="test", type=JobType.VERIFY, framework=Framework.HARBOR,
               resources=ResourceSpec(n_gpus=0), verify=VerifyConfig(dataset_id=dataset.id, **options))


def test_static_routes_reject_encoded_traversal_and_symlinks(client, tmp_path, monkeypatch):
    from backend.app import main
    if not hasattr(main, "spa"):
        pytest.skip("Static SPA not mounted; build frontend first")
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("test SPA")
    (static / "safe.txt").write_text("public")
    private = tmp_path / "secret.txt"
    private.write_text("private secret")
    (static / "link.txt").symlink_to(private)
    monkeypatch.setattr(main, "FRONTEND_DIST", static)
    assert client.get("/%2e%2e/secret.txt").status_code == 404
    assert client.get("/link.txt").status_code == 404
    assert client.get("/safe.txt").text == "public"
    assert client.get("/jobs/deep-link").text == "test SPA"


def test_stage_credentials_and_literal_values_survive_native_resolution(tmp_path, harbor_dataset, monkeypatch):
    from dotenv import load_dotenv
    from harbor.utils.env import resolve_env_vars
    _fake_harbor_bin(monkeypatch, tmp_path)
    strange = 'quote"\\slash\n${MISSING_REVIEW_VAR} # literal'
    job = verify_job(harbor_dataset, agent="nop", agent_env={
        "OPENAI_API_KEY": "agent-key-original", "LITERAL": strange,
    }, verifier_env={"OPENAI_API_KEY": "judge-key-different"})
    built = harbor.build(job, tmp_path / "work", tmp_path / "out")
    for key, value in built.env.items():
        monkeypatch.setenv(key, value)
    load_dotenv(tmp_path / "work/.env", override=True)
    def stage(flag):
        return dict(built.command[i + 1].split("=", 1) for i, value in enumerate(built.command) if value == flag)
    assert resolve_env_vars(stage("--agent-env"))["OPENAI_API_KEY"] == "agent-key-original"
    assert resolve_env_vars(stage("--verifier-env"))["OPENAI_API_KEY"] == "judge-key-different"
    assert resolve_env_vars(stage("--agent-env"))["LITERAL"] == strange
    assert os.environ["OPENAI_API_KEY"] == "agent-key-original"
    rendered = built.masked_command_line() + json.dumps(built.masked_env()) + (tmp_path / "work/.env").read_text()
    assert "agent-key-original" not in rendered and "judge-key-different" not in rendered
    assert set(built.secrets) >= {"agent-key-original", "judge-key-different"}


def test_custom_agent_and_explicit_zero_overrides(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = verify_job(harbor_dataset, agent="oracle", agent_import_path="custom.module:Agent", model="test-model",
                     environment_gpus=0, extra_args={"seed": 0})
    built = harbor.build(job, tmp_path / "w", tmp_path / "o")
    assert built.command[built.command.index("--agent") + 1] == "custom.module:Agent"
    assert "--agent-import-path" not in built.command
    assert built.command[built.command.index("--override-gpus") + 1] == "0"
    assert built.command[built.command.index("--seed") + 1] == "0"
    job.verify.model = ""
    with pytest.raises(BuildError, match="需要指定模型"):
        harbor.build(job, tmp_path / "w", tmp_path / "o")
    with pytest.raises(ValueError):
        VerifyConfig(environment_gpus=-1)


def test_regrade_preserves_custom_verifier(tmp_path, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(harbor_actions, "resolve_harbor_bin", lambda: "harbor")
    monkeypatch.setattr(harbor_actions, "resolve_harbor_python", lambda _: "python")
    job = Job(name="custom verifier", owner_id="test", type=JobType.VERIFY, framework=Framework.HARBOR,
              verify=VerifyConfig(tasks_path=str(tmp_path), extra_args={"--verifier": "custom.module:Verifier"},
                                  verifier_kwargs={"threshold": "0.75"}),
              harbor_action=HarborAction(mode="regrade", source_job_id="original"))
    built = harbor_actions.build_action(job, tmp_path / "w", tmp_path / "o")
    assert built.command[built.command.index("--verifier") + 1] == "custom.module:Verifier"
    assert built.command[built.command.index("--verifier-kwarg") + 1] == "threshold=0.75"


def test_verifier_errors_and_auxiliary_metrics_are_not_scores(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    (root / "config.json").write_text("{}")
    (root / "result.json").write_text(json.dumps({"stats": {"n_completed_trials": 2,
        "evals": {"test": {"reward_stats": {
            "reward": {"0": ["failed"], "0.25": ["valid"]},
            "verifier_error": {"1": ["failed"], "0": ["valid"]},
            "graded_score": {"0": ["failed"], "0.25": ["valid"]},
            "criteria_counted": {"100": ["valid"]},
        }}}}}))
    parsed = metrics_parser.parse_harbor(tmp_path, "job")
    assert parsed.summary.resolved_rate == 0
    assert parsed.summary.best_reward == 0.25
    assert parsed.summary.n_errors == 1
    assert harbor_trials._primary_reward({"verifier_error": 1}) is None
    assert harbor_trials._primary_reward({"criteria_counted": 4, "graded_score": 0.5}) == 0.5
    # reward-kit 多维评分没有总分键：取各项平均，不能丢成 None
    assert harbor_trials._primary_reward({"correctness": 1.0, "format": 1.0}) == 1.0
    assert harbor_trials._primary_reward({"correctness": 1.0, "format": 0.0}) == 0.5


def test_multi_criteria_rewards_count_toward_pass_rate(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    (root / "config.json").write_text("{}")
    (root / "result.json").write_text(json.dumps({"stats": {"n_completed_trials": 2,
        "evals": {"test": {"reward_stats": {
            "correctness": {"1.0": ["a", "b"]},
            "format": {"1.0": ["a", "b"]},
        }}}}}))
    parsed = metrics_parser.parse_harbor(tmp_path, "job")
    assert parsed.summary.resolved_rate == 1.0
    assert parsed.summary.best_reward == 1.0


def test_failed_or_missing_checks_never_become_pass():
    from backend.app.models import CheckStatus, ReviewCheck, ReviewTask, ReviewFinding, ReviewVerdict
    from backend.app.services.review_report import build_report
    task = ReviewTask(name="review", owner_id="test", workspace="test")
    manifest = {"tasks": [{"task_id": "task"}]}
    for checks, findings in [([ReviewCheck(code="C1", title="check", status=CheckStatus.FAILED)], []),
                             ([ReviewCheck(code="C1", title="check", status=CheckStatus.DONE)], []),
                             ([], [])]:
        report = build_report(task, manifest, checks, findings)
        assert report["stats"]["pass_rate"] == 0
        assert report["stats"]["n_needs_review"] == 1
        assert report["needs_review"]


def test_scheduler_reconciles_orphan_preparing_but_preserves_owned_prepare():
    from backend.app.services import scheduler as module
    scheduler = module.Scheduler()
    job = Job(name="preparing", owner_id="test", type=JobType.VERIFY, framework=Framework.HARBOR,
              status=JobStatus.QUEUED, verify=VerifyConfig(), resources=ResourceSpec(n_gpus=1))
    store.jobs.put(job)
    try:
        scheduler._claim(job, [0])
        scheduler._reconcile()
        assert store.jobs.get(job.id).status == JobStatus.PREPARING
        module._PREPARING.discard(job.id)  # Simulate lost in-memory preparation ownership on restart.
        scheduler._reconcile()
        restored = store.jobs.get(job.id)
        assert restored.status == JobStatus.FAILED
        assert restored.resources.gpu_ids == []
        assert restored.runtime.finished_at is not None
    finally:
        module._PREPARING.discard(job.id)
        store.jobs.delete(job.id)


@pytest.mark.parametrize(("code", "status"), [(0, JobStatus.SUCCEEDED), (3, JobStatus.FAILED)])
def test_reconcile_settles_orphans_by_recorded_exit_code(tmp_path, code, status):
    """平台重启期间跑完的任务按包装脚本落盘的退出码结算，而不是一律「进程丢失」。"""
    import subprocess
    from backend.app.services import runner, scheduler as module
    exit_file = tmp_path / runner.EXIT_CODE_FILE
    finished = subprocess.run([*runner._EXIT_WRAPPER, "sh", "-c", f"exit {code}"],
                              env={**os.environ, "PRT_EXIT_FILE": str(exit_file)})
    assert finished.returncode == code
    assert exit_file.read_text().strip() == str(code)
    log = tmp_path / "stdout.log"
    log.write_text("Traceback: boom\n")
    job = Job(name="orphan", owner_id="test", type=JobType.VERIFY, framework=Framework.HARBOR,
              status=JobStatus.RUNNING, verify=VerifyConfig(), resources=ResourceSpec(n_gpus=1, gpu_ids=[0]))
    job.runtime.work_dir = str(tmp_path)
    job.runtime.log_file = str(log)
    store.jobs.put(job)
    try:
        module.Scheduler()._reconcile()
        settled = store.jobs.get(job.id)
        assert settled.status == status
        assert settled.runtime.exit_code == code
        assert settled.resources.gpu_ids == []
        assert "进程已不存在" not in settled.runtime.error
    finally:
        store.jobs.delete(job.id)


def test_unsupported_training_choices_fail_explicitly(tmp_path):
    config = RLConfig(model_path="/test", framework=Framework.VERL, finetuning_type="lora")
    job = Job(name="lora", owner_id="test", type=JobType.RL, framework=Framework.VERL, rl=config)
    with pytest.raises(BuildError, match="verl LoRA"):
        verl.build(job, tmp_path, tmp_path)
    config.framework = Framework.TRL
    config.rollout.backend = "sglang"
    with pytest.raises(BuildError, match="SGLang"):
        trl.build(job, tmp_path, tmp_path)
    config.rollout.backend = "vllm"
    config.rollout.tensor_model_parallel_size = 3
    with pytest.raises(BuildError, match="张量并行"):
        trl.validate_options(config, 4)


@pytest.mark.parametrize("algorithm", [TrainStage.GRPO, TrainStage.RLOO, TrainStage.DPO, TrainStage.KTO])
def test_trl_emits_requested_limits_evaluation_and_rollout(tmp_path, parquet_dataset, monkeypatch, algorithm):
    (tmp_path / "trl").mkdir()
    monkeypatch.setattr(settings, "trl_root", tmp_path)
    monkeypatch.setattr(trl, "resolve_attn_implementation", lambda *_: ("", ""))
    config = RLConfig(model_path="/test", framework=Framework.TRL, dataset_id=parquet_dataset.id,
                      algorithm=algorithm, total_training_steps=123, test_freq=17, val_before_train=True,
                      clip_ratio=0.75)
    config.rollout.tensor_model_parallel_size = 2
    job = Job(name="trl", owner_id="test", type=JobType.RL, framework=Framework.TRL,
              resources=ResourceSpec(n_gpus=2), rl=config)
    built = trl.build(job, tmp_path / "w", tmp_path / "o")
    import ast
    script = next(value for key, value in built.files.items() if key.startswith("train_"))
    tree = ast.parse(script)
    payload = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "CONFIG")
    assert payload["max_steps"] == 123
    assert payload["eval_strategy"] == "steps" and payload["eval_steps"] == 17
    assert payload["eval_on_start"] is True
    if algorithm in {TrainStage.GRPO, TrainStage.RLOO}:
        assert payload["vllm_tensor_parallel_size"] == 2
    if algorithm == TrainStage.RLOO:
        assert payload["epsilon"] == payload["epsilon_high"] == 0.75
    if algorithm in {TrainStage.GRPO, TrainStage.RLOO}:
        # TRL：每卡验证 batch × 卡数必须能被组大小整除，否则 GRPOConfig 初始化就报错
        assert payload["per_device_eval_batch_size"] == config.rollout.n
    else:
        assert "per_device_eval_batch_size" not in payload


def test_trl_rejects_eval_batch_override_not_divisible_by_group():
    config = RLConfig(model_path="/test", framework=Framework.TRL, dataset_id="d", algorithm=TrainStage.GRPO,
                      test_freq=10, extra_args={"per_device_eval_batch_size": 8})
    config.rollout.n = 5
    with pytest.raises(BuildError, match="验证批次"):
        trl.validate_options(config, 1)
    config.extra_args = {"per_device_eval_batch_size": 5}
    trl.validate_options(config, 1)
    config.extra_args = {"per_device_eval_batch_size": 8, "num_generations_eval": 4}
    trl.validate_options(config, 1)


def test_native_harbor_parses_custom_agent_zero_and_stage_env(tmp_path, harbor_dataset, monkeypatch):
    import subprocess
    import sys
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = verify_job(harbor_dataset, agent="oracle", agent_import_path="custom.module:Agent", model="demo",
                     environment_gpus=0, agent_env={"OPENAI_API_KEY": "agent-private-key"},
                     verifier_env={"OPENAI_API_KEY": "verifier-private-key"})
    built = harbor.build(job, tmp_path / "work", tmp_path / "output")
    completed = subprocess.run(
        [sys.executable, "-c", "from harbor.cli.main import app; app()", *built.command[3:], "--print-config"],
        env={**os.environ, **built.env, "NO_COLOR": "1"}, cwd=tmp_path, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    native = json.loads(completed.stdout)
    assert native["agents"][0]["name"] == "custom.module:Agent"
    from harbor.agents import factory
    from harbor.models.trial.config import AgentConfig
    resolved = []
    monkeypatch.setattr(factory, "_import_agent_class", lambda path: resolved.append(path) or object)
    assert factory.AgentFactory.get_agent_class_from_config(AgentConfig.model_validate(native["agents"][0])) is object
    assert resolved == ["custom.module:Agent"]
    assert native["environment"]["override_gpus"] == 0
    assert native["agents"][0]["env"]["OPENAI_API_KEY"] == "${PRT_AGENT_OPENAI_API_KEY}"
    assert native["verifier"]["env"]["OPENAI_API_KEY"] == "${PRT_VERIFIER_OPENAI_API_KEY}"
