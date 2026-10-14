"""Job lifecycle: create -> review -> queue -> run -> inspect."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..builders import BuildError, build_job
from ..config import settings
from ..redaction import has_secret_placeholders, restore_secret_placeholders
from ..models import (
    DatasetKind,
    Framework,
    Job,
    JobCreate,
    JobStatus,
    JobType,
    RLConfig,
    SFTConfig,
    TrainStage,
    User,
    utcnow,
)
from ..store import store
from ..models.job import HarborAction, VerifyBatchCreate
from . import audit_service, metrics_parser, model_service, project_service, runner
from .scheduler import refresh_metrics, scheduler

# Which dataset kinds each algorithm can actually consume.
_ALLOWED_DATASETS: dict[TrainStage, set[DatasetKind]] = {
    TrainStage.SFT: {DatasetKind.SFT_ALPACA, DatasetKind.SFT_SHAREGPT},
    TrainStage.DPO: {DatasetKind.DPO_PAIRS},
    TrainStage.ORPO: {DatasetKind.DPO_PAIRS},
    TrainStage.SIMPO: {DatasetKind.DPO_PAIRS},
    TrainStage.KTO: {DatasetKind.KTO_PAIRS},
    TrainStage.GRPO: {DatasetKind.VERL_PARQUET, DatasetKind.HARBOR_TASKS},
    TrainStage.DAPO: {DatasetKind.VERL_PARQUET, DatasetKind.HARBOR_TASKS},
    TrainStage.GSPO: {DatasetKind.VERL_PARQUET, DatasetKind.HARBOR_TASKS},
    TrainStage.PPO: {DatasetKind.VERL_PARQUET},
    TrainStage.RLOO: {DatasetKind.VERL_PARQUET},
    TrainStage.REINFORCE_PLUS_PLUS: {DatasetKind.VERL_PARQUET},
    TrainStage.REMAX: {DatasetKind.VERL_PARQUET},
}


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def create_job(payload: JobCreate, *, owner: User, harbor_action: HarborAction | None = None) -> Job:
    job, project_name = _prepare_job(payload, owner=owner, harbor_action=harbor_action)
    store.jobs.put(job)
    _announce_created_job(job, owner=owner, project_name=project_name)
    if job.status == JobStatus.QUEUED:
        scheduler.dispatch()
    return job


def create_verify_batch(payload: VerifyBatchCreate, *, owner: User) -> list[Job]:
    prepared: list[tuple[Job, str]] = []
    names: set[str] = set()
    for index, agent in enumerate(payload.agents, 1):
        item = payload.common.model_copy(deep=True)
        assert item.verify is not None
        item.verify = item.verify.model_copy(update=agent.model_dump())
        # Self-check agents have no model; never inherit another row's credentials.
        if agent.agent in {"oracle", "nop"}:
            item.verify.model = ""
            item.verify.model_api_id = ""
        try:
            job, project_name = _prepare_job(item, owner=owner)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, f"第 {index} 项（{agent.agent}）：{exc.detail}") from exc
        assert job.verify is not None
        suffix = (job.verify.model.strip().rstrip("/").rsplit("/", 1)[-1]
                  or job.verify.agent)
        base = f"{payload.common.name.strip()}-{suffix}"
        name = base
        counter = 2
        while name in names:
            name = f"{base}-{counter}"
            counter += 1
        if len(name) > 120:
            raise HTTPException(400, f"第 {index} 项的任务名称超过 120 字符，请缩短任务名称或模型名称")
        names.add(name)
        job.name = name
        prepared.append((job, project_name))
    # No persistence or scheduling until every row has passed validation.
    jobs = [job for job, _ in prepared]
    store.jobs.put_many(jobs)
    for job, project_name in prepared:
        _announce_created_job(job, owner=owner, project_name=project_name)
    if any(job.status == JobStatus.QUEUED for job in jobs):
        scheduler.dispatch()
    return jobs


def _prepare_job(
    payload: JobCreate, *, owner: User, harbor_action: HarborAction | None = None,
) -> tuple[Job, str]:
    # Editing a rerun receives masked API values. Resolve them server-side only
    # for the source owner/admin; never run a literal mask as an API credential.
    data = payload.model_dump()
    if has_secret_placeholders(data):
        if not payload.parent_job_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请填写真实密钥，不能提交脱敏占位值")
        source = get_job(payload.parent_job_id)
        _assert_can_control(source, owner)
        try:
            payload = JobCreate.model_validate(restore_secret_placeholders(data, source.model_dump()))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="原任务中没有对应密钥，请重新填写") from exc
    framework = _framework_for(payload)
    project = project_service.assert_usable(payload.project_id)
    _validate_resources(payload, owner)

    if payload.type == JobType.SFT:
        _validate_sft(payload)
    elif payload.type == JobType.RL:
        _validate_rl(payload)
    else:
        _validate_verify(payload, harbor_action=harbor_action)

    system = store.system_settings()
    require_approval = system.require_approval if system.updated_by else settings.require_approval
    initial_status = JobStatus.PENDING_REVIEW if require_approval else JobStatus.QUEUED

    job = Job(
        name=payload.name.strip(),
        note=payload.note,
        type=payload.type,
        framework=framework,
        status=initial_status,
        owner_id=owner.id,
        owner_email=owner.email,
        project_id=project.id,
        tags=payload.tags,
        resources=payload.resources,
        sft=payload.sft,
        rl=payload.rl,
        verify=payload.verify,
        parent_job_id=payload.parent_job_id,
        harbor_action=harbor_action,
    )
    job.review.required = require_approval
    if initial_status == JobStatus.QUEUED:
        job.queued_at = utcnow()

    return job, project.name


def _announce_created_job(job: Job, *, owner: User, project_name: str) -> None:
    project_service.touch(job.project_id)
    audit_service.record(
        "job.create",
        actor=owner,
        target_type="job",
        target_id=job.id,
        summary=f"在项目「{project_name}」提交{_TYPE_TEXT[job.type]}任务「{job.name}」({job.framework.value})",
        detail={"gpus": job.resources.n_gpus, "project_id": job.project_id},
    )
    if job.review.required:
        audit_service.notify_admins(
            "有新的任务待审核",
            body=f"{owner.email} 提交了{_TYPE_TEXT[job.type]}任务「{job.name}」",
            level="warning",
            link=f"/jobs/{job.id}",
        )


_TYPE_TEXT = {JobType.SFT: "训练", JobType.RL: "强化学习", JobType.VERIFY: "评测"}


def _framework_for(payload: JobCreate) -> Framework:
    if payload.type == JobType.SFT:
        if payload.sft is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少 SFT 配置")
        return Framework.LLAMAFACTORY
    if payload.type == JobType.RL:
        if payload.rl is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少 RL 配置")
        return payload.rl.framework
    if payload.verify is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少评测配置")
    return Framework.HARBOR


def _validate_resources(payload: JobCreate, owner: User) -> None:
    system = store.system_settings()
    limit = owner.max_gpus_per_job or system.default_max_gpus_per_job or 8
    if payload.resources.n_gpus > limit:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"单个任务最多可申请 {limit} 张 GPU"
        )
    total = scheduler.total_gpus
    if total and payload.resources.n_gpus > total:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"本机共有 {total} 张 GPU，无法申请 {payload.resources.n_gpus} 张"
        )


def _dataset_or_400(dataset_id: str, *, label: str):
    dataset = store.datasets.get(dataset_id)
    if dataset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{label}不存在: {dataset_id}")
    if dataset.status.value != "ready":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{label} {dataset.name} 尚未就绪")
    return dataset


def _check_kind(dataset, stage: TrainStage) -> None:
    allowed = _ALLOWED_DATASETS.get(stage, set())
    if allowed and dataset.kind not in allowed:
        names = " / ".join(sorted(k.value for k in allowed))
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{stage.value.upper()} 需要 {names} 类型的数据集，当前为 {dataset.kind.value}",
        )


def _check_learning_rate(value: float, *, upper: float, label: str) -> None:
    """离谱的学习率在提交时就拦下。

    真机回归：数字框把 1e-6 显示成 0.000001，用户一编辑就提交成了 1.000001 ——
    这种值 RL 一步就把模型训炸，排队几分钟后才发现太浪费。
    """
    if not (0 < value <= upper):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{label}学习率 {value!r} 不合理（应在 0 到 {upper:g} 之间，常用量级 1e-6 ~ 1e-4）。"
            "输入框支持科学计数法，直接输 1e-6 即可。",
        )


def _validate_sft(payload: JobCreate) -> None:
    config = payload.sft
    assert config is not None
    _check_learning_rate(config.learning_rate, upper=0.1, label="SFT ")
    config.model_path = model_service.resolve_model_path(config.model_id, config.model_path)
    if not config.dataset_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请至少选择一个训练数据集")
    for dataset_id in config.dataset_ids:
        _check_kind(_dataset_or_400(dataset_id, label="数据集"), config.stage)
    for dataset_id in config.eval_dataset_ids:
        _check_kind(_dataset_or_400(dataset_id, label="验证集"), config.stage)
    if config.stage not in {TrainStage.SFT, TrainStage.DPO, TrainStage.KTO, TrainStage.ORPO, TrainStage.SIMPO}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"LLaMA-Factory 不支持算法 {config.stage.value}"
        )
    if not config.template or config.template == "auto":
        entry = model_service.get_model(config.model_id) if config.model_id else None
        config.template = (entry.llamafactory_template if entry else None) or "default"


def _validate_rl(payload: JobCreate) -> None:
    config = payload.rl
    assert config is not None
    _check_learning_rate(config.learning_rate, upper=0.01, label="RL ")
    config.model_path = model_service.resolve_model_path(config.model_id, config.model_path)
    if not config.dataset_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请选择训练数据集")
    dataset = _dataset_or_400(config.dataset_id, label="数据集")
    _check_kind(dataset, config.algorithm)

    if dataset.kind == DatasetKind.HARBOR_TASKS:
        if config.framework != Framework.TRL:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Harbor 任务树目前只能通过 TRL 做 agentic RL，请把框架切换为 TRL",
            )
        config.harbor.enabled = True
    if config.framework == Framework.VERL and config.algorithm == TrainStage.GSPO:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="GSPO 请使用 TRL 框架")
    # GRPO / RLOO 系都要求至少 2 条采样才能算组内相对优势（TRL 自己也会拒）
    if config.rollout.n < 2 and config.algorithm in {
        TrainStage.GRPO, TrainStage.DAPO, TrainStage.GSPO, TrainStage.RLOO
    }:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="组相对算法的 rollout 组大小至少为 2")


def _validate_verify(payload: JobCreate, *, harbor_action: HarborAction | None = None) -> None:
    config = payload.verify
    assert config is not None
    from ..builders.harbor import validate_secret_env

    if config.use_platform_image and harbor_action is None:
        from . import harbor_image_service
        if config.environment_type != "docker" or config.registry_dataset or config.force_build:
            raise HTTPException(400, "平台镜像需要本地任务集、Docker 环境，并关闭强制重建")
        if not harbor_image_service.status()["ready"]:
            raise HTTPException(400, "平台 Harbor 镜像尚未就绪，请管理员先构建")

    try:
        for env_source in (config.agent_env, config.verifier_env, payload.resources.env):
            validate_secret_env(env_source)
    except BuildError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not (config.dataset_id or config.tasks_path or config.registry_dataset):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请选择要评测的 Harbor 任务集")
    if config.dataset_id:
        dataset = _dataset_or_400(config.dataset_id, label="任务集")
        if dataset.kind != DatasetKind.HARBOR_TASKS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"评测需要 harbor_tasks 类型的数据集，当前为 {dataset.kind.value}",
            )
        if dataset.rewardkit and not config.disable_verification:
            if not config.judge_model_api_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="该任务集是 reward-kit 格式，请选择 judge 模型（用于注入 JUDGE_* 环境变量）",
                )
    if config.batch_judge and not config.disable_verification:
        from .harbor_action_service import verifier_path
        from .harbor_judge_service import batch_judge_patches
        try:
            batch_judge_patches(verifier_path(config))
        except (BuildError, OSError, ValueError) as exc:
            raise HTTPException(400, f"无法启用 Batch Judge: {exc}") from exc
    from . import model_api_service
    model_api_service.agent_protocol(config.agent, config.agent_api_protocol)
    if config.judge_model_api_id and not config.disable_verification:
        model_api_service.require_ready(config.judge_model_api_id, config.judge_api_protocol)
    if harbor_action is not None and harbor_action.mode != "rerun":
        # Recorded configuration supplies the agent for resume; regrade never runs it.
        return
    if config.model_api_id:
        # 提交时就把「没填密钥 / 已停用」挡下来，别等排队到了才失败
        from . import resources_service

        resolved = model_api_service.resolve(
            config.model_api_id, model_style=resources_service.model_style_for(config.agent),
            protocol=model_api_service.agent_protocol(config.agent, config.agent_api_protocol),
        )
        try:
            validate_secret_env({**resolved.env, **config.agent_env})
        except BuildError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        config.model = resolved.model  # 列表/详情页展示用
    elif config.agent not in {"oracle", "nop"} and not config.model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"agent {config.agent} 需要指定模型")


# --------------------------------------------------------------------------- #
# Review / control
# --------------------------------------------------------------------------- #
def review_job(job_id: str, *, approve: bool, comment: str, reviewer: User) -> Job:
    job = get_job(job_id)
    if job.status != JobStatus.PENDING_REVIEW:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该任务不在待审核状态")

    def mutate(item: Job) -> None:
        item.review.reviewer_id = reviewer.id
        item.review.reviewer_email = reviewer.email
        item.review.reviewed_at = utcnow()
        item.review.comment = comment
        if approve:
            item.status = JobStatus.QUEUED
            item.queued_at = utcnow()
            item.progress.message = "排队中"
        else:
            item.status = JobStatus.REJECTED
            item.progress.message = "已驳回"
        item.updated_at = utcnow()

    updated = store.jobs.update(job_id, mutate)
    assert updated is not None
    audit_service.record(
        "job.approve" if approve else "job.reject",
        actor=reviewer,
        target_type="job",
        target_id=job_id,
        summary=f"{'通过' if approve else '驳回'}任务「{updated.name}」" + (f"：{comment}" if comment else ""),
    )
    audit_service.notify(
        updated.owner_id,
        f"任务 {updated.name} {'已通过审核' if approve else '被驳回'}",
        body=comment or ("已进入排队队列" if approve else "请修改后重新提交"),
        level="success" if approve else "warning",
        link=f"/jobs/{job_id}",
    )
    if approve:
        scheduler.dispatch()
    return updated


def cancel_job(job_id: str, *, actor: User, reason: str = "") -> Job:
    with runner.LIFECYCLE_LOCK:
        job = get_job(job_id)
        _assert_can_control(job, actor)
        if job.status.is_terminal:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="任务已结束，无法终止")

        def mutate(item: Job) -> None:
            item.runtime.cancel_requested = True
            item.updated_at = utcnow()
            if item.status in {JobStatus.QUEUED, JobStatus.PENDING_REVIEW, JobStatus.PREPARING}:
                item.resources.gpu_ids = []
                item.status = JobStatus.CANCELLED
                item.runtime.finished_at = utcnow()
                item.progress.message = "已取消"

        updated = store.jobs.update(job_id, mutate)
        assert updated is not None
    if updated.status == JobStatus.RUNNING or updated.status == JobStatus.PREPARING:
        runner.terminate(updated)
    audit_service.record(
        "job.cancel",
        actor=actor,
        target_type="job",
        target_id=job_id,
        summary=f"终止任务「{updated.name}」" + (f"：{reason}" if reason else ""),
    )
    if actor.id != updated.owner_id:
        audit_service.notify(
            updated.owner_id,
            f"任务 {updated.name} 被管理员终止",
            body=reason,
            level="warning",
            link=f"/jobs/{job_id}",
        )
    return store.jobs.get(job_id) or updated


def rename_job(job_id: str, *, name: str, actor: User) -> Job:
    old_name = ""

    def mutate(item: Job) -> None:
        nonlocal old_name
        _assert_can_control(item, actor)
        if not item.status.is_terminal:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="仅已结束的任务可以改名")
        old_name = item.name
        item.name = name
        item.updated_at = utcnow()

    updated = store.jobs.update(job_id, mutate)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="任务不存在")
    audit_service.record(
        "job.rename",
        actor=actor,
        target_type="job",
        target_id=job_id,
        summary=f"将任务「{old_name}」改名为「{updated.name}」",
        detail={"old_name": old_name, "new_name": updated.name},
    )
    return updated


def resubmit_job(job_id: str, *, actor: User) -> Job:
    """Clone a finished/rejected job back into the review queue."""
    source = get_job(job_id)
    _assert_can_control(source, actor)
    sft, rl, note = source.sft, source.rl, source.note
    # 旧任务可能带着后来才被禁止的坏值（真机上 GRPO 的学习率被数字框改成了 1.000001）。
    # 重跑时替用户改回该算法的默认值并写进备注，而不是把人堵在「重跑」按钮上。
    fixes: list[str] = []
    if rl is not None and not (0 < rl.learning_rate <= 0.01):
        default_lr = RLConfig.model_fields["learning_rate"].default
        fixes.append(f"学习率 {rl.learning_rate!r} 不合理，已改回默认 {default_lr:g}")
        rl = rl.model_copy(update={"learning_rate": default_lr})
    if sft is not None and not (0 < sft.learning_rate <= 0.1):
        default_lr = SFTConfig.model_fields["learning_rate"].default
        fixes.append(f"学习率 {sft.learning_rate!r} 不合理，已改回默认 {default_lr:g}")
        sft = sft.model_copy(update={"learning_rate": default_lr})
    if fixes:
        note = (note + "\n" if note else "") + "重跑时自动修正：" + "；".join(fixes)

    # 名字别一直叠「(重跑) (重跑)」
    base_name = source.name.replace(" (重跑)", "").strip()
    payload = JobCreate(
        name=f"{base_name} (重跑)",
        note=note,
        type=source.type,
        project_id=source.project_id,
        tags=source.tags,
        resources=source.resources.model_copy(update={"gpu_ids": []}),
        sft=sft,
        rl=rl,
        verify=source.verify,
        parent_job_id=source.id,
    )
    # Repeating a recovery/regrade must retain its operation. Dropping this
    # would silently run the agent again when the user asked only to re-score.
    return create_job(payload, owner=actor, harbor_action=source.harbor_action)


def batch_resubmit_jobs(job_ids: list[str], *, actor: User) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for job_id in dict.fromkeys(job_ids):
        try:
            source = get_job(job_id)
            _assert_can_control(source, actor)
            if not source.status.is_terminal:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="仅支持重跑已成功、失败、终止或驳回的任务")
            cloned = resubmit_job(job_id, actor=actor)
            results.append({"source_id": job_id, "job_id": cloned.id, "status": cloned.status.value, "error": ""})
        except HTTPException as exc:
            results.append({"source_id": job_id, "job_id": "", "status": "", "error": str(exc.detail)})
    return {"results": results, "succeeded": sum(bool(row["job_id"]) for row in results),
            "failed": sum(bool(row["error"]) for row in results)}


def delete_job(job_id: str, *, actor: User, purge_outputs: bool = False) -> None:
    from .harbor_action_service import SOURCE_LOCK
    with SOURCE_LOCK:
        _delete_job(job_id, actor=actor, purge_outputs=purge_outputs)


def _delete_job(job_id: str, *, actor: User, purge_outputs: bool = False) -> None:
    job = get_job(job_id)
    _assert_can_control(job, actor)
    if job.status.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="请先终止任务再删除")
    dependents = store.jobs.find(lambda item: item.harbor_action is not None and item.harbor_action.source_job_id == job_id)
    if dependents:
        raise HTTPException(409, "该任务仍被恢复或重新判分任务引用，请先删除这些派生任务")
    work_dir = Path(job.runtime.work_dir or runner.work_dir_for(job))
    if work_dir.is_dir():
        shutil.rmtree(work_dir, ignore_errors=True)
    if purge_outputs:
        output_dir = runner.output_dir_for(job)
        if output_dir.is_dir():
            shutil.rmtree(output_dir, ignore_errors=True)
    store.jobs.delete(job_id)
    audit_service.record(
        "job.delete",
        actor=actor,
        target_type="job",
        target_id=job_id,
        summary=f"删除任务「{job.name}」",
        detail={"purge_outputs": purge_outputs},
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_job(job_id: str) -> Job:
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return job


def get_job_for(job_id: str, actor: User) -> Job:
    job = get_job(job_id)
    _assert_can_view(job, actor)
    # Terminal jobs are re-parsed too: it is one bounded log-tail read per page
    # open, and it lets a parser fix heal jobs that finished before the upgrade.
    return refresh_metrics(job)


def list_jobs(
    *,
    actor: User,
    mine_only: bool = False,
    statuses: list[JobStatus] | None = None,
    types: list[JobType] | None = None,
    project_id: str = "",
    owner_id: str = "",
    query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[Job], int]:
    def keep(job: Job) -> bool:
        if mine_only and job.owner_id != actor.id:
            return False
        if owner_id and job.owner_id != owner_id:
            return False
        if project_id and job.project_id != project_id:
            return False
        if statuses and job.status not in statuses:
            return False
        if types and job.type not in types:
            return False
        if query:
            haystack = f"{job.name} {job.note} {job.owner_email} {job.id} {' '.join(job.tags)}".lower()
            if query.lower() not in haystack:
                return False
        return True

    items = store.jobs.find(keep)
    items.sort(key=lambda j: j.created_at, reverse=True)
    total = len(items)
    return items[offset : offset + limit], total


def job_log(job_id: str, *, actor: User, tail_bytes: int | None = None, offset: int = 0) -> dict[str, Any]:
    job = get_job(job_id)
    _assert_can_view(job, actor)
    path = Path(job.runtime.log_file or "")
    if not path.is_file():
        return {"content": "", "size": 0, "offset": 0, "eof": True}
    size = path.stat().st_size
    limit = tail_bytes or settings.log_tail_bytes
    with path.open("rb") as handle:
        if offset > 0:
            handle.seek(min(offset, size))
        elif size > limit:
            handle.seek(size - limit)
            handle.readline()
        data = handle.read()
    return {
        "content": data.decode("utf-8", errors="replace"),
        "size": size,
        "offset": size,
        "eof": job.status.is_terminal,
    }


def job_metrics(job_id: str, *, actor: User) -> dict[str, Any]:
    job = get_job(job_id)
    _assert_can_view(job, actor)
    job = refresh_metrics(job)
    work_dir = Path(job.runtime.work_dir or runner.work_dir_for(job))
    payload = metrics_parser.read_series(work_dir)
    payload["status"] = job.status.value
    payload["framework"] = job.framework.value
    return payload


def job_artifacts(job_id: str, *, actor: User) -> list[dict[str, Any]]:
    """Checkpoints / result files produced by the job."""
    job = get_job(job_id)
    _assert_can_view(job, actor)
    output_dir = Path(job.runtime.output_dir or runner.output_dir_for(job))
    if not output_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(output_dir.iterdir()):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "is_dir": path.is_dir(),
                "size_bytes": stat.st_size if path.is_file() else None,
                "modified_at": stat.st_mtime,
            }
        )
    return items[:500]


def artifact_file(job_id: str, relative_path: str, *, actor: User) -> Path:
    """Resolve a downloadable artifact, refusing anything outside the job's output."""
    job = get_job(job_id)
    _assert_can_view(job, actor)
    root = Path(job.runtime.output_dir or runner.output_dir_for(job)).resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="非法的产物路径") from exc
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="产物不存在或不是文件")
    return target


