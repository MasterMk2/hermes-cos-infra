#!/usr/bin/env python3
"""Phase J.1 — register one project with the coding worker.

The handoff route can only start work the operator has registered, and
"registered" means three separate things on this host:

  1. an entry in the **supervisor** allowlist  (`jobs/worker_config.json`)
  2. a matching entry in the **coding bridge** registry (`bridge/config.json`)
  3. a checkout with at least one commit, because a worktree cannot be branched
     from an unborn repository

Doing that by hand across two files is how allowlists drift apart. This does it
as one reviewable operation, and refuses rather than guessing:

  * both registries are read, updated in memory and written atomically; every
    entry that was already there is preserved and the result is re-read and
    verified before the temporary file replaces the original
  * the two entries are generated from **one** description, so the repository
    id, path, test command and write scope cannot disagree between them
  * a `--dry-run` prints exactly what would change and writes nothing
  * `--rollback <backup-dir>` puts both files back

The bootstrap is local only. It creates a checkout with a README and the
approved specification, commits it with neutral author metadata, and stops.
It never contacts a network, never creates or pushes to a remote, and never
writes application code — choosing a licence or scaffolding an app are product
decisions, not deployment steps.

Usage:

    onboard_project.py plan     --project fastrawphotoview --config <json> ...
    onboard_project.py bootstrap --project fastrawphotoview --config <json> ...
    onboard_project.py apply    --project fastrawphotoview --config <json> ...
    onboard_project.py rollback --backup <dir>

`--config` is a small JSON description; `--print-template` emits one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

#: An absolute path. The deployment is POSIX; the validator does not assume the
#: host running it is, because this module's tests run on both. What matters is
#: that a *relative* path is refused — a registry path resolved against a
#: working directory would depend on how the operator happened to invoke this.
_ABSOLUTE_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")

# Neutral author metadata for the bootstrap commit. A deployment step must not
# attribute a commit to a person, and it must not leak an account name into a
# repository that may later be published.
COMMIT_NAME = "Hermes Coding Worker"
COMMIT_EMAIL = "coding-worker@localhost"

TEMPLATE = {
    "project": "fastrawphotoview",
    "repo_id": "fastrawphotoview",
    "agent": "pi-sandbox-hermes-commandcode",
    "supervisor_config": "/srv/hermes/hermes-data/jobs/worker_config.json",
    "supervisor_repo_path": "/opt/data/coding-worker/repos/fastrawphotoview",
    "bridge_config": "/REPLACE/coding-worker/bridge/config.json",
    "bridge_repo_path": "/REPLACE/coding-worker/repos/fastrawphotoview",
    "spec_dir": "/opt/hermes-coding-handoff/specs/fastrawphotoview",
    "test_commands": ["cargo test --manifest-path src-tauri/Cargo.toml"],
    "allowed_paths": ["src-tauri/", "src/", "tests/", "package.json",
                      "LICENSES.md", "README.md"],
    "note": "Phase J.1 onboarding. Registered for the chat handoff route.",
}

REQUIRED_KEYS = tuple(TEMPLATE)

README_TEMPLATE = """# {project}

Working repository for the supervised coding worker.

This checkout was created by the Phase J.1 onboarding step so that a worktree
can be branched from a real commit. It contains the approved specification and
nothing else: no application code, no dependency manifest and no licence
choice, because those are decisions for the implementation, not for a
deployment script.

