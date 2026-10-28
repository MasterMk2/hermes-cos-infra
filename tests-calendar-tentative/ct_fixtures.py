"""Isolated fixtures for the calendar tentative-registration suite.

Every database here is created in a temporary directory with the REAL
schemas: commitments from `commitments.store.SCHEMA_SQL`, the Calendar index
from H.1's `cal_index.INDEX_SCHEMA`, the Gmail index from H.2's
`gm_index.INDEX_SCHEMA`. A fixture that copied a schema would keep passing
after the real one changed.

`FakeCalendar` stands in for Google. It keeps deleted events (as Google
does), enforces If-Match, refuses a duplicate id with 409, and can be told
to lose an answer after applying a change — the case the engine's UNKNOWN
handling exists for. It also performs the owner's actions: delete, edit,
recolour, move to another calendar.

All titles and names are invented.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for sub in ("src", os.path.join("src", "calendar-tentative", "app"),
            os.path.join("src", "calendar", "app"), os.path.join("src", "gmail", "app")):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

from cal_index import INDEX_SCHEMA  # noqa: E402
from commitments.store import SCHEMA_SQL  # noqa: E402
from gm_index import INDEX_SCHEMA as GMAIL_INDEX_SCHEMA  # noqa: E402
from ct_config import parse_config  # noqa: E402
from ct_google import ApiError  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=JST)
CALENDAR_ID = "primary"
OWNER_CALENDAR_ID = "owner-main@group.calendar.google.com"
OWNER_ADDRESS = "owner@example.com"
#: How H.1 names the primary calendar in its snapshot: the owner's address.
H1_PRIMARY_ID = OWNER_ADDRESS

RAW_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, type TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'open',
  source_type TEXT, source_id TEXT
);
"""


def make_cfg(**sections):
    data = {"calendar": {"calendar_id": CALENDAR_ID}}
    for key, value in sections.items():
        data.setdefault(key, {}).update(value)
    return parse_config(data)


