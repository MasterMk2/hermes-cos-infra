#!/usr/bin/env python3
"""Attention Engine — what it has been doing, for a human.

Read-only. Prints no credential, and no notification body: what the owner was
told is in the notification row, and a status command that dumps it into a
terminal is a status command that leaks personal content into a screenshot.
Counts, states, reasons and timings only (section 102).

Written in Python for the same reason `gw_status.py` is: the `sqlite3` command
line tool is not installed on this host, and the shell version of that script
silently printed empty tables.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

DEFAULT_DB = "/srv/hermes/attention/state/attention.db"


def connect(path: str):
    if not os.path.exists(path):
        return None, f"データベースがまだありません: {path}"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM scan_runs LIMIT 1")
        return con, None
    except sqlite3.Error as exc:
        return None, f"読めません ({exc})"


def table(con, title, sql, params=()):
    print(f"== {title} ==")
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"   クエリ失敗: {exc}\n")
        return
    if not rows:
        print("   (なし)\n")
        return
    cols = rows[0].keys()
    widths = [max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols]
    print("   " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    print("   " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("   " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print()


def kv(title, mapping):
    print(f"== {title} ==")
    if not mapping:
        print("   (なし)\n")
        return
    width = max(len(str(k)) for k in mapping)
    for k, v in mapping.items():
        print(f"   {str(k).ljust(width)}  {v}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="attention engine status")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args(argv)

    con, err = connect(args.db)
    if con is None:
        print(f"== database ==\n   {err}\n")
        return 1

    since = f"-{max(1, args.days)} days"

    meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}
    kv("エンジン", {
        "schema_version": meta.get("schema_version"),
        "initialized_at": meta.get("initialized_at"),
        "baseline_done": meta.get("baseline_done", "0"),
        "last_scan_finished_at": meta.get("last_scan_finished_at"),
    })

    # Section 90: the engine's own liveness. Reported here rather than
    # alerted on by the engine itself (section 91).
    row = con.execute(
        "SELECT MAX(started_at) AS last_scan,"
        " MAX(CASE WHEN ok = 1 THEN started_at END) AS last_ok,"
        " COUNT(*) AS scans, COALESCE(SUM(llm_calls), 0) AS llm"
        " FROM scan_runs"
    ).fetchone()
    pending = con.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM notifications"
        " WHERE state IN ('PENDING','DELIVERING')"
    ).fetchone()
    kv("スキャン", {
        "last_scan_at": row["last_scan"],
        "last_successful_scan_at": row["last_ok"],
        "scans_total": row["scans"],
        "signal_backlog": con.execute(
            "SELECT COUNT(*) FROM signals WHERE state='NEW'").fetchone()[0],
        "pending_notifications": pending["n"],
        "oldest_pending_at": pending["oldest"] or "-",
        "llm_calls_total": row["llm"],
    })

    table(con, f"直近 {args.days} 日の判断 (理由別)",
          "SELECT decision, reason, COUNT(*) AS n FROM attention_decisions"
          f" WHERE decided_at >= datetime('now', '{since}')"
          " GROUP BY decision, reason ORDER BY n DESC LIMIT 20")

    table(con, "シグナル (source 別)",
          "SELECT source_type, signal_type, COUNT(*) AS n FROM signals"
          " GROUP BY source_type, signal_type ORDER BY n DESC LIMIT 15")

    table(con, "通知 (状態別)",
          "SELECT state, severity, kind, COUNT(*) AS n FROM notifications"
          " GROUP BY state, severity, kind ORDER BY n DESC")

    table(con, "配信試行 (チャネル別・本文なし)",
          "SELECT channel, outcome, COUNT(*) AS n,"
          " SUM(quota_consumed) AS quota FROM delivery_attempts"
          f" WHERE attempted_at >= datetime('now', '{since}')"
          " GROUP BY channel, outcome ORDER BY n DESC")

    table(con, "LINE 予算台帳 (当月)",
          "SELECT day, state, COUNT(*) AS n FROM channel_budget"
          " WHERE channel='line' AND month = strftime('%Y-%m','now','+9 hours')"
          " GROUP BY day, state ORDER BY day DESC LIMIT 10")

    table(con, "障害エピソード",
          "SELECT service, reason, state, substr(first_degraded_at,1,19) AS since,"
          " observations AS obs,"
          " CASE WHEN notified_at IS NULL THEN 'no' ELSE 'yes' END AS notified"
          " FROM notification_episodes ORDER BY first_degraded_at DESC LIMIT 10")

    table(con, "ダイジェスト",
          "SELECT substr(created_at,1,19) AS at, kind, item_count"
          " FROM digests ORDER BY rowid DESC LIMIT 5")

    table(con, "直近の通知 (本文は表示しない)",
          "SELECT substr(created_at,1,19) AS at, severity, kind, state,"
          " reason, delivered_channels AS ch FROM notifications"
          " ORDER BY rowid DESC LIMIT 10")

    table(con, "スキャン失敗",
          "SELECT substr(started_at,1,19) AS at, error FROM scan_runs"
          " WHERE ok = 0 ORDER BY id DESC LIMIT 5")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
