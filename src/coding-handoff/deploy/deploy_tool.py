#!/usr/bin/env python3
"""Phase J.1 rollout — the structured half of the installer.

Every step that needs to parse JSON, render a template or edit a YAML file
lives here rather than inside a shell heredoc. That is a house rule with a
reason behind it: this repository is edited on Windows and deployed to Linux,
and a Python body embedded in a shell script carries backslashes and `\\n`
literals through two levels of quoting on the way. A file that is executed is
a file that can be linted, tested and reviewed.

Subcommands (all read-only unless stated):

    manifest-files <manifest> [component]   TSV of what to install
    manifest-drift <manifest>               TSV of what must not change
    render-unit <template> <out> k=v ...    write a systemd unit  (WRITES)
    render-config <out> k=v ...             write handoff.json    (WRITES)
    check-registry <app-dir> <registry>     parse the project registry
    enable-gateway-route <config> <endpoint>  add the config section (WRITES)
    self-check <manifest> <root>            payload hashes still match

Nothing here prints a credential, and nothing here contacts a network.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

# What `render-config` will accept. An installer that could set an arbitrary
# key here would be a way to reconfigure the broker without review.
CONFIG_KEYS = (
    "listen_host", "listen_port", "token_file", "registry_file", "jobs_dir",
    "bridge_config", "bridge_socket", "ledger_db", "available_toolchains",
    "dispatch_enabled",
)

# What `render-unit` will substitute. Unknown placeholders are an error, and a
# placeholder left unsubstituted is an error, so a renamed field fails loudly
# instead of shipping a unit with `@JOBS_DIR@` in it.
UNIT_KEYS = (
    "SERVICE_USER", "SERVICE_GROUP", "STATE_DIR", "APP_DIR", "CONFIG_DIR",
    "CONFIG_FILE", "SPEC_DIR", "JOBS_DIR", "BRIDGE_SOCKET",
    "BRIDGE_SOCKET_DIR", "BRIDGE_CONFIG", "GATEWAY_DIR", "JOBS_PARENT",
    "REPO_BINDS",
)

#: The one placeholder whose value is a rendered block rather than a path.
MULTILINE_UNIT_KEYS = frozenset({"REPO_BINDS"})

#: A bind target this shallow would expose most of a home directory or the
#: whole filesystem. Below this depth the installer binds each repository
#: individually instead.
MIN_BIND_DEPTH = 3

# --------------------------------------------------------- job-layer patches
#
# The deployed job layer is logically identical to the committed one but is not
# byte-identical: it uses CRLF, and `transport.py` carries the operator's own
# resolved bridge path where the repository carries a placeholder. That real
# path must never enter this repository, and re-installing the placeholder over
# it would break the existing container consumers that rely on the fallback.
#
# So these two files are not copied. They are transformed in place: the live
# bytes are pinned by hash, an anchored region is rewritten, and everything
# outside that region — line endings and the operator's path included — is
# asserted to be byte-identical afterwards.
#
# The anchors below are the committed text. `tests-handoff/test_job_patches.py`
# applies each transformation to the committed file and requires the result to
# equal the repository's own edited copy, so the deployed change and the
# reviewed change cannot drift apart.

JOB_PATCHES = {
    "create-job-id": (
        (
            "import json\nimport secrets\nimport sqlite3\n",
            "import json\nimport re\nimport secrets\nimport sqlite3\n",
        ),
        (
            "def new_id(prefix):\n",
            "#: The shape the supervisor, the bridge and the worker all validate.\n"
            'JOB_ID_RE = re.compile(r"^job_[0-9a-f]{10}$")\n'
            "\n"
            "\n"
            "def new_id(prefix):\n",
        ),
        (
            "def create_job(goal, repository, acceptance_criteria, agent=None,"
            " project_id=None,\n"
            '               task_id=None, parent_job_id=None, kind="implementation",\n'
            '               priority="normal", max_attempts=6, tests_command=None):\n'
            "    con = connect()\n"
            "    try:\n"
            '        jid = new_id("job")\n',

            "def create_job(goal, repository, acceptance_criteria, agent=None,"
            " project_id=None,\n"
            '               task_id=None, parent_job_id=None, kind="implementation",\n'
            '               priority="normal", max_attempts=6, tests_command=None,\n'
            "               job_id=None):\n"
            '    """Create one job. `job_id` lets a caller supply a *deterministic* id.\n'
            "\n"
            "    J.1: the handoff broker derives a job id from the owner's authorization and\n"
            "    records it before creating anything, so that a crash between creation and\n"
            "    bookkeeping is recoverable. Passing it here makes `jobs.job_id` — a primary\n"
            "    key — the thing that enforces exactly-once, rather than the broker's own\n"
            "    bookkeeping. A duplicate raises `sqlite3.IntegrityError`, which the caller\n"
            '    is expected to read as "already created" and resolve with `get_job`.\n'
            "\n"
            "    Omitting it keeps the original behaviour: a fresh random id.\n"
            '    """\n'
            "    con = connect()\n"
            "    try:\n"
            '        jid = job_id or new_id("job")\n'
            "        if job_id is not None and not JOB_ID_RE.match(str(job_id)):\n"
            '            raise ValueError(f"job_id must match {JOB_ID_RE.pattern}")\n',
        ),
    ),
    "bridge-sock-env": (
        (
            # The anchor starts at the blank line after the candidate list, so
            # the inserted block lands with the same spacing the repository's
            # own copy has — and so the list itself, which on a deployed host
            # holds a resolved operator path, is never part of the match.
            "\n\ndef bridge_sock():\n"
            "    for p in BRIDGE_SOCK_CANDIDATES:\n",

            "\n"
            "#: J.1: an explicit override, for a supervisor running outside the Hermes\n"
            "#: container. The host fallback above is an operator-specific literal and\n"
            "#: cannot be relied on; a process that knows where the socket is should say so\n"
            "#: rather than have this list guess. Unset — which is every existing\n"
            "#: deployment — changes nothing.\n"
            'BRIDGE_SOCK_ENV = "HERMES_CODING_BRIDGE_SOCK"\n'
            "\n"
            "\n"
            "def bridge_sock():\n"
            "    override = os.environ.get(BRIDGE_SOCK_ENV)\n"
            "    if override:\n"
            "        if not os.path.exists(override):\n"
            "            raise WorkerUnconfigured(\n"
            '                f"{BRIDGE_SOCK_ENV} points at {override!r}, which does not exist"\n'
            "            )\n"
            "        return override\n"
            "    for p in BRIDGE_SOCK_CANDIDATES:\n",
        ),
    ),
}


