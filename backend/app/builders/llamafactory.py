"""LLaMA-Factory command builder (SFT / DPO / KTO / ORPO / SimPO)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config import settings
from ..models import Dataset, Job, SFTConfig, TrainStage
from ..services import dataset_service
from ..store import store
from .base import BuildError, BuildResult

_DEEPSPEED_CONFIGS = {
    "zero2": "examples/deepspeed/ds_z2_config.json",
    "zero3": "examples/deepspeed/ds_z3_config.json",
    "zero3_offload": "examples/deepspeed/ds_z3_offload_config.json",
}

# Which LLaMA-Factory `stage` implements each of our algorithm names.
_STAGE_MAP = {
    TrainStage.SFT: "sft",
    TrainStage.DPO: "dpo",
    TrainStage.KTO: "kto",
    TrainStage.ORPO: "dpo",
    TrainStage.SIMPO: "dpo",
}

_PREF_LOSS_OVERRIDE = {
    TrainStage.ORPO: "orpo",
    TrainStage.SIMPO: "simpo",
}


def _collect_datasets(ids: list[str]) -> list[Dataset]:
    datasets: list[Dataset] = []
    for dataset_id in ids:
        dataset = store.datasets.get(dataset_id)
        if dataset is None:
            raise BuildError(f"数据集不存在: {dataset_id}")
        datasets.append(dataset)
    return datasets


def _merge_dataset_info(datasets: list[Dataset], work_dir: Path) -> tuple[str, list[str]]:
    """Write a merged ``dataset_info.json`` and return (dataset_dir, names)."""
    merged: dict[str, Any] = {}
    names: list[str] = []
    for dataset in datasets:
        entries = dataset_service.lf_entries(dataset)
        if not entries:
            raise BuildError(f"数据集 {dataset.name} 未生成 LLaMA-Factory 配置（类型不受支持？）")
        for raw_name, entry in entries.items():
            unique = f"{dataset.id}__{raw_name}"
            merged[unique] = entry
            names.append(unique)
    if not merged:
        raise BuildError("没有可用的数据集")
    data_dir = work_dir / "lf_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dataset_info.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(data_dir), names


def build(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    config: SFTConfig = job.sft  # type: ignore[assignment]
    if config is None:
        raise BuildError("缺少 SFT 配置")

    root = settings.llamafactory_root
    if not (root / "src" / "llamafactory").is_dir():
        raise BuildError(f"LLaMA-Factory 源码目录无效: {root}")

    stage = _STAGE_MAP.get(config.stage)
    if stage is None:
        raise BuildError(f"LLaMA-Factory 不支持算法: {config.stage.value}")

    train_datasets = _collect_datasets(config.dataset_ids)
    if not train_datasets:
        raise BuildError("请至少选择一个训练数据集")
    eval_datasets = _collect_datasets(config.eval_dataset_ids)

    dataset_dir, train_names = _merge_dataset_info(train_datasets + eval_datasets, work_dir)
    eval_names: list[str] = []
    if eval_datasets:
        eval_prefixes = {d.id for d in eval_datasets}
        eval_names = [n for n in train_names if n.split("__")[0] in eval_prefixes]
        train_names = [n for n in train_names if n.split("__")[0] not in eval_prefixes]

    cfg: dict[str, Any] = {
        # model
        "model_name_or_path": config.model_path,
        "trust_remote_code": config.trust_remote_code,
        # method
        "stage": stage,
        "do_train": True,
        "finetuning_type": config.finetuning_type,
        # dataset
        "dataset_dir": dataset_dir,
        "dataset": ",".join(train_names),
        "template": config.template,
        "cutoff_len": config.cutoff_len,
        "overwrite_cache": True,
        "preprocessing_num_workers": 16,
        "dataloader_num_workers": 4,
        # output
        "output_dir": str(output_dir),
        "logging_steps": config.logging_steps,
        "save_steps": config.save_steps,
        "plot_loss": config.plot_loss,
        "overwrite_output_dir": True,
        "save_only_model": config.save_only_model,
        "report_to": config.report_to,
        # train
        "per_device_train_batch_size": config.per_device_train_batch_size,
        "gradient_accumulation_steps": config.gradient_accumulation_steps,
        "learning_rate": config.learning_rate,
        "num_train_epochs": config.num_train_epochs,
        "lr_scheduler_type": config.lr_scheduler_type,
        "warmup_ratio": config.warmup_ratio,
        "weight_decay": config.weight_decay,
        "max_grad_norm": config.max_grad_norm,
        "ddp_timeout": 180000000,
    }

    if config.compute_type == "pure_bf16":
        cfg["pure_bf16"] = True
    else:
        cfg[config.compute_type] = True

    if config.max_samples:
        cfg["max_samples"] = config.max_samples
    if config.max_steps:
        cfg["max_steps"] = config.max_steps
    if config.gradient_checkpointing:
        cfg["gradient_checkpointing"] = True
    if config.quantization_bit:
        cfg["quantization_bit"] = config.quantization_bit
        cfg["quantization_method"] = "bnb"
    if config.flash_attn != "auto":
        cfg["flash_attn"] = config.flash_attn
    if config.liger_kernel:
        cfg["enable_liger_kernel"] = True
    if config.resume_from_checkpoint:
        cfg["resume_from_checkpoint"] = config.resume_from_checkpoint

    if config.finetuning_type == "lora":
        cfg.update(
            {
                "lora_rank": config.lora.rank,
                "lora_alpha": config.lora.alpha,
                "lora_dropout": config.lora.dropout,
                "lora_target": config.lora.target,
            }
        )
        if config.lora.create_new_adapter:
            cfg["create_new_adapter"] = True
    elif config.finetuning_type == "freeze":
        cfg["freeze_trainable_layers"] = config.freeze_trainable_layers

    if stage == "dpo":
        cfg["pref_beta"] = config.pref_beta
        cfg["pref_loss"] = _PREF_LOSS_OVERRIDE.get(config.stage, config.pref_loss)
        if config.pref_ftx:
            cfg["pref_ftx"] = config.pref_ftx

    if config.deepspeed_stage != "none":
        ds_path = root / _DEEPSPEED_CONFIGS[config.deepspeed_stage]
        if not ds_path.is_file():
            raise BuildError(f"DeepSpeed 配置缺失: {ds_path}")
        cfg["deepspeed"] = str(ds_path)

    if eval_names:
        cfg["eval_dataset"] = ",".join(eval_names)
        cfg["eval_strategy"] = "steps"
        cfg["eval_steps"] = config.eval_steps or config.save_steps
        cfg["per_device_eval_batch_size"] = config.per_device_train_batch_size
    elif config.val_size > 0:
        cfg["val_size"] = config.val_size
        cfg["eval_strategy"] = "steps"
        cfg["eval_steps"] = config.eval_steps or config.save_steps
        cfg["per_device_eval_batch_size"] = config.per_device_train_batch_size

    # user escape hatch — always applied last so it can override anything
    cfg.update(config.extra_args)

    config_yaml = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    python_bin = settings.python_for("llamafactory")

    env = {
        "PYTHONPATH": str(root / "src"),
        "LLAMAFACTORY_VERBOSITY": "INFO",
        "DISABLE_VERSION_CHECK": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    if job.resources.n_gpus > 1:
        env["FORCE_TORCHRUN"] = "1"
        env["NPROC_PER_NODE"] = str(job.resources.n_gpus)

    notes = [
        f"stage={stage} finetuning_type={config.finetuning_type} template={config.template}",
        f"训练集: {len(train_names)} 个（{sum(d.stats.n_records for d in train_datasets)} 条样本）",
    ]
    if config.deepspeed_stage != "none":
        notes.append(f"DeepSpeed: {config.deepspeed_stage}")

    return BuildResult(
        command=[python_bin, "-m", "llamafactory.cli", "train", "train_config.yaml"],
        cwd=str(work_dir),
        env=env,
        files={"train_config.yaml": config_yaml},
        output_dir=str(output_dir),
        notes=notes,
    )
