"""Keep the loopback CLI endpoint and its restricted SSH reverse tunnel alive."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('cli_path'):
        os.environ['PATH'] = config['cli_path']
    if config.get('codex_home'):
        os.environ['CODEX_HOME'] = config['codex_home']
    port = int(config.get('port', 18766))
    if not 1024 <= port <= 65535:
        raise ValueError('Invalid port')
    target = config['ssh_target']
    if not isinstance(target, str) or target.startswith('-') or any(c.isspace() for c in target):
        raise ValueError('Invalid SSH target')
    app = Path(__file__).resolve().parent
    lock = (config_path.parent / 'supervisor.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0)
        if lock.read(1) == b'':
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (config_path.parent / 'supervisor.pid').write_text(str(os.getpid()), encoding='ascii')
    commands = {
        'worker': [sys.executable, str(app / 'premium_server.py'), '--port', str(port),
                   '--token-file', str(config_path.parent / 'bridge.token')],
        'tunnel': ['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                   '-R', f'127.0.0.1:{port}:127.0.0.1:{port}', target],
    }
    children = {}
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    creation = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    try:
        while not stopped:
            for name, command in commands.items():
                if name not in children or children[name].poll() is not None:
                    children[name] = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL, cwd=app, **creation)
            state = {name: 'running' if proc.poll() is None else 'stopped' for name, proc in children.items()}
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            temp = config_path.parent / 'status.tmp'
            temp.write_text(json.dumps(state), encoding='utf-8')
            os.replace(temp, config_path.parent / 'status.json')
            time.sleep(5)
    finally:
        for proc in children.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in children.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        lock.close()


if __name__ == '__main__':
    main()
