"""Bridge routing, per-user credentials, lifecycle hooks and actual protocol adapter."""
import asyncio
import importlib.util
import json
import logging
import shlex
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from backend.app.builders import harbor
from backend.app.config import settings
from backend.app.models import Framework, Job, JobType, ModelApiConfig, ModelPlatformConfig, ResourceSpec, VerifyConfig
from backend.app.services import cc_bridge_service, model_api_service
from backend.app.store import store
from .conftest import grant_key, project_id, user_id_of
from .test_builders import harbor_dataset


def runtime_module():
    spec = importlib.util.spec_from_file_location('test_cc_bridge_runtime', settings.resources_dir / 'cc_bridge_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(**updates):
    return VerifyConfig(agent='claude-code', use_openai_bridge=True, model='openai/gpt-6.1-sol-codex',
                        agent_env={'OPENAI_API_KEY': 'sk-manual-bridge-secret', 'OPENAI_BASE_URL': 'https://api.atlascloud.ai/v1'}, **updates)


def test_manual_bridge_resolves_upstream_and_disables_deferred_tool_search():
    result = model_api_service.resolve_agent(config(), user_id='unused')
    assert result.model == 'openai/gpt-6.1-sol-codex'
    assert result.env['PRT_CC_BRIDGE_UPSTREAM_API_KEY'] == 'sk-manual-bridge-secret'
    assert result.env['ANTHROPIC_API_KEY'] != 'sk-manual-bridge-secret'
    assert result.env['ENABLE_TOOL_SEARCH'] == 'false'
    assert 'sk-manual-bridge-secret' in result.secrets
    assert model_api_service.agent_protocol('claude-code', 'openai', use_openai_bridge=True) == 'responses'


@pytest.mark.parametrize('requested', ['', 'openai', 'anthropic'])
def test_bridge_overrides_saved_native_protocol(requested):
    value = config(agent_api_protocol=requested)
    assert model_api_service.agent_protocol('claude-code', requested, use_openai_bridge=True) == 'responses'
    assert model_api_service.resolve_agent(value, user_id='unused').env['PRT_CC_BRIDGE_ENABLED'] == '1'


def test_native_claude_still_rejects_openai_without_bridge():
    with pytest.raises(HTTPException) as caught:
        model_api_service.agent_protocol('claude-code', 'openai')
    assert '必须使用 Anthropic' in caught.value.detail


@pytest.mark.parametrize('requested', ['openai', 'anthropic'])
@pytest.mark.parametrize('source', ['manual', 'platform'])
def test_api_single_submit_accepts_bridge_with_openai_or_saved_protocol(client, member_headers, harbor_dataset, requested, source):
    value = config(dataset_id=harbor_dataset.id, agent_api_protocol=requested)
    if source == 'platform':
        platform = ModelPlatformConfig(vendor='atlascloud', name='Responses-only bridge', endpoints={'responses': 'https://api.atlascloud.ai/v1'})
        model = ModelApiConfig(name='GPT Responses only', model='openai/gpt-6.1-sol-codex', model_is_server_id=True,
                               platform_id=platform.id, supported_protocols=['responses'])
        store.model_platforms.put(platform)
        store.model_apis.put(model)
        grant_key(user_id_of('dev@prt.local'), platform.id, 'sk-manual-bridge-secret')
        value.model_api_id = model.id
        value.agent_env = {}
    response = client.post('/api/jobs', headers=member_headers, json={
        'name': 'bridge submit', 'type': 'verify', 'project_id': project_id(),
        'resources': {'n_gpus': 0}, 'verify': value.model_dump(),
    })
    assert response.status_code == 201, response.text
    assert response.json()['verify']['use_openai_bridge'] is True
    assert response.json()['verify']['agent_api_protocol'] == 'openai'
    assert 'sk-manual-bridge-secret' not in response.text


@pytest.mark.parametrize('agent', ['codex', 'oracle', 'custom:Agent'])
def test_bridge_rejects_other_agents(agent):
    with pytest.raises(HTTPException):
        model_api_service.agent_protocol(agent, use_openai_bridge=True)


@pytest.mark.parametrize('kind', ['key', 'url', 'model', 'reserved'])
def test_bridge_rejects_missing_or_unsafe_configuration(kind):
    value = config()
    if kind == 'key': value.agent_env.pop('OPENAI_API_KEY')
    if kind == 'url': value.agent_env['OPENAI_BASE_URL'] = 'https://user:password@bad.invalid/v1'
    if kind == 'model': value.model = 'https://model.invalid'
    if kind == 'reserved': value.agent_env['PRT_CC_BRIDGE_ENABLED'] = '0'
    with pytest.raises(HTTPException):
        model_api_service.resolve_agent(value, user_id='unused')


def test_platform_selects_responses_and_submitters_own_key():
    platform = ModelPlatformConfig(vendor='atlascloud', name='bridge test', endpoints={'responses': 'https://api.atlascloud.ai/v1', 'anthropic': 'https://wrong.invalid/messages'})
    model = ModelApiConfig(name='bridge GPT', model='openai/gpt-6.1-sol-codex', model_is_server_id=True,
                           platform_id=platform.id, supported_protocols=['responses', 'anthropic'])
    store.model_platforms.put(platform)
    store.model_apis.put(model)
    grant_key('bridge-user-a', platform.id, 'sk-bridge-key-user-a')
    grant_key('bridge-user-b', platform.id, 'sk-bridge-key-user-b')
    value = VerifyConfig(agent='claude-code', use_openai_bridge=True, model_api_id=model.id)
    a = model_api_service.resolve_agent(value, user_id='bridge-user-a')
    b = model_api_service.resolve_agent(value, user_id='bridge-user-b')
    assert a.env['PRT_CC_BRIDGE_UPSTREAM_BASE_URL'] == 'https://api.atlascloud.ai/v1'
    assert a.env['PRT_CC_BRIDGE_UPSTREAM_API_KEY'] == 'sk-bridge-key-user-a'
    assert b.env['PRT_CC_BRIDGE_UPSTREAM_API_KEY'] == 'sk-bridge-key-user-b'
    with pytest.raises(HTTPException):
        model_api_service.resolve_agent(value, user_id='bridge-no-key')


def test_builder_stages_bridge_bundle_and_masks_all_credentials(tmp_path, harbor_dataset):
    item = Job(name='bridge', type=JobType.VERIFY, framework=Framework.HARBOR,
               owner_id='bridge-user', resources=ResourceSpec(n_gpus=0), verify=config(dataset_id=harbor_dataset.id))
    built = harbor.build(item, tmp_path / 'work', tmp_path / 'out')
    assert '--agent' in built.command and built.command[built.command.index('--agent')+1] == 'claude-code'
    assert 'prt_cc_bridge_runtime.py' in built.files
    assert 'cc-openai-bridge/src/cc_openai_bridge/app.py' in built.files
    assert 'cc-openai-bridge/prt_bridge_server.py' in built.files
    assert 'sk-manual-bridge-secret' not in built.masked_command_line()
    assert 'sk-manual-bridge-secret' not in json.dumps(built.masked_env())
    assert 'sk-manual-bridge-secret' not in json.dumps(built.files)
    assert built.env['PRT_AGENT_PRT_CC_BRIDGE_ENABLED'] == '1'
    item.verify.use_openai_bridge = False
    item.verify.agent_api_protocol = 'anthropic'
    built = harbor.build(item, tmp_path / 'work2', tmp_path / 'out2')
    assert 'prt_cc_bridge_runtime.py' not in built.files


@pytest.mark.parametrize('requested', ['openai', 'anthropic'])
def test_api_batch_retains_each_rows_bridge_flag(client, member_headers, harbor_dataset, requested):
    body = {'common': {'name': 'bridge batch', 'type': 'verify', 'project_id': project_id(),
                       'resources': {'n_gpus': 0}, 'verify': {'dataset_id': harbor_dataset.id}},
            'agents': [config(agent_api_protocol=requested).model_dump(include={'agent','agent_api_protocol','use_openai_bridge','model','agent_env'}), {'agent': 'oracle'}]}
    response = client.post('/api/jobs/verify-batch', headers=member_headers, json=body)
    assert response.status_code == 201, response.text
    jobs = response.json()
    assert jobs[0]['verify']['use_openai_bridge'] is True
    assert jobs[0]['verify']['agent_api_protocol'] == 'openai'
    assert jobs[1]['verify']['use_openai_bridge'] is False
    assert 'sk-manual-bridge-secret' not in response.text


class Environment:
    def __init__(self, runtime, fail_ready=False):
        self.runtime, self.fail_ready = runtime, fail_ready
        self.calls = []
        self.uploads = []
    async def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if shlex.quote(self.runtime.READY) in command:
            return SimpleNamespace(return_code=int(self.fail_ready), stdout=json.dumps({'url': 'http://127.0.0.1:23123'}), stderr='')
        return SimpleNamespace(return_code=0, stdout='', stderr='')
    async def upload_dir(self, source, destination):
        self.uploads.append((source, destination))


def test_runtime_auth_tool_harness_and_cleanup_on_success_failure_cancel():
    runtime = runtime_module()
    for outcome in ['success', 'failure', 'cancel', 'startup_failure']:
        async def scenario():
            agent = SimpleNamespace(_get_env=lambda key: {'PRT_CC_BRIDGE_ENABLED': '1',
                'PRT_CC_BRIDGE_UPSTREAM_BASE_URL': 'https://upstream.invalid/v1',
                'PRT_CC_BRIDGE_UPSTREAM_API_KEY': 'sk-upstream-secret'}.get(key),
                model_name='gpt-6-astra', environment_logs_dir=Path('/logs/agent'), logger=logging.getLogger('bridge-test'))
            env = Environment(runtime, fail_ready=outcome=='startup_failure')
            visited = []
            async def original(self, instruction, environment, context):
                visited.append((instruction, context))
                auth = runtime.bridge_auth(lambda _: {'wrong': 'credentials'})(self)
                assert auth['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:23123'
                assert auth['ANTHROPIC_API_KEY'] != 'sk-upstream-secret'
                if outcome == 'failure': raise ValueError('agent failed')
                if outcome == 'cancel': raise asyncio.CancelledError()
                return 'native tools retained'
            wrapped = runtime.bridge_run(original)
            if outcome == 'failure':
                with pytest.raises(ValueError): await wrapped(agent, 'instruction', env, 'ctx')
            elif outcome == 'cancel':
                with pytest.raises(asyncio.CancelledError): await wrapped(agent, 'instruction', env, 'ctx')
            elif outcome == 'startup_failure':
                with pytest.raises(RuntimeError): await wrapped(agent, 'instruction', env, 'ctx')
                assert not visited
            else:
                assert await wrapped(agent, 'instruction', env, 'ctx') == 'native tools retained'
            assert any(shlex.quote(runtime.STOP) in command for command, _ in env.calls)
            assert all('sk-upstream-secret' not in command for command, _ in env.calls)
            assert env.calls[0][1]['env']['BRIDGE_ALLOWED_MODELS'] == 'gpt-6-astra'
            assert env.calls[0][1]['env']['PYTHONPATH'] == runtime.SOURCE
        asyncio.run(scenario())


def test_bridge_auth_wins_over_placeholder_agent_env():
    """Harbor 合并顺序：persistent < 每次 exec 的 env < Trial 的 scoped env（--agent-env）。"""
    import contextlib
    runtime = runtime_module()

    class ScopedEnvironment(Environment):
        def __init__(self, runtime):
            super().__init__(runtime)
            # Trial 把 --agent-env 作为 scoped env 包住 agent.run
            self.overlays = [{'ANTHROPIC_BASE_URL': 'http://127.0.0.1:18876', 'ANTHROPIC_API_KEY': 'prt-managed-bridge'}]
        @contextlib.contextmanager
        def scoped_exec_env(self, env):
            self.overlays.append(dict(env))
            try:
                yield
            finally:
                self.overlays.pop()
        def merged(self, env):
            result = dict(env or {})
            for overlay in self.overlays:
                result.update(overlay)
            return result

    agent = SimpleNamespace(_get_env=lambda key: {'PRT_CC_BRIDGE_ENABLED': '1',
        'PRT_CC_BRIDGE_UPSTREAM_BASE_URL': 'https://upstream.invalid/v1',
        'PRT_CC_BRIDGE_UPSTREAM_API_KEY': 'sk-upstream-secret'}.get(key),
        model_name='openai/gpt-6-astra', environment_logs_dir=Path('/logs/agent'), logger=logging.getLogger('bridge-test'))
    env = ScopedEnvironment(runtime)
    seen = {}
    async def original(self, instruction, environment, context):
        # Claude Code 原生 run：auth env 作为每次 exec 的 env，再被 scoped env 覆盖
        seen.update(environment.merged(runtime.bridge_auth(lambda _: {})(self)))
    asyncio.run(runtime.bridge_run(original)(agent, 'task', env, None))
    assert seen['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:23123'
    assert seen['ANTHROPIC_API_KEY'] not in {'prt-managed-bridge', 'sk-upstream-secret'}
    assert seen['CLAUDE_CODE_ATTRIBUTION_HEADER'] == '0'
    assert len(env.overlays) == 1


def test_normal_claude_does_not_start_bridge():
    runtime = runtime_module()
    async def original(*_): return 'unchanged'
    agent = SimpleNamespace(_get_env=lambda _: None)
    env = Environment(runtime)
    assert asyncio.run(runtime.bridge_run(original)(agent, 'task', env, None)) == 'unchanged'
    assert not env.calls


def test_platform_image_preinstalls_bridge_dependencies_with_same_pins():
    import tomllib
    pins = tomllib.loads((settings.resources_dir / 'cc-openai-bridge' / 'pyproject.toml').read_text())['project']['dependencies']
    dockerfile = (settings.resources_dir / 'harbor-eval.Dockerfile').read_text()
    assert '/opt/prt-cc-openai-bridge/venv' in dockerfile
    for pin in pins:
        assert f'"{pin}"' in dockerfile
    runtime = runtime_module()
    # 预装环境命中时不联网：源码经 PYTHONPATH 加载，Bridge 进程同样带上
    assert 'PYTHONPATH=' + runtime.SOURCE in runtime.INSTALL
    assert runtime.INSTALL.index('exit 0') < runtime.INSTALL.index('pip install')


def test_bridge_bundle_scripts_compile():
    files = cc_bridge_service.runtime_files(config())
    for name, text in files.items():
        if name.endswith('.py'): compile(text, name, 'exec')


def test_runtime_setup_uploads_bundle_and_uses_separate_environment(tmp_path):
    runtime = runtime_module()
    (tmp_path / 'setup').mkdir()
    agent = SimpleNamespace(_get_env=lambda key: '1' if key=='PRT_CC_BRIDGE_ENABLED' else None, logs_dir=tmp_path)
    env = Environment(runtime)
    calls = []
    async def original(*_): calls.append('native-setup')
    asyncio.run(runtime.bridge_setup(original)(agent, env))
    assert calls == ['native-setup']
    assert len(env.uploads) == 1
    assert env.uploads[0][1] == '/opt/prt-cc-openai-bridge/source'
    assert any(command == runtime.INSTALL for command, _ in env.calls)
    assert '--python 3.12' in runtime.INSTALL
    assert '/venv/bin/python' in runtime.INSTALL
    assert (tmp_path / 'setup' / 'openai-bridge.txt').is_file()


def test_resume_build_keeps_bridge_routing_and_bundled_runtime(tmp_path):
    from backend.app.builders.harbor_actions import build_action
    from backend.app.models.job import HarborAction
    item = Job(name='bridge resume', type=JobType.VERIFY, framework=Framework.HARBOR,
               owner_id='bridge-owner', verify=config(), harbor_action=HarborAction(mode='resume', source_job_id='previous'))
    built = build_action(item, tmp_path / 'work', tmp_path / 'out')
    assert 'prt_cc_bridge_runtime.py' in built.files
    refs = json.loads(built.files['harbor-env-refs.json'])
    assert refs['agent']['PRT_CC_BRIDGE_ENABLED'] == '${PRT_SELECTED_AGENT_PRT_CC_BRIDGE_ENABLED}'
    assert 'PRT_CC_BRIDGE_UPSTREAM_API_KEY' in refs['agent']
    assert 'PRT_CC_BRIDGE_UPSTREAM_API_KEY' not in refs['verifier']
    assert 'sk-manual-bridge-secret' not in json.dumps(built.masked_env())


def test_runtime_prompt_cache_key_is_stable_per_model_and_task():
    runtime = runtime_module()
    keys = []
    for model, task in [('gpt-6-astra', 'task-a'), ('gpt-6-astra', 'task-a'), ('gpt-6-astra', 'task-b'), ('other', 'task-a')]:
        agent = SimpleNamespace(_get_env=lambda key: {'PRT_CC_BRIDGE_ENABLED': '1',
            'PRT_CC_BRIDGE_UPSTREAM_BASE_URL': 'https://upstream.invalid/v1',
            'PRT_CC_BRIDGE_UPSTREAM_API_KEY': 'sk-upstream-secret'}.get(key),
            model_name=model, environment_logs_dir=Path('/logs/agent'), logger=logging.getLogger('bridge-test'))
        env = Environment(runtime)
        env.environment_name = task
        async def original(*_): return None
        asyncio.run(runtime.bridge_run(original)(agent, 'task', env, None))
        keys.append(env.calls[0][1]['env']['BRIDGE_PROMPT_CACHE_KEY'])
    assert keys[0] == keys[1]
    assert len({keys[0], keys[2], keys[3]}) == 3
    assert all(len(key) <= 64 for key in keys)


def _run_install(tmp_path, *, pip_ok_without_mirror, with_uv=False):
    """在 bash 里真实执行 INSTALL：python3 / venv python / uv 都换成桩，模拟题目镜像的 pip 源缺 Bridge 锁定的版本。"""
    import os
    import subprocess
    runtime = runtime_module()
    root = tmp_path / 'bridge'
    (root / 'source').mkdir(parents=True)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'installed'
    log = tmp_path / 'calls.log'
    venv_python = (
        '#!/bin/bash\n'
        f'echo "venv-python PIP_INDEX_URL=${{PIP_INDEX_URL:-}} $*" >> {log}\n'
        f'if [ "$1" = "-c" ]; then [ -f {marker} ]; exit $?; fi\n'
        'if [ "$1 $2" = "-m pip" ]; then\n'
        + (f'  if [ "${{PIP_INDEX_URL:-}}" = "https://pypi.org/simple" ]; then touch {marker}; exit 0; fi\n' if pip_ok_without_mirror else '')
        + '  echo "ERROR: No matching distribution found for fastapi==0.143.1" >&2; exit 1\nfi\nexit 1\n')
    make_venv = f'mkdir -p "$VENV/bin" && cat > "$VENV/bin/python" <<\'STUB\'\n{venv_python}STUB\nchmod +x "$VENV/bin/python"\n'
    (bin_dir / 'python3').write_text(
        '#!/bin/bash\nif [ "$1" = "-c" ]; then exit 0; fi\n'
        f'if [ "$1 $2" = "-m venv" ]; then VENV="$3"\n{make_venv}exit 0; fi\nexit 1\n')
    if with_uv:
        (bin_dir / 'uv').write_text(
            f'#!/bin/bash\necho "uv $*" >> {log}\n'
            f'if [ "$1" = "venv" ]; then VENV="${{@: -1}}"\n{make_venv}exit 0; fi\n'
            f'if [ "$1 $2" = "pip install" ]; then touch {marker}; exit 0; fi\nexit 1\n')
    (bin_dir / 'sleep').write_text('#!/bin/sh\nexit 0\n')  # 重试间隔不必真等
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    script = runtime.INSTALL.replace(runtime.ROOT, str(root))
    env = {**os.environ, 'PATH': f'{bin_dir}:/usr/bin:/bin'}
    env.pop('PIP_INDEX_URL', None)
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=120)
    return result, log.read_text() if log.exists() else ''


def test_install_falls_back_to_official_pypi_when_image_mirror_lacks_pin(tmp_path):
    # job_34b20cb0c983：题目镜像的 pip 源下载不到 fastapi==0.143.1，Agent 还没启动就失败
    result, calls = _run_install(tmp_path, pip_ok_without_mirror=True)
    assert result.returncode == 0, result.stderr
    assert 'PIP_INDEX_URL=https://pypi.org/simple -m pip install' in calls


def test_install_falls_back_to_uv_when_pip_cannot_install(tmp_path):
    result, calls = _run_install(tmp_path, pip_ok_without_mirror=False, with_uv=True)
    assert result.returncode == 0, result.stderr
    assert 'uv venv --clear --python 3.12' in calls
    assert 'uv pip install' in calls
