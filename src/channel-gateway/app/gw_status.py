#!/usr/bin/env python3
"""Unified Channel Gateway — what it is doing right now, for a human.

Read-only. Prints no message body and no credential: states, counts,
delivery modes and audit categories. If you want to know what someone
actually said, that is deliberately not here.

Written in Python rather than as sqlite3 CLI calls because the `sqlite3`
command is NOT installed on the target host — the shell version produced
empty sections and said nothing about why, which is exactly the kind of
silent failure this project keeps having to fix. The venv's Python is
guaranteed present, since it is what runs the service.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = "/srv/hermes/channel-gateway/state/gateway.db"


def connect(path: str):
    """Read-only connection, or an explanation of why not."""
    if not os.path.exists(path):
        return None, f"データベースがまだありません: {path}"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM inbound_messages LIMIT 1")
        return con, None
    except sqlite3.OperationalError as exc:
        # state/ is 0750 owned by the service user. Say who can read it
        # rather than printing nothing.
        return None, (
            f"読めません ({exc}). root か channelgateway で実行してください: "
            f"sudo -u channelgateway {sys.argv[0]}"
        )


def table(con, title: str, sql: str, params=()):
    print(f"== {title} ==")
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"   クエリ失敗: {exc}")
        print()
        return
    if not rows:
        print("   (なし)")
        print()
        return
    cols = rows[0].keys()
    widths = [max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols]
    print("   " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    print("   " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("   " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="channel gateway status")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args(argv)

    con, err = connect(args.db)
    if con is None:
        print(f"== database ==\n   {err}\n")
        return 1

    table(con, "受信キュー",
          "SELECT state, COUNT(*) AS n FROM inbound_messages"
          " GROUP BY state ORDER BY n DESC")

    table(con, "送信",
          "SELECT platform, delivery_mode, state, COUNT(*) AS n"
          " FROM outbound_messages GROUP BY platform, delivery_mode, state"
          " ORDER BY n DESC")

    # Push is metered at 200 messages a month on the free plan in Japan, and
    # LINE counts by recipient, so one request with several chunks is one
    # message. This counts requests that actually went out as a push.
    table(con, "LINE push 使用量 (無料枠は月 200 通・reply は無料)",
          "SELECT strftime('%Y-%m', sent_at) AS month, COUNT(DISTINCT"
          " gateway_request_id) AS pushes FROM outbound_messages"
          " WHERE platform='line' AND delivery_mode='push' AND state='SENT'"
          " GROUP BY month ORDER BY month DESC LIMIT 3")

    table(con, "配信の内訳 (直近 10 件)",
          "SELECT i.gateway_request_id AS request, i.platform, i.state,"
          " (SELECT group_concat(DISTINCT o.delivery_mode) FROM outbound_messages o"
          "   WHERE o.gateway_request_id = i.gateway_request_id) AS mode,"
          " (SELECT COUNT(*) FROM outbound_messages o"
          "   WHERE o.gateway_request_id = i.gateway_request_id AND o.state='SENT')"
          "   AS sent_chunks,"
          " i.updated_at"
          " FROM inbound_messages i ORDER BY i.rowid DESC LIMIT 10")

    table(con, "送信試行 (直近 10 件)",
          "SELECT attempted_at, mode, outcome, COALESCE(status_code,'') AS code,"
          " COALESCE(error_category,'') AS category"
          " FROM delivery_attempts ORDER BY id DESC LIMIT 10")

    # G.2. Proactive notifications are the messages nobody asked for, so they
    # are shown separately from the reply traffic: a LINE row here is a push
    # the Attention Engine chose to spend, and that is the number worth
    # noticing on a metered channel.
    table(con, "能動通知 (G.2 Attention Engine)",
          "SELECT channel, state, delivery_mode,"
          " SUM(quota_consumed) AS quota, COUNT(*) AS n"
          " FROM proactive_deliveries GROUP BY channel, state, delivery_mode"
          " ORDER BY n DESC")

    table(con, "能動通知 (直近 8 件・本文なし)",
          "SELECT substr(created_at,1,19) AS at, channel, severity, state,"
          " COALESCE(reason,'') AS reason, length(body) AS chars"
          " FROM proactive_deliveries ORDER BY rowid DESC LIMIT 8")

    table(con, "LINE quota キャッシュ (gateway が API から読んだ値)",
          "SELECT ok, COALESCE(quota_type,'') AS type,"
          " COALESCE(limit_value,'') AS target,"
          " COALESCE(used,'') AS used,"
          " COALESCE(error_category,'') AS error,"
          " substr(fetched_at,1,19) AS fetched FROM line_quota_cache")

    table(con, "監査 (本文は含まない・直近 12 件)",
          "SELECT at, event, COALESCE(platform,'') AS platform,"
          " COALESCE(detail,'') AS detail"
          " FROM audit_events ORDER BY id DESC LIMIT 12")

    table(con, "直近のエラー",
          "SELECT updated_at, platform, state, last_error FROM inbound_messages"
          " WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 5")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
