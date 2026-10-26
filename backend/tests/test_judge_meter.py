import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.config import settings

spec = importlib.util.spec_from_file_location('prt_runtime_meter', settings.resources_dir / 'harbor_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
PROXY = settings.resources_dir / 'judge_meter_proxy.py'

ANTHROPIC_STREAM = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"claude-x",'
    '"usage":{"input_tokens":10,"cache_read_input_tokens":80,"cache_creation_input_tokens":5,"output_tokens":1}}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":42}}\n\n'
)
CHAT_STREAM = (
    'data: {"id":"chatcmpl-1","model":"qwen3.8-max","choices":[{"delta":{"content":"hi"}}]}\n\n'
    'data: {"id":"chatcmpl-1","model":"qwen3.8-max","choices":[],"usage":{"prompt_tokens":100,'
    '"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":60}}}\n\ndata: [DONE]\n\n'
)


class Upstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Upstream.seen.append((self.path, self.headers.get('Authorization'), body))
        if self.path.endswith('/messages'):
            payload, kind = ANTHROPIC_STREAM, 'text/event-stream'
        elif body.get('stream'):
            payload, kind = CHAT_STREAM, 'text/event-stream'
        elif self.path.endswith('/fail'):
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"usage":{"prompt_tokens":1,"completion_tokens":1}}')
            return
        else:
            payload, kind = json.dumps({'id': 'resp_1', 'model': 'gpt-x', 'usage': {
                'input_tokens': 30, 'output_tokens': 4, 'input_tokens_details': {'cached_tokens': 10}}}), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.end_headers()
        self.wfile.write(payload.encode())


@pytest.fixture
def meter(tmp_path):
    Upstream.seen = []
    upstream = HTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    state, usage = tmp_path / 'state.json', tmp_path / 'prt-model-usage.jsonl'
    env = {**os.environ, 'PRT_JUDGE_METER_UPSTREAM': f'http://127.0.0.1:{upstream.server_address[1]}/compatible/v1',
           'NO_PROXY': '*', 'no_proxy': '*'}
    process = subprocess.Popen([sys.executable, str(PROXY), str(state), str(usage)], env=env)
    for _ in range(100):
        if state.exists():
            break
        time.sleep(0.05)
    port = json.loads(state.read_text())['port']
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, body):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer sk-user', 'Content-Type': 'application/json'})
        try:
            return opener.open(req, timeout=10).read()
        except urllib.error.HTTPError as exc:
            return exc.read()

    yield post, usage
    process.terminate()
    process.wait(5)
    upstream.shutdown()


def records(usage):
    return [json.loads(line) for line in usage.read_text().splitlines()] if usage.exists() else []


def test_proxy_records_usage_for_each_protocol_and_keeps_client_credentials(meter):
    post, usage = meter
    assert b'message_delta' in post('/compatible/v1/messages', {'model': 'claude-x', 'stream': True})
    assert b'[DONE]' in post('/compatible/v1/chat/completions', {'model': 'qwen3.8-max', 'stream': True})
    assert b'resp_1' in post('/compatible/v1/responses', {'model': 'gpt-x'})
    rows = {row['request_id']: row for row in records(usage)}
    assert rows['msg_1']['metrics'] == {'prompt_tokens': 95, 'completion_tokens': 42, 'cached_tokens': 80,
                                        'extra': {'cache_creation_input_tokens': 5, 'cache_creation': {}}}
    assert rows['chatcmpl-1']['metrics']['prompt_tokens'] == 100
    assert rows['chatcmpl-1']['metrics']['cached_tokens'] == 60
    assert rows['resp_1']['metrics']['prompt_tokens'] == 30
    assert rows['resp_1']['metrics']['cached_tokens'] == 10
    assert all(row['schema_version'] == 'prt-usage-v1' for row in rows.values())
    # Path is forwarded as-is, credentials untouched, streaming chat asks for the usage chunk.
    assert [path for path, *_ in Upstream.seen] == ['/compatible/v1/messages', '/compatible/v1/chat/completions',
                                                     '/compatible/v1/responses']
    assert {auth for _, auth, _ in Upstream.seen} == {'Bearer sk-user'}
    assert Upstream.seen[1][2]['stream_options'] == {'include_usage': True}
    assert 'sk-user' not in usage.read_text()


def test_proxy_ignores_failed_responses(meter):
    post, usage = meter
    post('/compatible/v1/fail', {'model': 'x'})
    assert records(usage) == []


def test_local_url_keeps_path_and_query():
    assert runtime.local_judge_url('https://dashscope.aliyuncs.com/compatible-mode/v1?x=1', 4321) == \
        'http://127.0.0.1:4321/compatible-mode/v1?x=1'


