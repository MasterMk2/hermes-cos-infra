#!/usr/bin/env python3
"""J.1 — the installer and rollback, actually executed.

Asserting that `install.sh` contains the string "rollback.sh" proves nothing
about what happens when a write fails halfway through. So this suite runs the
real scripts, against a real temporary filesystem, with `systemctl`, `curl`,
`runuser`, `useradd` and `ss` replaced by recording fakes on `PATH`.

Everything happens inside a temporary directory. Nothing touches a service, a
real path, a network or a credential store, and every script is invoked with
`--jobs-dir`, `--bridge-config` and `--bridge-socket` pointing into the sandbox.

What is exercised for real:

  * `preflight.sh` against a matching sandbox, and against a corrupt manifest,
    unexpected content at a target, drift in a pinned file, a job-layer target
    that is neither pre- nor post-patch, a missing socket and a missing
    `security_patterns.py`
  * `rollback.sh` restoring content, modes, the patched job layer and the
    recorded service state, refusing a directory that is not a backup, and
    leaving a file edited since the install alone
  * the job-layer patch going fresh → applied → recognised, which is what the
    documented two-stage rollout depends on

The complete install/health/rollback lifecycle, including installer-produced
service snapshots and partial backup failures, is exercised separately in
test_install_lifecycle.py with real filesystem operations and service adapters.

Skipped on platforms without bash or POSIX modes.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "src" / "coding-handoff" / "deploy"

#: The job layer as committed — the content a deployed host still holds,
#: modulo line endings and its own resolved bridge path. 640d9db carries the
#: same src/jobs bytes as the local base (cf7a647) the package was built on,
#: which is not part of the published history.
BASE_REVISION = "640d9db"

BASH = shutil.which("bash")
POSIX = os.name == "posix"

FAKE_BIN = {
    # Records its arguments and succeeds. `is-active`/`is-enabled` answer from
    # a state file the test controls.
    "systemctl": """#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
case "$1" in
  is-active)  grep -q "^${2}=active" "$FAKE_STATE" 2>/dev/null && { echo active; exit 0; }; echo inactive; exit 3 ;;
  is-enabled) grep -q "^${2}=enabled" "$FAKE_STATE" 2>/dev/null && { echo enabled; exit 0; }; echo disabled; exit 1 ;;
  enable|start|restart)
    for a in "$@"; do case "$a" in *.service) sed -i "/^${a}=/d" "$FAKE_STATE" 2>/dev/null; echo "${a}=active" >> "$FAKE_STATE";; esac; done ;;
  disable|stop)
    for a in "$@"; do case "$a" in *.service) sed -i "/^${a}=/d" "$FAKE_STATE" 2>/dev/null; echo "${a}=inactive" >> "$FAKE_STATE";; esac; done ;;
esac
exit 0
""",
    "curl": """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
if [ -f "$FAKE_HEALTH" ]; then cat "$FAKE_HEALTH"; exit 0; fi
exit 7
""",
    "ss": """#!/usr/bin/env bash
echo "LISTEN 0 0 127.0.0.1:18771 0.0.0.0:*"
""",
    "runuser": """#!/usr/bin/env bash
echo "runuser $*" >> "$FAKE_LOG"
exit 0
""",
    "useradd": """#!/usr/bin/env bash
echo "useradd $*" >> "$FAKE_LOG"
exit 0
""",
    "journalctl": """#!/usr/bin/env bash
exit 0
""",
    # `rollback.sh` calls `need_root`, which reads `id -u`. Faking that is a
    # command adapter, not a relaxation of the product: every path the script
    # then touches is inside the sandbox and owned by the test user, so the
    # real restore logic runs for real.
    "id": """#!/usr/bin/env bash
