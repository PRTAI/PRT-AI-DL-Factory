"""样例审核：采证、防编造闸门、返回校验、汇总口径、报告固定格式、接口权限。

Agent 执行全部打桩（``review_agent_service.run``），测试不碰网络。真正值得测的是平台自己的
那一半：证据是不是从压缩包的记录里读出来的、模型胡编的质检项有没有被拦掉、
模型说不清的题有没有被算成通过、报告的四节骨架有没有变。
"""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from pathlib import Path

import pytest

from backend.app.models import (
    CheckStatus,
    ModelApiConfig,
    ReviewCreate,
    ReviewStatus,
    ReviewVerdict,
)
from backend.app.services import archive, review_agent_service, review_evidence, review_service
from backend.app.store import store
from .conftest import project_id

SPEC_TEXT = """\
# 外发版-评测题包交付规范（rewardkit）20260913

## §3.1 必填元数据
每道题必须填写 task_id、difficulty 等字段，且取值必须是非空字符串。

## §8.2.2 脚本可执行位
solution/solve.sh、tests/test.sh 必须带可执行位。
"""

TASK_TOML = 'version = "1.0"\n\n[metadata]\ntask_id = "{tid}"\ndifficulty = "{difficulty}"\n'

# 规范原文逐字摘录 —— 拆项阶段的 quote 必须能在文档里定位到
QUOTE_PERM = "solution/solve.sh、tests/test.sh 必须带可执行位"
QUOTE_META = "取值必须是非空字符串"


# --------------------------------------------------------------------------- #
# 样例数据
# --------------------------------------------------------------------------- #
def _write_zip(target: Path, files: dict[str, tuple[str, int]]) -> Path:
    """files: 相对路径 -> (内容, unix 权限位)。0 表示不写权限位。"""
    with zipfile.ZipFile(target, "w") as zf:
        for rel, (content, mode) in files.items():
            info = zipfile.ZipInfo(rel)
            if mode:
                info.external_attr = (mode | 0o100000) << 16
            zf.writestr(info, content)
    return target


@pytest.fixture
def samples_zip(tmp_path) -> Path:
    """外层 zip 里套一个领域 zip，内外各有一道题；两道题的 solve.sh 权限不同。"""
    inner = _write_zip(
        tmp_path / "PRT-金融-会计-20260918.zip",
        {
            # 记录的是 0644：缺可执行位
            "PRT-金融-会计-20260918/FIN-WF-006/task.toml": (
                TASK_TOML.format(tid="FIN-WF-006", difficulty="easy"),
                0o644,
            ),
            "PRT-金融-会计-20260918/FIN-WF-006/instruction.md": ("# 题目\n\n做个表。\n", 0o644),
            "PRT-金融-会计-20260918/FIN-WF-006/solution/solve.sh": ("#!/bin/bash\n", 0o644),
            "PRT-金融-会计-20260918/FIN-WF-006/tests/test.sh": ("#!/bin/bash\n", 0o755),
        },
    )
    outer = tmp_path / "samples.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, "金融/PRT-金融-会计-20260918.zip")
        for rel, (content, mode) in {
            "金融/FIN-WF-100/task.toml": (TASK_TOML.format(tid="FIN-WF-100", difficulty=""), 0o644),
            "金融/FIN-WF-100/instruction.md": ("# 题目\n\n对个账。\n", 0o644),
            "金融/FIN-WF-100/solution/solve.sh": ("#!/bin/bash\n", 0o755),
            "金融/FIN-WF-100/tests/test.sh": ("#!/bin/bash\n", 0o755),
        }.items():
            info = zipfile.ZipInfo(rel)
            info.external_attr = (mode | 0o100000) << 16
            zf.writestr(info, content)
    return outer