def detect_newline(raw: bytes) -> str:
    return "\r\n" if raw.count(b"\r\n") > raw.count(b"\n") - raw.count(b"\r\n") \
        else "\n"


def apply_job_patch(kind: str, raw: bytes) -> bytes:
    """Rewrite the anchored regions and nothing else.

    Works on an LF view so the anchors are written once, then restores the
    file's own line-ending style. A file that already carries the change is
    returned unchanged, so the operation is idempotent.
    """
    patches = JOB_PATCHES.get(kind)
    if patches is None:
        fail(f"unknown patch {kind!r}; known: {sorted(JOB_PATCHES)}")
    newline = detect_newline(raw)
    text = raw.decode("utf-8").replace("\r\n", "\n")
    for old, new in patches:
        if new in text:
            continue           # already applied
        if text.count(old) != 1:
            fail(f"{kind}: anchor not found exactly once "
                 f"(found {text.count(old)})")
        text = text.replace(old, new, 1)
    return text.replace("\n", newline).encode("utf-8")


def unapply_job_patch(kind: str, raw: bytes) -> bytes:
    """The inverse transformation, used only to *recognise* an applied patch.

    Reversing is how "already installed" is told apart from "content this
    package has never seen". Comparing the reversal against the recorded live
    hash is exact: an unrelated edit does not reverse into it.
    """
    patches = JOB_PATCHES.get(kind)
    if patches is None:
        fail(f"unknown patch {kind!r}")
    newline = detect_newline(raw)
    text = raw.decode("utf-8").replace("\r\n", "\n")
    for old, new in reversed(patches):
        if new not in text:
            return raw
        text = text.replace(new, old, 1)
    return text.replace("\n", newline).encode("utf-8")


