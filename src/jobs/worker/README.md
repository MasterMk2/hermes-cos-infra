# External Coding Worker Setup (Phase F.1.1)

Hermes SupervisorとCoding Agent実行環境を分離するためのWorker側セットアップ。
**この作業はユーザー自身が行う**(root権限・認証・repo登録は人間の責任)。

## 必要なもの

- Linux(推奨)またはmacOSマシン。WindowsはWSL2を推奨
- git / python3.11+
- Coding Agent CLIのうち1つ: **Claude Code**(優先)または Codex
  - 既にインストール・ログイン済みであること(Hermes credentialとは完全分離)
- SSH server(鍵認証有効)

## 1. 専用アカウントとWorker Root

```bash
sudo useradd -m -s /bin/bash hermes-worker
sudo -u hermes-worker mkdir -p ~hermes-worker/hermes-worker/{repos,worktrees,jobs,logs,bin,config}
```

Worker Rootは `~hermes-worker/hermes-worker/`。Coding Agentが触るのはここだけ。

## 2. workerctl 設置

`workerctl_server.py` を `~hermes-worker/hermes-worker/bin/workerctl` として
置き、実行権限を付与。**F.1.1-Pi.2 からは canonical secret pattern module
`security_patterns.py` も同じ bin/ にコピーする必要がある**
(pattern は単一ソースで管理、drift は regression test で検出):

```bash
sudo -u hermes-worker cp workerctl_server.py ~hermes-worker/hermes-worker/bin/workerctl
sudo -u hermes-worker cp ../../security_patterns.py ~hermes-worker/hermes-worker/bin/security_patterns.py
sudo -u hermes-worker chmod +x ~hermes-worker/hermes-worker/bin/workerctl
```

- `security_patterns.py` が workerctl と同じディレクトリに無いと起動しない
- 内部コマンド (`_run_job`) は SSH 表面からは呼べない
  (SSH_ORIGINAL_COMMAND 存在時に reject)。detached runner の内部呼び出し
  のみが使用する(M4)

## 3. 設定(config/worker.json)

```json
{
  "agent": {
    "name": "claude-code",
    "command_template": "claude -p \"$(cat {prompt_file})\" --allowedTools \"Read\" \"Edit\" \"Glob\" \"Grep\" \"TodoWrite\" \"Bash(git add:*)\" \"Bash(git commit:*)\" \"Bash(git diff:*)\" \"Bash(git status:*)\" \"Bash(python3 -m unittest:*)\" --permission-mode acceptEdits"
  },
  "repositories": {
    "coding-worker-smoke-test": {
      "path": "~/hermes-worker/repos/coding-worker-smoke-test",
      "test_commands": ["python3 -m unittest discover -s tests -v"],
      "allowed_paths": ["pkg/", "tests/", "README.md"]
    }
  }
}
```

- **`--dangerously-skip-permissions` 等のbypass flagは使用しない**(Section 18/19)
- approvalで停止する場合は、allowedTools/permission-modeで必要最小限だけ許可
- repository登録は人間が編集する(Hermesからは不可能)

Codexの場合: `codex exec --sandbox workspace-write -` 等の
**現在の公式sandbox/approvalモード**を確認して設定すること(yolo系option禁止)。

## 4. restricted SSH(Hermes Pi側の鍵をWorkerへ)

authorized_keys は **必ず forced command + 制限オプション付き**:

```text
command="/home/hermes-worker/hermes-worker/bin/workerctl request",restrict ssh-ed25519 AAAA... hermes-supervisor
```

- `restrict` = port/agent/X11 forwarding等の全面禁止
- SSH接続は `workerctl` subcommandのみ。generic shellは提供されない
- テンプレート: `authorized_keys.template` 参照

## 5. Hermes側設定(worker情報入力後)

Hermes Pi上で `jobs/remote_worker.json` を作成(人間が編集):

```json
{
  "host": "<worker host/ip>",
  "port": 22,
  "username": "hermes-worker",
  "ssh_key": "/opt/data/jobs/keys/worker_ed25519"
}
```

鍵生成(Hermes Pi上、管理鍵とは別物):

```bash
ssh-keygen -t ed25519 -f /srv/hermes/hermes-data/jobs/keys/worker_ed25519 -N ""
```

公開鍵をWorkerのauthorized_keysへ(手動、制限オプション付きで)。

## 6. 検証

Worker側(hermes-workerアカウントで):

```bash
~hermes-worker/hermes-worker/bin/workerctl selftest
```

- 個人Hermesデータ不在 / path traversal拒否 / symlink escape拒否 を検査

Hermes側(鍵設定後):

```bash
bash jobs/worker/validate_ssh.sh
```

- 正常request通過 / whoami・sh・bash・任意command拒否 / forwarding拒否 を実測

## 7. (推奨) systemd による kernel-level閉じ込め

アカウント分離に加え、Job実行を `ReadWritePaths=worker root` の一時unitで
包むとworktree外への書込がkernelレベルで失敗する(Section 26の厳密解)。
`install_worker.sh` が雛形を出力する。
