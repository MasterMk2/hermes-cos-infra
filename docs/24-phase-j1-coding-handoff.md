# Phase J.1 — Discord Front → Coding Worker handoff

## 直した問題

2026-09-10 13:00 JST、オーナーが Discord で FastRawPhotoView の GUI 案を承認し、
「完成まで走らせて」と指示した。Front は「Discord からは実装できません。CLI を
開いてください」と答え、ジョブは 1 件も作られなかった（既存 57 件はすべて 9/3
以前）。

原因は 3 つあり、どれか 1 つを直しても通らない。

1. `gw_canonical.ENVELOPE_PREAMBLE` が「このチャネルでは新規コーディングの開始は
   できない」と明言していた。Front の返答はこの指示に忠実だった。
2. Front のモデルバックエンドは Antigravity ブリッジで、ツール要求を自身の
   read-only MCP ループに委譲し、ネイティブの function call を返さない
   (`bridge_server.py:397-422`)。Hermes 側のツールセットを変えても書き込みは
   できない。
3. `platform_toolsets.api_server` は `jobs_readonly` のみ。ここに
   `jobs_supervisor` を足せば、Gmail や Discord から取り込んだテキストが
   混ざったコンテキストのモデルに `job_create` を渡すことになる。

## 方針

**権限判断はゲートウェイが行い、書き込み経路はモデルを通らない。**

ゲートウェイは偽装不可能なオーナー識別を持つ唯一のコンポーネントである
(`gw_canonical.build_envelope` はメッセージ本文由来の引数を一切受け取らない)。
そこで、コーディング要求の判定を決定的なコードで行い、型付きブローカー経由で
Worker に渡す。Hermes の設定権限は一切広げていない。

```
Discord DM ─▶ channel-gateway ─┬─▶ (大半) Hermes /v1/runs   … 従来どおり
                               │       planning / GitHub / health / premium
                               └─▶ gw_coding.classify   … 権限判定（純粋関数）
                                       │
                                       ▼
                                   loopback HTTP + Bearer
                                       │
                                 coding-handoff broker
                                       │
                                 jobs_db + supervisor.dispatch_job
                                       │
                                 coding bridge ─▶ 隔離コンテナ ─▶ 独立レビュー
```

## 権限モデル

1. オーナー本人の DM 以外はルートに入らない。判定は envelope の subject に対して
   行い、本文は見ない。
2. プロジェクト名を挙げても何も起きない。**提案**（スコープ全文と確認コード）が
   提示されるだけ。
3. 承認と認められるのは、**未失効・未消費の提案があり**、本文の**節**が限定された
   肯定文法に**完全一致**し、否定・条件・疑問・引用のいずれの拒否条件にも当たらない
   場合のみ。
4. 「いいね」だけは承認ではない。明示的な言い方を促す返答をする。
5. 確認コードは拒否条件を上書きしない。

### 部分一致をやめた理由（重要）

初版は `_AUTHORIZE_RE.search()` による部分一致だった。独立レビューで次が再現した。

| 入力 | 初版 | 現在 |
|---|---|---|
| 実行してはいけません | AUTHORIZE | needs_explicit (negated) |
| 進捗の確認をお願いします | AUTHORIZE | status |
| 「実行して」という文言はどういう意味？ | AUTHORIZE | pass_through |

日本語は動詞語幹の**後ろ**で否定する。語幹に一致した時点で受理する実装は、
否定文を許可として読む。現在は「節全体が命令であること」を要求し、否定・条件・
疑問・引用を先に拒否する。ステータス判定は権限判定より**前**に行う
（`お願いします` を開始指示と読まないため）。

## 仕様のバージョン管理

仕様はプロジェクトごとのディレクトリ内の連番ファイル（`0001-*.md`）。運用者が
新しいリビジョンを置くだけで、Discord での合意内容が承認対象になる。

提案は**リビジョン番号とその内容の sha256**、さらにスコープ全体（リポジトリ /
エージェント / テストコマンド / 書き込み範囲 / 受け入れ条件 / ツールチェーン /
試行回数）を含む `scope_digest` に束縛される。承認時にダイジェストが一致しなければ
実行せず、再確認を求める。仕様の差し替えは起こり得ない。

