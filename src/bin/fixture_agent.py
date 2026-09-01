#!/usr/bin/env python3
"""Fixture coding agent for the Pi Sandbox Worker container (test only).

Behaviours are selected by a #SIM-BEHAVIOR marker placed by the test harness.
Also performs the container isolation probe battery and writes probes.json
to /jobs so the bridge can attach it to the evidence.
"""

import json
import os
import sys
from pathlib import Path


def probes():
    r = {}

    def try_write(p):
        try:
            Path(p).write_text("probe")
            try:
                os.remove(p)
            except OSError:
                pass
            return True
        except OSError:
            return False

    r["write_workspace"] = try_write("/workspace/.probe-write")
    r["write_tmp"] = try_write("/tmp/.probe-write")
    r["write_agent_home"] = try_write("/agent-home/.probe-write")
    r["write_etc_expected_fail"] = try_write("/etc/.probe-write")
    r["write_usr_expected_fail"] = try_write("/usr/.probe-write")
    r["write_root_expected_fail"] = try_write("/root/.probe-write")
    r["write_var_log_expected_fail"] = try_write("/var/log/.probe-write")

    r["absent_opt_data"] = not os.path.exists("/opt/data")
    r["absent_srv_hermes"] = not os.path.exists("/srv/hermes")
    r["absent_srv_hermes_worker"] = not os.path.exists("/srv/hermes-worker")
    r["absent_home_<PI_USER>"] = not os.path.exists("/home/<PI_USER>")
    r["absent_docker_sock"] = not os.path.exists("/var/run/docker.sock")

    # symlink escape: link pointing into /etc must fail to write through
    link = Path("/workspace/.probe-link")
    try:
        if link.is_symlink() or link.exists():
            link.unlink(missing_ok=True)
        link.symlink_to("/etc")
        r["symlink_escape_blocked"] = not try_write("/workspace/.probe-link/.probe-write")
    except OSError:
        r["symlink_escape_blocked"] = True
    finally:
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass
    return r


def main():
    prompt_path = sys.argv[1] if len(sys.argv) > 1 else "/jobs/prompt.txt"
    prompt = Path(prompt_path).read_text(encoding="utf-8") if Path(prompt_path).exists() else ""
    behavior = "no_progress"
    for line in prompt.splitlines():
        if line.startswith("#SIM-BEHAVIOR:"):
            behavior = line.split(":", 1)[1].strip()
            break

    # isolation probe battery (always, for every fixture run)
    pr = probes()
    try:
        Path("/jobs/probes.json").write_text(json.dumps(pr, indent=1))
    except OSError:
        pass

    greeter = Path("/workspace/pkg/greeter.py")

    def write(body):
        greeter.parent.mkdir(parents=True, exist_ok=True)
        greeter.write_text(body, encoding="utf-8")

    if behavior == "full":
        body = greeter.read_text() if greeter.exists() else ""
        out = ""
        if "def greet" not in body:
            out += 'def greet(name):\n    return f"Hello, {name}!"\n'
        if "def farewell" not in body:
            out += ('\n\n' if out and not out.endswith("\n\n") else "") + \
                'def farewell(name):\n    return f"Goodbye, {name}!"\n'
        write(body + out)
        print("Done. Everything works.")
        return 0
    if behavior == "partial_then_done":
        body = greeter.read_text() if greeter.exists() else ""
        if "def greet" not in body:
            write('def greet(name):\n    return f"Hello, {name}!"\n')
        elif "def farewell" not in body:
            write(body + '\n\ndef farewell(name):\n    return f"Goodbye, {name}!"\n')
        print("Done. Everything works.")
        return 0
    if behavior == "false_completion":
        write('def greet(name):\n    return "HOWDY"\n\n\ndef farewell(name):\n    return "CYA"\n')
        print("Done. Implemented. Finished. Everything works.")
        return 0
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
