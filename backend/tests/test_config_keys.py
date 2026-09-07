"""Guard rails: the keys our builders emit must exist in the installed frameworks.

These tests read the framework sources directly (no heavy imports), so a
framework upgrade that renames a config key fails here instead of failing at
2am inside a real training run.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml

from backend.app.builders import llamafactory as lf_builder
from backend.app.builders import verl as verl_builder
from backend.app.config import settings
from backend.app.models import (
    Dataset,
    DatasetKind,
    DatasetStatus,
    Framework,
    Job,
    JobType,
    ResourceSpec,
    RLConfig,
    SFTConfig,
    TrainStage,
)
from backend.app.store import store

# Keys that come from transformers' Seq2SeqTrainingArguments rather than
# LLaMA-Factory's own dataclasses (verified against LLaMA-Factory's shipped
# examples/train_lora/*.yaml).
_TRANSFORMERS_KEYS = {
    "do_train",
    "output_dir",
    "overwrite_output_dir",
    "logging_steps",
    "save_steps",
    "save_only_model",
    "report_to",
    "per_device_train_batch_size",
    "per_device_eval_batch_size",
    "gradient_accumulation_steps",
    "learning_rate",
    "num_train_epochs",
    "max_steps",
    "lr_scheduler_type",
    "warmup_ratio",
    "weight_decay",
    "max_grad_norm",
    "bf16",
    "fp16",
    "fp32",
    "ddp_timeout",
    "gradient_checkpointing",
    "deepspeed",
    "resume_from_checkpoint",
    "eval_strategy",
    "eval_steps",
    "dataloader_num_workers",
}


def _llamafactory_known_keys() -> set[str]:
    """Field names declared across LLaMA-Factory's own hparams dataclasses."""
    root = settings.llamafactory_root / "src" / "llamafactory" / "hparams"
    if not root.is_dir():
        pytest.skip("LLaMA-Factory 源码不可用")
    keys: set[str] = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                keys.add(node.target.id)
    return keys


@pytest.fixture
def sft_dataset(tmp_path) -> Dataset:
    root = tmp_path / "keys-sft"
    root.mkdir()
    (root / "train.json").write_text(json.dumps([{"instruction": "a", "output": "b"}]), "utf-8")
    (root / "dataset_info.json").write_text(
        json.dumps({"train": {"file_name": str(root / "train.json")}}), "utf-8"
    )
    dataset = Dataset(
        name="keys-sft",
        kind=DatasetKind.SFT_ALPACA,
        status=DatasetStatus.READY,
        owner_id="usr_keys",
        root_path=str(root),
        lf_dataset_names=["train"],
    )
    store.datasets.put(dataset)
    return dataset


@pytest.fixture
def parquet_dataset(tmp_path) -> Dataset:
    root = tmp_path / "keys-verl"
    root.mkdir()
    (root / "train.parquet").write_bytes(b"PAR1")
    (root / ".prt_meta.json").write_text(
        json.dumps({"train_files": [str(root / "train.parquet")], "val_files": []}), "utf-8"
    )
    dataset = Dataset(
        name="keys-verl",
        kind=DatasetKind.VERL_PARQUET,
        status=DatasetStatus.READY,
        owner_id="usr_keys",
        root_path=str(root),
    )
    store.datasets.put(dataset)
    return dataset


def test_llamafactory_yaml_keys_are_known(tmp_path, sft_dataset):
    known = _llamafactory_known_keys() | _TRANSFORMERS_KEYS
    emitted: set[str] = set()

    for stage, finetuning, deepspeed in (
        (TrainStage.SFT, "lora", "none"),
        (TrainStage.SFT, "full", "zero3"),
        (TrainStage.DPO, "lora", "none"),
        (TrainStage.KTO, "freeze", "zero2"),
    ):
        config = SFTConfig(
            stage=stage,
            model_path="/tmp/model",
            dataset_ids=[sft_dataset.id],
            finetuning_type=finetuning,  # type: ignore[arg-type]
            deepspeed_stage=deepspeed,  # type: ignore[arg-type]
            quantization_bit=4 if finetuning == "lora" else None,
            flash_attn="fa2",
            liger_kernel=True,
            max_samples=100,
            max_steps=10,
            val_size=0.1,
            resume_from_checkpoint="/tmp/ckpt",
        )
        job = Job(
            name="keys",
            type=JobType.SFT,
            framework=Framework.LLAMAFACTORY,
            owner_id="usr_keys",
            resources=ResourceSpec(n_gpus=1),
            sft=config,
        )
        result = lf_builder.build(job, tmp_path / f"w-{stage.value}-{finetuning}", tmp_path / "o")
        emitted |= set(yaml.safe_load(result.files["train_config.yaml"]))

    unknown = sorted(emitted - known)
    assert not unknown, f"LLaMA-Factory 不认识这些配置键: {unknown}"


