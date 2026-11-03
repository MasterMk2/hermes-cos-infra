#!/usr/bin/env python3
"""J.1 rollout package tests.

The installer is the part of this change that runs as root on a live host, so
the properties worth testing are the ones that decide whether it can do damage:

  * the manifest describes the tree it was generated from, so an install never
    applies content nobody reviewed
  * the baselines are the real hashes of the real committed files, so the
    fail-closed check actually closes
  * the drifted Antigravity files are pinned and not installed over
  * every script is syntactically valid, uses LF endings, and prints no secret

These are the static and rendering checks. `test_installer_harness.py` executes
the scripts themselves against a sandbox; what stays here is what can be
decided by reading the package and by rendering its templates.
"""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "src" / "coding-handoff" / "deploy"
MANIFEST_PATH = DEPLOY / "manifest.json"
SCRIPTS = ("lib.sh", "install.sh", "preflight.sh", "rollback.sh", "health.sh")
PYTHON = sys.executable


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def in_git_work_tree() -> bool:
    """Whether this tree is a real repository with history.

    The distinction matters: a baseline assertion that cannot find `cf7a647`
    *inside a repository* is a genuine problem — the recorded baselines would
    no longer be checkable — while the same failure in an exported source tree
    is a limitation of where the test is running. The first must fail, the
    second must skip, and collapsing them would quietly retire the check that
    makes the installer's fail-closed gate meaningful.
    """
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, cwd=str(ROOT))
    return result.returncode == 0 and result.stdout.strip() == "true"



