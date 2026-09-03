#!/usr/bin/env bash
# ============================================================================
# Discord Knowledge Collector - unprivileged deployment.
#
#   bash deploy.sh [--source DIR] [--no-venv]
#
# Run this as the OPERATOR account (never as root, never as the service
# account) AFTER install-root.sh has created the directory tree. It copies the
# application and configuration into place and builds the virtualenv.
#
# It touches no secret, starts no service and never writes to
# /srv/hermes/hermes-data except the search-index directory.
# ============================================================================
set -euo pipefail

APP_ROOT="/srv/hermes/discord-knowledge"
INDEX_DIR="/srv/hermes/hermes-data/knowledge-index/discord"
MCP_DIR="/srv/hermes/hermes-data/tools/discord-knowledge"
EVENTS_DIR="/srv/hermes/hermes-data/tools/events"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_VENV=1

die() { echo "deploy: $*" >&2; exit 1; }
say() { echo "  $*"; }

# Install one file and say whether it actually changed anything.
#
# Silence here once hid a real regression: a file edited straight into the
# destination was later reverted by a deploy from a stale source, and nothing
# in the output said so. Every install now reports new / updated / unchanged,
# and a summary counts them.
CHANGED=0
UNCHANGED=0
CREATED=0
install_reporting() {
  local mode="$1" src="$2" dest="$3" label="${4:-$(basename "$2")}"
  if [[ ! -e "$dest" ]]; then
    install -m "$mode" "$src" "$dest"
    CREATED=$((CREATED + 1))
    say "+ new       ${label}"
  elif cmp -s "$src" "$dest"; then
    UNCHANGED=$((UNCHANGED + 1))
  else
    install -m "$mode" "$src" "$dest"
    CHANGED=$((CHANGED + 1))
    say "~ updated   ${label}  ($(sha256sum "$dest" | cut -c1-12))"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)   SOURCE_DIR="${2:-}"; [[ -n "$SOURCE_DIR" ]] || die "--source needs a value"; shift 2 ;;
    --source=*) SOURCE_DIR="${1#*=}"; shift ;;
    --no-venv)  BUILD_VENV=0; shift ;;
    -h|--help)  sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
done

[[ "$(id -u)" != "0" ]] || die "run as the operator account, not as root"
[[ -d "$APP_ROOT/app" ]] || die "$APP_ROOT/app is missing - run install-root.sh first"
[[ -w "$APP_ROOT/app" ]] || die "$APP_ROOT/app is not writable by $(id -un)"
[[ -d "$SOURCE_DIR/app" ]] || die "no app/ directory under $SOURCE_DIR"

echo "== collector application =="
for f in "$SOURCE_DIR"/app/*.py; do
  install_reporting 0644 "$f" "$APP_ROOT/app/$(basename "$f")" "app/$(basename "$f")"
done
say "$(ls -1 "$SOURCE_DIR"/app/*.py | wc -l) modules considered in $APP_ROOT/app"

echo "== configuration =="
if [[ -f "$APP_ROOT/config/config.yaml" ]]; then
  say "= config.yaml already present; left untouched (it holds your allowlist)"
  say "  reference copy: $APP_ROOT/config/config.yaml.dist"
  install -m 0644 "$SOURCE_DIR/config/config.yaml" "$APP_ROOT/config/config.yaml.dist"
else
  install -m 0644 "$SOURCE_DIR/config/config.yaml" "$APP_ROOT/config/config.yaml"
  say "installed config.yaml (placeholders; nothing is collected until you edit it)"
fi

echo "== helper scripts =="
for f in "$SOURCE_DIR"/bin/*.sh; do
  install_reporting 0755 "$f" "$APP_ROOT/bin/$(basename "$f")" "bin/$(basename "$f")"
done
say "$(ls -1 "$SOURCE_DIR"/bin/*.sh | wc -l) scripts considered in $APP_ROOT/bin"

if (( BUILD_VENV )); then
  echo "== virtualenv =="
  [[ -d "$APP_ROOT/venv" ]] || python3 -m venv "$APP_ROOT/venv"
  "$APP_ROOT/venv/bin/pip" install --quiet --upgrade pip
  "$APP_ROOT/venv/bin/pip" install --quiet -r "$SOURCE_DIR/requirements.txt"
  say "$("$APP_ROOT/venv/bin/python" -c 'import discord,yaml,sys; print(f"python {sys.version.split()[0]}, discord.py {discord.__version__}, PyYAML {yaml.__version__}")')"
  # The service only ever reads the venv; ProtectSystem=strict enforces that.
  chmod -R go-w "$APP_ROOT/venv"
fi

echo "== retrieval side (read by the assistant, not by the collector) =="
mkdir -p "$INDEX_DIR" "$MCP_DIR"
install_reporting 0644 "$SOURCE_DIR/mcp/discord_mcp.py" "$MCP_DIR/discord_mcp.py" "mcp/discord_mcp.py"
if [[ -f "$SOURCE_DIR/events/discord_event_extract.py" ]]; then
  install_reporting 0644 "$SOURCE_DIR/events/discord_event_extract.py"     "$EVENTS_DIR/discord_event_extract.py" "events/discord_event_extract.py"
fi
echo "== summary =="
say "${CREATED} new, ${CHANGED} updated, ${UNCHANGED} already current"
if (( CHANGED > 0 )); then
  say "NOTE: updated files above replaced a DIFFERENT version that was already"
  say "      installed. If you edited anything directly in place, it is now gone."
fi

echo "== configuration check =="
"$APP_ROOT/venv/bin/python" "$APP_ROOT/app/collector.py" \
  --config "$APP_ROOT/config/config.yaml" --check && rc=0 || rc=$?
if (( rc == 78 )); then
  say "collector reports: allowlist not filled in yet (exit 78, expected on a"
  say "fresh install). Nothing will be collected until guild and channel ids"
  say "are added to $APP_ROOT/config/config.yaml."
elif (( rc != 0 )); then
  die "collector --check failed with exit $rc"
fi

echo
echo "Deployed. The service is NOT started by this script."
echo "Next: fill in the allowlist, install the real bot token, then"
echo "      sudo systemctl start discord-knowledge"
