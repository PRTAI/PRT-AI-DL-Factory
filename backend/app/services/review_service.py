"""样例审核：上传 → 采证 → 拆质检项 → 并发 subagent 判定 → 固定格式 docx。

不走 GPU 调度队列（不吃显卡，只吃模型 API 额度），四个阶段都在平台进程内跑，
所以位置类比 ``deployment_service`` / ``export_service``。

两条硬规矩，都是为了报告可信：

1. **模型不采证**。文件在不在、权限位多少、字段是不是空串，全部由
   ``review_evidence`` 用代码读出来；模型只在这些事实上判「符不符合规范」。
2. **模型不许编规范**。拆出的每个质检项必须附规范原文逐字摘录（``quote``），
   平台回查需求文档，定位不上的项**直接丢弃**并写进备注。

模型也不被允许产生任何落盘产物：没有脚本、没有中间文件，交付物只有 docx
（外加平台自己写的 manifest / checks / findings / report JSON，用于复查）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import HTTPException, status

from ..config import settings
from ..models import (
    CheckStatus,
    ModelApiConfig,
    ReviewCheck,
    ReviewCreate,
    ReviewFinding,
    ReviewStage,
    ReviewStats,
    ReviewStatus,
    ReviewTask,
    ReviewVerdict,
    User,
    utcnow,
)
from ..store import store
from . import archive, audit_service, llm_client, review_evidence, review_report

CHUNK = 4 * 1024 * 1024
MAX_DOCS = 10
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")
# 给模型看的证据视图白名单：拆项阶段只能从这些里面挑，挑不出来就用默认集合
EVIDENCE_VIEWS = ("present", "files", "metadata", "instruction", "solution", "tests", "environment")
DEFAULT_VIEWS = ("present", "files", "metadata")

_running: dict[str, asyncio.Task] = {}


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #
def work_dir(item: ReviewTask) -> Path:
    return settings.reviews_dir / item.workspace


def _paths(item: ReviewTask) -> dict[str, Path]:
    base = work_dir(item)
    return {
        "base": base,
        "upload": base / "upload",
        "docs": base / "docs",
        "extracted": base / "extracted",
        "findings": base / "findings",
        "llm": base / "llm",
        "manifest": base / "manifest.json",
        "checks": base / "checks.json",
        "report_json": base / "report.json",
        "report_docx": base / "report.docx",
    }


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_reviews(*, actor: User, mine_only: bool = True, project_id: str = "") -> list[ReviewTask]:
    def keep(item: ReviewTask) -> bool:
        if project_id and item.project_id != project_id:
            return False
        if mine_only or not actor.is_admin:
            return item.owner_id == actor.id
        return True

    items = store.reviews.find(keep)
    return sorted(items, key=lambda i: i.created_at, reverse=True)


def get_review(review_id: str, *, actor: User) -> ReviewTask:
    item = store.reviews.get(review_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="审核记录不存在")
    _assert_can_view(item, actor)
    return item


def findings_of(review_id: str, *, actor: User) -> list[dict[str, Any]]:
    """逐题结论。数据量可能上万条，所以只存在磁盘上，不进 reviews.json。"""
    item = get_review(review_id, actor=actor)
    directory = _paths(item)["findings"]
    rows: list[dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.extend(data.get("findings") or [])
    return rows


def report_file(review_id: str, *, actor: User) -> Path:
    item = get_review(review_id, actor=actor)
    path = Path(item.report_path) if item.report_path else _paths(item)["report_docx"]
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="报告还没生成")
    return path


def archive_file(review_id: str, *, actor: User) -> Path:
    item = get_review(review_id, actor=actor)
    candidates = sorted(_paths(item)["upload"].glob("*")) if _paths(item)["upload"].is_dir() else []
    files = [p for p in candidates if p.is_file()]
    if not files:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="原始压缩包已不存在")
    return files[0]


def _assert_can_view(item: ReviewTask, actor: User) -> None:
    if actor.is_admin or item.owner_id == actor.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只有审核的发起人或管理员可以查看")


# --------------------------------------------------------------------------- #
# 创建
# --------------------------------------------------------------------------- #
def create_review(
    payload: ReviewCreate,
    *,
    archive_name: str,
    archive_stream: BinaryIO,
    docs: list[tuple[str, BinaryIO]],
    owner: User,
) -> ReviewTask:
    config = _model_api_or_400(payload.model_api_id)
    if not docs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请至少上传一份需求文档（质检规范）")
    if len(docs) > MAX_DOCS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"需求文档最多 {MAX_DOCS} 份")
    if not archive_name.lower().endswith(ARCHIVE_SUFFIXES):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="样例请打包成 .zip / .tar / .tar.gz")

    item = ReviewTask(
        name=payload.name.strip(),
        supplier=payload.supplier.strip(),
        spec_title=payload.spec_title.strip(),
        prompt=payload.prompt.strip(),
        model_api_id=config.id,
        model_label=f"{config.name}（{config.model_id}）",
        owner_id=owner.id,
        owner_email=owner.email,
        project_id=payload.project_id,
        archive_name=archive_name,
    )
    paths = _paths(item)
    item.work_dir = str(paths["base"])
    for key in ("upload", "docs"):
        paths[key].mkdir(parents=True, exist_ok=True)

    try:
        item.archive_bytes = _save(archive_stream, paths["upload"] / _safe_name(archive_name))
        names: list[str] = []
        for filename, stream in docs:
            safe = _safe_name(filename)
            if Path(safe).suffix.lower() not in review_evidence.DOC_SUFFIXES:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"需求文档 {filename} 格式不支持，请用 docx / pdf / md / txt",
                )
            _save(stream, paths["docs"] / safe)
            names.append(safe)
        item.doc_names = names
    except HTTPException:
        shutil.rmtree(paths["base"], ignore_errors=True)
        raise

    store.reviews.put(item)
    audit_service.record(
        "review.create",
        actor=owner,
        target_type="review",
        target_id=item.id,
        summary=f"提交样例审核「{item.name}」",
        detail={"workspace": item.workspace, "docs": item.doc_names, "model_api": config.id},
    )
    _spawn(item.id)
    return item


def _save(stream: BinaryIO, dest: Path) -> int:
    total = 0
    limit = settings.max_upload_mb * 1024 * 1024
    with dest.open("wb") as handle:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                handle.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"{dest.name} 超过 {settings.max_upload_mb} MB 上限",
                )
            handle.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{dest.name} 是空文件")
    return total


def _safe_name(filename: str) -> str:
    """只取文件名部分，去掉路径与控制字符——用户上传的名字直接进磁盘路径。"""
    name = Path(filename.replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip().strip(".") or "upload"
    return name[:180]


def _model_api_or_400(model_api_id: str) -> ModelApiConfig:
    if not model_api_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请选择用于审核的模型 API")
    config = store.model_apis.get(model_api_id)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="模型 API 配置不存在")
    if not config.ready:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"模型 API「{config.name}」不可用：{'已停用' if not config.enabled else '没有配置密钥'}",
        )
    return config


# --------------------------------------------------------------------------- #
# 控制
# --------------------------------------------------------------------------- #
def cancel_review(review_id: str, *, actor: User) -> ReviewTask:
    item = get_review(review_id, actor=actor)
    _assert_can_control(item, actor)
    if item.status.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该审核已经结束")
    task = _running.pop(review_id, None)
    if task is not None:
        task.cancel()
    _mark(review_id, ReviewStatus.CANCELLED, error="已取消")
    return get_review(review_id, actor=actor)


def delete_review(review_id: str, *, actor: User, purge_files: bool = False) -> None:
    item = get_review(review_id, actor=actor)
    _assert_can_control(item, actor)
    if not item.status.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="请先取消再删除")
    if purge_files:
        shutil.rmtree(work_dir(item), ignore_errors=True)
    store.reviews.delete(review_id)
    audit_service.record(
        "review.delete",
        actor=actor,
        target_type="review",
        target_id=review_id,
        summary=f"删除样例审核「{item.name}」",
        detail={"purge_files": purge_files},
    )


def _assert_can_control(item: ReviewTask, actor: User) -> None:
    if actor.is_admin or item.owner_id == actor.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只有审核的发起人或管理员可以操作")


def reconcile() -> None:
    """平台重启后把没人看管的审核标成失败。

    审核跑在事件循环里，没有可以接管的子进程——重启就等于中断，只能让用户重提。
    """
    for item in store.reviews.find(lambda i: not i.status.is_terminal):
        if item.id in _running:
            continue
        _mark(item.id, ReviewStatus.FAILED, error="平台重启时审核被中断，请重新提交")


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def _spawn(review_id: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环（测试里直接 await run_review）
        return
    task = loop.create_task(run_review(review_id))
    _running[review_id] = task
    task.add_done_callback(lambda _t: _running.pop(review_id, None))


async def run_review(review_id: str) -> None:
    item = store.reviews.get(review_id)
    if item is None:
        return
    config = store.model_apis.get(item.model_api_id)
    if config is None:
        _mark(review_id, ReviewStatus.FAILED, error="模型 API 配置已被删除")
        return

    def start(entry: ReviewTask) -> None:
        entry.status = ReviewStatus.RUNNING
        entry.stage = ReviewStage.COLLECTING
        entry.started_at = utcnow()
        entry.updated_at = utcnow()

    store.reviews.update(review_id, start)

    try:
        manifest = await asyncio.to_thread(_stage_collect, review_id)
        if not manifest["tasks"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="压缩包里没找到任何题目（题目目录需要包含 task.toml / instruction.md / metadata.json 之一）",
            )
        docs = await asyncio.to_thread(_read_docs, review_id)
        checks = await _stage_plan(review_id, config, manifest, docs)
        findings = await _stage_check(review_id, config, manifest, checks)
        await asyncio.to_thread(_stage_report, review_id, manifest, checks, findings)
    except asyncio.CancelledError:
        _mark(review_id, ReviewStatus.CANCELLED, error="已取消")
        raise
    except HTTPException as exc:
        _mark(review_id, ReviewStatus.FAILED, error=str(exc.detail))
    except Exception as exc:  # noqa: BLE001 - 审核失败要留下原因，不能让异常消失
        _mark(review_id, ReviewStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


def _mark(review_id: str, new_status: ReviewStatus, *, error: str = "") -> None:
    def mutate(entry: ReviewTask) -> None:
        entry.status = new_status
        entry.error = error
        entry.updated_at = utcnow()
        if new_status.is_terminal:
            entry.finished_at = utcnow()
            if new_status is ReviewStatus.SUCCEEDED:
                entry.stage = ReviewStage.DONE

    store.reviews.update(review_id, mutate)


def _note(review_id: str, text: str) -> None:
    def mutate(entry: ReviewTask) -> None:
        if text not in entry.notes:
            entry.notes.append(text)
        entry.updated_at = utcnow()

    store.reviews.update(review_id, mutate)


def _stage(review_id: str, stage: ReviewStage) -> None:
    def mutate(entry: ReviewTask) -> None:
        entry.stage = stage
        entry.updated_at = utcnow()

    store.reviews.update(review_id, mutate)


def _current(review_id: str) -> ReviewTask:
    item = store.reviews.get(review_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="审核记录已被删除")
    return item


# ------------------------------------------------------------------ 阶段 ① ---
def _stage_collect(review_id: str) -> dict[str, Any]:
    item = _current(review_id)
    paths = _paths(item)
    upload = sorted(p for p in paths["upload"].glob("*") if p.is_file())
    if not upload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="上传的压缩包已不存在")

    shutil.rmtree(paths["extracted"], ignore_errors=True)
    extracted = archive.extract_recursive(upload[0], paths["extracted"])
    manifest = review_evidence.collect(extracted)
    review_evidence.write_manifest(manifest, paths["manifest"])

    for note in manifest["notes"]:
        _note(review_id, note)

    def mutate(entry: ReviewTask) -> None:
        entry.stats.n_tasks = manifest["n_tasks"]
        entry.updated_at = utcnow()

    store.reviews.update(review_id, mutate)
    return manifest


def _read_docs(review_id: str) -> list[dict[str, str]]:
    item = _current(review_id)
    docs = []
    for name in item.doc_names:
        path = _paths(item)["docs"] / name
        if path.is_file():
            docs.append({"name": name, "text": review_evidence.read_document_text(path)})
    if not docs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="需求文档已不存在")
    return docs


# ------------------------------------------------------------------ 阶段 ② ---
PLAN_SYSTEM = """\
你是评测题包静态验收的质检方案设计者。用户会给你一份或多份《交付规范》文档，
以及平台能从样例数据里读到的证据字段说明。你的任务是把规范里**明确写出**的验收要求
拆成一条条可判定的质检项。

