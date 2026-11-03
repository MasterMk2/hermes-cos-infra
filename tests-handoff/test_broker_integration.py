#!/usr/bin/env python3
"""J.1 integration tests — the real Broker against the real job database.

The unit suite exercises the broker's pieces. It did not exercise
`Broker.authorize`, and two duplicate-job bugs lived comfortably underneath a
passing suite as a result. So this file wires up the actual object:

  * the real `jobs_db` module, copied into a temporary directory, creating a
    real SQLite database with the real schema and the real `job_id` primary key
  * a fake `supervisor` that records dispatches instead of spawning runners
  * a real registry, real specification files and a real ledger on disk

and then does the things that actually happen in production: the same message
arriving twice, two turns racing, a process dying between creating a job and
recording that it did, and an operator editing the registry at the exact
moment an approval is being executed.

No network, no docker, no runner, no sends.
"""

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import handoff_ledger as ledger
import handoff_server as server
import handoff_spec as spec

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_SUPERVISOR = '''\
"""Test double: records dispatch calls, spawns nothing."""
import jobs_db

DISPATCHED = []
BEHAVIOUR = {"status": "RUNNING"}


def dispatch_job(job_id):
    DISPATCHED.append(job_id)
    if BEHAVIOUR.get("raise"):
        raise RuntimeError(BEHAVIOUR["raise"])
    job = jobs_db.get_job(job_id)
    if not job:
        return {"error": "not_found", "job_id": job_id}
    if job["status"] in ("RUNNING", "DISPATCHING"):
        return {"error": "already_active", "job_id": job_id,
                "status": job["status"]}
    jobs_db.transition(job_id, BEHAVIOUR.get("status", "RUNNING"))
    return {"job_id": job_id, "status": BEHAVIOUR.get("status", "RUNNING")}
'''

SPEC_TEXT = """# Demo specification

Build the demo module and its tests. Stay inside the registered write scope.
"""

CRITERIA = [
    {"id": "c1", "description": "module exists",
     "verification": {"kind": "file_exists", "params": {"path": "pkg/x.py"}}},
]


def registry_doc(**overrides):
    entry = {
        "repository": "coding-worker-smoke-test",
        "agent": "pi-sandbox-hermes-commandcode",
        "spec_dir": None,          # filled in by the fixture
        "aliases": ["Demo"],
        "tests_command": "python3 -m unittest discover -s tests -v",
        # Mirrors production: the project, the supervisor registry and the
        # bridge registry all name the same write scope. The readiness gate
        # requires them to agree, because the bridge's copy is what actually
        # enforces `git_scope`.
        "allowed_paths": ["pkg/", "tests/", "README.md"],
        "required_toolchains": ["python3", "git"],
        "enabled": True,
        "acceptance_criteria": [dict(c) for c in CRITERIA],
    }
    entry.update(overrides)
    return {"version": 1, "projects": {"demo": entry}}


