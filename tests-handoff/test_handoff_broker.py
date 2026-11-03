#!/usr/bin/env python3
"""J.1 unit tests — the Coding Handoff Broker.

The broker is the component that can actually create a job, so the claims
tested here are about what it refuses:

  1. A registry that would let a chat turn choose something dangerous — a
     fixture agent, an undeclared toolchain, an ambiguous name, a repository
     path — is refused at load time, not at dispatch time.
  2. Readiness is specific and fails closed. An unmeasured fact is not a pass.
  3. An approval binds exact bytes. A specification edited after the owner read
     it cannot be dispatched under their approval.
  4. One authorization id produces at most one job, across retries and crashes.
  5. The job prompt contains the reviewed specification and nothing else.

No network, no docker, no supervisor: every external fact is injected.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import handoff_core as core
import handoff_ledger as ledger
import handoff_registry as registry
import handoff_server as server
import handoff_spec as spec

VALID_PROJECT = {
    "repository": "coding-worker-smoke-test",
    "agent": "pi-sandbox-hermes-commandcode",
    "spec_dir": "/opt/hermes-coding-handoff/specs/demo",
    "aliases": ["Demo"],
    "tests_command": "python3 -m unittest discover -s tests -v",
    "allowed_paths": ["pkg/", "tests/"],
    "required_toolchains": ["python3", "git"],
    "enabled": True,
    "acceptance_criteria": [
        {"id": "c1", "description": "module exists",
         "verification": {"kind": "file_exists", "params": {"path": "pkg/x.py"}}},
        {"id": "c2", "description": "tests pass",
         "verification": {"kind": "test_command", "params": {}}},
    ],
}


def registry_doc(**overrides):
    entry = dict(VALID_PROJECT)
    entry.update(overrides)
    return {"version": 1, "projects": {"demo": entry}}


class RegistryValidation(unittest.TestCase):
    def test_a_valid_entry_parses(self):
        reg = registry.parse_registry(registry_doc())
        project = reg.get("demo")
        self.assertEqual(project.repository, "coding-worker-smoke-test")
        self.assertEqual(project.required_toolchains, ("python3", "git"))

    def test_alias_resolution_is_case_insensitive_and_exact(self):
        reg = registry.parse_registry(registry_doc())
        self.assertIsNotNone(reg.resolve("DEMO"))
        self.assertIsNotNone(reg.resolve("demo"))
        self.assertIsNone(reg.resolve("demo-2"))
        self.assertIsNone(reg.resolve(""))

    def test_a_fixture_agent_is_refused(self):
        for agent in sorted(registry.FIXTURE_AGENTS):
            with self.subTest(agent=agent), \
                    self.assertRaises(registry.RegistryError) as caught:
                registry.parse_registry(registry_doc(agent=agent))
            self.assertIn("fixture agent", str(caught.exception))

    def test_an_unknown_agent_is_refused(self):
        with self.assertRaises(registry.RegistryError):
            registry.parse_registry(registry_doc(agent="pi-sandbox-unicorn"))

    def test_a_repository_path_is_not_a_repository_id(self):
        with self.assertRaises(registry.RegistryError):
            registry.parse_registry(
                registry_doc(repository="/opt/data/coding-worker/repos/x"))

    def test_undeclared_toolchains_are_refused(self):
        with self.assertRaises(registry.RegistryError) as caught:
            registry.parse_registry(registry_doc(required_toolchains=[]))
        self.assertIn("python3 and git only", str(caught.exception))

    def test_write_scope_must_stay_inside_the_repository(self):
        for bad in (["/etc/"], ["../elsewhere/"], [""]):
            with self.subTest(paths=bad), \
                    self.assertRaises(registry.RegistryError):
                registry.parse_registry(registry_doc(allowed_paths=bad))

    def test_acceptance_criteria_are_mandatory(self):
        with self.assertRaises(registry.RegistryError) as caught:
            registry.parse_registry(registry_doc(acceptance_criteria=[]))
        self.assertIn("never be accepted or refused", str(caught.exception))

    def test_an_unknown_verification_kind_is_refused(self):
        bad = [{"id": "c1", "description": "d",
                "verification": {"kind": "vibes", "params": {}}}]
        with self.assertRaises(registry.RegistryError):
            registry.parse_registry(registry_doc(acceptance_criteria=bad))

    def test_a_test_criterion_requires_a_test_command(self):
        with self.assertRaises(registry.RegistryError) as caught:
            registry.parse_registry(registry_doc(tests_command=None))
        self.assertIn("test_command criterion requires", str(caught.exception))

    def test_duplicate_criterion_ids_are_refused(self):
        dup = [dict(VALID_PROJECT["acceptance_criteria"][0]),
               dict(VALID_PROJECT["acceptance_criteria"][0])]
        with self.assertRaises(registry.RegistryError):
            registry.parse_registry(registry_doc(acceptance_criteria=dup))

    def test_an_ambiguous_name_across_projects_is_refused(self):
        doc = registry_doc()
        doc["projects"]["other"] = dict(VALID_PROJECT, aliases=["Demo"])
        with self.assertRaises(registry.RegistryError) as caught:
            registry.parse_registry(doc)
        self.assertIn("ambiguous name", str(caught.exception))

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(registry.RegistryError):
            registry.parse_registry(registry_doc(run_anything=True))

    def test_public_scope_never_leaks_host_paths(self):
        scope = registry.parse_registry(registry_doc()).get("demo").public_scope()
        self.assertNotIn("spec_dir", scope)
        self.assertNotIn("/opt/", json.dumps(scope))

    def test_only_enabled_projects_are_advertised(self):
        reg = registry.parse_registry(registry_doc(enabled=False))
        self.assertEqual(reg.enabled_refs(), [])
        reg = registry.parse_registry(registry_doc())
        self.assertEqual(reg.enabled_refs(),
                         [{"key": "demo", "aliases": ["Demo"]}])

    def test_the_shipped_sample_registry_is_valid(self):
        sample = (Path(__file__).resolve().parents[1]
                  / "src" / "coding-handoff" / "config" / "projects.sample.json")
        reg = registry.load_registry(sample)
        self.assertIn("fastrawphotoview", reg.projects)
        self.assertIn("handoff-protocol-check", reg.projects)

    def test_the_shipped_fastrawphotoview_entry_keeps_the_agreed_stack(self):
        sample = (Path(__file__).resolve().parents[1]
                  / "src" / "coding-handoff" / "config" / "projects.sample.json")
        project = registry.load_registry(sample).get("fastrawphotoview")
        # The product direction is Tauri + Rust. The entry must declare that
        # honestly rather than being trimmed to whatever the image can build.
        self.assertIn("cargo", project.required_toolchains)
        self.assertIn("rustc", project.required_toolchains)
        self.assertIn("node", project.required_toolchains)


class SpecRevisions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def write(self, name, text):
        path = Path(self.dir) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_no_revisions_is_not_an_error(self):
        self.assertEqual(spec.list_revisions(self.dir), [])
        self.assertIsNone(spec.latest_revision(self.dir))

    def test_latest_revision_wins(self):
        self.write("0001-first.md", "# First\n\nalpha text\n")
        self.write("0002-second.md", "# Second\n\nbeta text\n")
        latest = spec.latest_revision(self.dir)
        self.assertEqual(latest.revision, 2)
        self.assertEqual(latest.title, "Second")

    def test_non_revision_files_are_ignored_not_fatal(self):
        self.write("0001-first.md", "# First\n\nalpha\n")
        self.write("notes.txt", "scratch")
        self.write("draft.md", "not numbered")
        self.assertEqual(len(spec.list_revisions(self.dir)), 1)

    def test_an_empty_revision_is_refused(self):
        self.write("0001-empty.md", "   \n")
        with self.assertRaises(spec.SpecError):
            spec.list_revisions(self.dir)

    def test_the_label_identifies_exact_bytes(self):
        self.write("0001-first.md", "# First\n\nalpha\n")
        revision = spec.latest_revision(self.dir)
        self.assertRegex(revision.label, r"^rev\.0001 \([0-9a-f]{8}\)$")

    def test_editing_a_revision_changes_its_hash(self):
        self.write("0001-first.md", "# First\n\nalpha\n")
        before = spec.latest_revision(self.dir)
        self.write("0001-first.md", "# First\n\nalpha and more\n")
        after = spec.latest_revision(self.dir)
        self.assertEqual(before.revision, after.revision)
        self.assertNotEqual(before.sha256, after.sha256)

    def test_reading_a_changed_revision_under_an_old_hash_is_refused(self):
        self.write("0001-first.md", "# First\n\nalpha\n")
        pinned = spec.latest_revision(self.dir)
        self.write("0001-first.md", "# First\n\nsomething else entirely\n")
        with self.assertRaises(spec.SpecError) as caught:
            spec.read_spec_text(pinned)
        self.assertIn("no longer matches", str(caught.exception))

    def test_scope_digest_covers_the_scope_and_the_spec(self):
        self.write("0001-first.md", "# First\n\nalpha\n")
        revision = spec.latest_revision(self.dir)
        project = registry.parse_registry(registry_doc()).get("demo")
        base = spec.scope_digest(project.public_scope(), revision)

        wider = registry.parse_registry(
            registry_doc(allowed_paths=["pkg/", "tests/", "deploy/"])).get("demo")
        self.assertNotEqual(base, spec.scope_digest(wider.public_scope(), revision))

        other_agent = registry.parse_registry(
            registry_doc(agent="pi-sandbox-codex")).get("demo")
        self.assertNotEqual(base,
                            spec.scope_digest(other_agent.public_scope(), revision))

        self.write("0001-first.md", "# First\n\nrewritten\n")
        self.assertNotEqual(
            base, spec.scope_digest(project.public_scope(),
                                    spec.latest_revision(self.dir)))

    def test_the_shipped_fastrawphotoview_spec_is_concrete(self):
        specs = (Path(__file__).resolve().parents[1] / "src" / "coding-handoff"
                 / "specs" / "fastrawphotoview")
        revision = spec.latest_revision(specs)
        self.assertIsNotNone(revision)
        text = spec.read_spec_text(revision)
        # The agreed product, not a skeleton: transports, format, stack,
        # the three-pane layout, import and the licence obligation.
        for required in ("SMB", "FTP", "NEF", "Tauri", "Rust",
                         "thumbnail", "import", "LICENSES.md",
                         "rating", "preview"):
            with self.subTest(required=required):
                self.assertIn(required.lower(), text.lower())
        self.assertGreater(len(text), 2000)


class Preflight(unittest.TestCase):
    def project(self, **overrides):
        return registry.parse_registry(registry_doc(**overrides)).get("demo")

    def revision(self):
        return spec.SpecRevision(revision=1, name="0001-x.md", path="/tmp/x",
                                 sha256="a" * 64, size_bytes=10, title="t",
                                 summary="s")

    def ready_facts(self, **overrides):
        base = dict(
            supervisor_repo={"test_commands": [VALID_PROJECT["tests_command"]],
                             "allowed_paths": ["pkg/", "tests/"]},
            # The bridge's copy is what actually enforces `git_scope`, so
            # readiness requires it to name the same scope as the proposal.
            bridge_repo={"path": "/somewhere/repos/demo",
                         "allowed_paths": ["pkg/", "tests/"],
                         "test_commands": [VALID_PROJECT["tests_command"]]},
            bridge_agents=("hermes-commandcode-glm53flash",),
            agent_map={"pi-sandbox-hermes-commandcode":
                       "hermes-commandcode-glm53flash"},
            repo_path_exists=True,
            repo_has_head=True,
            spec_revision=self.revision(),
            worker_online=True,
            active_job=None,
            available_toolchains=("python3", "git"),
        )
        base.update(overrides)
        return core.Facts(**base)

    def codes(self, result):
        return {m["code"] for m in result.missing}

    def test_a_fully_ready_project_is_ready(self):
        result = core.preflight(self.project(), self.ready_facts())
        self.assertTrue(result.ready, result.missing)

    def test_unmeasured_facts_never_read_as_a_pass(self):
        result = core.preflight(self.project(), core.Facts())
        self.assertFalse(result.ready)
        self.assertIn(core.WORKER_OFFLINE, self.codes(result))
        self.assertIn(core.SPEC_MISSING, self.codes(result))
        self.assertIn(core.SUPERVISOR_ALLOWLIST, self.codes(result))

    def test_an_unborn_repository_is_reported_as_such(self):
        result = core.preflight(self.project(),
                                self.ready_facts(repo_has_head=False))
        self.assertFalse(result.ready)
        self.assertIn(core.REPO_UNBORN, self.codes(result))
        detail = [m["detail"] for m in result.missing
                  if m["code"] == core.REPO_UNBORN][0]
        self.assertIn("initial commit", detail)

    def test_a_missing_toolchain_is_a_provisioning_requirement(self):
        project = self.project(required_toolchains=["python3", "git", "cargo",
                                                    "rustc", "node", "npm"])
        result = core.preflight(project, self.ready_facts())
        self.assertFalse(result.ready)
        self.assertIn(core.TOOLCHAIN_MISSING, self.codes(result))
        detail = [m["detail"] for m in result.missing
                  if m["code"] == core.TOOLCHAIN_MISSING][0]
        for tool in ("cargo", "rustc", "node", "npm"):
            self.assertIn(tool, detail)

    def test_both_allowlists_are_required(self):
        self.assertIn(core.SUPERVISOR_ALLOWLIST,
                      self.codes(core.preflight(
                          self.project(), self.ready_facts(supervisor_repo=None))))
        self.assertIn(core.BRIDGE_ALLOWLIST,
                      self.codes(core.preflight(
                          self.project(), self.ready_facts(bridge_repo=None))))

    def test_a_wider_registry_scope_than_the_supervisor_registered(self):
        project = self.project(allowed_paths=["pkg/", "tests/", "deploy/"])
        result = core.preflight(project, self.ready_facts())
        self.assertIn(core.PATHS_NOT_REGISTERED, self.codes(result))

    def test_a_scope_the_worker_would_not_enforce_is_refused(self):
        """The approved scope has to be the *effective* scope.

        `git_scope` is evaluated by the bridge against its own registry, so a
        bridge entry permitting more than the proposal showed would accept
        changes the owner never approved — and widening it afterwards would not
        change the proposal's digest.
        """
        facts = self.ready_facts(
            bridge_repo={"path": "/somewhere/repos/demo",
                         "allowed_paths": ["pkg/", "tests/", "private/"]})
        result = core.preflight(self.project(), facts)
        self.assertFalse(result.ready)
        self.assertIn(core.EFFECTIVE_SCOPE_MISMATCH, self.codes(result))

    def test_a_bridge_entry_without_a_scope_is_refused(self):
        facts = self.ready_facts(bridge_repo={"path": "/somewhere/repos/demo"})
        self.assertIn(core.EFFECTIVE_SCOPE_MISMATCH,
                      self.codes(core.preflight(self.project(), facts)))

    def test_a_test_command_the_bridge_does_not_know_is_refused(self):
        facts = self.ready_facts(
            bridge_repo={"path": "/somewhere/repos/demo",
                         "allowed_paths": ["pkg/", "tests/"],
                         "test_commands": ["make check"]})
        self.assertIn(core.TESTS_NOT_ALLOWLISTED,
                      self.codes(core.preflight(self.project(), facts)))

    def test_an_unallowlisted_test_command(self):
        facts = self.ready_facts(
            supervisor_repo={"test_commands": ["make check"],
                             "allowed_paths": ["pkg/", "tests/"]})
        self.assertIn(core.TESTS_NOT_ALLOWLISTED,
                      self.codes(core.preflight(self.project(), facts)))

    def test_an_agent_the_bridge_has_never_heard_of(self):
        facts = self.ready_facts(bridge_agents=("fixture",))
        self.assertIn(core.AGENT_UNAVAILABLE,
                      self.codes(core.preflight(self.project(), facts)))

    def test_a_busy_worker_is_not_a_failure_but_is_not_ready(self):
        facts = self.ready_facts(active_job="job_aaaaaaaaaa")
        result = core.preflight(self.project(), facts)
        self.assertFalse(result.ready)
        self.assertIn(core.WORKER_BUSY, self.codes(result))

    def test_a_disabled_project_is_never_ready(self):
        result = core.preflight(self.project(enabled=False), self.ready_facts())
        self.assertIn(core.DISABLED, self.codes(result))


class StatusProjection(unittest.TestCase):
    def test_only_allowlisted_fields_are_exposed(self):
        view = core.status_view({
            "job_id": "job_0123456789", "status": "RUNNING", "goal": "SECRET SPEC",
            "prompt_path": "/opt/data/jobs/prompts/x.txt",
            "acceptance_criteria": [{"id": "a"}, {"id": "b"}],
            "acceptance_results": [{"id": "a", "status": "PASS"},
                                   {"id": "b", "status": "FAIL"}],
        }, project_key="demo")
        self.assertEqual(view["acceptance_pass"], 1)
        self.assertEqual(view["acceptance_total"], 2)
        self.assertNotIn("goal", view)
        self.assertNotIn("prompt_path", view)
        self.assertEqual(view["project_key"], "demo")

    def test_the_persisted_row_wins_over_the_dispatch_claim(self):
        outcome = core.dispatch_outcome(
            {"job_id": "job_0123456789", "status": "DISPATCHING"},
            {"job_id": "job_0123456789", "status": "WAITING_HUMAN",
             "failure_reason": "worker unconfigured"})
        self.assertEqual(outcome["status"], "WAITING_HUMAN")
        self.assertEqual(outcome["failure_reason"], "worker unconfigured")


class Ledger(unittest.TestCase):
    AUTH = "gw_abc12345"
    DIGEST = "a" * 64

    def setUp(self):
        self.con = ledger.open_db(":memory:")
        self.addCleanup(self.con.close)
        self.job_id = ledger.deterministic_job_id(self.AUTH, "demo", self.DIGEST)

    def claim(self, **overrides):
        args = dict(conversation_id="c", project_key="demo",
                    scope_digest=self.DIGEST, job_id=self.job_id)
        args.update(overrides)
        auth = args.pop("authorization_id", self.AUTH)
        return ledger.claim(self.con, auth, **args)

    def test_the_job_id_is_derived_and_well_formed(self):
        self.assertRegex(self.job_id, ledger.JOB_ID_RE)
        self.assertEqual(
            self.job_id,
            ledger.deterministic_job_id(self.AUTH, "demo", self.DIGEST))
        self.assertNotEqual(
            self.job_id,
            ledger.deterministic_job_id(self.AUTH, "other", self.DIGEST))
        self.assertNotEqual(
            self.job_id,
            ledger.deterministic_job_id(self.AUTH, "demo", "b" * 64))

    def test_first_caller_wins_and_the_job_id_is_recorded_immediately(self):
        outcome, row, epoch = self.claim()
        self.assertEqual(outcome, ledger.WON)
        self.assertIsNone(row)
        self.assertEqual(epoch, 1)
        # Recorded before any job exists: this is what recovery reads.
        self.assertEqual(ledger.get(self.con, self.AUTH)["job_id"], self.job_id)

    def test_a_completed_authorization_replays(self):
        _, _, epoch = self.claim()
        ledger.settle(self.con, self.AUTH, epoch=epoch, state=ledger.CREATED)
        outcome, row, _ = self.claim()
        self.assertEqual(outcome, ledger.DONE)
        self.assertEqual(row["job_id"], self.job_id)

    def test_a_fresh_claim_in_flight_blocks_a_duplicate(self):
        self.claim()
        outcome, row, _ = self.claim()
        self.assertEqual(outcome, ledger.IN_PROGRESS)
        self.assertEqual(row["state"], ledger.CLAIMING)

    def test_a_stale_claim_is_retaken_under_a_new_epoch(self):
        _, _, first = self.claim()
        later = (datetime.now(timezone.utc)
                 + timedelta(seconds=ledger.STALE_CLAIM_SECONDS + 5))
        outcome, row, second = self.claim(now=later)
        self.assertEqual(outcome, ledger.WON)
        self.assertGreater(second, first)
        self.assertEqual(row["job_id"], self.job_id)

    def test_a_fenced_writer_cannot_settle_after_being_retaken(self):
        _, _, first = self.claim()
        later = (datetime.now(timezone.utc)
                 + timedelta(seconds=ledger.STALE_CLAIM_SECONDS + 5))
        _, _, second = self.claim(now=later)
        # The original writer wakes up and tries to record its result.
        self.assertFalse(ledger.settle(self.con, self.AUTH, epoch=first,
                                       state=ledger.CREATED))
        self.assertTrue(ledger.settle(self.con, self.AUTH, epoch=second,
                                      state=ledger.CREATED))
        self.assertEqual(ledger.get(self.con, self.AUTH)["state"],
                         ledger.CREATED)

    def test_a_refusal_may_be_retried_under_a_new_epoch(self):
        _, _, epoch = self.claim()
        ledger.settle(self.con, self.AUTH, epoch=epoch, state=ledger.REFUSED,
                      detail="not ready")
        outcome, _, retry_epoch = self.claim()
        self.assertEqual(outcome, ledger.WON)
        self.assertGreater(retry_epoch, epoch)

    def test_the_same_id_with_a_different_request_conflicts(self):
        self.claim()
        for changed in ({"project_key": "other"},
                        {"scope_digest": "b" * 64},
                        {"conversation_id": "elsewhere"}):
            with self.subTest(changed=changed):
                outcome, _, _ = self.claim(**changed)
                self.assertEqual(outcome, ledger.CONFLICT)

    def test_distinct_authorizations_are_independent(self):
        self.assertEqual(self.claim()[0], ledger.WON)
        other = ledger.deterministic_job_id("gw_two12345", "demo", self.DIGEST)
        self.assertEqual(
            self.claim(authorization_id="gw_two12345", job_id=other)[0],
            ledger.WON)

    def test_only_jobs_this_broker_created_are_recognised(self):
        self.claim()
        self.assertIsNotNone(ledger.job_is_ours(self.con, self.job_id))
        self.assertIsNone(ledger.job_is_ours(self.con, "job_ffffffffff"))

    def test_latest_for_project_includes_an_in_flight_claim(self):
        self.claim()
        # The id is already durable, so "being created" is a truer answer than
        # "nothing here".
        self.assertEqual(
            ledger.latest_for_project(self.con, "demo")["job_id"], self.job_id)


class GoalIsolation(unittest.TestCase):
    """The one thing that reaches the Worker."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "0001-thing.md"
        self.path.write_text("# Thing\n\nBuild the thing properly.\n",
                             encoding="utf-8")
        self.addCleanup(lambda: (self.path.unlink(), os.rmdir(self.dir)))

    def test_the_goal_is_the_specification_and_the_registered_scope(self):
        project = registry.parse_registry(registry_doc()).get("demo")
        revision = spec.latest_revision(self.dir)
        goal = server.build_goal(project, revision)
        self.assertIn("Build the thing properly.", goal)
        self.assertIn("rev.0001", goal)
        self.assertIn("pkg/, tests/", goal)
        # Nothing from a channel, a person or a credential.
        for forbidden in ("discord", "conversation", "owner", "envelope",
                          "authenticated", "token", "session"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, goal.lower())

    def test_the_goal_states_the_real_toolchain_limits(self):
        project = registry.parse_registry(registry_doc()).get("demo")
        goal = server.build_goal(project, spec.latest_revision(self.dir))
        self.assertIn("no network access", goal)


