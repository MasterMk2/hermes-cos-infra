"""Administrative and operational CLI for Unified Commitments Layer."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from .models import (
    Commitment,
    CommitmentEvidence,
    Responsibility,
    WorkState,
    Authority,
    compute_temporal_state,
)
from .store import (
    init_db,
    save_commitment,
    get_commitment,
    list_commitments,
    get_status,
)
from .reconcile import (
    run_backfill,
    merge_commitments,
)
from .agenda import (
    generate_agenda,
)

DEFAULT_EVENTS_DB = "/srv/hermes/hermes-data/events/events.db"
DEFAULT_CALENDAR_DB = "/srv/hermes/calendar/index/index.db"


def main():
    parser = argparse.ArgumentParser(description="Unified Commitments Management CLI")
    parser.add_argument("--db", default=os.environ.get("EVENTS_DB", DEFAULT_EVENTS_DB), help="Path to events.db")
    parser.add_argument("--calendar-db", default=os.environ.get("CALENDAR_DB", DEFAULT_CALENDAR_DB), help="Path to calendar index.db")

    subparsers = parser.add_subparsers(dest="subcommand", help="Action to perform")

    # init
    subparsers.add_parser("init", help="Initialize commitment tables")

    # backfill
    subparsers.add_parser("backfill", help="Run idempotent baseline backfill")

    # status
    subparsers.add_parser("status", help="Show commitment metrics and counts")

    # agenda
    sub_agenda = subparsers.add_parser("agenda", help="Generate agenda projection")
    sub_agenda.add_argument("--days", type=int, default=7, help="Window days for upcoming items")
    sub_agenda.add_argument("--json", action="store_true", help="Output raw JSON")

    # get
    sub_get = subparsers.add_parser("get", help="Get commitment by ID")
    sub_get.add_argument("commitment_id", help="Commitment ID")

    # list
    sub_list = subparsers.add_parser("list", help="List commitments")
    sub_list.add_argument("--responsibility", help="Filter by responsibility")
    sub_list.add_argument("--state", help="Filter by work state")
    sub_list.add_argument("--temporal", help="Filter by temporal state")
    sub_list.add_argument("--limit", type=int, default=20, help="Max results")

    # merge
    sub_merge = subparsers.add_parser("merge", help="Merge two commitments")
    sub_merge.add_argument("target_id", help="Target commitment ID (survives)")
    sub_merge.add_argument("source_id", help="Source commitment ID (superseded)")
    sub_merge.add_argument("--reason", default="admin_manual_merge", help="Audit reason")

    # resolve
    sub_res = subparsers.add_parser("resolve", help="Mark commitment resolved")
    sub_res.add_argument("commitment_id", help="Commitment ID")
    sub_res.add_argument("--reason", default="user_confirmed_done", help="Audit reason")

    # reopen
    sub_reop = subparsers.add_parser("reopen", help="Reopen commitment")
    sub_reop.add_argument("commitment_id", help="Commitment ID")
    sub_reop.add_argument("--reason", default="reopened_by_user", help="Audit reason")

    # update-due
    sub_due = subparsers.add_parser("update-due", help="Update deadline")
    sub_due.add_argument("commitment_id", help="Commitment ID")
    sub_due.add_argument("due_at", help="New due_at ISO timestamp")
    sub_due.add_argument("--reason", default="deadline_adjusted", help="Audit reason")

    args = parser.parse_args()
    if not args.subcommand:
        parser.print_help()
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    init_db(conn)

    cal_conn = None
    if os.path.isfile(args.calendar_db):
        cal_conn = sqlite3.connect(args.calendar_db)

    try:
        if args.subcommand == "init":
            print(f"Commitment schema initialized in {args.db}")

        elif args.subcommand == "backfill":
            res = run_backfill(conn, cal_conn, actor="admin_cli")
            print(json.dumps(res, indent=2, ensure_ascii=False))

        elif args.subcommand == "status":
            st = get_status(conn)
            print(json.dumps(st, indent=2, ensure_ascii=False))

        elif args.subcommand == "agenda":
            ag = generate_agenda(conn, window_days=args.days)
            if args.json:
                print(json.dumps(ag, indent=2, ensure_ascii=False))
            else:
                print(f"=== AGENDA (as of {ag['as_of']}) ===")
                print(f"Today: {ag['summary']['today_count']} | Soon: {ag['summary']['soon_count']} | Overdue: {ag['summary']['overdue_count']} | Waiting: {ag['summary']['waiting_count']}")
                if ag["today"]:
                    print("\n[TODAY]")
                    for item in ag["today"]:
                        print(f"  * [{item['responsibility']}] {item['title']} (due: {item['due_at'] or 'none'})")
                if ag["soon"]:
                    print("\n[SOON (next 7 days)]")
                    for item in ag["soon"][:10]:
                        print(f"  * [{item['responsibility']}] {item['title']} (due: {item['due_at'] or item['scheduled_at']})")
                if ag["overdue"]:
                    print("\n[OVERDUE]")
                    for item in ag["overdue"][:10]:
                        print(f"  * [{item['responsibility']}] {item['title']} (due: {item['due_at']})")
                if ag["waiting_for"]:
                    print("\n[WAITING FOR]")
                    for item in ag["waiting_for"][:10]:
                        print(f"  * [waiting on {item['counterparty'] or 'counterparty'}] {item['title']}")

        elif args.subcommand == "get":
            cmt = get_commitment(conn, args.commitment_id)
            if cmt is None:
                print(f"Not found: {args.commitment_id}", file=sys.stderr)
                sys.exit(1)
            print(json.dumps(cmt.to_dict(), indent=2, ensure_ascii=False))

        elif args.subcommand == "list":
            cmts = list_commitments(
                conn,
                responsibility=args.responsibility,
                work_state=args.state,
                temporal_state=args.temporal,
                limit=args.limit,
            )
            print(json.dumps([c.to_dict() for c in cmts], indent=2, ensure_ascii=False))

        elif args.subcommand == "merge":
            res = merge_commitments(conn, args.target_id, args.source_id, actor="admin_cli", reason=args.reason)
            if res is None:
                print("Merge failed (one of the commitments was not found or identical)", file=sys.stderr)
                sys.exit(1)
            print(f"Successfully merged {args.source_id} into {args.target_id}")

        elif args.subcommand == "resolve":
            cmt = get_commitment(conn, args.commitment_id)
            if not cmt:
                print(f"Not found: {args.commitment_id}", file=sys.stderr)
                sys.exit(1)
            cmt.work_state = WorkState.DONE.value
            cmt.resolved_at = datetime.now(timezone.utc).isoformat()
            save_commitment(conn, cmt, actor="admin_cli", reason=args.reason)
            print(f"Resolved {args.commitment_id}")

        elif args.subcommand == "reopen":
            cmt = get_commitment(conn, args.commitment_id)
            if not cmt:
                print(f"Not found: {args.commitment_id}", file=sys.stderr)
                sys.exit(1)
            cmt.work_state = WorkState.ACTIONABLE.value
            cmt.resolved_at = None
            save_commitment(conn, cmt, actor="admin_cli", reason=args.reason)
            print(f"Reopened {args.commitment_id}")

        elif args.subcommand == "update-due":
            cmt = get_commitment(conn, args.commitment_id)
            if not cmt:
                print(f"Not found: {args.commitment_id}", file=sys.stderr)
                sys.exit(1)
            cmt.due_at = args.due_at
            save_commitment(conn, cmt, actor="admin_cli", reason=args.reason)
            print(f"Updated due_at for {args.commitment_id} to {args.due_at}")

    finally:
        conn.close()
        if cal_conn:
            cal_conn.close()


if __name__ == "__main__":
    main()
