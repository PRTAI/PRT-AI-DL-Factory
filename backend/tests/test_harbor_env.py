"""Task verifier variable discovery, defaults and credential redaction."""

import json
import subprocess

import pytest

from backend.app.services import harbor_env_service


def test_verifier_variables_merge_tasks_and_hide_embedded_secrets(tmp_path):
    for index in range(2):
        task = tmp_path / f"医疗-{index}"
        task.mkdir()
        (task / "task.toml").write_text(
            '# ' + 'padding' * 400 + '\n[verifier.env]\n'
            'EVAL_API_KEY = "${EVAL_API_KEY:-}"\n'
            'SERVICE_TOKEN = "${SERVICE_TOKEN}"\n'
            'HIDDEN_SECRET = "sk-embedded-secret"\n'
            'ALIAS = "${GLOBAL_SECRET:-sk-default-secret}"\n'
            f'TIMEOUT = "${{TIMEOUT:-{30 + index}}}"\n'
            'LITELLM_DROP_PARAMS = "true"\n', encoding="utf-8",
        )
    detected = harbor_env_service.inspect_root(tmp_path)
    fields = {field["key"]: field for field in detected["variables"]}
    assert detected["n_tasks"] == 2 and detected["warnings"] == []
    assert fields["EVAL_API_KEY"]["required"] is False and fields["EVAL_API_KEY"]["default"] == ""
    assert fields["SERVICE_TOKEN"]["required"] is True
    assert fields["SERVICE_TOKEN"]["source_variables"] == ["SERVICE_TOKEN"]
    assert fields["TIMEOUT"]["conflicting_defaults"] is True and fields["TIMEOUT"]["default"] is None
    assert fields["LITELLM_DROP_PARAMS"]["default"] == "true"
    assert fields["HIDDEN_SECRET"]["has_task_value"] is True and fields["HIDDEN_SECRET"]["default"] is None
    assert fields["ALIAS"]["secret"] is True and fields["ALIAS"]["source_variable"] == "GLOBAL_SECRET"
    assert "sk-embedded-secret" not in json.dumps(detected)
    assert "sk-default-secret" not in json.dumps(detected)


def test_verifier_detection_skips_broken_files_and_symlink_escape(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_text('[verifier.env]\nOUTSIDE = "secret"\n')
    (root / "task.toml").symlink_to(outside)
    broken = root / "broken"
    broken.mkdir()
    (broken / "task.toml").write_text("[broken")
    detected = harbor_env_service.inspect_root(root)
    assert detected["variables"] == [] and detected["n_tasks"] == 0
    assert len(detected["warnings"]) == 1


def test_verifier_environment_catalog_api(client, member_headers, make_zip):
    files = {}
    for name, env in [("医疗001", 'EVAL_API_KEY = "${EVAL_API_KEY:-}"'),
                      ("医疗002", 'CUSTOM_API_KEY = "${CUSTOM_API_KEY}"')]:
        files.update({
            f"suite/{name}/task.toml": f'[verifier.env]\n{env}\n',
            f"suite/{name}/instruction.md": "任务",
            f"suite/{name}/environment/Dockerfile": "FROM ubuntu:24.04",
        })
    zipped = make_zip(files)
    with zipped.open("rb") as stream:
        uploaded = client.post("/api/datasets", headers=member_headers,
                               files={"file": (zipped.name, stream, "application/zip")},
                               data={"name": "verifier-env-suite", "kind": "harbor_tasks"})
    assert uploaded.status_code == 201, uploaded.text
    endpoint = "/api/catalog/harbor-verifier-env"
    query = {"dataset_id": uploaded.json()["id"]}
    assert client.get(endpoint, params=query).status_code == 401
    response = client.get(endpoint, headers=member_headers, params=query)
    assert response.status_code == 200, response.text
    assert response.json()["n_tasks"] == 2
    assert len(response.json()["task_timeouts"]) == 2
    assert "available" in response.json()["timeout_defaults"]
    assert [field["key"] for field in response.json()["variables"]] == ["CUSTOM_API_KEY", "EVAL_API_KEY"]
    path_response = client.get(endpoint, headers=member_headers, params={"tasks_path": uploaded.json()["root_path"]})
    assert path_response.json() == response.json()
    assert client.get(endpoint, headers=member_headers, params={**query, "tasks_path": "/tmp"}).status_code == 400
    assert client.get(endpoint, headers=member_headers, params={"dataset_id": "missing"}).status_code == 404


def test_task_timeouts_preserve_per_task_values_and_missing_fields(tmp_path):
    for name, text in {
        "医疗001": "[agent]\ntimeout_sec=7200\n[verifier]\ntimeout_sec=1800.5\n",
        "医疗002": "[agent]\ntimeout_sec=120\n",
    }.items():
        task = tmp_path / name
        task.mkdir()
        (task / "task.toml").write_text(text)
    detected = harbor_env_service.inspect_root(tmp_path)
    first, second = detected["task_timeouts"]
    assert first["agent"] == {"seconds": 7200, "source": "task"}
    assert first["verifier"] == {"seconds": 1800.5, "source": "task"}
    assert second["agent"] == {"seconds": 120, "source": "task"}
    assert second["verifier"] == {"seconds": None, "source": "harbor_default"}
    assert detected["warnings"] == []


def test_step_timeouts_match_harbor_default_and_inheritance(tmp_path):
    (tmp_path / "task.toml").write_text(
        '[agent]\ntimeout_sec=7200\n[verifier]\ntimeout_sec=3600\n'
        '[[steps]]\nname="first"\n[steps.agent]\ntimeout_sec=900\n'
        '[steps.verifier]\ntimeout_sec=240\n[[steps]]\nname="second"\n'
    )
    defaults = {"available": True, "steps": {"agent": None, "verifier": 600}}
    rows = harbor_env_service.inspect_root(tmp_path, defaults=defaults)["task_timeouts"]
    assert rows[0]["agent"] == {"seconds": 900, "source": "step"}
    assert rows[0]["verifier"] == {"seconds": 240, "source": "step"}
    assert rows[1]["agent"] == {"seconds": 7200, "source": "task"}
    assert rows[1]["verifier"] == {"seconds": 600, "source": "step_default"}
    unknown = harbor_env_service.inspect_root(tmp_path)["task_timeouts"][1]
    assert unknown["agent"]["source"] == "unavailable"
    assert unknown["verifier"]["source"] == "unavailable"


@pytest.mark.parametrize("value", ["-1", "0", "true", '"3600"', "nan", "inf"])
def test_invalid_timeout_is_reported_without_inventing_effective_value(tmp_path, value):
    (tmp_path / "task.toml").write_text(f"[agent]\ntimeout_sec={value}\n")
    detected = harbor_env_service.inspect_root(tmp_path)
    assert detected["task_timeouts"][0]["agent"] == {"seconds": None, "source": "invalid"}
    assert "agent.timeout_sec" in detected["warnings"][0]


def test_installed_timeout_defaults_probe_is_bounded_and_fails_closed(monkeypatch):
    def run(argv, **kwargs):
        assert argv[:2] == ["/test/harbor/python", "-c"]
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(argv, 10)
    monkeypatch.setattr(harbor_env_service.subprocess, "run", run)
    assert harbor_env_service._timeout_defaults.__wrapped__("/test/harbor/python") == {"available": False}
