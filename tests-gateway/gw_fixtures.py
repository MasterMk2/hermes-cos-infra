#!/usr/bin/env python3
"""G.1 test fixtures — synthetic identities and payloads.

Every identifier here is invented. No real LINE user id, Discord snowflake,
channel secret or token appears in this repository, and none is needed: the
properties under test are structural.
"""

import json

# A LINE user id is 'U' plus 32 hex characters. This one is not a real user.
OWNER_LINE = "U" + "0123456789abcdef" * 2
STRANGER_LINE = "U" + "fedcba9876543210" * 2

# Discord snowflakes: 17-19 digits. Invented.
OWNER_DISCORD = "100000000000000001"
STRANGER_DISCORD = "200000000000000002"

# A channel secret is opaque to us; any bytes work for HMAC.
CHANNEL_SECRET = "test-channel-secret-not-a-real-one"

DM_CHANNEL_ID = "300000000000000003"


def line_text_event(text="こんにちは", *, user_id=OWNER_LINE,
                    message_id="msg-0001", reply_token="tok-0001",
                    timestamp=1767225600000, redelivery=False,
                    message_type="text"):
    """One `message` event as LINE delivers it."""
    message = {"id": message_id, "type": message_type}
    if message_type == "text":
        message["text"] = text
    return {
        "type": "message",
        "mode": "active",
        "timestamp": timestamp,
        "source": {"type": "user", "userId": user_id},
        "webhookEventId": f"whe-{message_id}",
        "deliveryContext": {"isRedelivery": bool(redelivery)},
        "replyToken": reply_token,
        "message": message,
    }


def line_body(events, destination="Udestination0000000000000000000000"):
    """A complete webhook body, as bytes — the form a signature covers."""
    return json.dumps(
        {"destination": destination, "events": events},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


def line_verification_body():
    """What the LINE console's 'Verify' button posts: signed, no events."""
    return line_body([])


BASE_CONFIG = {
    "owner": {
        "line_user_id": OWNER_LINE,
        "discord_user_id": OWNER_DISCORD,
    },
    "paths": {
        "state_db": "/tmp/gw-test.db",
        "log_dir": "/tmp/gw-test-logs",
    },
    "line": {"enabled": True, "listen_host": "127.0.0.1", "listen_port": 8790},
    "discord": {"enabled": True},
    "hermes": {"base_url": "http://127.0.0.1:8642"},
}


def config_dict(**overrides):
    """A deep-ish copy of BASE_CONFIG with section overrides applied."""
    import copy

    data = copy.deepcopy(BASE_CONFIG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return data