@pytest.fixture
def spec_doc(tmp_path) -> Path:
    path = tmp_path / "交付规范.md"
    path.write_text(SPEC_TEXT, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 打桩的模型
# --------------------------------------------------------------------------- #
PLAN_TWO_CHECKS = {
    "checks": [
        {
            "code": "F08",
            "title": "脚本可执行位",
            "source": "§8.2.2",
            "requirement": "solution/solve.sh、tests/test.sh 必须带可执行位",
            "quote": QUOTE_PERM,
            "how_to_judge": "看 files 里的 mode / executable",
            "evidence_needed": ["files"],
        },
        {
            "code": "M02",
            "title": "必填元数据为非空字符串",
            "source": "§3.1",
            "requirement": "task_id、difficulty 必须是非空字符串",
            "quote": QUOTE_META,
            "how_to_judge": "看 metadata 里的取值",
            "evidence_needed": ["metadata"],
        },
    ]
}


def _evidence_ids(user: str) -> list[str]:
    """从 prompt 的证据段里取题号（别把「输出格式」里的占位符也算进来）。"""
    body = user.split("## 待判定的题目证据")[-1].split("## 输出格式")[0]
    return re.findall(r'"task_id": "([^"]+)"', body)


def _stub_agent(monkeypatch, *, plan: dict, results):
    """results(code, task_ids) -> list[dict]"""
    calls: list[str] = []

    async def fake_agent(config, *, system, user, **kwargs):
        del config, kwargs
        if "质检方案设计者" in system:
            calls.append("plan")
            return json.dumps(plan, ensure_ascii=False), {"prompt_tokens": 9, "completion_tokens": 3}
        code = re.search(r"编号：(\S+)", user).group(1)
        calls.append(f"check:{code}")
        return json.dumps({"results": results(code, _evidence_ids(user))}, ensure_ascii=False), {}

    monkeypatch.setattr(review_agent_service, "run", fake_agent)
    return calls


def _honest_results(code: str, task_ids: list[str]) -> list[dict]:
    rows = []
    for task_id in task_ids:
        if code == "F08" and task_id == "FIN-WF-006":
            rows.append(
                {
                    "task_id": task_id,
                    "verdict": "fail",
                    "problems": ["solution/solve.sh 权限 0644，缺可执行位"],
                }
            )
        elif code == "M02" and task_id == "FIN-WF-100":
            rows.append(
                {"task_id": task_id, "verdict": "fail", "problems": ["difficulty 为空字符串"]}
            )
        else:
            rows.append({"task_id": task_id, "verdict": "pass", "problems": []})
    return rows


# --------------------------------------------------------------------------- #
# 提交 helper（绕开接口，直接用 service，这样能同步 await 整个 worker）
# --------------------------------------------------------------------------- #
def _model_api() -> ModelApiConfig:
    config = store.model_apis.first(lambda c: c.name == "review-stub")
    if config is None:
        config = ModelApiConfig(name="review-stub", provider="anthropic")
        store.model_apis.put(config)
    from .conftest import grant_key
    for user in store.users.all():  # 每个账号用自己的密钥，测试里大家都填同一个
        grant_key(user.id, config.id, "sk-stub-key")
    return config


def _owner():
    user = store.users.first(lambda u: u.email == "admin@prt.local")
    assert user is not None
    return user


def _submit(samples_zip: Path, spec_doc: Path, *, prompt: str = "") -> str:
    config = _model_api()
    with samples_zip.open("rb") as arc, spec_doc.open("rb") as doc:
        item = review_service.create_review(
            ReviewCreate(
                name="work0-1-0921",
                supplier="铂瑞缇",
                spec_title="外发版-评测题包交付规范（rewardkit）20260913",
                prompt=prompt,
                model_api_id=config.id,
                project_id=project_id(),
            ),
            archive_name=samples_zip.name,
            archive_stream=arc,
            docs=[(spec_doc.name, doc)],
            owner=_owner(),
        )
    return item.id


def _run(review_id: str) -> None:
    asyncio.run(review_service.run_review(review_id))


# --------------------------------------------------------------------------- #
# 阶段 ①：采证
# --------------------------------------------------------------------------- #
def test_inner_zip_is_expanded_with_bang_paths(samples_zip, tmp_path):
    result = archive.extract_recursive(samples_zip, tmp_path / "extracted")
    manifest = review_evidence.collect(result)

    by_id = {t["task_id"]: t for t in manifest["tasks"]}
    assert set(by_id) == {"FIN-WF-006", "FIN-WF-100"}
    assert by_id["FIN-WF-006"]["rel_path"] == (
        "金融/PRT-金融-会计-20260918.zip!/PRT-金融-会计-20260918/FIN-WF-006"
    )
    assert by_id["FIN-WF-100"]["rel_path"] == "金融/FIN-WF-100"
    assert "金融/PRT-金融-会计-20260918.zip" in result.inner_archives


def test_modes_come_from_the_archive_not_the_extracted_copy(samples_zip, tmp_path):
    """extractall 不保留可执行位，所以权限必须取压缩包里记录的那份。"""
    result = archive.extract_recursive(samples_zip, tmp_path / "extracted")
    manifest = review_evidence.collect(result)
    files = {
        t["task_id"]: {f["path"]: f for f in t["files"]} for t in manifest["tasks"]
    }

    assert files["FIN-WF-006"]["solution/solve.sh"]["mode"] == "0644"
    assert files["FIN-WF-006"]["solution/solve.sh"]["executable"] is False
    assert files["FIN-WF-006"]["tests/test.sh"]["executable"] is True
    assert files["FIN-WF-100"]["solution/solve.sh"]["executable"] is True


def test_metadata_keeps_empty_strings_verbatim(samples_zip, tmp_path):
    result = archive.extract_recursive(samples_zip, tmp_path / "extracted")
    manifest = review_evidence.collect(result)
    meta = next(t for t in manifest["tasks"] if t["task_id"] == "FIN-WF-100")["metadata"]
    # 归一化会把「必填字段是空串」这个问题抹掉，所以原值必须原样保留
    assert meta["task.toml"]["metadata"]["difficulty"] == ""


# --------------------------------------------------------------------------- #
# 阶段 ②：防编造闸门
# --------------------------------------------------------------------------- #
def test_fabricated_quote_is_dropped(monkeypatch, samples_zip, spec_doc):
    plan = {
        "checks": [
            PLAN_TWO_CHECKS["checks"][0],
            {
                "code": "X99",
                "title": "必须提供中文题解视频",
                "source": "§12.1",
                "requirement": "每题附 3 分钟讲解视频",
                "quote": "每题必须附带一段不少于三分钟的中文讲解视频",
                "how_to_judge": "看 files 里有没有 mp4",
                "evidence_needed": ["files"],
            },
        ]
    }
    _stub_agent(monkeypatch, plan=plan, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.SUCCEEDED, item.error
    dropped = [c for c in item.checks if c.status is CheckStatus.DROPPED]
    assert [c.code for c in dropped] == ["X99"]
    assert "找不到" in dropped[0].drop_reason
    assert any("X99" in note for note in item.notes)
    # 被丢弃的项不许进入报告正文（只在审核备注里留一句「丢弃了什么、为什么」）
    report = json.loads((review_service.work_dir(item) / "report.json").read_text("utf-8"))
    assert "X99" not in json.dumps(
        {k: v for k, v in report.items() if k != "notes"}, ensure_ascii=False
    )
    assert any("X99" in note for note in report["notes"])


def test_all_checks_fabricated_fails_the_review(monkeypatch, samples_zip, spec_doc):
    plan = {
        "checks": [
            {
                "code": "X01",
                "title": "凭空要求",
                "source": "§99",
                "requirement": "随便",
                "quote": "这段话在规范文档里根本不存在，纯属编造",
                "how_to_judge": "-",
                "evidence_needed": ["files"],
            }
        ]
    }
    _stub_agent(monkeypatch, plan=plan, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.FAILED
    assert "全都无法在需求文档里定位" in item.error


# --------------------------------------------------------------------------- #
# 阶段 ③：返回校验
# --------------------------------------------------------------------------- #
def test_unknown_task_id_is_dropped_and_missing_answer_needs_review(
    monkeypatch, samples_zip, spec_doc
):
    def sloppy(code: str, task_ids: list[str]) -> list[dict]:
        if code != "F08":
            return _honest_results(code, task_ids)
        # 一条凭空冒出来的题号 + 只答了一道题
        return [
            {"task_id": "FIN-WF-999", "verdict": "fail", "problems": ["不存在的题"]},
            {"task_id": task_ids[0], "verdict": "pass", "problems": []},
        ]

    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=sloppy)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.SUCCEEDED, item.error
    assert any("FIN-WF-999" in note for note in item.notes)

    findings = json.loads(
        (review_service.work_dir(item) / "findings" / "F08.json").read_text("utf-8")
    )["findings"]
    assert "FIN-WF-999" not in {f["task_id"] for f in findings}
    # 没被回答的那道题不能算通过
    unanswered = [f for f in findings if f["verdict"] == ReviewVerdict.NEEDS_REVIEW.value]
    assert len(unanswered) == 1
    assert "没有返回" in unanswered[0]["problems"][0]


def test_fail_without_problems_is_downgraded(monkeypatch, samples_zip, spec_doc):
    def mute(code: str, task_ids: list[str]) -> list[dict]:
        return [{"task_id": tid, "verdict": "fail", "problems": []} for tid in task_ids]

    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=mute)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.SUCCEEDED, item.error
    # 说不出问题就不算不合格，也绝不算通过
    assert item.stats.n_fail == 0
    assert item.stats.n_pass == 0
    assert item.stats.n_needs_review == 2


def test_non_json_reply_is_retried_then_fails_the_check(monkeypatch, samples_zip, spec_doc):
    attempts: list[str] = []

    async def fake_agent(config, *, system, user, **kwargs):
        del config, kwargs
        if "质检方案设计者" in system:
            return json.dumps(PLAN_TWO_CHECKS, ensure_ascii=False), {}
        code = re.search(r"编号：(\S+)", user).group(1)
        attempts.append(code)
        if code == "F08":
            return "抱歉，我无法完成这个请求。", {}
        task_ids = re.findall(r'"task_id": "([^"]+)"', user)
        return json.dumps({"results": _honest_results(code, task_ids)}, ensure_ascii=False), {}

    monkeypatch.setattr(review_agent_service, "run", fake_agent)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    # 保留报告，但不能把检查执行失败当作审核全部完成。
    assert item.status is ReviewStatus.FAILED
    assert item.report_path
    assert item.stats.n_pass == 0
    assert item.stats.n_needs_review >= 1
    assert attempts.count("F08") == 2  # 追加「只输出 JSON」重试一次
    failed = [c for c in item.checks if c.status is CheckStatus.FAILED]
    assert [c.code for c in failed] == ["F08"]
    assert any("F08" in note and "失败" in note for note in item.notes)


# --------------------------------------------------------------------------- #
# 阶段 ④：汇总与报告
# --------------------------------------------------------------------------- #
def test_aggregation_and_pass_rate(monkeypatch, samples_zip, spec_doc):
    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.SUCCEEDED, item.error
    assert item.stats.n_tasks == 2
    assert item.stats.n_fail == 2  # 两道题各栽在一个质检项上
    assert item.stats.n_pass == 0
    assert item.stats.n_checks == 2

    report = json.loads((review_service.work_dir(item) / "report.json").read_text("utf-8"))
    assert report["stats"]["pass_rate"] == 0.0
    assert {s["code"] for s in report["sections"]} == {"F08", "M02"}
    rows = {r["task_id"]: r for r in report["tasks"]}
    assert rows["FIN-WF-006"]["fail_codes"] == ["F08"]
    assert rows["FIN-WF-100"]["fail_codes"] == ["M02"]
    assert rows["FIN-WF-006"]["verdict_label"] == "不合格"


def test_needs_review_is_not_counted_as_passing():
    """通过率必须是「通过/受检」，不能是「受检-不合格」——否则待复核会被算成通过。"""
    from backend.app.models import ReviewCheck, ReviewFinding, ReviewTask
    from backend.app.services import review_report

    task = ReviewTask(name="b", owner_id="u")
    manifest = {
        "tasks": [
            {"task_id": "T1", "rel_path": "a/T1"},
            {"task_id": "T2", "rel_path": "a/T2"},
            {"task_id": "T3", "rel_path": "a/T3"},
            {"task_id": "T4", "rel_path": "a/T4"},
        ]
    }
    checks = [ReviewCheck(code="C01", title="某项")]
    findings = [
        ReviewFinding(check_code="C01", task_id="T1", verdict=ReviewVerdict.PASS),
        ReviewFinding(
            check_code="C01", task_id="T2", verdict=ReviewVerdict.FAIL, problems=["坏了"]
        ),
        ReviewFinding(
            check_code="C01", task_id="T3", verdict=ReviewVerdict.NEEDS_REVIEW, problems=["缺证据"]
        ),
        # 同一道题既有 fail 又有待复核 → 归不合格
        ReviewFinding(
            check_code="C01", task_id="T4", verdict=ReviewVerdict.FAIL, problems=["也坏了"]
        ),
        ReviewFinding(
            check_code="C01", task_id="T4", verdict=ReviewVerdict.NEEDS_REVIEW, problems=["顺带"]
        ),
    ]
    report = review_report.build_report(task, manifest, checks, findings)
    assert report["stats"] == {
        "n_tasks": 4,
        "n_pass": 1,
        "n_fail": 2,
        "n_needs_review": 1,
        "n_checks": 1,
        "pass_rate": 0.25,
    }
    assert review_report.stats_line(report["stats"]) == (
        "受检 4 题；通过 1 题；不合格 2 题；需人工复核 1 题；通过率 25.0%。"
    )


def test_docx_keeps_the_fixed_format(monkeypatch, samples_zip, spec_doc):
    def mixed(code: str, task_ids: list[str]) -> list[dict]:
        if code == "M02":
            return [
                {"task_id": tid, "verdict": "needs_review", "problems": ["字段含义不明"]}
                for tid in task_ids
            ]
        return _honest_results(code, task_ids)

    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=mixed)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.SUCCEEDED, item.error
    docx = Path(item.report_path)
    assert docx.is_file()

    xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
    texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)

    assert texts[0] == "work0-1-0921 静态验收不合格返修报告"
    assert texts[1] == "供应商：铂瑞缇"
    # 四节标题按固定顺序出现
    order = [t for t in texts if t in {"一、说明", "二、不合格明细", "三、受检题目清单", "四、需人工复核"}]
    assert order == ["一、说明", "二、不合格明细", "三、受检题目清单", "四、需人工复核"]
    assert any(t.startswith("依据：《外发版-评测题包交付规范") for t in texts)
    assert any(t.startswith("受检 2 题；通过 0 题；") for t in texts)
    # 不合格明细的「出处 / 要求」行与三列表头
    assert any(t.startswith("出处：§8.2.2") and "要求：" in t for t in texts)
    for head in ("题号", "源数据相对位置", "具体问题", "结论", "不合格项", "检查项", "待确认点"):
        assert head in texts
    # 固定配色与列宽
    assert 'w:fill="34495E"' in xml and 'w:fill="F8F9FA"' in xml
    assert 'w:color="BDC3C7"' in xml
    assert 'w:w="1180"' in xml  # 59pt = 1180 twips，第一列
    assert "微软雅黑" in xml


