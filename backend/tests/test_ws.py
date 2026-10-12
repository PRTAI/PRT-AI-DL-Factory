"""WebSocket streams: system telemetry and live job logs."""

from __future__ import annotations

import json
import time
import zipfile

import pytest

from .conftest import FAKE_MODEL_ID, project_id


def _token(client, email: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_system_stream_requires_a_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/system") as ws:
            ws.receive_json()


def test_system_stream_sends_history(client, admin_headers):
    token = admin_headers["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(f"/ws/system?token={token}") as ws:
        payload = ws.receive_json()
    assert payload["type"] == "history"
    assert "points" in payload
    assert payload["gpu"]["total"] >= 0


@pytest.fixture
def running_job(client, admin_headers, member_headers, tmp_path, monkeypatch):
    """A job whose stubbed command prints a marker and then exits."""
    from backend.app.builders.base import BuildResult
    from backend.app.services import runner

    def fake_build(job, work_dir, output_dir):
        del job
        return BuildResult(
            command=["bash", "-c", "echo WS-MARKER-LINE; sleep 1; echo done"],
            cwd=str(work_dir),
            output_dir=str(output_dir),
        )

    monkeypatch.setattr(runner, "build_job", fake_build)

    records = [{"instruction": "q", "input": "", "output": "a"}]
    archive = tmp_path / "ws.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("train.json", json.dumps(records))
    with archive.open("rb") as handle:
        dataset_id = client.post(
            "/api/datasets",
            headers=member_headers,
            files={"file": ("ws.zip", handle, "application/zip")},
            data={"name": "ws-data", "kind": "sft_alpaca"},
        ).json()["id"]

    job = client.post(
        "/api/jobs",
        headers=member_headers,
        json={
            "name": "ws-stream-job",
            "type": "sft",
            "project_id": project_id(),
            "resources": {"n_gpus": 0},
            "sft": {
                "stage": "sft",
                "model_id": FAKE_MODEL_ID,
                "model_path": "",
                "dataset_ids": [dataset_id],
            },
        },
    ).json()
    client.post(f"/api/jobs/{job['id']}/review", headers=admin_headers, json={"approve": True})

    deadline = time.time() + 30
    while time.time() < deadline:
        status = client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"]
        if status in {"running", "succeeded", "failed"}:
            break
        time.sleep(0.3)
    return job


def test_job_log_stream(client, member_headers, running_job):
    token = member_headers["Authorization"].split(" ", 1)[1]
    log_text = ""
    saw_status = False
    with client.websocket_connect(f"/ws/jobs/{running_job['id']}/log?token={token}") as ws:
        for _ in range(12):
            message = ws.receive_json()
            if message["type"] == "log":
                log_text += message["content"]
            elif message["type"] == "status":
                saw_status = True
            elif message["type"] == "end":
                break
    assert "WS-MARKER-LINE" in log_text
    assert saw_status


def test_job_log_stream_allows_shared_job_viewers(client, admin_headers, running_job):

    created = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": "wsoutsider@prt.local",
            "password": "outsider-pass-9",
            "role": "user",
            "must_change_password": False,
        },
    )
    assert created.status_code in (201, 409)
    token = _token(client, "wsoutsider@prt.local", "outsider-pass-9")
    with client.websocket_connect(f"/ws/jobs/{running_job['id']}/log?token={token}") as ws:
        messages = []
        for _ in range(12):
            message = ws.receive_json()
            messages.append(message)
            if message['type'] == 'end':
                break
    assert any(message['type'] == 'status' for message in messages)
    assert 'WS-MARKER-LINE' in ''.join(message.get('content', '') for message in messages)