class Workspace:
    """A temp dir with events.db, index.db and the state db path."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.events_path = os.path.join(self.dir, "events.db")
        self.index_path = os.path.join(self.dir, "index.db")
        self.state_path = os.path.join(self.dir, "state", "tentative.db")
        os.makedirs(os.path.dirname(self.state_path))
        self.events = sqlite3.connect(self.events_path)
        self.events.row_factory = sqlite3.Row
        self.events.executescript(SCHEMA_SQL)
        self.events.executescript(RAW_EVENTS_SQL)
        self.index = sqlite3.connect(self.index_path)
        self.index.row_factory = sqlite3.Row
        self.index.executescript(INDEX_SCHEMA)
        self.gmail_path = os.path.join(self.dir, "gmail-index.db")
        self.gmail = sqlite3.connect(self.gmail_path)
        self.gmail.executescript(GMAIL_INDEX_SCHEMA)
        self._n = 0
        self._mail_clock = 1_790_000_000_000

    def close(self):
        self.events.close()
        self.index.close()
        self.gmail.close()
        self._tmp.cleanup()

    # -- gmail index -----------------------------------------------------

    def add_mail(self, source_id, *, sender="Someone <someone@example.org>",
                 to=OWNER_ADDRESS, cc="", labels=("INBOX", "CATEGORY_PERSONAL"),
                 thread_id=None, is_sent=False, deleted=False, subject="件名", body="本文",
                 at=None):
        """One row of H.2's published index. `source_id` is `gmail:<id>`.

        The default labels are a person's mail filed under Primary. `at` is
        internal_date_ms; by default each mail is a minute after the last.
        """
        self._mail_clock += 60_000
        at = self._mail_clock if at is None else at
        labels = list(labels) + (["SENT"] if is_sent and "SENT" not in labels else [])
        message_id = source_id.split(":", 1)[-1]
        self.gmail.execute(
            """INSERT INTO gmail_messages (source_id, message_id, thread_id, received_at,
                 internal_date_ms, from_header, to_header, cc_header, subject, labels_json,
                 routine_excluded, snippet, body_text, commitment_text, content_state, mime_json,
                 attachments_json, is_sent, deleted_at, indexed_at, revision, baseline,
                 content_hash)
               VALUES (?, ?, ?, '2026-09-30T00:00:00+00:00', ?, ?, ?, ?, ?, ?, 0, '', ?, ?,
                       'available', '{}', '[]', ?, ?, '2026-09-30T00:00:00+00:00', 0, 0, 'h')""",
            (source_id, message_id, thread_id or f"t-{message_id}", at,
             OWNER_ADDRESS if is_sent else sender, to, cc, subject,
             json.dumps(labels), body, body, 1 if is_sent else 0,
             "2026-09-30T00:00:00+00:00" if deleted else None),
        )
        self.gmail.commit()

    def relabel_mail(self, source_id, labels):
        self.gmail.execute("UPDATE gmail_messages SET labels_json = ? WHERE source_id = ?",
                           (json.dumps(list(labels)), source_id))
        self.gmail.commit()

    # -- commitments -----------------------------------------------------

    def add_commitment(self, cid, title, *, responsibility="SELF", work_state="ACTIONABLE",
                       due_at=None, due_precision=None, scheduled_at=None,
                       scheduled_end_at=None, authority="evidence", confidence=0.85,
                       currentness="current", counterparty=None, sources=None,
                       raw_type=None, source_count=None):
        sources = sources if sources is not None else [("discord", f"msg-{cid}")]
        stamp = "2026-09-30T12:00:00+09:00"
        self.events.execute(
            """INSERT INTO commitments (commitment_id, title, summary, responsibility, work_state,
                 counterparty, due_at, due_precision, scheduled_at, scheduled_end_at, authority,
                 confidence, currentness, created_at, updated_at, source_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, title, title, responsibility, work_state, counterparty, due_at, due_precision,
             scheduled_at, scheduled_end_at, authority, confidence, currentness, stamp, stamp,
             source_count or len(sources) or 1),
        )
        for source_type, source_id in sources:
            self.events.execute(
                """INSERT INTO commitment_evidence (commitment_id, source_type, source_id,
                     observed_at, evidence_role, confidence, created_at)
                   VALUES (?, ?, ?, ?, 'origin', 1.0, ?)""",
                (cid, source_type, source_id, stamp, stamp),
            )
            if raw_type:
                self._n += 1
                self.events.execute(
                    "INSERT INTO events (event_id, type, source_type, source_id) VALUES (?, ?, ?, ?)",
                    (f"evt_{self._n}", raw_type, source_type, source_id),
                )
        self.events.commit()

    def update_commitment(self, cid, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.events.execute(f"UPDATE commitments SET {sets} WHERE commitment_id = ?",
                            (*fields.values(), cid))
        self.events.commit()

    def delete_commitment(self, cid):
        self.events.execute("DELETE FROM commitment_evidence WHERE commitment_id = ?", (cid,))
        self.events.execute("DELETE FROM commitments WHERE commitment_id = ?", (cid,))
        self.events.commit()

    # -- calendar index --------------------------------------------------

    def add_index_event(self, event_id, summary, start_at, *, calendar_id=OWNER_CALENDAR_ID,
                        end_at=None, all_day=False, status="confirmed"):
        self.index.execute(
            """INSERT INTO calendar_events (source_id, calendar_id, calendar_name, event_id,
                 status, summary, start_at, end_at, all_day, recurrence_state, meaningful_hash)
               VALUES (?, ?, 'cal', ?, ?, ?, ?, ?, ?, 'one_off', 'h')""",
            (f"gcal:{calendar_id}:{event_id}", calendar_id, event_id, status, summary,
             start_at, end_at, 1 if all_day else 0),
        )
        self.index.commit()


class FakeCalendar:
    """Google Calendar, as far as this service can observe it."""

    def __init__(self, calendar_id=CALENDAR_ID):
        self.calendar_id = calendar_id
        self.events: dict = {}
        self.calls: list = []
        #: verb -> list of ("raise", exc) | ("apply_then_raise", exc)
        self.fail: dict = {}
        self.auth_error = None
        self._etag = 0

    # -- helpers ----------------------------------------------------------

    def _next_etag(self):
        self._etag += 1
        return f'"etag-{self._etag}"'

    def _run(self, verb, apply):
        queue = self.fail.get(verb) or []
        if queue:
            action, exc = queue.pop(0)
            if action == "raise":
                raise exc
            apply()
            raise exc
        return apply()

    def _check_calendar(self, calendar_id):
        if calendar_id != self.calendar_id:
            raise ApiError(403, "forbidden")

    def live(self):
        return {k: v for k, v in self.events.items() if v.get("status") != "cancelled"}

    # -- the client interface -------------------------------------------

    def authenticate(self):
        if self.auth_error is not None:
            raise self.auth_error

    def insert_event(self, calendar_id, body):
        self._check_calendar(calendar_id)
        self.calls.append(("insert", body["id"]))

        def apply():
            if body["id"] in self.events:
                raise ApiError(409, "duplicate")
            resource = copy.deepcopy(body)
            resource["etag"] = self._next_etag()
            resource["htmlLink"] = f"https://calendar.example/{body['id']}"
            self.events[body["id"]] = resource
            return copy.deepcopy(resource)
        return self._run("insert", apply)

    def get_event(self, calendar_id, event_id):
        self._check_calendar(calendar_id)
        self.calls.append(("get", event_id))

        def apply():
            found = self.events.get(event_id)
            return copy.deepcopy(found) if found else None
        return self._run("get", apply)

    def patch_event(self, calendar_id, event_id, body, *, etag):
        self._check_calendar(calendar_id)
        self.calls.append(("patch", event_id))

        def apply():
            current = self.events.get(event_id)
            if current is None or current.get("status") == "cancelled":
                raise ApiError(404, "notFound")
            if current["etag"] != etag:
                raise ApiError(412, "conditionNotMet")
            current.update(copy.deepcopy(body))
            current["etag"] = self._next_etag()
            return copy.deepcopy(current)
        return self._run("patch", apply)

    def delete_event(self, calendar_id, event_id, *, etag):
        self._check_calendar(calendar_id)
        self.calls.append(("delete", event_id))

        def apply():
            current = self.events.get(event_id)
            if current is None or current.get("status") == "cancelled":
                raise ApiError(410, "deleted")
            if current["etag"] != etag:
                raise ApiError(412, "conditionNotMet")
            current["status"] = "cancelled"
            current["etag"] = self._next_etag()
        return self._run("delete", apply)

    def list_events(self, calendar_id, *, private_property):
        self._check_calendar(calendar_id)
        self.calls.append(("list", private_property))
        key, _, value = private_property.partition("=")

        def marked(event):
            private = (event.get("extendedProperties") or {}).get("private") or {}
            return private.get(key) == value
        return self._run("list", lambda: [copy.deepcopy(v) for v in self.events.values()
                                          if marked(v)])

    def verbs(self):
        return [verb for verb, _ in self.calls]

    # -- the owner --------------------------------------------------------

    def owner_delete(self, event_id):
        self.events[event_id]["status"] = "cancelled"
        self.events[event_id]["etag"] = self._next_etag()

    def owner_edit(self, event_id, **fields):
        self.events[event_id].update(fields)
        self.events[event_id]["etag"] = self._next_etag()

    def owner_move_away(self, event_id):
        """Moved to another calendar: gone from this one entirely."""
        return self.events.pop(event_id)

    def owner_add(self, event_id, summary):
        """An event of the owner's own on the same (primary) calendar."""
        self.events[event_id] = {"id": event_id, "summary": summary, "status": "confirmed",
                                 "etag": self._next_etag()}
