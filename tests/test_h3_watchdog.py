#!/usr/bin/env python3
"""H3: the supervision loop must stay active for a SILENT process.

Before the fix, proc.stdout.readline() blocked the watchdog loop itself —
a silent agent disabled timeout/hang detection. The loop now drains output
via a reader thread and wakes every poll interval."""

import subprocess
import sys
import time
import unittest

import helpers  # noqa: F401  (sys.path setup)

import bridge  # noqa: E402


def _spawn(code):
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


class TestSilentAgentWatchdog(unittest.TestCase):
    def test_silent_process_hang_detected_and_terminated(self):
        """Synthetic silent agent: no output, sleeps far beyond the hang
        threshold. Expect: watchdog fires, stop called, loop returns fast,
        process terminated — while the job state stays owned by the caller."""
        proc = _spawn("import time; time.sleep(60)")
        stop_calls = []

        def stop_fn():
            stop_calls.append(1)
            proc.kill()  # stand-in for `docker stop`

        started = time.time()
        exit_status, timed_out, tail = bridge.supervise_process(
            proc, timeout_s=60, hang_watchdog_s=1.0, grace_s=0,
            stop_fn=stop_fn, poll_s=0.05)
        elapsed = time.time() - started

        self.assertTrue(timed_out, "hang must be detected")
        self.assertEqual(stop_calls, [1], "stop_fn invoked exactly once")
        self.assertLess(elapsed, 15,
                        f"watchdog must not be blocked by silence ({elapsed:.1f}s)")
        proc.wait(timeout=10)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(tail, "")

    def test_silent_process_absolute_timeout_fires(self):
        proc = _spawn("import time; time.sleep(60)")
        stop_calls = []

        def stop_fn():
            stop_calls.append(1)
            proc.kill()

        started = time.time()
        exit_status, timed_out, _ = bridge.supervise_process(
            proc, timeout_s=1.0, hang_watchdog_s=999, grace_s=0,
            stop_fn=stop_fn, poll_s=0.05)
        elapsed = time.time() - started

        self.assertTrue(timed_out)
        self.assertEqual(stop_calls, [1])
        self.assertLess(elapsed, 15)
        proc.wait(timeout=10)

    def test_loud_process_exits_normally_without_stop(self):
        proc = _spawn("print('hello'); print('working'); print('done')")
        stop_calls = []

        def stop_fn():
            stop_calls.append(1)
            proc.kill()

        exit_status, timed_out, tail = bridge.supervise_process(
            proc, timeout_s=60, hang_watchdog_s=1.0, grace_s=0,
            stop_fn=stop_fn, poll_s=0.05)

        self.assertEqual(stop_calls, [])
        self.assertFalse(timed_out)
        self.assertEqual(exit_status, 0)
        for line in ("hello", "working", "done"):
            self.assertIn(line, tail)

    def test_cancel_requested_during_run_stops_process(self):
        proc = _spawn("import time; time.sleep(60)")
        stop_calls = []
        cancelled = {"flag": False}

        def stop_fn():
            stop_calls.append(1)
            proc.kill()

        def cancelled_fn():
            return cancelled["flag"]

        # flip the cancel flag shortly after the run starts
        import threading

        def late_cancel():
            time.sleep(0.5)
            cancelled["flag"] = True

        threading.Thread(target=late_cancel, daemon=True).start()
        exit_status, timed_out, _ = bridge.supervise_process(
            proc, timeout_s=60, hang_watchdog_s=999, grace_s=0,
            stop_fn=stop_fn, cancelled_fn=cancelled_fn, poll_s=0.05)

        self.assertTrue(timed_out, "cancel must terminate the supervision")
        self.assertEqual(stop_calls, [1])
        proc.wait(timeout=10)

    def test_worktree_activity_counts_as_progress(self):
        # a silent process whose mtime_fn keeps advancing must NOT hang-fire
        proc = _spawn("import time; time.sleep(2)")
        stop_calls = []

        def stop_fn():
            stop_calls.append(1)
            proc.kill()

        exit_status, timed_out, _ = bridge.supervise_process(
            proc, timeout_s=30, hang_watchdog_s=0.2, grace_s=0,
            mtime_fn=lambda: time.time(),  # simulated active worktree
            stop_fn=stop_fn, poll_s=0.05)

        self.assertEqual(stop_calls, [])
        self.assertFalse(timed_out)
        proc.wait(timeout=10)
        self.assertEqual(exit_status, 0)


if __name__ == "__main__":
    unittest.main()
