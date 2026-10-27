#!/usr/bin/env python3
"""J.1 onboarding tests.

Registering a project touches the two allowlists the whole worker is gated on,
so the properties that matter are about what it must never do:

  * lose an entry that was already there
  * leave a half-written allowlist behind
  * invent a path, an agent or a test command
  * touch a network

The bootstrap is exercised against real git repositories in a temporary
directory. Nothing here has a remote, and no test performs a network operation.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "coding-handoff" / "deploy"))

import onboard_project as onboard  # noqa: E402

EXISTING_FIXTURES = ("coding-worker-smoke-test", "fix-loop-fixture",
                     "f2-fixloop", "f2-loop2", "f2-loop3")


def supervisor_doc():
    return {
        "worker": {"kind": "local-fixture-or-pi-sandbox", "enabled": True},
        "default_agent": None,
        "production_agents": ["pi-sandbox-hermes-commandcode",
                              "pi-sandbox-claude-code"],
        "fixture_agents": ["simulated"],
        "repositories": {
            name: {"path": f"/opt/data/coding-worker/repos/{name}",
                   "test_commands": ["python3 -m unittest discover -s tests -v"],
                   "allowed_paths": ["pkg/", "tests/", "README.md"],
                   "allow_simulated": True}
            for name in EXISTING_FIXTURES
        },
    }


def bridge_doc():
    return {
        "image": "hermes-coding-worker:1.0.1-hermes-commandcode",
        "agents": {"fixture": "x", "hermes-commandcode-glm53flash": "y"},
        "repositories": {
            name: {"path": f"/worker/coding-worker/repos/{name}",
                   "test_commands": ["python3 -m unittest discover -s tests -v"]}
            for name in EXISTING_FIXTURES
        },
    }


class OnboardFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="onboard-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        self.supervisor = self.root / "worker_config.json"
        self.bridge = self.root / "bridge-config.json"
        self.supervisor.write_text(json.dumps(supervisor_doc(), indent=2),
                                   encoding="utf-8")
        self.bridge.write_text(json.dumps(bridge_doc(), indent=2),
                               encoding="utf-8")

        self.spec_dir = self.root / "specs" / "fastrawphotoview"
        self.spec_dir.mkdir(parents=True)
        shutil.copyfile(
            ROOT / "src" / "coding-handoff" / "specs" / "fastrawphotoview"
            / "0001-nas-raw-browser-foundation.md",
            self.spec_dir / "0001-nas-raw-browser-foundation.md")

        self.checkout = self.root / "repos" / "fastrawphotoview"
        self.desc = dict(onboard.TEMPLATE)
        self.desc.update({
            "supervisor_config": str(self.supervisor),
            "bridge_config": str(self.bridge),
            "spec_dir": str(self.spec_dir),
            "bridge_repo_path": str(self.checkout),
            "supervisor_repo_path": "/opt/data/coding-worker/repos/fastrawphotoview",
        })
        self.config = self.root / "desc.json"
        self.write_desc()

    def write_desc(self, **overrides):
        data = dict(self.desc)
        data.update(overrides)
        self.config.write_text(json.dumps(data), encoding="utf-8")
        return data

    def load(self):
        return onboard.load_description(str(self.config))

    def read(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))


class Description(OnboardFixture):
    def test_the_template_is_not_usable_unedited(self):
        self.config.write_text(json.dumps(onboard.TEMPLATE), encoding="utf-8")
        with self.assertRaises(onboard.OnboardError) as caught:
            self.load()
        self.assertIn("placeholder", str(caught.exception))

    def test_relative_paths_are_refused(self):
        self.write_desc(bridge_repo_path="repos/frpv")
        with self.assertRaises(onboard.OnboardError):
            self.load()

    def test_traversal_is_refused(self):
        self.write_desc(supervisor_repo_path="/opt/data/../../etc/passwd")
        with self.assertRaises(onboard.OnboardError):
            self.load()

    def test_absolute_write_scope_is_refused(self):
        self.write_desc(allowed_paths=["/etc/"])
        with self.assertRaises(onboard.OnboardError):
            self.load()

    def test_unknown_keys_are_refused(self):
        data = dict(self.desc)
        data["run_as"] = "root"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(onboard.OnboardError):
            self.load()

    def test_an_agent_outside_production_agents_is_refused(self):
        self.write_desc(agent="pi-sandbox-unicorn")
        with self.assertRaises(onboard.OnboardError) as caught:
            onboard.plan(self.load())
        self.assertIn("production_agents", str(caught.exception))


class Plan(OnboardFixture):
    def test_plan_changes_nothing(self):
        before = (self.supervisor.read_bytes(), self.bridge.read_bytes())
        onboard.plan(self.load())
        self.assertEqual(
            before, (self.supervisor.read_bytes(), self.bridge.read_bytes()))

    def test_plan_reports_both_registries_and_the_checkout(self):
        result = onboard.plan(self.load())
        self.assertFalse(result["supervisor"]["already_present"])
        self.assertFalse(result["bridge"]["already_present"])
        self.assertFalse(result["checkout"]["has_commit"])
        self.assertEqual(sorted(result["supervisor"]["existing_entries"]),
                         sorted(EXISTING_FIXTURES))

    def test_the_two_entries_agree(self):
        result = onboard.plan(self.load())
        supervisor = result["supervisor"]["new_entry"]
        bridge = result["bridge"]["new_entry"]
        self.assertEqual(supervisor["test_commands"], bridge["test_commands"])
        self.assertEqual(supervisor["allowed_paths"], bridge["allowed_paths"])
        # The paths differ by design: the supervisor registry is container-local
        # and the bridge registry is host-local.
        self.assertNotEqual(supervisor["path"], bridge["path"])

    def test_a_production_entry_is_never_simulated(self):
        entry = onboard.plan(self.load())["supervisor"]["new_entry"]
        self.assertFalse(entry["allow_simulated"])


class Apply(OnboardFixture):
    def test_all_five_existing_entries_survive(self):
        onboard.apply(self.load(), str(self.root / "backup"))
        for path in (self.supervisor, self.bridge):
            with self.subTest(path=path.name):
                repositories = self.read(path)["repositories"]
                for name in EXISTING_FIXTURES:
                    self.assertIn(name, repositories)
                self.assertIn("fastrawphotoview", repositories)
                self.assertEqual(len(repositories), len(EXISTING_FIXTURES) + 1)

    def test_unrelated_top_level_keys_survive(self):
        onboard.apply(self.load(), str(self.root / "backup"))
        supervisor = self.read(self.supervisor)
        self.assertEqual(supervisor["production_agents"],
                         supervisor_doc()["production_agents"])
        self.assertIn("worker", supervisor)
        self.assertEqual(self.read(self.bridge)["image"], bridge_doc()["image"])

    def test_applying_twice_is_idempotent(self):
        onboard.apply(self.load(), str(self.root / "b1"))
        first = self.supervisor.read_text(encoding="utf-8")
        onboard.apply(self.load(), str(self.root / "b2"))
        self.assertEqual(first, self.supervisor.read_text(encoding="utf-8"))

    def test_the_backup_holds_both_originals(self):
        backup = self.root / "backup"
        onboard.apply(self.load(), str(backup))
        saved = sorted(p.name for p in backup.iterdir())
        self.assertEqual(len(saved), 3)
        self.assertIn("postimages.json", saved)
        self.assertTrue(any(n.startswith("supervisor-") for n in saved))
        self.assertTrue(any(n.startswith("bridge-") for n in saved))

    def test_rollback_restores_both_registries(self):
        original = (self.supervisor.read_text(encoding="utf-8"),
                    self.bridge.read_text(encoding="utf-8"))
        backup = self.root / "backup"
        onboard.apply(self.load(), str(backup))
        self.assertIn("fastrawphotoview", self.read(self.supervisor)["repositories"])

        onboard.rollback(str(backup), self.load())
        for path in (self.supervisor, self.bridge):
            with self.subTest(path=path.name):
                self.assertNotIn("fastrawphotoview",
                                 self.read(path)["repositories"])
        self.assertEqual(json.loads(original[0]), self.read(self.supervisor))
        self.assertEqual(json.loads(original[1]), self.read(self.bridge))

    def test_the_file_mode_is_preserved(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are not meaningful on this platform")
        os.chmod(self.supervisor, 0o640)
        onboard.apply(self.load(), str(self.root / "backup"))
        self.assertEqual(self.supervisor.stat().st_mode & 0o777, 0o640)

    def test_no_temporary_file_is_left_behind(self):
        onboard.apply(self.load(), str(self.root / "backup"))
        leftovers = [p.name for p in self.root.iterdir()
                     if p.name.startswith(".onboard-")]
        self.assertEqual(leftovers, [])


class ConcurrentApply(OnboardFixture):
    def test_second_registry_edit_is_preserved_and_first_is_restored(self):
        from unittest.mock import patch
        original = self.read(self.supervisor)
        write = onboard.write_json_atomic
        changed = dict(self.read(self.bridge), operator_change=True)
        def interleaved(path, data, **kwargs):
            write(path, data, **kwargs)
            if path == str(self.supervisor) and "fastrawphotoview" in data["repositories"]:
                self.bridge.write_text(json.dumps(changed), encoding="utf-8")
        with patch.object(onboard, "write_json_atomic", side_effect=interleaved):
            with self.assertRaises(onboard.OnboardError):
                onboard.apply(self.load(), str(self.root / "backup"))
        self.assertEqual(self.read(self.bridge), changed)
        self.assertEqual(self.read(self.supervisor), original)

    def test_edit_to_first_registry_is_not_clobbered_during_rollback(self):
        from unittest.mock import patch
        write = onboard.write_json_atomic
        changed = dict(self.read(self.supervisor), operator_change=True)
        def interleaved(path, data, **kwargs):
            if path == str(self.bridge):
                self.supervisor.write_text(json.dumps(changed), encoding="utf-8")
                raise OSError("injected second write failure")
            return write(path, data, **kwargs)
        with patch.object(onboard, "write_json_atomic", side_effect=interleaved):
            with self.assertRaises(onboard.OnboardError):
                onboard.apply(self.load(), str(self.root / "backup"))
        self.assertEqual(self.read(self.supervisor), changed)

    def test_a_second_transaction_cannot_acquire_the_same_registries(self):
        with onboard.registry_locks(self.load()):
            with self.assertRaises(onboard.OnboardError):
                onboard.apply(self.load(), str(self.root / "backup"))
        self.assertFalse(list(self.root.glob("*.onboard.lock")))

    def test_later_operator_edit_blocks_manual_rollback(self):
        backup = self.root / "backup"
        onboard.apply(self.load(), str(backup))
        changed = dict(self.read(self.bridge), operator_change=True)
        self.bridge.write_text(json.dumps(changed), encoding="utf-8")
        before = self.supervisor.read_bytes()
        with self.assertRaises(onboard.OnboardError):
            onboard.rollback(str(backup), self.load())
        self.assertEqual(self.supervisor.read_bytes(), before)
        self.assertEqual(self.read(self.bridge), changed)


class Bootstrap(OnboardFixture):
    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git is not available")

    def head_of(self, path):
        return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True)

    def test_an_absent_checkout_gets_one_clean_commit(self):
        result = onboard.bootstrap(self.load())
        self.assertTrue(result["bootstrapped"])
        self.assertEqual(self.head_of(self.checkout).returncode, 0)
        self.assertTrue((self.checkout / "README.md").is_file())

    def test_the_commit_carries_neutral_author_metadata(self):
        onboard.bootstrap(self.load())
        shown = subprocess.run(
            ["git", "-C", str(self.checkout), "log", "-1", "--format=%an%n%ae%n%cn%n%ce"],
            capture_output=True, text=True)
        lines = shown.stdout.strip().splitlines()
        self.assertEqual(lines, [onboard.COMMIT_NAME, onboard.COMMIT_EMAIL,
                                 onboard.COMMIT_NAME, onboard.COMMIT_EMAIL])

    def test_the_bootstrap_ships_the_specification_and_nothing_else(self):
        onboard.bootstrap(self.load())
        listed = subprocess.run(
            ["git", "-C", str(self.checkout), "ls-files"],
            capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(listed),
                         ["README.md",
                          "docs/0001-nas-raw-browser-foundation.md"])
        # No application substitute, no licence choice, no dependency manifest.
        for forbidden in ("Cargo.toml", "package.json", "LICENSE",
                          "LICENSES.md", "src-tauri"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, listed)

    def test_the_specification_committed_is_the_approved_one(self):
        onboard.bootstrap(self.load())
        committed = (self.checkout / "docs"
                     / "0001-nas-raw-browser-foundation.md").read_text(
            encoding="utf-8")
        self.assertIn("Tauri", committed)
        self.assertIn("SMB", committed)
        self.assertIn("NEF", committed)

    def test_no_remote_is_configured(self):
        onboard.bootstrap(self.load())
        remotes = subprocess.run(["git", "-C", str(self.checkout), "remote"],
                                 capture_output=True, text=True)
        self.assertEqual(remotes.stdout.strip(), "")

    def test_bootstrapping_twice_does_not_add_a_commit(self):
        onboard.bootstrap(self.load())
        first = self.head_of(self.checkout).stdout
        second = onboard.bootstrap(self.load())
        self.assertFalse(second["bootstrapped"])
        self.assertEqual(first, self.head_of(self.checkout).stdout)

    def test_a_missing_specification_refuses_rather_than_committing_nothing(self):
        for path in self.spec_dir.iterdir():
            path.unlink()
        with self.assertRaises(onboard.OnboardError):
            onboard.bootstrap(self.load())
        self.assertFalse(self.checkout.exists()
                         and (self.checkout / ".git").exists()
                         and self.head_of(self.checkout).returncode == 0)

    def test_the_bootstrap_makes_the_repository_dispatchable(self):
        """The live blocker: an unborn repository cannot be branched from."""
        self.assertFalse(onboard.has_commit(str(self.checkout)))
        onboard.bootstrap(self.load())
        self.assertTrue(onboard.has_commit(str(self.checkout)))
        worktree = self.root / "wt"
        added = subprocess.run(
            ["git", "-C", str(self.checkout), "worktree", "add", "-b",
             "hermes/job-test", str(worktree)],
            capture_output=True, text=True)
        self.assertEqual(added.returncode, 0, added.stderr)


class NoNetwork(unittest.TestCase):
    def test_the_module_performs_no_network_operation(self):
        source = (ROOT / "src" / "coding-handoff" / "deploy"
                  / "onboard_project.py").read_text(encoding="utf-8")
        body = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        for forbidden in ("git clone", "git push", "git fetch", "git pull",
                          "gh repo", "urlopen", "requests.", "http://",
                          "https://"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_terminal_prompts_are_disabled(self):
        source = (ROOT / "src" / "coding-handoff" / "deploy"
                  / "onboard_project.py").read_text(encoding="utf-8")
        self.assertIn("GIT_TERMINAL_PROMPT", source)
        self.assertIn("GIT_ASKPASS", source)


if __name__ == "__main__":
    unittest.main()