def test_manual_section_is_omitted_when_empty(monkeypatch, samples_zip, spec_doc):
    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    xml = zipfile.ZipFile(Path(item.report_path)).read("word/document.xml").decode("utf-8")
    assert "四、需人工复核" not in xml


# --------------------------------------------------------------------------- #
# 留存与接口
# --------------------------------------------------------------------------- #
def test_workspace_layout_is_preserved(monkeypatch, samples_zip, spec_doc):
    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    base = review_service.work_dir(item)
    assert base.name == item.id.removeprefix("rev_")
    assert len(base.name) == 16
    for rel in (
        "upload/samples.zip",
        "docs/交付规范.md",
        "extracted/金融/FIN-WF-100/task.toml",
        "manifest.json",
        "checks.json",
        "findings/F08.json",
        "report.json",
        "report.docx",
    ):
        assert (base / rel).exists(), rel
    # 模型不许留下过程文件：工作目录里只有平台自己写的 JSON、上传件与 docx
    produced = {q.suffix for q in base.glob("*")} | {q.suffix for q in (base / "agents" / "responses").glob("*")}
    assert produced <= {"", ".json", ".docx"}


def test_agent_archive_never_contains_the_api_key(monkeypatch, samples_zip, spec_doc):
    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)

    item = store.reviews.get(review_id)
    archived = list((review_service.work_dir(item) / "agents" / "responses").glob("*.json"))
    assert archived
    blob = "\n".join(p.read_text("utf-8") for p in archived)
    assert "sk-stub-key" not in blob
    assert '"agent": "claude-code"' in blob


