#!/usr/bin/env bash
# Move hermes-discord-completion from the operator's user manager to a
# dedicated, sandboxed system service.
#
#   sudo bash install-discord-completion-root.sh install   # backup, identity, ACL, units, probes (timer NOT enabled)
#   sudo bash install-discord-completion-root.sh enable    # enable the system timer (user timer must be off)
#   sudo bash install-discord-completion-root.sh status
#   sudo bash install-discord-completion-root.sh rollback  # remove system unit, ACL entries and identity
#
# Never prints message bodies or commitment titles.
set -euo pipefail

SVC_USER=hermescompletion
# The operator account whose user manager ran the previous user-level unit.
# Taken from sudo so the account name is not committed to the repository.
OPERATOR="${HDC_OPERATOR:-${SUDO_USER:-}}"
UNIT=hermes-discord-completion
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVENTS_DIR=/srv/hermes/hermes-data/events
EVENTS_DB=$EVENTS_DIR/events.db
TOOLS_PKG=/srv/hermes/hermes-data/tools/commitments
DISCORD_DIR=/srv/hermes/knowledge/discord
DISCORD_DB=$DISCORD_DIR/messages.db
MNT=/opt/hermes-discord-completion
BACKUP_ROOT=/srv/hermes/backups
DROPIN_DIR=/run/systemd/system/$UNIT.service.d
PROBE=/run/$UNIT-probe.py

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ -n "$OPERATOR" ] && [ "$OPERATOR" != root ] || die "run via sudo from the operator account, or set HDC_OPERATOR"

user_timer_state() {
  systemctl --user -M "$OPERATOR@" is-active "$UNIT.timer" 2>/dev/null || true
}

run_with_dropin() {   # $1 = label, $2 = ExecStart command line
  install -d -m 0755 "$DROPIN_DIR"
  printf '[Service]\nExecStart=\nExecStart=%s\n' "$2" > "$DROPIN_DIR/zz-$1.conf"
  systemctl daemon-reload
  local rc=0
  systemctl start "$UNIT.service" || rc=$?
  local inv
  inv="$(systemctl show -p InvocationID --value "$UNIT.service")"
  journalctl -o cat --no-pager "_SYSTEMD_INVOCATION_ID=$inv" | tail -n 40 || true
  rm -f "$DROPIN_DIR/zz-$1.conf"
  rmdir "$DROPIN_DIR" 2>/dev/null || true
  systemctl daemon-reload
  return "$rc"
}

