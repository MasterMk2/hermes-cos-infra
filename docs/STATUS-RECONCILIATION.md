# Hermes implementation / runtime reconciliation

## 現行コードと検証（2026-10-05）

GitHub の実装基準は [`0445c7c8`](https://github.com/MasterMk2/hermes-cos-infra/commit/0445c7c8b7da7b52553eccf3f156b0882e6a8438)。
以下はコードの main 反映と合成テストの結果であり、現在の Pi への配備・実配信は未確認。

- [PR #3](https://github.com/MasterMk2/hermes-cos-infra/pull/3): proactive 通知、Attention、Planning の送信結果不明を永続化し、自動再送・誤成功を抑止。既存の状態管理を拡張した
- [PR #4](https://github.com/MasterMk2/hermes-cos-infra/pull/4): 通常 Discord 返信の本文・宛先・chunk を固定し、確認済み先頭部分と確実な未送信 tail のみを継続。UNKNOWN があれば返信全体を保留。Morning Brief の予約中・本文不一致を配信成功にしない
- [PR #5](https://github.com/MasterMk2/hermes-cos-infra/pull/5): 通常 LINE の固定 batch（最大5件、UTF-16上限）と reply/push mode を保存。reply 開始後に結果不明なら自動 push しない。409 は有効期間内の固定 push intent と provider 受理IDが確認できる場合だけ受理として扱う
- [main の offline CI](https://github.com/MasterMk2/hermes-cos-infra/actions/runs/37247078869): Ubuntu 24.04、Python 3.12.14 / 3.13.15 の各環境で Gateway 344 + Attention 158 + Planning 607 = **1,109 passed**、別に **1,188 subtests passed / 3 skipped**。3件は外部 private denylist がなく未検証。全サービスの回帰・実機・実サービスの検証ではない
- 送信開始後の曖昧な失敗やクラッシュ窓は `UNKNOWN` のまま保持する。手動 reconcile / UI は未実装。`SENT` は provider の受理を表し、端末到達・既読を保証しない。SDK 内部 retry を含む exactly-once は保証しない
- 旧版へ戻す前に状態DB互換を確認する。新しい UNKNOWN を旧版が再送可能へ読み替える rollback は安全ではない。実機DBの変更・移行は今回行っていない

再現方法と制約は [OFFLINE-TESTS.md](OFFLINE-TESTS.md)、
[proactive](RESTART-SAFETY.md)、[Discord](REPLY-RESTART-SAFETY.md)、
[LINE](LINE-RESTART-SAFETY.md) を参照。
既存 events / commitments / jobs / Attention / Planning は存在する。
出典・時刻・確信度・訂正を統一した記憶、Reflection、成長ログの完成証明にはならない。
9月10日記録の後続ローカル変更回収、自然配信、I/O・起動順序の運用残件は引き続き未確認。

## 初回照合の記録（2026-10-04）

照合日: 2026-10-04。対象は GitHub の実装と文書、隔離クラウド上で再実行した
合成テスト。初回照合時には Pi への接続、認証値の読み取り、本番操作、
再配備、再起動、送信、push / merge は行っていない。

初回照合文書の GitHub 反映は、再確認した基準コミットを親とする文書のみの
draft PR として扱った。この文書PRは実装・本番構成を変更しない。送信対象の全文を確認し、
履歴中の個人識別子・固有の接続情報をプレースホルダへ置換した。
これは送信する5文書の確認であり、全履歴・完全なtracked tree・
外部private denylistの検査を代替しない。

## 1. 初回照合で三つの状態を分ける

| 証拠の種類 | 確認できたこと | 確認できないこと |
|---|---|---|
| GitHub の実体 | infra main は `640d9db1044a68612e2b9fdf0204a68ebef9ea0f`（9月8日）。I.2 の planning 実装と投入時修正を含む | 現在の Pi が同一 revision を実行しているか |
| 9月10日までの作業記録 | pi-setup `PROGRESS.md` は I.2 完了、担当者・広告除外、プレミアムCLI経路の本番適用を報告 | その後続ローカル変更の現在の内容、現在の本番健全性 |
| 今回の再実行 | 合成DB・偽の配信transportで planning 595 PASS / 7 SKIP、1025 subtests PASS | 現行本番データの品質、自然な実配信、全回帰・全履歴privacy検査 |

正式な作業記録上の到達点を H.3.2 / I.1 へ巻き戻さない。
一方、9月10日の記録を「現在も稼働中」という観測に置き換えない。
I.2 を新規実装し直すことや I.3 の開始は今回の残件ではない。

## 2. GitHub で確認した I.2 実装

- `src/planning/planner.py`: canonical commitments の決定論的な分類・順位付け
- `src/planning/renderer.py`: 日本語テンプレートと任意のLLM経路のgrounding検証
- `src/planning/mcp_planning.py` / `src/events/event_mcp.py`: 計画・ブリーフの読み取り専用MCP
- `src/planning/brief_service.py` / `brief_state.py` / `delivery.py`: scheduled run、状態管理、Gatewayへの配信要求と重複抑止
- `src/planning/systemd/` / `install-root.sh`: 専用identity・timerの配備用コード
- `src/antigravity-bridge/config/mcp-allowlist-i2.*`: 既存allowlistへの2ツール追加手順・snippet

`640d9db` の二つの修正は既にコードへ入っている。

1. installer、CLI、service、status script は
   `/srv/hermes/planning/config.yaml` を参照する。古い
   `config/config.yaml` を正として案内しない。
2. Discord collector 側のstoreが到達不能などで読めない場合は、
   既存の `events.db` の `meta.discord_last_run` をfreshnessの根拠にできる。
   collectorが正常に読める場合はそのheartbeatを優先する。fallback先が
   設定されていない場合は元のエラー種別を保持する。権限を拡張する修正ではない。

初回照合の既存テストでこれらの形を確認した。その時点では追加の挙動変更が必要な
再現不具合は確認していなかった。その後に再現・修正した送信境界は上の現行コード欄を参照。この文書PRの変更は文書のみ。

## 3. リモートと後続ローカル作業の差

[pi-setup の9月10日記録](
https://github.com/MasterMk2/hermes-pi-setup/blob/f3b378eb6a39e721ef6e1d585ec7e76e35b313b6/PROGRESS.md)
は、内側のローカル main `cf7a647` と `fix/owner-task-routing` の未コミット変更を記録している。

2026-10-04 の GitHub 照合では:

- main は `640d9db`、ブランチ一覧で得られたのは main のみ
- `cf7a647` のcommit取得は「No commit found for SHA」
- `docs/20-phase-i2-daily-planning-morning-brief.md`、
  `OWNER_FILTER_RESUME.md`、担当者判定・プレミアムCLI経路の後続資料は、
  参照した infra main のtreeに存在しない

ローカル実体が消えたという結論にはならない。リモートの古いコードを
本番に上書きしたり、記録から後続コードを推測して作り直したりしない。
作業環境が利用可能になったら、既存のworktreeと変更を先に回収・比較する。
公開前の秘密情報・個人情報検査は別途必要。

## 4. テスト結果の出所

### 初回照合で再実行したもの（履歴）

- 基準: infra `640d9db1044a68612e2b9fdf0204a68ebef9ea0f`
- 環境: Linux cloud workspace、Python 3.12.14、pytest 9.1.1
- 取得方法: GitHub connector から SHA 固定で必要なソース・テストを取得。
  実ファイルの Git blob SHA を照合。完全な Git clone ではない
- コマンド: `python -m pytest tests-planning -q -ra`
- 結果: **595 passed, 7 skipped, 1025 subtests passed**
- データ: 隔離された合成fixtures。本番DBを取得せず、実Gatewayへの送信なし

7件のSKIPをPASS扱いにしない:

- 2件: pre-I.2 baselineとsanctioned privacy変更を検証するGit履歴がない
- 2件: Git追跡ファイル一覧に基づくprivacy scan。必要ファイルのみを
  materializeしたsnapshotのため完全なtracked treeがない
- 3件: リポジトリ外のprivate privacy denylistがこの環境にない

完全な履歴検査・tracked-tree検査・denylist照合は今回未実行。
そのため、このテスト結果だけを根拠に公開可能とは判断しない。
Gateway、Pi用環境、Hermesコンテナ、他の全回帰スイートも今回の対象外。

### 過去の記録

- [9月8日のproduction fix commit](
  https://github.com/MasterMk2/hermes-cos-infra/commit/640d9db1044a68612e2b9fdf0204a68ebef9ea0f)
  は planning 602 PASS、3日分のPi dry-runで全5source freshと報告。
  現在のPiの再検証結果ではない
- pi-setup の9月10日記録は、I.2回帰 **1,134 / 1,134 PASS** と
  Gate 121–127 PASS、Gate 124の根拠に基づく訂正を報告。
  対応する最終レポートの実体は今回取得できていないため、この数を
  現在のGitHub snapshotの測定値として引用しない
- I.1-Sの既存資料には `RESUME.md` の510件（GitHub26件）と
  `docs/19-...` の509件（GitHub25件）の差がある。
  実行対象・時点の完全なログを回収するまで、どちらかを現在の総数に採用しない。
  履歴を保つため当時の数字は書き換えない

初回照合の GitHub Actions 問い合わせでは、基準 `640d9db` の exact SHA に対する workflow run は0件だった。上記ローカルテストをCI成功と表現しない。

## 5. 再開後の完了条件

1. **ローカル変更の回収・Git整理**
   9月10日記録のworktree、base SHA、未コミット差分、新規ファイルを確認。
   秘密情報を含めずレビューできる単位へ整理し、必要な回帰を実行する。
   ローカル保存・GitHub反映・本番配備を区別する
2. **自然な朝の実配信**
   修正後の通常配信で、本人以外の作業・広告の混入、本文と根拠、
   配信成否、Attentionとの内容重複を観測する。
   非送信dry-runや合成成功で代替しない
3. **I/O停止と起動順序**
   記録上は復旧確認・依存設定の適用済みだが、長期再発と実起動は未解決。
   自然な起動か承認済み保守で証拠を確認する。確認だけの再起動はしない

現在のPi実測は未実施。上記1〜3に必要な作業環境・運用証拠が得られるまで
未確認のまま残す。この文書はアクセス拡張、認証変更、再配備、再起動、
外部送信、push、merge、I.3開始の承認ではない。
