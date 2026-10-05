"""pytest guard: tests may use temporary Unix sockets, never IP networking.

Loaded only by check_offline.py. A swallowed transport exception cannot hide
an attempted network call: any attempt also makes the test session fail.
"""

import socket
import os

_attempts = []
_originals = {}


def _deny(*args, **kwargs):
    _attempts.append(os.environ.get("PYTEST_CURRENT_TEST", "collection"))
    raise RuntimeError("IP/DNS networking is disabled in offline tests")


def pytest_configure(config):
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        original = getattr(socket.socket, name, None)
        if original is None:
            continue
        _originals[name] = original
        def guarded(sock, *args, _original=original, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                return _deny()
            return _original(sock, *args, **kwargs)
        setattr(socket.socket, name, guarded)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        _originals[name] = getattr(socket, name)
        setattr(socket, name, _deny)


def pytest_sessionfinish(session, exitstatus):
    if _attempts:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _attempts:
        terminalreporter.write_line(f"FAILED: {len(_attempts)} blocked IP/DNS network attempt(s)")
        for test in sorted(set(_attempts)):
            terminalreporter.write_line(f"  {test}")
    else:
        terminalreporter.write_line("Offline guard: no IP/DNS network attempts")


def pytest_unconfigure(config):
    for name, original in _originals.items():
        target = socket.socket if name in ("connect", "connect_ex", "sendto", "sendmsg") else socket
        setattr(target, name, original)
