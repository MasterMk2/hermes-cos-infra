#!/usr/bin/env python3
"""G.2 unit tests — the LINE budget (spec sections 119, 120, 151).

Every number in these tests comes from a quota payload, never from a constant
in the engine. That is the point of section 6: the monthly allowance is
whatever LINE says it is, and a hardcoded 200 keeps spending confidently
against a figure that has become wrong.

The two cases most likely to be got backwards, and therefore tested most
carefully:

    type "none"  -> no target limit is set. HEALTHY, not UNKNOWN.
    a failed read -> UNKNOWN. Urgent and critical only, and barely.
"""

import unittest

import att_budget as budget
import att_policy as policy
import att_store as store
import att_fixtures as fx


def evaluate(payload, cfg=None, at="2026-09-04T12:00", spent_today=0):
    cfg = cfg or fx.config()
    return budget.evaluate(payload, cfg, fx.clock_at(at),
                           spent_today=spent_today)


class TestClassification(unittest.TestCase):
    def test_states(self):
        for payload, expected in (
            (fx.QUOTA_HEALTHY, budget.HEALTHY),
            (fx.QUOTA_CONSERVE, budget.CONSERVE),
            (fx.QUOTA_CRITICAL, budget.CRITICAL),
            (fx.QUOTA_EXHAUSTED, budget.EXHAUSTED),
            (fx.QUOTA_UNKNOWN, budget.UNKNOWN),
            (None, budget.UNKNOWN),
        ):
            with self.subTest(state=expected):
                self.assertEqual(evaluate(payload).state, expected)

    def test_no_target_limit_is_healthy_not_unknown(self):
        # Section 6. `type: "none"` means the account has no cap set. Mapping
        # it to UNKNOWN would throttle a channel that is not throttled.
        b = evaluate(fx.QUOTA_UNLIMITED)
        self.assertEqual(b.state, budget.HEALTHY)
        self.assertTrue(b.unlimited)
        self.assertTrue(b.ok)

    def test_an_unlimited_account_still_has_a_daily_cap(self):
        # Section 10: an uncapped plan is not a reason to make LINE the
        # routine feed.
        b = evaluate(fx.QUOTA_UNLIMITED)
        cfg = fx.config()
        self.assertLessEqual(b.daily_allowance,
                             cfg.line_budget["max_per_day_hard_cap"])

    def test_unknown_never_reports_numbers(self):
        b = evaluate(fx.QUOTA_UNKNOWN)
        self.assertFalse(b.ok)
        self.assertIsNone(b.remaining)
        self.assertFalse(b.unlimited)
        self.assertLessEqual(b.daily_allowance,
                             fx.config().line_budget["unknown_max_per_day"])


class TestDailyAllowance(unittest.TestCase):
    """Sections 99, 100, 101 — derived, never arithmetic on a fixed number."""

    def test_the_reserve_is_taken_out_before_the_daily_share(self):
        b = evaluate(fx.QUOTA_HEALTHY)          # 190 of 200 left
        self.assertEqual(b.reserve, 50)         # 25% of the reported target
        self.assertEqual(b.routine_pool, 140)

    def test_the_allowance_scales_with_the_days_left(self):
        early = evaluate(fx.QUOTA_HEALTHY, at="2026-09-01T12:00")
        late = evaluate(fx.QUOTA_HEALTHY, at="2026-09-29T12:00")
        # Same pool, fewer days: a bigger daily share near the month's end.
        self.assertLess(early.daily_allowance, late.daily_allowance)

    def test_the_allowance_is_not_the_whole_remaining_pool(self):
        # Section 100's reserve factor: today never gets to spend everything.
        b = evaluate(fx.QUOTA_HEALTHY, at="2026-09-30T12:00")
        self.assertLess(b.daily_allowance, b.routine_pool)

    def test_nothing_here_is_derived_from_a_hardcoded_monthly_figure(self):
        # Section 99. Halve the reported target and every derived number
        # halves with it; a constant in the code could not do that.
        halved = dict(fx.QUOTA_HEALTHY, limit=100, remaining=95, used=5)
        a = evaluate(fx.QUOTA_HEALTHY)
        b = evaluate(halved)
        self.assertEqual(a.reserve, 50)
        self.assertEqual(b.reserve, 25)
        self.assertNotEqual(a.routine_pool, b.routine_pool)

    def test_an_exhausted_month_has_no_allowance(self):
        self.assertEqual(evaluate(fx.QUOTA_EXHAUSTED).daily_allowance, 0)


