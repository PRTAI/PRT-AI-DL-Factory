"""Dataset upload, validation and safety."""

from __future__ import annotations

import io
import json
import zipfile

import pytest


def _harbor_contract_zip(make_zip, *, prefix="harbor-format-data/qa001-capacity-audit"):
    return make_zip(
        {
            f"{prefix}/task.toml": 'version = "1.0"\n',
            f"{prefix}/instruction.md": "# 成交容量审计\n",
            f"{prefix}/environment/Dockerfile": "FROM ubuntu:24.04\n",
            f"{prefix}/environment/input_files/audit_contract.json": json.dumps(
                {"schema_version": 3, "aggregation": "股票×交易日", "amount_unit": "yuan"}
            ),
            f"{prefix}/tests/test.sh": "#!/bin/bash\necho 1.0 > /logs/verifier/reward.txt\n",
            f"{prefix}/solution/solve.sh": "#!/bin/bash\necho audit\n",
        },
        "harbor-contract.zip",
    )


def _upload(client, headers, path, *, name, kind, **extra):
    with open(path, "rb") as handle:
        data = {"name": name, "kind": kind, "description": "pytest", "tags": "test", **extra}
        return client.post(
            "/api/datasets",
            headers=headers,
            files={"file": (path.name, handle, "application/zip")},
            data=data,
        )


def test_upload_alpaca(client, member_headers, alpaca_zip):
    response = _upload(client, member_headers, alpaca_zip, name="alpaca-demo", kind="sft_alpaca")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["project_id"]
    assert body["stats"]["n_records"] == 12
    assert body["lf_dataset_names"] == ["train"]

    preview = client.get(f"/api/datasets/{body['id']}/preview", headers=member_headers).json()
    assert len(preview["records"]) == 5


def test_upload_harbor_tasks(client, member_headers, harbor_zip):
    response = _upload(client, member_headers, harbor_zip, name="harbor-demo", kind="harbor_tasks")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["stats"]["n_tasks"] == 3
    # root_path must be the *suite* dir: harbor iterates its children looking
    # for task dirs, so task.toml has to live one level below it.
    from pathlib import Path

    suite = Path(body["root_path"])
    assert sorted(p.parent.name for p in suite.glob("*/task.toml")) == [
        "demo-task-0",
        "demo-task-1",
        "demo-task-2",
    ]

    preview = client.get(f"/api/datasets/{body['id']}/preview", headers=member_headers).json()
    assert preview["kind"] == "harbor_tasks"
    assert preview["tasks"][0]["instruction"].startswith("# 任务")


@pytest.mark.parametrize("kind", ["harbor_tasks", "sft_alpaca"])
def test_upload_harbor_wrapper_matches_first_task(client, member_headers, make_zip, kind):
    from pathlib import Path

    wrapper = "PRT-医疗-001-20261003-1"
    files = {f"{wrapper}/.DS_Store": "metadata", ".DS_Store": "metadata"}
    task_names = [f"PRT-医疗-{i:03d}-20261003-1" for i in range(1, 4)]
    for task_name in task_names:
        prefix = f"{wrapper}/{task_name}"
        files.update({
            f"{prefix}/task.toml": 'version = "1.0"\n',
            f"{prefix}/instruction.md": "# 医疗任务\n",
            f"{prefix}/environment/Dockerfile": "FROM ubuntu:24.04\n",
            f"{prefix}/tests/test.sh": "#!/bin/bash\ntrue\n",
            f"{prefix}/solution/solve.sh": "#!/bin/bash\ntrue\n",
        })
    response = _upload(client, member_headers, make_zip(files), name="medical-suite", kind=kind)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "harbor_tasks"
    assert body["status"] == "ready"
    assert body["stats"]["n_tasks"] == 3
    root = Path(body["root_path"])
    assert sorted(path.parent.name for path in root.glob("*/task.toml")) == task_names
    assert not list(root.glob(".prt-flatten-*"))