def _flatten(node, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.add(path)
            keys |= _flatten(value, path)
    return keys


def test_verl_override_paths_exist(tmp_path, parquet_dataset):
    generated = settings.verl_root / "verl" / "trainer" / "config" / "_generated_ppo_trainer.yaml"
    if not generated.is_file():
        pytest.skip("verl 源码不可用")
    known = _flatten(yaml.safe_load(generated.read_text(encoding="utf-8")))

    emitted: set[str] = set()
    for algorithm in (TrainStage.GRPO, TrainStage.DAPO, TrainStage.PPO, TrainStage.RLOO):
        config = RLConfig(
            framework=Framework.VERL,
            algorithm=algorithm,
            model_path="/tmp/model",
            dataset_id=parquet_dataset.id,
            use_kl_loss=True,
            filter_groups_enable=True,
            total_training_steps=100,
            custom_reward_function_path="/tmp/reward.py",
            custom_reward_function_name="compute_score",
        )
        job = Job(
            name="keys",
            type=JobType.RL,
            framework=Framework.VERL,
            owner_id="usr_keys",
            resources=ResourceSpec(n_gpus=2),
            rl=config,
        )
        result = verl_builder.build(job, tmp_path / f"w-{algorithm.value}", tmp_path / "o")
        for part in result.command[3:]:
            emitted.add(part.split("=", 1)[0])

    unknown = sorted(key for key in emitted if key not in known)
    assert not unknown, f"verl 配置树里不存在这些 Hydra 覆盖路径: {unknown}"


def _harbor_run_flags() -> set[str]:
    """Every option `harbor jobs start` accepts, read straight from its signature.

    Typer derives a flag from the parameter name when no explicit string is
    given, so both forms have to be considered.
    """
    jobs_cli = settings.harbor_root / "src" / "harbor" / "cli" / "jobs.py"
    if not jobs_cli.is_file():
        pytest.skip("Harbor 源码不可用")
    tree = ast.parse(jobs_cli.read_text(encoding="utf-8"))
    flags: set[str] = set()
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "start"):
            continue
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            explicit: list[str] = []
            annotation = arg.annotation
            if isinstance(annotation, ast.Subscript) and isinstance(annotation.slice, ast.Tuple):
                for meta in annotation.slice.elts[1:]:
                    if isinstance(meta, ast.Call):
                        for value in meta.args:
                            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                                if value.value.startswith("-"):
                                    explicit.extend(value.value.split("/"))
            flags.update(explicit or [f"--{arg.arg.replace('_', '-')}"])
    if not flags:
        pytest.skip("无法从 Harbor CLI 中解析出参数")
    return flags


def test_harbor_flags_match_cli(tmp_path):
    """Every flag the Harbor builder emits must exist in `harbor run`."""
    known = _harbor_run_flags()

    from backend.app.builders import harbor as harbor_builder
    from backend.app.models import VerifyConfig

    binary = tmp_path / "harbor"
    binary.write_text("#!/bin/bash\n", encoding="utf-8")
    binary.chmod(0o755)
    original = harbor_builder.resolve_harbor_bin
    harbor_builder.resolve_harbor_bin = lambda: str(binary)  # type: ignore[assignment]
    try:
        tasks = tmp_path / "tasks" / "demo"
        (tasks / "environment").mkdir(parents=True)
        (tasks / "task.toml").write_text('version = "1.0"\n', "utf-8")
        (tasks / "instruction.md").write_text("do it\n", "utf-8")
        dataset = Dataset(
            name="keys-harbor",
            kind=DatasetKind.HARBOR_TASKS,
            status=DatasetStatus.READY,
            owner_id="usr_keys",
            root_path=str(tasks.parent),
        )
        store.datasets.put(dataset)

        config = VerifyConfig(
            dataset_id=dataset.id,
            agent="claude-code",
            model="anthropic/claude-sonnet-4-5",
            n_attempts=4,
            max_retries=2,
            n_tasks=10,
            task_names=["a-*"],
            exclude_task_names=["b-*"],
            agent_env={"ANTHROPIC_API_KEY": "sk-x"},
            agent_kwargs={"a": "1"},
            verifier_env={"OPENAI_API_KEY": "sk-y"},
            environment_kwargs={"c": "2"},
            verifier_kwargs={"d": "3"},
            environment_cpus=2,
            environment_memory_mb=4096,
            environment_gpus=1,
            timeout_multiplier=2.0,
            agent_timeout_multiplier=1.5,
            verifier_timeout_multiplier=1.5,
            disable_verification=True,
            force_build=True,
            export_traces=True,
            sharegpt=True,
            agent_import_path="pkg.mod:Agent",
        )
        job = Job(
            name="keys",
            type=JobType.VERIFY,
            framework=Framework.HARBOR,
            owner_id="usr_keys",
            resources=ResourceSpec(n_gpus=0),
            verify=config,
        )
        result = harbor_builder.build(job, tmp_path / "w", tmp_path / "o")
    finally:
        harbor_builder.resolve_harbor_bin = original  # type: ignore[assignment]

    flags = {part for part in result.command if part.startswith("--")}
    missing = sorted(flags - known)
    assert not missing, f"harbor run 不支持这些参数: {missing}"
    # sanity: the extractor really did find the CLI surface
    assert {"--n-attempts", "--include-task-name", "--export-sharegpt"} <= known


def test_generated_framework_support_is_current():
    """The cheat-sheet data must match the checked-out framework versions."""
    payload = json.loads(
        (Path(settings.resources_dir) / "framework_support.json").read_text(encoding="utf-8")
    )
    if (settings.llamafactory_root / "src").is_dir():
        assert payload["llamafactory"]["models"], "LLaMA-Factory 模型表为空，请重新运行 scripts/gen_framework_support.py"
    if (settings.harbor_root / "src").is_dir():
        assert "oracle" in payload["harbor"]["agents"]
        assert "docker" in payload["harbor"]["environments"]
    if (settings.verl_root / "verl").is_dir():
        assert "grpo" in payload["verl"]["advantage_estimators"]