class TestPermission(unittest.TestCase):
    """Section 119's five named cases, plus the reserve."""

    def cfg(self, **channels):
        return fx.config(channels=channels) if channels else fx.config()

    def test_healthy_plus_urgent_allows_line(self):
        ok, reason = budget.line_allowed(evaluate(fx.QUOTA_HEALTHY), "URGENT",
                                         self.cfg())
        self.assertTrue(ok, reason)

    def test_conserve_plus_routine_refuses_line(self):
        ok, reason = budget.line_allowed(evaluate(fx.QUOTA_CONSERVE), "INFO",
                                         self.cfg())
        self.assertFalse(ok)
        self.assertEqual(reason, budget.R_ROUTINE)

    def test_critical_budget_plus_important_refuses_line(self):
        ok, reason = budget.line_allowed(evaluate(fx.QUOTA_CRITICAL),
                                         "IMPORTANT", self.cfg())
        self.assertFalse(ok)

    def test_critical_budget_still_allows_a_critical_notification(self):
        ok, _ = budget.line_allowed(evaluate(fx.QUOTA_CRITICAL), "CRITICAL",
                                    self.cfg())
        self.assertTrue(ok)

    def test_exhausted_refuses_even_critical(self):
        # Section 83: when LINE physically cannot deliver, the notification
        # goes to Discord. It is never dropped, and the fallback is what
        # `channels_for` produces.
        b = evaluate(fx.QUOTA_EXHAUSTED)
        ok, reason = budget.line_allowed(b, "CRITICAL", self.cfg())
        self.assertFalse(ok)
        self.assertEqual(reason, budget.R_EXHAUSTED)
        chans, _ = policy.channels_for("CRITICAL", self.cfg(), b)
        self.assertEqual(chans, ["discord"])

    def test_unknown_refuses_routine_and_rations_urgent(self):
        b = evaluate(fx.QUOTA_UNKNOWN)
        self.assertFalse(budget.line_allowed(b, "INFO", self.cfg())[0])
        self.assertFalse(budget.line_allowed(b, "IMPORTANT", self.cfg())[0])
        self.assertTrue(budget.line_allowed(b, "URGENT", self.cfg())[0])
        spent = evaluate(fx.QUOTA_UNKNOWN, spent_today=1)
        self.assertFalse(budget.line_allowed(spent, "URGENT", self.cfg())[0])

    def test_the_daily_allowance_stops_routine_escalation(self):
        b = evaluate(fx.QUOTA_HEALTHY, spent_today=99)
        ok, reason = budget.line_allowed(b, "URGENT", self.cfg())
        self.assertFalse(ok)
        self.assertEqual(reason, budget.R_DAILY_SPENT)

    def test_critical_may_exceed_the_daily_share(self):
        # That is what the emergency reserve is for (section 101).
        b = evaluate(fx.QUOTA_HEALTHY, spent_today=99)
        ok, reason = budget.line_allowed(b, "CRITICAL", self.cfg())
        self.assertTrue(ok)
        self.assertEqual(reason, "line_emergency_override")

    def test_routine_may_not_touch_the_reserve(self):
        nearly = {"ok": True, "quota_type": "limited", "limit": 200,
                  "used": 160, "remaining": 40, "unlimited": False}
        b = evaluate(nearly)          # reserve 50 > remaining 40
        self.assertEqual(b.routine_pool, 0)
        self.assertFalse(budget.line_allowed(b, "URGENT", self.cfg())[0])
        self.assertTrue(budget.line_allowed(b, "CRITICAL", self.cfg())[0])

    def test_escalation_can_be_switched_off_entirely(self):
        cfg = fx.config(channels={"line_escalation_enabled": False})
        ok, reason = budget.line_allowed(evaluate(fx.QUOTA_HEALTHY),
                                         "CRITICAL", cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, budget.R_DISABLED)

    def test_routine_line_can_be_switched_on_deliberately(self):
        cfg = fx.config(channels={"routine_line_enabled": True})
        ok, _ = budget.line_allowed(evaluate(fx.QUOTA_HEALTHY), "IMPORTANT",
                                    cfg)
        self.assertTrue(ok)


