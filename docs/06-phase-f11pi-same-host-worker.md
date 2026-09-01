# Phase 6 — F.1.1-Pi Same-Host Isolated Coding Worker

## 目的

外部PC Workerを使わず、同一Raspberry Pi内で
Hermes container と **execution boundaryが別の** Worker containerを実現する。

## Architecture

```text
Hermes Container (/opt/data)
  ↓ jobs-supervisor MCP → PiSandboxWorkerAdapter
  ↓ typed JSON over Unix socket (socket dirのみmount)
Host Coding Bridge (cron watchdogで常駐)
  ↓ action allowlist: worker_status / job_start / job_status /
    job_result / job_cancel / job_cleanup
  ↓ repo_id → worktree解決 / test commandはregistryから / image・mount・
    command・path はHermesから指定不可能
Disposable Worker Container
  ↓ /workspace(job worktree) + /jobs + /agent-home のみmount
Coding Agent → evidence → deterministic validation
  → IMPLEMENTATION_ACCEPTED → review_status=WAITING_REVIEW
```

## Security Features

- Worker container: `--cap-drop ALL`, `no-new-privileges`, `--read-only`
  rootfs + tmpfs /tmp, memory 1250m(swap禁止), cpu 2, pids 256,
  uid 1000, published port無し, 使い捨て(job毎に起動・削除)
- docker.sock はHermesにもWorkerにも渡さない(Docker-in-Docker禁止)
- Hermes Personal Data(`/opt/data`, `/srv/hermes`, home directory)は
  Worker containerのmountに存在しない
- BridgeはHermesから image / mount / command / path を一切受け付けない
  (unexpected_fields / unknown_repo_id / unknown_agent でreject、実測)
- Worker image はimmutable version tag(`:latest`禁止)
- 同時実行job数 = 1 / OOM検出(docker inspect OOMKilled)→ failed / heartbeat watchdog

## Isolation Test (fixture agentがcontainer内で実行、全PASS)

```text
/workspace書込 OK / /tmp OK / /agent-home OK
/etc /usr /root /var/log 書込 BLOCKED (read-only rootfs)
/opt/data /srv/hermes /home/<PI_USER> docker.sock — 全て不存在
symlink escape — blocked
```

## E2E Tests (bridge経由、全PASS)

- T-P2 fixture job: 実装→test→evidence→IMPLEMENTATION_ACCEPTED→WAITING_REVIEW
- T-P3 continuation: 2 runs、同一worktree
- T-P4 false completion: 自己申告「Done」でもtest失敗→COMPLETED不遷移→WAITING_HUMAN
- T-P4 no-progress: 同一signature 3連続→停止
- T-P5 crash recovery: runner死→reconcile→INTERRUPTED→job_continue→re-adopt→完了
- T-P6/T-P7: 未登録repo_id・任意field・任意action・壊れたversionは全てreject

## Resilience

- 全stepでsetup-state.jsonを更新 → 再起動後も継続(実際にreboot検証済み)
- bridge は cron watchdog (@reboot + 毎分、flock) で自動起動
- 発生した問題は全て分類(config / implementation bug / environmental)し、
  root cause修正後に再試行: makefile mode / proto fields / container内
  prompt path / zombie pid / pkill自己一致 / MCP env sanitization

## Known Limitations

- 実Coding Agent(Claude Code等)は未接続: image準備済み、agent選定+credential待ち
- bridge は管理userで稼働(cron watchdog方式)。専用user+systemd化は今後の強化
- Worker root は `/home/<PI_USER>/coding-worker` (個人data treeとは分離)
