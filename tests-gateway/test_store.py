#!/usr/bin/env python3
"""G.1 unit tests — the durable queue.

What is being tested is crash behaviour, expressed as invariants that hold no
matter where a worker stops: a message is never processed twice, answers
within a conversation never overtake each other, and a reservation for an
outbound chunk can be taken exactly once (spec sections 29, 31, 61, 62, 118,
119).
"""

import sqlite3
import unittest

import gw_fixtures as fx
import gw_store as store
from gw_canonical import canonical_from_line
from gw_schema import SCHEMA_VERSION, init_db, integrity_ok


def mem_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    return init_db(con)


def owner_message(message_id="m1", text="やあ", user_id=fx.OWNER_LINE):
    return canonical_from_line(
        fx.line_text_event(text=text, message_id=message_id, user_id=user_id),
        owner_line_user_id=fx.OWNER_LINE,
    )


class TestSchema(unittest.TestCase):
    def test_init_is_idempotent(self):
        con = mem_db()
        init_db(con)
        init_db(con)
        self.assertTrue(integrity_ok(con))

    def test_version_recorded(self):
        self.assertIsInstance(SCHEMA_VERSION, int)


class TestDeduplication(unittest.TestCase):
    def test_same_platform_message_recorded_once(self):
        con = mem_db()
        msg = owner_message()
        rid1, new1 = store.record_inbound(con, msg)
        rid2, new2 = store.record_inbound(con, msg)
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(rid1, rid2)
        count = con.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0]
        self.assertEqual(count, 1)

    def test_redelivery_is_a_duplicate(self):
        """LINE redelivers with the same message id; the row must not double."""
        con = mem_db()
        store.record_inbound(con, owner_message())
        redelivered = canonical_from_line(
            fx.line_text_event(message_id="m1", redelivery=True),
            owner_line_user_id=fx.OWNER_LINE,
        )
        _, is_new = store.record_inbound(con, redelivered)
        self.assertFalse(is_new)

    def test_distinct_messages_both_recorded(self):
        con = mem_db()
        store.record_inbound(con, owner_message("m1"))
        _, is_new = store.record_inbound(con, owner_message("m2"))
        self.assertTrue(is_new)

    def test_unauthenticated_lands_in_received_not_authenticated(self):
        con = mem_db()
        stranger = canonical_from_line(
            fx.line_text_event(user_id=fx.STRANGER_LINE),
            owner_line_user_id=fx.OWNER_LINE,
        )
        rid, _ = store.record_inbound(con, stranger)
        self.assertEqual(store.get_inbound(con, rid)["state"], "RECEIVED")


class TestClaiming(unittest.TestCase):
    def test_claims_in_arrival_order(self):
        con = mem_db()
        for i in range(3):
            m = canonical_from_line(
                fx.line_text_event(message_id=f"m{i}", timestamp=1767225600000 + i),
                owner_line_user_id=fx.OWNER_LINE,
            )
            rid, _ = store.record_inbound(con, m)
            store.enqueue(con, rid)
        # One conversation, so only one may be in flight; drain in order.
        seen = []
        for _ in range(3):
            row = store.claim_next(con, max_active=4)
            if row is None:
                break
            seen.append(row["platform_message_id"])
            store.set_state(con, row["gateway_request_id"], "RESPONDED")
            store.release_conversation(con, row["conversation_id"])
        self.assertEqual(seen, ["m0", "m1", "m2"])

    def test_one_in_flight_per_conversation(self):
        """A second message from the same chat waits (section 119)."""
        con = mem_db()
        for i in range(2):
            rid, _ = store.record_inbound(con, owner_message(f"m{i}"))
            store.enqueue(con, rid)
        first = store.claim_next(con, max_active=4)
        self.assertIsNotNone(first)
        self.assertIsNone(store.claim_next(con, max_active=4))

    def test_different_conversations_run_concurrently(self):
        con = mem_db()
        a = owner_message("m1")
        b = canonical_from_line(
            fx.line_text_event(message_id="m2", user_id=fx.STRANGER_LINE),
            owner_line_user_id=fx.STRANGER_LINE,   # a second authorized chat
        )
        for m in (a, b):
            rid, _ = store.record_inbound(con, m)
            store.enqueue(con, rid)
        self.assertIsNotNone(store.claim_next(con, max_active=4))
        self.assertIsNotNone(store.claim_next(con, max_active=4))

    def test_global_bound_respected(self):
        con = mem_db()
        for i in range(3):
            m = canonical_from_line(
                fx.line_text_event(message_id=f"m{i}",
                                   user_id="U" + f"{i:032x}"),
                owner_line_user_id="U" + f"{i:032x}",
            )
            rid, _ = store.record_inbound(con, m)
            store.enqueue(con, rid)
        self.assertIsNotNone(store.claim_next(con, max_active=2))
        self.assertIsNotNone(store.claim_next(con, max_active=2))
        self.assertIsNone(store.claim_next(con, max_active=2))

    def test_empty_queue_returns_none(self):
        self.assertIsNone(store.claim_next(mem_db(), max_active=2))