def test_api_history_download_and_isolation(
    monkeypatch, client, member_headers, admin_headers, samples_zip, spec_doc
):
    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=_honest_results)
    config = _model_api()
    with samples_zip.open("rb") as arc, spec_doc.open("rb") as doc:
        response = client.post(
            "/api/reviews",
            headers=member_headers,
            files=[
                ("archive", ("samples.zip", arc.read(), "application/zip")),
                ("docs", ("交付规范.md", doc.read(), "text/markdown")),
            ],
            data={
                "name": "接口冒烟",
                "supplier": "铂瑞缇",
                "spec_title": "交付规范",
                "model_api_id": config.id,
                "project_id": project_id(),
            },
        )
    assert response.status_code == 201, response.text
    review_id = response.json()["id"]

    # worker 在同一个事件循环里跑，轮询等它结束
    for _ in range(200):
        detail = client.get(f"/api/reviews/{review_id}", headers=member_headers).json()
        if detail["status"] in {"succeeded", "failed", "cancelled"}:
            break
    assert detail["status"] == "succeeded", detail.get("error")

    mine = client.get("/api/reviews", headers=member_headers).json()
    assert review_id in {item["id"] for item in mine}
    # 别人的历史里看不到
    others = client.get("/api/reviews", headers=admin_headers).json()
    assert review_id not in {item["id"] for item in others}

    findings = client.get(f"/api/reviews/{review_id}/findings", headers=member_headers).json()
    assert len(findings) == 4  # 2 题 × 2 个质检项

    report = client.get(f"/api/reviews/{review_id}/report.docx", headers=member_headers)
    assert report.status_code == 200
    assert report.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert report.content[:2] == b"PK"

    raw = client.get(f"/api/reviews/{review_id}/archive", headers=member_headers)
    assert raw.status_code == 200 and raw.content[:2] == b"PK"

    # 管理员可以看别人的详情，普通成员不行
    assert client.get(f"/api/reviews/{review_id}", headers=admin_headers).status_code == 200
    stranger = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": "stranger@prt.local",
            "display_name": "路人",
            "password": "stranger-pass-1",
            "role": "user",
            "must_change_password": False,
        },
    )
    assert stranger.status_code in {201, 409}
    token = client.post(
        "/api/auth/login", json={"email": "stranger@prt.local", "password": "stranger-pass-1"}
    ).json()["access_token"]
    stranger_headers = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/reviews/{review_id}", headers=stranger_headers).status_code == 403
    assert client.get(f"/api/reviews/{review_id}/log", headers=stranger_headers).status_code == 403
    assert client.get(f"/api/reviews/{review_id}/log", headers=member_headers).status_code == 200
    assert client.delete(f"/api/reviews/{review_id}", headers=stranger_headers).status_code == 403