def job_trials(
    job_id: str,
    *,
    actor: User,
    page: int = 1,
    page_size: int = 10,
    phase: str = "",
    query: str = "",
) -> dict[str, Any]:
    """评测任务逐 trial 的阶段（分页）。跑着的排前面，其次失败，再是完成。"""
    job = get_job(job_id)
    _assert_can_view(job, actor)
    if job.type != JobType.VERIFY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="只有评测任务有 trial 明细")
    from . import harbor_trials

    output_dir = Path(job.runtime.output_dir or runner.output_dir_for(job))
    harbor_dir = metrics_parser.harbor_job_dir(output_dir, job.id)
    if harbor_dir is None:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "pages": 1,
                "counts": {p: 0 for p in harbor_trials.PHASES} | {"total": 0, "passed": 0}, "job_dir": str(output_dir)}
    return harbor_trials.page(harbor_dir, page_no=page, page_size=page_size, phase=phase, query=query,
                              job_status=job.status.value,
                              finished_at=job.runtime.finished_at.isoformat() if job.runtime.finished_at else None)


def job_trial_detail(job_id: str, trial_name: str, *, actor: User) -> dict[str, Any]:
    job = get_job(job_id)
    _assert_can_view(job, actor)
    from . import harbor_trials

    output_dir = Path(job.runtime.output_dir or runner.output_dir_for(job))
    harbor_dir = metrics_parser.harbor_job_dir(output_dir, job.id)
    payload = harbor_trials.detail(
        harbor_dir, trial_name, job_status=job.status.value,
        finished_at=job.runtime.finished_at.isoformat() if job.runtime.finished_at else None,
    ) if harbor_dir else None
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="trial 不存在")
    return payload


