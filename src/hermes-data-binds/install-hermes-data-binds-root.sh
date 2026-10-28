#!/usr/bin/env bash
# Make the services that use /srv/hermes/hermes-data independent of its mode.
#
# The Hermes agent re-tightens /srv/hermes/hermes-data to 0700 on every
# auth-store save, which masks every named ACL on it. Each drop-in in this
# directory replaces hermes-data with a read-only tmpfs inside one unit's
# namespace and binds back only the subdirectories that unit uses.
#
#   sudo bash install-hermes-data-binds-root.sh install   # probe, install drop-ins, test oneshot units
#   sudo bash install-hermes-data-binds-root.sh restart   # restart long-running units and probe inside them
#   sudo bash install-hermes-data-binds-root.sh status
#   sudo bash install-hermes-data-binds-root.sh rollback
#
# Prints access results and counts only; never message bodies or titles.
#
# hermes-classifier-broker is not covered. It reads the bridge bearer token
# from /srv/hermes/hermes-data/config.yaml (0640, group hermesreconcile), so
# hiding that file stopped it from starting (2026-10-28). It needs its own fix
# first: read the token from where the bridge reads it, or bind config.yaml.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DROPIN=30-hermes-data-binds.conf
UNITS=(hermes-morning-brief hermes-line-reconcile hermes-calendar hermes-antigravity)
LONG_RUNNING=(hermes-calendar hermes-antigravity)
PROBE=/run/hermes-data-binds-probe.py
BACKUP_ROOT=/srv/hermes/backups

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

write_probe() {
  cat > "$PROBE" <<'PY'
import os, sqlite3, sys
H = "/srv/hermes/hermes-data"
key = sys.argv[1]
baseline = "--baseline" in sys.argv
fails = 0
def run(name, fn, required=True, expect=True):
    global fails
    try:
        detail = fn(); got = True
    except Exception as e:
        detail = type(e).__name__; got = False
    ok = got == expect
    if ok: tag = "PASS"
    elif baseline: tag = "DENY"
    elif required: tag = "FAIL"; fails += 1
    else: tag = "INFO"
    extra = f" ({detail})" if detail else ""
    print(f"  {tag:<4} {name:<50} {'allowed' if got else 'denied'}{extra}")
def read(p):
    with open(p, "rb") as f: f.read(1)
def sq(p, table):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try: return f"{table}={c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}"
    finally: c.close()
def x_ok(p):
    if not os.access(p, os.X_OK): raise PermissionError("not executable")
def dir_write(d):
    p = os.path.join(d, f".hdb-probe-{os.getpid()}")
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd); os.unlink(p)
def rw(p): open(p, "r+b").close()
def append(p):
    if not os.path.exists(p): raise FileNotFoundError(p)
    open(p, "ab").close()
CHECKS = {
  "hermes-morning-brief": [
    ("events.db read", lambda: sq(f"{H}/events/events.db", "commitments"), True),
    ("planning dir write", lambda: dir_write(f"{H}/planning"), True),
    ("line-reconcile-health.json read", lambda: read(f"{H}/events/line-reconcile-health.json"), False),
    ("knowledge-index/line/index.db read", lambda: sq(f"{H}/knowledge-index/line/index.db", "sqlite_master"), False),
  ],
  "hermes-line-reconcile": [
    ("reconcile-line.sh executable", lambda: x_ok(f"{H}/tools/line-knowledge/reconcile-line.sh"), True),
    ("tools/events/event_extract.py read", lambda: read(f"{H}/tools/events/event_extract.py"), True),
    ("commitments/store.py read", lambda: read(f"{H}/commitments/store.py"), True),
    ("knowledge-index/line/index.db read", lambda: sq(f"{H}/knowledge-index/line/index.db", "sqlite_master"), True),
    ("events dir write", lambda: dir_write(f"{H}/events"), True),
    ("events.db open r+b", lambda: rw(f"{H}/events/events.db"), True),
    ("logs/line-reconcile.log append-open", lambda: append(f"{H}/logs/line-reconcile.log"), True),
    ("logs/line-refresh.log read", lambda: read(f"{H}/logs/line-refresh.log"), False),
  ],
  "hermes-calendar": [
    ("events.db read", lambda: sq(f"{H}/events/events.db", "commitments"), True),
  ],
  "hermes-antigravity": [
    ("events.db read", lambda: sq(f"{H}/events/events.db", "commitments"), True),
    ("knowledge-index/line/index.db read", lambda: sq(f"{H}/knowledge-index/line/index.db", "sqlite_master"), True),
    ("knowledge-index/discord/index.db read", lambda: sq(f"{H}/knowledge-index/discord/index.db", "sqlite_master"), False),
    ("tools/line-knowledge/line_mcp.py read", lambda: read(f"{H}/tools/line-knowledge/line_mcp.py"), False),
    ("tools/events/event_status.py read", lambda: read(f"{H}/tools/events/event_status.py"), False),
    ("logs/line-refresh.log read", lambda: read(f"{H}/logs/line-refresh.log"), False),
    ("events/line-reconcile-health.json read", lambda: read(f"{H}/events/line-reconcile-health.json"), False),
  ],
}
print(f"  probe {key} uid={os.getuid()} gid={os.getgid()} groups={sorted(os.getgroups())}{' [baseline]' if baseline else ''}")
for name, fn, req in CHECKS[key]:
    run(name, fn, required=req)
