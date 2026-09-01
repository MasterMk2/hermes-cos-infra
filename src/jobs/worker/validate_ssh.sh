#!/usr/bin/env bash
# validate_ssh.sh — Phase F.1.1 Section 15: verify the restricted SSH boundary.
# Runs from the HERMES side after jobs/remote_worker.json exists.
# Never sends secrets; prints PASS/FAIL per check only.

set -u
cd "$(dirname "$0")/.."

CFG=remote_worker.json
if [ ! -f "$CFG" ]; then
  echo "SKIP: $CFG not present (remote worker not configured yet)"
  exit 0
fi

HOST=$(python3 -c "import json;print(json.load(open('$CFG'))['host'])")
USER_=$(python3 -c "import json;print(json.load(open('$CFG'))['username'])")
KEY=$(python3 -c "import json;print(json.load(open('$CFG'))['ssh_key'])")
PORT=$(python3 -c "import json;print(json.load(open('$CFG')).get('port',22))")

SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -i $KEY -p $PORT $USER_@$HOST"

pass=0; fail=0
check() { # name, expect_ok (0=must succeed, 1=must fail), command...
  local name=$1 expect=$2; shift 2
  if "$@" >/dev/null 2>&1; then rc=0; else rc=1; fi
  if [ "$rc" -eq "$expect" ]; then echo "PASS $name"; pass=$((pass+1));
  else echo "FAIL $name (rc=$rc, expected rc=$expect)"; fail=$((fail+1)); fi
}

# allowed protocol surface
check "workerctl status allowed"          0 $SSH workerctl status
check "workerctl job_status allowed"      0 $SSH workerctl job_status selftest-nonexistent

# generic shell / arbitrary commands must be rejected
check "whoami rejected"                   1 $SSH whoami
check "sh rejected"                       1 $SSH sh
check "bash rejected"                     1 $SSH bash
check "arbitrary command rejected"        1 $SSH "cat /etc/passwd"
check "powershell rejected"               1 $SSH powershell

# forwarding must be rejected by the key restrictions
check "port forwarding rejected"          1 $SSH -o ExitOnForwardFailure=yes -L 19999:127.0.0.1:22 sleep 2
check "agent forwarding rejected"         1 $SSH -o ExitOnForwardFailure=yes -A sleep 2

echo "RESULT: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