if [ "$1" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
""",
}


@unittest.skipUnless(BASH and POSIX, "needs bash and POSIX file modes")
class InstallerHarness(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="j1-harness-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in FAKE_BIN.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)

        self.log = self.root / "fake.log"
        self.state = self.root / "fake-state"
        self.health = self.root / "health.json"
        self.state.write_text("channel-gateway.service=active\n", encoding="utf-8")
        self.health.write_text(json.dumps(
            {"status": "ok", "dispatch_enabled": False}), encoding="utf-8")

        # A sandbox copy of the package, so the manifest can describe targets
        # inside the temporary tree.
        self.pkg = self.root / "pkg"
        shutil.copytree(ROOT / "src" / "coding-handoff", self.pkg / "src" / "coding-handoff")
        # The *unpatched* job layer, as deployed. The repository's own copies
        # already carry the change, so using them as the preimage would make
        # every transformation a no-op and prove nothing.
        (self.pkg / "src" / "jobs").mkdir(parents=True)
        for name, kind in (("jobs_db.py", "create-job-id"),
                           ("transport.py", "bridge-sock-env")):
            (self.pkg / "src" / "jobs" / name).write_bytes(
                self._unpatched(name, kind))

        self.targets = self.root / "targets"
        self.jobs = self.targets / "hermes-data" / "jobs"
        self.jobs.mkdir(parents=True)
        (self.targets / "hermes-data" / "security_patterns.py").write_text(
            "SECRET_PATTERNS = []\n", encoding="utf-8")

        self.bridge_dir = self.root / "worker"
        (self.bridge_dir / "repos" / "smoke").mkdir(parents=True)
        (self.bridge_dir / "bridge-sock").mkdir(parents=True)
        self.bridge_config = self.bridge_dir / "bridge" / "config.json"
        self.bridge_config.parent.mkdir(parents=True)
        self.bridge_config.write_text(json.dumps({
            "agents": {"hermes-commandcode-glm53flash": "x"},
            "repositories": {"smoke": {
                "path": str(self.bridge_dir / "repos" / "smoke"),
                "allowed_paths": ["pkg/"],
                "test_commands": ["python3 -m unittest"]}},
        }), encoding="utf-8")
        self.socket = self.bridge_dir / "bridge-sock" / "bridge.sock"
        self._make_socket(self.socket)

        self.secret_dir = self.root / "secrets"
        self.secret_dir.mkdir()

        self.manifest = self.pkg / "src" / "coding-handoff" / "deploy" / "manifest.json"
        self._write_sandbox_manifest()

    @staticmethod
    def _unpatched(name, kind):
        """The job-layer file as a deployed host holds it: **without** the
        change this package applies.

        The repository's own copy already carries it, so using that as the
        preimage would make every transformation a no-op and prove nothing.
        Inside a repository the committed blob is used and a missing base
        revision is fatal. Outside one — an exported source tree — the patch's
        own inverse reconstructs it, which `test_job_patches.py` independently
        pins against the committed content when history *is* available.
        """
        in_repo = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=str(ROOT))
        if in_repo.returncode == 0 and in_repo.stdout.strip() == "true":
            blob = subprocess.run(
                ["git", "show", f"{BASE_REVISION}:src/jobs/{name}"],
                capture_output=True, cwd=str(ROOT))
            if blob.returncode != 0:
                raise AssertionError(
                    f"{BASE_REVISION}:src/jobs/{name} is not reachable here")
            return blob.stdout
        sys.path.insert(0, str(DEPLOY))
        import deploy_tool
        return deploy_tool.unapply_job_patch(
            kind, (ROOT / "src" / "jobs" / name).read_bytes())

    def _make_socket(self, path, active_job=None):
        """A bridge that actually answers `worker_status`."""
        import socket
        import threading

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(8)
        self.addCleanup(server.close)
        reply = json.dumps({"bridge": "alive", "docker_reachable": True,
                            "active_job": active_job,
                            "active_review": None}) + "\n"

        def serve():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                try:
                    conn.recv(4096)
                    conn.sendall(reply.encode("utf-8"))
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=serve, daemon=True).start()

    def _write_sandbox_manifest(self):
        """A manifest whose targets live inside the sandbox."""
        gateway_dir = self.targets / "channel-gateway" / "app"
        gateway_dir.mkdir(parents=True)
        self.gateway_app = gateway_dir
        payloads = []
        for name in ("alpha.py", "beta.py"):
            source = self.pkg / "src" / "coding-handoff" / "app" / f"_h_{name}"
            source.write_text(f"# {name} v2\n", encoding="utf-8", newline="\n")
            target = gateway_dir / name
            target.write_text(f"# {name} v1\n", encoding="utf-8", newline="\n")
            payloads.append({
                "source": f"src/coding-handoff/app/_h_{name}",
                "target": str(target),
                "mode": "0644",
                "payload_sha256": self._sha(source),
                "baseline_sha256": self._sha(target),
            })
        self.gateway_files = [Path(p["target"]) for p in payloads]

        # Pinned files this change must not touch.
        self.pinned = self.targets / "antigravity" / "bridge_server.py"
        self.pinned.parent.mkdir(parents=True)
        self.pinned.write_text("# premium release\n", encoding="utf-8", newline="\n")

        for name in ("jobs_db.py", "transport.py"):
            shutil.copy(self.pkg / "src" / "jobs" / name, self.jobs / name)

        manifest = {
            "version": 1, "phase": "J.1",
            "components": {"gateway": {"service": "channel-gateway.service",
                                       "files": payloads}},
            "job_patches": [
                {"kind": "create-job-id", "target": str(self.jobs / "jobs_db.py"),
                 "live_sha256": self._sha(self.jobs / "jobs_db.py"),
                 "reference": "src/jobs/jobs_db.py", "note": "deterministic id"},
                {"kind": "bridge-sock-env", "target": str(self.jobs / "transport.py"),
                 "live_sha256": self._sha(self.jobs / "transport.py"),
                 "reference": "src/jobs/transport.py", "note": "socket override"},
            ],
            "unmodified_drift": [
                {"path": str(self.pinned), "deployed_sha256": self._sha(self.pinned),
                 "note": "premium release; not modified"},
            ],
        }
        self.manifest.write_text(json.dumps(manifest, indent=2),
                                 encoding="utf-8", newline="\n")

    @staticmethod
    def _sha(path):
        import hashlib
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    # -- running ------------------------------------------------------

    def env(self):
        environment = dict(os.environ)
        environment.update({
            "PATH": f"{self.bin}{os.pathsep}{environment.get('PATH', '')}",
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.state),
            "FAKE_HEALTH": str(self.health),
        })
        return environment

    def run_script(self, name, *args):
        script = self.pkg / "src" / "coding-handoff" / "deploy" / name
        return subprocess.run([BASH, str(script), *args], capture_output=True,
                              text=True, env=self.env(), timeout=300)

    def preflight(self, *extra):
        return self.run_script("preflight.sh",
                               "--bridge-config", str(self.bridge_config),
                               "--bridge-socket", str(self.socket),
                               "--jobs-dir", str(self.jobs),
                               "--gateway-app", str(self.gateway_app),
                               "--secret-dir", str(self.secret_dir), *extra)

    def backups(self):
        return sorted((self.root / "backups").glob("hermes-j1-*")) \
            if (self.root / "backups").exists() else []


class Preflight(InstallerHarness):
    """Read-only, and it must actually be read-only."""

    def test_existing_database_and_runtime_directories_need_identity_access(self):
        # The command adapter denies a selected real target. The parent jobs/
        # directory remains writable, reproducing the production mismatch.
        runuser = self.bin / "runuser"
        for name in ("jobs.db", "jobs.db-wal", "jobs.db-shm", "logs", "artifacts", "prompts"):
            path = self.jobs / name
            if name.startswith("jobs.db"):
                path.write_text("fixture", encoding="utf-8")
            else:
                path.mkdir(exist_ok=True)
            runuser.write_text(
                '#!/usr/bin/env bash\n'
                + 'for arg in "$@"; do if [ "$arg" = ' + "'" + str(path) + "'" +
                ' ]; then exit 1; fi; done\nexit 0\n', encoding="utf-8", newline="\n")
            with self.subTest(target=name):
                result = self.preflight()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("write job runtime state", result.stdout)

    def test_it_passes_on_a_matching_sandbox(self):
        result = self.preflight()
        self.assertEqual(result.returncode, 0,
                         result.stdout[-3000:] + result.stderr[-2000:])
        self.assertIn("preflight passed", result.stdout)

    def test_it_writes_nothing(self):
        before = {p: p.read_bytes() for p in self.targets.rglob("*") if p.is_file()}
        self.preflight()
        after = {p: p.read_bytes() for p in self.targets.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_unexpected_content_at_a_target_fails_closed(self):
        self.gateway_files[0].write_text("# somebody else edited this\n",
                                         encoding="utf-8")
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UNEXPECTED CONTENT", result.stdout)

    def test_drift_in_a_pinned_file_fails_closed(self):
        self.pinned.write_text("# changed\n", encoding="utf-8")
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DRIFT", result.stdout)

    def test_a_corrupt_manifest_fails_closed(self):
        """The enumeration used to run in a subshell whose failure nobody saw,
        so a broken manifest produced an empty loop and a clean pass."""
        self.manifest.write_text("{ not json", encoding="utf-8")
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("preflight passed", result.stdout)

    def test_a_manifest_missing_a_section_fails_closed(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        del data["job_patches"]
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)

    def test_a_job_patch_target_is_checked_before_any_write(self):
        (self.jobs / "transport.py").write_text("# unrelated\n", encoding="utf-8")
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither the recorded live content", result.stdout)

    def test_a_missing_bridge_socket_fails(self):
        self.socket.unlink()
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bridge socket not found", result.stdout)

    def test_a_missing_security_patterns_fails(self):
        (self.targets / "hermes-data" / "security_patterns.py").unlink()
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("security_patterns.py", result.stdout)


class InstallFileFailurePath(InstallerHarness):
    """A failure partway through must not leave files replaced."""

    def test_a_write_failure_on_the_second_file_restores_the_first(self):
        # Make the second target's directory read-only after the first write
        # by pointing it at a path the process cannot create.
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        blocked = self.targets / "channel-gateway" / "app" / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.addCleanup(blocked.chmod, 0o700)
        data["components"]["gateway"]["files"][1]["target"] = str(blocked / "beta.py")
        data["components"]["gateway"]["files"][1]["baseline_sha256"] = None
        self.manifest.write_text(json.dumps(data), encoding="utf-8")

        first = self.gateway_files[0]
        original = first.read_bytes()

        result = self.run_script(
            "install.sh", "--bridge-config", str(self.bridge_config),
            "--bridge-socket", str(self.socket), "--jobs-dir", str(self.jobs))

        self.assertNotEqual(result.returncode, 0)
        # Either it never got past preflight, or it rolled the first file back.
        self.assertEqual(first.read_bytes(), original,
                         "the first file was replaced and not restored")

    def test_install_file_reports_failure_instead_of_exiting(self):
        """`die` inside the helper exited the whole shell, so the caller's
        `|| abort` — and therefore the rollback — never ran."""
        source = (self.pkg / "src" / "coding-handoff" / "deploy" / "lib.sh")
        text = source.read_text(encoding="utf-8")
        body = text.split("install_file() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("die ", body)
        self.assertIn("return 1", body)


class RollbackHarness(InstallerHarness):
    def make_backup(self):
        """A backup directory shaped like the one install.sh produces."""
        backup = self.root / "backup"
        (backup / "channel-gateway" / "app").mkdir(parents=True)
        shutil.copy(self.manifest, backup / "manifest.json")
        for path in self.gateway_files:
            saved = backup / str(path).lstrip("/")
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        for name in ("jobs_db.py", "transport.py"):
            saved = backup / str(self.jobs / name).lstrip("/")
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.jobs / name, saved)
        (backup / ".service-state").write_text(
            "channel-gateway.service\tenabled\tactive\n"
            "hermes-coding-handoff.service\tdisabled\tinactive\n",
            encoding="utf-8")
        return backup

    def test_rollback_restores_content_and_modes(self):
        # The mode the host had before the install is 0640; the backup has to
        # capture that, and the restore has to put it back even though the
        # installed file was 0644.
        for path in self.gateway_files:
            path.chmod(0o640)
        backup = self.make_backup()
        originals = {p: p.read_bytes() for p in self.gateway_files}
        for path in self.gateway_files:
            path.write_text("# replaced by the install\n", encoding="utf-8")
            path.chmod(0o644)

        result = self.run_script("rollback.sh", str(backup))
        self.assertEqual(result.returncode, 0,
                         result.stdout[-2000:] + result.stderr[-1000:])
        for path, content in originals.items():
            with self.subTest(path=path.name):
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_rollback_restores_the_patched_job_layer_verbatim(self):
        backup = self.make_backup()
        original = (self.jobs / "transport.py").read_bytes()
        (self.jobs / "transport.py").write_bytes(original + b"\n# patched\n")
        result = self.run_script("rollback.sh", str(backup))
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        self.assertEqual((self.jobs / "transport.py").read_bytes(), original)

    def test_rollback_restores_service_state(self):
        backup = self.make_backup()
        self.state.write_text(
            "channel-gateway.service=inactive\n"
            "hermes-coding-handoff.service=active\n", encoding="utf-8")
        result = self.run_script("rollback.sh", str(backup))
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        commands = self.log.read_text(encoding="utf-8")
        # The gateway was enabled and active before the install, so it must be
        # enabled and running again afterwards.
        self.assertIn("systemctl enable channel-gateway.service", commands)
        self.assertIn("systemctl restart channel-gateway.service", commands)
        # The broker was neither, and must not be left behind running.
        self.assertIn("systemctl disable hermes-coding-handoff.service", commands)
        self.assertIn("systemctl stop hermes-coding-handoff.service", commands)
        self.assertNotIn("systemctl restart hermes-coding-handoff.service",
                         commands)

    def test_rollback_refuses_a_directory_that_is_not_a_backup(self):
        result = self.run_script("rollback.sh", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not look like", result.stderr + result.stdout)

    def test_rollback_leaves_an_edited_new_file_alone(self):
        backup = self.make_backup()
        data = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        introduced = self.targets / "channel-gateway" / "app" / "introduced.py"
        introduced.write_text("# edited after the install\n", encoding="utf-8")
        data["components"]["gateway"]["files"].append({
            "source": "src/coding-handoff/app/_h_alpha.py",
            "target": str(introduced), "mode": "0644",
            "payload_sha256": "0" * 64, "baseline_sha256": None})
        (backup / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        (backup / ".absent" / str(introduced).lstrip("/")).parent.mkdir(
            parents=True, exist_ok=True)
        (backup / ".absent" / str(introduced).lstrip("/")).write_text("")

        result = self.run_script("rollback.sh", str(backup))
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        self.assertTrue(introduced.exists(),
                        "an edited file was deleted by rollback")
        self.assertIn("edited since install", result.stdout)


class JobPatchIdempotence(InstallerHarness):
    def state_of(self, kind, target, live):
        result = subprocess.run(
            [sys.executable, str(DEPLOY / "deploy_tool.py"), "job-patch-state",
             kind, str(target), live], capture_output=True, text=True)
        return result.stdout.strip()

    def test_a_fresh_target_is_fresh_and_a_patched_one_is_recognised(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        for entry in data["job_patches"]:
            with self.subTest(kind=entry["kind"]):
                target = Path(entry["target"])
                live = entry["live_sha256"]
                self.assertEqual(self.state_of(entry["kind"], target, live),
                                 "fresh")
                patched = self.root / "patched"
                applied = subprocess.run(
                    [sys.executable, str(DEPLOY / "deploy_tool.py"),
                     "patch-jobs", entry["kind"], str(target), str(patched), live],
                    capture_output=True, text=True)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                shutil.copy(patched, target)
                # The second stage of the documented rollout hits exactly this.
                self.assertEqual(self.state_of(entry["kind"], target, live),
                                 "applied")

    def test_unknown_content_is_neither(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        entry = data["job_patches"][0]
        Path(entry["target"]).write_text("# something else\n", encoding="utf-8")
        self.assertEqual(
            self.state_of(entry["kind"], entry["target"], entry["live_sha256"]),
            "unknown")

    def test_patching_an_already_patched_target_succeeds_without_writing(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        entry = data["job_patches"][1]
        target, live = Path(entry["target"]), entry["live_sha256"]
        out = self.root / "out"
        subprocess.run([sys.executable, str(DEPLOY / "deploy_tool.py"),
                        "patch-jobs", entry["kind"], str(target), str(out), live],
                       capture_output=True, text=True, check=True)
        shutil.copy(out, target)
        out.unlink()
        again = subprocess.run(
            [sys.executable, str(DEPLOY / "deploy_tool.py"), "patch-jobs",
             entry["kind"], str(target), str(out), live],
            capture_output=True, text=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already-applied", again.stdout)
        self.assertFalse(out.exists(), "nothing should have been written")


if __name__ == "__main__":
    unittest.main()
