#!/usr/bin/env bash
# Human-readable non-secret status from the published snapshot.
set -euo pipefail
INDEX=/srv/hermes/calendar/index/index.db
if [ ! -r "$INDEX" ]; then
  echo "calendar index: not published"
  exit 1
fi
python3 - "$INDEX" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in con.execute("SELECT calendar_name,calendar_id,auth_state,last_full_sync_at,last_incremental_sync_at,last_error_code,watch_state,event_count,pending_sync_age_seconds FROM calendar_status ORDER BY calendar_name"):
    print("calendar=%s id=%s auth=%s full=%s incremental=%s error=%s watch=%s events=%s pending_age=%s" % tuple(row))
PY