铁律：
1. 只能来自规范原文。规范没写的要求一律不许出现，不要凭经验补充「业界惯例」。
2. 每条质检项必须给出 quote —— 从规范里**逐字复制**的一段原文（不少于 10 个字，
   不要改写、不要省略号拼接）。平台会回文档里做原文比对，比对不上的质检项会被丢弃。
3. 拆得细：一条质检项只管一件事（一个字段、一个文件、一种权限），不要把多个要求塞进一条。
   规范里有多少条可判定的要求就拆多少条，但不要为了凑数把一条要求拆成同义的多条。
4. 只拆「静态可判定」的要求：看文件、看字段、看文本就能下结论。需要真跑容器、
   跑测试、联网核查的要求不要拆进来。
5. code 用规范自己的编号（例如 F08、M02、§8.2.2）；规范没有编号时按 C01、C02 顺序生成。

只输出 JSON，不要解释，不要 markdown 围栏。"""

PLAN_USER = """\
## 交付规范文档
{docs}

## 用户补充要求（优先级高于你的判断，但不能突破上面的铁律）
{prompt}

## 平台能提供的证据字段（每道题都有）
{evidence_doc}

## 数据样本（真实证据，节选 {n_sample} 道题，共 {n_tasks} 道）
{sample}

## 输出格式
{{"checks": [
  {{"code": "F08",
    "title": "脚本可执行位",
    "source": "§8.2.2",
    "requirement": "写进报告的一句话要求（不超过 80 字）",
    "quote": "规范原文逐字摘录",
    "how_to_judge": "拿到上述证据后怎么判定合格/不合格，写清楚判据",
    "evidence_needed": ["files"]}}
]}}

