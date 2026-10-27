#!/usr/bin/env python3
"""Run the same offline suites locally and in CI; never install or send."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--without-unix-sockets", action="store_true",
                        help="explicit partial run; omitted socket tests yield exit code 2")
    args = parser.parse_args()
    print(f"Runtime: Python {sys.version.split()[0]} on {sys.platform}", flush=True)
    failed = False
    for suite, module_path in (
            ("tests-gateway", "src/channel-gateway/app"),
            ("tests-attention", "src/attention/app"),
            ("tests-planning", "src"),
            ("tests-calendar-tentative", "src/calendar-tentative/app")):
        env = os.environ.copy()
        # Explicit paths, no user/site pytest plugins or inherited pytest flags.
        env["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in
                                         ("scripts", "src", module_path, suite))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env.pop("PYTEST_ADDOPTS", None)
        command = [sys.executable, "-m", "pytest", "-p", "no:logging",
                   "-p", "offline_guard", suite, "-q", "-ra"]
        if args.without_unix_sockets and suite == "tests-gateway":
            command.append("--ignore=tests-gateway/test_proactive_socket.py")
        print(f"\nCHECK {suite}", flush=True)
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        print(f"{'PASS' if result.returncode == 0 else 'FAILED'} {suite}", flush=True)
        failed |= result.returncode != 0
    if args.without_unix_sockets:
        print("UNVERIFIED: tests-gateway/test_proactive_socket.py was explicitly omitted; "
              "this is not a full check", flush=True)
    return 1 if failed else 2 if args.without_unix_sockets else 0


if __name__ == "__main__":
    raise SystemExit(main())
