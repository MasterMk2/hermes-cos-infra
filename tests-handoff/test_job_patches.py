#!/usr/bin/env python3
"""J.1 — the two job-layer transformations.

`jobs_db.py` and `transport.py` are not installed by copying. The deployed
copies are logically identical to the committed ones but not byte-identical:
they use CRLF, and `transport.py` carries the operator's own resolved bridge
path where the repository carries a placeholder. Copying the repository file
over that would reinstate a path that does not exist on the host and break the
existing container consumers.

So they are transformed in place. The claims tested here:

  1. applying a transformation to the committed file produces **exactly** the
     repository's own edited copy — so the deployed change and the reviewed
     change cannot drift apart
  2. every line outside the anchored region survives verbatim, including a line
     holding a host-specific path
  3. CRLF input produces CRLF output
  4. content the package was not prepared against is refused
  5. applying twice changes nothing

No real path from any host appears in this file; the preservation test uses an
invented one.
"""

import hashlib
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "coding-handoff" / "deploy"))

import deploy_tool  # noqa: E402

BASE_REVISION = "cf7a647"

# Invented, and shaped like the thing that must survive: a resolved absolute
# path in the fallback list. Nothing here is anybody's real directory.
INVENTED_HOST_PATH = "/home/example-operator/coding-worker/bridge-sock/bridge.sock"


def in_git_work_tree() -> bool:
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, cwd=str(ROOT))
    return result.returncode == 0 and result.stdout.strip() == "true"


def committed(path: str) -> bytes:
    """The file at the base revision.

    Outside a repository this is a limitation of where the test runs, and it
    skips. *Inside* one, a missing base revision means the equivalence between
    the reviewed edit and the deployed transformation cannot be checked at all,
    which is a failure rather than an inconvenience.
    """
    if not in_git_work_tree():
        raise unittest.SkipTest("not a git work tree; run this in a clone")
    result = subprocess.run(["git", "show", f"{BASE_REVISION}:{path}"],
                            capture_output=True, cwd=str(ROOT))
    if result.returncode != 0:
        raise AssertionError(
            f"{BASE_REVISION}:{path} is not reachable in this repository")
    return result.stdout


def lf(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n")


class Equivalence(unittest.TestCase):
    """The transformation and the reviewed edit are the same change."""

    def check(self, kind, path):
        base = committed(path)
        patched = deploy_tool.apply_job_patch(kind, base)
        current = (ROOT / path).read_bytes()
        self.assertEqual(
            lf(patched).decode("utf-8"), lf(current).decode("utf-8"),
            f"{kind} does not reproduce {path}")

    def test_create_job_id_reproduces_the_repository_file(self):
        self.check("create-job-id", "src/jobs/jobs_db.py")

    def test_bridge_sock_env_reproduces_the_repository_file(self):
        self.check("bridge-sock-env", "src/jobs/transport.py")


class Preservation(unittest.TestCase):
    def test_a_host_specific_fallback_line_survives(self):
        base = committed("src/jobs/transport.py").decode("utf-8")
        # Stand in for the deployed file: the placeholder already resolved.
        live = base.replace(
            '    "/home/<PI_USER>/coding-worker/bridge-sock/bridge.sock",',
            f'    "{INVENTED_HOST_PATH}",')
        self.assertIn(INVENTED_HOST_PATH, live)

        patched = deploy_tool.apply_job_patch(
            "bridge-sock-env", live.encode("utf-8")).decode("utf-8")

        self.assertIn(INVENTED_HOST_PATH, patched,
                      "the deployed fallback must be preserved")
        self.assertNotIn("<PI_USER>", patched,
                         "the placeholder must not be reintroduced")
        self.assertIn("HERMES_CODING_BRIDGE_SOCK", patched)

    def test_only_the_anchored_region_changes(self):
        base = committed("src/jobs/jobs_db.py").decode("utf-8")
        patched = deploy_tool.apply_job_patch(
            "create-job-id", base.encode("utf-8")).decode("utf-8")
        before = base.splitlines()
        after = set(patched.splitlines())
        lost = [line for line in before if line not in after]
        # Exactly the signature lines and the one `jid = …` line the change
        # rewrites. Nothing else may disappear.
        self.assertLessEqual(len(lost), 4, lost)
        for line in lost:
            self.assertTrue(
                "create_job(" in line or "tests_command=None):" in line
                or "jid = new_id" in line or "import secrets" in line, line)

    def test_the_public_signature_stays_backward_compatible(self):
        base = committed("src/jobs/jobs_db.py").decode("utf-8")
        patched = deploy_tool.apply_job_patch(
            "create-job-id", base.encode("utf-8")).decode("utf-8")
        # Every existing keyword survives; only an optional one is added.
        for keyword in ("goal", "repository", "acceptance_criteria", "agent=None",
                        "project_id=None", "task_id=None", "parent_job_id=None",
                        'kind="implementation"', 'priority="normal"',
                        "max_attempts=6", "tests_command=None"):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, patched)
        self.assertIn("job_id=None", patched)