def test_submit_rejects_bad_input(client, member_headers, samples_zip, spec_doc):
    config = _model_api()
    # 没有需求文档
    with samples_zip.open("rb") as arc:
        response = client.post(
            "/api/reviews",
            headers=member_headers,
            files=[("archive", ("samples.zip", arc.read(), "application/zip"))],
            data={"name": "无文档", "model_api_id": config.id, "project_id": project_id()},
        )
    assert response.status_code == 422

    # 样例不是压缩包
    with spec_doc.open("rb") as doc:
        body = doc.read()
    response = client.post(
        "/api/reviews",
        headers=member_headers,
        files=[
            ("archive", ("样例.txt", b"not an archive", "text/plain")),
            ("docs", ("交付规范.md", body, "text/markdown")),
        ],
        data={"name": "错格式", "model_api_id": config.id, "project_id": project_id()},
    )
    assert response.status_code == 400
    assert "打包" in response.json()["detail"]

    # 模型 API 不存在
    response = client.post(
        "/api/reviews",
        headers=member_headers,
        files=[
            ("archive", ("samples.zip", samples_zip.read_bytes(), "application/zip")),
            ("docs", ("交付规范.md", body, "text/markdown")),
        ],
        data={"name": "坏模型", "model_api_id": "mdl_nope", "project_id": project_id()},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("project_value,expected", [
    (None, 422), ("", 422), ("   ", 400), ("prj_missing", 404), ("archived", 400),
])
def test_submission_requires_usable_project(
    monkeypatch, client, member_headers, samples_zip, spec_doc, project_value, expected,
):
    from backend.app.models import Project

    monkeypatch.setattr(review_service, "_spawn", lambda _: None)
    if project_value == "archived":
        project = Project(name="归档质检项目", owner_id=_owner().id, archived=True)
        store.projects.put(project)
        project_value = project.id
    data = {"name": "项目校验", "model_api_id": _model_api().id}
    if project_value is not None:
        data["project_id"] = project_value
    before = len(store.reviews.all())
    response = client.post(
        "/api/reviews", headers=member_headers, data=data,
        files=[
            ("archive", ("samples.zip", samples_zip.read_bytes(), "application/zip")),
            ("docs", ("规范.md", spec_doc.read_bytes(), "text/markdown")),
        ],
    )
    assert response.status_code == expected, response.text
    assert len(store.reviews.all()) == before


def test_review_project_is_canonical_and_history_is_scoped(
    monkeypatch, client, member_headers, samples_zip, spec_doc,
):
    monkeypatch.setattr(review_service, "_spawn", lambda _: None)
    project = client.post("/api/projects", headers=member_headers, json={"name": "质检归属测试"}).json()
    response = client.post(
        "/api/reviews", headers=member_headers,
        data={"name": "项目内质检", "model_api_id": _model_api().id, "project_id": project["slug"]},
        files=[
            ("archive", ("samples.zip", samples_zip.read_bytes(), "application/zip")),
            ("docs", ("规范.md", spec_doc.read_bytes(), "text/markdown")),
        ],
    )
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["project_id"] == project["id"]
    rows = client.get("/api/reviews", headers=member_headers, params={"project_id": project["id"]}).json()
    assert [row["id"] for row in rows] == [item["id"]]
    other_rows = client.get("/api/reviews", headers=member_headers, params={"project_id": project_id()}).json()
    assert item["id"] not in {row["id"] for row in other_rows}
    detail = client.get(f"/api/projects/{project['id']}", headers=member_headers).json()
    assert detail["stats"]["review_tasks"] == 1
    assert detail["project"]["updated_at"] >= item["created_at"]


@pytest.mark.parametrize("review_status", ["queued", "succeeded", "failed", "cancelled"])
def test_project_with_reviews_cannot_be_deleted(client, admin_headers, review_status):
    from backend.app.models import Project, ReviewTask

    project = Project(name=f"保留质检-{review_status}", owner_id=_owner().id)
    store.projects.put(project)
    item = ReviewTask(name="历史质检", owner_id=_owner().id, project_id=project.id, status=review_status)
    store.reviews.put(item)
    response = client.delete(f"/api/projects/{project.id}", headers=admin_headers)
    assert response.status_code == 409
    assert "质检" in response.json()["detail"]
    assert store.projects.get(project.id) is not None


def test_orphan_review_migration_preserves_valid_archived_project():
    from backend.app.models import Project, ReviewTask

    project = Project(name="已有归档项目", owner_id=_owner().id, archived=True)
    store.projects.put(project)
    blank = ReviewTask(name="无归属历史质检", owner_id=_owner().id)
    missing = ReviewTask(name="失效项目历史质检", owner_id=_owner().id, project_id="deleted_project")
    valid = ReviewTask(name="归档项目质检", owner_id=_owner().id, project_id=project.id)
    for item in (blank, missing, valid):
        store.reviews.put(item)
    try:
        assert review_service.adopt_orphan_reviews() == 2
        adopted = store.reviews.get(blank.id).project_id
        assert store.projects.get(adopted) is not None
        assert store.reviews.get(missing.id).project_id == adopted
        assert store.reviews.get(valid.id).project_id == project.id
        assert review_service.adopt_orphan_reviews() == 0
    finally:
        for item in (blank, missing, valid):
            store.reviews.delete(item.id)


def test_failed_agent_call_preserves_error(monkeypatch, samples_zip, spec_doc):
    async def fail(*args, **kwargs):
        raise review_agent_service.ReviewAgentError("Agent 未生成结论文件")

    monkeypatch.setattr(review_agent_service, "run", fail)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)
    item = store.reviews.get(review_id)
    assert item.status is ReviewStatus.FAILED
    logs = list((review_service.work_dir(item) / "agents" / "responses").glob("*.json"))
    assert len(logs) == 1
    archived = json.loads(logs[0].read_text())
    assert archived["error"] == "Agent 未生成结论文件"
    assert "sk-stub-key" not in logs[0].read_text()


