"""Job-local hooks for the supplied bridge; executes only in Harbor's interpreter."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import secrets
import shlex
import uuid
from pathlib import Path

PREFIX = 'PRT_CC_BRIDGE_'
ROOT = '/opt/prt-cc-openai-bridge'
PYTHON = ROOT + '/venv/bin/python'

INSTALL = r'''
set -eu
export LITELLM_LOCAL_MODEL_COST_MAP=True
if [ -x /opt/prt-cc-openai-bridge/venv/bin/python ] && /opt/prt-cc-openai-bridge/venv/bin/python -c 'from importlib.metadata import version; assert version("cc-openai-bridge") == "1.0.0"; assert version("litellm") == "1.105.1"' >/dev/null 2>&1; then exit 0; fi
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14)' >/dev/null 2>&1 && python3 -m venv /opt/prt-cc-openai-bridge/venv; then
  /opt/prt-cc-openai-bridge/venv/bin/python -m pip install --no-cache-dir /opt/prt-cc-openai-bridge/source
else
  uv_bin=$(command -v uv || true)
  if [ -z "$uv_bin" ]; then
    curl -LsSf https://astral.sh/uv/install.sh -o /opt/prt-cc-openai-bridge/install-uv.sh
    UV_INSTALL_DIR=/opt/prt-cc-openai-bridge/uv sh /opt/prt-cc-openai-bridge/install-uv.sh
    uv_bin=/opt/prt-cc-openai-bridge/uv/uv
  fi
  "$uv_bin" venv --clear --python 3.12 /opt/prt-cc-openai-bridge/venv
  "$uv_bin" pip install --python /opt/prt-cc-openai-bridge/venv/bin/python --no-cache /opt/prt-cc-openai-bridge/source
fi
/opt/prt-cc-openai-bridge/venv/bin/python -c 'import cc_openai_bridge; from importlib.metadata import version; assert version("litellm") == "1.105.1"'
'''

READY = r'''
import json,sys,time,urllib.request
from pathlib import Path
path=Path(sys.argv[1])
for _ in range(150):
    try:
        state=json.loads(path.read_text())
        url='http://127.0.0.1:'+str(state['port'])
        with urllib.request.urlopen(url+'/health',timeout=1) as response:
            if json.load(response).get('status')=='ok':
                print(json.dumps({'url':url})); raise SystemExit(0)
    except (OSError,ValueError,KeyError):
        time.sleep(.2)
raise SystemExit('Bridge failed to become ready; see agent/openai-bridge.txt')
'''

STOP = r'''
import json,os,signal,sys,time
from pathlib import Path
path=Path(sys.argv[1])
try:
    state=json.loads(path.read_text()); pid=int(state['pid']); start=str(state['start'])
except (OSError,ValueError,KeyError):
    raise SystemExit(0)
def same():
    try: return pid>1 and Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]==start
    except OSError: return False
for sig in (signal.SIGTERM,signal.SIGKILL):
    if not same(): break
    try: os.kill(pid,sig)
    except ProcessLookupError: break
    if sig==signal.SIGTERM: time.sleep(1)
path.unlink(missing_ok=True)
'''


def cache_key(model, task):
    # 同一模型 + 同一题目的 Trial 共用上游缓存分片；默认的按会话键会把每个 Trial 打散到不同分片
    return 'prt-' + hashlib.sha256(f'{model}\n{task}'.encode()).hexdigest()[:32]


def enabled(agent):
    return agent._get_env(PREFIX + 'ENABLED') == '1'


async def checked(environment, command, **kwargs):
    result = await environment.exec(command=command, user='root', **kwargs)
    if result.return_code != 0:
        raise RuntimeError('Claude Code OpenAI Bridge 初始化失败；请检查 Agent setup 或 openai-bridge 日志')
    return result


def bridge_setup(original):
    @functools.wraps(original)
    async def setup(self, environment):
        await original(self, environment)
        if not enabled(self):
            return
        bundle = Path(__file__).parent / 'cc-openai-bridge'
        await checked(environment, 'mkdir -p ' + ROOT + '/source')
        await environment.upload_dir(bundle, ROOT + '/source')
        result = await environment.exec(command=INSTALL, user='root', timeout_sec=600)
        (self.logs_dir / 'setup' / 'openai-bridge.txt').write_text(
            (result.stdout or '') + (result.stderr or ''), encoding='utf-8')
        if result.return_code != 0:
            raise RuntimeError('Bridge 依赖安装失败，请查看 Agent setup/openai-bridge.txt；需要联网安装独立的 Python 3.12 环境')
    setup._prt_cc_bridge = True
    return setup


def bridge_auth(original):
    @functools.wraps(original)
    def auth(self):
        if not enabled(self):
            return original(self)
        url, key = self._prt_bridge_connection
        return {'ANTHROPIC_BASE_URL': url, 'ANTHROPIC_API_KEY': key,
                'ANTHROPIC_AUTH_TOKEN': '', 'CLAUDE_CODE_OAUTH_TOKEN': '',
                'ANTHROPIC_SMALL_FAST_MODEL': self.model_name,
                # 首行归因头带每个提示的内容哈希，会让上游前缀缓存跨 Trial 全部失效
                'CLAUDE_CODE_ATTRIBUTION_HEADER': '0',
                'CLAUDE_CODE_USE_BEDROCK': '0', 'CLAUDE_CODE_USE_VERTEX': '0'}
    return auth


def bridge_run(original):
    @functools.wraps(original)
    async def run(self, instruction, environment, context):
        if not enabled(self):
            return await original(self, instruction, environment, context)
        state = '/tmp/prt-cc-bridge-' + uuid.uuid4().hex + '.json'
        key = secrets.token_urlsafe(32)
        env = {'UPSTREAM_BASE_URL': self._get_env(PREFIX + 'UPSTREAM_BASE_URL'),
               'UPSTREAM_API_KEY': self._get_env(PREFIX + 'UPSTREAM_API_KEY'),
               'BRIDGE_API_KEY': key, 'BRIDGE_ALLOWED_MODELS': self.model_name,
               'BRIDGE_PROMPT_CACHE_KEY': cache_key(self.model_name, getattr(environment, 'environment_name', '')),
               'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}
        logfile = str(self.environment_logs_dir / 'openai-bridge.txt')
        start = f'nohup {PYTHON} {ROOT}/source/prt_bridge_server.py {shlex.quote(state)} > {shlex.quote(logfile)} 2>&1 < /dev/null &'
        try:
            await checked(environment, start, env=env, timeout_sec=10)
            ready = await checked(environment, f'{PYTHON} -c {shlex.quote(READY)} {shlex.quote(state)}', timeout_sec=45)
            self._prt_bridge_connection = (json.loads(ready.stdout)['url'], key)
            # --agent-env 里的占位 ANTHROPIC_* 是 Trial 的 scoped env，优先级高于每次 exec 的 env；
            # 不再叠一层 scoped env，Claude Code 会连到占位端口（ConnectionRefused）。
            scoped = getattr(environment, 'scoped_exec_env', None)
            with scoped(bridge_auth(lambda _: {})(self)) if scoped else contextlib.nullcontext():
                return await original(self, instruction, environment, context)
        finally:
            cleanup = asyncio.create_task(environment.exec(
                command=f'{PYTHON} -c {shlex.quote(STOP)} {shlex.quote(state)}', user='root', timeout_sec=8))
            try:
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=10)
            except (Exception, asyncio.CancelledError):
                cleanup.cancel()
                self.logger.warning('PRT: Bridge 清理未完成，将由 Harbor 容器回收流程继续清理')
    return run


def install():
    from harbor.agents.installed.claude_code import ClaudeCode
    if getattr(ClaudeCode.setup, '_prt_cc_bridge', False):
        return
    ClaudeCode.setup = bridge_setup(ClaudeCode.setup)
    ClaudeCode.run = bridge_run(ClaudeCode.run)
    ClaudeCode._resolve_auth_env = bridge_auth(ClaudeCode._resolve_auth_env)