class BrokerFixture(unittest.TestCase):
    """A broker with real persistence and a fake dispatcher."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handoff-it-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        # Real job layer: the canonical jobs_db, with its real schema.
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir()
        shutil.copy(REPO_ROOT / "src" / "jobs" / "jobs_db.py",
                    self.jobs_dir / "jobs_db.py")
        shutil.copy(REPO_ROOT / "src" / "jobs" / "transport.py",
                    self.jobs_dir / "transport.py")
        (self.jobs_dir / "supervisor.py").write_text(FAKE_SUPERVISOR,
                                                     encoding="utf-8")
        (self.jobs_dir / "worker_config.json").write_text(json.dumps({
            "repositories": {
                "coding-worker-smoke-test": {
                    "path": "/opt/data/coding-worker/repos/coding-worker-smoke-test",
                    "test_commands": ["python3 -m unittest discover -s tests -v"],
                    "allowed_paths": ["pkg/", "tests/", "README.md"],
                }
            }
        }), encoding="utf-8")

        # A real repository with a real HEAD, in the host namespace the bridge
        # registry uses. Created with git so the preflight check is real.
        self.repo = self.root / "repos" / "coding-worker-smoke-test"
        self.repo.mkdir(parents=True)
        self._git_init(self.repo)

        self.bridge_config = self.root / "bridge-config.json"
        self.bridge_config.write_text(json.dumps({
            "repositories": {
                "coding-worker-smoke-test": {
                    "path": str(self.repo),
                    "allowed_paths": ["pkg/", "tests/", "README.md"],
                    "test_commands": [
                        "python3 -m unittest discover -s tests -v"]}},
            "agents": {"fixture": "x", "hermes-commandcode-glm53flash": "y"},
        }), encoding="utf-8")

        self.spec_dir = self.root / "specs" / "demo"
        self.spec_dir.mkdir(parents=True)
        (self.spec_dir / "0001-demo.md").write_text(SPEC_TEXT, encoding="utf-8")

        self.registry_file = self.root / "projects.json"
        self.write_registry()

        self.cfg = {
            "listen_host": "127.0.0.1", "listen_port": 18771,
            "token_file": str(self.root / "token"),
            "registry_file": str(self.registry_file),
            "jobs_dir": str(self.jobs_dir),
            "bridge_config": str(self.bridge_config),
            "bridge_socket": "",
            "ledger_db": str(self.root / "ledger.db"),
            "available_toolchains": ("python3", "git"),
            "dispatch_enabled": True,
        }
        self.broker = server.Broker(self.cfg)
        # The worker bridge is not running in a test; report it online so the
        # readiness gate under test is the one this file is about.
        self.broker.worker_state = lambda: (True, None)

        for name in ("jobs_db", "supervisor", "transport"):
            sys.modules.pop(name, None)
            self.addCleanup(sys.modules.pop, name, None)
        sys.path.insert(0, str(self.jobs_dir))
        self.addCleanup(self._drop_path)

    def _drop_path(self):
        while str(self.jobs_dir) in sys.path:
            sys.path.remove(str(self.jobs_dir))

    def _git_init(self, path):
        import subprocess
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "PATH": __import__("os").environ.get("PATH", "")}
        subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
        (path / "README.md").write_text("demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"],
                       check=True, env=env)

    def write_registry(self, **overrides):
        doc = registry_doc(spec_dir=str(self.spec_dir), **overrides)
        self.registry_file.write_text(json.dumps(doc), encoding="utf-8")

    # -- helpers ------------------------------------------------------

    def digest(self):
        return self.broker.preflight("demo")["scope_digest"]

    def authorize(self, auth_id="gw_authorization1", digest=None):
        return self.broker.authorize(
            authorization_id=auth_id, conversation_id="discord:1",
            project_key="demo", scope_digest=digest or self.digest())

    def jobs_module(self):
        import jobs_db
        return jobs_db

    def all_jobs(self):
        jobs_db = self.jobs_module()
        jobs_db.init_db()
        return jobs_db.list_jobs(limit=100)

    def supervisor_module(self):
        import supervisor
        return supervisor


class HappyPath(BrokerFixture):
    def test_preflight_is_ready_and_binds_a_digest(self):
        pre = self.broker.preflight("demo")
        self.assertTrue(pre["ready"], pre.get("missing"))
        self.assertRegex(pre["scope_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(pre["spec"]["revision"], 1)

    def test_authorize_creates_exactly_one_job_and_dispatches_it(self):
        result = self.authorize()
        self.assertNotIn("error", result)
        self.assertRegex(result["job_id"], ledger.JOB_ID_RE)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(len(self.all_jobs()), 1)
        self.assertEqual(self.supervisor_module().DISPATCHED, [result["job_id"]])

    def test_the_job_goal_is_the_specification_only(self):
        result = self.authorize()
        job = self.jobs_module().get_job(result["job_id"])
        self.assertIn("Build the demo module", job["goal"])
        # Nothing from the conversation, the owner or the transport.
        for forbidden in ("discord:1", "gw_authorization1", "owner",
                          "conversation", "token"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, job["goal"].lower())

    def test_the_job_carries_the_registered_scope_not_a_caller_choice(self):
        job = self.jobs_module().get_job(self.authorize()["job_id"])
        self.assertEqual(job["agent"], "pi-sandbox-hermes-commandcode")
        self.assertEqual(job["repository"], "coding-worker-smoke-test")
        self.assertEqual(job["tests_command"],
                         "python3 -m unittest discover -s tests -v")
        self.assertEqual(job["project_id"], "demo")


class Replay(BrokerFixture):
    def test_the_same_authorization_twice_yields_one_job(self):
        first = self.authorize()
        second = self.authorize()
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second.get("replayed"))
        self.assertEqual(len(self.all_jobs()), 1)

    def test_a_replay_reports_the_current_status_not_a_stale_one(self):
        first = self.authorize()
        self.jobs_module().transition(first["job_id"], "WAITING_HUMAN")
        second = self.authorize()
        self.assertEqual(second["status"], "WAITING_HUMAN")

    def test_a_replay_survives_the_project_becoming_not_ready(self):
        first = self.authorize()
        # The worker goes busy after the job was dispatched.
        self.broker.worker_state = lambda: (True, "job_ffffffffff")
        second = self.authorize()
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertNotIn("error", second)

    def test_a_replay_survives_the_specification_changing(self):
        # A redelivery carries the digest the gateway stored, which is the
        # digest of what the owner approved — not a freshly computed one.
        digest = self.digest()
        first = self.authorize(digest=digest)
        (self.spec_dir / "0002-next.md").write_text(
            "# Next\n\nSomething else entirely.\n", encoding="utf-8")
        second = self.authorize(digest=digest)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(self.all_jobs()), 1)

    def test_a_replay_survives_the_registry_widening(self):
        digest = self.digest()
        first = self.authorize(digest=digest)
        self.write_registry(allowed_paths=["pkg/", "tests/"])
        second = self.authorize(digest=digest)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["job_id"], first["job_id"])

    def test_the_same_id_with_a_different_scope_conflicts(self):
        self.authorize()
        clash = self.broker.authorize(
            authorization_id="gw_authorization1", conversation_id="discord:1",
            project_key="demo", scope_digest="b" * 64)
        self.assertEqual(clash["error"], "authorization_conflict")
        self.assertEqual(len(self.all_jobs()), 1)


class CrashWindows(BrokerFixture):
    def test_unstarted_recovery_respects_disabled_dispatch(self):
        digest = self.digest()
        self.supervisor_module().BEHAVIOUR["raise"] = "process died"
        with self.assertRaises(RuntimeError):
            self.authorize(digest=digest)
        self.supervisor_module().BEHAVIOUR.pop("raise")
        self.broker.cfg["dispatch_enabled"] = False
        result = self.authorize(digest=digest)
        self.assertEqual(result["error"], "recovery_blocked")
        self.assertEqual(result["status"], "NEW")
        self.assertEqual(len(self.supervisor_module().DISPATCHED), 1)
        self.broker.cfg["dispatch_enabled"] = True
        recovered = self.authorize(digest=digest)
        self.assertEqual(recovered["status"], "RUNNING")
        self.assertEqual(len(self.all_jobs()), 1)

    def test_supervisor_scope_change_after_create_blocks_first_dispatch(self):
        from unittest.mock import patch
        jobs = self.jobs_module()
        original = jobs.create_job
        def create_then_change(**kwargs):
            result = original(**kwargs)
            config = self.jobs_dir / "worker_config.json"
            doc = json.loads(config.read_text())
            doc["repositories"]["coding-worker-smoke-test"]["allowed_paths"] = ["."]
            config.write_text(json.dumps(doc))
            return result
        with patch.object(jobs, "create_job", side_effect=create_then_change):
            result = self.authorize()
        self.assertEqual(result["error"], "recovery_blocked")
        self.assertEqual(result["status"], "NEW")
        self.assertEqual(self.supervisor_module().DISPATCHED, [])

    """A writer that dies between the durable steps."""

    def _stale(self, auth_id):
        """Age the claim so recovery is allowed to take it over."""
        con = ledger.open_db(self.cfg["ledger_db"])
        try:
            con.execute(
                "UPDATE authorizations SET updated_at = '2000-01-01T00:00:00+00:00'"
                " WHERE authorization_id = ?", (auth_id,))
            con.commit()
        finally:
            con.close()

    def test_a_crash_after_create_and_dispatch_does_not_create_a_second_job(self):
        # Die inside dispatch, after the job row exists.
        self.supervisor_module().BEHAVIOUR["raise"] = "process died"
        with self.assertRaises(RuntimeError):
            self.authorize()
        self.assertEqual(len(self.all_jobs()), 1)
        created = self.all_jobs()[0]["job_id"]

        self.supervisor_module().BEHAVIOUR.pop("raise")
        self._stale("gw_authorization1")
        recovered = self.authorize()

        self.assertEqual(recovered["job_id"], created)
        self.assertEqual(len(self.all_jobs()), 1)
        self.assertTrue(recovered.get("replayed"))

    def test_recovery_finishes_the_dispatch_the_dead_writer_never_did(self):
        self.supervisor_module().BEHAVIOUR["raise"] = "process died"
        with self.assertRaises(RuntimeError):
            self.authorize()
        job_id = self.all_jobs()[0]["job_id"]
        self.assertEqual(self.jobs_module().get_job(job_id)["status"], "NEW")

        self.supervisor_module().BEHAVIOUR.pop("raise")
        self._stale("gw_authorization1")
        recovered = self.authorize()
        self.assertEqual(recovered["status"], "RUNNING")
        self.assertEqual(len(self.all_jobs()), 1)

    def test_unstarted_recovery_rechecks_readiness_after_the_crash(self):
        digest = self.digest()
        self.supervisor_module().BEHAVIOUR["raise"] = "process died"
        with self.assertRaises(RuntimeError):
            self.authorize(digest=digest)
        self.supervisor_module().BEHAVIOUR.pop("raise")
        # The spec is edited and the worker goes busy before recovery runs.
        (self.spec_dir / "0001-demo.md").write_text(
            "# Demo\n\nRewritten after the crash.\n", encoding="utf-8")
        self.broker.worker_state = lambda: (True, "job_ffffffffff")
        self._stale("gw_authorization1")

        recovered = self.authorize(digest=digest)
        self.assertEqual(recovered["error"], "recovery_blocked")
        self.assertEqual(recovered["status"], "NEW")
        self.assertEqual(len(self.all_jobs()), 1)

    def test_a_fresh_claim_in_flight_is_reported_not_duplicated(self):
        con = ledger.open_db(self.cfg["ledger_db"])
        try:
            digest = self.digest()
            ledger.claim(con, "gw_authorization1", conversation_id="discord:1",
                         project_key="demo", scope_digest=digest,
                         job_id=ledger.deterministic_job_id(
                             "gw_authorization1", "demo", digest))
        finally:
            con.close()
        result = self.authorize()
        self.assertEqual(result["error"], "authorization_in_progress")
        self.assertEqual(self.all_jobs(), [])


class Concurrency(BrokerFixture):
    def test_two_simultaneous_duplicates_produce_one_job(self):
        digest = self.digest()
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            try:
                results.append(self.authorize(digest=digest))
            except Exception as exc:  # recorded, not swallowed
                results.append({"error": f"raised:{exc}"})

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.all_jobs()), 1,
                         f"expected one job, got {self.all_jobs()}")
        job_ids = {r.get("job_id") for r in results if r.get("job_id")}
        self.assertLessEqual(len(job_ids), 1)

    def test_two_distinct_authorizations_produce_two_jobs(self):
        digest = self.digest()
        first = self.authorize("gw_authorization1", digest)
        # The first job is RUNNING; the worker would refuse a second in
        # production, but the broker's job is to not conflate the two.
        second = self.authorize("gw_authorization2", digest)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.all_jobs()), 2)


class SnapshotRace(BrokerFixture):
    def test_a_registry_change_after_the_digest_is_refused(self):
        digest = self.digest()
        self.write_registry(allowed_paths=["pkg/", "tests/"])
        result = self.authorize(digest=digest)
        self.assertIn(result.get("error"),
                      {"scope_changed_since_proposal", "not_ready"})
        self.assertEqual(self.all_jobs(), [])

    def test_a_specification_edit_after_the_digest_is_refused(self):
        digest = self.digest()
        (self.spec_dir / "0001-demo.md").write_text(
            "# Demo\n\nA different instruction entirely.\n", encoding="utf-8")
        result = self.authorize(digest=digest)
        self.assertEqual(result.get("error"), "scope_changed_since_proposal")
        self.assertEqual(self.all_jobs(), [])

    def test_a_new_revision_after_the_digest_is_refused(self):
        digest = self.digest()
        (self.spec_dir / "0002-next.md").write_text(
            "# Next\n\nA second revision.\n", encoding="utf-8")
        result = self.authorize(digest=digest)
        self.assertEqual(result.get("error"), "scope_changed_since_proposal")
        self.assertEqual(self.all_jobs(), [])

    def test_the_registry_is_read_once_per_authorization(self):
        """The scope that is validated is the scope that executes.

        Swapping the registry the moment the snapshot is taken must not let a
        later read's agent or write scope run under the earlier read's digest.
        """
        digest = self.digest()
        original = self.broker.registry
        calls = {"n": 0}

        def swapping_registry():
            calls["n"] += 1
            if calls["n"] == 2:
                # Between validation and execution, the operator edits.
                self.write_registry(agent="pi-sandbox-codex")
            return original()

        self.broker.registry = swapping_registry
        result = self.authorize(digest=digest)
        if result.get("job_id"):
            job = self.jobs_module().get_job(result["job_id"])
            self.assertEqual(job["agent"], "pi-sandbox-hermes-commandcode")
        else:
            self.assertIn(result.get("error"),
                          {"scope_changed_since_proposal", "not_ready"})


class Readiness(BrokerFixture):
    def test_an_unborn_repository_blocks_everything(self):
        empty = self.root / "repos" / "empty"
        empty.mkdir(parents=True)
        import subprocess
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        self.bridge_config.write_text(json.dumps({
            "repositories": {"coding-worker-smoke-test": {
                "path": str(empty),
                "allowed_paths": ["pkg/", "tests/", "README.md"],
                "test_commands": ["python3 -m unittest discover -s tests -v"]}},
            "agents": {"hermes-commandcode-glm53flash": "y"},
        }), encoding="utf-8")

        pre = self.broker.preflight("demo")
        self.assertFalse(pre["ready"])
        self.assertIn("repository_has_no_commits",
                      {m["code"] for m in pre["missing"]})
        result = self.authorize(digest="c" * 64)
        self.assertEqual(result["error"], "not_ready")
        self.assertEqual(self.all_jobs(), [])

    def test_a_missing_toolchain_blocks_dispatch(self):
        self.write_registry(required_toolchains=["python3", "git", "cargo",
                                                 "rustc", "node", "npm"])
        pre = self.broker.preflight("demo")
        self.assertFalse(pre["ready"])
        self.assertIn("worker_toolchain_missing",
                      {m["code"] for m in pre["missing"]})
        self.assertEqual(self.all_jobs(), [])

    def test_dispatch_disabled_blocks_everything(self):
        self.cfg["dispatch_enabled"] = False
        pre = self.broker.preflight("demo")
        self.assertFalse(pre["ready"])
        self.assertIn("dispatch_disabled", {m["code"] for m in pre["missing"]})
        result = self.authorize(digest="c" * 64)
        self.assertEqual(result["error"], "not_ready")
        self.assertEqual(self.all_jobs(), [])

    def test_a_refusal_can_be_retried_once_the_blocker_clears(self):
        # The approved scope has not changed; only the broker's own gate has.
        # A redelivery after the gate opens must be able to proceed.
        digest = self.digest()
        self.cfg["dispatch_enabled"] = False
        self.assertEqual(self.authorize(digest=digest)["error"], "not_ready")
        self.assertEqual(self.all_jobs(), [])
        self.cfg["dispatch_enabled"] = True
        result = self.authorize(digest=digest)
        self.assertEqual(result.get("status"), "RUNNING")
        self.assertEqual(len(self.all_jobs()), 1)


class StatusReadback(BrokerFixture):
    def test_status_of_a_job_this_broker_created(self):
        job_id = self.authorize()["job_id"]
        status = self.broker.status(job_id=job_id)
        self.assertEqual(status["status"], "RUNNING")
        self.assertEqual(status["project_key"], "demo")
        self.assertNotIn("goal", status)

    def test_status_by_project_finds_the_latest_job(self):
        self.authorize("gw_authorization1")
        status = self.broker.status(project_key="demo")
        self.assertEqual(status["status"], "RUNNING")

    def test_a_job_created_outside_the_route_is_refused(self):
        jobs_db = self.jobs_module()
        jobs_db.init_db()
        outside = jobs_db.create_job(goal="operator work",
                                     repository="coding-worker-smoke-test",
                                     acceptance_criteria=CRITERIA)
        self.assertEqual(self.broker.status(job_id=outside)["error"],
                         "job_not_from_handoff")

    def test_status_by_authorization_id_answers_about_that_approval(self):
        result = self.authorize("gw_authorization1")
        status = self.broker.status(authorization_id="gw_authorization1")
        self.assertEqual(status["job_id"], result["job_id"])
        self.assertEqual(status["authorization_id"], "gw_authorization1")

    def test_an_unknown_authorization_is_definitively_unknown(self):
        """Distinct from "uncertain": nothing was ever recorded, so nothing was
        created and the owner can safely instruct again."""
        self.assertEqual(
            self.broker.status(authorization_id="gw_neverhappened")["error"],
            "authorization_unknown")

    def test_an_authorization_never_falls_back_to_an_older_job(self):
        """A project with earlier work must not have one of those jobs
        reported as the outcome of a different approval."""
        self.authorize("gw_authorization1")
        older = self.all_jobs()[0]["job_id"]
        answer = self.broker.status(authorization_id="gw_somethingelse1")
        self.assertEqual(answer["error"], "authorization_unknown")
        self.assertNotIn(older, str(answer))

    def test_a_recorded_authorization_without_a_job_is_uncertain(self):
        con = ledger.open_db(self.cfg["ledger_db"])
        try:
            digest = self.digest()
            ledger.claim(con, "gw_authorization1", conversation_id="discord:1",
                         project_key="demo", scope_digest=digest,
                         job_id=ledger.deterministic_job_id(
                             "gw_authorization1", "demo", digest))
        finally:
            con.close()
        answer = self.broker.status(authorization_id="gw_authorization1")
        self.assertNotIn("error", answer)
        self.assertEqual(answer["status"], "")
        self.assertTrue(answer["job_id"])

    def test_an_authorized_but_uncreated_job_is_not_reported_as_running(self):
        con = ledger.open_db(self.cfg["ledger_db"])
        try:
            digest = self.digest()
            ledger.claim(con, "gw_authorization1", conversation_id="discord:1",
                         project_key="demo", scope_digest=digest,
                         job_id=ledger.deterministic_job_id(
                             "gw_authorization1", "demo", digest))
        finally:
            con.close()
        status = self.broker.status(project_key="demo")
        self.assertNotEqual(status.get("status"), "RUNNING")


if __name__ == "__main__":
    unittest.main()