def job_patch_state(kind: str, raw: bytes, live_sha: str) -> str:
    """`fresh` | `applied` | `unknown` for one job-layer target."""
    actual = hashlib.sha256(raw).hexdigest()
    if actual == live_sha:
        return "fresh"
    try:
        reversed_bytes = unapply_job_patch(kind, raw)
    except (UnicodeDecodeError, ValueError):
        return "unknown"
    if hashlib.sha256(reversed_bytes).hexdigest() == live_sha:
        return "applied"
    return "unknown"


def cmd_job_patch_state(kind: str, source: str, live_sha: str) -> None:
    path = Path(source)
    if not path.is_file():
        print("absent")
        return
    print(job_patch_state(kind, path.read_bytes(), live_sha))


def cmd_patch_jobs(kind: str, source: str, out: str, expect_sha: str) -> None:
    raw = Path(source).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if expect_sha != "-":
        state = job_patch_state(kind, raw, expect_sha)
        if state == "unknown":
            fail(f"{source} is {actual}, which is neither the recorded live "
                 f"content {expect_sha} nor that content with this patch "
                 "already applied; refusing to patch")
        if state == "applied":
            # Re-running the installer to flip a flag must not fail because the
            # first run already did this. Exactly the documented two-stage
            # rollout depends on it.
            print(f"{actual}\t{source}\talready-applied")
            return
    patched = apply_job_patch(kind, raw)

    # Prove the transformation touched only what it claims to. Every line that
    # is not part of an anchor must survive verbatim — which is how the
    # operator's own resolved path is preserved without this package ever
    # seeing it.
    before = raw.decode("utf-8").replace("\r\n", "\n").splitlines()
    after = patched.decode("utf-8").replace("\r\n", "\n").splitlines()
    removed = [line for line in before if line not in after]
    allowed = set()
    for old, new in JOB_PATCHES[kind]:
        allowed.update(l for l in old.split("\n") if l and l not in new)
    unexpected = [l for l in removed if l not in allowed]
    if unexpected:
        fail(f"{kind}: {len(unexpected)} line(s) would be lost, refusing")
    if detect_newline(raw) != detect_newline(patched):
        fail(f"{kind}: line endings would change")

    Path(out).write_bytes(patched)
    print(f"{hashlib.sha256(patched).hexdigest()}\t{out}")


GATEWAY_SECTION = """
# Phase J.1. The gateway answers an owner's explicit request to start
# registered coding work itself, through a loopback broker, instead of asking a
# read-only assistant to do something it has no tool for.
coding_handoff:
  enabled: true
  endpoint: {endpoint}
  timeout_seconds: 20
  proposal_ttl_seconds: 1800
  projects_cache_seconds: 120
"""


def fail(message: str) -> None:
    sys.stderr.write(f"deploy_tool: {message}\n")
    raise SystemExit(2)


