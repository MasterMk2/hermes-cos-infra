# Phase H.2 — Gmail Intelligence & Commitment Ingestion

> 状態: **COMPLETED (2026-09-05)**。
> 実装・Pi 配備・read-only OAuth・Pub/Sub pull・実 Gmail 差分同期・
> イベント抽出ブリッジ・照合 DB・Hermes MCP・Attention 健全性プローブ・
> および既存サービスの全回帰テストを実測完了。

## Architecture

``text
Google Cloud Pub/Sub
  └─ projects/<PROJECT>/subscriptions/hermes-gmail-events-pull
             ↓ pull (credential boundary)
hermes-gmail.service (User=hermesgmail, scopes: gmail.readonly)
  ├─ canonical SQLite state (/srv/hermes/gmail/state/state.db)
  ├─ bounded baseline & incremental history sync
  ├─ content-change-gated published search index
  │    └─ /srv/hermes/gmail/index/index.db (0440, group: hermesreconcile)
  └─ Hermes container (read-only mount: /opt/gmail)
       └─ read-only Gmail MCP (gmail_search, gmail_get, gmail_thread, gmail_status)

hermes-gmail-event-ingest.timer (5分間隔)
  └─ hermes-gmail-event-ingest.service
       ├─ Event Layer 抽出 (/srv/hermes/hermes-data/events/events.db)
       └─ 照合用最小プロジェクション (/srv/hermes/gmail-event/reconcile/calendar-reconcile.db)
            └─ hermes-calendar.service が参照 (秘密情報・本文非露出)

attention-engine.service (User=<PI_USER>)
  ├─ probe_gmail (status.json / index.db 健全性検査)
  └─ Event / Calendar / Gmail の統合通知判定
``

## Security Boundaries & Isolation

1. **専用アカウント・最小権限**:
   - hermesgmail は専用の nologin システムアカウント（docker, sudo, oot 等の特権グループなし）。
   - OAuth 資格情報（client-id, client-secret, refresh-token, pubsub-service-account.json）は /etc/hermes-secrets/google-gmail/ に 0400 root:root で配置され、systemd の LoadCredential を介してのみプロセスに渡る。
2. **インデックス公開面の厳格化**:
   - /srv/hermes/gmail/index/ は 2750 hermesgmail:hermesreconcile。
   - index.db および status.json は 0440 hermesgmail:hermesreconcile。
   - 差分更新版の gm_index.py では、index.db は index-a.db / index-b.db（どちらも 0440）の一方を指すシンボリックリンク。変更は読み手が使っていない側へ差分で書き、書き終えてからリンクを切り替える。読み手は書き込みのロックを待たない。全件の作り直しは、差分で追えないときと、夜 4 時台の圧縮だけ。
   - 一般ユーザーや無権限プロセスからは完全遮断（PermissionError）。
   - Hermes Docker コンテナは /opt/gmail に read-only マウントされ、書き込み試行は OSError: [Errno 30] Read-only file system で拒絶される。
3. **カレンダー照合境界**:
   - Calendar サービスは econcile/calendar-reconcile.db を通じて照合メタデータ（vent_id, summary, due_at, status, source_type）のみを読み取る。
   - Gmail のメール本文、ヘッダー、添付ファイル、OAuth 資格情報へのアクセス経路は一切存在しない。
4. **外部入力としてのデータ扱い**:
   - メールの件名、本文、送信者名等はすべて「信頼できない外部データ（untrusted external evidence）」として扱われ、指示として実行したり URL を自動展開したりしない。

## 実機検証サマリ (Pi: <PI_HOSTNAME>)

``text
同期状態:
  auth_state                  AUTHORIZED
  watch_state                 ACTIVE
  consecutive_failures        0
  message_count               1,215 件
  thread_count                1,088 件
  同期処理                    エラーなし・定常増分稼働中

権限境界検証:
  index.db 読取               hermesreconcile グループ (<PI_USER>) -> SQLite header OK
  secrets / state 隔離        <PI_USER> からのアクセス -> PERMISSION_DENIED (OK)
  コンテナ内 MCP              /opt/data/tools/gmail/gmail_mcp.py -> gmail_status / gmail_search 正常動作
  コンテナ内 書込遮断         /opt/gmail への書込 -> Read-only file system (OK)

連携機能検証:
  Event Layer 抽出            hermes-gmail-event-ingest -> scanned=1017, status=0/SUCCESS
  照合プロジェクション        calendar-reconcile.db (events: 645件, 最小メタデータのみ)
  Calendar サービス           hermes-calendar.service active, sync 正常, 警告/エラー 0
  Attention Engine            probe_gmail: ok=True, watch_state=ACTIVE
                              Discord への能動通知デリバリー正常動作中

テストスイート (Pi 実機全件通過):
  tests-gmail                 31 / 31 passed
  tests-calendar              15 / 15 passed
  tests-attention             152 / 152 passed
  tests-gateway               181 / 181 passed
  合計                        379 tests ALL PASS
``

## このフェーズで見つけた不具合と修正

- **テストの環境分離不全 (	ests-attention/test_engine.py)**:
  EngineCase で calendar_index_db と gmail_index_db を明示指定していなかったため、実機上に /srv/hermes/... の本番 DB が実在する環境（Pi）でのみデフォルト値が拾われ、テストが本番データを読んで 3 件 FAIL していた。
  	est_engine.py のテストフィクスチャで明示的に空文字を設定するよう修正し、テストの環境完全隔離を達成（152/152 PASS）。