if not baseline:
    for secret in (".env", "auth.json", "config.yaml", "sessions", "memories"):
        run(f"{secret} not reachable", lambda s=secret: os.lstat(f"{H}/{s}"), expect=False)
print(f"  RESULT {key}: {'baseline only' if baseline else ('OK' if fails == 0 else f'{fails} required check(s) failed')}")
sys.exit(0 if (baseline or fails == 0) else 1)
PY
  chmod 0644 "$PROBE"
}

identity_props() {  # unit -> -p User=... -p Group=... -p SupplementaryGroups=...
  local u=$1 user group supp
  user="$(systemctl show -p User --value "$u.service")"
  group="$(systemctl show -p Group --value "$u.service")"
  supp="$(systemctl show -p SupplementaryGroups --value "$u.service")"
  [ -n "$user" ] || die "$u has no User="
  printf '%s\n' -p "User=$user"
  [ -n "$group" ] && printf '%s\n' -p "Group=$group"
  [ -n "$supp" ] && printf '%s\n' -p "SupplementaryGroups=$supp"
  return 0
}

transient_probe() {  # unit with|baseline
  local u=$1 mode=$2
  local -a props=()
  mapfile -t props < <(identity_props "$u")
  props+=(-p NoNewPrivileges=yes -p ProtectSystem=strict)
  if [ "$mode" = with ]; then
    while IFS= read -r line; do props+=(-p "$line"); done \
      < <(grep -E '^(TemporaryFileSystem|BindPaths|BindReadOnlyPaths)=' "$HERE/$u.conf")
    systemd-run --quiet --wait --pipe --collect "${props[@]}" /usr/bin/python3 -s "$PROBE" "$u"
  else
    systemd-run --quiet --wait --pipe --collect "${props[@]}" /usr/bin/python3 -s "$PROBE" "$u" --baseline
  fi
}

run_unit_override() {  # unit label command-line
  local u=$1 label=$2 cmd=$3 dd=/run/systemd/system/$1.service.d rc=0 inv
  install -d -m 0755 "$dd"
  printf '[Service]\nExecStart=\nExecStart=%s\n' "$cmd" > "$dd/zz-$label.conf"
  systemctl daemon-reload
  systemctl start "$u.service" || rc=$?
  inv="$(systemctl show -p InvocationID --value "$u.service")"
  journalctl -o cat --no-pager "_SYSTEMD_INVOCATION_ID=$inv" | tail -n 40 || true
  rm -f "$dd/zz-$label.conf"; rmdir "$dd" 2>/dev/null || true
  systemctl daemon-reload
  return "$rc"
}

ns_probe() {  # unit: run the probe inside the running unit's mount namespace with its exact credentials
  local u=$1 pid uid gid groups
  pid="$(systemctl show -p MainPID --value "$u.service")"
  [ "$pid" -gt 0 ] 2>/dev/null || { echo "  FAIL $u has no MainPID"; return 1; }
  uid="$(awk '/^Uid:/{print $2}' /proc/$pid/status)"
  gid="$(awk '/^Gid:/{print $2}' /proc/$pid/status)"
  groups="$(awk '/^Groups:/{$1=""; print}' /proc/$pid/status | xargs | tr ' ' ',')"
  if [ -n "$groups" ]; then
    nsenter -t "$pid" -m -- setpriv --reuid="$uid" --regid="$gid" --groups="$groups" -- /usr/bin/python3 -s "$PROBE" "$u"
  else
    nsenter -t "$pid" -m -- setpriv --reuid="$uid" --regid="$gid" --clear-groups -- /usr/bin/python3 -s "$PROBE" "$u"
  fi
}

