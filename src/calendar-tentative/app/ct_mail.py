#!/usr/bin/env python3
"""Calendar Tentative Registration — is this Gmail evidence from a person?

A commitment whose only evidence is Gmail is proposed only when at least one
of its messages passes this screen. The screen is deterministic and uses
three kinds of fact that H.2 already publishes, never the message body:

* **What the owner did.** The owner sent the message, wrote in its thread
  before it arrived, or has written to its sender (or to the list it came
  through). This is the strongest signal available and the one a sender's
  wording cannot fake.
* **What Gmail itself decided.** `CATEGORY_PROMOTIONS`/`SOCIAL` (bulk),
  `CATEGORY_FORUMS` (mailing lists), `CATEGORY_UPDATES` (automated notices),
  `CATEGORY_PERSONAL` (Primary). These labels exist only while Gmail's smart
  features are on — and Google turns those OFF by default for accounts in
  Japan. So the absence of a category is never read as "personal": a stranger's
  mail passes only when Gmail itself filed it under Primary.
* **What the headers say.** Automated sender addresses (`no-reply@`,
  `store-news@`, a `news.` sending host), and blind-copy blasts whose To/Cc
  name nobody but the sender.

Order matters and is stated once here:

     1. message unknown to the index                       -> unscreened
     2. a draft (the owner's unsent text)                  -> draft
     3. deleted, SPAM or TRASH                             -> discarded
     4. the owner sent it                                  -> pass (sent)
     5. it arrived after the owner wrote in its thread     -> pass (thread)
     6. Gmail filed it as promotions or social             -> bulk
     7. no sender address can be read                      -> unscreened
     8. the owner has written to the sender                -> pass (correspondent)
     9. Gmail filed it as a mailing list:
          addressed to a list the owner has written to     -> pass (correspondent)
          otherwise                                        -> list
    10. Gmail "updates", or an automated sender            -> notification *
    11. a blind-copy blast (To/Cc empty or only the sender) -> broadcast *
    12. Gmail filed it under Primary                       -> pass (personal)
    13. no Gmail category at all                           -> uncategorized

    * passes only in people_and_notifications

Rule 5 counts only mail that arrived after the owner's first message in the
thread: replying to a newsletter (to unsubscribe, say) must not turn the
newsletter itself into a conversation. For the same reason an automated
address never becomes a correspondent. Rule 6 sits above rule 8 because a shop
the owner once emailed is still a shop when Gmail files its next mail under
Promotions; mailing lists sit below it because people the owner works with
post to them.

Nothing here reads `body_text`, `snippet` or `commitment_text`: every column
selected is named in `_MESSAGE_SQL`, `_SENT_SQL` and `_CATEGORY_SQL`.
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

BULK_CATEGORIES = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"})
LIST_CATEGORIES = frozenset({"CATEGORY_FORUMS"})
NOTIFICATION_CATEGORIES = frozenset({"CATEGORY_UPDATES"})
PERSONAL_CATEGORY = "CATEGORY_PERSONAL"
DISCARDED_LABELS = frozenset({"SPAM", "TRASH"})
DRAFT_LABEL = "DRAFT"

# Pass bases, strongest first.
BASIS_SENT = "sent"
BASIS_THREAD = "thread"
BASIS_CORRESPONDENT = "correspondent"
BASIS_PERSONAL = "personal"
BASIS_NOTIFICATION = "notification"
BASIS_BROADCAST = "broadcast"
BASIS_ORDER = (BASIS_SENT, BASIS_THREAD, BASIS_CORRESPONDENT, BASIS_PERSONAL,
               BASIS_NOTIFICATION, BASIS_BROADCAST)

BASIS_LABELS = {
    BASIS_SENT: "あなたが送ったメール",
    BASIS_THREAD: "あなたが返信したスレッドのメール",
    BASIS_CORRESPONDENT: "やり取りのある相手からのメール",
    BASIS_PERSONAL: "Gmail がメイン（個人）に分類したメール",
    BASIS_NOTIFICATION: "自動通知のメール",
    BASIS_BROADCAST: "一斉送信（BCC）のメール",
}

# Skip reasons. When several messages all fail, the one closest to passing is
# reported (this order), so the counts say what a looser mode would add.
R_GMAIL_BROADCAST = "gmail_broadcast"
R_GMAIL_NOTIFICATION = "gmail_notification"
R_GMAIL_UNCATEGORIZED = "gmail_uncategorized"
R_GMAIL_LIST = "gmail_list"
R_GMAIL_BULK = "gmail_bulk"
R_GMAIL_UNSCREENED = "gmail_unscreened"
R_GMAIL_DISCARDED = "gmail_discarded"
R_GMAIL_DRAFT = "gmail_draft"
FAILURE_ORDER = (R_GMAIL_BROADCAST, R_GMAIL_NOTIFICATION, R_GMAIL_UNCATEGORIZED,
                 R_GMAIL_LIST, R_GMAIL_BULK, R_GMAIL_UNSCREENED, R_GMAIL_DISCARDED,
                 R_GMAIL_DRAFT)

#: Anchored forms that tokenising would split apart (no-reply, do-not-reply).
_AUTOMATED_LOCAL = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon|postmaster"
    r"|bounces?|notifications?|notify|notice|alerts?|news|newsletters?|mail[-_.]?mag(?:azine)?"
    r"|magazine|campaigns?|marketing|promo(?:tions?)?)(?:[-_.+].*)?$",
    re.IGNORECASE,
)
#: Whole tokens of a local part (split on - _ . +) that mark bulk mail:
#: store-news@, jp-newsletter@, merumaga@. Whole-token matching keeps
#: newsroom@, notes@ and info@ human.
_BULK_LOCAL_TOKENS = frozenset({
    "news", "enews", "mailnews", "newsletter", "newsletters", "mailmag", "magazine", "mag",
    "merumaga", "melmaga", "mag2", "campaign", "campaigns", "marketing", "promo",
    "promotion", "promotions", "noreply", "donotreply",
})
_LOCAL_SPLIT = re.compile(r"[-_.+]")
#: First label of the sender's domain that marks a bulk sending host. `mail.`
#: and `email.` are deliberately absent: universities and companies send real
#: mail from hosts named that way.
_AUTOMATED_SUBDOMAINS = frozenset({
    "mailer", "mailmag", "news", "newsletter", "bounce", "bounces", "campaign",
    "campaigns", "marketing", "magazine",
})
#: Strict parsing (CPython 3.13) rejects a display name with unquoted
#: specials such as `ABC Store [Official] <news@x>`. The address in angle
#: brackets is still unambiguous, so it is recovered rather than lost.
_ANGLE_ADDR = re.compile(r"<\s*([^<>\s@]+@[^<>\s@]+)\s*>")

_MESSAGE_SQL = """SELECT source_id, thread_id, from_header, to_header, cc_header, labels_json,
                         is_sent, deleted_at, internal_date_ms
                  FROM gmail_messages WHERE source_id IN ({marks})"""
_SENT_SQL = """SELECT thread_id, from_header, to_header, cc_header, internal_date_ms
               FROM gmail_messages WHERE is_sent = 1 AND deleted_at IS NULL"""
#: Counts only: is Gmail categorising this mailbox at all?
_CATEGORY_SQL = """SELECT COUNT(*),
                          COALESCE(SUM(CASE WHEN instr(labels_json, '"CATEGORY_') > 0
                                            THEN 1 ELSE 0 END), 0)
                   FROM gmail_messages
                   WHERE is_sent = 0 AND deleted_at IS NULL
                     AND instr(labels_json, '"DRAFT"') = 0"""

REQUIRED_COLUMNS = frozenset({"source_id", "thread_id", "from_header", "to_header", "cc_header",
                              "labels_json", "is_sent", "deleted_at", "internal_date_ms"})


@dataclass(frozen=True)
class Message:
    thread_id: str
    sender: str
    addressed: frozenset
    labels: frozenset
    is_sent: bool
    deleted: bool
    received_ms: int


@dataclass
class MailFacts:
    messages: dict = field(default_factory=dict)
    #: thread_id -> internal_date_ms of the owner's first sent message in it.
    sent_threads: dict = field(default_factory=dict)
    correspondents: frozenset = frozenset()
    #: Received, undeleted, non-draft messages in the index, and how many of
    #: them carry any Gmail category. Zero categorised means smart features
    #: are off and only rules 4, 5, 8 and 9 can pass anything.
    received_total: int = 0
    received_categorized: int = 0


def address_of(header) -> str:
    text = str(header or "")
    address = parseaddr(text)[1].strip().lower()
    if "@" in address:
        return address
    found = _ANGLE_ADDR.findall(text)
    return found[-1].strip().lower() if found else ""


def addresses_in(*headers) -> set:
    """Every address in the headers; one malformed header cannot erase another."""
    found: set = set()
    for header in headers:
        text = str(header or "")
        addrs = [a for _, a in getaddresses([text]) if a and "@" in a]
        if not addrs and text.strip():
            addrs = _ANGLE_ADDR.findall(text)
        found |= {a.strip().lower() for a in addrs}
    return found


def is_automated_sender(address: str) -> bool:
    local, _, domain = str(address or "").partition("@")
    if not domain:
        return False
    if _AUTOMATED_LOCAL.match(local):
        return True
    if any(token in _BULK_LOCAL_TOKENS for token in _LOCAL_SPLIT.split(local.lower())):
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
                addressed=frozenset(addresses_in(row[3], row[4])), labels=_labels(row[5]),
                is_sent=bool(row[6]), deleted=bool(row[7]), received_ms=int(row[8] or 0))
    threads: dict = {}
    owners, recipients = set(), set()
    for thread_id, from_header, to_header, cc_header, sent_ms in con.execute(_SENT_SQL):
        if thread_id:
            key = str(thread_id)
            sent_ms = int(sent_ms or 0)
            threads[key] = min(threads.get(key, sent_ms), sent_ms)
        owners |= addresses_in(from_header)
        recipients |= addresses_in(to_header, cc_header)
    facts.sent_threads = threads
    # Writing to oneself makes nobody a correspondent, and neither does
    # replying to a robot (an unsubscribe reply to newsletter@, say).
    facts.correspondents = frozenset(a for a in recipients - owners if not is_automated_sender(a))
    total, categorized = con.execute(_CATEGORY_SQL).fetchone()
    facts.received_total, facts.received_categorized = int(total or 0), int(categorized or 0)
    return facts


def screen_message(message: Message | None, facts: MailFacts, mode: str) -> tuple:
    """(passed, basis_or_reason) for one message. See the module docstring."""
    if message is None:
        return False, R_GMAIL_UNSCREENED
    labels = message.labels
    if DRAFT_LABEL in labels:
        return False, R_GMAIL_DRAFT
    if message.deleted or labels & DISCARDED_LABELS:
        return False, R_GMAIL_DISCARDED
    if message.is_sent:
        return True, BASIS_SENT
    first_reply = facts.sent_threads.get(message.thread_id) if message.thread_id else None
    if first_reply is not None and message.received_ms >= first_reply:
        return True, BASIS_THREAD
    if labels & BULK_CATEGORIES:
        return False, R_GMAIL_BULK
    if not message.sender:
        return False, R_GMAIL_UNSCREENED
    if message.sender in facts.correspondents:
        return True, BASIS_CORRESPONDENT
    if labels & LIST_CATEGORIES:
        if message.addressed & facts.correspondents:
            return True, BASIS_CORRESPONDENT
        return False, R_GMAIL_LIST
    widened = mode == MODE_PEOPLE_AND_NOTIFICATIONS
    if labels & NOTIFICATION_CATEGORIES or is_automated_sender(message.sender):
        return (True, BASIS_NOTIFICATION) if widened else (False, R_GMAIL_NOTIFICATION)
    if not (message.addressed - {message.sender}):
        return (True, BASIS_BROADCAST) if widened else (False, R_GMAIL_BROADCAST)
    if PERSONAL_CATEGORY in labels:
        return True, BASIS_PERSONAL
    return False, R_GMAIL_UNCATEGORIZED


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