write_probe() {
  cat > "$PROBE" <<'PY'
import os, socket, sqlite3, sys, pwd
ok = True
def check(name, expect_ok, fn):
    global ok
    try:
        fn(); got = True; detail = "allowed"
    except Exception as e:
        got = False; detail = type(e).__name__
    passed = (got == expect_ok)
    ok &= passed
    print(f"{'PASS' if passed else 'FAIL'}  {name:<46} expected={'allowed' if expect_ok else 'denied'} got={detail}")
def require(cond):
    if not cond:
        raise AssertionError()
pw = pwd.getpwuid(os.getuid())
print("identity", pw.pw_name, "uid", os.getuid(), "groups", sorted(os.getgroups()))
check("identity is hermescompletion", True, lambda: require(pw.pw_name == "hermescompletion"))
check("supplementary groups == own group only", True, lambda: require(not set(os.getgroups()) - {os.getgid()}))
check("read /srv/hermes/hermes-data/.env", False, lambda: open("/srv/hermes/hermes-data/.env").read(1))
check("list /srv/hermes/hermes-data", False, lambda: os.listdir("/srv/hermes/hermes-data"))
check("read /srv/hermes/hermes-data/auth.json", False, lambda: open("/srv/hermes/hermes-data/auth.json").read(1))
check("connect /var/run/docker.sock", False, lambda: socket.socket(socket.AF_UNIX).connect("/var/run/docker.sock"))
check("list /etc/hermes-secrets", False, lambda: os.listdir("/etc/hermes-secrets"))
check("list /home", False, lambda: require(os.listdir("/home")))
check("open AF_INET socket", False, lambda: socket.socket(socket.AF_INET).close())
check("write code dir (read-only bind)", False, lambda: open("/opt/hermes-discord-completion/lib/commitments/.probe", "w"))
check("write Discord archive dir", False, lambda: open("/srv/hermes/knowledge/discord/.probe", "w"))
def events_dir_write():
    p = f"/opt/hermes-discord-completion/events/.hdc-probe-{os.getpid()}"
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd); os.unlink(p)
check("create+unlink temp file in events dir", True, events_dir_write)
check("open events.db r+b (no write)", True, lambda: open("/opt/hermes-discord-completion/events/events.db", "r+b").close())
def events_ro():
    c = sqlite3.connect("file:/opt/hermes-discord-completion/events/events.db?mode=ro", uri=True)
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    print("      events.db commitments:", c.execute("SELECT COUNT(*) FROM commitments").fetchone()[0])
check("events.db read + quick_check", True, events_ro)
def discord_ro():
    c = sqlite3.connect("file:/srv/hermes/knowledge/discord/messages.db?mode=ro", uri=True)
    print("      messages.db rows:", c.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
check("messages.db read (WAL, read-only)", True, discord_ro)
print("PROBE", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
PY
  chmod 0644 "$PROBE"
}

cmd_install() {
  for f in "$UNIT.service" "$UNIT.timer"; do [ -f "$HERE/$f" ] || die "missing $HERE/$f"; done
  [ -f "$TOOLS_PKG/discord_completion.py" ] || die "missing $TOOLS_PKG/discord_completion.py"
  [ -f "$EVENTS_DB" ] || die "missing $EVENTS_DB"
  [ -f "$DISCORD_DB" ] || die "missing $DISCORD_DB"
  if [ -e "$MNT" ] && [ -n "$(find "$MNT" -mindepth 1 -not -type d -print -quit)" ]; then
    die "$MNT exists and contains files; refusing to reuse it"
  fi

  local ts bk
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  bk="$BACKUP_ROOT/discord-completion-hardening-$ts"
  step "backup -> $bk"
  install -d -o root -g root -m 0700 "$bk"
  getfacl -p "$EVENTS_DIR" "$EVENTS_DB" "$DISCORD_DIR" "$DISCORD_DIR"/messages.db* > "$bk/acl-before.txt" 2>/dev/null || true
  local op_home; op_home="$(getent passwd "$OPERATOR" | cut -d: -f6)"
  cp -p "$op_home/.config/systemd/user/$UNIT.service" "$op_home/.config/systemd/user/$UNIT.timer" "$bk/" 2>/dev/null || true
  python3 - "$EVENTS_DB" "$bk/events.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
print("events.db backup integrity:", dst.execute("PRAGMA integrity_check").fetchone()[0],
      "commitments:", dst.execute("SELECT COUNT(*) FROM commitments").fetchone()[0])
dst.close(); src.close()
PY
  chmod 0600 "$bk/events.db"
  echo "$bk" > /run/$UNIT-last-backup

  step "identity $SVC_USER"
  if ! getent passwd "$SVC_USER" >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --comment "Hermes Discord completion" "$SVC_USER"
  fi
  id "$SVC_USER"

  step "ACL (named entries only; masks are not widened)"
  setfacl -m "u:$SVC_USER:rwx,d:u:$SVC_USER:rw-" "$EVENTS_DIR"
  setfacl -m "u:$SVC_USER:rw-" "$EVENTS_DB"
  setfacl -m "u:$SVC_USER:r-x,d:u:$SVC_USER:r--" "$DISCORD_DIR"
  for f in "$DISCORD_DIR"/messages.db "$DISCORD_DIR"/messages.db-wal "$DISCORD_DIR"/messages.db-shm; do
    [ -e "$f" ] && setfacl -m "u:$SVC_USER:r--" "$f"
  done
  getfacl -p "$EVENTS_DIR" "$EVENTS_DB" "$DISCORD_DIR" "$DISCORD_DIR"/messages.db* > "$bk/acl-after.txt" 2>/dev/null || true
  diff "$bk/acl-before.txt" "$bk/acl-after.txt" || true

  step "mount points $MNT"
  install -d -o root -g root -m 0755 "$MNT" "$MNT/events" "$MNT/lib" "$MNT/lib/commitments"

  step "units"
  install -o root -g root -m 0644 "$HERE/$UNIT.service" "$HERE/$UNIT.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemd-analyze verify "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer" 2>&1 \
    | grep -F "$UNIT" || echo "systemd-analyze verify: no findings for $UNIT"

  step "DAC check outside the sandbox (the normal path must stay closed)"
  if runuser -u "$SVC_USER" -- test -r "$EVENTS_DB"; then
    die "$SVC_USER can reach $EVENTS_DB through hermes-data; expected no traversal"
  else
    echo "PASS  $SVC_USER cannot traverse /srv/hermes/hermes-data"
  fi
  runuser -u "$SVC_USER" -- test -r "$DISCORD_DB" && echo "PASS  $SVC_USER can read the Discord archive" \
    || die "$SVC_USER cannot read $DISCORD_DB"

  step "isolation probe inside the real unit sandbox"
  write_probe
  run_with_dropin probe "/usr/bin/python3 -s $PROBE" || { rm -f "$PROBE"; die "isolation probe failed (system timer NOT enabled). Rollback: sudo bash $0 rollback"; }
  rm -f "$PROBE"

  step "dry run inside the real unit sandbox (no --apply)"
  run_with_dropin dryrun "/usr/bin/python3 -s -m commitments.discord_completion --events-db $MNT/events/events.db --discord-db $DISCORD_DB scan" \
    || die "dry run failed (system timer NOT enabled). Rollback: sudo bash $0 rollback"

  step "done"
  echo "System unit installed and verified; the system timer is NOT enabled."
  echo "User timer ($OPERATOR) is currently: $(user_timer_state)"
  echo "Next: stop the user timer as $OPERATOR, then: sudo bash $0 enable"
  echo "Backup: $bk"
}

cmd_enable() {
  [ -f "/etc/systemd/system/$UNIT.service" ] || die "run 'install' first"
  local st; st="$(user_timer_state)"
  [ "$st" != "active" ] || die "user timer of $OPERATOR is still active; run as $OPERATOR: systemctl --user disable --now $UNIT.timer"
  systemctl enable --now "$UNIT.timer"
  systemctl list-timers "$UNIT.timer" --no-pager
}

cmd_status() {
  systemctl list-timers "$UNIT.timer" --no-pager 2>/dev/null || true
  systemctl show "$UNIT.service" -p Result -p ExecMainStatus -p ExecMainExitTimestamp -p NRestarts --no-pager 2>/dev/null || true
  echo "user timer ($OPERATOR): $(user_timer_state)"
  journalctl -u "$UNIT.service" -n 5 -o short-iso --no-pager 2>/dev/null || true
}

cmd_rollback() {
  systemctl disable --now "$UNIT.timer" 2>/dev/null || true
  systemctl stop "$UNIT.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer"
  rm -rf "$DROPIN_DIR"; rm -f "$PROBE"
  systemctl daemon-reload
  if getent passwd "$SVC_USER" >/dev/null; then
    setfacl -x "u:$SVC_USER,d:u:$SVC_USER" "$EVENTS_DIR" 2>/dev/null || true
    setfacl -x "u:$SVC_USER" "$EVENTS_DB" 2>/dev/null || true
    find "$EVENTS_DIR" -maxdepth 1 -type f -exec setfacl -x "u:$SVC_USER" {} + 2>/dev/null || true
    setfacl -x "u:$SVC_USER,d:u:$SVC_USER" "$DISCORD_DIR" 2>/dev/null || true
    find "$DISCORD_DIR" -maxdepth 1 -type f -exec setfacl -x "u:$SVC_USER" {} + 2>/dev/null || true
    userdel "$SVC_USER" 2>/dev/null || true
    getent group "$SVC_USER" >/dev/null && groupdel "$SVC_USER" 2>/dev/null || true
  fi
  rmdir "$MNT/lib/commitments" "$MNT/lib" "$MNT/events" "$MNT" 2>/dev/null || true
  echo "System unit, ACL entries and identity removed."
  echo "The Discord archive dir may keep base default ACL entries; the original is in acl-before.txt of the backup."
  echo "Re-enable the user timer as $OPERATOR: systemctl --user enable --now $UNIT.timer"
}

case "${1:-}" in
  install)  cmd_install ;;
  enable)   cmd_enable ;;
  status)   cmd_status ;;
  rollback) cmd_rollback ;;
  *) die "usage: $0 install|enable|status|rollback" ;;
esac