class TestRecovery(unittest.TestCase):
    def test_stale_claim_is_requeued(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        store.enqueue(con, rid)
        store.claim_next(con, max_active=2)
        # Pretend the worker died five minutes ago.
        con.execute(
            "UPDATE inbound_messages SET claimed_at = ? WHERE gateway_request_id = ?",
            (store._iso_minus(9999), rid),
        )
        result = store.recover_stale(con, lease_seconds=300)
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(store.get_inbound(con, rid)["state"], "QUEUED")

    def test_exhausted_attempts_fail_instead_of_looping(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        store.enqueue(con, rid)
        store.claim_next(con, max_active=2)
        con.execute(
            "UPDATE inbound_messages SET claimed_at = ?, attempts = 9"
            " WHERE gateway_request_id = ?",
            (store._iso_minus(9999), rid),
        )
        result = store.recover_stale(con, lease_seconds=300, max_attempts=3)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(store.get_inbound(con, rid)["state"], "FAILED")

    def test_fresh_claim_is_left_alone(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        store.enqueue(con, rid)
        store.claim_next(con, max_active=2)
        result = store.recover_stale(con, lease_seconds=300)
        self.assertEqual(result, {"requeued": 0, "failed": 0, "reply_unknown": 0, "reply_confirmed": 0})


class TestOutboundReservation(unittest.TestCase):
    def _reserve(self, con, rid, index=0):
        return store.reserve_outbound(
            con, request_id=rid, platform="line",
            conversation_id=f"line:{fx.OWNER_LINE}",
            target_user_id=fx.OWNER_LINE, body="answer",
            chunk_index=index, chunk_total=1, delivery_mode="reply",
        )

    def test_reservation_is_once_only(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        oid1, new1 = self._reserve(con, rid)
        oid2, new2 = self._reserve(con, rid)
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(oid1, oid2)

    def test_chunks_are_distinct_reservations(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        oid0, _ = self._reserve(con, rid, 0)
        oid1, new = self._reserve(con, rid, 1)
        self.assertNotEqual(oid0, oid1)
        self.assertTrue(new)

    def test_already_delivered_requires_every_chunk_sent(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        oid0, _ = self._reserve(con, rid, 0)
        oid1, _ = self._reserve(con, rid, 1)
        self.assertFalse(store.already_delivered(con, rid))
        store.mark_outbound(con, oid0, "SENT")
        self.assertFalse(store.already_delivered(con, rid))
        store.mark_outbound(con, oid1, "SENT")
        self.assertTrue(store.already_delivered(con, rid))

    def test_no_reservations_is_not_delivered(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        self.assertFalse(store.already_delivered(con, rid))


class TestSessions(unittest.TestCase):
    def test_stable_per_conversation(self):
        con = mem_db()
        a = store.session_id_for(con, f"line:{fx.OWNER_LINE}", "line")
        b = store.session_id_for(con, f"line:{fx.OWNER_LINE}", "line")
        self.assertEqual(a, b)

    def test_platforms_never_share_a_session(self):
        """A half-finished LINE thread must not surface in a Discord DM."""
        con = mem_db()
        a = store.session_id_for(con, f"line:{fx.OWNER_LINE}", "line")
        b = store.session_id_for(con, f"discord:{fx.DM_CHANNEL_ID}", "discord")
        self.assertNotEqual(a, b)

    def test_session_id_has_no_colon(self):
        con = mem_db()
        sid = store.session_id_for(con, f"line:{fx.OWNER_LINE}", "line")
        self.assertNotIn(":", sid)


class TestRetention(unittest.TestCase):
    def test_old_text_cleared_but_row_survives(self):
        con = mem_db()
        rid, _ = store.record_inbound(con, owner_message())
        con.execute(
            "UPDATE inbound_messages SET received_at = ? WHERE gateway_request_id = ?",
            (store._iso_minus(30 * 86400), rid),
        )
        result = store.purge_expired(con, retention_days=14)
        self.assertEqual(result["inbound_cleared"], 1)
        row = store.get_inbound(con, rid)
        self.assertIsNotNone(row)          # dedupe still works
        self.assertIsNone(row["text"])     # content is gone
        self.assertIsNone(row["reply_token"])

    def test_recent_text_is_kept(self):
        con = mem_db()
        store.record_inbound(con, owner_message())
        result = store.purge_expired(con, retention_days=14)
        self.assertEqual(result["inbound_cleared"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