The specification the worker is given is `{spec_name}`, and the authoritative
copy lives with the handoff broker's registry.
"""


class OnboardError(RuntimeError):
    pass


# ------------------------------------------------------------------ config


def load_description(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OnboardError(f"cannot read {path}: {exc}") from exc
    missing = [k for k in REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise OnboardError(f"description is missing: {missing}")
    unknown = set(data) - set(REQUIRED_KEYS)
    if unknown:
        raise OnboardError(f"description has unknown keys: {sorted(unknown)}")
    for key in ("supervisor_config", "supervisor_repo_path", "bridge_config",
                "bridge_repo_path", "spec_dir"):
        value = str(data[key])
        if not _ABSOLUTE_RE.match(value) or ".." in value:
            raise OnboardError(f"{key} must be an absolute path without '..'")
        if "/REPLACE/" in value:
            raise OnboardError(
                f"{key} still holds the template placeholder; the bridge tree "
                "differs per host and must be stated explicitly")
    if not isinstance(data["test_commands"], list) or not data["test_commands"]:
        raise OnboardError("test_commands must be a non-empty list")
    if not isinstance(data["allowed_paths"], list) or not data["allowed_paths"]:
        raise OnboardError("allowed_paths must be a non-empty list")
    for path_prefix in data["allowed_paths"]:
        if _ABSOLUTE_RE.match(str(path_prefix)) or ".." in str(path_prefix):
            raise OnboardError(
                f"allowed_paths entry {path_prefix!r} must be repository-relative")
    return data


# -------------------------------------------------------------- registries


def read_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OnboardError(f"cannot read {path}: {exc}") from exc


def write_json_atomic(path: str, data: dict, *, expected_bytes=None) -> None:
    """Write, verify, then replace.

    The temporary file is created in the destination directory so the rename is
    atomic, and the result is read back and compared before it replaces the
    original — a truncated allowlist would take the whole worker offline.
    """
    target = Path(path)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False,
        dir=str(target.parent), prefix=".onboard-")
    try:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        verified = json.loads(Path(handle.name).read_text(encoding="utf-8"))
        if verified != data:
            raise OnboardError(f"verification failed writing {path}")
        os.chmod(handle.name, mode)
        if target.exists():
            stat = target.stat()
            try:
                os.chown(handle.name, stat.st_uid, stat.st_gid)
            except (AttributeError, PermissionError, OSError):
                pass
        if expected_bytes is not None and target.read_bytes() != expected_bytes:
            raise OnboardError("registry changed concurrently; refusing replacement")
        os.replace(handle.name, str(target))
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


def supervisor_entry(desc: dict) -> dict:
    return {
        "path": desc["supervisor_repo_path"],
        "test_commands": list(desc["test_commands"]),
        "allowed_paths": list(desc["allowed_paths"]),
        "allow_simulated": False,
        "note": desc["note"],
    }


def bridge_entry(desc: dict) -> dict:
    return {
        "path": desc["bridge_repo_path"],
        "test_commands": list(desc["test_commands"]),
        "allowed_paths": list(desc["allowed_paths"]),
        "note": desc["note"],
    }


def plan(desc: dict) -> dict:
    """What would change, without changing anything."""
    supervisor = read_json(desc["supervisor_config"])
    bridge = read_json(desc["bridge_config"])
    repo_id = desc["repo_id"]

    existing_supervisor = sorted((supervisor.get("repositories") or {}))
    existing_bridge = sorted((bridge.get("repositories") or {}))

    if desc["agent"] not in (supervisor.get("production_agents") or []):
        raise OnboardError(
            f"{desc['agent']} is not in the supervisor's production_agents; "
            "onboarding must not invent an agent")

    return {
        "repo_id": repo_id,
        "supervisor": {
            "path": desc["supervisor_config"],
            "already_present": repo_id in existing_supervisor,
            "existing_entries": existing_supervisor,
            "new_entry": supervisor_entry(desc),
        },
        "bridge": {
            "path": desc["bridge_config"],
            "already_present": repo_id in existing_bridge,
            "existing_entries": existing_bridge,
            "new_entry": bridge_entry(desc),
        },
        "checkout": {
            "path": desc["bridge_repo_path"],
            "exists": Path(desc["bridge_repo_path"]).is_dir(),
            "has_commit": has_commit(desc["bridge_repo_path"]),
        },
    }


@contextmanager
def registry_locks(desc):
    acquired = []
    try:
        for path in sorted({str(Path(desc[key]).resolve()) for key in
                            ("supervisor_config", "bridge_config")}):
            lock = Path(path + ".onboard.lock")
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise OnboardError("another registry transaction holds the lock") from exc
            os.close(fd)
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.unlink()


def apply(desc: dict, backup_dir: str) -> dict:
    with registry_locks(desc):
        return _apply_locked(desc, backup_dir)


def _apply_locked(desc: dict, backup_dir: str) -> dict:
    """Update both allowlists, preserving every entry already there."""
    result = plan(desc)
    backup = Path(backup_dir)
    backup.mkdir(parents=True, exist_ok=True)
    os.chmod(backup, 0o700)

    originals = {}
    for name, key in (("supervisor", "supervisor_config"),
                      ("bridge", "bridge_config")):
        source = Path(desc[key])
        originals[str(source)] = source.read_bytes()
        saved = backup / f"{name}-{source.name}"
        with saved.open("xb") as handle:
            handle.write(originals[str(source)])
        shutil.copystat(source, saved)

    # Both documents are read, updated and validated *before* either is
    # written. Writing the first and then discovering the second is unreadable
    # would leave the two allowlists disagreeing about what the worker may
    # touch, which is the exact condition this operation exists to prevent.
    prepared = []
    for key, entry_for in (("supervisor_config", supervisor_entry),
                           ("bridge_config", bridge_entry)):
        document = json.loads(originals[desc[key]])
        document.setdefault("repositories", {})
        before = set(document["repositories"])
        document["repositories"][desc["repo_id"]] = entry_for(desc)
        if not before <= set(document["repositories"]):
            raise OnboardError("refusing to write: an existing entry would be lost")
        prepared.append((desc[key], document, before))

    written = []
    postimages = {}
    expected_postimages = {
        path: json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        for path, document, _ in prepared}
    # Recovery metadata must be durable before either live file changes.
    with (backup / "postimages.json").open("x", encoding="utf-8") as handle:
        json.dump(expected_postimages, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        for path, document, _ in prepared:
            # Compare *both* inputs before each replacement. Cooperative
            # writers are serialized, and unrelated edits are never adopted.
            for other, original in originals.items():
                expected = postimages.get(other, original)
                if Path(other).read_bytes() != expected:
                    raise OnboardError("registry changed concurrently; refusing update")
            postimage = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            write_json_atomic(path, document, expected_bytes=originals[path])
            postimages[path] = postimage
            written.append(path)
    except Exception:
        # Put back whatever was already replaced, from the backup taken above.
        for path in written:
            name = "supervisor" if path == desc["supervisor_config"] else "bridge"
            saved = backup / f"{name}-{Path(path).name}"
            if saved.is_file():
                try:
                    write_json_atomic(
                        path, json.loads(saved.read_text(encoding="utf-8")),
                        expected_bytes=postimages[path])
                except Exception:  # nothing further can be done safely here
                    raise OnboardError(
                        f"{path} was updated and could not be restored; "
                        f"the original is in {saved}")
        raise

    result["backup"] = str(backup)
    result["applied"] = True
    return result


def rollback(backup_dir: str, desc: dict) -> dict:
    with registry_locks(desc):
        return _rollback_locked(backup_dir, desc)


def _rollback_locked(backup_dir: str, desc: dict) -> dict:
    backup = Path(backup_dir)
    postimages = read_json(str(backup / "postimages.json"))
    for key in ("supervisor_config", "bridge_config"):
        if Path(desc[key]).read_bytes() != postimages[desc[key]].encode("utf-8"):
            raise OnboardError("registry changed since onboarding; refusing rollback")
    restored = []
    for name, key in (("supervisor", "supervisor_config"),
                      ("bridge", "bridge_config")):
        target = Path(desc[key])
        saved = backup / f"{name}-{target.name}"
        if saved.is_file():
            data = json.loads(saved.read_text(encoding="utf-8"))
            write_json_atomic(str(target), data,
                              expected_bytes=postimages[str(target)].encode("utf-8"))
            restored.append(str(target))
    return {"restored": restored}


# --------------------------------------------------------------- bootstrap


def git(*args, cwd=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": COMMIT_NAME, "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
        "GIT_COMMITTER_NAME": COMMIT_NAME, "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
        # No network, no credential prompt, ever.
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
    })
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def has_commit(repo_path: str) -> bool:
    if not Path(repo_path, ".git").exists():
        return False
    return git("-C", repo_path, "rev-parse", "--verify", "HEAD").returncode == 0


def bootstrap(desc: dict) -> dict:
    """Give the repository a clean first commit, locally.

    The remote for this project exists and is empty, so there is no commit to
    branch a worktree from and every dispatch would fail at `git worktree add`.
    This creates one — README plus the approved specification, nothing else.

    It performs no network operation of any kind: no clone, no fetch, no remote
    is configured and no push is attempted. Publishing the result is a separate,
    human decision.
    """
    repo_path = Path(desc["bridge_repo_path"])
    if has_commit(str(repo_path)):
        return {"bootstrapped": False, "reason": "already has a commit",
                "path": str(repo_path)}

    spec_dir = Path(desc["spec_dir"])
    revisions = sorted(p for p in spec_dir.glob("[0-9][0-9][0-9][0-9]-*.md"))
    if not revisions:
        raise OnboardError(f"no specification revision found in {spec_dir}")
    spec = revisions[-1]

    # An unborn repository is not necessarily an empty one. It may hold files
    # somebody put there, a staged index, or a configured remote — and the
    # `git commit` below would sweep every staged path into the commit, not
    # just the two added here. "README and the approved specification, and
    # nothing else" only holds if that is verified rather than assumed.
    existing = [p.name for p in repo_path.iterdir()
                if p.name != ".git"] if repo_path.is_dir() else []
    if existing:
        raise OnboardError(
            f"{repo_path} already contains {sorted(existing)[:5]}; refusing to "
            "overwrite. Bootstrap expects an empty or absent directory")

    repo_path.mkdir(parents=True, exist_ok=True)
    if not Path(repo_path, ".git").exists():
        result = git("init", "-q", str(repo_path))
        if result.returncode != 0:
            raise OnboardError(f"git init failed: {result.stderr[:200]}")

    staged = git("-C", str(repo_path), "diff", "--cached", "--name-only")
    if staged.returncode == 0 and staged.stdout.strip():
        raise OnboardError(
            f"{repo_path} has a non-empty index; a commit here would include "
            "files this step did not add. Refusing")

    remotes = git("-C", str(repo_path), "remote")
    if remotes.returncode == 0 and remotes.stdout.strip():
        raise OnboardError(
            f"{repo_path} already has remote(s) {remotes.stdout.split()}; "
            "refusing. Removing somebody's remote is not a deployment step")

    (repo_path / "README.md").write_text(
        README_TEMPLATE.format(project=desc["project"], spec_name=spec.name),
        encoding="utf-8", newline="\n")
    docs = repo_path / "docs"
    docs.mkdir(exist_ok=True)
    shutil.copyfile(spec, docs / spec.name)

    add = git("-C", str(repo_path), "add", "README.md", f"docs/{spec.name}")
    if add.returncode != 0:
        raise OnboardError(f"git add failed: {add.stderr[:200]}")
    # `--only` restricts the commit to the paths named here, so even a
    # surprising index cannot smuggle a file into this commit.
    commit = git("-C", str(repo_path), "commit", "-q", "--only",
                 "README.md", f"docs/{spec.name}", "-m",
                 f"chore: bootstrap {desc['project']} working checkout")
    if commit.returncode != 0:
        raise OnboardError(f"git commit failed: {commit.stderr[:200]}")

    head = git("-C", str(repo_path), "rev-parse", "HEAD")
    return {"bootstrapped": True, "path": str(repo_path),
            "head": head.stdout.strip()[:12], "spec": spec.name,
            "remote": None}


# -------------------------------------------------------------------- cli


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command",
                        choices=("plan", "bootstrap", "apply", "rollback",
                                 "print-template"))
    parser.add_argument("--config")
    parser.add_argument("--backup", default=None)
    args = parser.parse_args(argv)

    if args.command == "print-template":
        print(json.dumps(TEMPLATE, indent=2, ensure_ascii=False))
        return 0

    if not args.config:
        parser.error("--config is required")
    desc = load_description(args.config)

    try:
        if args.command == "plan":
            print(json.dumps(plan(desc), indent=2, ensure_ascii=False))
        elif args.command == "bootstrap":
            print(json.dumps(bootstrap(desc), indent=2, ensure_ascii=False))
        elif args.command == "apply":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = args.backup or f"/var/backups/hermes-j1-onboard-{stamp}"
            print(json.dumps(apply(desc, backup), indent=2, ensure_ascii=False))
        elif args.command == "rollback":
            if not args.backup:
                parser.error("--backup is required for rollback")
            print(json.dumps(rollback(args.backup, desc), indent=2,
                             ensure_ascii=False))
    except OnboardError as exc:
        sys.stderr.write(f"onboard: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
