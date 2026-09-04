#!/usr/bin/env python3
"""Attention Engine — the canonical signal.

Every source normalises to this one shape (section 15), and the two fields
that carry the most weight are the ones that are easiest to get wrong:

`signal_id`
    Derived from what the signal IS, never from when it was generated
    (section 16). A five-minute scan that minted a fresh id each pass would
    rediscover the same deadline 288 times a day and, worse, would defeat
    every duplicate check downstream — because the checks would all be
    comparing ids that are unique by construction.

`created_at`
    When the underlying thing HAPPENED, not when this engine noticed it.
    D.1 backfilled fourteen days of Discord history in one afternoon, so
    "recently ingested" and "recent" are unrelated here; a rule that used
    observation time would have announced a fortnight of old requests as
    news (section 25).

The module is pure: no database, no clock beyond what is passed in, no
network. That is what lets the policy tests run without any of the three.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace

SOURCE_EVENT = "event"
SOURCE_CODING = "coding"
SOURCE_HEALTH = "health"

# Signal types. Each is a fact about a thing, not an instruction about what to
# do with it — the policy decides that.
SIG_DUE_SOON = "due_soon"
SIG_OVERDUE = "overdue"
SIG_NEW_COMMITMENT = "new_high_confidence_commitment"
SIG_WAITING_FOR_USER = "waiting_for_user"
SIG_SNOOZE_EXPIRED = "snooze_expired"

SIG_JOB_COMPLETED = "coding_completed"
SIG_JOB_WAITING_HUMAN = "coding_waiting_human"
SIG_JOB_REVIEW_LIMIT = "coding_review_cycle_limit"
SIG_JOB_FAILED_TO_START = "coding_failed_to_start"

SIG_HEALTH_DEGRADED = "health_degraded"
SIG_HEALTH_RECOVERED = "health_recovered"

SEVERITIES = ("INFO", "IMPORTANT", "URGENT", "CRITICAL")
SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

#: Bound on a summary copied out of another component. The Event Layer's own
#: summaries are one line, but the bound is enforced here rather than assumed,
#: because the text ends up in a LINE push whose length is metered by LINE.
MAX_SUMMARY_CHARS = 220
MAX_SUBJECT_CHARS = 80

# Control characters and the bidirectional-override family. Stripped on the
# way in, not on the way out: a signal summary is carried into a notification,
# a log line and a status table, and sanitising at each of the three is how
# one of them gets forgotten.
# Ranges rather than literals: writing these characters into the source
# would put invisible bytes in a file people have to read and diff.
_CONTROL_CODEPOINTS = (
    list(range(0x00, 0x09)) + list(range(0x0B, 0x20)) + [0x7F]   # C0, DEL
    + list(range(0x200B, 0x2010))       # zero width, LTR/RTL marks
    + [0x2028, 0x2029]                  # line / paragraph separators
    + list(range(0x202A, 0x202F))       # bidi embedding and override
    + list(range(0x2066, 0x206A))       # bidi isolates
)
_CONTROL_RE = re.compile(
    "[" + "".join(re.escape(chr(c)) for c in _CONTROL_CODEPOINTS) + "]"
)


def clean_text(value, limit: int = MAX_SUMMARY_CHARS) -> str:
    """One line, bounded, no control characters.

    The content is data from LINE and Discord by way of the Event Layer. It
    is never an instruction (section 75); this function's job is only to stop
    it from breaking a log line or a terminal.
    """
    if value is None:
        return ""
    s = _CONTROL_RE.sub("", str(value))
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def derive_id(*parts) -> str:
    """A stable signal id from its identifying parts.

    Hashed rather than concatenated so that an event id containing a colon,
    or a service name containing a slash, cannot collide with a differently
    split set of parts. The readable prefix survives for the log.
    """
    joined = ":".join(str(p) for p in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"sig_{digest[:24]}"


@dataclass(frozen=True)
class Signal:
    """One normalised thing worth considering. Not one thing worth sending."""

    signal_id: str
    source_type: str
    source_id: str
    signal_type: str
    created_at: str            # when it happened (ISO-8601 UTC)
    subject: str
    summary: str
    dedupe_key: str
    entity_key: str
    severity: str = "INFO"
    urgency: float = 0.0
    confidence: float = 1.0
    actionability: float = 0.0
    due_at: str | None = None
    expires_at: str | None = None
    recommended_channel: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
        if self.source_type not in (SOURCE_EVENT, SOURCE_CODING, SOURCE_HEALTH):
            raise ValueError(f"unknown source_type {self.source_type!r}")

    def with_severity(self, severity: str) -> "Signal":
        return replace(self, severity=severity)

    def metadata_json(self) -> str:
        return json.dumps(self.metadata, ensure_ascii=False, sort_keys=True)

    def to_row(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "signal_type": self.signal_type,
            "created_at": self.created_at,
            "subject": self.subject,
            "summary": self.summary,
            "severity": self.severity,
            "urgency": self.urgency,
            "confidence": self.confidence,
            "actionability": self.actionability,
            "due_at": self.due_at,
            "expires_at": self.expires_at,
            "dedupe_key": self.dedupe_key,
            "entity_key": self.entity_key,
            "recommended_channel": self.recommended_channel,
            "metadata": self.metadata_json(),
        }


def make_signal(*, source_type: str, source_id: str, signal_type: str,
                created_at: str, subject: str, summary: str,
                key_parts=(), entity_key=None, **kw) -> Signal:
    """Build a signal with its id and dedupe key derived from `key_parts`.

    `key_parts` is the identity of the signal — what makes THIS one different
    from the next one, and the same as the one the previous scan produced.
    A window ("3h") belongs in it, because a 24-hour reminder and a 3-hour
    reminder about the same deadline are two notifications by design
    (section 64); a timestamp does not, because that would make every scan a
    new signal.
    """
    parts = tuple(key_parts) or (source_type, source_id, signal_type)
    dedupe_key = ":".join(str(p) for p in parts)
    return Signal(
        signal_id=derive_id(*parts),
        source_type=source_type,
        source_id=str(source_id),
        signal_type=signal_type,
        created_at=created_at,
        subject=clean_text(subject, MAX_SUBJECT_CHARS),
        summary=clean_text(summary),
        dedupe_key=dedupe_key,
        entity_key=str(entity_key or f"{source_type}:{source_id}"),
        **kw,
    )


def max_severity(a: str, b: str) -> str:
    return a if SEVERITY_ORDER[a] >= SEVERITY_ORDER[b] else b


def at_least(severity: str, floor: str) -> bool:
    return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[floor]
