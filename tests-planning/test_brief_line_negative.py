"""LINE deliveries = 0 — phase sections 61, 69, 99.

The routine morning brief must never spend a LINE push. This file proves it
three ways, because "we always pass discord" is only an assertion about the
code as written today:

  1. **Structurally** — `delivery.send_brief()` has no channel parameter, the
     module defines no LINE constant, and no string literal anywhere in the
     modules T4 owns is equal to `"line"`. A future edit that adds one fails
     here rather than in production.
  2. **Behaviourally** — every request body this service can produce, across
     every reachable outcome, carries `preferred_channel == "discord"`.
  3. **By refusal** — a configuration asking for `channel: line` does not get a
     LINE push, and does not get a silent Discord one either. It refuses to
     deliver at all, before anything is generated.

The gateway side is already proven in production and is not re-asserted here:
`gw_proactive.py:291-294` is a plain `if channel == CHANNEL_LINE: ... else:
_send_discord(...)` with no `except`, no retry and no second branch, and the
recipient is chosen from the same validated variable at `:256-257`, so a
`discord` request cannot reach `_send_line` (profile section H.5).

Run with:  PYTHONPATH=src python -m pytest tests-planning -q
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from planning import brief_service, brief_state, delivery  # noqa: E402
from test_brief_delivery import (  # noqa: E402
    FakeClient, base_config, make_planner, make_renderer, no_events, stub_plan,
)

JST = ZoneInfo("Asia/Tokyo")
MORNING = datetime(2026, 9, 8, 8, 30, tzinfo=JST)

#: Every module T4 owns that could conceivably name a channel.
OWNED_MODULES = ("brief_state.py", "delivery.py", "brief_service.py", "cli.py")


@pytest.fixture
def state():
    con = brief_state.open_state(":memory:")
    try:
        yield brief_state.BriefState(con)
    finally:
        con.close()


def run_at(config, state, client, *, now=MORNING, **kw):
    return brief_service.run_scheduled_brief(
        config, now=now, state=state, client=client,
        planner=kw.pop("planner", make_planner()),
        renderer=kw.pop("renderer", make_renderer()),
        open_events=no_events, sleep=lambda _s: None, **kw)


# --- 1. structural -------------------------------------------------------

def test_delivery_defines_discord_and_no_line_constant():
    assert delivery.CHANNEL_DISCORD == "discord"
    for name in ("CHANNEL_LINE", "LINE", "PROACTIVE_CHANNELS", "CHANNELS"):
        assert not hasattr(delivery, name), \
            f"delivery.{name} exists; the brief must know only one channel"


def test_send_brief_has_no_channel_parameter():
    """A channel argument is a way for a caller to get this wrong."""
    params = inspect.signature(delivery.DeliveryClient.send_brief).parameters
    assert set(params) == {"self", "notification_id", "text", "reason"}
    assert "channel" not in params
    assert "preferred_channel" not in params
    assert "severity" not in params


@pytest.mark.parametrize("module", OWNED_MODULES)
def test_no_owned_module_contains_the_string_line(module):
    """No string literal equal to "line" (in any case) in the code T4 owns.

    Comments and docstrings discuss LINE at length — that is the point of the
    documentation — so this checks literals, which is what could ever reach a
    request body. A docstring is a single long constant and never equals
    "line".
    """
    tree = ast.parse((SRC / "planning" / module).read_text(encoding="utf-8"))
    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.strip().lower() == "line"
    ]
    assert offenders == [], f"{module} contains a literal 'line' channel value"


def test_the_only_permitted_channel_is_discord():
    assert brief_service.PERMITTED_CHANNEL == "discord"
    assert brief_service.PERMITTED_CHANNEL == delivery.CHANNEL_DISCORD


def test_severity_is_info_so_the_brief_is_never_an_escalation():
    """Phase sections 44 and 61: LINE is reserved for scarce escalation, and
    the brief is not one. INFO also matches the attention engine's digests."""
    assert delivery.SEVERITY_INFO == "INFO"


def test_notification_id_can_never_carry_a_channel():
    nid = delivery.notification_id_for("2026-09-08")
    assert "line" not in nid.lower()
    assert nid.startswith("morning-brief:")


# --- 2. behavioural ------------------------------------------------------

def test_the_request_body_is_exactly_the_gateway_allowlist(tmp_path, state):
    client = FakeClient()
    run_at(base_config(tmp_path), state, client)
    body = client.calls[0]
    # `gw_proactive.ALLOWED_FIELDS`. Anything else is a 400 unknown_field —
    # in particular there is no way to name a recipient.
    assert set(body) == {"notification_id", "preferred_channel", "severity",
                         "text", "reason", "dedupe_key"}
    assert body["preferred_channel"] == "discord"
    assert body["severity"] == "INFO"
    assert len(body["text"]) <= delivery.MAX_TEXT_CHARS
    assert len(body["notification_id"]) <= 120


@pytest.mark.parametrize("scripted", [
    None,
    ("delivered", 200, "ok"),
    ("already_delivered", 200, "already_delivered"),
    ("in_flight", 409, "delivery_in_progress"),
    ("content_mismatch", 409, "content_revision_mismatch"),
    ("failed", 502, "discord_send_failed"),
    ("unreachable", None, "FileNotFoundError"),
    ("unknown", None, "timeout"),
    ("rejected", 400, "text_too_long"),
])
def test_every_reachable_outcome_still_only_ever_asks_for_discord(
        scripted, tmp_path, state):
    results = ()
    if scripted is not None:
        outcome, http_status, detail = scripted
        results = (delivery.Result(outcome, detail=detail,
                                   http_status=http_status),)
    client = FakeClient(*results)
    run_at(base_config(tmp_path), state, client)
    assert client.calls, "expected at least one request"
    for call in client.calls:
        assert call["preferred_channel"] == "discord"
        assert "line" not in str(call["notification_id"]).lower()
        assert "line" not in str(call["dedupe_key"]).lower()


