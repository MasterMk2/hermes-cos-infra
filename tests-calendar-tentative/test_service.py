"""The command line: plan needs nothing, run in dry_run writes nothing."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta

import yaml
from ct_fixtures import CALENDAR_ID, JST, Workspace

import ct_service


def _days_from_now(days: int) -> str:
    # The CLI reads the real clock, so its fixtures are relative to it.
    return (datetime.now(JST) + timedelta(days=days)).replace(
        hour=17, minute=0, second=0, microsecond=0).isoformat()


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.ws.add_commitment("c1", "協賛ロゴの入稿", due_at=_days_from_now(30),
                               due_precision="day")
        self.ws.add_commitment("c2", "見積書の送付", due_at=_days_from_now(-30),
                               due_precision="day")

    def config(self, **extra):
        data = {
            "paths": {"state_db": self.ws.state_path, "events_db": self.ws.events_path,
                      "calendar_index_db": self.ws.index_path,
                      "gmail_index_db": self.ws.gmail_path},
            "calendar": {"calendar_id": CALENDAR_ID},
            "selection": {"horizon_days": 366},
        }
        for key, value in extra.items():
            data.setdefault(key, {}).update(value)
        path = os.path.join(self.ws.dir, "config.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, allow_unicode=True)
        return path

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = ct_service.main(list(args))
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None)


class ServiceTests(ServiceCase):
    def test_plan_shows_titles_and_writes_nothing(self):
        code, data = self.cli("--config", self.config(), "plan")
        self.assertEqual(code, 0)
        self.assertEqual(data["mode"], "plan")
        self.assertFalse(os.path.exists(self.ws.state_path))
        self.assertEqual(data["skipped"], {"past": 1})
        self.assertEqual(len(data["plan"]["create"]), 1)
        self.assertIn("協賛ロゴの入稿", data["plan"]["create"][0])

    def test_dry_run_mode_prints_counts_not_titles(self):
        code, data = self.cli("--config", self.config(behaviour={"mode": "dry_run"}), "run")
        self.assertEqual(code, 0)
        self.assertEqual(data["plan"]["create"], 1)
        self.assertNotIn("協賛", json.dumps(data, ensure_ascii=False))
        self.assertFalse(os.path.exists(self.ws.state_path))

    def test_a_calendar_other_than_primary_is_a_configuration_stop(self):
        code, _ = self.cli("--config", self.config(
            calendar={"calendar_id": "tentative@group.calendar.google.com"}), "run")
        self.assertEqual(code, ct_service.EXIT_CONFIG)

    def test_run_without_credentials_is_a_configuration_stop(self):
        empty = os.path.join(self.ws.dir, "no-creds")
        os.makedirs(empty)
        code, _ = self.cli("--config", self.config(oauth={"credential_dir": empty}), "run")
        self.assertEqual(code, ct_service.EXIT_CONFIG)

    def test_a_malformed_config_exits_78(self):
        path = os.path.join(self.ws.dir, "bad.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("calendar: {calendar_id: somebody@example.com}\n")
        code, _ = self.cli("--config", path, "status")
        self.assertEqual(code, ct_service.EXIT_CONFIG)

    def test_check_accepts_our_own_entries_in_h1(self):
        # They are on the primary calendar, which H.1 reads; selection
        # ignores them by id, so check only reports them.
        self.ws.add_index_event("hermesct" + "0" * 40, "【仮】x", "2026-10-05", all_day=True,
                                calendar_id="owner@example.com")
        code, _ = self.cli("--config", self.config(), "check")
        self.assertEqual(code, ct_service.EXIT_OK)

    def test_plan_screens_gmail_only_rows_through_the_index(self):
        self.ws.add_mail("gmail:sent", is_sent=True, to="client@example.org")
        self.ws.add_mail("gmail:promo", labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.ws.add_commitment("mine", "請求書を送る", due_at=_days_from_now(10), due_precision="day",
                               sources=[("gmail", "gmail:sent")])
        self.ws.add_commitment("ad", "セール最終日", due_at=_days_from_now(10), due_precision="day",
                               sources=[("gmail", "gmail:promo")])
        code, data = self.cli("--config", self.config(), "plan")
        self.assertEqual(code, 0)
        self.assertTrue(data["gmail_screen_available"])
        self.assertEqual(data["skipped"].get("gmail_bulk"), 1)
        self.assertTrue(any("請求書を送る" in line for line in data["plan"]["create"]))
        self.assertFalse(any("セール" in line for line in data["plan"]["create"]))

    def test_without_the_gmail_index_mail_only_rows_are_held_back(self):
        self.ws.add_commitment("mine", "請求書を送る", due_at=_days_from_now(10), due_precision="day",
                               sources=[("gmail", "gmail:sent")])
        missing = os.path.join(self.ws.dir, "absent.db")
        code, data = self.cli("--config", self.config(paths={"gmail_index_db": missing}), "plan")
        self.assertEqual(code, 0)
        self.assertFalse(data["gmail_screen_available"])
        self.assertEqual(data["skipped"].get("gmail_unscreened"), 1)

    def test_status_without_state_is_not_an_error(self):
        code, data = self.cli("--config", self.config(), "status")
        self.assertEqual(code, 0)
        self.assertEqual(data["state_db"], "absent")

    def test_the_readonly_connection_refuses_writes(self):
        con = ct_service.open_readonly(self.ws.events_path)
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("UPDATE commitments SET title = 'x'")


if __name__ == "__main__":
    unittest.main()
