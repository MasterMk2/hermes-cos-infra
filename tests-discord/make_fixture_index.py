#!/usr/bin/env python3
"""Build a Discord search index fixture through the real pipeline.

The fixture is produced by the production code path — normalize -> source DB
-> index_queue -> indexer — rather than hand-written SQL, so what the MCP
tests query is what the collector would actually have produced.

Usage:
    python3 make_fixture_index.py <output-index.db>
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import dk_fixtures as fx
import dk_store
import yaml
from dk_normalize import normalize_message
from dk_schema import open_source_db

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def build(out_path: Path) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="dk-fixture-"))
    source = tmp / "messages.db"
    index = tmp / "index.db"
    cfg_path = tmp / "config.yaml"

    data = fx.config_dict(owner=fx.OWNER_ID)
    data["paths"] = {
        "source_db": str(source),
        "index_db": str(index),
        "state_dir": str(tmp),
        "log_dir": str(tmp),
    }
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    con = open_source_db(source)
    dk_store.upsert_guild(con, fx.GUILD_A, "Guild A")
    dk_store.upsert_guild(con, fx.GUILD_B, "Guild B")
    for cid, ctx in fx.CHANNEL_CTX.items():
        gid = fx.GUILD_B if cid == fx.CHANNEL_B1 else fx.GUILD_A
        dk_store.upsert_channel(
            con,
            cid,
            gid,
            channel_name=ctx["channel_name"],
            channel_type=ctx["channel_type"],
            parent_channel_id=ctx["parent_channel_id"],
            is_thread=ctx["is_thread"],
            readable=True,
        )

    names = {c: v["channel_name"] for c, v in fx.CHANNEL_CTX.items()}

    def put(payload, guild_id=fx.GUILD_A, guild_name="Guild A"):
        cid = payload["channel_id"]
        rec = normalize_message(
            payload,
            guild_id=guild_id,
            guild_name=guild_name,
            channel=fx.CHANNEL_CTX[cid],
            channel_names=names,
        )
        dk_store.apply_message(con, rec)
        return rec

    catalogue = {}

    catalogue["plain"] = put(fx.message(content="来週の打ち合わせは水曜日にしましょう"))
    catalogue["short"] = put(fx.message(content="OK です", created=fx.at(1, 13)))
    catalogue["other_author"] = put(
        fx.message(
            author=fx.user(fx.OTHER_ID, "alice", "Alice"),
            content="資料の準備は私が引き受けます",
            created=fx.at(1, 14),
        )
    )
    catalogue["bot"] = put(fx.BOT_MESSAGE)
    catalogue["webhook"] = put(fx.WEBHOOK_MESSAGE)
    catalogue["thread"] = put(fx.THREAD_MESSAGE)
    catalogue["reply_target"] = put(fx.REPLY_TARGET)
    catalogue["reply"] = put(fx.REPLY)
    catalogue["attachment"] = put(fx.WITH_ATTACHMENT)
    catalogue["mention"] = put(fx.MENTION_MESSAGE)
    catalogue["guild_b"] = put(
        fx.message(channel_id=fx.CHANNEL_B1, content="Bサーバーの連絡事項です", created=fx.at(4)),
        guild_id=fx.GUILD_B,
        guild_name="Guild B",
    )
    for i, payload in enumerate(fx.injection_messages()):
        catalogue[f"injection_{i}"] = put(payload)

    # An edited message: the old body must not survive in the index.
    edited_original = fx.message(content="旧本文OLDBODY", created=fx.at(5))
    catalogue["edited"] = put(edited_original)
    catalogue["edited"] = put(
        fx.message(
            message_id=edited_original["id"],
            content="新本文NEWBODY",
            created=fx.at(5),
            edited=fx.at(6),
        )
    )

    # A deleted message: tombstone only.
    doomed = fx.message(content="削除される本文GONEBODY", created=fx.at(7))
    catalogue["deleted"] = put(doomed)
    dk_store.apply_delete(con, fx.GUILD_A, fx.CHANNEL_A1, doomed["id"])

    dk_store.set_ingest_states(
        con,
        {
            "collector_health": "CONNECTED",
            "gateway_connected": "1",
            # Written now so the published health reads as live rather than
            # stale; the stale branch is exercised directly in the MCP tests.
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_ready_at": "2026-09-02T12:00:00+00:00",
            "last_event_at": "2026-09-02T12:30:00+00:00",
            "last_successful_ingest_at": "2026-09-02T12:30:00+00:00",
            "last_error_summary": "",
            "session_resumed_count": "2",
            "full_reconnect_count": "1",
            "messages_created": str(len(catalogue)),
            "messages_edited": "1",
            "messages_deleted": "1",
            "rate_limit_hits": "0",
        },
    )
    dk_store.save_backfill_state(
        con, fx.CHANNEL_A1, fx.GUILD_A, last_cursor="123", complete=True, added=12
    )
    con.commit()
    con.close()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_DIR)
    proc = subprocess.run(
        [
            sys.executable,
            str(APP_DIR / "indexer.py"),
            "--config",
            str(cfg_path),
            "--source-db",
            str(source),
            "--index-db",
            str(index),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    if proc.returncode != 0:
        raise SystemExit(f"indexer failed: {proc.stdout}\n{proc.stderr}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(index, out_path)
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "index": str(out_path),
        "indexer": proc.stdout.strip(),
        "catalogue": {k: v["source_id"] for k, v in catalogue.items()},
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_fixture_index.py <output-index.db>")
    info = build(Path(sys.argv[1]))
    print(info["indexer"])
    print(f"FIXTURE_INDEX {info['index']} entries={len(info['catalogue'])}")
    import json

    (Path(sys.argv[1]).parent / "fixture-catalogue.json").write_text(
        json.dumps(info["catalogue"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