cmd_install() {
  for u in "${UNITS[@]}"; do
    [ -f "$HERE/$u.conf" ] || die "missing $HERE/$u.conf"
    systemctl cat "$u.service" >/dev/null 2>&1 || die "unit $u.service not found"
  done
  local ts bk
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  bk="$BACKUP_ROOT/hermes-data-binds-$ts"
  step "backup -> $bk"
  install -d -o root -g root -m 0700 "$bk"
  stat -c '%a %U:%G %n' /srv/hermes/hermes-data > "$bk/hermes-data-mode.txt"
  getfacl -p /srv/hermes/hermes-data /srv/hermes/hermes-data/logs > "$bk/acl.txt" 2>/dev/null || true
  for u in "${UNITS[@]}"; do systemctl cat "$u.service" > "$bk/$u.service.before" 2>&1 || true; done
  echo "$bk" > /run/hermes-data-binds-last-backup
  echo "hermes-data mode now: $(cat "$bk/hermes-data-mode.txt")"

  write_probe
  step "baseline: each identity WITHOUT the binds (informational)"
  for u in "${UNITS[@]}"; do transient_probe "$u" baseline || true; done

  step "pre-validation: each identity WITH the planned binds (must pass)"
  local bad=0
  for u in "${UNITS[@]}"; do transient_probe "$u" with || bad=1; done
  [ "$bad" -eq 0 ] || { rm -f "$PROBE"; die "pre-validation failed; nothing was installed"; }

  step "install drop-ins"
  for u in "${UNITS[@]}"; do
    local d=/etc/systemd/system/$u.service.d
    install -d -m 0755 "$d"
    [ -e "$d/$DROPIN" ] && cp -p "$d/$DROPIN" "$bk/$u.$DROPIN.prev"
    install -o root -g root -m 0644 "$HERE/$u.conf" "$d/$DROPIN"
  done
  systemctl daemon-reload
  for u in "${UNITS[@]}"; do
    systemctl show -p DropInPaths --value "$u.service" | tr ' ' '\n' | grep -q "/$DROPIN$" \
      && echo "  loaded: $u" || die "$u did not load $DROPIN"
  done

  step "hermes-morning-brief: real unit sandbox, 'plan' (never delivers)"
  run_unit_override hermes-morning-brief probe "/usr/bin/python3 -s $PROBE hermes-morning-brief" \
    || die "morning-brief probe failed in the real unit. Rollback: sudo bash $0 rollback"
  run_unit_override hermes-morning-brief plan "/srv/hermes/planning/venv/bin/python -m planning.cli --config /srv/hermes/planning/config.yaml plan" \
    || die "morning-brief plan failed in the real unit. Rollback: sudo bash $0 rollback"

  step "hermes-line-reconcile: real unit sandbox, probe only (no reconcile run)"
  run_unit_override hermes-line-reconcile probe "/usr/bin/python3 -s $PROBE hermes-line-reconcile" \
    || die "line-reconcile probe failed in the real unit. Rollback: sudo bash $0 rollback"

  rm -f "$PROBE"
  step "done"
  echo "Drop-ins installed. Oneshot units use them from their next run (next brief: 08:30)."
  echo "Long-running units still run in their old namespace until restarted:"
  echo "  sudo bash $0 restart"
  echo "Backup: $bk"
}

cmd_restart() {
  write_probe
  local bad=0
  for u in "${LONG_RUNNING[@]}"; do
    step "restart $u"
    systemctl show -p DropInPaths --value "$u.service" | tr ' ' '\n' | grep -q "/$DROPIN$" || { echo "  FAIL drop-in not loaded"; bad=1; continue; }
    systemctl restart "$u.service"
    sleep 6
    if ! systemctl is-active --quiet "$u.service"; then
      echo "  FAIL $u is $(systemctl is-active "$u.service")"; journalctl -u "$u.service" -n 15 -o cat --no-pager | cut -c1-200; bad=1; continue
    fi
    systemctl show "$u.service" -p ActiveState -p SubState -p NRestarts -p ExecMainStartTimestamp --no-pager | sed 's/^/  /'
    ns_probe "$u" || bad=1
  done
  step "functional checks"
  ls -la /run/hermes/classifier.sock 2>&1 | sed 's/^/  /' || bad=1
  python3 - <<'PY' || bad=1
import urllib.request
try:
    with urllib.request.urlopen("http://172.18.0.1:8765/health", timeout=10) as r:
        print("  antigravity /health HTTP", r.status)
except Exception as e:
    print("  antigravity /health FAILED", type(e).__name__); raise SystemExit(1)
PY
  journalctl -u hermes-calendar.service --since "-2 min" -o cat --no-pager | cut -c1-160 | tail -n 6 | sed 's/^/  calendar: /'
  rm -f "$PROBE"
  [ "$bad" -eq 0 ] && echo "RESTART OK" || die "a restart check failed; see above. Rollback: sudo bash $0 rollback"
}

cmd_status() {
  stat -c 'hermes-data: %a %U:%G' /srv/hermes/hermes-data
  for u in "${UNITS[@]}"; do
    local loaded=no
    systemctl show -p DropInPaths --value "$u.service" | tr ' ' '\n' | grep -q "/$DROPIN$" && loaded=yes
    printf '%-28s drop-in=%-3s active=%-9s result=%s\n' "$u" "$loaded" \
      "$(systemctl is-active "$u.service")" "$(systemctl show -p Result --value "$u.service")"
  done
}

cmd_rollback() {
  for u in "${UNITS[@]}"; do
    rm -f "/etc/systemd/system/$u.service.d/$DROPIN"
    rmdir "/etc/systemd/system/$u.service.d" 2>/dev/null || true
  done
  rm -f "$PROBE"
  systemctl daemon-reload
  for u in "${LONG_RUNNING[@]}"; do
    systemctl is-active --quiet "$u.service" && systemctl restart "$u.service" || true
  done
  echo "Drop-ins removed; long-running units restarted without them."
  echo "Note: they need /srv/hermes/hermes-data to be 0710 again to work (restart the hermes container)."
}

case "${1:-}" in
  install)  cmd_install ;;
  restart)  cmd_restart ;;
  status)   cmd_status ;;
  rollback) cmd_rollback ;;
  *) die "usage: $0 install|restart|status|rollback" ;;
esac
