"""The run loop against a fake Google: idempotency, lost answers, the owner."""

from __future__ import annotations

import json
import unittest
from datetime import timedelta

from ct_fixtures import CALENDAR_ID, NOW, FakeCalendar, Workspace, make_cfg

import ct_engine
import ct_select as sel
import ct_store as store
from ct_google import ApiError

DUE = {"due_at": "2026-10-05T17:00:00+09:00", "due_precision": "day"}


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.cfg = make_cfg()
        self.google = FakeCalendar()
        self.state = store.open_db(self.ws.state_path)
        self.addCleanup(self.state.close)

    def run_once(self, cfg=None, *, now=NOW, notifier=None):
        cfg = cfg or self.cfg
        selection = sel.select(self.ws.events, cfg=cfg, now=now, index_con=self.ws.index)
        return ct_engine.run(cfg=cfg, state_con=self.state, selection=selection,
                             client=self.google, now=now, notifier=notifier)

    def row(self, cid):
        return store.get(self.state, cid)

    def state_of(self, cid):
        return self.row(cid)["state"]

    def event_of(self, cid):
        return self.google.events[self.row(cid)["event_id"]]


class CreateTests(EngineCase):
    def test_first_run_creates_tentative_entries_with_fixed_ids(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.ws.add_commitment("c2", "定例ミーティング", due_at="2026-10-03T18:00:00+09:00",
                               due_precision="minute", raw_type="appointment_candidate")
        report = self.run_once()
        self.assertEqual(report.counts.get("created"), 2)
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        event = self.event_of("c1")
        self.assertEqual(event["id"], sel.event_id_for(CALENDAR_ID, "c1", 1))
        self.assertEqual(event["summary"], "[仮] 〆 協賛ロゴの入稿")
        self.assertEqual(event["status"], "tentative")
        self.assertEqual(event["transparency"], "transparent")
        self.assertEqual(event["extendedProperties"]["private"],
                         {"hermes_ct": "1", "hermes_commitment": "c1"})
        self.assertEqual(self.event_of("c2")["start"]["dateTime"], "2026-10-03T18:00:00+09:00")

    def test_a_second_run_changes_nothing(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.run_once()
        self.google.calls.clear()
        report = self.run_once()
        self.assertNotIn("insert", self.google.verbs())
        self.assertNotIn("patch", self.google.verbs())
        self.assertNotIn("created", report.counts)
        self.assertEqual(len(self.google.events), 1)

    def test_entries_are_created_soonest_first_within_the_per_run_cap(self):
        for day in range(2, 8):
            self.ws.add_commitment(f"c{day}", f"作業{day}",
                                   due_at=f"2026-10-0{day}T12:00:00+09:00", due_precision="day")
        cfg = make_cfg(limits={"max_creates_per_run": 2})
        report = self.run_once(cfg)
        self.assertEqual(report.counts["created"], 2)
        self.assertEqual(report.counts["create_deferred"], 4)
        self.assertEqual({self.state_of("c2"), self.state_of("c3")}, {store.ACTIVE})
        self.assertEqual(self.state_of("c7"), store.PENDING)
        self.run_once(cfg)
        self.assertEqual(len(self.google.live()), 4)

    def test_max_active_bounds_the_whole_calendar(self):
        for day in range(2, 6):
            self.ws.add_commitment(f"c{day}", f"作業{day}",
                                   due_at=f"2026-10-0{day}T12:00:00+09:00", due_precision="day")
        cfg = make_cfg(limits={"max_active": 3})
        self.run_once(cfg)
        self.run_once(cfg)
        self.assertEqual(len(self.google.live()), 3)

    def test_a_reservation_that_is_no_longer_wanted_is_forgotten(self):
        for day in range(2, 4):
            self.ws.add_commitment(f"c{day}", f"作業{day}",
                                   due_at=f"2026-10-0{day}T12:00:00+09:00", due_precision="day")
        cfg = make_cfg(limits={"max_creates_per_run": 1})
        self.run_once(cfg)
        self.assertEqual(self.state_of("c3"), store.PENDING)
        self.ws.update_commitment("c3", work_state="DONE")
        report = self.run_once(cfg)
        self.assertIsNone(self.row("c3"))
        self.assertEqual(report.counts["dropped_pending"], 1)
        self.assertEqual(len(self.google.events), 1)


class LostAnswerTests(EngineCase):
    def test_an_insert_whose_answer_was_lost_is_read_back_not_repeated(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.google.fail["insert"] = [("apply_then_raise", ApiError(None, "TimeoutError"))]
        report = self.run_once()
        self.assertEqual(self.state_of("c1"), store.UNKNOWN)
        self.assertEqual(report.counts["held_unknown"], 1)
        report = self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertEqual(report.counts["resolved_created"], 1)
        self.assertEqual(self.google.verbs().count("insert"), 1)
        self.assertEqual(len(self.google.events), 1)

    def test_an_insert_that_never_arrived_is_retried_after_google_says_so(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.google.fail["insert"] = [("raise", ApiError(None, "TimeoutError"))]
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.UNKNOWN)
        self.run_once()  # GET -> 404 -> PENDING -> insert
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertEqual(self.google.verbs().count("insert"), 2)
        self.assertEqual(len(self.google.events), 1)

    def test_a_crash_mid_insert_is_treated_as_unknown(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        cfg = make_cfg(limits={"max_creates_per_run": 0})
        self.run_once(cfg)
        self.assertTrue(store.transition(self.state, "c1", store.INSERTING, expect=store.PENDING))
        self.state.commit()
        # The process died after Google accepted the event.
        row = self.row("c1")
        body = json.loads(row["body_json"])
        self.google.insert_event(CALENDAR_ID, ct_engine.insert_resource(
            body, event_id=row["event_id"], commitment_id="c1"))
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertEqual(len(self.google.events), 1)

    def test_a_duplicate_id_on_insert_is_resolved_by_reading_it(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        cfg = make_cfg(limits={"max_creates_per_run": 0})
        self.run_once(cfg)
        row = self.row("c1")
        self.google.insert_event(CALENDAR_ID, ct_engine.insert_resource(
            json.loads(row["body_json"]), event_id=row["event_id"], commitment_id="c1"))
        report = self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertEqual(report.counts["resolved_created"], 1)

    def test_a_refusal_is_retried_then_gives_up_but_an_outage_never_does(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.ws.add_commitment("c2", "見積書の送付", **DUE)
        cfg = make_cfg(limits={"max_attempts": 2})
        self.google.fail["insert"] = [
            ("raise", ApiError(400, "invalid")),
            ("raise", ApiError(None, "gaierror", sent=False)),
            ("raise", ApiError(400, "invalid")),
            ("raise", ApiError(None, "gaierror", sent=False)),
            ("raise", ApiError(None, "gaierror", sent=False)),
        ]
        self.run_once(cfg)
        self.run_once(cfg)
        self.assertEqual(self.state_of("c1"), store.FAILED)
        self.assertEqual(self.state_of("c2"), store.PENDING)
        self.run_once(cfg)
        self.assertEqual(self.state_of("c2"), store.PENDING)
        self.run_once(cfg)
        self.assertEqual(self.state_of("c2"), store.ACTIVE)

    def test_an_oauth_outage_stops_the_run_before_anything_changes(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.google.auth_error = ApiError(400, "invalid_grant", sent=False)
        with self.assertRaises(ApiError):
            self.run_once()
        self.assertIsNone(self.row("c1"))
        self.assertEqual(self.google.calls, [])


class OwnerTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.run_once()
        self.event_id = self.row("c1")["event_id"]

    def test_a_deleted_entry_is_rejected_and_never_recreated(self):
        self.google.owner_delete(self.event_id)
        report = self.run_once()
        self.assertEqual(self.state_of("c1"), store.REJECTED)
        self.assertEqual(report.counts["rejected"], 1)
        self.run_once()
        self.assertEqual(self.google.verbs().count("insert"), 1)
        self.assertTrue(store.is_retired(self.state, self.event_id))

    def test_an_entry_moved_to_a_real_calendar_is_confirmed(self):
        moved = self.google.owner_move_away(self.event_id)
        self.ws.add_index_event(self.event_id, moved["summary"], "2026-10-05", all_day=True)
        report = self.run_once()
        self.assertEqual(self.state_of("c1"), store.CONFIRMED)
        self.assertEqual(report.counts["confirmed"], 1)

    def test_an_edited_entry_is_left_alone_from_then_on(self):
        self.google.owner_edit(self.event_id, summary="協賛ロゴ 入稿（確定）")
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.USER_EDITED)
        self.ws.update_commitment("c1", due_at="2026-10-06T17:00:00+09:00")
        self.google.calls.clear()
        self.run_once()
        self.assertEqual([v for v in self.google.verbs() if v in ("patch", "delete", "insert")], [])
        self.assertEqual(self.google.events[self.event_id]["summary"], "協賛ロゴ 入稿（確定）")

    def test_a_recoloured_entry_is_still_ours(self):
        self.google.owner_edit(self.event_id, colorId="5")
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertEqual(self.row("c1")["etag"], self.google.events[self.event_id]["etag"])
        self.ws.update_commitment("c1", due_at="2026-10-06T17:00:00+09:00")
        self.run_once()
        self.assertEqual(self.google.events[self.event_id]["start"], {"date": "2026-10-06"})


class FollowTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.run_once()
        self.event_id = self.row("c1")["event_id"]

    def test_a_moved_deadline_moves_the_entry_with_if_match(self):
        self.ws.update_commitment("c1", due_at="2026-10-07T12:00:00+09:00")
        report = self.run_once()
        self.assertEqual(report.counts["updated"], 1)
        event = self.google.events[self.event_id]
        self.assertEqual(event["start"], {"date": "2026-10-07"})
        self.assertEqual(self.row("c1")["etag"], event["etag"])
        self.assertEqual(self.google.verbs().count("insert"), 1)

    def test_updates_can_be_switched_off(self):
        self.ws.update_commitment("c1", due_at="2026-10-07T12:00:00+09:00")
        self.run_once(make_cfg(behaviour={"update_on_change": False}))
        self.assertEqual(self.google.events[self.event_id]["start"], {"date": "2026-10-05"})

    def test_an_owner_edit_racing_our_update_wins(self):
        self.ws.update_commitment("c1", due_at="2026-10-07T12:00:00+09:00")
        self.google.fail["list"] = []

        original_patch = self.google.patch_event

        def racing_patch(calendar_id, event_id, body, *, etag):
            self.google.owner_edit(event_id, summary="手で直した")
            return original_patch(calendar_id, event_id, body, etag=etag)

        self.google.patch_event = racing_patch
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.USER_EDITED)
        self.assertEqual(self.google.events[self.event_id]["summary"], "手で直した")

    def test_an_update_whose_answer_was_lost_is_adopted_not_misread(self):
        self.ws.update_commitment("c1", due_at="2026-10-07T12:00:00+09:00")
        self.google.fail["patch"] = [("apply_then_raise", ApiError(None, "TimeoutError"))]
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        row = self.row("c1")
        self.assertEqual(row["applied_revision"], row["content_revision"])
        self.assertEqual(row["etag"], self.google.events[self.event_id]["etag"])

    def test_a_resolved_commitment_withdraws_its_future_entry(self):
        self.ws.update_commitment("c1", work_state="DONE")
        report = self.run_once()
        self.assertEqual(report.counts["withdrawn"], 1)
        self.assertEqual(self.state_of("c1"), store.WITHDRAWN)
        self.assertEqual(self.google.events[self.event_id]["status"], "cancelled")
        self.run_once()
        self.assertEqual(self.google.verbs().count("insert"), 1)

    def test_a_real_event_appearing_withdraws_the_tentative_one(self):
        self.ws.add_index_event("real", "協賛ロゴの入稿", "2026-10-05", all_day=True)
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.WITHDRAWN)
        self.assertEqual(self.row("c1")["last_error"], sel.R_MATCHES_EXISTING)

    def test_a_deleted_commitment_withdraws_its_entry(self):
        self.ws.delete_commitment("c1")
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.WITHDRAWN)

    def test_the_past_is_never_withdrawn(self):
        self.ws.update_commitment("c1", work_state="DONE")
        later = NOW + timedelta(days=10)
        self.run_once(now=later)
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertNotIn("delete", self.google.verbs())

    def test_withdrawal_can_be_switched_off(self):
        self.ws.update_commitment("c1", work_state="DONE")
        self.run_once(make_cfg(behaviour={"withdraw_on_resolution": False}))
        self.assertEqual(self.state_of("c1"), store.ACTIVE)

    def test_merely_ineligible_is_not_a_reason_to_delete(self):
        self.ws.update_commitment("c1", responsibility="COUNTERPARTY")
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertNotIn("delete", self.google.verbs())

    def test_a_lost_delete_answer_still_ends_as_withdrawn(self):
        self.ws.update_commitment("c1", work_state="DONE")
        self.google.fail["delete"] = [("apply_then_raise", ApiError(None, "TimeoutError"))]
        report = self.run_once()
        self.assertEqual(report.counts["withdraw_failed"], 1)
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.WITHDRAWN)

    def test_an_owner_edit_after_resolution_is_not_deleted(self):
        self.ws.update_commitment("c1", work_state="DONE")
        original = self.google.delete_event

        def racing_delete(calendar_id, event_id, *, etag):
            self.google.owner_edit(event_id, summary="自分で残す")
            return original(calendar_id, event_id, etag=etag)

        self.google.delete_event = racing_delete
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.USER_EDITED)
        self.assertNotEqual(self.google.events[self.event_id].get("status"), "cancelled")

    def test_an_unreadable_calendar_skips_maintenance_but_not_creation(self):
        self.ws.update_commitment("c1", due_at="2026-10-07T12:00:00+09:00")
        self.ws.add_commitment("c2", "見積書の送付", **DUE)
        self.google.fail["list"] = [("raise", ApiError(503, "backendError"))]
        report = self.run_once()
        self.assertEqual(report.counts["list_failed"], 1)
        self.assertNotIn("patch", self.google.verbs())
        self.assertEqual(self.state_of("c2"), store.ACTIVE)


class SafetyTests(EngineCase):
    def test_a_state_database_cannot_be_pointed_at_another_calendar(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.run_once()
        other = make_cfg(calendar={"calendar_id": "another@group.calendar.google.com"})
        self.google = FakeCalendar("another@group.calendar.google.com")
        with self.assertRaises(ct_engine.CalendarChanged):
            self.run_once(other)

    def test_only_the_tentative_calendar_is_ever_called(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.run_once()
        self.ws.update_commitment("c1", work_state="DONE")
        self.run_once()
        # FakeCalendar raises 403 for any other calendar id; reaching here
        # with a withdrawn row means every call targeted the configured one.
        self.assertEqual(self.state_of("c1"), store.WITHDRAWN)

    def test_repropose_uses_a_new_event_id(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.run_once()
        first = self.row("c1")["event_id"]
        self.google.owner_delete(first)
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.REJECTED)
        new_id = store.repropose(self.state, "c1", event_id_for=sel.event_id_for)
        self.state.commit()
        self.assertNotEqual(new_id, first)
        self.run_once()
        self.assertEqual(self.state_of("c1"), store.ACTIVE)
        self.assertEqual(len(self.google.live()), 1)
        self.assertIsNone(store.repropose(self.state, "c1", event_id_for=sel.event_id_for))

    def test_the_events_database_is_not_modified(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        before = self.ws.events.execute("SELECT * FROM commitments").fetchall()
        self.run_once()
        self.ws.update_commitment("c1", work_state="DONE")
        mid = [tuple(r) for r in self.ws.events.execute("SELECT * FROM commitments")]
        self.run_once()
        after = [tuple(r) for r in self.ws.events.execute("SELECT * FROM commitments")]
        self.assertEqual(mid, after)
        self.assertEqual(len(before), len(after))


class PlanTests(EngineCase):
    def test_plan_is_local_and_lists_what_would_happen(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.ws.add_commitment("c2", "見積書の送付", **DUE)
        self.run_once(make_cfg(limits={"max_creates_per_run": 1}))
        self.ws.update_commitment("c1", work_state="DONE")
        self.google.calls.clear()
        selection = sel.select(self.ws.events, cfg=self.cfg, now=NOW, index_con=self.ws.index)
        report = ct_engine.plan(self.state, selection, cfg=self.cfg, now=NOW)
        self.assertEqual(self.google.calls, [])
        self.assertEqual(report.plan["create"], ["10/5(月) 〆 見積書の送付"])
        self.assertEqual(len(report.plan["withdraw"]), 1)
        self.assertIn("resolved", report.plan["withdraw"][0])


if __name__ == "__main__":
    unittest.main()