class LineEndings(unittest.TestCase):
    def test_crlf_input_produces_crlf_output(self):
        base = lf(committed("src/jobs/jobs_db.py")).replace(b"\n", b"\r\n")
        patched = deploy_tool.apply_job_patch("create-job-id", base)
        self.assertIn(b"\r\n", patched)
        # Every newline is a CRLF: no line was left with a bare LF, including
        # the ones the transformation inserted.
        self.assertEqual(patched.count(b"\n"), patched.count(b"\r\n"))
        self.assertIn(b"JOB_ID_RE = re.compile", patched)

    def test_lf_input_stays_lf(self):
        base = lf(committed("src/jobs/transport.py"))
        patched = deploy_tool.apply_job_patch("bridge-sock-env", base)
        self.assertNotIn(b"\r\n", patched)


class Idempotence(unittest.TestCase):
    def test_applying_twice_is_a_no_op(self):
        for kind, path in (("create-job-id", "src/jobs/jobs_db.py"),
                           ("bridge-sock-env", "src/jobs/transport.py")):
            with self.subTest(kind=kind):
                once = deploy_tool.apply_job_patch(kind, committed(path))
                twice = deploy_tool.apply_job_patch(kind, once)
                self.assertEqual(once, twice)


class Refusal(unittest.TestCase):
    def run_tool(self, kind, source, out, expect):
        return subprocess.run(
            [sys.executable,
             str(ROOT / "src" / "coding-handoff" / "deploy" / "deploy_tool.py"),
             "patch-jobs", kind, str(source), str(out), expect],
            capture_output=True, text=True)

    def test_unexpected_content_is_refused(self):
        import tempfile
        source = Path(tempfile.mkdtemp()) / "jobs_db.py"
        source.write_bytes(committed("src/jobs/jobs_db.py"))
        result = self.run_tool("create-job-id", source, source.with_suffix(".out"),
                               "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to patch", result.stderr)

    def test_a_missing_anchor_is_refused(self):
        import tempfile
        source = Path(tempfile.mkdtemp()) / "transport.py"
        source.write_bytes(b"# nothing like the real file\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        result = self.run_tool("bridge-sock-env", source,
                               source.with_suffix(".out"), digest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("anchor not found", result.stderr)

    def test_an_unknown_patch_is_refused(self):
        import tempfile
        source = Path(tempfile.mkdtemp()) / "x.py"
        source.write_bytes(b"x = 1\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        result = self.run_tool("do-whatever", source, source.with_suffix(".out"),
                               digest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown patch", result.stderr)


class Privacy(unittest.TestCase):
    def test_no_host_path_is_embedded_in_the_patch_definitions(self):
        source = (ROOT / "src" / "coding-handoff" / "deploy"
                  / "deploy_tool.py").read_text(encoding="utf-8")
        # A home directory literal in a tracked file is the failure mode this
        # whole transformation approach exists to avoid.
        self.assertNotRegex(source, r"/home/(?!<)[A-Za-z0-9._-]+/")
        self.assertNotRegex(source, r"/Users/[A-Za-z0-9._-]+/")
        self.assertNotRegex(source, r"[Cc]:\\\\Users\\\\")

    def test_this_test_file_embeds_no_real_path_either(self):
        text = Path(__file__).read_text(encoding="utf-8")
        self.assertIn("example-operator", text)
        for pattern in (r"/Users/[A-Za-z0-9._-]+/", r"[Cc]:\\\\Users\\\\"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))


if __name__ == "__main__":
    unittest.main()