@pytest.mark.parametrize("kind", ["sft_alpaca", "sft_sharegpt", "dpo_pairs", "kto_pairs", "verl_parquet"])
@pytest.mark.parametrize("prefix", ["harbor-format-data/qa001-capacity-audit", "qa001-capacity-audit"])
def test_harbor_contract_is_not_validated_as_training_json(client, member_headers, make_zip, kind, prefix):
    path = _harbor_contract_zip(make_zip, prefix=prefix)
    response = _upload(client, member_headers, path, name="capacity-audit", kind=kind)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "harbor_tasks"
    assert body["status"] == "ready"
    assert body["stats"]["n_tasks"] == 1
    assert body["stats"]["n_records"] == 0
    assert body["lf_dataset_names"] == []
    assert kind in body["stats"]["warnings"][0]
    preview = client.get(f"/api/datasets/{body['id']}/preview", headers=member_headers).json()
    assert preview["kind"] == "harbor_tasks"
    assert preview["tasks"][0]["instruction"].startswith("# 成交容量审计")


def test_harbor_suite_with_default_format_keeps_task_root(client, member_headers, harbor_zip):
    from pathlib import Path

    response = _upload(client, member_headers, harbor_zip, name="auto-suite", kind="sft_alpaca")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "harbor_tasks"
    assert body["stats"]["n_tasks"] == 3
    assert len(list(Path(body["root_path"]).glob("*/task.toml"))) == 3


def test_harbor_auto_detection_keeps_explicit_rewardkit(client, member_headers, make_zip):
    response = _upload(
        client, member_headers, _harbor_contract_zip(make_zip),
        name="auto-rk", kind="sft_alpaca", rewardkit="true",
    )
    assert response.status_code == 201, response.text
    assert response.json()["rewardkit"] is True


def test_raw_harbor_upload_stays_raw(client, member_headers, make_zip):
    response = _upload(client, member_headers, _harbor_contract_zip(make_zip), name="raw-suite", kind="raw")
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "raw"


def test_unrelated_task_config_does_not_change_training_format(client, member_headers, make_zip):
    path = make_zip({
        "train.json": json.dumps([{"instruction": "a", "output": "b"}]),
        "config/task.toml": 'version = "1.0"\n',
    })
    response = _upload(client, member_headers, path, name="training-config", kind="sft_alpaca")
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "sft_alpaca"


@pytest.mark.parametrize("missing", ["instruction.md", "environment/Dockerfile"])
def test_detected_harbor_still_requires_task_structure(client, member_headers, make_zip, missing):
    files = {
        "qa001/task.toml": 'version = "1.0"\n',
        "qa001/instruction.md": "# 审计\n",
        "qa001/environment/Dockerfile": "FROM ubuntu:24.04\n",
        "qa001/environment/input_files/audit_contract.json": "{}",
    }
    del files[f"qa001/{missing}"]
    if missing.startswith("environment/"):
        del files["qa001/environment/input_files/audit_contract.json"]
    response = _upload(client, member_headers, make_zip(files), name="incomplete-suite", kind="sft_alpaca")
    assert response.status_code == 400
    assert ("environment/" if missing.startswith("environment/") else missing) in response.json()["detail"]


def test_replace_file_auto_detects_harbor_and_rolls_back(client, member_headers, make_zip):
    from pathlib import Path

    training = make_zip({"train.json": json.dumps([{"instruction": "a", "output": "b"}])})
    created = _upload(client, member_headers, training, name="replace-suite", kind="sft_alpaca").json()
    dataset_id = created["id"]
    harbor = _harbor_contract_zip(make_zip)

    def replace(path):
        with path.open("rb") as handle:
            return client.put(
                f"/api/datasets/{dataset_id}/file", headers=member_headers,
                files={"file": (path.name, handle, "application/zip")},
            )

    response = replace(harbor)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == dataset_id
    assert body["kind"] == "harbor_tasks"
    assert body["lf_dataset_names"] == []
    assert not (Path(body["root_path"]) / "dataset_info.json").exists()
    invalid = make_zip({"qa001/task.toml": 'version = "1.0"\n'}, "invalid-suite.zip")
    assert replace(invalid).status_code == 400
    after = client.get(f"/api/datasets/{dataset_id}", headers=member_headers).json()
    assert after["kind"] == "harbor_tasks"
    assert after["status"] == "ready"
    assert after["stats"]["n_tasks"] == 1
    assert len(list(Path(after["root_path"]).rglob("task.toml"))) == 1


