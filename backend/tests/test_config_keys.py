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


def test_llamafactory_export_keys_are_known():
    """合并导出的 YAML 键必须真的存在于 ExportArguments / ModelArguments。"""
    from backend.app.models import ExportTask
    from backend.app.services import export_service

    known = _llamafactory_known_keys() | _TRANSFORMERS_KEYS
    task = ExportTask(
        name="merge",
        job_id="job_x",
        base_model_path="/models/qwen",
        adapter_path="/outputs/job_x",
        template="qwen3_nothink",
        export_dir="/exports/exp_x/model",
        owner_id="usr_keys",
    )
    emitted = set(export_service.build_config(task))
    unknown = sorted(emitted - known)
    assert not unknown, f"LLaMA-Factory 不认识这些导出键: {unknown}"
    # 少了任何一个，导出的就不是合并后的完整权重
    assert {"model_name_or_path", "adapter_name_or_path", "export_dir", "template"} <= emitted


def test_llamafactory_flash_attn_values_are_accepted(tmp_path, sft_dataset):
    """flash_attn 的取值来自 extras.constants.AttentionFunction 枚举。"""
    path = settings.llamafactory_root / "src" / "llamafactory" / "extras" / "constants.py"
    if not path.is_file():
        pytest.skip("LLaMA-Factory 源码不可用")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AttentionFunction":
            allowed = {
                item.value.value
                for item in node.body
                if isinstance(item, ast.Assign)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            }
    assert allowed, "没能从 constants.py 里解析出 AttentionFunction 的取值"

    # 平台暴露的每一个选项都必须是框架认识的
    offered = set(SFTConfig.model_fields["flash_attn"].annotation.__args__)
    assert offered <= allowed, f"平台提供了框架不认识的 flash_attn 取值: {sorted(offered - allowed)}"

    for value in sorted(offered - {"auto"}):
        config = SFTConfig(
            model_path="/tmp/model",
            dataset_ids=[sft_dataset.id],
            flash_attn=value,
        )
        job = Job(
            name="attn",
            type=JobType.SFT,
            framework=Framework.LLAMAFACTORY,
            owner_id="usr_keys",
            resources=ResourceSpec(n_gpus=1),
            sft=config,
        )
        result = lf_builder.build(job, tmp_path / f"attn-{value}", tmp_path / "o")
        emitted = yaml.safe_load(result.files["train_config.yaml"])["flash_attn"]
        assert emitted == value
        assert emitted in allowed, f"LLaMA-Factory 不接受 flash_attn={emitted}"

    # auto 表示交给框架自己判断，不该出现在配置里
    config = SFTConfig(model_path="/tmp/model", dataset_ids=[sft_dataset.id], flash_attn="auto")
    job = Job(
        name="attn-auto",
        type=JobType.SFT,
        framework=Framework.LLAMAFACTORY,
        owner_id="usr_keys",
        resources=ResourceSpec(n_gpus=1),
        sft=config,
    )
    result = lf_builder.build(job, tmp_path / "attn-auto", tmp_path / "o")
    assert "flash_attn" not in yaml.safe_load(result.files["train_config.yaml"])


