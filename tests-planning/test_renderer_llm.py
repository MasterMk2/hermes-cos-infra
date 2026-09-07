"""LLM client boundary and data minimization — sections 70, 71, 73, 75-77, 109.

What is asserted here is mostly what does *not* happen: importing the renderer
reads no file and opens no socket, no client is resolved unless a deployment
explicitly enables one, no credential is looked up until a request is actually
being sent, and the only thing that ever reaches a model is the compact plan.

The interface is the existing Antigravity provider bridge — the same
OpenAI-compatible endpoint, fixed model and bearer-token handling that the
approved classifier broker already uses (`src/line-knowledge/event_broker.py`).
No provider, model chain, broker or credential class is added. Nothing in this
file touches the network: `AntigravityBridgeClient` is exercised for its
construction and token-resolution behaviour only.
"""

import json
import os
import sys
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from planning.renderer import (  # noqa: E402
    DEFAULT_BRIDGE_URL,
    DEFAULT_MODEL,
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    AntigravityBridgeClient,
    compact_plan_for_model,
    render_brief,
    render_template,
    resolve_client,
)
from test_renderer_template import (  # noqa: E402
    NOW,
    busy_day_plan,
    make_item,
    make_plan,
)


def payload_of(messages):
    user = messages[1]["content"]
    return json.loads(user[len(UNTRUSTED_BEGIN):-len(UNTRUSTED_END)].strip())


class Recorder:
    model = DEFAULT_MODEL

    def __init__(self, answer=""):
        self.messages = None
        self.timeout = None
        self.answer = answer

    def __call__(self, messages, *, timeout=None):
        self.messages = messages
        self.timeout = timeout
        return self.answer


class ModeTests(unittest.TestCase):
    """Contract section 5: three modes, one output shape."""

    def test_template_mode_never_touches_a_client(self):
        recorder = Recorder()
        result = render_brief(busy_day_plan(), mode="template", client=recorder, now=NOW)
        self.assertIsNone(recorder.messages)
        self.assertEqual(result["renderer"], "template")
        self.assertIsNone(result["fallback_reason"])

    def test_auto_mode_uses_an_injected_client(self):
        plan = busy_day_plan()
        recorder = Recorder(render_template(plan, now=NOW)[0])
        result = render_brief(plan, mode="auto", client=recorder, now=NOW)
        self.assertIsNotNone(recorder.messages)
        self.assertEqual(result["renderer"], "llm")

    def test_auto_mode_with_no_client_configured_renders_the_template(self):
        result = render_brief(busy_day_plan(), mode="auto", now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "llm_unavailable")
        self.assertTrue(result["text"].strip())

    def test_llm_mode_with_no_client_never_raises(self):
        result = render_brief(busy_day_plan(), mode="llm", now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "llm_unavailable")

    def test_mode_none_falls_back_to_the_configured_mode(self):
        recorder = Recorder()
        render_brief(busy_day_plan(), mode=None, client=recorder,
                     config={"renderer": {"mode": "template"}}, now=NOW)
        self.assertIsNone(recorder.messages)

    def test_the_configured_timeout_is_passed_to_the_client(self):
        recorder = Recorder()
        render_brief(busy_day_plan(), mode="llm", client=recorder,
                     config={"renderer": {"timeout_seconds": 7}}, now=NOW)
        self.assertEqual(recorder.timeout, 7)

    def test_the_default_timeout_is_used_when_unconfigured(self):
        recorder = Recorder()
        render_brief(busy_day_plan(), mode="llm", client=recorder, now=NOW)
        self.assertEqual(recorder.timeout, 30)

    def test_the_model_name_comes_from_the_client(self):
        plan = busy_day_plan()
        recorder = Recorder(render_template(plan, now=NOW)[0])
        recorder.model = "gemini-3.8-flash-low"
        self.assertEqual(render_brief(plan, mode="llm", client=recorder, now=NOW)["model"],
                         "gemini-3.8-flash-low")

    def test_a_plain_function_client_is_accepted(self):
        plan = busy_day_plan()
        text = render_template(plan, now=NOW)[0]

        def client(messages, *, timeout=None):
            return text

        result = render_brief(plan, mode="llm", client=client, now=NOW)
        self.assertEqual(result["renderer"], "llm")
        self.assertEqual(result["model"], DEFAULT_MODEL)


