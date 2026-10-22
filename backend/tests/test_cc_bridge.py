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


def test_api_batch_retains_each_rows_bridge_flag(client, member_headers, harbor_dataset):
    body = {'common': {'name': 'bridge batch', 'type': 'verify', 'project_id': project_id(),
                       'resources': {'n_gpus': 0}, 'verify': {'dataset_id': harbor_dataset.id}},
            'agents': [config().model_dump(include={'agent','use_openai_bridge','model','agent_env'}), {'agent': 'oracle'}]}
    response = client.post('/api/jobs/verify-batch', headers=member_headers, json=body)
    assert response.status_code == 201, response.text
    jobs = response.json()
    assert jobs[0]['verify']['use_openai_bridge'] is True
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
        asyncio.run(scenario())


def test_normal_claude_does_not_start_bridge():
    runtime = runtime_module()
    async def original(*_): return 'unchanged'
    agent = SimpleNamespace(_get_env=lambda _: None)
    env = Environment(runtime)
    assert asyncio.run(runtime.bridge_run(original)(agent, 'task', env, None)) == 'unchanged'
    assert not env.calls


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