def test_rl_attn_auto_falls_back_when_flash_attn_missing(tmp_path, parquet_dataset, monkeypatch):
    """auto 必须问过环境。

    发 ``flash_attention_2`` 到没装 flash-attn 的环境里，transformers 是在**加载模型时**
    才抛 `FlashAttention2 has been toggled on, but it cannot be used`——
    任务会先白排队几分钟。所以 auto 要在生成命令时就探测并回落。
    """
    from backend.app.builders import trl as trl_builder
    from backend.app.builders.base import resolve_attn_implementation
    from backend.app.services import resources_service

    def make_job(framework: Framework, attn: str) -> Job:
        return Job(
            name="attn-auto",
            type=JobType.RL,
            framework=framework,
            owner_id="usr_keys",
            resources=ResourceSpec(n_gpus=1),
            rl=RLConfig(
                framework=framework,
                algorithm=TrainStage.GRPO,
                model_path="/tmp/model",
                dataset_id=parquet_dataset.id,
                attn_implementation=attn,
            ),
        )

    for available, expected in ((False, "sdpa"), (True, "flash_attention_2")):
        monkeypatch.setattr(resources_service, "has_flash_attn", lambda _bin, v=available: v)

        resolved, note = resolve_attn_implementation("auto", "/fake/python")
        assert resolved == expected, note

        verl_result = verl_builder.build(
            make_job(Framework.VERL, "auto"), tmp_path / f"verl-auto-{expected}", tmp_path / "o"
        )
        assert (
            f"+actor_rollout_ref.model.override_config.attn_implementation={expected}"
            in verl_result.command
        )

        trl_result = trl_builder.build(
            make_job(Framework.TRL, "auto"), tmp_path / f"trl-auto-{expected}", tmp_path / "o"
        )
        body = next(iter(trl_result.files.values()))
        assert f'"attn_implementation": "{expected}"' in body

    # "default" 表示完全不下发
    monkeypatch.setattr(resources_service, "has_flash_attn", lambda _bin: False)
    assert resolve_attn_implementation("default", "/fake/python") == ("", "")
    verl_result = verl_builder.build(
        make_job(Framework.VERL, "default"), tmp_path / "verl-default", tmp_path / "o"
    )
    assert not any("attn_implementation" in part for part in verl_result.command)

    # 显式指定就照发，但要在 notes 里警告环境不匹配
    resolved, note = resolve_attn_implementation("flash_attention_2", "/fake/python")
    assert resolved == "flash_attention_2"
    assert "flash-attn" in note


def test_trl_attn_implementation_goes_through_model_init_kwargs(tmp_path, parquet_dataset):
    """TRL 没有 attn_implementation 参数，只能塞进 model_init_kwargs。"""
    from backend.app.builders import trl as trl_builder

    config = RLConfig(
        framework=Framework.TRL,
        algorithm=TrainStage.GRPO,
        model_path="/tmp/model",
        dataset_id=parquet_dataset.id,
        attn_implementation="flash_attention_2",
    )
    job = Job(
        name="trl-attn",
        type=JobType.RL,
        framework=Framework.TRL,
        owner_id="usr_keys",
        resources=ResourceSpec(n_gpus=1),
        rl=config,
    )
    script = trl_builder.build(job, tmp_path / "trl-attn", tmp_path / "o")
    body = next(iter(script.files.values()))
    assert '"model_init_kwargs"' in body
    assert '"attn_implementation": "flash_attention_2"' in body

    grpo_config = settings.trl_root / "trl" / "trainer" / "grpo_config.py"
    if grpo_config.is_file():
        source = grpo_config.read_text(encoding="utf-8")
        assert "model_init_kwargs" in source
        assert "attn_implementation:" not in source  # 确认它不是一个顶层字段


def _flatten(node, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.add(path)
            keys |= _flatten(value, path)
    return keys


def _open_dicts(node, prefix: str = "") -> set[str]:
    """Config nodes that are declared as an empty mapping.

    verl leaves a few of these (``model.override_config``) as free-form dicts the
    user is meant to fill in, so a ``+parent.child=value`` override is legitimate
    even though ``child`` is nowhere in the config tree.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                if not value:
                    found.add(path)
                else:
                    found |= _open_dicts(value, path)
    return found


def test_verl_override_paths_exist(tmp_path, parquet_dataset):
    generated = settings.verl_root / "verl" / "trainer" / "config" / "_generated_ppo_trainer.yaml"
    if not generated.is_file():
        pytest.skip("verl 源码不可用")
    tree = yaml.safe_load(generated.read_text(encoding="utf-8"))
    known = _flatten(tree)
    free_form = _open_dicts(tree)

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
            attn_implementation="sdpa",
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

    unknown = sorted(key for key in emitted if not _known_override(key, known, free_form))
    assert not unknown, f"verl 配置树里不存在这些 Hydra 覆盖路径: {unknown}"
    # attn_implementation 只能靠 "+" 塞进 override_config，退化成普通覆盖会直接报错
    assert any(
        key == "+actor_rollout_ref.model.override_config.attn_implementation" for key in emitted
    )


def _known_override(key: str, known: set[str], free_form: set[str]) -> bool:
    if key.startswith("+"):
        path = key[1:]
        parent = path.rsplit(".", 1)[0]
        return parent in free_form
    return key in known


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
