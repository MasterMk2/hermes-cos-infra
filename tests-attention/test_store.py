#!/usr/bin/env python3
"""G.2 unit tests — the durable store's write behaviour.

Section 138: the state database is on a USB flash disk and the engine runs
288 times a day. What is asserted here is that a scan which discovers nothing
new writes nothing new — the difference between an idle engine costing a row
a scan and costing a row per open signal per scan.
"""

import unittest

import att_store as store
import att_fixtures as fx
from att_signals import make_signal


def signal(**over):
    base = dict(source_type="event", source_id="evt_1", signal_type="due_soon",
                created_at="2026-09-04T00:00:00+00:00", subject="s",
                summary="やること", key_parts=("event", "evt_1", "due-soon", "3h"),
                severity="IMPORTANT", confidence=0.9, actionability=0.8,
                urgency=0.6)
    base.update(over)
    return make_signal(**base)


def writes(con) -> int:
    return con.total_changes


class TestIdleScansDoNotWrite(unittest.TestCase):
    def test_the_same_signal_twice_inserts_once(self):
        con = fx.db()
        row, new1 = store.upsert_signal(con, signal())
        self.assertTrue(new1)
        row2, new2 = store.upsert_signal(con, signal())
        self.assertFalse(new2)
        self.assertEqual(row["signal_id"], row2["signal_id"])
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)

    def test_an_unchanged_signal_costs_no_write(self):
        con = fx.db()
        store.upsert_signal(con, signal())
        before = writes(con)
        for _ in range(20):
            store.upsert_signal(con, signal())
        self.assertEqual(writes(con), before,
                         "an unchanged signal was rewritten")

    def test_a_changed_signal_is_written(self):
        con = fx.db()
        store.upsert_signal(con, signal())
        before = writes(con)
        store.upsert_signal(con, signal(severity="URGENT"))
        self.assertGreater(writes(con), before)
        self.assertEqual(store.get_signal(
            con, signal().signal_id)["severity"], "URGENT")

    def test_a_new_due_date_is_written(self):
        con = fx.db()
        store.upsert_signal(con, signal())
        before = writes(con)
        store.upsert_signal(con, signal(due_at="2026-09-05T00:00:00+00:00"))
        self.assertGreater(writes(con), before)

    def test_observed_at_is_never_overwritten(self):
        # Section 25: `observed_at` is when we FIRST saw it. Refreshing it
        # would make every backfilled item look like it arrived just now.
        con = fx.db()
        first, _ = store.upsert_signal(con, signal())
        store.upsert_signal(con, signal(severity="URGENT"))
        again = store.get_signal(con, first["signal_id"])
        self.assertEqual(first["observed_at"], again["observed_at"])


if __name__ == "__main__":
    unittest.main()
