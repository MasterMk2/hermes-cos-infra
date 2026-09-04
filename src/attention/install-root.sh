#!/usr/bin/env bash
# Hermes Attention Engine — one-time root installation.
#
# Installs NO credential, because the engine has none: it reaches LINE and
# Discord only through the gateway's unix socket, which fixes the recipient to
# the owner and keeps both tokens on its own side (sections 48, 111).
#
# ENGINE_USER must be given. It is the account that OWNS the source data, and
# it has to be: measured on 2026-09-04, reading a WAL-mode SQLite database
# needs write access to the directory holding it, because SQLite creates the
# -shm index before it can read. A dedicated reader account behind a read-only
# bind mount fails with "attempt to write a readonly database" on every scan.
#
# Isolation therefore comes from the unit's InaccessiblePaths, from dropping
# every supplementary group, and from PRAGMA query_only on each source — all
# three of which `att_engine.py --selftest` measures rather than asserts.
#
# Safe to re-run.

set -euo pipefail

ROOT=/srv/hermes/attention
UNIT=/etc/systemd/system/attention-engine.service
LOGROTATE=/etc/logrotate.d/hermes-attention
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_USER="${ENGINE_USER:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
if [ -z "$ENGINE_USER" ]; then
  echo "set ENGINE_USER to the account that owns /srv/hermes/hermes-data:" >&2
  echo "  sudo ENGINE_USER=<user> bash $0" >&2
  exit 1
fi
if ! id -u "$ENGINE_USER" >/dev/null 2>&1; then
  echo "user '$ENGINE_USER' does not exist" >&2
  exit 1
fi
ENGINE_GROUP="${ENGINE_GROUP:-$(id -gn "$ENGINE_USER")}"

say() { printf '  %-34s %s\n' "$1" "$2"; }

echo "== Hermes Attention Engine: install =="
say "engine account" "$ENGINE_USER:$ENGINE_GROUP"

# --- backup ---------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/g2-attention-install-$STAMP"
mkdir -p "$BACKUP"
for f in "$UNIT" "$ROOT/config/config.yaml"; do
  [ -f "$f" ] && cp -a "$f" "$BACKUP/" || true
done
say "backup" "$BACKUP"

# --- directories ----------------------------------------------------------
# app/, bin/, config/ and venv/ stay root-owned: the service must not be able
# to rewrite its own code or loosen its own configuration.
for d in "$ROOT" "$ROOT/app" "$ROOT/bin" "$ROOT/config"; do
  [ -d "$d" ] || mkdir -p "$d"
  chown root:root "$d"; chmod 0755 "$d"
done
for d in "$ROOT/state" "$ROOT/logs"; do
  [ -d "$d" ] || mkdir -p "$d"
  chown "$ENGINE_USER:$ENGINE_GROUP" "$d"; chmod 0750 "$d"
done
say "tree" "$ROOT"

# --- virtualenv -----------------------------------------------------------
# PyYAML and nothing else. The gateway client is http.client over AF_UNIX,
# which is standard library — a notification path with no third-party
# dependency is one fewer thing that can break at 03:00.
if [ -x "$ROOT/venv/bin/python" ]; then
  say "venv" "exists"
else
  python3 -m venv "$ROOT/venv"
  say "venv" "created"
fi
"$ROOT/venv/bin/pip" install --quiet --upgrade pip >/dev/null
"$ROOT/venv/bin/pip" install --quiet "PyYAML>=6,<7"
say "dependencies" "$("$ROOT/venv/bin/python" -c 'import yaml;print("pyyaml "+yaml.__version__)')"
chown -R root:root "$ROOT/venv"

# --- configuration --------------------------------------------------------
# Two values are site-specific and are therefore NOT in the repository: the
# address the Hermes API listens on, and the directory the LINE refresh cron
# job writes its reports to. Pass them as environment variables and they are
# substituted into the freshly installed configuration. Leave them unset and
# those two health probes stay disabled — which section 14 prefers to a probe
# that invents its source.
if [ -f "$ROOT/config/config.yaml" ]; then
  say "config.yaml" "exists (left untouched)"
else
  CONF_TMP="$(mktemp)"
  sed -e "s|^  hermes_health_url: \"\"|  hermes_health_url: ${ATT_HERMES_HEALTH_URL:-\"\"}|" \
      -e "s|^  line_refresh_output_dir: \"\"|  line_refresh_output_dir: ${ATT_LINE_REFRESH_DIR:-\"\"}|" \
      "$SRC/config/config.sample.yaml" >"$CONF_TMP"
  install -m 0644 -o root -g root "$CONF_TMP" "$ROOT/config/config.yaml"
  rm -f "$CONF_TMP"
  say "config.yaml" "installed from sample"
  [ -n "${ATT_HERMES_HEALTH_URL:-}" ] && say "hermes_health_url" "set" || true
  [ -n "${ATT_LINE_REFRESH_DIR:-}" ] && say "line_refresh_output_dir" "set" || true
fi

# --- application ----------------------------------------------------------
bash "$SRC/bin/deploy.sh"

# --- unit -----------------------------------------------------------------
# The account name is substituted here rather than committed: a unit file in
# the repository must not carry an operating-system account name.
TMP="$(mktemp)"
sed -e "s|@ENGINE_USER@|$ENGINE_USER|g" -e "s|@ENGINE_GROUP@|$ENGINE_GROUP|g" \
    "$SRC/systemd/attention-engine.service.in" >"$TMP"
if cmp -s "$TMP" "$UNIT" 2>/dev/null; then
  say "unit" "unchanged"
else
  install -m 0644 -o root -g root "$TMP" "$UNIT"
  say "unit" "installed"
fi
rm -f "$TMP"
systemctl daemon-reload

# --- log rotation ---------------------------------------------------------
# D.1 and G.1 both shipped without this and both listed it as a limitation.
cat >"$LOGROTATE" <<ROTATE
/srv/hermes/attention/logs/*.log {
    weekly
    rotate 6
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $ENGINE_USER $ENGINE_GROUP
    create 0640 $ENGINE_USER $ENGINE_GROUP
}
ROTATE
chmod 0644 "$LOGROTATE"
logrotate --debug "$LOGROTATE" >/dev/null 2>&1 && say "logrotate" "installed and parsed" \
  || say "logrotate" "installed (dry run reported issues; check manually)"

echo
echo "== next steps =="
cat <<'NEXT'
  1. Review /srv/hermes/attention/config/config.yaml
       - sources.line_refresh_output_dir and sources.hermes_health_url are
         empty by default; fill them in to enable those two health probes
       - quiet_hours, channels and line_budget are the policy knobs

  2. Check without sending anything:
       sudo -u <engine user> /srv/hermes/attention/venv/bin/python \
         /srv/hermes/attention/app/att_engine.py \
         --config /srv/hermes/attention/config/config.yaml --check

  3. Confirm the isolation boundary is real (opens every credential path and
     prints the errno):
       sudo systemd-run --pipe --wait --unit=attention-selftest \
         --property=... # or simply start the service and run:
       sudo -u <engine user> /srv/hermes/attention/venv/bin/python \
         /srv/hermes/attention/app/att_engine.py --selftest

  4. One scan, no service:
       ... att_engine.py --once

  5. Start it:
       sudo systemctl enable --now attention-engine
       sudo bash /srv/hermes/attention/bin/att-status.sh
NEXT
