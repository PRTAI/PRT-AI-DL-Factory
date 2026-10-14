from pathlib import Path
import tomllib
from unittest.mock import Mock

import pytest

from backend.app.services import harbor_image_service as images

DOCKERFILE = '''FROM python:3.12-slim
RUN useradd -m -u 1000 agent
RUN pip install pandas
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY input_files/ /app/input_files/
RUN chown -R root:root /app/input_files && chmod -R a-w /app/input_files
RUN mkdir -p /app/output && chown -R agent:agent /app/output
WORKDIR /app
'''


def task_root(tmp_path):
    root = tmp_path / 'dataset'
    env = root / '题目一' / 'environment'
    (env / 'input_files').mkdir(parents=True)
    (env / 'input_files' / 'data.json').write_text('{"sample": 1}')
    (env / 'requirements.txt').write_text('some-task-package==1.0\n')
    (env / 'Dockerfile').write_text(DOCKERFILE)
    (env.parent / 'task.toml').write_text('[environment]\ndocker_image="old-image"\ncpus=2\n[verifier.env]\nEVAL_API_KEY="${KEY}"\n')
    return root


def test_task_snapshot_preserves_inputs_and_initialization(tmp_path):
    root = task_root(tmp_path)
    target = images.prepare_tasks(root, tmp_path / 'job' / 'tasks', 'sha256:abc')
    env = target / '题目一' / 'environment'
    result = (env / 'Dockerfile').read_text()
    assert result.startswith('FROM sha256:abc\nCOPY requirements.txt')
    assert 'RUN pip install pandas' not in result
    assert result.splitlines()[1:] == DOCKERFILE.splitlines()[3:]
    assert (env / 'requirements.txt').read_text() == 'some-task-package==1.0\n'
    assert (env / 'input_files' / 'data.json').read_text() == '{"sample": 1}'
    config = tomllib.loads((env.parent / 'task.toml').read_text())
    assert config['environment'] == {'cpus': 2}
    assert config['verifier']['env']['EVAL_API_KEY'] == '${KEY}'
    assert (root / '题目一' / 'environment' / 'Dockerfile').read_text() == DOCKERFILE
    assert 'old-image' in (root / '题目一' / 'task.toml').read_text()
    # Existing snapshots retain their original image across previews/rebuilds.
    images.prepare_tasks(root, target, 'sha256:new')
    assert (env / 'Dockerfile').read_text() == result


@pytest.mark.parametrize('content', [
    'FROM one\nFROM two\nCOPY input_files/ /app/\n',
    'FROM one\nCOPY other/ /app/\n',
    'FROM one\nENV SPECIAL=1\nCOPY input_files/ /app/\n',
])
def test_unsupported_templates_fail_explicitly(content):
    with pytest.raises(ValueError):
        images.lightweight_dockerfile(content)


def test_compose_and_symlinks_rejected_without_partial_copy(tmp_path):
    root = task_root(tmp_path)
    compose = root / '题目一' / 'environment' / 'docker-compose.yaml'
    compose.touch()
    target = tmp_path / 'target'
    with pytest.raises(ValueError, match='Compose'):
        images.prepare_tasks(root, target, 'image')
    assert not target.exists()
    compose.unlink()
    (root / 'link').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='符号链接'):
        images.prepare_tasks(root, target, 'image')
    assert not target.exists()


def test_image_status_and_admin_only_build(client, admin_headers, member_headers, monkeypatch):
    monkeypatch.setattr(images.subprocess, 'run', lambda *a, **k: Mock(returncode=0, stdout='[{"Id":"sha256:123","Created":"today"}]'))
    response = client.get('/api/system/harbor-image', headers=member_headers)
    assert response.status_code == 200
    assert response.json()['ready']
    assert response.json()['image_id'] == 'sha256:123'
    assert client.post('/api/system/harbor-image/build', headers=member_headers).status_code == 403
    assert client.post('/api/system/harbor-image/build').status_code == 401
    assert client.get('/api/system/harbor-image/recipe', headers=admin_headers).json()['dockerfile'].startswith('FROM python:3.12-slim')
    monkeypatch.setattr(images, 'start', lambda: {'status': 'running', 'image': images.IMAGE})
    assert client.post('/api/system/harbor-image/build', headers=admin_headers).json()['status'] == 'running'


@pytest.mark.parametrize('batch', [False, True])
def test_builder_uses_snapshot(tmp_path, monkeypatch, batch):
    from backend.app.builders import harbor
    from backend.app.models import Job, JobType, VerifyConfig
    root = task_root(tmp_path)
    tests = root / '题目一/tests'
    tests.mkdir()
    (tests / 'judge.toml').write_text('[judge]\nmode="individual"\n')
    monkeypatch.setattr(harbor, 'resolve_harbor_bin', lambda: '/usr/bin/harbor')
    monkeypatch.setattr(images, 'status', lambda: {'ready': True, 'image_id': 'sha256:123'})
    monkeypatch.setattr(images, 'pin_image', lambda image_id: 'prt-ai-factory-harbor-eval-base-image:sha-test')
    job = Job(name='image-test', type=JobType.VERIFY, framework='harbor', owner_id='test', verify=VerifyConfig(tasks_path=str(root), agent='nop', use_platform_image=True, batch_judge=batch))
    result = harbor.build(job, tmp_path / 'work', tmp_path / 'outputs')
    assert result.command[result.command.index('--path') + 1] == str(tmp_path / 'work' / 'platform-image-tasks')
    assert '--force-build' not in result.command
    assert any('平台 Harbor 镜像' in note for note in result.notes)
    copied = tmp_path / 'work/platform-image-tasks/题目一/tests/judge.toml'
    assert tomllib.loads(copied.read_text())['judge']['mode'] == ('batched' if batch else 'individual')
    assert tomllib.loads((tests / 'judge.toml').read_text())['judge']['mode'] == 'individual'
    job.verify.force_build = True
    with pytest.raises(harbor.BuildError, match='关闭强制重建'):
        harbor.build(job, tmp_path / 'work', tmp_path / 'outputs')


def test_pin_uses_local_immutable_tag(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return Mock(returncode=0)
    monkeypatch.setattr(images.subprocess, 'run', run)
    image_id = 'sha256:' + 'a' * 64
    tag = images.pin_image(image_id)
    assert tag.endswith(':sha-' + 'a' * 64)
    assert calls == [['docker', 'tag', image_id, tag]]


def test_build_uses_empty_context_and_prevents_concurrent_builds(monkeypatch):
    callbacks = []
    proc = Mock(returncode=0)
    launch = Mock(return_value=proc)
    monkeypatch.setattr(images.subprocess, 'Popen', launch)
    monkeypatch.setattr(images.threading, 'Thread', lambda **kwargs: Mock(start=lambda: callbacks.append(kwargs['target'])))
    monkeypatch.setattr(images, '_BUILD', {'status': 'idle'})
    assert images.start()['status'] == 'running'
    assert launch.call_args.args[0] == ['docker', 'build', '-t', images.IMAGE, '-']
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as conflict:
        images.start()
    assert conflict.value.status_code == 409
    callbacks[0]()
    assert images._BUILD['status'] == 'succeeded'
    assert proc.communicate.call_args.args[0] == images.recipe().encode()
    proc.returncode = 1
    images.start()
    callbacks[1]()
    assert images._BUILD['status'] == 'failed'
