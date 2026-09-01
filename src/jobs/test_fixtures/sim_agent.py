#!/usr/bin/env python3
"""Deterministic simulated coding agent — TEST FIXTURE ONLY.

Used exclusively by the supervisor E2E tests against repos with
allow_simulated=true. Behaviour is selected by a #SIM-BEHAVIOR marker that
only the test harness places in the prompt.

It deliberately self-reports success even when work is incomplete, so the
supervisor's "agent stop != completion" behaviour can be tested.
"""

import sys
from pathlib import Path


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    behavior = "no_progress"
    for line in prompt.splitlines():
        if line.startswith("#SIM-BEHAVIOR:"):
            behavior = line.split(":", 1)[1].strip()
            break

    greeter = Path("pkg/greeter.py")
    test_file = Path("tests/test_greeter.py")

    if behavior == "partial_then_done":
        # Attempt 1: implement greet() only (self-report success anyway).
        # Attempt 2: implement farewell() so the tests pass.
        body = greeter.read_text(encoding="utf-8") if greeter.exists() else ""
        greeter.parent.mkdir(parents=True, exist_ok=True)
        if "def greet" not in body:
            greeter.write_text(
                body + 'def greet(name):\n    return f"Hello, {name}!"\n',
                encoding="utf-8",
            )
        elif "def farewell" not in body:
            greeter.write_text(
                body + '\n\ndef farewell(name):\n    return f"Goodbye, {name}!"\n',
                encoding="utf-8",
            )
        print("Done. Everything works.")
        return 0

    if behavior == "false_completion":
        # Deliberately wrong implementation + confident self-report.
        greeter.parent.mkdir(parents=True, exist_ok=True)
        greeter.write_text(
            'def greet(name):\n    return "HOWDY"\n\n\ndef farewell(name):\n    return "CYA"\n',
            encoding="utf-8",
        )
        print("Done. Implemented. Finished. Everything works.")
        return 0

    # no_progress: touch nothing, claim success.
    assert not test_file.exists() or True
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