def load_manifest(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read manifest {path}: {exc}")


def parse_pairs(args) -> dict:
    values = {}
    for arg in args:
        if "=" not in arg:
            fail(f"expected key=value, got {arg!r}")
        key, _, value = arg.partition("=")
        values[key.strip()] = value
    return values


def cmd_manifest_files(manifest_path: str, component: str = "all") -> None:
    manifest = load_manifest(manifest_path)
    for name, entry in manifest["components"].items():
        if component not in ("all", name):
            continue
        for item in entry["files"]:
            print("\t".join([
                item["source"], item["target"], item["mode"],
                item["payload_sha256"], item["baseline_sha256"] or "-",
            ]))


def cmd_manifest_drift(manifest_path: str) -> None:
    for entry in load_manifest(manifest_path).get("unmodified_drift", []):
        print("\t".join([entry["path"], entry["deployed_sha256"],
                         entry["note"]]))


def cmd_manifest_patches(manifest_path: str) -> None:
    for entry in load_manifest(manifest_path).get("job_patches", []):
        print("\t".join([entry["kind"], entry["target"], entry["live_sha256"],
                         entry["note"]]))


def cmd_self_check(manifest_path: str, root: str) -> None:
    manifest = load_manifest(manifest_path)
    problems = 0
    for entry in manifest["components"].values():
        for item in entry["files"]:
            path = Path(root) / item["source"]
            if not path.is_file():
                print(f"MISSING\t{item['source']}")
                problems += 1
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["payload_sha256"]:
                print(f"CHANGED\t{item['source']}")
                problems += 1
    print(f"checked\t{sum(len(c['files']) for c in manifest['components'].values())}")
    raise SystemExit(1 if problems else 0)


def _posix_common_parent(paths) -> str:
    """The deepest directory containing every path, in POSIX terms.

    `os.path.commonpath` follows the *host's* flavour, so on a Windows
    development machine it returns backslash-separated nonsense for the Linux
    paths this tool reasons about. The target is always POSIX, so the
    comparison is too.
    """
    parts = [PurePosixPath(p).parts for p in paths]
    common = []
    for chunk in zip(*parts):
        if len(set(chunk)) != 1:
            break
        common.append(chunk[0])
    return str(PurePosixPath(*common)) if common else "/"


def bridge_repo_binds(bridge_config: str) -> list:
    """Directories the broker must be able to read to observe repositories.

    `ProtectHome=tmpfs` hides the operator's home, and on this host the coding
    bridge's repositories live under it. Binding only the socket directory and
    the bridge config — which an earlier draft did — leaves every registered
    repository invisible, so `git_has_head` reports every project as missing
    and nothing can ever be dispatched.

    Prefer the common parent of the registered repositories, so a repository
    onboarded *after* the unit was rendered is visible without re-rendering.
    Fall back to binding each one individually when that parent would be too
    shallow to be a safe bind target.
    """
    data = json.loads(Path(bridge_config).read_text(encoding="utf-8"))
    paths = sorted({
        str(entry.get("path")) for entry in
        (data.get("repositories") or {}).values() if entry.get("path")
    })
    if not paths:
        return []
    if len(paths) == 1:
        candidate = str(PurePosixPath(paths[0]).parent)
    else:
        candidate = _posix_common_parent(paths)
    depth = len([p for p in PurePosixPath(candidate).parts if p != "/"])
    if depth < MIN_BIND_DEPTH or candidate in ("/", "/home", "/root", "/srv"):
        return paths
    return [candidate]


def cmd_bridge_status(socket_path: str) -> None:
    """Print `idle`, `busy:<what>` or `unreachable` for the coding bridge.

    Read-only: one `worker_status` request, which the bridge answers without
    touching any job. The installer uses it to refuse while work is running.

    Why it matters: `supervisor._spawn_runner` uses `start_new_session=True`,
    which gives the runner its own session but leaves it in the *same cgroup*
    as whatever started it. A `systemctl restart` of this broker therefore
    kills a supervisor child it spawned, while the bridge's container keeps
    going. That is recoverable — `supervisor.reconcile()` moves a RUNNING job
    whose runner is gone to AGENT_STOPPED, and the bridge re-adopts a live
    container on the next `job_start` — but recovering from an avoidable
    interruption is not the same as not causing one.
    """
    import socket as _socket

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        sock.connect(socket_path)
        request = json.dumps({"version": 1, "action": "worker_status"}) + "\n"
        sock.sendall(request.encode("utf-8"))
        with sock.makefile("r") as handle:
            line = handle.readline()
    except (OSError, ValueError):
        print("unreachable")
        return
    finally:
        sock.close()
    try:
        data = json.loads(line) if line.strip() else {}
    except ValueError:
        print("unreachable")
        return
    active = data.get("active_job") or data.get("active_review")
    print(f"busy:{active}" if active else "idle")


def cmd_read_health_flag(name: str) -> None:
    """Print one boolean from the broker's /health JSON, read on stdin.

    Used to compare what the *running* process reports against what the
    installer was asked for; a config file rewritten without a restart would
    otherwise pass a health check that only proves the port answers.
    """
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        print("unreadable")
        return
    value = data.get(name)
    print("true" if value is True else "false" if value is False else "unknown")


def cmd_bridge_repo_binds(bridge_config: str) -> None:
    for path in bridge_repo_binds(bridge_config):
        print(path)


def cmd_render_unit(template: str, out: str, pairs) -> None:
    values = parse_pairs(pairs)
    unknown = set(values) - set(UNIT_KEYS)
    if unknown:
        fail(f"unknown unit placeholders: {sorted(unknown)}")
    missing = set(UNIT_KEYS) - set(values)
    if missing:
        fail(f"missing unit placeholders: {sorted(missing)}")
    for key, value in values.items():
        if not value.strip():
            fail(f"{key} is empty")
        if ("\n" in value or "\r" in value) and key not in MULTILINE_UNIT_KEYS:
            fail(f"{key} contains a newline")
    text = Path(template).read_text(encoding="utf-8")
    for key in UNIT_KEYS:
        text = text.replace(f"@{key}@", values[key])
    if "@" in text and any(f"@{k}@" in text for k in UNIT_KEYS):
        fail("a placeholder was left unsubstituted")
    # LF only: a systemd unit with CRLF fails in a way that reads as a syntax
    # error rather than as an encoding problem.
    Path(out).write_text(text, encoding="utf-8", newline="\n")
    print(out)


def cmd_render_config(out: str, pairs) -> None:
    values = parse_pairs(pairs)
    unknown = set(values) - set(CONFIG_KEYS)
    if unknown:
        fail(f"unknown config keys: {sorted(unknown)}")
    config = {
        "listen_host": values.get("listen_host", "127.0.0.1"),
        "listen_port": int(values.get("listen_port", 18771)),
        "token_file": values["token_file"],
        "registry_file": values["registry_file"],
        "jobs_dir": values["jobs_dir"],
        "bridge_config": values["bridge_config"],
        "bridge_socket": values["bridge_socket"],
        "ledger_db": values["ledger_db"],
        # Measured on the host, not guessed: the worker image provides these
        # and nothing else. Widening it is a claim about the image.
        "available_toolchains": [t for t in
                                 values.get("available_toolchains",
                                            "python3,git").split(",") if t],
        "dispatch_enabled": values.get("dispatch_enabled", "0") == "1",
    }
    for key in ("token_file", "registry_file", "jobs_dir", "bridge_config",
                "bridge_socket", "ledger_db"):
        if not str(config[key]).startswith("/"):
            fail(f"{key} must be an absolute path, got {config[key]!r}")
    Path(out).write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    print(out)


def cmd_check_registry(app_dir: str, registry: str) -> None:
    sys.path.insert(0, app_dir)
    try:
        import handoff_registry
    except ImportError as exc:
        fail(f"cannot import the broker registry module: {exc}")
    try:
        parsed = handoff_registry.load_registry(registry)
    except handoff_registry.RegistryError as exc:
        fail(str(exc))
    enabled = parsed.enabled_refs()
    print(f"projects\t{len(parsed.projects)}\tenabled\t{len(enabled)}")


_ENABLED_FALSE_RE = re.compile(r"^(\s+enabled:\s*)(false|no|off)\s*$",
                               re.IGNORECASE | re.MULTILINE)


def cmd_enable_gateway_route(config_path: str, endpoint: str) -> None:
    path = Path(config_path)
    text = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines)
               if re.match(r"^coding_handoff\s*:", line)]
    if headers:
        if len(headers) != 1:
            fail("coding_handoff must occur exactly once at the top level")
        start = headers[0]
        if not re.fullmatch(r"coding_handoff\s*:[ \t]*(?:#[^\r\n]*)?", lines[start].rstrip("\r\n")):
            fail("coding_handoff must use a block mapping")
        end = next((i for i in range(start + 1, len(lines))
                    if re.match(r"^[^\s#]", lines[i])), len(lines))
        children = [line for line in lines[start + 1:end]
                    if line.strip() and not line.lstrip().startswith("#")]
        indent = min((len(line) - len(line.lstrip(" ")) for line in children), default=2)
        enabled = []
        for i in range(start + 1, end):
            line = lines[i]
            if len(line) - len(line.lstrip(" ")) == indent and re.match(r"^\s+enabled\s*:", line):
                enabled.append(i)
        if len(enabled) > 1:
            fail("coding_handoff.enabled must not be duplicated")
        if enabled:
            i = enabled[0]
            match = re.fullmatch(r"([ ]+enabled\s*:[ \t]*)(true|false|yes|no|on|off)([ \t]*(?:#[^\r\n]*)?)(\r?\n)?", lines[i], re.IGNORECASE)
            if not match:
                fail("coding_handoff.enabled must be a boolean")
            if match.group(2).lower() in ("true", "yes", "on"):
                print("already-configured")
                return
            lines[i] = lines[i][:match.start(2)] + "true" + lines[i][match.end(2):]
        else:
            if not lines[start].endswith("\n"):
                lines[start] += newline
            lines.insert(start + 1, " " * indent + "enabled: true" + newline)
        path.write_bytes("".join(lines).encode("utf-8"))
        print("enabled-existing-section")
        return
    if text and not text.endswith("\n"):
        text += newline
    section = GATEWAY_SECTION.format(endpoint=endpoint).replace("\n", newline)
    path.write_bytes((text + section).encode("utf-8"))
    print("section-added")