def test_line_deliveries_are_zero_across_a_whole_week(tmp_path, state):
    """Phase section 99, stated the way the gate states it."""
    config = base_config(tmp_path)
    client = FakeClient()
    day = datetime(2026, 9, 8, 8, 30, tzinfo=JST)
    for offset in range(7):
        run_at(config, state, client,
               now=day + timedelta(days=offset),
               planner=make_planner(
                   stub_plan(date=(day + timedelta(days=offset)).strftime("%Y-%m-%d"))))
    assert len(client.calls) == 7
    line_deliveries = [c for c in client.calls
                       if c["preferred_channel"] != "discord"]
    assert line_deliveries == []


def test_the_failure_notice_is_also_discord_only(tmp_path, state):
    config = base_config(tmp_path, notify_on_failure=True)
    client = FakeClient()
    run_at(config, state, client, planner=make_planner(exc=RuntimeError("x")))
    assert [c["preferred_channel"] for c in client.calls] == ["discord"]


def test_the_real_client_builds_a_discord_body(monkeypatch):
    """Not the fake: the production `DeliveryClient`, with only its transport
    replaced, so the body that would go on the wire is the one asserted."""
    sent = {}

    def fake_request(self, method, path, body=None):
        sent["method"] = method
        sent["path"] = path
        sent["body"] = body
        return 200, {"status": "delivered", "detail": "ok"}

    monkeypatch.setattr(delivery.DeliveryClient, "_request", fake_request)
    client = delivery.DeliveryClient("/nonexistent/proactive.sock")
    result = client.send_brief(notification_id="morning-brief:2026-09-08",
                               text="今日の予定は 1 件です。")
    assert result.ok
    assert result.channel == "discord"
    assert sent["method"] == "POST"
    assert sent["path"] == "/internal/notify"
    assert sent["body"]["preferred_channel"] == "discord"
    assert set(sent["body"]) == {"notification_id", "preferred_channel",
                                 "severity", "text", "reason", "dedupe_key"}


# --- 3. by refusal -------------------------------------------------------

@pytest.mark.parametrize("configured", ["line", "LINE", " Line ", "line,discord",
                                        "sms", "", None])
def test_a_non_discord_channel_in_config_refuses_to_deliver(configured,
                                                            tmp_path, state):
    config = base_config(tmp_path, channel=configured)
    client = FakeClient()
    outcome = run_at(config, state, client)

    assert client.calls == [], "nothing may be sent on a non-discord channel"
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_DELIVERY
    assert outcome["detail"].startswith("channel_not_permitted:")
    # And it refuses BEFORE generating: no plan hash was recorded.
    assert outcome["plan_hash"] is None


def test_the_refusal_happens_before_the_planner_runs(tmp_path, state):
    called = []
    config = base_config(tmp_path, channel="line")
    run_at(config, state, FakeClient(),
           planner=lambda conn, **kw: called.append(1) or stub_plan())
    assert called == [], "a line configuration must not even build a plan"


def test_a_discord_configuration_is_accepted_case_insensitively(tmp_path,
                                                                state):
    for value in ("discord", "Discord", " DISCORD "):
        con = brief_state.open_state(":memory:")
        try:
            st = brief_state.BriefState(con)
            client = FakeClient()
            outcome = run_at(base_config(tmp_path, channel=value), st, client)
            assert outcome["status"] == brief_service.ST_DELIVERED
            assert client.calls[0]["preferred_channel"] == "discord"
        finally:
            con.close()


# --- the systemd unit grants no channel credential either ----------------

def test_the_service_unit_grants_no_token_and_no_extra_network():
    unit = (SRC / "planning" / "systemd"
            / "hermes-morning-brief.service.in").read_text(encoding="utf-8")
    # No credential material of any kind is passed in (phase sections 52, 69).
    for forbidden in ("LINE_CHANNEL_ACCESS_TOKEN", "DISCORD_TOKEN",
                      "DISCORD_BOT_TOKEN", "LoadCredential",
                      "EnvironmentFile", "GITHUB_TOKEN", "OAUTH"):
        assert forbidden not in unit, f"{forbidden} must not appear in the unit"
    # AF_UNIX by default; AF_INET only via an explicit install-time opt-in.
    assert "RestrictAddressFamilies=@RENDERER_ADDRESS_FAMILIES@" in unit
    assert "AF_INET" not in unit.split("[Service]")[1].split(
        "RestrictAddressFamilies")[1].split("\n")[0]
    for denied in ("/run/docker.sock", "/var/run/docker.sock",
                   "/srv/hermes/attention/state",
                   "/srv/hermes/channel-gateway/config",
                   "/srv/hermes/gmail/state", "/srv/hermes/calendar/state"):
        assert f"InaccessiblePaths=-{denied}" in unit
    # Canonical truth is mounted read-only (phase section 64).
    assert "ReadOnlyPaths=/srv/hermes/hermes-data/events" in unit
    assert "ReadWritePaths=/srv/hermes/hermes-data/events" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=\n" in unit


def test_the_installer_refuses_the_forbidden_groups():
    script = (SRC / "planning" / "install-root.sh").read_text(encoding="utf-8")
    assert 'FORBIDDEN_GROUPS="docker sudo adm root wheel staff"' in script
    assert "--dry-run" in script
    assert "--uninstall" in script
    assert "refusing to widen" in script
    # It must not hard-code the operator's account name (privacy rule).
    assert "ATTENTION_USER" in script
