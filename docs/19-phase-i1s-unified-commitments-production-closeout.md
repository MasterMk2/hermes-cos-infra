# Phase I.1-S — Unified Commitments Production Closeout

> 状態: **COMPLETED (2026-09-06)**。
> 合成テストデータ完全パージ・バックフィル冪等性実証 (737件完全一致)・
> 最小特権ファイルシステム境界確立 (`HERMES_HOME_MODE=0710` + `<PI_USER>:hermesreconcile` 監査/修正)・
> 実機 Discord / LINE フロントエンド E2E 完遂 (手動 `-t` 不要・Antigravity プライマリ自律実行)・
> Attention Engine 洪水平抑・ノイズ抑止・新規 SELF シグナル実証・全7テストスイート (509/509 PASS) 完全通過。

---

## 1. Executive Summary

Phase I.1-S では、先行フェーズ I.1 で構築された Unified Commitments & Agenda Layer に対し、本番環境 (Production Pi: `<PI_HOSTNAME>`) における 4 つの Completion Gate を実機検証・クローズアウトしました。

1. **Gate A: Synthetic Production Data Cleanup**
   - 開発・E2Eテスト時の合成コミットメント 4 件 (`cmt_e2e_discord_req_b`, `cmt_e2e_gmail_req_b`, `cmt_ambig_1`, `cmt_ambig_2`) および合成エビデンス・履歴行を完全削除。
   - 実コミットメント `cmt_bf343048b742` に混入していたモックエビデンス (`gmail:h2_e2e_mock_exam_confirm`) を安全にパージし、正常な `source_count = 1` に復旧。
   - バックフィル再実行による冪等性を実証: 0件追加、0件重複、737件の整合性を完全維持。
2. **Gate B: Least-Privilege Filesystem Boundary**
   - Docker Compose 設定 `HERMES_HOME_MODE=0755` を監査し、`HERMES_HOME_MODE=0710` および `HERMES_GID=980` (`hermesreconcile`) へ改修。
    - `/srv/hermes/hermes-data` 直下の権限を `0710 <PI_USER>:hermesreconcile` に厳格化。
   - `hermesantigravity` ユーザーによるアクセス検証: `events.db`, `calendar`, `gmail`, `discord`, `line` の各 index.db は正常読み取り可能、一方 `.env`, `auth.json`, `config.yaml`, `docker.sock` は `Permission denied` (完全遮断)。
   - 任意ローカルユーザー (`nobody`) によるアクセス拒絶 (`ALL DENIED`) を実証。
   - コンテナ再作成 (`docker compose up -d`) 後の権限永続性を実証。
3. **Gate C: Real Discord / LINE Frontend E2E**
   - 手動 `-t event_layer` や `-t internal` の injection を一切使用せず、Channel Gateway 経由での自然言語クエリを自動実証。
   - Discord クエリ:
     - `今日やること何？`: 正常応答 (315文字、カレンダー予定＋優先タスク)
     - `今、返事待ちって何がある？`: 正常応答 (544文字、WAITING コミットメント整理)
     - `期限過ぎてるものある？`: 正常応答 (553文字、OVERDUE コミットメント整理)
   - LINE クエリ:
     - `今日やること何？`: 署名付き Webhook 経由で Gateway が受領・処理し、プッシュ配信で正常応答 (422文字)。
   - 全クエリにおいて Antigravity Primary (`gemini-3.8-flash-medium`) が自律的に `agenda_get` / `event_*` MCP を呼び出し、フォールバックなしで完遂。
4. **Gate D: Attention Integration & Flood Validation**
   - `att_events.py` が Commitment Layer の `DISMISSED` 状態を参照し、360件の定常ノイズを初期段階で完全除外することを確認。
   - 737件の過去コミットメントによる Proactive 通知の氾濫 (Flood) が 0 件に平抑されていることを実機ログ (`journalctl`) で実証。
   - 新規 `SELF + ACTIONABLE + DUE_SOON/OVERDUE` コミットメントに対するシグナル発行および期限超過遷移を単体テスト (`test_commitments_attention.py`) で実証。

---

## 2. Regression Test Results

全 7 スイート 509 件の回帰テストがすべて PASS (100%):

| テストスイート | パス / 対象 | 結果 |
| :--- | :--- | :--- |
| **tests-antigravity** | `hermes-cos-infra/tests-antigravity` | **19 / 19 PASS** |
| **tests-github** | `/opt/data/tests-github` (container on Pi) | **25 / 25 PASS** |
| **tests-gmail** | `hermes-cos-infra/tests-gmail` | **31 / 31 PASS** |
| **tests-calendar** | `hermes-cos-infra/tests-calendar` | **15 / 15 PASS** |
| **tests-attention** | `hermes-cos-infra/tests-attention` | **155 / 155 PASS** |
| **tests-commitments** | `hermes-cos-infra/tests-commitments` | **13 / 13 PASS** |
| **tests-gateway** | `/home/<PI_USER>/g1-tests/tests-gateway` (Pi) | **251 / 251 PASS** |
| **合計** | | **509 / 509 PASS** |

---

## 3. Deployment & Environment Artifacts

- **Pi Host (`<PI_HOSTNAME>`)**:
  - `/opt/hermes-stack/docker-compose.yaml`: `HERMES_GID=980`, `HERMES_HOME_MODE=0710`
  - `/srv/hermes/hermes-data`: `0710 <PI_USER>:hermesreconcile`
  - `/srv/hermes/hermes-data/events`: `0750 <PI_USER>:hermesreconcile`
  - `/srv/hermes/hermes-data/events/events.db`: `0640 <PI_USER>:hermesreconcile`
  - `/srv/hermes/hermes-data/knowledge-index`: `0750 <PI_USER>:hermesreconcile`
  - `/srv/hermes/hermes-data/config.yaml`: `0600 <PI_USER>:<PI_USER>`
- **Repositories**:
  - `HermesSetup`: `docker-compose.yaml` (0710 mode, GID 980)
  - `hermes-cos-infra`: `tests-attention/test_commitments_attention.py`, `docs/19-phase-i1s-unified-commitments-production-closeout.md`
