# Phase 1 — Maintenance (Configuration Modernization & Security Regression)

## 目的

安定稼働中のHermes環境に対し、新機能を追加せず以下のみを安全に実施する。

1. hostname表記の整合
2. config現行schema移行の確認
3. Docker image digest pinningの実態確認
4. 既存機能・Security Boundaryのregression test

## 作業と結果

### Hostname

- kernel/`/etc/hostname` は既にcanonical表記、`/etc/hosts` のみ旧表記が残存
- backup取得の上、hostsのcanonical部分のみ整合(alias構造は維持)
- SSH user / host key は未接触(reboot不要)

### Image pinning

- effective compose image は既に `nousresearch/hermes-agent@sha256:d07e791...` にpin済み
  (running container / local RepoDigests も同一) → 変更不要、upgrade不実施

### Config migration

- config.yaml は既に `_config_version: 39` で正規migration済み(旧config 2,020行 → 新schema 180行)
- 根拠: 全logsにmigration警告0件 + `hermes doctor` が
  "Config version up to date (v39)" / "No deprecated config keys" を検証
- version marker手書き偽装ではないことをdoctorにより確認

### Regression (全PASS)

- Provider: `HERMES_PROVIDER_OK` 応答、model/provider維持
- LINE MCP: search+get動作(本文非表示)
- MyLINE refresh: snapshot/index検証 + integrity ok
- Cron: 4 job全てenabled・schedule無変更
- Network: LAN bindのみ、0.0.0.0 bind無し、新port無し
- Auth: API未認証401 / Dashboard未認証302
- Mounts: hermes-data bindのみ、docker.sock無し
- Storage: Docker Root Dir はUSB、SD 35%使用
- backup: `/srv/hermes/backups/config-modernization-*/`

## Known Findings

- `AGENTS.md` がsecurity scannerに `prompt_injection` でblock、
  `USER.md` entry が `html_comment_injection` でblock(当時) → 次Phase対応
