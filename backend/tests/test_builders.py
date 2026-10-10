"""Command builders must emit flags the installed frameworks actually accept."""

from __future__ import annotations

import json
import os
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
    ModelApiConfig,
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


@pytest.mark.parametrize("value", ["1", "12", "abc", "true", "false", "null", "1.0", "123456789"])
def test_harbor_rejects_dangerous_secret_placeholders(tmp_path, harbor_dataset, monkeypatch, value):
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = _job(JobType.VERIFY, Framework.HARBOR, verify=VerifyConfig(
        dataset_id=harbor_dataset.id, agent="nop",
        verifier_env={"EVAL_API_KEY": value, "EVAL_API_BASE": "1"},
    ))
    with pytest.raises(BuildError, match="EVAL_API_KEY.*脱敏"):
        harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    assert not (tmp_path / "w" / ".env").exists()


def test_harbor_allows_local_auth_sentinel_and_non_secret_numbers(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    job = _job(JobType.VERIFY, Framework.HARBOR, verify=VerifyConfig(
        dataset_id=harbor_dataset.id, agent="nop", agent_env={"OPENAI_API_KEY": "EMPTY"},
        verifier_env={"EVAL_API_KEY": "sk-real-secret", "BATCH_SIZE": "1"},
    ))
    result = harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    assert "OPENAI_API_KEY=EMPTY" in result.command and "BATCH_SIZE=1" in result.command
    assert "sk-real-secret" not in result.masked_command_line()


def test_harbor_acknowledges_optional_task_variables_without_overriding_defaults(tmp_path, harbor_dataset, monkeypatch):
    from io import StringIO
    from dotenv import dotenv_values

    _fake_harbor_bin(monkeypatch, tmp_path)
    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    monkeypatch.delenv("TIMEOUT", raising=False)
    for name, timeout in [("demo", "30"), ("other", "60")]:
        task = Path(harbor_dataset.root_path) / name
        task.mkdir(exist_ok=True)
        (task / "task.toml").write_text(
            '[verifier.env]\nEVAL_API_KEY = "${EVAL_API_KEY:-}"\n'
            f'TIMEOUT = "${{TIMEOUT:-{timeout}}}"\nREQUIRED_TOKEN = "${{REQUIRED_TOKEN}}"\n',
        )
    job = _job(JobType.VERIFY, Framework.HARBOR, verify=VerifyConfig(dataset_id=harbor_dataset.id, agent="nop"))
    result = harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    env_path = Path(result.command[result.command.index("--env-file") + 1])
    declared = dotenv_values(stream=StringIO(env_path.read_text()))
    assert declared == {"EVAL_API_KEY": None, "TIMEOUT": None}
    assert "REQUIRED_TOKEN" not in declared
    assert "EVAL_API_KEY" not in result.env
    assert not any(part.startswith("EVAL_API_KEY=") for part in result.command)


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


def _rewardkit_dataset(harbor_dataset) -> Dataset:
    harbor_dataset.rewardkit = True
    store.datasets.put(harbor_dataset)
    return harbor_dataset


def test_harbor_judge_env_injection(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    dataset = _rewardkit_dataset(harbor_dataset)
    judge = ModelApiConfig(
        name="judge-openai", provider="openai", model="gpt-5", api_key="sk-judge-secret", base_url=""
    )
    store.model_apis.put(judge)
    job = _job(
        JobType.VERIFY,
        Framework.HARBOR,
        verify=VerifyConfig(
            dataset_id=dataset.id,
            agent="oracle",
            judge_model_api_id=judge.id,
            judge_api_protocol="openai",
        ),
    )
    result = harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    command = result.command

    # 只注入这 5 个 JUDGE_*
    assert "JUDGE_MODEL=gpt-5" in command
    assert "JUDGE_PROVIDER=openai" in command
    assert "JUDGE_API_PROTOCOL=openai" in command
    assert "JUDGE_API_KEY=sk-judge-secret" in command
    # base_url 为空 => 不发（不回退默认值）
    assert not any(part.startswith("JUDGE_BASE_URL=") for part in command)
    # 不注入 provider 的标准变量（任务侧 test.sh 自己映射）
    assert not any(part.startswith("OPENAI_API_KEY=") for part in command)
    # 密钥掩码；其余 4 个可读
    assert "sk-judge-secret" not in result.masked_command_line()
    assert result.masked_env()["JUDGE_API_KEY"] == "***"
    assert result.masked_env()["JUDGE_MODEL"] == "gpt-5"
    # .env 0600 供 task.toml 的 ${VAR} 插值
    env_file = tmp_path / "w" / ".env"
    assert env_file.is_file()
    assert "JUDGE_API_KEY=sk-judge-secret" in env_file.read_text(encoding="utf-8")


def test_harbor_judge_env_user_override_wins(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    dataset = _rewardkit_dataset(harbor_dataset)
    judge = ModelApiConfig(name="judge-a", provider="anthropic", model="claude-x", api_key="sk-a")
    store.model_apis.put(judge)
    config = VerifyConfig(
        dataset_id=dataset.id,
        agent="oracle",
        judge_model_api_id=judge.id,
        verifier_env={"JUDGE_MODEL": "override-model"},
    )
    result = harbor_builder.build(_job(JobType.VERIFY, Framework.HARBOR, verify=config), tmp_path / "w", tmp_path / "o")
    assert "JUDGE_MODEL=override-model" in result.command
    assert "JUDGE_MODEL=claude-x" not in result.command


def test_harbor_rewardkit_without_judge_rejected(tmp_path, harbor_dataset, monkeypatch):
    _fake_harbor_bin(monkeypatch, tmp_path)
    dataset = _rewardkit_dataset(harbor_dataset)
    job = _job(
        JobType.VERIFY, Framework.HARBOR, verify=VerifyConfig(dataset_id=dataset.id, agent="oracle")
    )
    with pytest.raises(BuildError, match="reward-kit"):
        harbor_builder.build(job, tmp_path / "w", tmp_path / "o")

    # 跳过 verifier 时既不要求也不注入 judge
    job.verify.disable_verification = True
    result = harbor_builder.build(job, tmp_path / "w2", tmp_path / "o2")
    assert not any(part.startswith("JUDGE_") for part in result.command)


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
            if not filename.endswith(".py"):
                continue  # harbor 模式还会写一份 dist-info 元数据
            compile(source, filename, "exec")  # 语法错就在这里炸，而不是排队几分钟后
            assert "true" not in source.split("CONFIG = ")[1].split("\n\n")[0].replace("True", "")
            assert "null" not in source


def test_trl_generation_batch_is_divisible_by_group_size(tmp_path, parquet_dataset):
    """TRL 要求 generation_batch_size 能被 num_generations 整除，否则 Config 直接拒。

    真机回归：GRPO 一启动就
    `generation_batch_size (2) must be divisible by num_generations (8)`。
    """
    from backend.app.builders.trl import _group_batch

    for prompts in (1, 2, 7, 8, 64, 65):
        for group in (2, 4, 8, 16):
            for n_gpus in (1, 2, 4):
                per_device, accum, effective = _group_batch(prompts, group, n_gpus)
                generation_batch = per_device * n_gpus * accum
                assert generation_batch % group == 0, (prompts, group, n_gpus)
                assert accum >= 1 and per_device >= 1
                assert effective >= prompts - n_gpus  # 只向上取整到卡数的整数倍

    # 端到端：生成脚本里的三个数也要满足这个约束
    for algorithm, group in ((TrainStage.GRPO, 8), (TrainStage.RLOO, 4)):
        config = RLConfig(
            framework=Framework.TRL, algorithm=algorithm, model_path="/tmp/m",
            dataset_id=parquet_dataset.id, train_batch_size=2,
        )
        config.rollout.n = group
        job = _job(JobType.RL, Framework.TRL, rl=config)
        result = trl_builder.build(job, tmp_path / f"batch-{algorithm.value}", tmp_path / "o")
        source = next(iter(result.files.values()))
        cfg = {}
        import ast
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CONFIG":
                cfg = ast.literal_eval(node.value)
        generation_batch = cfg["per_device_train_batch_size"] * 1 * cfg["gradient_accumulation_steps"]
        assert generation_batch % cfg["num_generations"] == 0, cfg
        # RLOOConfig 没有 loss_type
        assert ("loss_type" in cfg) == (algorithm != TrainStage.RLOO)


def test_trl_harbor_mode_makes_harbor_importable(tmp_path, harbor_dataset):
    """真机回归：训练循环跑起来后在 HarborSpec.reset() 里
    `ModuleNotFoundError: No module named 'harbor'`。

    Harbor agentic RL 在训练进程内起沙箱，所以 harbor 必须能被 trl 的解释器导入；
    但不能 pip 装进 trl 环境（会把 openai/websockets 降级，vllm 依赖它们）。
    """
    from backend.app.config import settings

    config = RLConfig(
        framework=Framework.TRL, algorithm=TrainStage.GRPO, model_path="/tmp/m",
        dataset_id=harbor_dataset.id,
    )
    config.harbor.enabled = True
    job = _job(JobType.RL, Framework.TRL, rl=config)
    work_dir = tmp_path / "w"
    result = trl_builder.build(job, work_dir, tmp_path / "o")

    parts = result.env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(settings.trl_root), "trl 检出必须最前面"
    assert str(settings.harbor_root / "src") in parts

    # harbor/__init__.py 会读自己的 dist 元数据，光给代码会 PackageNotFoundError
    meta = [name for name in result.files if name.endswith(".dist-info/METADATA")]
    assert meta, result.files.keys()
    body = result.files[meta[0]]
    assert "Name: harbor" in body and "Version: " in body
    assert str(work_dir / "harbor_meta") in parts

    # 非 harbor 模式不该挂这些
    plain = RLConfig(framework=Framework.TRL, algorithm=TrainStage.DPO, model_path="/tmp/m",
                     dataset_id=harbor_dataset.id)
    plain_job = _job(JobType.RL, Framework.TRL, rl=plain)
    with pytest.raises(BuildError):
        trl_builder.build(plain_job, tmp_path / "w2", tmp_path / "o")  # DPO 不吃 harbor_tasks


# --------------------------------------------------------------------------- #
# 路径必须是绝对路径（否则 harbor 会把 --env-file / --jobs-dir 再拼接一次）
# --------------------------------------------------------------------------- #
def test_relative_data_root_is_resolved_absolute():
    from backend.app.config import REPO_ROOT, Settings

    settings = Settings(data_root="./data")
    assert settings.data_root.is_absolute()
    assert settings.data_root == REPO_ROOT / "data"
    assert settings.jobs_dir.is_absolute()
    assert settings.outputs_dir.is_absolute()
