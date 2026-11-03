#!/usr/bin/env bash
# Phase J.1 rollout — shared helpers.
#
# Sourced by preflight.sh, install.sh, rollback.sh and health.sh.
#
# Everything structured is delegated to deploy_tool.py. No heredocs here: this
# repository is edited on Windows and deployed to Linux, and an embedded script
# carrying backslashes through two levels of shell quoting is a class of bug
# this package does not need.
#
# Two rules the whole package follows:
#   * fail closed. Any hash that does not match, any path that is missing, any
#     unit that is not what we expect, stops the run before the first write.
#   * never print a secret. Tokens are generated and installed, never echoed,
#     never logged, never passed on a command line.

set -o pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; PLAIN=$'\033[0m'
if [ ! -t 1 ]; then RED=""; GREEN=""; YELLOW=""; PLAIN=""; fi

FAILURES=0
DEPLOY_TOOL="${DEPLOY_TOOL:-}"
PY="${PY:-python3}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %sok%s   %s\n' "$GREEN" "$PLAIN" "$*"; }
warn() { printf '  %swarn%s %s\n' "$YELLOW" "$PLAIN" "$*"; }
bad()  { printf '  %sFAIL%s %s\n' "$RED" "$PLAIN" "$*"; FAILURES=$((FAILURES + 1)); }
die()  { printf '%sfatal:%s %s\n' "$RED" "$PLAIN" "$*" >&2; exit 1; }

