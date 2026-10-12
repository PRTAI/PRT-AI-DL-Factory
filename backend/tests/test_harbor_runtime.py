import asyncio
import importlib.util
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


def test_phase_caps_keep_shorter_task_limits():
    assert runtime.capped(lambda self: 5000, 1200)(None) == 1200
    assert runtime.capped(lambda self: 20, 1200)(None) == 20
    assert runtime.capped(lambda self: None, 1800)(None) == 1800


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
            await runtime.guarded_exec(original, 0.01)(env, 'sensitive-command', env={'KEEP': 'value'})
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
        task = asyncio.create_task(runtime.guarded_exec(original, 60)(env, 'sleep 999'))
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


def test_harbor_builder_installs_runtime_limits(tmp_path, monkeypatch):
    from backend.app.builders import harbor
    from backend.app.models import Job, VerifyConfig
    monkeypatch.setattr(harbor, 'resolve_harbor_bin', lambda: '/fake/harbor')
    monkeypatch.setattr(harbor, 'resolve_harbor_python', lambda _: '/fake/python')
    task = tmp_path / 'task'
    task.mkdir()
    job = Job(name='bounded', type='verify', framework='harbor', owner_id='user', verify=VerifyConfig(tasks_path=str(task), agent='nop', agent_timeout_seconds=90, verifier_timeout_seconds=60))
    result = harbor.build(job, tmp_path / 'work', tmp_path / 'out')
    assert result.command[:3] == ['/fake/python', '-u', str(tmp_path / 'work' / 'harbor_runtime.py')]
    assert result.env['PRT_AGENT_TIMEOUT_SECONDS'] == '90'
    assert result.env['PRT_VERIFIER_TIMEOUT_SECONDS'] == '60'
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
