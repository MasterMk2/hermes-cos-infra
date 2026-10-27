# Handoff route verification — not an application

This specification exists to prove that the chat → broker → supervisor →
worker → review path works end to end on real infrastructure, using a
repository and a toolchain that already exist.

**It is not FastRawPhotoView, and passing it says nothing about
FastRawPhotoView.** A green run here means the handoff protocol is sound: an
owner's explicit authorization in a channel produced a durable job id, the
supervisor dispatched it, the worker container ran an isolated agent, the
registered tests executed, deterministic acceptance was evaluated and the
independent review gate ran. Application readiness is reported separately and
has its own prerequisites.

## Repository

`coding-worker-smoke-test`, already registered in both the supervisor and the
coding bridge allowlists, with `python3 -m unittest discover -s tests -v` as
its registered test command. Write scope is `pkg/`, `tests/` and `README.md`.

## Task

In `pkg/`, add a module `handoff_probe.py` exposing:

```python
def route_signature(job_id: str, revision: int) -> str
```

It returns a stable, lower-case hex digest of `f"{job_id}:{revision}"` using
SHA-256, truncated to 16 characters. The same inputs must always produce the
same output, and a different `job_id` or `revision` must produce a different
one.

In `tests/`, add `test_handoff_probe.py` covering: stability across repeated
calls, sensitivity to each argument independently, the exact output length, and
that the result contains only `0-9a-f`.

Use the standard library only. The worker container has `python3` and `git`,
no network, and no package installation is possible.

## Acceptance

- `pkg/handoff_probe.py` exists and defines `route_signature`.
- The registered test command passes.
- All changes stay inside the registered write scope.

## Explicitly not covered

RAW decoding, transports, GUI, Rust, Node, licence review, or anything else
belonging to the product. Do not add them here.
