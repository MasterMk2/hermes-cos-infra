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

## Resource Budget (Pi 4)

```text
Pi RAM ≈ 4GB total
Hermes container limit ≈ 2GB
Worker container      ≈ 1.25GB (memory-swap 同値 = swap無効)
Test container        ≈ 1.25GB (agent containerと同一limits)
```

- Worker と test container を同時に高負荷稼働させない設計を維持
- 同時実行 job 数 = 1 (concurrency=1, bridge が busy を返す)
- OOM: bridgeが docker inspect OOMKilled を検出 → job failed(COMPLETEDにしない)
- 再起動後: cron watchdog(@reboot + 毎分)がbridgeを自動起動

## Network Egress Policy (F.1.1-Pi.2)

実Coding Agent接続後も、Worker の outbound Internet は**完全遮断しない**:

```text
Worker has outbound network access required for:
- LLM provider API (coding agent がモデルを呼ぶため)
- explicitly permitted dependency resolution (allowlistされた依存解決のみ)

Worker has no published inbound port.
```

- Worker は LAN 上の他サービス(Hermes dashboard/API 含む)に依存しない
- Hermes 側から Worker への経路は bridge.sock (Unix domain socket) のみ
- egress の絞り込み(ドメイン allowlist / proxy)は将来の強化候補

## Socket Authority — Residual Risk (F.1.1-Pi.2)

`bridge.sock` へのアクセスは allowlist 済み coding operation の実行権限を
与える。Hermes container 内で prompt injection 等が成立した場合でも:

- arbitrary repo path 不可(repo_id allowlist のみ)
- arbitrary command 不可(agent command は bridge config にのみ存在)
- arbitrary image 不可(immutable version tag)
- arbitrary mount / container args 不可(unexpected_fields で reject)

ただし **allowlist 済み repository への job 起動自体は可能**。これを
明示的な residual risk として記録する。影響範囲は bridge config の
registry に登録された repo の job worktree に限定され、個人 data tree
(`/opt/data`, `/srv/hermes`, home)は Worker から不存在(実測)。

## Dashboard / API TLS TODO (F.1.1-Pi.2)

現在の Hermes dashboard/API は **LAN-only HTTP**。今回 HTTPS/TLS 導入は
Scope 外とし、勝手な新 port / proxy は導入しない。将来の hardening:

```text
Future hardening: SSH tunnel or TLS reverse proxy
```

## Job Lifecycle

NEW → QUEUED → DISPATCHING → RUNNING → AGENT_STOPPED → VALIDATING →
IMPLEMENTATION_ACCEPTED (review_status=WAITING_REVIEW) → [Phase F.2 review] → COMPLETED
/ CONTINUE_REQUIRED / WAITING_HUMAN / INTERRUPTED / FAILED / FAILED_TO_START / CANCELLED

- INTERRUPTED: supervisor再起動やbridge到達不能時。jobは保存され、
  job_continue で再採用(re-adopt)できる(実測済み)
- FAILED_TO_START: runner spawn 失敗。job_continue で再試行可能(M7)
- CANCELLED: terminal intent。cancel 後に VALIDATING / ACCEPTED へ進まない
  (cancel_requested_at を永続化、finalize は cancelled を上書きしない)
- canonical changed set = git diff (base_sha...HEAD) UNION porcelain
  (staged/unstaged/untracked)。agent が commit しても検査対象は消えない

## PiSandboxWorkerAdapter — Production Default (F.1.1-Pi.2)

PiSandboxWorkerAdapter は production の既定 transport である
(F.1.1-Pi.2 で確定)。local-fixture adapter は開発/test 専用で、
production implementation job には使用しない(MCP 層でガード済み)。

## Deployed Compose Source of Truth (F.1.1-Pi.2)

```text
実機 Pi に deploy されているのは: src/docker-compose.hermes.yaml
  - image は digest pin 済み (nousresearch/hermes-agent@sha256:d07e...)
  - bridge-sock dir mount を含む唯一の運用定義

repository 内の他の compose は template / snapshot:
  - docker-compose.yaml        … 初期 setup 用 template (:latest 記載)
  - docker-compose-stageB.yaml … stage B 移行時の template (:latest 記載)
```

`:latest` が template に残るのは「digest pin 前の初期取得用」の意味のみ。
template を実機に適用する際は必ず digest pin に更新すること。
実機 compose を不用意に変更しない。

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
