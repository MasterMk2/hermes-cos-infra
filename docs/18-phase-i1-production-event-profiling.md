# Phase I.1 — Production Event Profiling Report

Date: 2026-09-06
Target Database: `/srv/hermes/hermes-data/events/events.db` (Production)
Related Databases:
- `/srv/hermes/calendar/index/index.db`
- `/srv/hermes/gmail/index/index.db`
- `/srv/hermes/hermes-data/knowledge-index/line/index.db`
- `/srv/hermes/knowledge/discord/messages.db`
- `/srv/hermes/gmail-event/reconcile/calendar-reconcile.db`

---

## 1. Executive Summary

A comprehensive, read-only profiling of the production `events.db` was performed.
Key finding: **The current Event Layer suffers from extreme Gmail automated noise and overdue inflation**, while containing high-value, authentic human commitments in Discord and LINE, as well as authoritative scheduled commitments in Google Calendar that are currently completely unlinked (0 links).

- **Total events**: 656
- **Source breakdown**: Gmail 560 (85.4%), Discord 93 (14.2%), LINE 3 (0.5%)
- **Status breakdown**: Candidate 552 (84.1%), Open 63 (9.6%), Superseded 41 (6.2%), Resolved 0 (0%), Cancelled 0 (0%), Ignored 0 (0%)
- **Overdue inflation**: 157 events in `open` / `candidate` have `due_at < now()`, of which **~90% are expired marketing emails, newsletters, or daily schedule digests** mistakenly classified as requests with deadlines.
- **Calendar linkage**: Exactly 0 links in `event_calendar_links` and `event_links`, despite Google Calendar holding 81 authoritative schedule entries (including 39 in 2026: classes, exams, trips, album deadlines).
- **Actor semantics**: 648 events (98.8%) are labeled `counterparty`, squashing user tasks, waiting-for dependencies, third-party tasks, and automated bots into a single bucket.

---

## 2. Quantitative Baseline

### 2.1 Event Counts by Source & Status

| Source Type | Total | Candidate | Open | Superseded | Resolved | Cancelled |
|:------------|------:|----------:|-----:|-----------:|---------:|----------:|
| **Gmail**   | 560   | 547       | 13   | 0          | 0        | 0         |
| **Discord** | 93    | 4         | 48   | 41         | 0        | 0         |
| **LINE**    | 3     | 1         | 2    | 0          | 0        | 0         |
| **Total**   | **656** | **552** | **63**| **41**    | **0**    | **0**     |

### 2.2 Temporal Metrics

- **Events with `due_at`**: 283 (43.1%)
- **Events without `due_at`**: 373 (56.9%)
- **Due Precision Distribution**:
  - `unknown`: 373
  - `day`: 165
  - `minute`: 79
  - `date`: 17
  - `exact_datetime`: 10
  - `daypart`: 5
  - `week`: 4
  - `approximate`: 2
  - `month`: 1
- **Overdue Events (`due_at < now`)**:
  - In `open` or `candidate`: 157
  - Total across all statuses: 171
- **Snoozed Events**: 0
- **Project Linked Events (`project_id IS NOT NULL`)**: 0

### 2.3 Calendar Database (`/srv/hermes/calendar/index/index.db`)

- **Total Calendar Events**: 81
  - Events in 2026: 39 (e.g. `サークル合宿`, `地域の交流会`, `模擬試験`, `【締切】アルバム 個人ページ入稿`, `実習`, `【締切】アルバム 実習班ページ入稿`, `第2回 学年ガイダンス`, `Hermes H1 E2E [cancelled]`)
  - Events in 2027: 42 (birthdays, recurring tests)
- **Active Links in `event_links`**: 0
- **Active Links in `event_calendar_links`**: 0

---

## 3. Deep Qualitative Analysis

### 3.1 Actor & Responsibility Semantics

