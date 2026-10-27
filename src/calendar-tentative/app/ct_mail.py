#!/usr/bin/env python3
"""Calendar Tentative Registration — is this Gmail evidence from a person?

A commitment whose only evidence is Gmail is proposed only when at least one
of its messages passes this screen. The screen is deterministic and uses
three kinds of fact that H.2 already publishes, never the message body:

* **What the owner did.** The owner sent the message, wrote in its thread,
  or has written to its sender before. Newsletters, campaigns and
  notification robots are never correspondents; this is the strongest signal
  available and the one that cannot be gamed by a sender's wording.
* **What Gmail itself decided.** `CATEGORY_PROMOTIONS`, `CATEGORY_SOCIAL`
  and `CATEGORY_FORUMS` are Gmail's own classification of bulk, social and
  mailing-list mail; `CATEGORY_UPDATES` is its classification of automated
  notices (receipts, confirmations, account mail).
* **What the sender address says.** `no-reply@`, `notifications@`,
  `newsletter@` and similar local parts, or a `news.` / `mailer.` style
  sending subdomain, are automated by construction.

Order matters and is stated once here:

    1. message unknown to the index                -> unscreened
    2. deleted, SPAM or TRASH                       -> discarded
    3. the owner sent it                            -> pass (sent)
    4. the owner wrote in its thread                -> pass (thread)
    5. Gmail filed it as promotions/social/forums   -> bulk
    6. the owner has written to the sender          -> pass (correspondent)
    7. Gmail "updates", or an automated sender      -> notification
                                                       (passes only in
                                                       people_and_notifications)
    8. anything else                                -> pass (personal)

Rule 5 is above rule 6 on purpose: a shop the owner once emailed is still a
shop when Gmail files its next mail under Promotions. Rules 3-4 are above it
because a thread the owner replied to is a conversation whatever Gmail
thinks of the first message.

Nothing here reads `body_text`, `snippet` or `commitment_text`: the columns
selected are listed in `_MESSAGE_SQL` and `_SENT_SQL`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr

MODE_OFF = "off"
MODE_PEOPLE = "people"
MODE_PEOPLE_AND_NOTIFICATIONS = "people_and_notifications"
MODE_ALL = "all"

BULK_CATEGORIES = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"})
NOTIFICATION_CATEGORIES = frozenset({"CATEGORY_UPDATES"})
DISCARDED_LABELS = frozenset({"SPAM", "TRASH"})

# Pass bases, strongest first.
BASIS_SENT = "sent"
BASIS_THREAD = "thread"
BASIS_CORRESPONDENT = "correspondent"
BASIS_PERSONAL = "personal"
BASIS_NOTIFICATION = "notification"
BASIS_ORDER = (BASIS_SENT, BASIS_THREAD, BASIS_CORRESPONDENT, BASIS_PERSONAL,
               BASIS_NOTIFICATION)

BASIS_LABELS = {
    BASIS_SENT: "あなたが送ったメール",
    BASIS_THREAD: "あなたが返信したスレッドのメール",
    BASIS_CORRESPONDENT: "やり取りのある相手からのメール",
    BASIS_PERSONAL: "個人宛てのメール",
    BASIS_NOTIFICATION: "自動通知のメール",
}

# Skip reasons. When several messages all fail, the one closest to passing
# is reported (this order), so the count says what a looser mode would add.
R_GMAIL_NOTIFICATION = "gmail_notification"
R_GMAIL_BULK = "gmail_bulk"
R_GMAIL_DISCARDED = "gmail_discarded"
R_GMAIL_UNSCREENED = "gmail_unscreened"
FAILURE_ORDER = (R_GMAIL_NOTIFICATION, R_GMAIL_BULK, R_GMAIL_DISCARDED, R_GMAIL_UNSCREENED)

_AUTOMATED_LOCAL = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon|postmaster"
    r"|bounces?|notifications?|notify|notice|alerts?|news|newsletters?|mail[-_.]?mag(?:azine)?"
    r"|magazine|campaigns?|marketing|promo(?:tions?)?)(?:[-_.+].*)?$",
    re.IGNORECASE,
)
#: First label of the sender's domain that marks a bulk sending host. `mail.`
#: and `email.` are deliberately absent: universities and companies send real
#: mail from hosts named that way.
_AUTOMATED_SUBDOMAINS = frozenset({
    "mailer", "mailmag", "news", "newsletter", "bounce", "bounces", "campaign",
    "campaigns", "marketing", "magazine",
})

_MESSAGE_SQL = """SELECT source_id, thread_id, from_header, labels_json, is_sent, deleted_at
                  FROM gmail_messages WHERE source_id IN ({marks})"""
_SENT_SQL = """SELECT thread_id, from_header, to_header, cc_header
               FROM gmail_messages WHERE is_sent = 1 AND deleted_at IS NULL"""

REQUIRED_COLUMNS = frozenset({"source_id", "thread_id", "from_header", "to_header", "cc_header",
                              "labels_json", "is_sent", "deleted_at"})


@dataclass(frozen=True)
class Message:
    thread_id: str
    sender: str
    labels: frozenset
    is_sent: bool
    deleted: bool


@dataclass
class MailFacts:
    messages: dict = field(default_factory=dict)
    sent_threads: frozenset = frozenset()
    correspondents: frozenset = frozenset()


def address_of(header) -> str:
    return parseaddr(str(header or ""))[1].strip().lower()


def addresses_in(*headers) -> set:
    return {addr.strip().lower() for _, addr in getaddresses([str(h or "") for h in headers])
            if addr and "@" in addr}


def is_automated_sender(address: str) -> bool:
    local, _, domain = str(address or "").partition("@")
    if not domain:
        return False
    if _AUTOMATED_LOCAL.match(local):
        return True
    return domain.split(".", 1)[0] in _AUTOMATED_SUBDOMAINS


def _labels(raw) -> frozenset:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(str(item) for item in value) if isinstance(value, list) else frozenset()


def index_usable(con) -> bool:
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(gmail_messages)")}
    except sqlite3.Error:
        return False
    return REQUIRED_COLUMNS <= cols


def load_facts(con, source_ids) -> MailFacts:
    """Facts for the given evidence ids, plus what the owner has sent."""
    facts = MailFacts()
    ids = sorted({str(s) for s in source_ids if s})
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        for row in con.execute(_MESSAGE_SQL.format(marks=",".join("?" * len(chunk))), chunk):
            facts.messages[str(row[0])] = Message(
                thread_id=str(row[1] or ""), sender=address_of(row[2]),
                labels=_labels(row[3]), is_sent=bool(row[4]), deleted=bool(row[5]))
    threads, owners, recipients = set(), set(), set()
    for thread_id, from_header, to_header, cc_header in con.execute(_SENT_SQL):
        if thread_id:
            threads.add(str(thread_id))
        owners |= addresses_in(from_header)
        recipients |= addresses_in(to_header, cc_header)
    facts.sent_threads = frozenset(threads)
    # Mail to oneself does not make oneself a correspondent of a newsletter
    # that is addressed the same way.
    facts.correspondents = frozenset(recipients - owners)
    return facts


def screen_message(message: Message | None, facts: MailFacts, mode: str) -> tuple:
    """(passed, basis_or_reason) for one message. See the module docstring."""
    if message is None:
        return False, R_GMAIL_UNSCREENED
    if message.deleted or message.labels & DISCARDED_LABELS:
        return False, R_GMAIL_DISCARDED
    if message.is_sent:
        return True, BASIS_SENT
    if message.thread_id and message.thread_id in facts.sent_threads:
        return True, BASIS_THREAD
    if message.labels & BULK_CATEGORIES:
        return False, R_GMAIL_BULK
    if message.sender and message.sender in facts.correspondents:
        return True, BASIS_CORRESPONDENT
    if message.labels & NOTIFICATION_CATEGORIES or is_automated_sender(message.sender):
        if mode == MODE_PEOPLE_AND_NOTIFICATIONS:
            return True, BASIS_NOTIFICATION
        return False, R_GMAIL_NOTIFICATION
    return True, BASIS_PERSONAL


def screen_commitment(source_ids, facts: MailFacts | None, mode: str) -> tuple:
    """(passed, basis_or_reason) for a commitment's Gmail evidence.

    Passes when any message passes, reporting the strongest basis; fails with
    the reason closest to passing. Without facts (no index), nothing passes.
    """
    if facts is None:
        return False, R_GMAIL_UNSCREENED
    passed, failed = [], []
    for source_id in sorted(set(source_ids)):
        ok, label = screen_message(facts.messages.get(source_id), facts, mode)
        (passed if ok else failed).append(label)
    if passed:
        return True, min(passed, key=BASIS_ORDER.index)
    if not failed:
        return False, R_GMAIL_UNSCREENED
    return False, min(failed, key=FAILURE_ORDER.index)
