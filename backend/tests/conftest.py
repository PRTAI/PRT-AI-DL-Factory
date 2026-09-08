"""Test fixtures.

Environment variables are set *before* ``backend.app`` is imported because the
settings object is built once at import time.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="prt-test-"))
_DATA_ROOT = _TMP_ROOT / "data"
_MODELS_ROOT = _TMP_ROOT / "models"

os.environ["PRT_DATA_ROOT"] = str(_DATA_ROOT)
os.environ["PRT_MODELS_ROOT"] = str(_MODELS_ROOT)
os.environ["PRT_BOOTSTRAP_ADMIN_EMAIL"] = "admin@prt.local"
os.environ["PRT_BOOTSTRAP_ADMIN_PASSWORD"] = "prt-admin-2026"
os.environ["PRT_REQUIRE_APPROVAL"] = "true"
os.environ["PRT_SCHEDULER_INTERVAL_SECONDS"] = "0.5"
os.environ["PRT_MONITOR_INTERVAL_SECONDS"] = "60"

# Fake model zoo entry so job validation has something to point at.
_FAKE_MODEL = _MODELS_ROOT / "Qwen" / "Qwen3-4B-Instruct-2507"
_FAKE_MODEL.mkdir(parents=True, exist_ok=True)
(_FAKE_MODEL / "config.json").write_text(
    json.dumps(
        {
            "architectures": ["Qwen3ForCausalLM"],
            "model_type": "qwen3",
            "hidden_size": 2560,
            "num_hidden_layers": 36,
            "max_position_embeddings": 262144,
            "torch_dtype": "bfloat16",
        }
    ),
    encoding="utf-8",
)
(_FAKE_MODEL / "tokenizer_config.json").write_text("{}", encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def admin_headers(client) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"email": "admin@prt.local", "password": "prt-admin-2026"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="session")
def member_headers(client, admin_headers) -> dict[str, str]:
    client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": "dev@prt.local",
            "display_name": "普通成员",
            "password": "dev-password-1",
            "role": "user",
            "must_change_password": False,
        },
    )
    response = client.post(
        "/api/auth/login", json={"email": "dev@prt.local", "password": "dev-password-1"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #
_PROJECT: dict[str, str] = {}


@pytest.fixture(scope="session", autouse=True)
def default_project(client, member_headers) -> str:
    """任务必须归属项目，所以整个测试会话共用一个。"""
    response = client.post(
        "/api/projects", headers=member_headers, json={"name": "pytest 项目", "tags": ["test"]}
    )
    assert response.status_code == 201, response.text
    _PROJECT["id"] = response.json()["id"]
    return _PROJECT["id"]


def project_id() -> str:
    """给构造 payload 的模块级 helper 用（fixture 已经 autouse 跑过了）。"""
    return _PROJECT["id"]


# --------------------------------------------------------------------------- #
# Archive helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_zip(tmp_path):
    def _make(files: dict[str, str], name: str = "dataset.zip") -> Path:
        target = tmp_path / name
        with zipfile.ZipFile(target, "w") as zf:
            for rel, content in files.items():
                zf.writestr(rel, content)
        return target

    return _make


@pytest.fixture(scope="session")
def dataset_id_for_projects(client, member_headers, tmp_path_factory) -> str:
    """一个就绪的 alpaca 数据集，供不关心数据集本身的测试复用。"""
    tmp = tmp_path_factory.mktemp("shared-ds")
    archive = tmp / "shared.zip"
    records = [{"instruction": f"q{i}", "input": "", "output": f"a{i}"} for i in range(4)]
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("train.json", json.dumps(records))
    with archive.open("rb") as handle:
        response = client.post(
            "/api/datasets",
            headers=member_headers,
            files={"file": ("shared.zip", handle, "application/zip")},
            data={"name": "shared-data", "kind": "sft_alpaca"},
        )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def alpaca_zip(make_zip):
    records = [
        {"instruction": f"问题 {i}", "input": "", "output": f"回答 {i}"} for i in range(12)
    ]
    return make_zip({"train.json": json.dumps(records, ensure_ascii=False)}, "alpaca.zip")


@pytest.fixture
def harbor_zip(make_zip):
    task_toml = (
        'version = "1.0"\n\n[metadata]\ndifficulty = "easy"\n\n'
        "[agent]\ntimeout_sec = 60.0\n\n[verifier]\ntimeout_sec = 60.0\n"
    )
    files = {}
    for index in range(3):
        base = f"tasks/demo-task-{index}"
        files[f"{base}/task.toml"] = task_toml
        files[f"{base}/instruction.md"] = f"# 任务 {index}\n\n请输出 hello。\n"
        files[f"{base}/environment/Dockerfile"] = "FROM ubuntu:24.04\n"
        files[f"{base}/tests/test.sh"] = "#!/bin/bash\necho 1.0 > /logs/verifier/reward.txt\n"
        files[f"{base}/solution/solve.sh"] = "#!/bin/bash\necho hello\n"
    return make_zip(files, "harbor.zip")


MODELS_ROOT = _MODELS_ROOT
DATA_ROOT = _DATA_ROOT
FAKE_MODEL_ID = "Qwen/Qwen3-4B-Instruct-2507"
