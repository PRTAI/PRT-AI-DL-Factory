"""Dataset upload, validation and safety."""

from __future__ import annotations

import io
import json
import zipfile


def _upload(client, headers, path, *, name, kind):
    with open(path, "rb") as handle:
        return client.post(
            "/api/datasets",
            headers=headers,
            files={"file": (path.name, handle, "application/zip")},
            data={"name": name, "kind": kind, "description": "pytest", "tags": "test"},
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