def _duration(started: Any, finished: Any) -> float | None:
    if not started or not finished:
        return None
    try:
        from datetime import datetime

        return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    except (TypeError, ValueError):
        return None


def preview_command(job_id: str, *, actor: User) -> dict[str, Any]:
    """Render the exact command a job will run — used by reviewers."""
    job = get_job(job_id)
    _assert_can_view(job, actor)
    work_dir = runner.work_dir_for(job)
    output_dir = runner.output_dir_for(job)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = build_job(job, work_dir, output_dir)
    except BuildError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "command": result.masked_command_line(),
        "cwd": result.cwd,
        "env": result.masked_env(),
        "notes": result.notes,
        "files": {name: content[:20000] for name, content in result.files.items()},
        "output_dir": result.output_dir,
    }


def verify_from_training(job_id: str, *, actor: User) -> dict[str, Any]:
    """Suggest a follow-up verification for a finished training job."""
    job = get_job(job_id)
    _assert_can_view(job, actor)
    if job.type == JobType.VERIFY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="该任务本身就是评测任务")
    if job.status != JobStatus.SUCCEEDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="只有成功结束的训练任务才能发起评测")
    output_dir = Path(job.runtime.output_dir or runner.output_dir_for(job))
    checkpoints = [
        str(p) for p in sorted(output_dir.glob("checkpoint-*")) if p.is_dir()
    ] if output_dir.is_dir() else []
    # 顺带把项目带回去：评测某次训练的产物，理应落在同一个项目里，
    # 而不是落在用户当前恰好选中的那个项目。
    project = store.projects.get(job.project_id)
    return {
        "parent_job_id": job.id,
        "parent_job_name": job.name,
        "project_id": job.project_id,
        "project_name": project.name if project else "",
        "model_path": str(output_dir),
        "checkpoints": checkpoints,
        "hint": "评测本地训练产物时，建议先用 vLLM 起一个 OpenAI 兼容服务，再选择 terminus / claude-code 等 agent。",
    }


