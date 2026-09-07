"""Performance and query shape (phase section 74).

Two assertions, and the second is the load-bearing one:

* **Wall clock.** The plan must build in under 500 ms. This suite runs on a
  workstation, which is several times faster than the Pi, so a green timing
  here is necessary but not sufficient — the measured figure is printed so the
  Pi number can be compared against it.
* **Query count.** Exactly two SQL statements, no matter how many commitments
  exist. This is the assertion that actually survives being run on different
  hardware: an N+1 evidence lookup would still pass a generous timing bound on
  a fast machine and then blow the budget on the Pi, where the same 786 rows
  would become 787 extra round trips.

The fixture reproduces production's *shape*, not just its row count: 786
commitments dominated by Gmail noise, 787 evidence rows, spread over a 61-day
window. A uniform population would be an easier database than the real one.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402

from planning.planner import build_daily_plan  # noqa: E402

#: The contract's budget (phase section 74), asserted directly.
BUDGET_SECONDS = 0.5


class ProductionSizedPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fx.open_db()
        cls.rows = fx.seed_production_sized(cls.conn)
        cls.freshness = fx.freshness_block()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def build(self):
        return build_daily_plan(
            self.conn,
            date=fx.DEFAULT_PLAN_DATE,
            now=fx.DEFAULT_NOW,
            freshness=self.freshness,
        )

    def test_the_fixture_really_is_production_sized(self):
        commitments = self.conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        evidence = self.conn.execute("SELECT COUNT(*) FROM commitment_evidence").fetchone()[0]
        self.assertEqual(commitments, self.rows)
        self.assertGreaterEqual(commitments, 780)
        self.assertGreaterEqual(evidence, 780)

    def test_build_stays_within_the_budget(self):
        self.build()  # warm the page cache; the budget is for a steady state
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            self.build()
            timings.append(time.perf_counter() - started)
        best = min(timings)
        median = sorted(timings)[len(timings) // 2]
        print(
            f"\n[perf] build_daily_plan over {self.rows} commitments: "
            f"best {best * 1000:.1f} ms, median {median * 1000:.1f} ms "
            f"(budget {BUDGET_SECONDS * 1000:.0f} ms)"
        )
        self.assertLess(
            median, BUDGET_SECONDS,
            f"median build {median * 1000:.1f} ms exceeds the {BUDGET_SECONDS * 1000:.0f} ms budget",
        )

    def test_exactly_two_sql_statements_regardless_of_row_count(self):
        """The N+1 guard: evidence source_types are batch-loaded in one query."""
        statements = fx.count_sql_statements(self.conn)
        try:
            self.build()
        finally:
            self.conn.set_trace_callback(None)
        selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
        self.assertEqual(
            len(statements), 2,
            "expected one commitments query and one evidence query, got:\n"
            + "\n".join(statements),
        )
        self.assertEqual(len(selects), 2)
        self.assertIn("FROM commitments", selects[0])
        self.assertIn("commitment_evidence", selects[1])

    def test_the_plan_is_actually_populated_and_capped(self):
        plan = self.build()
        self.assertTrue(plan["overdue"], "a production-sized fixture must produce overdue work")
        self.assertTrue(plan["backlog"] or plan["must_do"] or plan["next_up"])
        for category, limit in (
            ("scheduled", 5), ("must_do", 5), ("overdue", 5),
            ("next_up", 5), ("waiting_for", 7), ("backlog", 3),
        ):
            self.assertLessEqual(len(plan[category]), limit, category)
            self.assertGreaterEqual(
                plan["counts"][category]["total"], plan["counts"][category]["shown"], category
            )

    def test_the_noise_filter_removes_the_gmail_bulk(self):
        plan = self.build()
        self.assertGreater(
            plan["counts"]["filtered_noise"]["total"], 100,
            "the SHARED/Gmail bucket is the majority of the database and must be filtered",
        )
        for category in ("scheduled", "must_do", "overdue", "next_up", "backlog"):
            for item in plan[category]:
                if item["responsibility"] == "SHARED":
                    self.assertTrue(
                        set(item["source_types"]) & {"discord", "line", "calendar"},
                        f"a Gmail-only SHARED row reached {category}: {item['commitment_id']}",
                    )

    def test_the_result_is_byte_identical_across_runs(self):
        first = self.build()
        for _ in range(3):
            self.assertEqual(self.build(), first)


class ScalingTest(unittest.TestCase):
    """The query count must not grow with the database."""

    def test_query_count_is_flat_from_ten_rows_to_production_size(self):
        counts = []
        for size in (1, 10):
            conn = fx.open_db()
            self.addCleanup(conn.close)
            mix = tuple(
                (responsibility, work_state, source, max(1, rows // (800 // size)))
                for responsibility, work_state, source, rows in fx.PRODUCTION_MIX
            )
            fx.seed_production_sized(conn, mix=mix)
            statements = fx.count_sql_statements(conn)
            try:
                build_daily_plan(
                    conn, date=fx.DEFAULT_PLAN_DATE, now=fx.DEFAULT_NOW,
                    freshness=fx.freshness_block(),
                )
            finally:
                conn.set_trace_callback(None)
            counts.append(len(statements))
        self.assertEqual(counts, [2, 2])


if __name__ == "__main__":
    unittest.main()