class FakeEnvironment:
    os = 'linux'

    def __init__(self, ready_code=0, start_code=0):
        self.calls, self.ready_code, self.start_code = [], ready_code, start_code

    async def upload_file(self, source, target):
        self.calls.append(('upload', target))

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append(('exec', command, env, user))
        if 'nohup' in command:
            return SimpleNamespace(return_code=self.start_code, stdout='')
        if '__prt_judge_meter_health' in command:
            return SimpleNamespace(return_code=self.ready_code, stdout='5555\n')
        return SimpleNamespace(return_code=0, stdout='')


def verifier(environment, env):
    return SimpleNamespace(environment=environment, override_env={'KEEP': '1'}, verifier_env=env,
                           task=SimpleNamespace(config=SimpleNamespace(verifier=SimpleNamespace(env={}))),
                           logger=logging.getLogger('test.meter'))


def run_verify(target):
    seen = {}

    async def original(self):
        seen['env'] = dict(self.override_env)
        return 'result'
    result = asyncio.run(runtime.metered_verify(original)(target))
    return result, seen['env']


@pytest.fixture
def harbor_paths(monkeypatch):
    """Stand-ins for the two Harbor helpers the wrapper uses (Harbor is not in the test venv)."""
    from pathlib import PurePosixPath
    import types
    paths = types.ModuleType('harbor.models.trial.paths')
    paths.EnvironmentPaths = SimpleNamespace(for_os=lambda _: SimpleNamespace(verifier_dir=PurePosixPath('/logs/verifier')))
    env = types.ModuleType('harbor.utils.env')
    env.resolve_env_vars = lambda values: {key: os.path.expandvars(value) for key, value in values.items()}
    for name in ('harbor', 'harbor.models', 'harbor.models.trial', 'harbor.utils'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, 'harbor.models.trial.paths', paths)
    monkeypatch.setitem(sys.modules, 'harbor.utils.env', env)
    # The proxy ships as prt_judge_meter.py next to the runtime; here it is the source file.
    monkeypatch.setattr(runtime, 'JUDGE_METER', PROXY.name)


def test_verifier_routes_judge_through_meter_and_restores_env(harbor_paths, monkeypatch):
    environment = FakeEnvironment()
    monkeypatch.setenv('PRT_VERIFIER_JUDGE_BASE_URL', 'https://api.example.com/v1')
    target = verifier(environment, {'JUDGE_BASE_URL': '${PRT_VERIFIER_JUDGE_BASE_URL}', 'JUDGE_API_KEY': 'sk-secret'})
    result, env = run_verify(target)
    assert result == 'result'
    assert env == {'KEEP': '1', 'JUDGE_BASE_URL': 'http://127.0.0.1:5555/v1', 'PRT_JUDGE_METERED': '1'}
    assert target.override_env == {'KEEP': '1'}
    start = next(call for call in environment.calls if call[0] == 'exec' and 'nohup' in call[1])
    assert start[2] == {'PRT_JUDGE_METER_UPSTREAM': 'https://api.example.com/v1'}
    assert 'sk-secret' not in json.dumps(environment.calls)
    assert environment.calls[-1][3] == 'root'  # stop script


@pytest.mark.parametrize('ready_code,start_code', [(1, 0), (0, 127)])
def test_verifier_falls_back_to_direct_judge(harbor_paths, monkeypatch, ready_code, start_code):
    environment = FakeEnvironment(ready_code=ready_code, start_code=start_code)
    target = verifier(environment, {'JUDGE_BASE_URL': 'https://api.example.com/v1'})
    result, env = run_verify(target)
    assert result == 'result'
    assert env == {'KEEP': '1'}


def test_verifier_without_judge_is_untouched(harbor_paths, monkeypatch):
    environment = FakeEnvironment()
    result, env = run_verify(verifier(environment, {}))
    assert env == {'KEEP': '1'}
    assert environment.calls == []


def test_rewardkit_recorder_defers_to_meter(monkeypatch):
    spec = importlib.util.spec_from_file_location('prt_rk', settings.resources_dir / 'rewardkit_runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('PRT_JUDGE_METERED', '1')
    monkeypatch.setitem(sys.modules, 'litellm', None)  # would raise if the recorder tried to import it
    module.install_usage_recorder()


def test_harbor_builder_ships_meter_only_with_judge():
    from backend.app.services import harbor_compat
    assert harbor_compat.judge_meter_files({}) == {}
    files = harbor_compat.judge_meter_files({'JUDGE_BASE_URL': 'https://x/v1'})
    assert 'def main' in files['prt_judge_meter.py']
