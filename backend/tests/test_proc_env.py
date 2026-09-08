"""子进程 PATH 修正。

回归的是一个真实故障：平台跑在自己的 conda 环境里，子进程继承的是**平台的** PATH，
于是 vLLM 找不到装在自己环境里的 ninja（`FileNotFoundError: 'ninja'`），
多卡 LLaMA-Factory 找不到 torchrun，verl 找不到 ray。
"""

from __future__ import annotations

import os

from backend.app.services.proc_env import with_interpreter_on_path


def test_interpreter_bin_dir_goes_first(tmp_path):
    bin_dir = tmp_path / "envs" / "verl" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("", encoding="utf-8")

    env = {"PATH": f"/platform/bin{os.pathsep}/usr/bin"}
    with_interpreter_on_path(env, str(python))

    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(bin_dir), env["PATH"]
    # 原来的条目要保留：框架也可能依赖系统工具（git / docker / nvidia-smi）
    assert "/platform/bin" in parts
    assert "/usr/bin" in parts


def test_no_duplicate_entries(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text("", encoding="utf-8")

    env = {"PATH": f"/usr/bin{os.pathsep}{bin_dir}"}
    with_interpreter_on_path(env, str(python))
    parts = env["PATH"].split(os.pathsep)
    assert parts.count(str(bin_dir)) == 1
    assert parts[0] == str(bin_dir)

    # 幂等：再调一次不该继续堆
    before = env["PATH"]
    with_interpreter_on_path(env, str(python))
    assert env["PATH"] == before


def test_bare_command_name_is_left_alone():
    env = {"PATH": "/usr/bin"}
    with_interpreter_on_path(env, "python")
    assert env["PATH"] == "/usr/bin"


def test_missing_path_key_is_tolerated(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text("", encoding="utf-8")

    env: dict[str, str] = {}
    with_interpreter_on_path(env, str(python))
    assert env["PATH"].startswith(str(bin_dir))


def test_launcher_puts_framework_bin_on_path(client, admin_headers, member_headers, monkeypatch, tmp_path):
    """端到端：子进程真的能看到框架环境里的可执行文件。"""
    import json
    import time
    import zipfile

    from backend.app.builders.base import BuildResult
    from backend.app.services import runner

    from .conftest import FAKE_MODEL_ID, project_id

    fake_env_bin = tmp_path / "fake-env" / "bin"
    fake_env_bin.mkdir(parents=True)
    marker = fake_env_bin / "ninja"
    marker.write_text("#!/bin/sh\necho fake-ninja\n", encoding="utf-8")
    marker.chmod(0o755)
    interpreter = fake_env_bin / "python"
    interpreter.symlink_to("/bin/sh")

    def fake_build(job, work_dir, output_dir):
        del job
        return BuildResult(
            # 用框架环境里的 "解释器" 启动，脚本里再去找 ninja ——
            # 找到就说明 PATH 已经被修正过
            command=[str(interpreter), "-c", "command -v ninja && echo FOUND_NINJA"],
            cwd=str(work_dir),
            output_dir=str(output_dir),
        )

    monkeypatch.setattr(runner, "build_job", fake_build)

    archive = tmp_path / "path.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("train.json", json.dumps([{"instruction": "q", "input": "", "output": "a"}]))
    with archive.open("rb") as handle:
        dataset_id = client.post(
            "/api/datasets",
            headers=member_headers,
            files={"file": ("path.zip", handle, "application/zip")},
            data={"name": "path-data", "kind": "sft_alpaca"},
        ).json()["id"]

    job = client.post(
        "/api/jobs",
        headers=member_headers,
        json={
            "name": "path-fix",
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
        if client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"] in {
            "succeeded",
            "failed",
        }:
            break
        time.sleep(0.3)

    log = client.get(f"/api/jobs/{job['id']}/log", headers=member_headers).json()["content"]
    assert "FOUND_NINJA" in log, log[-2000:]
