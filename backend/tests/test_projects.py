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


def test_combined_filters_are_an_intersection(
    client, admin_headers, member_headers, dataset_id_for_projects
):
    """关键词 / 负责人 / 类型三个条件是交集，且关键词也匹配项目里的任务。

    「找张三上周那个评测」不该要求先猜对项目名。
    """
    project = client.post(
        "/api/projects", headers=member_headers, json={"name": "联合筛选项目"}
    ).json()

    def submit(name: str, headers: dict[str, str], job_type: str = "sft") -> str:
        if job_type == "sft":
            payload = {
                "name": name,
                "type": "sft",
                "project_id": project["id"],
                "resources": {"n_gpus": 0},
                "sft": {
                    "stage": "sft",
                    "model_id": FAKE_MODEL_ID,
                    "model_path": "",
                    "dataset_ids": [dataset_id_for_projects],
                },
            }
        else:
            payload = {
                "name": name,
                "type": "verify",
                "project_id": project["id"],
                "resources": {"n_gpus": 0},
                "verify": {"agent": "oracle", "tasks_path": "/tmp/tasks"},
            }
        response = client.post("/api/jobs", headers=headers, json=payload)
        assert response.status_code == 201, response.text
        return response.json()["id"]

    dev_train = submit("dev-的-训练-alpha", member_headers)
    dev_verify = submit("dev-的-评测-alpha", member_headers, "verify")
    admin_verify = submit("admin-的-评测-alpha", admin_headers, "verify")

    def ids(**params) -> set[str]:
        rows = client.get("/api/projects", headers=member_headers, params=params).json()
        row = next((r for r in rows if r["project"]["id"] == project["id"]), None)
        return {m["id"] for m in row["matches"]} if row else set()

    def present(**params) -> bool:
        rows = client.get("/api/projects", headers=member_headers, params=params).json()
        return any(r["project"]["id"] == project["id"] for r in rows)

    # 关键词命中任务名 —— 项目名里根本没有 "alpha"
    assert ids(q="alpha") == {dev_train, dev_verify, admin_verify}

    # 关键词 + 类型
    assert ids(q="alpha", job_type="verify") == {dev_verify, admin_verify}

    # 负责人（拿 members 接口里的 id，普通成员也能调）
    members = client.get("/api/projects/members", headers=member_headers).json()
    admin_id = next(m["id"] for m in members if m["email"] == "admin@prt.local")
    dev_id = next(m["id"] for m in members if m["email"] == "dev@prt.local")

    assert ids(owner_id=admin_id) == {admin_verify}

    # 三个条件同时给 = 交集，不是并集
    assert ids(q="alpha", owner_id=dev_id, job_type="verify") == {dev_verify}
    assert ids(q="alpha", owner_id=admin_id, job_type="sft") == set()
    assert not present(q="alpha", owner_id=admin_id, job_type="sft")

    # 关键词只匹配项目名时，项目仍然出现（哪怕没有任务命中）
    assert present(q="联合筛选")

    # 统计里分开算训练与评测
    rows = client.get("/api/projects", headers=member_headers).json()
    stats = next(r["stats"] for r in rows if r["project"]["id"] == project["id"])
    assert stats["training_jobs"] == 1
    assert stats["verify_jobs"] == 2
    assert sorted(stats["members"]) == ["admin@prt.local", "dev@prt.local"]


def test_jobs_can_be_filtered_by_owner(client, admin_headers, member_headers, dataset_id_for_projects):
    """任务列表的负责人筛选：默认看全员，指定后只看那个人的。"""
    members = client.get("/api/projects/members", headers=member_headers).json()
    admin_id = next(m["id"] for m in members if m["email"] == "admin@prt.local")

    payload = {
        "name": "admin-的任务",
        "type": "sft",
        "project_id": project_id(),
        "resources": {"n_gpus": 0},
        "sft": {
            "stage": "sft",
            "model_id": FAKE_MODEL_ID,
            "model_path": "",
            "dataset_ids": [dataset_id_for_projects],
        },
    }
    mine = client.post("/api/jobs", headers=admin_headers, json=payload).json()["id"]

    scoped = client.get(f"/api/jobs?owner_id={admin_id}", headers=member_headers).json()
    assert mine in [row["id"] for row in scoped["items"]]
    assert all(row["owner_email"] == "admin@prt.local" for row in scoped["items"])

    # 别人也能看到（团队共享），只是默认不过滤
    everyone = client.get("/api/jobs", headers=member_headers).json()
    owners = {row["owner_email"] for row in everyone["items"]}
    assert len(owners) > 1, owners


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
