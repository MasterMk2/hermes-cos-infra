"""Persistent unknown delivery is a hold, not success or a retry licence."""

import att_fixtures as fx
import att_gateway
import att_store as store
from test_engine import EngineCase


class TestUnknownOutcome(EngineCase):
    def notification(self, con):
        row, _ = store.create_notification(
            con, dedupe_key="unknown:fixture", severity="CRITICAL",
            subject="fixture", body="合成テストの本文", reason="test")
        con.commit()
        return row

    def test_client_keeps_the_gateway_hold_distinct_from_transport_loss(self):
        client = att_gateway.GatewayClient("/unused-fixture.sock")
        client._request = lambda *a: (
            409, {"status": "outcome_unknown", "detail": "delivery_outcome_unknown"})
        result = client.notify(notification_id="fixture", channel="discord",
                               severity="IMPORTANT", text="fixture")
        self.assertEqual(result.status, att_gateway.OUTCOME_UNKNOWN)
        self.assertFalse(result.ok)
        self.assertFalse(result.certainly_unsent)

    def test_unknown_survives_reopen_and_a_stale_caller_snapshot(self):
        gw = fx.FakeGateway(answers=[att_gateway.Result(att_gateway.OUTCOME_UNKNOWN)])
        eng = self.engine(gateway=gw)
        con = eng.db()
        notification = self.notification(con)
        self.assertFalse(eng.deliver(con, notification, ["discord"], None))
        row = store.get_notification(con, notification["notification_id"])
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertIsNone(row["delivered_at"])
        self.assertEqual(len(gw.calls), 1)
        con.close()

        eng = self.engine(gateway=gw)
        con = eng.db()
        self.assertFalse(eng.deliver(con, notification, ["discord"], None))
        self.assertEqual(len(gw.calls), 1)
        self.assertEqual(store.stuck_notifications(con), [])
        con.close()

    def test_uncertain_line_reservation_is_neither_released_nor_claimed_spent(self):
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            answers=[att_gateway.Result(att_gateway.OUTCOME_UNKNOWN,
                                                        channel="line")])
        eng = self.engine(gateway=gw)
        con = eng.db()
        notification = self.notification(con)
        self.assertFalse(eng.deliver(con, notification, ["line"], None))
        reservation = con.execute("SELECT * FROM channel_budget").fetchone()
        self.assertEqual(reservation["state"], "RESERVED")
        self.assertEqual(store.get_notification(con, notification["notification_id"])["state"],
                         "UNKNOWN")
        con.close()