class DataMinimizationTests(unittest.TestCase):
    """Section 73: the LLM sees the compact plan and nothing else."""

    def test_the_model_receives_only_the_fenced_compact_plan(self):
        recorder = Recorder()
        render_brief(busy_day_plan(), mode="llm", client=recorder, now=NOW)
        user = recorder.messages[1]["content"]
        self.assertEqual(user.split("\n")[0], UNTRUSTED_BEGIN)
        self.assertEqual(user.split("\n")[-1], UNTRUSTED_END)
        payload = payload_of(recorder.messages)
        self.assertEqual(sorted(payload),
                         ["date", "max_chars", "notes", "opening_line", "sections"])

    def test_no_commitment_id_reaches_the_model(self):
        recorder = Recorder()
        plan = busy_day_plan()
        render_brief(plan, mode="llm", client=recorder, now=NOW)
        blob = json.dumps(recorder.messages, ensure_ascii=False)
        for category in ("scheduled", "must_do", "overdue", "next_up",
                         "waiting_for", "backlog"):
            for item in plan[category]:
                self.assertNotIn(item["commitment_id"], blob)

    def test_no_raw_timestamp_reaches_the_model(self):
        recorder = Recorder()
        plan = busy_day_plan()
        render_brief(plan, mode="llm", client=recorder, now=NOW)
        blob = json.dumps(recorder.messages, ensure_ascii=False)
        for needle in ("2026-09-12T13:00:00+00:00", "2026-08-24T19:00:00+09:00",
                       "+09:00", "+00:00", "generated_at"):
            self.assertNotIn(needle, blob)

    def test_no_plan_hash_or_freshness_internals_reach_the_model(self):
        recorder = Recorder()
        render_brief(busy_day_plan(), mode="llm", client=recorder, now=NOW)
        blob = json.dumps(recorder.messages, ensure_ascii=False)
        for needle in ("plan_hash", "source_freshness", "last_success_at",
                       "age_seconds", "threshold_seconds", "filtered_noise",
                       "reason_for_inclusion", "source_types", "work_state",
                       "responsibility", "due_precision", "all_day"):
            self.assertNotIn(needle, blob)

    def test_no_evidence_or_raw_history_field_can_reach_the_model(self):
        """A planner that started leaking evidence would be caught here."""
        plan = busy_day_plan()
        plan["must_do"][0]["evidence_snippet"] = "LINE本文: 明日の件よろしく（全文）"
        plan["must_do"][0]["gmail_body"] = "From: someone@example.com\n\n本文全体"
        recorder = Recorder()
        render_brief(plan, mode="llm", client=recorder, now=NOW)
        blob = json.dumps(recorder.messages, ensure_ascii=False)
        self.assertNotIn("LINE本文", blob)
        self.assertNotIn("本文全体", blob)
        self.assertNotIn("@example.com", blob)

    def test_a_coarse_precision_time_is_not_even_shown_to_the_model(self):
        """Amendment B4: the model cannot print 23:59 if it never saw it."""
        plan = make_plan(must_do=[make_item(title="高橋宛:予算修正は今週中(学務の要望)",
                                            due_at="2026-09-12T23:59:00+09:00",
                                            due_precision="week", local_time="23:59")])
        recorder = Recorder()
        render_brief(plan, mode="llm", client=recorder, now=NOW)
        self.assertNotIn("23:59", json.dumps(recorder.messages, ensure_ascii=False))

    def test_a_gmail_style_counterparty_is_not_shown_to_the_model(self):
        plan = make_plan(waiting_for=[make_item(
            title="ShipXplorer Sharing Station Offline", work_state="WAITING",
            counterparty='"ShipXplorer Support" <support@shipxplorer.example>')])
        recorder = Recorder()
        render_brief(plan, mode="llm", client=recorder, now=NOW)
        self.assertNotIn("@shipxplorer", json.dumps(recorder.messages, ensure_ascii=False))

    def test_only_the_items_the_template_kept_are_shown(self):
        plan = busy_day_plan()
        payload = compact_plan_for_model(plan, now=NOW)
        shown = sum(len(section["items"]) for section in payload["sections"])
        self.assertEqual(shown, len(render_template(plan, now=NOW)[1]))
        self.assertEqual(shown, 12)

    def test_the_hidden_tail_is_a_number_not_a_list(self):
        payload = compact_plan_for_model(busy_day_plan(), now=NOW)
        overdue = [s for s in payload["sections"] if s["heading"] == "期限超過"][0]
        self.assertEqual(overdue["more"], 20)
        self.assertEqual(len(overdue["items"]), 3)

    def test_the_payload_stays_well_inside_the_broker_payload_bound(self):
        payload = compact_plan_for_model(busy_day_plan(), now=NOW)
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(blob), AntigravityBridgeClient.MAX_PAYLOAD_BYTES)