def test_upload_harbor_rewardkit_flag(client, member_headers, harbor_zip):
    # 默认关（老客户端不带这个字段也要能上传）
    plain = _upload(client, member_headers, harbor_zip, name="plain", kind="harbor_tasks")
    assert plain.status_code == 201, plain.text
    assert plain.json()["rewardkit"] is False

    flagged = _upload(
        client, member_headers, harbor_zip, name="rk", kind="harbor_tasks", rewardkit="true"
    )
    assert flagged.status_code == 201, flagged.text
    assert flagged.json()["rewardkit"] is True

    # PATCH 往返：能把已上传的数据集改成 reward-kit，也能改回去
    dataset_id = flagged.json()["id"]
    off = client.patch(
        f"/api/datasets/{dataset_id}", headers=member_headers, json={"rewardkit": False}
    )
    assert off.status_code == 200, off.text
    assert off.json()["rewardkit"] is False


def test_alpaca_cannot_be_rewardkit(client, member_headers, alpaca_zip):
    # reward-kit 只对 harbor_tasks 有意义，别的类型后端直接落 False
    response = _upload(
        client, member_headers, alpaca_zip, name="alpaca-rk", kind="sft_alpaca", rewardkit="true"
    )
    assert response.status_code == 201, response.text
    assert response.json()["rewardkit"] is False


def test_alpaca_missing_fields_rejected(client, member_headers, make_zip):
    bad = make_zip({"train.json": json.dumps([{"foo": "bar"}])}, "bad.zip")
    response = _upload(client, member_headers, bad, name="bad", kind="sft_alpaca")
    assert response.status_code == 400
    assert "缺少必需字段" in response.json()["detail"]


def test_harbor_missing_instruction_rejected(client, member_headers, make_zip):
    bad = make_zip({"tasks/t1/task.toml": 'version = "1.0"\n'}, "bad-harbor.zip")
    response = _upload(client, member_headers, bad, name="bad-harbor", kind="harbor_tasks")
    assert response.status_code == 400
    assert "instruction.md" in response.json()["detail"]