def main(argv) -> int:
    if len(argv) < 2:
        fail("usage: deploy_tool.py <command> [args]")
    command, args = argv[1], argv[2:]
    if command == "manifest-files":
        cmd_manifest_files(*args[:2])
    elif command == "manifest-drift":
        cmd_manifest_drift(args[0])
    elif command == "manifest-patches":
        cmd_manifest_patches(args[0])
    elif command == "bridge-status":
        cmd_bridge_status(args[0])
    elif command == "read-health-flag":
        cmd_read_health_flag(args[0])
    elif command == "bridge-repo-binds":
        cmd_bridge_repo_binds(args[0])
    elif command == "job-patch-state":
        if len(args) < 3:
            fail("usage: job-patch-state <kind> <source> <live-sha256>")
        cmd_job_patch_state(args[0], args[1], args[2])
    elif command == "self-check":
        cmd_self_check(args[0], args[1])
    elif command == "render-unit":
        cmd_render_unit(args[0], args[1], args[2:])
    elif command == "render-config":
        cmd_render_config(args[0], args[1:])
    elif command == "check-registry":
        cmd_check_registry(args[0], args[1])
    elif command == "enable-gateway-route":
        cmd_enable_gateway_route(args[0], args[1])
    elif command == "patch-jobs":
        if len(args) < 4:
            fail("usage: patch-jobs <kind> <source> <out> <expected-sha256>")
        cmd_patch_jobs(args[0], args[1], args[2], args[3])
    else:
        fail(f"unknown command {command!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