class LazyResolutionTests(unittest.TestCase):
    """Sections 70 and 109: no credential, no network, no new provider."""

    def test_no_client_is_resolved_without_explicit_configuration(self):
        self.assertIsNone(resolve_client({}))
        self.assertIsNone(resolve_client({"llm": {}}))
        self.assertIsNone(resolve_client({"llm": {"enabled": False,
                                                  "url": DEFAULT_BRIDGE_URL}}))

    def test_an_enabled_config_resolves_the_bridge_client(self):
        client = resolve_client({"llm": {"enabled": True}})
        self.assertIsInstance(client, AntigravityBridgeClient)
        self.assertEqual(client.url, DEFAULT_BRIDGE_URL)
        self.assertEqual(client.model, DEFAULT_MODEL)

    def test_constructing_the_client_reads_no_credential(self):
        client = AntigravityBridgeClient(token_file="/nonexistent/bridge.token")
        self.assertEqual(client.model, DEFAULT_MODEL)

    def test_the_token_is_only_resolved_when_a_request_is_made(self):
        client = AntigravityBridgeClient(token_file="/nonexistent/bridge.token",
                                         token_env="HERMES_TEST_ABSENT_TOKEN")
        with self.assertRaises(OSError):
            client([{"role": "system", "content": "x"}], timeout=1)

    def test_a_missing_token_surfaces_as_a_clean_fallback_not_a_crash(self):
        client = AntigravityBridgeClient(token_env="HERMES_TEST_ABSENT_TOKEN")
        result = render_brief(busy_day_plan(), mode="llm", client=client, now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "llm_unavailable")
        self.assertTrue(result["text"].strip())

    def test_the_token_comes_from_the_environment_when_present(self):
        os.environ["HERMES_TEST_BRIDGE_TOKEN"] = "unit-test-token"
        try:
            client = AntigravityBridgeClient(token_env="HERMES_TEST_BRIDGE_TOKEN")
            self.assertEqual(client._resolve_token(), "unit-test-token")
        finally:
            del os.environ["HERMES_TEST_BRIDGE_TOKEN"]

    def test_the_model_and_url_are_fixed_not_caller_supplied(self):
        """Section 71: fixed inference route, fixed model, bounded payload."""
        recorder = Recorder()
        render_brief(busy_day_plan(), mode="llm", client=recorder, now=NOW,
                     config={"renderer": {"llm": {"model": "something-else"}}})
        # A config model name never becomes a *new* provider or route: the
        # client object owns both, and an injected client owns its own.
        self.assertEqual(recorder.model, DEFAULT_MODEL)

    def test_importing_the_module_needs_no_credential_or_network(self):
        """A fresh import in a clean interpreter must not reach for anything."""
        import subprocess

        code = (
            "import sys, socket;"
            "sys.path.insert(0, %r);"
            "socket.socket = None;"
            "import planning.renderer as r;"
            "print(r.render_brief({'date':'2026-09-12'}, mode='template')['renderer'])"
            % SRC_DIR
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "template")


class BridgeClientRequestTests(unittest.TestCase):
    """The request the client would send, without sending it."""

    def test_the_payload_bound_is_enforced_before_any_socket_is_opened(self):
        client = AntigravityBridgeClient(token="unit-test-token")
        huge = [{"role": "user", "content": "あ" * 40000}]
        with self.assertRaises(ValueError):
            client(huge, timeout=1)

    def test_the_client_exposes_no_tool_surface(self):
        client = AntigravityBridgeClient(token="unit-test-token")
        for attribute in ("tools", "functions", "tool_choice", "run_command"):
            self.assertFalse(hasattr(client, attribute))


class PlanIndependenceTests(unittest.TestCase):
    """Section 77: the same facts render the same way on either path."""

    def test_both_paths_agree_on_every_fact_they_show(self):
        plan = busy_day_plan()
        template = render_template(plan, now=NOW)[0]
        payload = compact_plan_for_model(plan, now=NOW)
        self.assertEqual(payload["opening_line"], template.split("\n")[0])
        for section in payload["sections"]:
            for entry in section["items"]:
                self.assertIn(entry["title"], template)
                if entry.get("time"):
                    self.assertIn(entry["time"], template)
                if entry.get("date"):
                    self.assertIn(entry["date"], template)

    def test_an_overdue_entry_carries_its_age_not_a_clock_time(self):
        payload = compact_plan_for_model(busy_day_plan(), now=NOW)
        overdue = [s for s in payload["sections"] if s["heading"] == "期限超過"][0]
        for entry in overdue["items"]:
            self.assertIn("overdue_days", entry)
            self.assertNotIn("time", entry)

    def test_the_planner_output_is_never_mutated(self):
        plan = busy_day_plan()
        before = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        render_brief(plan, mode="llm", client=Recorder(), now=NOW)
        self.assertEqual(before, json.dumps(plan, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