def test_zip_slip_rejected(client, member_headers, tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escaped.json", "[]")
    response = _upload(client, member_headers, evil, name="evil", kind="raw")
    assert response.status_code == 400
    assert "非法路径" in response.json()["detail"]


def test_dpo_dataset_generates_ranking_entry(client, member_headers, make_zip):
    records = [
        {"instruction": "写快排", "chosen": "def quicksort", "rejected": "自己搜"} for _ in range(4)
    ]
    path = make_zip({"train.json": json.dumps(records, ensure_ascii=False)}, "dpo.zip")
    response = _upload(client, member_headers, path, name="dpo-demo", kind="dpo_pairs")
    assert response.status_code == 201, response.text
    from backend.app.services import dataset_service

    dataset = dataset_service.get_dataset(response.json()["id"])
    entries = dataset_service.lf_entries(dataset)
    entry = next(iter(entries.values()))
    assert entry["ranking"] is True
    assert entry["columns"]["chosen"] == "chosen"


def test_upload_rejects_non_archive(client, member_headers):
    response = client.post(
        "/api/datasets",
        headers=member_headers,
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        data={"name": "plain", "kind": "raw"},
    )
    assert response.status_code == 400


def test_list_and_delete(client, member_headers, make_zip):
    path = make_zip({"train.json": json.dumps([{"instruction": "a", "output": "b"}])}, "tiny.zip")
    created = _upload(client, member_headers, path, name="tiny", kind="sft_alpaca").json()
    listed = client.get("/api/datasets?mine_only=true", headers=member_headers).json()
    assert any(d["id"] == created["id"] for d in listed)
    assert client.delete(f"/api/datasets/{created['id']}", headers=member_headers).status_code == 204
    assert client.get(f"/api/datasets/{created['id']}", headers=member_headers).status_code == 404


def test_replace_file_keeps_id_and_rolls_back_on_invalid(client, member_headers, make_zip):
    one = make_zip({"train.json": json.dumps([{"instruction": "a", "output": "b"}])}, "one.zip")
    created = _upload(client, member_headers, one, name="swap", kind="sft_alpaca").json()
    assert created["stats"]["n_records"] == 1

    def _replace(path):
        with open(path, "rb") as handle:
            return client.put(
                f"/api/datasets/{created['id']}/file",
                headers=member_headers,
                files={"file": (path.name, handle, "application/zip")},
            )

    rows = [{"instruction": "a", "output": "b"}] * 3
    three = make_zip({"train.json": json.dumps(rows)}, "three.zip")
    replaced = _replace(three)
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["id"] == created["id"]
    assert body["stats"]["n_records"] == 3
    assert body["archive_name"] == "three.zip"

    bad = make_zip({"train.json": json.dumps([{"foo": "bar"}])}, "bad.zip")
    assert _replace(bad).status_code == 400
    after = client.get(f"/api/datasets/{created['id']}", headers=member_headers).json()
    assert after["status"] == "ready"
    assert after["stats"]["n_records"] == 3
    preview = client.get(f"/api/datasets/{created['id']}/preview", headers=member_headers)
    assert preview.status_code == 200


def test_prebuild_runs_docker_build_for_selected_task(client, member_headers, harbor_zip, monkeypatch):
    from backend.app.services import prebuild_service

    created = _upload(client, member_headers, harbor_zip, name="pb", kind="harbor_tasks").json()
    task = created["files"][0]["path"]
    seen: list[list[str]] = []
    real_command = prebuild_service.build_command

    def fake_command(task_dir, tag):
        seen.append(real_command(task_dir, tag))
        return ["true"]  # 测试机不一定能连 docker，只验证选中的命令

    monkeypatch.setattr(prebuild_service, "build_command", fake_command)

    started = client.post(f"/api/datasets/{created['id']}/prebuild", headers=member_headers, json={"task": task})
    assert started.status_code == 200, started.text
    assert seen[0][1] == "build" and seen[0][-1].endswith("/environment")

    import time

    for _ in range(50):
        state = client.get(f"/api/datasets/prebuilds/{started.json()['id']}", headers=member_headers).json()
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["status"] == "succeeded"

    bad = client.post(f"/api/datasets/{created['id']}/prebuild", headers=member_headers, json={"task": "../../etc"})
    assert bad.status_code == 400


def test_dataset_project_upload_filter_and_replace(client, member_headers, alpaca_zip):
    project = client.post("/api/projects", headers=member_headers, json={"name": "数据归属上传"}).json()
    other = client.post("/api/projects", headers=member_headers, json={"name": "数据归属过滤"}).json()
    response = _upload(
        client, member_headers, alpaca_zip, name="shared-name", kind="sft_alpaca", project_id=project["id"],
    )
    assert response.status_code == 201, response.text
    dataset = response.json()
    assert dataset["project_id"] == project["id"]
    assert dataset["name"] == "shared-name"  # 前缀由项目名动态展示，避免改名后残留旧前缀
    scoped = client.get("/api/datasets", headers=member_headers, params={"project_id": project["id"]}).json()
    assert [row["id"] for row in scoped] == [dataset["id"]]
    assert client.get("/api/datasets", headers=member_headers, params={"project_id": other["id"]}).json() == []
    with alpaca_zip.open("rb") as handle:
        replaced = client.put(
            f"/api/datasets/{dataset['id']}/file", headers=member_headers,
            files={"file": (alpaca_zip.name, handle, "application/zip")},
        )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["project_id"] == project["id"]


def test_dataset_rejects_missing_or_archived_project(client, member_headers, alpaca_zip):
    missing = _upload(client, member_headers, alpaca_zip, name="missing-project", kind="sft_alpaca", project_id="prj_missing")
    assert missing.status_code == 404
    project = client.post("/api/projects", headers=member_headers, json={"name": "数据归属归档"}).json()
    assert client.patch(f"/api/projects/{project['id']}", headers=member_headers, json={"archived": True}).status_code == 200
    archived = _upload(client, member_headers, alpaca_zip, name="archived-project", kind="sft_alpaca", project_id=project["id"])
    assert archived.status_code == 400
    assert "已归档" in archived.json()["detail"]


def test_dataset_project_transfer_permissions_and_deletion(client, admin_headers, member_headers, alpaca_zip):
    source = client.post("/api/projects", headers=admin_headers, json={"name": "数据归属转移前"}).json()
    target = client.post("/api/projects", headers=admin_headers, json={"name": "数据归属转移后"}).json()
    created = _upload(
        client, admin_headers, alpaca_zip, name="transfer", kind="sft_alpaca", project_id=source["id"],
    ).json()
    url = f"/api/datasets/{created['id']}"
    assert client.patch(url, headers=member_headers, json={"project_id": target["id"]}).status_code == 403
    assert client.patch(url, headers=admin_headers, json={"project_id": ""}).status_code == 400
    assert client.patch(url, headers=admin_headers, json={"project_id": "prj_missing"}).status_code == 404
    moved = client.patch(url, headers=admin_headers, json={"project_id": target["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["project_id"] == target["id"]
    assert client.delete(f"/api/projects/{source['id']}", headers=admin_headers).status_code == 204
    blocked = client.delete(f"/api/projects/{target['id']}", headers=admin_headers)
    assert blocked.status_code == 409
    assert "数据集" in blocked.json()["detail"]
    assert client.delete(url, headers=admin_headers).status_code == 204
    assert client.delete(f"/api/projects/{target['id']}", headers=admin_headers).status_code == 204


def test_legacy_datasets_adopt_default_project_once(client, member_headers, alpaca_zip, tmp_path, monkeypatch):
    from backend.app.models import Dataset
    from backend.app.services import dataset_service
    from backend.app.store import store
    from backend.app.store.json_store import JsonCollection

    monkeypatch.setattr(store, "datasets", JsonCollection(tmp_path / "legacy_datasets.json", Dataset))
    created = _upload(client, member_headers, alpaca_zip, name="legacy", kind="sft_alpaca").json()
    original = store.datasets.get(created["id"])
    old_project_id = original.project_id
    legacy = original.model_dump(mode="json")
    legacy.pop("project_id")
    store.datasets.put(type(original).model_validate(legacy))
    assert dataset_service.adopt_orphan_datasets() == 1
    assert store.datasets.get(original.id).project_id == old_project_id
    assert dataset_service.adopt_orphan_datasets() == 0


def test_training_and_verify_can_use_external_project_data(client, member_headers, alpaca_zip, harbor_zip):
    from .conftest import FAKE_MODEL_ID

    current = client.post("/api/projects", headers=member_headers, json={"name": "跨项目数据使用"}).json()
    external = client.post("/api/projects", headers=member_headers, json={"name": "跨项目外部数据"}).json()
    training = _upload(client, member_headers, alpaca_zip, name="external-train", kind="sft_alpaca", project_id=external["id"]).json()
    harbor = _upload(client, member_headers, harbor_zip, name="external-verify", kind="harbor_tasks", project_id=external["id"]).json()
    train_job = client.post("/api/jobs", headers=member_headers, json={
        "name": "external-training", "type": "sft", "project_id": current["id"], "resources": {"n_gpus": 0},
        "sft": {"stage": "sft", "model_id": FAKE_MODEL_ID, "model_path": "", "dataset_ids": [training["id"]]},
    })
    assert train_job.status_code == 201, train_job.text
    verify_job = client.post("/api/jobs", headers=member_headers, json={
        "name": "external-evaluation", "type": "verify", "project_id": current["id"], "resources": {"n_gpus": 0},
        "verify": {"agent": "oracle", "dataset_id": harbor["id"]},
    })
    assert verify_job.status_code == 201, verify_job.text