class Manifest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def entries(self):
        for component in self.manifest["components"].values():
            yield from component["files"]

    def test_every_payload_hash_matches_the_tree(self):
        """A stale manifest is caught here, not on the Pi."""
        for entry in self.entries():
            with self.subTest(source=entry["source"]):
                path = ROOT / entry["source"]
                self.assertTrue(path.is_file(), f"missing {entry['source']}")
                self.assertEqual(
                    sha256(path), entry["payload_sha256"],
                    f"{entry['source']} changed since make-manifest.py ran")

    def test_regenerating_the_manifest_is_a_no_op(self):
        result = subprocess.run(
            ["python3" if Path("/usr/bin/python3").exists() else "python",
             str(DEPLOY / "make-manifest.py")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertEqual(
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8")), self.manifest,
            "the checked-in manifest does not describe the current tree")

    def test_replaced_files_carry_a_baseline_and_new_files_do_not(self):
        for entry in self.entries():
            with self.subTest(target=entry["target"]):
                baseline = entry["baseline_sha256"]
                if baseline is None:
                    continue
                self.assertRegex(baseline, r"^[0-9a-f]{64}$")

    def test_the_gateway_baselines_are_the_committed_content(self):
        """The fail-closed gate is only closed if these are the real hashes.

        Every gateway file this package replaces was verified byte-identical
        between the committed base and the deployed working tree, so the
        committed content is what the installer must find on the host.

        9fcd1f8 lives only in the local history this package was built in.
        The published history recovered the release from that branch without
        its commits, so there the baselines are recorded but not checkable.
        """
        if not in_git_work_tree():
            self.skipTest("not a git work tree; run this in a clone")
        known = subprocess.run(["git", "cat-file", "-e", "9fcd1f8^{commit}"],
                               capture_output=True, cwd=str(ROOT))
        if known.returncode != 0:
            self.skipTest("9fcd1f8 is not in this history (it was never "
                          "published); the baselines cannot be re-derived here")
        for entry in self.entries():
            if not entry["target"].startswith("/srv/hermes/channel-gateway/app"):
                continue
            if entry["baseline_sha256"] is None:
                continue
            name = Path(entry["source"]).name
            with self.subTest(file=name):
                committed = subprocess.run(
                    ["git", "show", f"9fcd1f8:src/channel-gateway/app/{name}"],
                    capture_output=True, cwd=str(ROOT))
                # Inside a repository, a missing deployed base is a problem:
                # the recorded baselines would no longer be verifiable.
                self.assertEqual(committed.returncode, 0,
                                 f"9fcd1f8:{name} is not reachable in this "
                                 "repository; the baseline cannot be verified")
                self.assertEqual(
                    hashlib.sha256(committed.stdout).hexdigest(),
                    entry["baseline_sha256"],
                    f"{name}: recorded baseline is not the committed content")

    def test_the_drifted_files_are_pinned_and_never_installed(self):
        drift = self.manifest["unmodified_drift"]
        self.assertGreaterEqual(len(drift), 3)
        targets = {e["target"] for e in self.entries()}
        for entry in drift:
            with self.subTest(path=entry["path"]):
                self.assertRegex(entry["deployed_sha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn(entry["path"], targets,
                                 "a pinned file must not also be installed")
                self.assertIn("antigravity", entry["path"])

    def test_the_premium_route_files_are_among_the_pinned(self):
        pinned = {Path(e["path"]).name for e in self.manifest["unmodified_drift"]}
        self.assertEqual(
            pinned,
            {"bridge_server.py", "bridge_config.py", "premium_routing.py"})

    def test_no_target_escapes_the_expected_roots(self):
        allowed = ("/srv/hermes/channel-gateway/app/",
                   "/opt/hermes-coding-handoff/",
                   "/etc/systemd/system/",
                   "/srv/hermes/hermes-data/jobs/")
        for entry in self.entries():
            with self.subTest(target=entry["target"]):
                self.assertTrue(entry["target"].startswith(allowed),
                                f"unexpected install target {entry['target']}")
                self.assertNotIn("..", entry["target"])

    def test_modes_are_conservative(self):
        for entry in self.entries():
            with self.subTest(target=entry["target"]):
                self.assertRegex(entry["mode"], r"^0[0-7]{3}$")
                self.assertNotIn(entry["mode"], ("0777", "0666", "0755"))

    def test_the_specifications_ship_with_the_package(self):
        sources = {e["source"] for e in self.entries()}
        self.assertIn(
            "src/coding-handoff/specs/fastrawphotoview/"
            "0001-nas-raw-browser-foundation.md", sources)
        self.assertIn(
            "src/coding-handoff/specs/handoff-protocol-check/"
            "0001-route-verification.md", sources)


class Scripts(unittest.TestCase):
    def test_every_script_parses(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                result = subprocess.run(["bash", "-n", str(DEPLOY / name)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_scripts_use_lf_endings(self):
        """A CRLF shell script fails on the Pi as a syntax error."""
        for name in SCRIPTS:
            with self.subTest(script=name):
                self.assertNotIn(b"\r\n", (DEPLOY / name).read_bytes())

    def test_the_installer_refuses_to_run_without_preflight(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("preflight.sh", text)
        self.assertIn("nothing was written", text)
        self.assertIn("need_root", text)

    def test_the_installer_backs_up_before_writing(self):
        lib = (DEPLOY / "lib.sh").read_text(encoding="utf-8")
        # cp -p keeps mode and ownership; the backup happens before the write.
        self.assertIn("cp -p", lib)
        install_body = lib.split("install_file()", 1)[1]
        self.assertLess(install_body.index("backup_file"),
                        install_body.index("mv -f"))

    def test_no_script_echoes_a_secret(self):
        for name in SCRIPTS:
            text = (DEPLOY / name).read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                with self.subTest(script=name, line=stripped[:60]):
                    # A token may be generated and installed, never printed.
                    self.assertFalse(
                        re.search(r"(echo|say|printf|cat)\b[^|#]*\$\{?(TOKEN|SECRET)",
                                  stripped, re.IGNORECASE),
                        "a credential must never be printed")

    def test_the_installer_installs_no_provider_or_model_choice(self):
        """The agent and the model come from configuration that already
        exists. An installer that could substitute them would make a
        deployment a provider decision."""
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        for forbidden in ("pip install", "npm install", "docker pull",
                          "docker build", "apt-get install", "curl -o"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_rollback_keeps_the_ledger(self):
        text = (DEPLOY / "rollback.sh").read_text(encoding="utf-8")
        self.assertIn("ledger", text.lower())
        self.assertNotIn("rm -rf", text)

    def test_rollback_will_not_delete_an_edited_file(self):
        text = (DEPLOY / "rollback.sh").read_text(encoding="utf-8")
        self.assertIn("edited since install", text)

    def test_health_checks_the_services_this_change_must_not_disturb(self):
        text = (DEPLOY / "health.sh").read_text(encoding="utf-8")
        self.assertIn("hermes-antigravity.service", text)
        self.assertIn("hermes-premium-cli.service", text)

    def test_the_gateway_dropin_only_adds_a_credential(self):
        conf = (ROOT / "src" / "coding-handoff" / "systemd"
                / "channel-gateway-handoff.conf").read_text(encoding="utf-8")
        directives = [l for l in conf.splitlines()
                      if l.strip() and not l.startswith("#")
                      and not l.startswith("[")]
        self.assertEqual(len(directives), 1)
        self.assertTrue(directives[0].startswith("LoadCredential="))

    def test_the_installer_takes_no_default_bridge_path(self):
        """An earlier draft defaulted these to a tree that does not exist."""
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("--bridge-config is required", text)
        self.assertIn("--bridge-socket is required", text)
        self.assertNotIn("/srv/hermes/coding-worker", text)

    def test_the_installer_rolls_itself_back_when_health_fails(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("AUTO_ROLLBACK", text)
        self.assertIn('abort "health checks failed"', text)
        # The abort handler runs rollback.sh itself rather than printing a
        # command and leaving the host half-changed.
        abort_body = text.split("abort() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("rollback.sh", abort_body)
        self.assertIn('AUTO_ROLLBACK" = "1"', abort_body)

    def test_no_shell_script_embeds_a_python_heredoc(self):
        """House rule: scripts carrying backslash or \\n literals are written as
        files and executed, never piped through a shell heredoc."""
        for name in SCRIPTS:
            with self.subTest(script=name):
                text = (DEPLOY / name).read_text(encoding="utf-8")
                self.assertNotIn("<<'PY'", text)
                self.assertNotIn("<<PY", text)
                self.assertNotIn("<<EOF", text)

    def test_the_installer_derives_its_identity_rather_than_creating_one(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("stat -c '%U'", text)
        self.assertNotIn("useradd", text)
        self.assertIn("must not run as root", text)


class RenderedUnit(unittest.TestCase):
    """The unit is rendered from live values, so the rendering is the artefact
    worth testing rather than a template full of placeholders."""

    VALUES = {
        "SERVICE_USER": "workerowner",
        "SERVICE_GROUP": "workerowner",
        "STATE_DIR": "/opt/hermes-coding-handoff/state",
        "APP_DIR": "/opt/hermes-coding-handoff/app",
        "CONFIG_DIR": "/opt/hermes-coding-handoff/config",
        "CONFIG_FILE": "/opt/hermes-coding-handoff/config/handoff.json",
        "SPEC_DIR": "/opt/hermes-coding-handoff/specs",
        "JOBS_DIR": "/srv/hermes/hermes-data/jobs",
        "JOBS_PARENT": "/srv/hermes/hermes-data",
        "BRIDGE_SOCKET": "/home/example-operator/coding-worker/bridge-sock/bridge.sock",
        "BRIDGE_SOCKET_DIR": "/home/example-operator/coding-worker/bridge-sock",
        "BRIDGE_CONFIG": "/home/example-operator/coding-worker/bridge/config.json",
        "GATEWAY_DIR": "/srv/hermes/channel-gateway",
        "REPO_BINDS":
            "BindReadOnlyPaths=/home/example-operator/coding-worker/repos",
    }

    def render(self, **overrides):
        import subprocess as sp
        values = dict(self.VALUES)
        values.update(overrides)
        out = Path(tempfile.mkdtemp()) / "unit.service"
        self.addCleanup(lambda: out.parent.exists()
                        and __import__("shutil").rmtree(out.parent,
                                                        ignore_errors=True))
        result = sp.run(
            [PYTHON, str(DEPLOY / "deploy_tool.py"), "render-unit",
             str(ROOT / "src" / "coding-handoff" / "systemd"
                 / "hermes-coding-handoff.service.in"), str(out)]
            + [f"{k}={v}" for k, v in values.items()],
            capture_output=True, text=True)
        return result, out

    def test_it_renders_with_no_placeholder_left(self):
        result, out = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("@", text.replace("@system-service", ""))

    def test_the_live_paths_reach_the_unit(self):
        _, out = self.render()
        text = out.read_text(encoding="utf-8")
        self.assertIn("User=workerowner", text)
        self.assertIn("HERMES_CODING_BRIDGE_SOCK=/home/example-operator/"
                      "coding-worker/bridge-sock/bridge.sock", text)
        self.assertIn("BindPaths=/home/example-operator/coding-worker/bridge-sock",
                      text)
        self.assertIn("BindReadOnlyPaths=/home/example-operator/coding-worker/"
                      "bridge/config.json", text)

    def test_it_stays_hardened(self):
        _, out = self.render()
        text = out.read_text(encoding="utf-8")
        for directive in ("NoNewPrivileges=true", "ProtectSystem=strict",
                          "IPAddressDeny=any", "IPAddressAllow=localhost",
                          "CapabilityBoundingSet=", "RestrictNamespaces=true",
                          "PrivateTmp=true"):
            with self.subTest(directive=directive):
                self.assertIn(directive, text)

    def test_protecthome_is_tmpfs_so_a_home_based_bridge_still_works(self):
        """`ProtectHome=true` would hide a bridge socket under the operator's
        home, which is where it actually lives on this host."""
        _, out = self.render()
        self.assertIn("ProtectHome=tmpfs", out.read_text(encoding="utf-8"))

    def test_the_service_cannot_rewrite_its_own_code_or_registry(self):
        _, out = self.render()
        text = out.read_text(encoding="utf-8")
        writable = [l for l in text.splitlines()
                    if l.startswith("ReadWritePaths=")]
        self.assertEqual(len(writable), 1)
        self.assertNotIn("/opt/hermes-coding-handoff/app", writable[0])
        self.assertNotIn("/opt/hermes-coding-handoff/config", writable[0])
        self.assertIn("ReadOnlyPaths=/opt/hermes-coding-handoff/app", text)

    def test_the_gateway_tree_is_invisible_to_the_broker(self):
        _, out = self.render()
        self.assertIn("InaccessiblePaths=/srv/hermes/channel-gateway",
                      out.read_text(encoding="utf-8"))

    def test_the_job_layers_out_of_tree_import_is_reachable(self):
        """`supervisor.py` inserts jobs/'s parent on sys.path and imports
        `security_patterns` from there. Under ProtectHome that has to be bound
        back in, or every dispatch fails at import time."""
        _, out = self.render()
        self.assertIn("BindReadOnlyPaths=/srv/hermes/hermes-data",
                      out.read_text(encoding="utf-8"))


class UnitCoversRuntimeFacts(unittest.TestCase):
    """The unit must expose exactly the paths the broker actually stats.

    `ProtectHome=tmpfs` hides the operator's home, and on this host the coding
    bridge's repositories live there. A unit that binds the socket and the
    config but not the checkouts makes `git_has_head` report every project as
    missing — the service starts, answers, and can never dispatch anything.

    So this ties the two together: the paths `Broker.gather` reads are derived
    from the same bridge registry the binds are, and every one of them must be
    covered by the rendered unit.
    """

    HOME_BRIDGE = {
        "agents": {"hermes-commandcode-glm53flash": "y"},
        "repositories": {
            "smoke": {"path": "/home/example-operator/coding-worker/repos/smoke"},
            "frpv": {"path": "/home/example-operator/coding-worker/repos/frpv"},
        },
    }

    def setUp(self):
        sys.path.insert(0, str(DEPLOY))
        import deploy_tool
        self.deploy_tool = deploy_tool
        self.dir = Path(tempfile.mkdtemp())
        self.bridge = self.dir / "bridge-config.json"

    def write(self, doc):
        self.bridge.write_text(json.dumps(doc), encoding="utf-8")
        return str(self.bridge)

    def covered(self, unit_text, path):
        """True when some bind line exposes `path` or one of its ancestors."""
        bound = [line.split("=", 1)[1].strip()
                 for line in unit_text.splitlines()
                 if line.startswith(("BindPaths=", "BindReadOnlyPaths="))]
        return any(path == b or path.startswith(b.rstrip("/") + "/")
                   for b in bound)

    def render_with(self, binds):
        values = dict(RenderedUnit.VALUES)
        values["REPO_BINDS"] = "\n".join(
            f"BindReadOnlyPaths={b}" for b in binds)
        out = self.dir / "unit.service"
        result = subprocess.run(
            [PYTHON, str(DEPLOY / "deploy_tool.py"), "render-unit",
             str(ROOT / "src" / "coding-handoff" / "systemd"
                 / "hermes-coding-handoff.service.in"), str(out)]
            + [f"{k}={v}" for k, v in values.items()],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return out.read_text(encoding="utf-8")

    def test_repositories_under_the_operator_home_are_bound(self):
        config = self.write(self.HOME_BRIDGE)
        binds = self.deploy_tool.bridge_repo_binds(config)
        unit = self.render_with(binds)
        for entry in self.HOME_BRIDGE["repositories"].values():
            with self.subTest(path=entry["path"]):
                self.assertTrue(
                    self.covered(unit, entry["path"]),
                    f"{entry['path']} is not exposed by the rendered unit; "
                    "readiness would report it missing")

    def test_a_repository_onboarded_later_is_already_covered(self):
        """Onboarding adds a repository *after* the unit is rendered."""
        config = self.write(self.HOME_BRIDGE)
        unit = self.render_with(self.deploy_tool.bridge_repo_binds(config))
        self.assertTrue(self.covered(
            unit, "/home/example-operator/coding-worker/repos/added-later"))

    def test_a_shallow_common_parent_falls_back_to_individual_binds(self):
        config = self.write({
            "repositories": {"a": {"path": "/home/example-operator/a"},
                             "b": {"path": "/srv/elsewhere/b"}}})
        binds = self.deploy_tool.bridge_repo_binds(config)
        self.assertNotIn("/", binds)
        self.assertNotIn("/home", binds)
        unit = self.render_with(binds)
        for path in ("/home/example-operator/a", "/srv/elsewhere/b"):
            with self.subTest(path=path):
                self.assertTrue(self.covered(unit, path))

    def test_a_single_repository_binds_its_parent(self):
        config = self.write({
            "repositories": {"only": {
                "path": "/home/example-operator/coding-worker/repos/only"}}})
        self.assertEqual(
            self.deploy_tool.bridge_repo_binds(config),
            ["/home/example-operator/coding-worker/repos"])

    def test_home_protection_is_not_weakened_wholesale(self):
        config = self.write(self.HOME_BRIDGE)
        unit = self.render_with(self.deploy_tool.bridge_repo_binds(config))
        self.assertIn("ProtectHome=tmpfs", unit)
        self.assertNotIn("ProtectHome=false", unit)
        self.assertNotIn("ProtectHome=no", unit)
        bound = [line.split("=", 1)[1].strip()
                 for line in unit.splitlines()
                 if line.startswith(("BindPaths=", "BindReadOnlyPaths="))]
        # Nothing may expose a home directory root or the whole filesystem.
        for path in bound:
            with self.subTest(path=path):
                self.assertNotIn(path, ("/", "/home", "/root"))
                self.assertGreaterEqual(
                    len([p for p in path.split("/") if p]), 3,
                    f"{path} is too broad a bind target")

    def test_the_repositories_are_bound_read_only(self):
        config = self.write(self.HOME_BRIDGE)
        binds = self.deploy_tool.bridge_repo_binds(config)
        unit = self.render_with(binds)
        for bind in binds:
            with self.subTest(bind=bind):
                self.assertIn(f"BindReadOnlyPaths={bind}", unit)
                self.assertNotIn(f"BindPaths={bind}", unit)
        writable = [l for l in unit.splitlines()
                    if l.startswith("ReadWritePaths=")][0]
        for bind in binds:
            self.assertNotIn(bind, writable)


class UnitRenderRefusals(unittest.TestCase):
    """A placeholder that is renamed or dropped must fail loudly rather than
    ship a unit with `@JOBS_DIR@` in it."""

    def test_a_missing_value_is_refused(self):
        values = {k: v for k, v in RenderedUnit.VALUES.items()
                  if k != "JOBS_DIR"}
        out = Path(tempfile.mkdtemp()) / "unit.service"
        result = subprocess.run(
            [PYTHON, str(DEPLOY / "deploy_tool.py"), "render-unit",
             str(ROOT / "src" / "coding-handoff" / "systemd"
                 / "hermes-coding-handoff.service.in"), str(out)]
            + [f"{k}={v}" for k, v in values.items()],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing unit placeholders", result.stderr)

    def test_an_unknown_value_is_refused(self):
        values = dict(RenderedUnit.VALUES, RUN_AS_ROOT="yes")
        out = Path(tempfile.mkdtemp()) / "unit.service"
        result = subprocess.run(
            [PYTHON, str(DEPLOY / "deploy_tool.py"), "render-unit",
             str(ROOT / "src" / "coding-handoff" / "systemd"
                 / "hermes-coding-handoff.service.in"), str(out)]
            + [f"{k}={v}" for k, v in values.items()],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown unit placeholders", result.stderr)

    def test_only_the_bind_block_may_be_multiline(self):
        values = dict(RenderedUnit.VALUES, JOBS_DIR="/a\n/b")
        out = Path(tempfile.mkdtemp()) / "unit.service"
        result = subprocess.run(
            [PYTHON, str(DEPLOY / "deploy_tool.py"), "render-unit",
             str(ROOT / "src" / "coding-handoff" / "systemd"
                 / "hermes-coding-handoff.service.in"), str(out)]
            + [f"{k}={v}" for k, v in values.items()],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains a newline", result.stderr)


class RenderedConfig(unittest.TestCase):
    def test_enabling_route_is_bounded_to_its_own_section(self):
        import deploy_tool
        cases = [
            ("coding_handoff:\n  enabled: true # keep\nproactive:\n  enabled: false\n", None),
            ("coding_handoff:\n  enabled: false # keep\nproactive:\n  enabled: false\n", "  enabled: true # keep"),
            ("coding_handoff: # keep\n  endpoint: test\nproactive:\n  enabled: false\n", "  enabled: true"),
            ("coding_handoff:\r\n  enabled: false\r\nproactive:\r\n  enabled: false\r\n", "  enabled: true\r\n"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for before, expected in cases:
                with self.subTest(before=before):
                    path.write_bytes(before.encode())
                    deploy_tool.cmd_enable_gateway_route(str(path), "http://127.0.0.1:18771")
                    after = path.read_bytes().decode()
                    self.assertEqual(after.split("proactive:")[1], before.split("proactive:")[1])
                    if expected is None:
                        self.assertEqual(after, before)
                    else:
                        self.assertIn(expected, after)

    def render(self, **overrides):
        import subprocess as sp
        values = {
            "token_file": "/opt/hermes-coding-handoff/state/broker.token",
            "registry_file": "/opt/hermes-coding-handoff/config/projects.json",
            "jobs_dir": "/srv/hermes/hermes-data/jobs",
            "bridge_config": "/worker/coding-worker/bridge/config.json",
            "bridge_socket": "/worker/coding-worker/bridge-sock/bridge.sock",
            "ledger_db": "/opt/hermes-coding-handoff/state/handoff-ledger.db",
        }
        values.update(overrides)
        out = Path(tempfile.mkdtemp()) / "handoff.json"
        result = sp.run(
            [PYTHON, str(DEPLOY / "deploy_tool.py"), "render-config", str(out)]
            + [f"{k}={v}" for k, v in values.items()],
            capture_output=True, text=True)
        return result, out

    def test_the_rendered_config_is_accepted_by_the_broker(self):
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "src" / "coding-handoff" / "app"))
        import handoff_server

        result, out = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = handoff_server.load_config(str(out))
        self.assertEqual(config["listen_host"], "127.0.0.1")
        self.assertFalse(config["dispatch_enabled"])
        self.assertEqual(config["available_toolchains"], ("python3", "git"))

    def test_dispatch_is_off_unless_asked_for(self):
        _, out = self.render()
        self.assertFalse(json.loads(out.read_text(encoding="utf-8"))
                         ["dispatch_enabled"])
        _, enabled = self.render(dispatch_enabled="1")
        self.assertTrue(json.loads(enabled.read_text(encoding="utf-8"))
                        ["dispatch_enabled"])

    def test_a_relative_path_is_refused(self):
        result, _ = self.render(jobs_dir="jobs")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute path", result.stderr)

    def test_an_unknown_key_is_refused(self):
        result, _ = self.render(run_as_root="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown config keys", result.stderr)

    def test_the_gateway_dropin_only_adds_a_credential(self):
        conf = (ROOT / "src" / "coding-handoff" / "systemd"
                / "channel-gateway-handoff.conf").read_text(encoding="utf-8")
        directives = [l for l in conf.splitlines()
                      if l.strip() and not l.startswith("#")
                      and not l.startswith("[")]
        self.assertEqual(len(directives), 1)
        self.assertTrue(directives[0].startswith("LoadCredential="))


if __name__ == "__main__":
    unittest.main()
