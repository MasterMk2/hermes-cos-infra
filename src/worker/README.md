# Coding Worker Infrastructure (Phase F.1.1-Pi)

Hermes Personal Chief of Staff と Coding Agent 実行環境の同一Pi上分離構成。

## Architecture

```text
Hermes Container (/opt/data)
  │  jobs-supervisor MCP (job_create/dispatch/status/...)
  │  PiSandboxWorkerAdapter
  │  typed JSON over Unix socket (bridge-sock only mount)
  ▼
Host Coding Bridge (bridge/bridge.py, cron watchdog)
  │  action allowlist: worker_status / job_start / job_status /
  │  job_result / job_cancel / job_cleanup
  │  repo_id → worktree 解決、testsはregistryから
  ▼
Disposable Worker Container (hermes-coding-worker:1.0.0, arm64)
  │  cap-drop ALL / no-new-privileges / read-only rootfs / tmpfs /tmp
  │  memory 1250m (swap無効) / cpu 2 / pids 256 / uid 1000 / port無し
  ▼
Job Worktree (/workspace) + /jobs + /agent-home
  │
  ▼
Coding Agent (fixture / 将来: claude-code 等) → evidence → IMPLEMENTATION_ACCEPTED
```

## Directory Layout

```text
/home/<PI_USER>/coding-worker/
├── repos/       登録repository(bridge config registryで管理)
├── worktrees/   job毎のgit worktree (hermes/job-<id> branch)
├── jobs/        prompt / state.json / evidence / probes (job毎)
├── logs/        bridge / runner / build logs
├── bin/         fixture_agent.py, watch-bridge.sh (container /opt/workerbin:ro)
├── bridge/      bridge.py, config.json, Dockerfile
├── bridge-sock/ bridge.sock (Hermes containerへ mount される唯一のもの)
└── agent-home/  Worker専用agent credential(Hermes credentialは絶対に置かない)
```

## Security Boundary(実測済み)

- Hermes container に docker.sock 無し(従来通り)
- Worker container に docker.sock 無し(Docker-in-Docker禁止)
- /srv/hermes/hermes-data, /opt/data, /home/<PI_USER> はWorkerから**不存在**(probe実測)
- /etc, /usr, /root, /var/log はread-only rootfsで書込不可(probe実測)
- symlink escape は _wt_path resolve check で拒否(probe実測)
- Hermesから指定できるのは repo_id / agent名 / prompt / tests_command(registry一致) / criteria のみ
  - 任意 image / mount / command / path / container args はbridgeが拒否(実測)
- worker image は immutable version tag(:latest禁止)

## Resource

- Worker: memory 1250m / memory-swap同値(swap使わない) / cpu 2 / pids 256
- 同時実行 job 数: 1(one job at a time)
- OOM: bridgeが docker inspect OOMKilled を検出 → job failed(COMPLETEDにしない)
- 再起動後: cron watchdog(@reboot + 毎分)がbridgeを自動起動

## Job Lifecycle

NEW → QUEUED → DISPATCHING → RUNNING → AGENT_STOPPED → VALIDATING →
IMPLEMENTATION_ACCEPTED (review_status=WAITING_REVIEW) → [Phase F.2 review] → COMPLETED
/ CONTINUE_REQUIRED / WAITING_HUMAN / INTERRUPTED / FAILED / CANCELLED

- INTERRUPTED: supervisor再起動やbridge到達不能時。jobは保存され、
  job_continue で再採用(re-adopt)できる(実測済み)

## Manual Commands(管理者)

```bash
# bridge状態
python3 - <<'EOF'
import socket, json
s = socket.socket(socket.AF_UNIX); s.connect("/home/<PI_USER>/coding-worker/bridge-sock/bridge.sock")
s.sendall(b'{"version":1,"action":"worker_status"}\n'); print(s.makefile("r").readline())
EOF
# job状態
... {"version":1,"action":"job_status","job_id":"job_xxx"} ...
# job停止
... {"version":1,"action":"job_cancel","job_id":"job_xxx"} ...
# bridge再起動
/home/<PI_USER>/coding-worker/bin/watch-bridge.sh
# worker image再build
docker build -t hermes-coding-worker:1.0.0 /home/<PI_USER>/coding-worker/bridge/
```

generic shell wrapper は存在しない。bridge action 以外の操作は管理者が直接行う。

## Known Limitations

- 本物のCoding Agent(Claude Code等)は未接続: arm64 imageは準備済みだが、
  agent CLI導入とそのcredential設定(Worker専用 /agent-home)はユーザー対応。
  現在のproduction agent種別は `pi-sandbox-fixture` のみ(test用)。
- bridge は <PI_USER> で稼働(専用userは sudo 再設定後に移行可能)。
- systemd 未使用(cron watchdogで代替)。sudo復旧後に systemd unit 化推奨。
- 深刻な失敗時のworktreeは保守のため保持(手動 cleanup)。