- Current DB fields: `actor` (`counterparty` 648, `user` 8).
- **The Problem**:
  1. Inbound messages and emails are automatically assigned `actor = 'counterparty'`.
  2. A request received from someone else asking the user to do something (e.g. `送付時にBCCへarchive@example.invalid追加の依頼`) has `actor = counterparty`, but the **responsible party** is `SELF`.
  3. A statement by someone else saying they will do something (e.g. `鈴木が薬学部教授会への送信を対応すると約束`) has `actor = counterparty`, and the **responsible party** is `COUNTERPARTY` (user has nothing to do, or is waiting for them).
  4. An outbound request sent by the user (e.g. LINE: `告知バナー流用可否と協賛ロゴ掲載の確認依頼`) has `actor = user`, but this is a `WAITING_FOR` state (waiting for 制作部 to respond).
  5. Newsletters and transactional emails (e.g. `Google アカウントの一部のデータを Claude と共有しました`, `動画配信サービス`, `飲食チェーン`) have `actor = counterparty`, but require zero action (pure notification / informational noise).
- **Conclusion**: We cannot rely on a simple `actor` column. We must define orthogonal dimensions:
  - `responsibility`: `SELF`, `COUNTERPARTY`, `SHARED`, `INFORMATIONAL`
  - `work_state`: `ACTIONABLE`, `WAITING`, `SCHEDULED`, `BLOCKED`, `DONE`, `CANCELLED`, `DISMISSED`

### 3.2 False Positive Profiling

- **Gmail Source Noise**:
  - Out of 560 Gmail events, **over 95% are false positives**.
  - All 13 Gmail events marked `status = 'open'` are automated emails:
    - `オンラインサービス : ご登録ありがとうございます`
    - `Bugzilla: confirm account creation`
    - `[GitHub] A personal access token has been added`
    - `家計簿アプリ ウィークリーメール`
    - `飲食チェーン 200円引きクーポン`
    - `PCメーカー学生ストア 決算売り尽くし` (given a bogus due date `2027-05-30`)
    - `Google カレンダー 本日、予定されている予定はありません。`
  - **Root Cause**: The Phase H.2 Gmail event extractor greedily treated promotional expiry dates ("8月9日まで", "本日23:59まで", "水曜はポイント3倍") as task deadlines and populated `classification_reason = 'received_explicit_request'`.

### 3.3 Overdue Noise Analysis

- The database reports **157 overdue open/candidate events**.
- When sampled, **148 of 157 (94%) are expired marketing emails and automated newsletters**:
  - `【8/9まで】キャラクター限定グッズ (フードデリバリー)` -> due 2026-08-09
  - `【本日締切！】サッカー撮影講習会 (写真サービス)` -> due 2026-08-14
  - `【本日23:59まで】最大5,000円OFFクーポン (会計SaaS)` -> due 2026-08-13
  - `山田太郎 さんの今日の予定リスト` -> daily email marked overdue each night
- **Real Overdue Items**:
  - LINE: `サークル旅行参加投票（締切9/2 24時）` -> Authentic deadline that passed.
  - Discord: `定例会の出欠・予定をメンバーに確認` -> Past internal date (8/25).
  - Discord: `高橋宛:予算修正は今週中` -> Past internal date (8/30).
- **Crucial Design Requirement**:
  - `OVERDUE` must **never be a stored immutable status**. It is a **derived temporal state** calculated dynamically: `due_at < now() AND work_state IN ('ACTIONABLE', 'WAITING')`.
  - Informational newsletters and dismissed items must never enter the active `ACTIONABLE` state, immediately eliminating 95%+ of the false-positive overdue noise.

### 3.4 Duplicate Profiling

- Exact Summary Duplicates (same subject repeated across time):
  - `本日、予定されている予定はありません。`: 15 instances
  - `【クリエイター支援サービスA】フォロー中トピック更新のお知らせ`: 11 instances
  - `Google アカウントの一部のデータを Claude と共有しました`: 11 instances
  - `山田太郎 さんの今日の予定リスト（午前 5 時現在）`: 9 instances
  - `New sign-in to your AI service account`: 7 instances
  - `【クリエイター支援サービスB】フォロー中のクリエイターから新しい投稿があります`: 6 instances
  - `セキュリティ通知`: 5 instances
  - `アナタにオススメの作品が更新されました`: 5 instances
  - `監視SaaS - Weekly Summary Report`: 4 instances
  - `【今日の究極ドリル】`: 4 instances
