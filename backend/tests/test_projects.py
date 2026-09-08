"""项目：创建、共享可见性、统计、归档、删除保护。"""

from __future__ import annotations

import json

from .conftest import FAKE_MODEL_ID, project_id


def test_project_list_is_shared_across_the_team(client, admin_headers, member_headers):
    created = client.post(
        "/api/projects",
        headers=member_headers,
        json={"name": "共享可见性验证", "description": "dev 建的", "tags": ["grpo"]},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["slug"]
    assert body["owner_email"] == "dev@prt.local"

    # 别人（管理员，以及任意成员）都能在列表里看到
    for headers in (admin_headers, member_headers):
        names = [row["project"]["name"] for row in client.get("/api/projects", headers=headers).json()]
        assert "共享可见性验证" in names

    hit = client.get("/api/projects?q=grpo", headers=admin_headers).json()
    assert [row["project"]["id"] for row in hit] == [body["id"]]
    assert client.get("/api/projects?q=不存在的关键词", headers=admin_headers).json() == []


def test_duplicate_project_name_rejected(client, member_headers):
    payload = {"name": "重名项目"}
    assert client.post("/api/projects", headers=member_headers, json=payload).status_code == 201
    dup = client.post("/api/projects", headers=member_headers, json=payload)
    assert dup.status_code == 409
    assert "已存在同名项目" in dup.json()["detail"]


def test_only_owner_or_admin_can_edit(client, admin_headers, member_headers):
    project = client.post("/api/projects", headers=member_headers, json={"name": "权限项目"}).json()

    client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": "stranger@prt.local",
            "password": "stranger-pass-1",
            "role": "user",
            "must_change_password": False,
        },
    )
    token = client.post(
        "/api/auth/login", json={"email": "stranger@prt.local", "password": "stranger-pass-1"}
    ).json()["access_token"]
    stranger = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/api/projects/{project['id']}", headers=stranger).status_code == 200
    assert (
        client.patch(
            f"/api/projects/{project['id']}", headers=stranger, json={"name": "抢名字"}
        ).status_code
        == 403
    )
    # 负责人可以改，管理员也可以
    assert (
        client.patch(
            f"/api/projects/{project['id']}", headers=member_headers, json={"description": "改过了"}
        ).status_code
        == 200
    )
    renamed = client.patch(
        f"/api/projects/{project['id']}", headers=admin_headers, json={"name": "管理员改名"}
    ).json()
    assert renamed["name"] == "管理员改名"
    # 中文项目名也要有区分度的 slug，不能都退化成 "project"
    assert renamed["slug"] == "管理员改名"


def test_archived_project_rejects_new_jobs(client, member_headers, admin_headers, make_zip):
    project = client.post("/api/projects", headers=member_headers, json={"name": "归档项目"}).json()
    records = [{"instruction": "q", "input": "", "output": "a"}]
    path = make_zip({"train.json": json.dumps(records)}, "arch.zip")
    with open(path, "rb") as handle:
        dataset_id = client.post(
            "/api/datasets",
            headers=member_headers,
            files={"file": ("arch.zip", handle, "application/zip")},
            data={"name": "arch-data", "kind": "sft_alpaca"},
        ).json()["id"]

    payload = {
        "name": "归档后提交",
        "type": "sft",
        "project_id": project["id"],
        "resources": {"n_gpus": 0},
        "sft": {"stage": "sft", "model_id": FAKE_MODEL_ID, "model_path": "", "dataset_ids": [dataset_id]},
    }
    assert client.post("/api/jobs", headers=member_headers, json=payload).status_code == 201

    client.patch(f"/api/projects/{project['id']}", headers=member_headers, json={"archived": True})
    blocked = client.post("/api/jobs", headers=member_headers, json=payload)
    assert blocked.status_code == 400
    assert "已归档" in blocked.json()["detail"]

    # 归档的项目默认不在列表里，显式要求才出现
    ids = [row["project"]["id"] for row in client.get("/api/projects", headers=admin_headers).json()]
    assert project["id"] not in ids
    ids = [
        row["project"]["id"]
        for row in client.get("/api/projects?include_archived=true", headers=admin_headers).json()
    ]
    assert project["id"] in ids


def test_project_with_jobs_cannot_be_deleted(client, member_headers, dataset_id_for_projects):
    project = client.post("/api/projects", headers=member_headers, json={"name": "有任务的项目"}).json()
    payload = {
        "name": "占位任务",
        "type": "sft",
        "project_id": project["id"],
        "resources": {"n_gpus": 0},
        "sft": {"stage": "sft", "model_id": FAKE_MODEL_ID, "model_path": "", "dataset_ids": [dataset_id_for_projects]},
    }
    job = client.post("/api/jobs", headers=member_headers, json=payload)
    assert job.status_code == 201, job.text

    blocked = client.delete(f"/api/projects/{project['id']}", headers=member_headers)
    assert blocked.status_code == 409
    assert "无法删除" in blocked.json()["detail"]

    stats = client.get(f"/api/projects/{project['id']}", headers=member_headers).json()["stats"]
    assert stats["total_jobs"] == 1
    assert stats["pending_review"] == 1
    assert stats["members"] == ["dev@prt.local"]

    listed = client.get(f"/api/projects/{project['id']}/jobs", headers=member_headers).json()
    assert [row["id"] for row in listed] == [job.json()["id"]]

    # 空项目可以删
    empty = client.post("/api/projects", headers=member_headers, json={"name": "空项目"}).json()
    assert client.delete(f"/api/projects/{empty['id']}", headers=member_headers).status_code == 204


def test_jobs_can_be_filtered_by_project(client, member_headers, dataset_id_for_projects):
    other = client.post("/api/projects", headers=member_headers, json={"name": "过滤项目"}).json()
    payload = {
        "name": "过滤任务",
        "type": "sft",
        "project_id": other["id"],
        "resources": {"n_gpus": 0},
        "sft": {"stage": "sft", "model_id": FAKE_MODEL_ID, "model_path": "", "dataset_ids": [dataset_id_for_projects]},
    }
    job_id = client.post("/api/jobs", headers=member_headers, json=payload).json()["id"]

    scoped = client.get(f"/api/jobs?project_id={other['id']}", headers=member_headers).json()
    assert [row["id"] for row in scoped["items"]] == [job_id]

    default_scoped = client.get(f"/api/jobs?project_id={project_id()}", headers=member_headers).json()
    assert job_id not in [row["id"] for row in default_scoped["items"]]

    overview = client.get(f"/api/jobs/overview?project_id={other['id']}", headers=member_headers).json()
    assert overview["total"] == 1
