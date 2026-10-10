"""Dataset upload, validation and safety."""

from __future__ import annotations

import io
import json
import zipfile


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
