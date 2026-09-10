"""Command builders must emit flags the installed frameworks actually accept."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from backend.app.builders import llamafactory as lf_builder
from backend.app.builders import harbor as harbor_builder
from backend.app.builders import trl as trl_builder
from backend.app.builders import verl as verl_builder
from backend.app.builders.base import BuildError
from backend.app.models import (
    Dataset,
    DatasetKind,
    DatasetStats,
    DatasetStatus,
    Framework,
    Job,
    JobType,
    ResourceSpec,
    RLConfig,
    SFTConfig,
    TrainStage,
    VerifyConfig,
)
from backend.app.store import store

from .conftest import FAKE_MODEL_ID, MODELS_ROOT


@pytest.fixture
def sft_dataset(tmp_path) -> Dataset:
    root = tmp_path / "sft-ds"
    root.mkdir()
    (root / "train.json").write_text(json.dumps([{"instruction": "a", "output": "b"}]), "utf-8")
    (root / "dataset_info.json").write_text(
        json.dumps({"train": {"file_name": str(root / "train.json"), "columns": {"prompt": "instruction", "response": "output"}}}),
        "utf-8",
    )
    dataset = Dataset(
        name="builder-sft",
        kind=DatasetKind.SFT_ALPACA,
        status=DatasetStatus.READY,
        owner_id="usr_test",
        root_path=str(root),
        stats=DatasetStats(n_records=1),
        lf_dataset_names=["train"],
    )
    store.datasets.put(dataset)
    return dataset


@pytest.fixture
def parquet_dataset(tmp_path) -> Dataset:
    root = tmp_path / "verl-ds"
    root.mkdir()
    (root / "train.parquet").write_bytes(b"PAR1")
    (root / "test.parquet").write_bytes(b"PAR1")
    (root / ".prt_meta.json").write_text(
        json.dumps(
            {
                "train_files": [str(root / "train.parquet")],
                "val_files": [str(root / "test.parquet")],
            }
        ),
        "utf-8",
    )
    dataset = Dataset(
        name="builder-verl",
        kind=DatasetKind.VERL_PARQUET,
        status=DatasetStatus.READY,
        owner_id="usr_test",
        root_path=str(root),
    )
    store.datasets.put(dataset)
    return dataset


@pytest.fixture
def harbor_dataset(tmp_path) -> Dataset:
    root = tmp_path / "harbor-ds" / "tasks"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "task.toml").write_text('version = "1.0"\n', "utf-8")
    (root / "demo" / "instruction.md").write_text("do it\n", "utf-8")
    dataset = Dataset(
        name="builder-harbor",
        kind=DatasetKind.HARBOR_TASKS,
        status=DatasetStatus.READY,
        owner_id="usr_test",
        root_path=str(root),
        stats=DatasetStats(n_tasks=1),
    )
    store.datasets.put(dataset)
    return dataset


def _job(job_type: JobType, framework: Framework, **blocks) -> Job:
    return Job(
        name="builder-test",
        type=job_type,
        framework=framework,
        owner_id="usr_test",
        owner_email="t@prt.local",
        resources=ResourceSpec(n_gpus=1, gpu_ids=[0]),
        **blocks,
    )


# --------------------------------------------------------------------------- #
# LLaMA-Factory
# --------------------------------------------------------------------------- #
def test_llamafactory_sft_yaml(tmp_path, sft_dataset):
    job = _job(
        JobType.SFT,
        Framework.LLAMAFACTORY,
        sft=SFTConfig(
            model_path=str(MODELS_ROOT / FAKE_MODEL_ID),
            template="qwen3",
            dataset_ids=[sft_dataset.id],
            finetuning_type="lora",
            deepspeed_stage="none",
        ),
    )
    result = lf_builder.build(job, tmp_path / "work", tmp_path / "out")
    assert result.command[1:4] == ["-m", "llamafactory.cli", "train"]
    config = yaml.safe_load(result.files["train_config.yaml"])
    assert config["stage"] == "sft"
    assert config["finetuning_type"] == "lora"
    assert config["bf16"] is True
    assert config["lora_rank"] == 8
    assert config["template"] == "qwen3"
    # merged dataset_info written into the job work dir, entry names namespaced
    merged = json.loads((tmp_path / "work" / "lf_data" / "dataset_info.json").read_text())
    assert list(merged) == [f"{sft_dataset.id}__train"]
    assert config["dataset"] == f"{sft_dataset.id}__train"


def test_llamafactory_dpo_sets_pref_loss(tmp_path, sft_dataset):
    job = _job(
        JobType.SFT,
        Framework.LLAMAFACTORY,
        sft=SFTConfig(
            stage=TrainStage.SIMPO,
            model_path=str(MODELS_ROOT / FAKE_MODEL_ID),
            dataset_ids=[sft_dataset.id],
        ),
    )
    config = yaml.safe_load(
        lf_builder.build(job, tmp_path / "w", tmp_path / "o").files["train_config.yaml"]
    )
    assert config["stage"] == "dpo"
    assert config["pref_loss"] == "simpo"


def test_llamafactory_multi_gpu_sets_torchrun_env(tmp_path, sft_dataset):
    job = _job(
        JobType.SFT,
        Framework.LLAMAFACTORY,
        sft=SFTConfig(model_path="/tmp/m", dataset_ids=[sft_dataset.id]),
    )
    job.resources.n_gpus = 4
    result = lf_builder.build(job, tmp_path / "w", tmp_path / "o")
    assert result.env["FORCE_TORCHRUN"] == "1"
    assert result.env["NPROC_PER_NODE"] == "4"


def test_llamafactory_requires_dataset(tmp_path):
    job = _job(JobType.SFT, Framework.LLAMAFACTORY, sft=SFTConfig(model_path="/tmp/m"))
    with pytest.raises(BuildError):
        lf_builder.build(job, tmp_path / "w", tmp_path / "o")


# --------------------------------------------------------------------------- #
# verl
# --------------------------------------------------------------------------- #
def test_verl_grpo_overrides(tmp_path, parquet_dataset):
    job = _job(
        JobType.RL,
        Framework.VERL,
        rl=RLConfig(
            framework=Framework.VERL,
            algorithm=TrainStage.GRPO,
            model_path="/tmp/model",
            dataset_id=parquet_dataset.id,
        ),
    )
    job.resources.n_gpus = 2
    result = verl_builder.build(job, tmp_path / "w", tmp_path / "o")
    overrides = set(result.command[3:])
    assert "algorithm.adv_estimator=grpo" in overrides
    assert "actor_rollout_ref.rollout.n=8" in overrides
    assert "trainer.n_gpus_per_node=2" in overrides
    assert any(o.startswith("data.train_files=[") for o in overrides)
    assert any(o.startswith("actor_rollout_ref.actor.clip_ratio_low=") for o in overrides)


def test_verl_dapo_enables_dynamic_sampling(tmp_path, parquet_dataset):
    job = _job(
        JobType.RL,
        Framework.VERL,
        rl=RLConfig(
            framework=Framework.VERL,
            algorithm=TrainStage.DAPO,
            model_path="/tmp/model",
            dataset_id=parquet_dataset.id,
            clip_ratio_low=0.2,
            clip_ratio_high=0.28,
            enable_overlong_buffer=True,
        ),
    )
    result = verl_builder.build(job, tmp_path / "w", tmp_path / "o")
    overrides = set(result.command)
    assert "algorithm.filter_groups.enable=True" in overrides
    assert "actor_rollout_ref.actor.clip_ratio_high=0.28" in overrides
    # unsupported knob is reported instead of silently emitted
    assert not any("overlong_buffer" in o for o in overrides)
    assert any("overlong" in note for note in result.notes)


def test_verl_rejects_harbor_dataset(tmp_path, harbor_dataset):
    job = _job(
        JobType.RL,
        Framework.VERL,
        rl=RLConfig(
            framework=Framework.VERL,
            algorithm=TrainStage.GRPO,
            model_path="/tmp/model",
            dataset_id=harbor_dataset.id,
        ),
    )
    with pytest.raises(BuildError, match="parquet"):
        verl_builder.build(job, tmp_path / "w", tmp_path / "o")


def test_verl_rejects_tp_larger_than_gpus(tmp_path, parquet_dataset):
    config = RLConfig(
        framework=Framework.VERL,
        algorithm=TrainStage.GRPO,
        model_path="/tmp/model",
        dataset_id=parquet_dataset.id,
    )
    config.rollout.tensor_model_parallel_size = 8
    job = _job(JobType.RL, Framework.VERL, rl=config)
    job.resources.n_gpus = 2
    with pytest.raises(BuildError, match="张量并行"):
        verl_builder.build(job, tmp_path / "w", tmp_path / "o")


# --------------------------------------------------------------------------- #
# TRL
# --------------------------------------------------------------------------- #
def test_trl_harbor_script(tmp_path, harbor_dataset):
    config = RLConfig(
        framework=Framework.TRL,
        algorithm=TrainStage.GRPO,
        model_path="/tmp/model",
        dataset_id=harbor_dataset.id,
    )
    config.harbor.enabled = True
    config.harbor.harness = "bash"
    job = _job(JobType.RL, Framework.TRL, rl=config)
    result = trl_builder.build(job, tmp_path / "w", tmp_path / "o")
    script = result.files["train_harbor_grpo.py"]
    assert "HarborSpec" in script
    assert harbor_dataset.root_path in script
    assert "GRPOTrainer" in script
    assert result.command[-1] == "train_harbor_grpo.py"


def test_trl_gspo_sets_sequence_importance_sampling(tmp_path, parquet_dataset):
    config = RLConfig(
        framework=Framework.TRL,
        algorithm=TrainStage.GSPO,
        model_path="/tmp/model",
        dataset_id=parquet_dataset.id,
    )
    job = _job(JobType.RL, Framework.TRL, rl=config)
    result = trl_builder.build(job, tmp_path / "w", tmp_path / "o")
    script = result.files["train_gspo.py"]
    assert "'importance_sampling_level': 'sequence'" in script
    assert "outcome_reward" in script


def test_trl_multi_gpu_uses_accelerate(tmp_path, parquet_dataset):
    config = RLConfig(
        framework=Framework.TRL,
        algorithm=TrainStage.GRPO,
        model_path="/tmp/model",
        dataset_id=parquet_dataset.id,
    )
    job = _job(JobType.RL, Framework.TRL, rl=config)
    job.resources.n_gpus = 4
    result = trl_builder.build(job, tmp_path / "w", tmp_path / "o")
    assert "accelerate.commands.launch" in result.command
    assert "--num_processes" in result.command


# --------------------------------------------------------------------------- #
# Harbor
# --------------------------------------------------------------------------- #
def _fake_harbor_bin(monkeypatch, tmp_path) -> Path:
    binary = tmp_path / "harbor"
    binary.write_text("#!/bin/bash\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(harbor_builder, "resolve_harbor_bin", lambda: str(binary))
    return binary


def test_harbor_verify_flags(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    config = VerifyConfig(
        dataset_id=harbor_dataset.id,
        agent="claude-code",
        model="anthropic/claude-sonnet-4-5",
        n_concurrent=16,
        n_attempts=4,
        environment_type="docker",
        agent_env={"ANTHROPIC_API_KEY": "sk-super-secret"},
        verifier_env={"HTTP_PROXY": "http://proxy:8080"},
        task_names=["bash-*"],
        n_tasks=25,
        export_traces=True,
    )
    job = _job(JobType.VERIFY, Framework.HARBOR, verify=config)
    result = harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    command = result.command

    assert "--n-attempts" in command and command[command.index("--n-attempts") + 1] == "4"
    assert "--n-concurrent" in command and command[command.index("--n-concurrent") + 1] == "16"
    assert "--agent" in command and command[command.index("--agent") + 1] == "claude-code"
    assert "--env" in command and command[command.index("--env") + 1] == "docker"
    assert "--agent-env" in command
    assert "ANTHROPIC_API_KEY=sk-super-secret" in command
    assert "--include-task-name" in command and "bash-*" in command
    assert "--n-tasks" in command and command[command.index("--n-tasks") + 1] == "25"
    assert "--export-traces" in command
    assert "--job-name" in command and job.id in command
    # secrets never make it into the rendered command line
    assert "sk-super-secret" not in result.masked_command_line()
    assert "***" in result.masked_command_line()
    assert result.masked_env()["ANTHROPIC_API_KEY"] == "***"
    # non-secret env is left readable
    assert result.masked_env()["HTTP_PROXY"] == "http://proxy:8080"


def test_harbor_oracle_needs_no_model(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = _job(
        JobType.VERIFY,
        Framework.HARBOR,
        verify=VerifyConfig(dataset_id=harbor_dataset.id, agent="oracle"),
    )
    result = harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    assert "--model" not in result.command
    assert any("oracle" in note for note in result.notes)


def test_harbor_agent_without_model_rejected(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = _job(
        JobType.VERIFY,
        Framework.HARBOR,
        verify=VerifyConfig(dataset_id=harbor_dataset.id, agent="codex"),
    )
    with pytest.raises(BuildError, match="需要指定模型"):
        harbor_builder.build(job, tmp_path / "w", tmp_path / "o")


def test_harbor_requires_task_source(tmp_path, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = _job(JobType.VERIFY, Framework.HARBOR, verify=VerifyConfig(agent="oracle"))
    with pytest.raises(BuildError, match="任务集"):
        harbor_builder.build(job, tmp_path / "w", tmp_path / "o")


def test_generated_trl_scripts_are_valid_python(tmp_path, harbor_dataset, parquet_dataset):
    """生成的训练脚本必须能被 Python 编译。

    真机回归：配置曾用 json.dumps 嵌进脚本，`"bf16": true` 在 Python 里是未定义的名字，
    GRPO 任务一启动就 NameError。
    """
    cases = [
        ("harbor", RLConfig(framework=Framework.TRL, algorithm=TrainStage.GRPO, model_path="/tmp/m",
                            dataset_id=harbor_dataset.id, use_kl_loss=True, total_training_steps=None)),
        ("grpo", RLConfig(framework=Framework.TRL, algorithm=TrainStage.GRPO, model_path="/tmp/m",
                          dataset_id=parquet_dataset.id, gradient_checkpointing=True)),
        ("dpo", RLConfig(framework=Framework.TRL, algorithm=TrainStage.DPO, model_path="/tmp/m",
                         dataset_id=parquet_dataset.id, total_training_steps=None)),
        ("custom-reward", RLConfig(framework=Framework.TRL, algorithm=TrainStage.GRPO, model_path="/tmp/m",
                                   dataset_id=parquet_dataset.id, custom_reward_function_path="/tmp/r.py",
                                   custom_reward_function_name="score")),
    ]
    for name, config in cases:
        if name == "harbor":
            config.harbor.enabled = True
        job = _job(JobType.RL, Framework.TRL, rl=config)
        result = trl_builder.build(job, tmp_path / f"w-{name}", tmp_path / "o")
        for filename, source in result.files.items():
            compile(source, filename, "exec")  # 语法错就在这里炸，而不是排队几分钟后
            assert "true" not in source.split("CONFIG = ")[1].split("\n\n")[0].replace("True", "")
            assert "null" not in source
