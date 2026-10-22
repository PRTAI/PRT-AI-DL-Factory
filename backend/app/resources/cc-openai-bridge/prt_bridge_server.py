"""Bind an unused loopback port before publishing process state; keys stay in env."""
import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path


def main():
    path = Path(sys.argv[1])
    start = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    state = {'pid': os.getpid(), 'start': start}
    path.write_text(json.dumps(state), encoding='utf-8')
    path.chmod(0o600)
    # Imports may be slow on the first run. Cleanup can already identify this PID.
    import uvicorn
    from cc_openai_bridge.app import create_app
    from cc_openai_bridge.settings import Settings
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        settings = replace(Settings.from_env(), port=sock.getsockname()[1], host='127.0.0.1')
        state['port'] = settings.port
        path.write_text(json.dumps(state), encoding='utf-8')
        server = uvicorn.Server(uvicorn.Config(create_app(settings), access_log=False, log_level='warning'))
        server.run(sockets=[sock])


if __name__ == '__main__':
    main()