evidence_needed 只能从这些里选：{views}"""

EVIDENCE_DOC = """\
- present: 关键文件/目录是否存在（task.toml、instruction.md、metadata.json、environment/、tests/、solution/）
- files: 文件清单，每个文件有 path、size、mode（**压缩包里记录的权限位**，八进制字符串；
  null 表示压缩包没有记录权限位，这种情况不能判定权限相关的项）、executable
- metadata: task.toml 与 metadata.json 的解析结果，值保持原样（空字符串就是空字符串）
- instruction: instruction.md 正文（可能被截断）
- solution: solution/solve.* 正文（可能被截断）
- tests: tests/test.* 正文（可能被截断）
- environment: environment/Dockerfile 等环境定义正文（可能被截断）"""


async def _stage_plan(
    review_id: str,
    config: ModelApiConfig,
    manifest: dict[str, Any],
    docs: list[dict[str, str]],
) -> list[ReviewCheck]:
    _stage(review_id, ReviewStage.PLANNING)
    item = _current(review_id)

    samples = manifest["tasks"][:2]
    user = PLAN_USER.format(
        docs="\n\n".join(f"### {d['name']}\n{d['text']}" for d in docs),
        prompt=item.prompt or "（无）",
        evidence_doc=EVIDENCE_DOC,
        n_sample=len(samples),
        n_tasks=manifest["n_tasks"],
        sample=_dump([_project(t, EVIDENCE_VIEWS) for t in samples]),
        views="、".join(EVIDENCE_VIEWS),
    )
    data = await _ask_json(review_id, config, system=PLAN_SYSTEM, user=user, tag="plan")
    raw = data.get("checks") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="模型没有拆出任何质检项，请检查需求文档内容")

    corpus = _normalize("\n".join(d["text"] for d in docs))
    checks: list[ReviewCheck] = []
    dropped: list[ReviewCheck] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            continue
        check = ReviewCheck(
            code=str(entry.get("code") or f"C{index:02d}").strip()[:32],
            title=str(entry.get("title") or "").strip()[:120],
            source=str(entry.get("source") or "").strip()[:120],
            requirement=str(entry.get("requirement") or "").strip()[:400],
            quote=str(entry.get("quote") or "").strip(),
            how_to_judge=str(entry.get("how_to_judge") or "").strip()[:1200],
        )
        views = [v for v in (entry.get("evidence_needed") or []) if v in EVIDENCE_VIEWS]
        check.views = views or list(DEFAULT_VIEWS)
        if not check.title:
            continue
        if check.code in seen:
            check.code = f"{check.code}-{index}"
        seen.add(check.code)

        reason = _quote_problem(check.quote, corpus)
        if reason:
            check.status = CheckStatus.DROPPED
            check.drop_reason = reason
            dropped.append(check)
        checks.append(check)

    kept = [c for c in checks if c.status is not CheckStatus.DROPPED]
    _write_json(
        _paths(item)["checks"],
        {
            "kept": [c.model_dump(mode="json") for c in kept],
            "dropped": [c.model_dump(mode="json") for c in dropped],
        },
    )
    for check in dropped:
        _note(review_id, f"丢弃质检项 {check.code} {check.title}：{check.drop_reason}")
    if not kept:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="拆出的质检项全都无法在需求文档里定位到原文，已全部丢弃；请确认上传的是正确的质检规范",
        )

    def mutate(entry: ReviewTask) -> None:
        entry.checks = checks
        entry.stats.n_checks = len(kept)
        entry.updated_at = utcnow()

    store.reviews.update(review_id, mutate)
    return kept


def _quote_problem(quote: str, corpus: str) -> str:
    """原文比对闸门。返回空串表示通过。"""
    if len(quote) < 10:
        return "没有给出足够长的规范原文摘录（少于 10 字）"
    if _normalize(quote) not in corpus:
        return f"规范原文摘录在需求文档里找不到：「{quote[:60]}」"
    return ""


def _normalize(text: str) -> str:
    """比对前抹掉空白差异：模型复制原文时常把换行、全角空格、缩进弄丢。"""
    return "".join(text.split())


# ------------------------------------------------------------------ 阶段 ③ ---
CHECK_SYSTEM = """\
你是评测题包静态验收的执行者。本次你**只负责一条质检项**，其他问题一概不管、不报。