class TestChannelPolicy(unittest.TestCase):
    """Section 9's table, and section 53's "severity is not a channel"."""

    def test_routine_is_discord(self):
        cfg = fx.config()
        b = evaluate(fx.QUOTA_HEALTHY)
        for severity in ("INFO", "IMPORTANT"):
            self.assertEqual(policy.channels_for(severity, cfg, b)[0],
                             ["discord"])

    def test_urgent_is_discord_then_line(self):
        chans, _ = policy.channels_for("URGENT", fx.config(),
                                       evaluate(fx.QUOTA_HEALTHY))
        self.assertEqual(chans, ["discord", "line"])

    def test_critical_is_line_first(self):
        chans, _ = policy.channels_for("CRITICAL", fx.config(),
                                       evaluate(fx.QUOTA_HEALTHY))
        self.assertEqual(chans, ["line", "discord"])

    def test_a_poor_budget_downgrades_an_important_notification_to_discord(self):
        # Section 53 stated exactly: IMPORTANT plus a critical budget is still
        # delivered — on Discord.
        chans, _ = policy.channels_for("IMPORTANT", fx.config(),
                                       evaluate(fx.QUOTA_CRITICAL))
        self.assertEqual(chans, ["discord"])

    def test_with_discord_off_urgent_still_reaches_line(self):
        cfg = fx.config(channels={"discord_enabled": False})
        chans, _ = policy.channels_for("URGENT", cfg,
                                       evaluate(fx.QUOTA_HEALTHY))
        self.assertEqual(chans, ["line"])


class TestReservation(unittest.TestCase):
    """Section 82 — the reservation, not just the receipt."""

    def test_the_allowance_cannot_be_oversubscribed(self):
        con = fx.db()
        taken = []
        for i in range(5):
            ok, _ = store.reserve_channel_budget(
                con, channel="line", notification_id=f"ntf_{i}",
                severity="URGENT", day="2026-09-04", month="2026-09",
                allowance=2)
            taken.append(ok)
        con.commit()
        self.assertEqual(taken, [True, True, False, False, False])
        self.assertEqual(store.spent_today(con, "line", "2026-09-04"), 2)

    def test_a_retry_of_the_same_notification_costs_nothing_more(self):
        con = fx.db()
        for _ in range(3):
            ok, _ = store.reserve_channel_budget(
                con, channel="line", notification_id="ntf_same",
                severity="URGENT", day="2026-09-04", month="2026-09",
                allowance=1)
            self.assertTrue(ok)
        con.commit()
        self.assertEqual(store.spent_today(con, "line", "2026-09-04"), 1)

    def test_a_released_reservation_frees_the_credit(self):
        con = fx.db()
        store.reserve_channel_budget(
            con, channel="line", notification_id="ntf_a", severity="URGENT",
            day="2026-09-04", month="2026-09", allowance=1)
        store.settle_budget(con, channel="line", notification_id="ntf_a",
                            spent=False)
        con.commit()
        self.assertEqual(store.spent_today(con, "line", "2026-09-04"), 0)
        ok, _ = store.reserve_channel_budget(
            con, channel="line", notification_id="ntf_b", severity="URGENT",
            day="2026-09-04", month="2026-09", allowance=1)
        self.assertTrue(ok)

    def test_the_day_key_is_local_not_utc(self):
        # 08:00 JST on the 4th is 23:00 UTC on the 3rd. A ledger bucketed in
        # UTC would put a morning notification on the previous day's budget.
        clock = fx.clock_at("2026-09-04T08:00")
        self.assertEqual(clock.day_key(), "2026-09-04")
        self.assertEqual(clock.now().strftime("%Y-%m-%d"), "2026-09-03")


if __name__ == "__main__":
    unittest.main()