class BrokerConfig(unittest.TestCase):
    def config(self, **overrides):
        base = {
            "listen_host": "127.0.0.1", "listen_port": 18771,
            "token_file": "/x/token", "registry_file": "/x/projects.json",
            "jobs_dir": "/x/jobs", "bridge_config": "/x/bridge.json",
            "ledger_db": "/x/ledger.db",
            "available_toolchains": ["python3", "git"],
        }
        base.update(overrides)
        return base

    def write(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        json.dump(data, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_valid_config_loads_with_dispatch_off_by_default(self):
        cfg = server.load_config(self.write(self.config()))
        self.assertFalse(cfg["dispatch_enabled"])

    def test_a_non_loopback_bind_is_refused(self):
        with self.assertRaises(server.ConfigError):
            server.load_config(self.write(self.config(listen_host="0.0.0.0")))

    def test_an_empty_toolchain_list_is_refused(self):
        with self.assertRaises(server.ConfigError):
            server.load_config(self.write(self.config(available_toolchains=[])))

    def test_unknown_config_keys_are_refused(self):
        with self.assertRaises(server.ConfigError):
            server.load_config(self.write(self.config(run_as_root=True)))

    def test_a_missing_required_path_is_refused(self):
        with self.assertRaises(server.ConfigError):
            server.load_config(self.write(self.config(ledger_db="")))


class FakeBroker:
    def __init__(self):
        self.cfg = {"dispatch_enabled": True}
        self.seen = []

    def registry(self):
        class _R:
            @staticmethod
            def enabled_refs():
                return [{"key": "demo", "aliases": []}]
        return _R()

    def preflight(self, key):
        self.seen.append(("preflight", key))
        return {"project_key": key, "ready": True}

    def authorize(self, **kwargs):
        self.seen.append(("authorize", kwargs))
        return {"job_id": "job_0123456789", "status": "RUNNING"}

    def status(self, **kwargs):
        self.seen.append(("status", kwargs))
        return {"job_id": kwargs.get("job_id"), "status": "RUNNING"}


class RequestSurface(unittest.TestCase):
    """The typed vocabulary, without standing up a socket."""

    def setUp(self):
        self.broker = FakeBroker()
        self.handler = server.Handler.__new__(server.Handler)
        self.handler.broker = self.broker

    def dispatch(self, path, body):
        return self.handler._dispatch(path, body)

    def test_unknown_action_is_refused(self):
        self.assertEqual(self.dispatch("/handoff/anything", {}),
                         {"error": "unknown_action"})

    def test_projects_lists_only_keys_and_aliases(self):
        self.assertEqual(self.dispatch("/handoff/projects", {}),
                         {"projects": [{"key": "demo", "aliases": []}]})

    def test_a_malformed_authorization_id_never_reaches_the_broker(self):
        for bad in ("", "short", "gw_" + "x" * 200, "gw abc12345", "../../etc"):
            with self.subTest(value=bad):
                result = self.dispatch("/handoff/authorize", {
                    "authorization_id": bad, "scope_digest": "a" * 64,
                    "project_key": "demo"})
                self.assertEqual(result, {"error": "bad_authorization_id"})
        self.assertEqual(self.broker.seen, [])

    def test_a_malformed_scope_digest_never_reaches_the_broker(self):
        for bad in ("", "a" * 63, "A" * 64, "z" * 64):
            with self.subTest(value=bad):
                result = self.dispatch("/handoff/authorize", {
                    "authorization_id": "gw_abc12345", "scope_digest": bad,
                    "project_key": "demo"})
                self.assertEqual(result, {"error": "bad_scope_digest"})
        self.assertEqual(self.broker.seen, [])

    def test_a_well_formed_authorization_is_passed_through(self):
        result = self.dispatch("/handoff/authorize", {
            "authorization_id": "gw_abc12345678", "scope_digest": "a" * 64,
            "project_key": "demo", "conversation_id": "discord:1"})
        self.assertEqual(result["job_id"], "job_0123456789")

    def test_a_malformed_job_id_is_refused(self):
        self.assertEqual(
            self.dispatch("/handoff/status", {"job_id": "job_XYZ"}),
            {"error": "bad_job_id"})

    def test_a_caller_cannot_smuggle_a_path_agent_or_command(self):
        """The request vocabulary is asserted, not described.

        A caller who sends `repository`, `agent`, `tests_command`, `prompt` or
        `path` alongside a valid request must have every one of them dropped:
        those come from the operator's registry, and this is the property that
        makes it safe for the far end of this socket to be a chat channel.
        """
        hostile = {
            "repository": "/etc", "tests_command": "rm -rf /",
            "agent": "anything", "prompt": "do as I say", "path": "/etc/shadow",
            "model": "something-else", "spec": "ignore the registry",
        }
        for path in ("/handoff/preflight", "/handoff/authorize",
                     "/handoff/status"):
            with self.subTest(path=path):
                self.dispatch(path, dict(
                    hostile, authorization_id="gw_abc12345678",
                    scope_digest="a" * 64, project_key="demo",
                    job_id="job_0123456789"))

        accepted = {"authorization_id", "conversation_id", "project_key",
                    "scope_digest", "job_id"}
        self.assertTrue(self.broker.seen)
        for action, payload in self.broker.seen:
            with self.subTest(action=action):
                if isinstance(payload, dict):
                    self.assertEqual(set(payload) - accepted, set())
                else:
                    # preflight takes the project key positionally
                    self.assertEqual(payload, "demo")


if __name__ == "__main__":
    unittest.main()