- Cross-Source Deduplication:
  - Calendar vs Events: e.g. Calendar has `Hermes H1 E2E`, `【締切】アルバム 個人ページ入稿`, `サークル合宿`.
  - Discord vs LINE vs Gmail: Real festival commitments (医薬祭, 協賛, 見積書) are discussed primarily on Discord, with cross-references to LINE (制作チーム) and Gmail (quotes from companies like ベンダA, ベンダB).

### 3.5 Lifecycle & History Transitions

- `events.db` contains 45 entries in `event_history`:
  - 41 entries are `status: open -> superseded`.
  - These supersedes chains are working properly in Discord! For example:
    - `evt_835eb1180706` -> superseded by `evt_a0ab1defaea1`
    - `evt_695a69057e10` -> superseded by `evt_8b78c64ff5df`
    - `evt_a1cb634855c7` -> superseded by `evt_5cb2db714612`
    - `evt_0054dda424fe` -> superseded by `evt_75b146f1c933` -> superseded by `evt_0f781a277bb1` -> superseded by `evt_eb736b1e62fe`
- Notice: `resolved`, `cancelled`, and `ignored` have **0 occurrences** in history. No event has ever been closed as completed.

---

## 4. Requirements for Canonical Commitment Semantics (I.1-B)

Based directly on the profiling evidence above:

1. **Commitment vs Evidence Separation**:
   - A single canonical commitment must aggregate 1 to N immutable evidence records (`commitment_evidence`).
   - Merging evidence from Gmail, Discord, LINE, and Calendar must preserve the raw source records and original timestamps.

2. **Orthogonal Semantic Dimensions**:
   - **Responsibility**:
     - `SELF`: User is directly responsible for executing the action.
     - `COUNTERPARTY`: An external party is responsible (often corresponding to a user's waiting state).
     - `SHARED`: Group/team shared responsibility.
     - `INFORMATIONAL`: Pure notice, newsletter, transaction receipt, no action required.
   - **Work State**:
     - `ACTIONABLE`: Open task waiting for action by the responsible party.
     - `WAITING`: Waiting for counterparty response, review, or external trigger.
     - `SCHEDULED`: Scheduled for a specific time slot on the calendar.
     - `BLOCKED`: Dependency not met.
     - `DONE`: Completed / resolved with verifiable evidence.
     - `CANCELLED`: Explicitly revoked or abandoned.
     - `DISMISSED`: Filtered as non-actionable noise / automated mailer.
   - **Derived Temporal State** (Computed dynamically, never stored as static status):
     - `NO_DATE`: No deadline or scheduled time.
     - `FUTURE`: Due/scheduled > 48 hours away.
     - `DUE_SOON`: Due/scheduled within 48 hours.
     - `OVERDUE`: Past due (`due_at < now()`) and still in `ACTIONABLE` or `WAITING`.

3. **Due Date vs Scheduled Time**:
   - `due_at`: Hard external deadline (when something must be submitted/finished).
   - `scheduled_at`: Dedicated calendar slot (when the event or activity occurs).
   - Google Calendar holds authoritative status for `scheduled_at`.

4. **Conservative Reconciliation & Quality Gates**:
   - **Explicit links win** (`event_calendar_links`, explicit human links).
   - **False merge is far worse than temporary duplicate**: If subjects or counterparty match ambiguously across sources without strong deterministic keys, keep them separate!
   - **False resolution is far worse than temporary open**: Never auto-resolve based on silence, calendar expiry, or weak inference. Require explicit completion evidence.
   - **Initial Flood Prevention**: The backfill must cleanly triage the 560 Gmail events. Marketing newsletters, automated alerts, and daily schedule notifications must be classified as `responsibility = INFORMATIONAL, work_state = DISMISSED` so they never spam the Chief of Staff agenda or trigger false overdue alerts.

---

## 5. Next Steps

With this profiling completed and documented:
1. Proceed to **I.1-B: Commitment Semantics Design** to define the exact database schema and state transition rules.
2. Implement **I.1-C: Canonical Commitment Store** with idempotent backfill.
3. Build **I.1-D: Evidence Reconciliation Layer** with Calendar authority.
4. Implement **I.1-E: Retrieval & Agenda MCP** (`commitment_list`, `commitment_get`, `agenda_get`, etc.).
5. Wire **I.1-F: Attention Integration** with flood suppression.
6. Execute **I.1-G: Production E2E & Regression**.