铁律：
1. 只依据用户给出的证据判定。证据里没有的信息不许推测，也不许假设文件内容。
2. 判定只有三种：
   - pass：证据足以证明这道题满足这条要求；
   - fail：证据足以证明这道题**违反**这条要求，此时必须在 problems 里写清楚
     具体违反点（引用证据里的字段名/文件名/取值），一条问题一句话；
   - needs_review：证据不足以下结论（例如压缩包没有记录权限位、字段含义模糊）。
     此时在 problems 里写「还缺什么才能判定」。
3. 不要把 pass 的题写进 problems；不要输出与本质检项无关的问题。
4. 证据里的每一道题都必须有且只有一条结论。

只输出 JSON，不要解释，不要 markdown 围栏。"""

CHECK_USER = """\
## 本次质检项
编号：{code}
名称：{title}
出处：{source}
要求：{requirement}
规范原文：{quote}
判定方法：{how_to_judge}

## 用户补充要求
{prompt}

## 证据字段说明
{evidence_doc}

## 待判定的题目证据（{n} 道）
{evidence}

## 输出格式
{{"results": [
  {{"task_id": "题号（必须与证据里的 task_id 完全一致）",
    "verdict": "pass | fail | needs_review",
    "problems": ["具体问题，fail 时必填"]}}
]}}"""


async def _stage_check(
    review_id: str,
    config: ModelApiConfig,
    manifest: dict[str, Any],
    checks: list[ReviewCheck],
) -> list[ReviewFinding]:
    _stage(review_id, ReviewStage.CHECKING)
    item = _current(review_id)
    _paths(item)["findings"].mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(max(1, settings.review_concurrency))
    results = await asyncio.gather(
        *(
            _run_check(review_id, config, manifest, check, semaphore, item.prompt)
            for check in checks
        ),
        return_exceptions=True,
    )

    findings: list[ReviewFinding] = []
    for check, result in zip(checks, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            _set_check(review_id, check.code, status=CheckStatus.FAILED, error=f"{type(result).__name__}: {result}")
            _note(review_id, f"质检项 {check.code} {check.title} 执行失败：{result}")
            continue
        findings.extend(result)
    return findings


async def _run_check(
    review_id: str,
    config: ModelApiConfig,
    manifest: dict[str, Any],
    check: ReviewCheck,
    semaphore: asyncio.Semaphore,
    prompt: str,
) -> list[ReviewFinding]:
    async with semaphore:
        _set_check(review_id, check.code, status=CheckStatus.RUNNING)
        known = {t["task_id"]: t.get("rel_path", "") for t in manifest["tasks"]}
        findings: list[ReviewFinding] = []
        for batch in _batches(manifest["tasks"], check.views):
            findings.extend(
                await _judge_batch(review_id, config, check, batch, known, prompt)
            )

        answered = {f.task_id for f in findings}
        for task in manifest["tasks"]:
            if task["task_id"] in answered:
                continue
            # 模型漏答的题绝不能算通过——那就是「9/10 崩了却显示 100%」那类错误
            findings.append(
                ReviewFinding(
                    check_code=check.code,
                    task_id=task["task_id"],
                    rel_path=task.get("rel_path", ""),
                    verdict=ReviewVerdict.NEEDS_REVIEW,
                    problems=["模型没有返回这道题的判定结果"],
                )
            )

        item = _current(review_id)
        _write_json(
            _paths(item)["findings"] / f"{_slug(check.code)}.json",
            {
                "check": check.model_dump(mode="json"),
                "findings": [f.model_dump(mode="json") for f in findings],
            },
        )
        _set_check(
            review_id,
            check.code,
            status=CheckStatus.DONE,
            counts={
                ReviewVerdict.FAIL: sum(1 for f in findings if f.verdict is ReviewVerdict.FAIL),
                ReviewVerdict.PASS: sum(1 for f in findings if f.verdict is ReviewVerdict.PASS),
                ReviewVerdict.NEEDS_REVIEW: sum(
                    1 for f in findings if f.verdict is ReviewVerdict.NEEDS_REVIEW
                ),
            },
        )
        return findings


async def _judge_batch(
    review_id: str,
    config: ModelApiConfig,
    check: ReviewCheck,
    batch: list[dict[str, Any]],
    known: dict[str, str],
    prompt: str,
) -> list[ReviewFinding]:
    user = CHECK_USER.format(
        code=check.code,
        title=check.title,
        source=check.source or "（规范未标注）",
        requirement=check.requirement or check.title,
        quote=check.quote,
        how_to_judge=check.how_to_judge or "按上面的要求判定",
        prompt=prompt or "（无）",
        evidence_doc=EVIDENCE_DOC,
        n=len(batch),
        evidence=_dump([_project(t, check.views) for t in batch]),
    )
    data = await _ask_json(
        review_id, config, system=CHECK_SYSTEM, user=user, tag=f"check-{_slug(check.code)}"
    )
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("模型返回里没有 results 数组")

    batch_ids = {t["task_id"] for t in batch}
    findings: list[ReviewFinding] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if task_id not in batch_ids:
            _note(
                review_id,
                f"质检项 {check.code} 返回了不存在的题号「{task_id[:40]}」，已丢弃",
            )
            continue
        if task_id in seen:
            continue
        seen.add(task_id)

        problems = [
            str(p).strip()[:500]
            for p in (row.get("problems") or [])
            if str(p).strip()
        ][:10]
        verdict = _verdict_of(row.get("verdict"))
        if verdict is ReviewVerdict.FAIL and not problems:
            # 说不出问题就不算不合格：宁可让人复核，也不能往报告里写空条目
            verdict = ReviewVerdict.NEEDS_REVIEW
            problems = ["模型判为不合格但没有说明具体问题"]
        findings.append(
            ReviewFinding(
                check_code=check.code,
                task_id=task_id,
                rel_path=known.get(task_id, ""),
                verdict=verdict,
                problems=problems if verdict is not ReviewVerdict.PASS else [],
            )
        )
    return findings


def _verdict_of(value: Any) -> ReviewVerdict:
    text = str(value or "").strip().lower()
    if text in {"fail", "failed", "不合格", "不通过"}:
        return ReviewVerdict.FAIL
    if text in {"pass", "passed", "ok", "通过", "合格"}:
        return ReviewVerdict.PASS
    return ReviewVerdict.NEEDS_REVIEW


def _set_check(
    review_id: str,
    code: str,
    *,
    status: CheckStatus,
    error: str = "",
    counts: dict[ReviewVerdict, int] | None = None,
) -> None:
    def mutate(entry: ReviewTask) -> None:
        for check in entry.checks:
            if check.code != code:
                continue
            check.status = status
            check.error = error
            if counts:
                check.n_fail = counts[ReviewVerdict.FAIL]
                check.n_pass = counts[ReviewVerdict.PASS]
                check.n_needs_review = counts[ReviewVerdict.NEEDS_REVIEW]
        entry.stats.n_checks_done = sum(
            1 for c in entry.checks if c.status in {CheckStatus.DONE, CheckStatus.FAILED}
        )
        entry.updated_at = utcnow()

    store.reviews.update(review_id, mutate)


# ------------------------------------------------------------------ 阶段 ④ ---
def _stage_report(
    review_id: str,
    manifest: dict[str, Any],
    checks: list[ReviewCheck],
    findings: list[ReviewFinding],
) -> None:
    _stage(review_id, ReviewStage.REPORTING)
    item = _current(review_id)
    paths = _paths(item)

    report = review_report.build_report(item, manifest, checks, findings)
    review_report.write_report_json(report, paths["report_json"])
    review_report.render_docx(report, paths["report_docx"])

    stats = report["stats"]

    def mutate(entry: ReviewTask) -> None:
        entry.stats = ReviewStats(
            n_tasks=stats["n_tasks"],
            n_pass=stats["n_pass"],
            n_fail=stats["n_fail"],
            n_needs_review=stats["n_needs_review"],
            n_checks=len(checks),
            n_checks_done=sum(1 for c in entry.checks if c.status is CheckStatus.DONE),
            n_findings=len(findings),
        )
        entry.report_path = str(paths["report_docx"])
        entry.status = ReviewStatus.SUCCEEDED
        entry.stage = ReviewStage.DONE
        entry.finished_at = utcnow()
        entry.updated_at = utcnow()

    store.reviews.update(review_id, mutate)


# --------------------------------------------------------------------------- #
# 证据投影 / 批次 / LLM
# --------------------------------------------------------------------------- #
def _project(task: dict[str, Any], views: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """只把这条质检项需要的证据发出去——prompt 长度直接决定成本和可靠性。"""
    out: dict[str, Any] = {"task_id": task["task_id"], "rel_path": task.get("rel_path", "")}
    texts = task.get("texts") or {}
    for view in views:
        if view == "present":
            out["present"] = task.get("present", {})
        elif view == "files":
            out["files"] = task.get("files", [])
            if task.get("files_truncated"):
                out["files_truncated"] = True
        elif view == "metadata":
            out["metadata"] = task.get("metadata", {})
            if task.get("metadata_errors"):
                out["metadata_errors"] = task["metadata_errors"]
        elif view == "instruction":
            out["instruction"] = texts.get("instruction.md", "")
        elif view == "solution":
            out["solution"] = {k: v for k, v in texts.items() if k.startswith("solution/")}
        elif view == "tests":
            out["tests"] = {k: v for k, v in texts.items() if k.startswith("tests/")}
        elif view == "environment":
            out["environment"] = {k: v for k, v in texts.items() if k.startswith("environment/")}
    return out


def _batches(tasks: list[dict[str, Any]], views: list[str]) -> list[list[dict[str, Any]]]:
    """按题数和字符预算双重切分，避免 prompt 超长把整条质检项拖垮。"""
    size = max(1, settings.review_batch_tasks)
    budget = max(4000, settings.review_batch_chars)
    out: list[list[dict[str, Any]]] = []
    for start in range(0, len(tasks), size):
        chunk = tasks[start : start + size]
        out.extend(_split_to_budget(chunk, views, budget))
    return out


def _split_to_budget(
    chunk: list[dict[str, Any]], views: list[str], budget: int
) -> list[list[dict[str, Any]]]:
    if len(chunk) <= 1 or len(_dump([_project(t, views) for t in chunk])) <= budget:
        return [chunk]
    middle = len(chunk) // 2
    return _split_to_budget(chunk[:middle], views, budget) + _split_to_budget(
        chunk[middle:], views, budget
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


async def _ask_json(
    review_id: str,
    config: ModelApiConfig,
    *,
    system: str,
    user: str,
    tag: str,
) -> Any:
    """发一次请求并解析 JSON；解析失败追加「只输出 JSON」再试一次。"""
    attempt_user = user
    last = ""
    for attempt in (1, 2):
        text, usage = await llm_client.chat(
            config,
            system=system,
            user=attempt_user,
            timeout=settings.review_llm_timeout_seconds,
        )
        _archive_call(review_id, config, tag=tag, attempt=attempt, user=attempt_user, text=text)
        _add_usage(review_id, usage)
        try:
            return llm_client.extract_json(text)
        except ValueError as exc:
            last = str(exc)
            attempt_user = (
                user + "\n\n注意：上一次回复无法解析成 JSON。只输出 JSON 本体，"
                "不要任何解释文字，不要 markdown 围栏。"
            )
    raise ValueError(f"模型输出无法解析成 JSON：{last}")


def _archive_call(
    review_id: str,
    config: ModelApiConfig,
    *,
    tag: str,
    attempt: int,
    user: str,
    text: str,
) -> None:
    """留档排错用。密钥不在这里——``redacted_request`` 只给出请求头的键名。"""
    try:
        item = _current(review_id)
        directory = _paths(item)["llm"]
        directory.mkdir(parents=True, exist_ok=True)
        seq = len(list(directory.glob("*.json"))) + 1
        _write_json(
            directory / f"{seq:03d}-{tag}-{attempt}.json",
            {
                "request": llm_client.redacted_request(config, {"user_chars": len(user)}),
                "prompt": user,
                "response": text,
            },
        )
    except Exception:  # noqa: BLE001 - 留档失败不该影响审核
        pass


def _add_usage(review_id: str, usage: dict[str, int]) -> None:
    def mutate(entry: ReviewTask) -> None:
        entry.usage["calls"] = int(entry.usage.get("calls", 0)) + 1
        for key, value in usage.items():
            entry.usage[key] = int(entry.usage.get(key, 0)) + value

    store.reviews.update(review_id, mutate)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _slug(code: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)
    return safe.strip("_") or "check"