need_root() { [ "$(id -u)" = "0" ] || die "this step must run as root"; }
need_cmd()  { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

sha256_of() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

tool() { "$PY" "$DEPLOY_TOOL" "$@"; }

# `while read < <(cmd)` runs cmd in a subshell whose exit status nobody sees,
# so a manifest that failed to parse produced an empty loop and a clean pass.
# Enumerations are therefore captured first and their status checked, and a
# failure is fatal rather than silent.
#
# `read_lines <varname> <command...>` — sets the named variable, returns
# non-zero if the command failed or produced nothing.
read_lines() {
  local __name="$1"; shift
  local __out __rc
  __out="$("$@")"
  __rc=$?
  printf -v "$__name" '%s' "$__out"
  if [ "$__rc" -ne 0 ]; then
    bad "enumeration failed: $* (exit $__rc)"
    return 1
  fi
  if [ -z "$__out" ]; then
    bad "enumeration returned nothing: $*"
    return 1
  fi
  return 0
}

manifest_files() { tool manifest-files "$1" "${2:-all}"; }
manifest_drift() { tool manifest-drift "$1"; }

# Structural checks on the manifest itself, before any of it is trusted.
verify_manifest() {
  local manifest="$1" files drift
  if ! "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); sys.exit(0 if d.get("components") and d.get("job_patches") and d.get("unmodified_drift") else 1)' "$manifest"; then
    bad "manifest is unreadable or missing required sections: $manifest"
    return 1
  fi
  read_lines files manifest_files "$manifest" all || return 1
  read_lines drift manifest_drift "$manifest" || return 1
  ok "manifest parses ($(printf '%s\n' "$files" | wc -l) files, $(printf '%s\n' "$drift" | wc -l) pinned)"
  return 0
}

# --- path validation -------------------------------------------------------
#
# The installer takes the bridge's config and socket as arguments rather than
# assuming a layout. An earlier draft defaulted them to a /srv path that does
# not exist on this host; installing a guessed path and asking the operator to
# correct it afterwards is how a deployment ends up half-configured.

require_abs() {
  case "$1" in
    /*) : ;;
    *) die "$2 must be an absolute path, got '$1'" ;;
  esac
  case "$1" in
    *..*) die "$2 must not contain '..'" ;;
  esac
}

require_dir()    { [ -d "$1" ] || die "$2 is not a directory: $1"; }
require_file()   { [ -f "$1" ] || die "$2 is not a file: $1"; }
require_socket() { [ -S "$1" ] || die "$2 is not a socket: $1"; }

# --- verification ----------------------------------------------------------

# Every payload file still hashes to what the manifest recorded. A package
# whose files were edited after the manifest was generated is not a package.
verify_payload() {
  local manifest="$1" root="$2" source target mode payload baseline actual rows
  read_lines rows manifest_files "$manifest" all || return 1
  while IFS=$'\t' read -r source target mode payload baseline; do
    [ -n "$source" ] || continue
    if [ ! -f "$root/$source" ]; then
      bad "payload missing: $source"
      continue
    fi
    actual="$(sha256_of "$root/$source")"
    [ "$actual" = "$payload" ] || bad "payload hash mismatch: $source"
  done <<< "$rows"
}

# The host holds exactly the content this package was prepared against. This is
# the fail-closed gate: a target whose current content is not the recorded
# baseline has been changed by something else, and installing over it would
# silently revert whatever that was.
verify_baselines() {
  local manifest="$1" source target mode payload baseline actual rows
  read_lines rows manifest_files "$manifest" all || return 1
  while IFS=$'\t' read -r source target mode payload baseline; do
    [ -n "$source" ] || continue
    if [ "$baseline" = "-" ]; then
      if [ -e "$target" ]; then
        actual="$(sha256_of "$target")"
        if [ "$actual" = "$payload" ]; then
          ok "already installed: $target"
        else
          bad "expected a new file but $target exists with unknown content"
        fi
      else
        ok "new file: $target"
      fi
      continue
    fi
    if [ ! -f "$target" ]; then
      bad "missing target: $target"
      continue
    fi
    actual="$(sha256_of "$target")"
    if [ "$actual" = "$baseline" ]; then
      ok "baseline matches: $target"
    elif [ "$actual" = "$payload" ]; then
      ok "already installed: $target"
    else
      bad "UNEXPECTED CONTENT at $target (neither the recorded baseline nor this package)"
    fi
  done <<< "$rows"
}

# Files this change does not touch, pinned so the install refuses to run on a
# host whose Antigravity bridge is not the reviewed premium release.
verify_drift() {
  local manifest="$1" path expected note actual rows
  read_lines rows manifest_drift "$manifest" || return 1
  while IFS=$'\t' read -r path expected note; do
    [ -n "$path" ] || continue
    if [ ! -f "$path" ]; then
      bad "expected to find $path ($note)"
      continue
    fi
    actual="$(sha256_of "$path")"
    if [ "$actual" = "$expected" ]; then
      ok "untouched, as expected: $path"
    else
      bad "DRIFT at $path — this package was prepared against a different deployment"
    fi
  done <<< "$rows"
}

# The job layer is patched in place rather than copied, so its targets have
# their own baseline check — and it has to happen in *preflight*, before any
# other file has been written. An earlier version discovered a bad job target
# only after replacing the gateway modules.
#
# Each target must be either the recorded live content or that content with
# this patch already applied. Anything else, or a missing target, is fatal.
verify_job_patches() {
  local manifest="$1" kind target live note state rows required=0 seen=0
  read_lines rows tool manifest-patches "$manifest" || return 1
  while IFS=$'\t' read -r kind target live note; do
    [ -n "$kind" ] || continue
    required=$((required + 1))
    if [ ! -f "$target" ]; then
      bad "job layer target missing: $target ($note)"
      continue
    fi
    state="$(tool job-patch-state "$kind" "$target" "$live")" || {
      bad "cannot classify $target"; continue; }
    case "$state" in
      fresh)   ok "job patch pending: $kind -> $target"; seen=$((seen + 1)) ;;
      applied) ok "job patch already applied: $kind -> $target"; seen=$((seen + 1)) ;;
      *)       bad "$target is neither the recorded live content nor that content with $kind applied" ;;
    esac
  done <<< "$rows"
  if [ "$required" -lt 2 ]; then
    bad "expected both job-layer patches in the manifest, found $required"
    return 1
  fi
  [ "$seen" -eq "$required" ]
}

# --- writing ---------------------------------------------------------------

# install_file <src> <dst> <mode> <backup-dir>
#
# Returns non-zero on any failure rather than calling `die`. `die` exits the
# whole shell, which meant a failure on the second file skipped `abort()` and
# left the first one replaced with no rollback.
# Copies via a temporary file in the destination directory and renames, so a
# reader never sees a half-written module. Ownership follows the existing file
# when there is one, so an operator-owned tree stays operator-owned.
backup_file() {
  local source="$1" saved="$2" tmp
  mkdir -p "$(dirname "$saved")" || return 1
  tmp="$(mktemp "$(dirname "$saved")/.backup-XXXXXX")" || return 1
  if ! cp -p "$source" "$tmp"; then rm -f "$tmp"; return 1; fi
  mv -f "$tmp" "$saved" || { rm -f "$tmp"; return 1; }
}

install_file() {
  local src="$1" dst="$2" mode="$3" backup="$4" dir owner tmp
  dir="$(dirname "$dst")"
  mkdir -p "$dir" || { bad "cannot create $dir"; return 1; }
  if [ -f "$dst" ]; then
    mkdir -p "$backup/$(dirname "${dst#/}")" || { bad "cannot create backup dir"; return 1; }
    backup_file "$dst" "$backup/${dst#/}" || { bad "cannot back up $dst"; return 1; }
    owner="$(stat -c '%u:%g' "$dst")"
  else
    owner="$(stat -c '%u:%g' "$dir")"
    # Record that this path did not exist, so rollback can tell "restore" from
    # "remove" without re-reading the manifest's baseline column.
    mkdir -p "$backup/.absent/$(dirname "${dst#/}")"
    : > "$backup/.absent/${dst#/}"
  fi
  tmp="$(mktemp "$dir/.j1-XXXXXX")" || { bad "cannot stage into $dir"; return 1; }
  cat "$src" > "$tmp" || { rm -f "$tmp"; bad "cannot write $tmp"; return 1; }
  chmod "$mode" "$tmp" || { rm -f "$tmp"; bad "cannot set mode for $dst"; return 1; }
  chown "$owner" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$dst" || { rm -f "$tmp"; bad "cannot install $dst"; return 1; }
  return 0
}

# Prove the service identity can actually reach what it needs, as that identity,
# before the unit is started. `sudo -u` here is a read test, not a privilege
# grant: it runs `test` as the account the broker will use.
check_access_as() {
  local user="$1" path="$2" mode="$3" label="$4"
  if ! command -v runuser >/dev/null 2>&1; then
    warn "runuser unavailable; cannot verify $label as $user"
    return
  fi
  if runuser -u "$user" -- test "$mode" "$path" 2>/dev/null; then
    ok "$user can $label: $path"
  else
    bad "$user cannot $label: $path"
  fi
}
