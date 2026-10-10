"""Task verifier variable discovery, defaults and credential redaction."""

import json

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
    assert [field["key"] for field in response.json()["variables"]] == ["CUSTOM_API_KEY", "EVAL_API_KEY"]
    path_response = client.get(endpoint, headers=member_headers, params={"tasks_path": uploaded.json()["root_path"]})
    assert path_response.json() == response.json()
    assert client.get(endpoint, headers=member_headers, params={**query, "tasks_path": "/tmp"}).status_code == 400
    assert client.get(endpoint, headers=member_headers, params={"dataset_id": "missing"}).status_code == 404