**J.1 では、チャットのテキストから仕様リビジョンを作る機能は実装しない。**
Worker に渡るのは運用者がレビューした仕様ファイルの中身だけで、Broker は会話履歴・
システムプロンプト・Gateway の資格情報を読み出さない。

Gateway は「どう？」「行けた感じ？」「もっかい試してみて」のような短い継続発言を、
同じ会話・プラットフォーム・送信者の認証済み入信履歴から補う。直前24時間・最大32件に
限定し、現在の発言より後に届いた内容、引用内の名前、複数プロジェクトの曖昧な言及は
対象の確定に使わない。これにより、導入前からの会話や、準備不足で提案を作れなかった
会話でも対象プロジェクトを引き継ぐ。履歴から復元するのは読み取り・新しい提案の対象だけで、
過去の承認を実行権限に転用しない。別の対象へ切り替える要求は、未確定の承認がなければ
古い提案を取り下げる。まだジョブがない対象の状況確認では、実際の準備不足を照会して返す。

## 冪等性

チャットのトランスポートは exactly-once を保証しない。3 段構えで 1 認可 1 ジョブを
保証する。

| 仕組み | 塞ぐ穴 |
|---|---|
| `authorization_id` 主キー | 同時到着した 2 つが両方 writer になること |
| **決定的 job_id を先に記録** | 作成後・記録前のクラッシュで 2 件目を作ること |
| `claim_epoch` | 遅れて復帰した旧 writer が復旧結果を上書きすること |

`job_id` は認可・プロジェクト・スコープダイジェストから導出し、ジョブが存在する
**前**に台帳へ書く。作成は canonical な `jobs.job_id` 主キーに対する
create-if-absent。復旧は「再実行」ではなく「前回が実際に何をしたかを調べて終わらせる」。

リプレイは preflight より**先**に解決する。既に走っているジョブの重複要求に対して
「ワーカーが混んでいるので作成しませんでした」と答えるのは、事実に反する。

## 名前空間（実測）

supervisor のリポジトリ登録はコンテナ内パス (`/opt/data/...`)、bridge の登録は
ホストパス。ブローカーはホストで動くため、

- ディスク上の事実（クリーンなベースの有無）は **bridge の登録**から見る
- supervisor の登録は**許可リストと承認済みテストコマンド・書き込み範囲**としてのみ使う
- 既存のどちらのパスも書き換えない

ホストからのディスパッチが安全なのは実測に基づく: `supervisor.py:1163` は
`local-fixture` 以外のアダプタで `repo_path = None` にするため、`pi-sandbox` の
ジョブは登録パスを一切解決しない。git 操作はすべて bridge 自身の worktree で起きる。

`transport.bridge_sock()` にはホスト用の環境変数
`HERMES_CODING_BRIDGE_SOCK` を追加した（未設定なら従来動作、既存コンテナ利用者に
影響なし）。

## ツールチェーンの正直さ

Worker イメージで実測できたのは `python3` と `git` のみ。ネットワークも無い。
FastRawPhotoView は合意どおり **Tauri + Rust** のままで、`required_toolchains` に
`rustc, cargo, node, npm` を宣言している。したがって preflight は
`worker_toolchain_missing` を返して**ジョブを作らない**。

これは仕様を弱めないための設計である。ビルドできない環境に合わせて製品を
stdlib Python に置き換えることはしない。プロビジョニングが前提条件として残る。

経路そのものの検証には別プロジェクト `handoff-protocol-check` を用意した
（既存の `coding-worker-smoke-test` を使う python3 のみのタスク）。**これが通っても
FastRawPhotoView のビルド可否については何も言えない。** 経路の成否とアプリの
ビルド可否は別々に報告する。

## 変更点