def test_review_pipeline_does_not_call_bare_llm(monkeypatch, samples_zip, spec_doc):
    from backend.app.services import llm_client

    async def forbidden(*args, **kwargs):
        raise AssertionError("审核不得回退到直接 LLM 调用")

    monkeypatch.setattr(llm_client, "chat", forbidden)
    _stub_agent(monkeypatch, plan=PLAN_TWO_CHECKS, results=_honest_results)
    review_id = _submit(samples_zip, spec_doc)
    _run(review_id)
    assert store.reviews.get(review_id).status is ReviewStatus.SUCCEEDED


def test_restart_also_reaps_cancelled_reviews(monkeypatch):
    from backend.app.models import ReviewTask

    item = ReviewTask(name="已取消的审核", owner_id=_owner().id, project_id=project_id(), agent="claude-code", status=ReviewStatus.CANCELLED)
    store.reviews.put(item)
    reaped = []

    async def reap(entry):
        reaped.append(entry.id)

    monkeypatch.setattr(review_agent_service, "reap_orphans", reap)
    asyncio.run(review_service.reconcile())
    assert item.id in reaped
    assert store.reviews.get(item.id).status is ReviewStatus.CANCELLED


@pytest.mark.parametrize("agent,provider,expected", [
    ("claude-code", "anthropic", 201), ("codex", "openai", 201),
    ("claude-code", "openai", 400), ("codex", "anthropic", 400), ("oracle", "anthropic", 422),
])
def test_review_validates_harness_and_model_protocol(
    monkeypatch, client, member_headers, samples_zip, spec_doc, agent, provider, expected,
):
    from .conftest import grant_key, user_id_of

    monkeypatch.setattr(review_service, "_spawn", lambda _: None)
    config = ModelApiConfig(name="review-protocol", provider=provider)
    store.model_apis.put(config)
    grant_key(user_id_of("dev@prt.local"), config.id, "sk-review-protocol")
    response = client.post(
        "/api/reviews", headers=member_headers,
        data={"name": "Agent 组合", "project_id": project_id(), "model_api_id": config.id, "agent": agent},
        files=[
            ("archive", ("samples.zip", samples_zip.read_bytes(), "application/zip")),
            ("docs", ("规范.md", spec_doc.read_bytes(), "text/markdown")),
        ],
    )
    assert response.status_code == expected, response.text
    if expected == 201:
        assert response.json()["agent"] == agent
        assert store.reviews.get(response.json()["id"]).agent == agent
