import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.config import settings
from backend.app.services import harbor_trials

spec = importlib.util.spec_from_file_location('prt_runtime', settings.resources_dir / 'harbor_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_command_wrapper_preserves_quotes_and_exit_code(tmp_path):
    command = "printf '%s' 'a b;$HOME'; exit 7"
    result = subprocess.run(['bash', '-c', runtime.grouped_command(command, str(tmp_path / 'exec.pid'))], capture_output=True, text=True)
    assert result.stdout == 'a b;$HOME'
    assert result.returncode == 7
    assert not (tmp_path / 'exec.pid').exists()


def test_timeout_cleans_container_group_without_exposing_command():
    async def scenario():
        calls = []
        async def original(self, command, **kwargs):
            calls.append((command, kwargs))
            if command.startswith('if [ -f'):
                return SimpleNamespace(return_code=0)
            await asyncio.sleep(30)
        env = SimpleNamespace(logger=logging.getLogger('test.guard'))
        with pytest.raises(asyncio.TimeoutError):
            await runtime.guarded_exec(original)(env, 'sensitive-command', env={'KEEP': 'value'}, timeout_sec=0.01)
        assert len(calls) == 2
        assert calls[0][1]['env'] == {'KEEP': 'value', 'PYTHONUNBUFFERED': '1'}
        assert calls[1][1]['user'] == 'root'
        assert 'kill -KILL --' in calls[1][0]
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_external_cancellation_runs_cleanup():
    async def scenario():
        started = asyncio.Event()
        cleaned = []
        async def original(self, command, **kwargs):
            if command.startswith('if [ -f'):
                cleaned.append(True)
                return SimpleNamespace(return_code=0)
            started.set()
            await asyncio.sleep(30)
        env = SimpleNamespace(logger=logging.getLogger('test.guard'))
        task = asyncio.create_task(runtime.guarded_exec(original)(env, 'sleep 999'))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_nested_logs_and_placeholder_still_running(tmp_path):
    trial = tmp_path / 'trial__123'
    (trial / 'agent' / 'sessions').mkdir(parents=True)
    (trial / 'verifier' / 'graded').mkdir(parents=True)
    (trial / 'config.json').write_text('{}')
    (trial / 'agent' / 'sessions' / 'events.jsonl').write_text('{"event":"working"}\n')
    (trial / 'verifier' / 'graded' / 'judge.log').write_text('criterion done')
    (trial / 'verifier' / 'reward_exit_message.json').write_text('{"exit_reason":"fail-closed placeholder"}')
    secret = tmp_path / 'outside.txt'
    secret.write_text('do not follow')
    (trial / 'agent' / 'link').symlink_to(tmp_path, target_is_directory=True)
    detail = harbor_trials.detail(tmp_path, trial.name)
    assert detail['trial']['phase'] == 'running_verifier'
    assert '占位' in detail['trial']['detail']
    assert detail['agent_logs'][0]['name'] == 'sessions/events.jsonl'
    assert any(item['name'] == 'graded/judge.log' for item in detail['verifier_logs'])
    assert 'do not follow' not in str(detail['agent_logs'])
    (trial / 'result.json').write_text('{"verifier_result":{"rewards":{"reward":0,"verifier_error":1}}}')
    detail = harbor_trials.detail(tmp_path, trial.name)
    assert detail['trial']['phase'] == 'failed'
    assert detail['trial']['exception_type'] == 'VerifierError'


def test_group_cleanup_stops_grandchild(tmp_path):
    pidfile = tmp_path / 'group.pid'
    marker = tmp_path / 'escaped'
    import shlex
    command = f"(sleep 4; touch {shlex.quote(str(marker))}) & wait"
    proc = subprocess.Popen(['bash', '-c', runtime.grouped_command(command, str(pidfile))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        subprocess.run(['bash', '-c', runtime.cleanup_command(str(pidfile))], check=True, timeout=5)
        proc.wait(timeout=3)
        time.sleep(2.1)
        assert not marker.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_long_single_line_log_is_not_empty(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_text('x' * 100000)
    assert harbor_trials._tail(path, 24000) == 'x' * 24000


def test_phase_events_override_file_mtime(tmp_path):
    import json
    import time
    trial = tmp_path / 'trial__1'
    trial.mkdir()
    (trial / 'config.json').write_text('{}')
    target = SimpleNamespace(paths=SimpleNamespace(trial_dir=trial), logger=logging.getLogger('test.phase'))
    async def emit(self, event):
        return 'original called'
    assert asyncio.run(runtime.record_phase(emit)(target, SimpleNamespace(value='agent-start'))) == 'original called'
    data = json.loads((trial / 'prt-phase.json').read_text())
    assert data['phase'] == 'running_agent'
    data['started_at'] = time.time() - 120
    (trial / 'prt-phase.json').write_text(json.dumps(data))
    state = harbor_trials._from_live_dir(trial)
    assert state.phase == 'running_agent'
    assert 119 <= state.phase_seconds['running_agent'] <= 125


def test_harbor_builder_ignores_legacy_platform_limits(tmp_path, monkeypatch):
    from backend.app.builders import harbor
    from backend.app.models import Job, VerifyConfig
    monkeypatch.setattr(harbor, 'resolve_harbor_bin', lambda: '/fake/harbor')
    monkeypatch.setattr(harbor, 'resolve_harbor_python', lambda _: '/fake/python')
    task = tmp_path / 'task'
    task.mkdir()
    job = Job(name='bounded', type='verify', framework='harbor', owner_id='user', verify=VerifyConfig(tasks_path=str(task), agent='nop', agent_timeout_seconds=90, verifier_timeout_seconds=60))
    result = harbor.build(job, tmp_path / 'work', tmp_path / 'out')
    assert result.command[:3] == ['/fake/python', '-u', str(tmp_path / 'work' / 'harbor_runtime.py')]
    assert 'PRT_AGENT_TIMEOUT_SECONDS' not in result.env
    assert 'PRT_VERIFIER_TIMEOUT_SECONDS' not in result.env
    assert 'agent_timeout_seconds' not in job.verify.model_dump()
    assert 'verifier_timeout_seconds' not in job.verify.model_dump()
    assert 'install_guards' in result.files['harbor_runtime.py']


def test_tree_cleanup_includes_new_sessions_and_avoids_reused_pid(monkeypatch):
    import pathlib
    import signal
    import time
    processes = {100: (1, 'a'), 101: (100, 'b'), 102: (101, 'c'), 999: (1, 'unrelated')}
    signals = []
    class FakeStat:
        def __init__(self, pid):
            self.pid = pid
            self.parent = SimpleNamespace(name=str(pid))
        def read_text(self):
            parent, started = processes[self.pid]
            return f'{self.pid} (judge with spaces) ' + ' '.join(['S', str(parent)] + ['0'] * 17 + [started])
    class FakeProc:
        def __init__(self, root):
            assert root == '/proc'
        def glob(self, pattern):
            return [FakeStat(pid) for pid in processes]
    def after_term(seconds):
        processes[101] = (1, 'reused')
        processes[102] = (1, 'c')  # reparented, but still our original grandchild
    with monkeypatch.context() as patch:
        patch.setattr(pathlib, 'Path', FakeProc)
        patch.setattr(os, 'kill', lambda pid, sig: signals.append((pid, sig)))
        patch.setattr(time, 'sleep', after_term)
        patch.setattr(sys, 'argv', ['cleanup', '100'])
        exec(runtime.TREE_CLEANUP, {})
    assert (102, signal.SIGTERM) in signals
    assert (102, signal.SIGKILL) in signals
    assert (101, signal.SIGKILL) not in signals
    assert not any(pid == 999 for pid, _ in signals)


def test_runtime_preserves_native_phase_timeouts(monkeypatch):
    class Trial:
        def _compute_agent_timeout_sec(self):
            return None
        def _compute_verifier_timeout_sec(self):
            return 10800
        def _step_agent_timeout_sec(self, step):
            return 7200
        def _step_verifier_timeout_sec(self, step):
            return 9600
    class DockerEnvironment:
        async def exec(self, command, **kwargs):
            return SimpleNamespace(return_code=0)
    names = ('_compute_agent_timeout_sec', '_compute_verifier_timeout_sec',
             '_step_agent_timeout_sec', '_step_verifier_timeout_sec')
    originals = {name: getattr(Trial, name) for name in names}
    monkeypatch.setitem(sys.modules, 'harbor.agents.installed.codex', SimpleNamespace(Codex=type('Codex', (), {})))
    monkeypatch.setitem(sys.modules, 'harbor.trial.trial', SimpleNamespace(Trial=Trial))
    monkeypatch.setitem(sys.modules, 'harbor.environments.docker.docker', SimpleNamespace(DockerEnvironment=DockerEnvironment))
    monkeypatch.setitem(sys.modules, 'harbor.verifier.verifier', SimpleNamespace(Verifier=type('Verifier', (), {'verify': lambda self: None})))
    runtime.install_guards()
    assert all(getattr(Trial, name) is method for name, method in originals.items())
    assert Trial()._compute_agent_timeout_sec() is None
    assert Trial()._compute_verifier_timeout_sec() == 10800
    assert Trial()._step_agent_timeout_sec(None) == 7200
    assert Trial()._step_verifier_timeout_sec(None) == 9600


@pytest.mark.parametrize('grace', [None, 0, 2500])
def test_codex_runtime_waits_for_mcp_after_native_config_merge(monkeypatch, grace):
    pytest.importorskip('harbor', reason='需要 Harbor 原生 Codex 配置合并')
    from harbor.agents.installed.codex import Codex
    from harbor.models.task.config import MCPServerConfig

    base = {
        'model_provider': 'custom',
        'model_providers': {'custom': {'base_url': 'http://model:8100/v1', 'wire_api': 'responses'}},
        'developer_instructions': 'Use the MCP tools.',
        'mcp_servers': {'existing': {'command': 'server', 'args': ['--readonly']}},
    }
    if grace is not None:
        base['mcp_optional_startup_grace_ms'] = grace
    before = json.dumps(base, sort_keys=True)
    # Exercise Harbor's real merge without creating a model client or container.
    agent = object.__new__(Codex)
    agent._base_config = base
    agent.mcp_servers = [MCPServerConfig(name='world', transport='http', url='http://world:8000/mcp/')]
    monkeypatch.setattr(Codex, '_build_effective_config', runtime.wait_for_codex_mcp(Codex._build_effective_config))
    effective = agent._build_effective_config(openai_base_url='http://override:8100/v1')
    assert effective['mcp_optional_startup_grace_ms'] == (0 if grace is None else grace)
    assert effective['model_provider'] == 'custom'
    assert effective['model_providers'] == base['model_providers']
    assert effective['developer_instructions'] == base['developer_instructions']
    assert effective['openai_base_url'] == 'http://override:8100/v1'
    assert effective['mcp_servers'] == {**base['mcp_servers'], 'world': {'url': 'http://world:8000/mcp/'}}
    assert json.dumps(base, sort_keys=True) == before


def test_codex_runtime_guard_installation_is_idempotent(monkeypatch):
    calls = []
    class Codex:
        def _build_effective_config(self, openai_base_url=None):
            calls.append(openai_base_url)
            return {}
    class DockerEnvironment:
        async def exec(self, command, **kwargs):
            return SimpleNamespace(return_code=0)
    monkeypatch.setitem(sys.modules, 'harbor.agents.installed.codex', SimpleNamespace(Codex=Codex))
    monkeypatch.setitem(sys.modules, 'harbor.trial.trial', SimpleNamespace(Trial=type('Trial', (), {})))
    monkeypatch.setitem(sys.modules, 'harbor.environments.docker.docker', SimpleNamespace(DockerEnvironment=DockerEnvironment))
    monkeypatch.setitem(sys.modules, 'harbor.verifier.verifier', SimpleNamespace(Verifier=type('Verifier', (), {'verify': lambda self: None})))
    runtime.install_guards()
    installed = Codex._build_effective_config
    runtime.install_guards()
    assert Codex._build_effective_config is installed
    assert Codex()._build_effective_config('http://model/v1') == {'mcp_optional_startup_grace_ms': 0}
    assert calls == ['http://model/v1']


@pytest.mark.parametrize('timeout', [None, 96000])
def test_long_commands_have_only_harbor_timeout(monkeypatch, timeout):
    async def scenario():
        clock = [0.0]
        started, release = asyncio.Event(), asyncio.Event()
        waits = []
        native_wait = asyncio.wait
        expected = SimpleNamespace(return_code=0)
        async def original(self, command, **kwargs):
            assert not command.startswith('if [ -f'), 'unexpected cleanup of a running command'
            started.set()
            await release.wait()
            return expected
        async def wait(tasks, *, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                await started.wait()
                clock[0] = 90000  # simulate 25 hours without waiting in real time
                return set(), tasks
            release.set()
            return await native_wait(tasks, timeout=timeout)
        monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(runtime.asyncio, 'wait', wait)
        env = SimpleNamespace(logger=logging.getLogger('test.no-platform-cap'))
        assert await runtime.guarded_exec(original)(env, 'long evaluation', timeout_sec=timeout) is expected
        assert waits == [60, 60]
    asyncio.run(scenario())


def _mounted_log_environment(tmp_path):
    return SimpleNamespace(
        logger=logging.getLogger('test.log-permissions'),
        trial_paths=SimpleNamespace(agent_dir=tmp_path / 'agent', verifier_dir=tmp_path / 'verifier'),
        _mounts=[{'type': 'bind', 'source': str(tmp_path / name), 'target': f'/logs/{name}'}
                 for name in ('agent', 'verifier')],
    )


def test_log_permission_script_preserves_owner_and_ignores_links(tmp_path):
    import stat
    root = tmp_path / 'verifier'
    graded = root / 'graded'
    graded.mkdir(parents=True)
    reward = graded / 'reward.json'
    reward.write_text('{"reward":0.5}')
    graded.chmod(0o700)
    reward.chmod(0o600)
    owner = reward.stat().st_uid
    outside = tmp_path / 'outside'
    outside.mkdir()
    private = outside / 'private.txt'
    private.write_text('private')
    private.chmod(0o600)
    (root / 'link').symlink_to(outside, target_is_directory=True)
    (root / 'linked-file').symlink_to(private)
    os.link(private, root / 'hardlink')
    result = subprocess.run([sys.executable, '-c', runtime.LOG_PERMISSIONS, str(os.getgid()), str(root.resolve())], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(graded.stat().st_mode) == 0o770
    assert stat.S_IMODE(reward.stat().st_mode) == 0o660
    assert reward.stat().st_uid == owner
    assert reward.read_text() == '{"reward":0.5}'
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_permission_repair_only_targets_writable_trial_log_mounts(tmp_path):
    env = _mounted_log_environment(tmp_path)
    env._mounts += [
        {'type': 'bind', 'source': str(tmp_path / 'inputs'), 'target': '/app'},
        {'type': 'bind', 'source': str(tmp_path / 'agent'), 'target': '/app/input_files'},
        {'type': 'bind', 'source': str(tmp_path / 'other-job'), 'target': '/logs/verifier'},
    ]
    assert runtime.log_mount_targets(env) == ['/logs/agent', '/logs/verifier']
    env._mounts[0]['read_only'] = True
    assert runtime.log_mount_targets(env) == ['/logs/verifier']


@pytest.mark.parametrize('outcome', ['success', 'failure', 'timeout', 'cancel'])
def test_permission_repair_runs_before_return_or_propagating_failure(tmp_path, outcome):
    async def scenario():
        calls = []
        env = _mounted_log_environment(tmp_path)
        ready = asyncio.Event()
        expected = SimpleNamespace(return_code=17)
        async def original(self, command, **kwargs):
            if command.startswith('python3 '):
                calls.append('permissions')
                assert kwargs['user'] == 'root'
                assert kwargs['timeout_sec'] == 8
                return SimpleNamespace(return_code=0)
            if command.startswith('if [ -f'):
                calls.append('kill')
                return SimpleNamespace(return_code=0)
            ready.set()
            if outcome == 'success':
                return expected
            if outcome == 'failure':
                raise RuntimeError('original error')
            await asyncio.sleep(30)
        wrapped = runtime.guarded_exec(original)
        if outcome == 'cancel':
            task = asyncio.create_task(wrapped(env, 'verify'))
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == 'timeout':
            with pytest.raises(asyncio.TimeoutError):
                await wrapped(env, 'verify', timeout_sec=0.01)
        elif outcome == 'failure':
            with pytest.raises(RuntimeError, match='original error'):
                await wrapped(env, 'verify')
        else:
            assert await wrapped(env, 'verify') is expected
        assert calls == (['kill', 'permissions'] if outcome in ('timeout', 'cancel') else ['permissions'])
    asyncio.run(scenario())


def test_permission_repair_failure_does_not_mask_verifier_error(tmp_path, caplog):
    async def scenario():
        async def original(self, command, **kwargs):
            if command.startswith('python3 '):
                return SimpleNamespace(return_code=1)
            raise RuntimeError('verifier error')
        with pytest.raises(RuntimeError, match='verifier error'):
            await runtime.guarded_exec(original)(_mounted_log_environment(tmp_path), 'verify')
    asyncio.run(scenario())
    assert '日志目录权限修复失败' in caplog.text


@pytest.mark.parametrize('case', ['platform', 'force', 'docker_image', 'other_base', 'compose'])
def test_platform_task_image_skips_compose_build(tmp_path, monkeypatch, case):
    env_dir = tmp_path / 'environment'
    env_dir.mkdir()
    base = 'ubuntu:24.04' if case == 'other_base' else runtime.PLATFORM_BASE + 'abc123'
    (env_dir / 'Dockerfile').write_text(f'FROM {base}\nCOPY input_files /app\n')
    if case == 'compose':
        (env_dir / 'docker-compose.yaml').write_text('services: {}\n')
    built = []
    async def fake_build(directory, dockerfile, logger):
        built.append(directory)
        return 'prt-platform-task:hash'
    monkeypatch.setattr(runtime, 'build_task_image', fake_build)
    seen = {}
    async def original(self, force_build):
        seen.update(image=self.task_env_config.docker_image, prebuilt=self._env_vars.prebuilt_image_name)
    env = SimpleNamespace(environment_dir=env_dir, logger=logging.getLogger('test.image'),
                          task_env_config=SimpleNamespace(docker_image='given:tag' if case == 'docker_image' else None),
                          _env_vars=SimpleNamespace(prebuilt_image_name=None))
    asyncio.run(runtime.reuse_task_image(original)(env, case == 'force'))
    if case == 'platform':
        assert built == [env_dir]
        assert seen == {'image': 'prt-platform-task:hash', 'prebuilt': 'prt-platform-task:hash'}
    else:
        assert not built
        assert seen['prebuilt'] is None


def test_platform_task_image_tag_is_content_addressed_and_reused(tmp_path, monkeypatch):
    pytest.importorskip('harbor')
    pytest.importorskip('filelock')
    env_dir = tmp_path / 'environment'
    env_dir.mkdir()
    dockerfile = env_dir / 'Dockerfile'
    dockerfile.write_text(f'FROM {runtime.PLATFORM_BASE}abc\n')
    commands = []
    present = set()
    async def fake_exists(image): return image in present
    class Process:
        returncode = 0
        async def communicate(self): return b'', None
    async def fake_exec(*argv, **kwargs):
        commands.append(argv)
        present.add(argv[3])
        return Process()
    monkeypatch.setattr(runtime, 'image_exists', fake_exists)
    monkeypatch.setattr(runtime.asyncio, 'create_subprocess_exec', fake_exec)
    log = logging.getLogger('test.image')
    first = asyncio.run(runtime.build_task_image(env_dir, dockerfile, log))
    again = asyncio.run(runtime.build_task_image(env_dir, dockerfile, log))
    assert first == again and first.startswith('prt-platform-task:')
    assert len(commands) == 1 and commands[0][:2] == ('docker', 'build')
    dockerfile.write_text(f'FROM {runtime.PLATFORM_BASE}def\n')
    assert asyncio.run(runtime.build_task_image(env_dir, dockerfile, log)) != first


def test_task_image_keep_clamps_and_defaults(monkeypatch):
    monkeypatch.delenv(runtime.TASK_IMAGE_KEEP_ENV, raising=False)
    assert runtime.task_image_keep() == runtime.DEFAULT_TASK_IMAGE_KEEP
    monkeypatch.setenv(runtime.TASK_IMAGE_KEEP_ENV, '5')
    assert runtime.task_image_keep() == 5
    monkeypatch.setenv(runtime.TASK_IMAGE_KEEP_ENV, '0')
    assert runtime.task_image_keep() == 1
    monkeypatch.setenv(runtime.TASK_IMAGE_KEEP_ENV, 'not-a-number')
    assert runtime.task_image_keep() == runtime.DEFAULT_TASK_IMAGE_KEEP


def test_prune_task_images_removes_only_images_beyond_the_retention(monkeypatch):
    calls = []
    class Process:
        def __init__(self, returncode=0, output=b''):
            self.returncode = returncode
            self.output = output
        async def communicate(self): return self.output, None
    async def fake_exec(*argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ('image', 'ls'):
            # docker image ls lists newest first
            return Process(output=b'prt-platform-task:new\nprt-platform-task:mid\nprt-platform-task:old\n')
        return Process()
    monkeypatch.setattr(runtime.asyncio, 'create_subprocess_exec', fake_exec)
    asyncio.run(runtime.prune_task_images(2, logging.getLogger('test.prune')))
    assert calls[-1] == ('docker', 'image', 'rm', 'prt-platform-task:old')


def test_prune_task_images_is_best_effort(monkeypatch, caplog):
    class Process:
        def __init__(self, returncode=0, output=b''):
            self.returncode = returncode
            self.output = output
        async def communicate(self): return self.output, None
    async def fake_exec(*argv, **kwargs):
        if argv[1:3] == ('image', 'ls'):
            return Process(output=b'prt-platform-task:new\nprt-platform-task:old\n')
        return Process(returncode=1, output=b'image is being used by a container')
    monkeypatch.setattr(runtime.asyncio, 'create_subprocess_exec', fake_exec)
    asyncio.run(runtime.prune_task_images(1, logging.getLogger('test.prune')))
    assert '清理旧平台题目镜像失败' in caplog.text


def test_build_task_image_prunes_only_after_a_real_build(tmp_path, monkeypatch):
    pytest.importorskip('harbor')
    pytest.importorskip('filelock')
    env_dir = tmp_path / 'environment'
    env_dir.mkdir()
    dockerfile = env_dir / 'Dockerfile'
    dockerfile.write_text(f'FROM {runtime.PLATFORM_BASE}abc\n')
    present = set()
    pruned = []
    class Process:
        returncode = 0
        async def communicate(self): return b'', None
    async def fake_exists(image): return image in present
    async def fake_exec(*argv, **kwargs):
        present.add(argv[3])
        return Process()
    async def fake_prune(keep, logger): pruned.append(keep)
    monkeypatch.setattr(runtime, 'image_exists', fake_exists)
    monkeypatch.setattr(runtime, 'prune_task_images', fake_prune)
    monkeypatch.setattr(runtime.asyncio, 'create_subprocess_exec', fake_exec)
    monkeypatch.setenv(runtime.TASK_IMAGE_KEEP_ENV, '4')
    log = logging.getLogger('test.image')
    first = asyncio.run(runtime.build_task_image(env_dir, dockerfile, log))
    assert asyncio.run(runtime.build_task_image(env_dir, dockerfile, log)) == first
    assert pruned == [4]