| ファイル | 変更 |
|---|---|
| `src/channel-gateway/app/gw_coding.py` | 新規。意図判定・権限文法・日本語描画（純粋） |
| `src/channel-gateway/app/gw_handoff.py` | 新規。ルート統合、遅延ブローカー参照 |
| `src/channel-gateway/app/gw_handoff_client.py` | 新規。loopback 限定クライアント |
| `gw_canonical.py` | capability 文をルート有効時のみ差し替え |
| `gw_config.py` | `coding_handoff` 節（既定 false）と資格情報名 |
| `gw_schema.py` / `gw_store.py` | `coding_handoff_proposals` 表（追加のみ、v3） |
| `gw_hermes.py` / `gw_service.py` | capability フラグ、`process()` に 1 箇所のフック |
| `src/coding-handoff/**` | 新規。ブローカー、登録簿、仕様、systemd、デプロイ一式 |
| `src/jobs/jobs_db.py` | `create_job(job_id=...)` 追加（省略時は従来どおり） |
| `src/jobs/transport.py` | ソケットパスの環境変数上書き（未設定なら従来どおり） |

無効時（既定）は `process()` の経路が従来と同一である。

## テスト

| スイート | 件数 | 内容 |
|---|---|---|
| `tests-gateway` | 330 | 既存 251 + 本フェーズ。敵対的権限、TTL、文脈維持、可用性 |
| `tests-handoff` | 167 | 登録簿、仕様束縛、台帳、実 SQLite 結合、クラッシュ窓、同時実行、パッケージ、jobs 変換、オンボーディング |

```
PYTHONPATH=src/channel-gateway/app:tests-gateway python3 -m unittest discover -s tests-gateway
PYTHONPATH=src/coding-handoff/app:src/jobs:src:tests-handoff python3 -m unittest discover -s tests-handoff
```

pytest は使わない（本番 venv にも WSL にも入っていない）。

## 導入

ブリッジの位置はホストごとに違うため、**既定値を持たせず明示引数で受け取る**。

```sh
# 1. 何も変えない検証（非 root でも可）
src/coding-handoff/deploy/preflight.sh \
    --bridge-config <BRIDGE_CONFIG> --bridge-socket <BRIDGE_SOCK>

# 2. 導入（既定は不活性: ゲートウェイ設定に節を足さず、dispatch も無効）
sudo src/coding-handoff/deploy/install.sh \
    --bridge-config <BRIDGE_CONFIG> --bridge-socket <BRIDGE_SOCK>

# 3. 経路とディスパッチを有効化
sudo src/coding-handoff/deploy/install.sh \
    --bridge-config <BRIDGE_CONFIG> --bridge-socket <BRIDGE_SOCK> \
    --enable-route --enable-dispatch

# 4. 取り消し
sudo src/coding-handoff/deploy/rollback.sh /var/backups/hermes-j1-<stamp>
```

- `manifest.json` が全て。列挙されたファイルだけを入れ、各対象が記録済み
  ベースラインと一致しなければ**何も書かずに停止**する。
  現パッケージの Gateway 基準は、実配備済みの J.1 `9fcd1f8` と同一の9ファイル。
  新規導入用の旧基準ではなく、この版からの更新として検査する。
- Antigravity の premium リリース 3 ファイルはハッシュで固定し、触らない。
- **サービス ID は作らず、導出する。** jobs ディレクトリの所有者をそのまま使う。
  新規ユーザだと 0700 のディレクトリと 0660 のソケットに ACL が要り、ブリッジが
  ソケットを作り直した瞬間に無言で失効する。unit は雛形から実測値で描画され、
  `ProtectHome=tmpfs` + `BindPaths` で必要な場所だけを見せる。
- **既存のDBと実行時ディレクトリの権限も検査する。** 親の `jobs/` が書けても、
  コンテナが作成した `jobs.db` や `logs/` が root 所有ならジョブ作成・起動は失敗する。
  root での preflight は、サービス ID による DB・存在する WAL/SHM・`logs/`・
  `artifacts/`・`prompts/` の読み書きと、ディレクトリの通過権限を確認する。
  不足時は導入前に停止する。運用側で元の ACL を `getfacl -p` で非公開バックアップし、
  対象に限定した名前付き ACL と、将来作成される SQLite sidecar・実行ログ用の
  default ACL を設定する。DB の所有者変更やサービスの root 実行は必要ない。
  復元には保存した ACL と `setfacl --restore` を使う。ACL の調整はインストーラーが
  自動で広げる処理には含めない。
