#!/usr/bin/env python3
"""H2: the canonical changed-file set = committed diff vs base_sha UNION
working-tree changes. An agent commit (clean tree) must stay visible to
scope validation and secret scan."""

import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import Sandbox, make_fixture_repo, run_git

import bridge             # noqa: E402
import supervisor         # noqa: E402
import validator          # noqa: E402
import workerctl_server   # noqa: E402

SECRET_BODY = 'API_KEY = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"\n'


def _commit_secret_file(repo):
    """Simulate an agent that modifies a secret-like file and COMMITS it,
    leaving the working tree clean."""
    p = repo / "pkg" / "leaked.py"
    p.write_text(SECRET_BODY, encoding="utf-8")
    run_git(repo, "add", "pkg/leaked.py")
    run_git(repo, "commit", "-q", "-m", "agent commit")
    return "pkg/leaked.py"


def _base_sha(repo):
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


class TestSupervisorCanonicalSet(unittest.TestCase):
    def test_committed_change_detected_on_clean_tree(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            f = _commit_secret_file(repo)
            self.assertEqual(run_git(repo, "status", "--porcelain").stdout, "")
            changed = supervisor.changed_files_since(str(repo), base)
            self.assertIn(f, changed)

    def test_untracked_and_staged_detected(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            (repo / "untracked.txt").write_text("u", encoding="utf-8")
            (repo / "staged.txt").write_text("s", encoding="utf-8")
            run_git(repo, "add", "staged.txt")
            changed = supervisor.changed_files_since(str(repo), base)
            self.assertIn("untracked.txt", changed)
            self.assertIn("staged.txt", changed)

    def test_rename_and_quoted_paths_via_porcelain_z(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            # rename + a path needing quoting (space) — NUL parsing must cope
            run_git(repo, "mv", "README.md", "docs read me.md")
            changed = supervisor.changed_files_since(str(repo), base)
            self.assertIn("docs read me.md", changed)
            self.assertNotIn("README.md", changed)

    def test_git_failure_fails_closed_not_empty(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            # a bogus base sha makes the diff fail -> must raise, not []
            with self.assertRaises(supervisor.GitStateUnknown):
                supervisor.changed_files_since(str(repo), "0" * 40)

    def test_secret_scan_sees_committed_change(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            f = _commit_secret_file(repo)
            changed = supervisor.changed_files_since(str(repo), base)
            hits = supervisor.secret_scan(str(repo), changed)
            self.assertTrue(hits, "secret scan must see the committed change")
            self.assertIn("credential_assignment", hits)

    def test_scope_validator_sees_committed_change(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            f = _commit_secret_file(repo)
            changed = supervisor.changed_files_since(str(repo), base)
            ok, detail = validator._check_git_scope(
                str(repo), {"allowed_paths": ["pkg/", "tests/"]}, changed)
            self.assertTrue(ok, f"pkg/leaked.py is in scope: {detail}")
            # out-of-scope committed file must be flagged
            (repo / "outside.txt").write_text("x", encoding="utf-8")
            run_git(repo, "add", "outside.txt")
            run_git(repo, "commit", "-q", "-m", "outside")
            changed2 = supervisor.changed_files_since(str(repo), base)
            ok2, detail2 = validator._check_git_scope(
                str(repo), {"allowed_paths": ["pkg/", "tests/"]}, changed2)
            self.assertFalse(ok2)
            self.assertIn("outside.txt", detail2)


class TestBridgeCanonicalSet(unittest.TestCase):
    def test_bridge_detects_committed_change_on_clean_tree(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            f = _commit_secret_file(repo)
            self.assertEqual(
                run_git(repo, "status", "--porcelain").stdout.strip(), "")
            changed = bridge.canonical_changed_files(str(repo), base)
            self.assertIn(f, changed)

    def test_bridge_secret_scan_sees_committed_change(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            _commit_secret_file(repo)
            changed = bridge.canonical_changed_files(str(repo), base)
            hits = bridge.secret_scan(str(repo), changed)
            self.assertIn("credential_assignment", hits)

    def test_bridge_scope_criterion_uses_canonical_set(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            (repo / "esc.txt").write_text("x", encoding="utf-8")
            run_git(repo, "add", "esc.txt")
            run_git(repo, "commit", "-q", "-m", "escape")
            changed = bridge.canonical_changed_files(str(repo), base)
            res = bridge.evaluate_criteria(
                [{"id": "s", "description": "d",
                  "verification": {"kind": "git_scope", "params": {}}}],
                {"allowed_paths": ["pkg/", "tests/"]}, str(repo),
                changed_files=changed)
            self.assertEqual(res[0]["status"], "FAIL")
            self.assertIn("esc.txt", res[0]["detail"])


class TestWorkerctlCanonicalSet(unittest.TestCase):
    def test_workerctl_detects_committed_change_on_clean_tree(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            base = _base_sha(repo)
            f = _commit_secret_file(repo)
            changed = workerctl_server.canonical_changed_files(str(repo), base)
            self.assertIn(f, changed)

    def test_workerctl_git_failure_fails_closed(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            with self.assertRaises(RuntimeError):
                workerctl_server.canonical_changed_files(str(repo), "0" * 40)


if __name__ == "__main__":
    unittest.main()