# --------------------------------------------------------------------------- #
def _assert_can_view(job: Job, actor: User) -> None:
    """任务在团队内是共享的（像 wandb 的项目一样），登录即可查看。

    写操作仍然收在 :func:`_assert_can_control`。
    """
    del job, actor


def _assert_can_control(job: Job, actor: User) -> None:
    if actor.is_admin or job.owner_id == actor.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="无权操作该任务")


def stats_overview(actor: User, *, project_id: str = "") -> dict[str, Any]:
    jobs = store.jobs.all()
    if project_id:
        jobs = [j for j in jobs if j.project_id == project_id]
    # 任务在团队内共享，所以看板统计的是全量；"我的" 单独给一个计数。
    visible = jobs
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for job in visible:
        by_status[job.status.value] = by_status.get(job.status.value, 0) + 1
        by_type[job.type.value] = by_type.get(job.type.value, 0) + 1
    return {
        "total": len(visible),
        "mine": sum(1 for j in visible if j.owner_id == actor.id),
        "by_status": by_status,
        "by_type": by_type,
        "pending_review": sum(1 for j in jobs if j.status == JobStatus.PENDING_REVIEW),
        "running": sum(1 for j in visible if j.status == JobStatus.RUNNING),
        "queued": sum(1 for j in visible if j.status == JobStatus.QUEUED),
    }