- **jobs レイヤは置換せず、その場で変換する。** 本番の 2 ファイルは committed と
  論理的に同一だがバイト一致ではない（CRLF、および transport.py が持つ解決済みの
  ブリッジパス）。置換すればこのホストに存在しないパスを書き戻してしまう。
  変換はピン留めしたハッシュを確認し、アンカー領域だけを書き換え、それ以外が
  バイト一致であることを検証する。`--enable-dispatch` はこの変換の成功を要求する。
- Python を含む処理は `deploy_tool.py` にあり、シェルのヒアドキュメントは使わない。
- 差し替えは同ディレクトリ内の一時ファイル経由で mode / 所有者を維持し、
  置換前に `/var/backups/hermes-j1-<stamp>/` へ退避する。
- **health が失敗したらインストーラ自身がロールバックする**（`--no-auto-rollback`
  で抑止可能）。
- 資格情報は生成してファイルに書くだけで、表示も環境変数化もしない。
  ロールバックは台帳と資格情報を残す（消すと再導入時に既存ジョブの重複を作るため）。

## プロジェクト登録（FastRawPhotoView）

登録は 2 つの許可リストと 1 つのクリーンなベースが揃って初めて成立する。手作業で
2 ファイルを触ると必ずずれるので、1 操作にした。

```sh
D=src/coding-handoff/deploy/onboard_project.py
python3 $D print-template > /tmp/frpv.json     # 編集: ブリッジ側の実パスを入れる
python3 $D plan      --config /tmp/frpv.json   # 何も変えずに差分を表示
python3 $D bootstrap --config /tmp/frpv.json   # ローカルに最初のコミットを作る
sudo python3 $D apply --config /tmp/frpv.json  # 2 つの許可リストを原子的に更新
sudo python3 $D rollback --config /tmp/frpv.json --backup <dir>
```

- 両方の登録は**1 つの記述から**生成するので、テストコマンドと書き込み範囲が
  ずれない。パスだけは意図的に違う（supervisor はコンテナ内、bridge はホスト）。
- 既存 5 件を含め、元からあるエントリは全て保持する。書き込みは一時ファイル →
  読み直して検証 → rename の順。
- bootstrap は **ローカルのみ**。README と承認済み仕様だけを、中立な author
  メタデータでコミットする。リモート作成も push も clone も fetch も行わない
  （`GIT_TERMINAL_PROMPT=0`）。アプリの代替コードも LICENSE の選択も置かない。
- リモートは空のままで、公開するかどうかは人間の判断として残る。

## 再起動とランナーのライフタイム（実測に基づく制約）

`supervisor._spawn_runner` (`src/jobs/supervisor.py:882`) は
`Popen(start_new_session=True)` を使う。これは**セッションを分けるだけで、cgroup は
親のまま**である。したがってブローカーの systemd unit が再起動されると、そこから
起動された supervisor runner は既定の `KillMode=control-group` により停止する。
一方 bridge が起動したワーカーコンテナは別プロセスなので走り続ける。

**KillMode は変更していない。** unit の停止方法を緩めるのは、インストールの副作用で
決めてよい話ではない。代わりに:

- `preflight.sh` と `install.sh` は bridge に `worker_status` を投げ、稼働中の
  ジョブ/レビューがあれば**インストールを拒否**する。完了を待って再実行する。
- 既存の復旧契約は確認済みで、この状況は回復可能である:
  `supervisor.reconcile()` は runner が消えた RUNNING/DISPATCHING を
  `AGENT_STOPPED`（再開可能）へ戻し、bridge の `a_job_start` は生きている
  コンテナを `readopted` として引き継ぐ。ただし「回復できる」ことと
  「中断させない」ことは別なので、避けられる中断は避ける。
- ブローカー自身は再起動で失う状態を持たない（台帳はディスク上）。

## 未解決 / 前提条件

- FastRawPhotoView リポジトリは空（HEAD 無し）。最初のコミットが必要。
- Worker イメージに Rust / Node が無い。プロビジョニングは別レビュー。
- `sudo` は現在使えない。ルート有効化には一時的な root 権限が要る。
- ブローカーからのディスパッチは runner をホスト側で起動する。本番での実地確認が必要。
- チャットからのジョブ取り消しは J.1 では提供しない（運用者の CLI に残す）。
