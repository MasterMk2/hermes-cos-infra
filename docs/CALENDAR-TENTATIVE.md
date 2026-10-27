# カレンダー自動仮登録（Calendar Tentative Registration）

Hermes がすでに統合している約束・締切（Discord / LINE / Gmail / Calendar →
`events.db` の commitments）のうち、**本人がやること・行くもの**で**日付があり**、
**まだカレンダーに載っていない**ものを、専用のサブカレンダー「Hermes 仮登録」に
**仮（tentative）の予定**として自動で入れる。

- 承認: その予定を自分のカレンダーへ移動する（予定の編集画面でカレンダーを変更）
- 却下: 削除する。**削除したものは二度と登録しない**
- 報告: 追加したら Discord に一通「カレンダーに仮登録しました（N件）」（任意）

OpenAI Dots の「見ていない間に仕事を見つけ、終わった形で承認に回す」を、
既存の安全境界を崩さない範囲で最初に実装したもの。判断は決定論的なルールのみで、
LLM は使わない。

> 状態: コードとオフライン試験のみ。Pi への配備、実 Google Calendar への書き込み、
> 実データでの選定結果は**未確認**。

## 構成

```text
events.db (commitments)        ─ read-only ─┐
calendar/index/index.db (H.1)  ─ read-only ─┤   hermes-calendar-tentative.timer (15分毎)
                                            ▼
            hermes-calendar-tentative.service  (User=hermestentative, oneshot)
              select → reserve → resolve → reconcile → maintain → insert → notify
                                            │  OAuth scope: calendar.app.created のみ
                                            ▼
            Google Calendar「Hermes 仮登録」(このサービス自身が作ったサブカレンダー)
                                            │
                                            └─ 任意: gateway proactive.sock → Discord DM
```

```text
src/calendar-tentative/
  app/ct_select.py    commitments → 「あるべき仮予定」と、それ以外の理由（Google を呼ばない）
  app/ct_engine.py    状態DBと Google の差分を取って適用する1回分の処理
  app/ct_store.py     状態DB（proposals / retired_ids / notifications / runs）
  app/ct_google.py    OAuth と Calendar v3（標準ライブラリのみ）
  app/ct_notify.py    gateway ソケット経由の Discord ダイジェスト
  app/ct_config.py    秘密情報を持たない厳格な設定
  app/ct_oauth.py     初回認可とサブカレンダー作成（root、127.0.0.1 のみ）
  app/ct_service.py   run / plan / status / check / repropose
  systemd/            oneshot service（テンプレート）と 15 分 timer
  install-root.sh     専用アカウント・資格情報ディレクトリ・コード・unit の配置
tests-calendar-tentative/   オフライン試験（scripts/check_offline.py の一部）
```

## 権限の境界 — 新しく増えるもの、増えないもの

これは Hermes に初めて加わる **Google Calendar への書き込み経路**。広げ方を最小にした。

| 項目 | 内容 |
|---|---|
| OAuth スコープ | `https://www.googleapis.com/auth/calendar.app.created` **のみ**。Google の説明は「サブカレンダーを作成し、その上の予定を参照・作成・変更・削除」。本人の primary や既存のカレンダーは**読めず書けない**。`ct_oauth.py` は Google が他のスコープを返したらトークンを保存しない |
| 資格情報 | H.1（読み取り専用）とは**別の refresh token**を `/etc/hermes-secrets/google-calendar-tentative/` に置き、systemd `LoadCredential` でのみ渡す。OAuth クライアント（client-id/secret）は H.1 と共用可 |
| 書き込み先 | 設定で `@group.calendar.google.com` で終わる ID しか受け付けない。`primary` やメールアドレス形式は起動前に拒否。状態DBは最初に使ったカレンダーに紐づき、別のIDを指すと止まる |
| 招待・通知 | 参加者なし、`sendUpdates=none`。他人に何も届かない |
| 予定の性質 | `status: tentative`、`transparency: transparent`（空き時間を塞がない） |
| commitments | `mode=ro` + `PRAGMA query_only` + systemd `ReadOnlyPaths`。このサービスは commitments を変更できない |
| Hermes 本体 | 新しいツールも権限も渡さない。チャットから仮登録を作る経路はない |
| Discord 通知 | gateway の既存 proactive ソケットのみ。受信者フィールドは存在せず、LINE は使わない |

## 選定ルール

I.2 planner の適格条件をそのまま使う（本番データで測定済みのため）。理由はすべて閉じた語彙で記録し、
`plan` / `status` で件数が見える。

| 理由 | 意味 |
|---|---|
| `resolved` / `superseded` / `informational` | 完了・取消・却下済み、置き換え済み、お知らせ |
| `not_owner_task` | `COUNTERPARTY`、または `WAITING` / `BLOCKED`（次の一手が相手側） |
| `shared_without_chat_evidence` | `SHARED` だが Discord/LINE/Calendar の裏付けがない |
| `gmail_only` | 根拠が Gmail だけ（I.1 で 95% がメルマガ等と測定）。`include_gmail_only: true` で対象にできる |
| `already_on_calendar` | `calendar_authority`、または Calendar の根拠を持つ |
| `matches_existing_event` | H.1 スナップショットに、正規化タイトルが**完全一致**し時刻も合う予定がある（類似度は使わない） |
| `low_confidence` | `confidence < min_confidence`（既定 0.7） |
| `no_date` / `imprecise_time` | 日付なし、または `week`/`month`/`daypart`/`approximate`/`unknown`。**時刻を推測しない** |
| `past` / `beyond_horizon` | 過ぎた、または `horizon_days`（既定 60 日）より先 |
| `duplicate_source` | 同じ元メッセージを指す別 commitment があり、そちらを残した |

### 予定の形

| 元データ | 仮予定 |
|---|---|
| 締切（`day`/`date` 精度） | その日（現地日付）の終日予定「[仮] 〆 件名」 |
| 締切（`minute`/`exact_datetime`） | 終日予定「[仮] 〆17:30 件名」 |
| `appointment_candidate`（時刻あり） | その時刻から `default_duration_minutes`（既定 60 分）の予定「[仮] 件名」 |
| `appointment_candidate`（日付のみ） | 終日予定「[仮] 件名」 |
| `scheduled_at` を持つもの | その枠どおり |

説明欄には出典の種類（Discord・Gmail など）、期限、相手、承認/却下の方法、`ref: <commitment_id>` だけを書く。
メッセージ本文は書かない。

## 一度しか作らない仕組み

- 予定IDは `(カレンダー, commitment, 世代)` から決まる（base32hex、`hermesct` + SHA-256）。
  同じものを二度 insert すると Google が 409 を返す
- insert の前に状態DBへ `INSERTING` をコミットする。応答が失われたら `UNKNOWN` とし、
  次回はそのIDを **GET して確かめる**（ある → `ACTIVE`、ない → `PENDING` に戻して再送）。
  闇雲に再送しない
- 一度でも Google に出たIDは `retired_ids` に残り、再利用しない（Google は削除済み予定のIDを保持する）
- 実行は 1 回ずつ（ロックファイル）。systemd の再起動ではなく timer が再試行
- Google が明確に拒否した insert は `max_attempts`（既定 5）で `FAILED`。
  こちら側の障害（DNS・接続拒否・OAuth 更新失敗）は回数に数えず、OAuth が使えない回は何も触らずに止まる

## 持ち主が常に勝つ

| 本人の操作 | 検出 | その後 |
|---|---|---|
| 削除 | 仮カレンダーから消えた／`cancelled` | `REJECTED`。二度と作らない |
| 自分のカレンダーへ移動 | 仮カレンダーから消え、同じ予定IDが H.1 スナップショットの別カレンダーにある | `CONFIRMED`。以後触らない |
| 内容を編集 | etag が変わり、件名・説明・日時が当方の記録と一致しない | `USER_EDITED`。以後触らない |
| 色だけ変更 | etag は変わったが内容は一致 | そのまま `ACTIVE` |

更新（PATCH）と取り下げ（DELETE）は `If-Match: <etag>` 付き。本人が先に触っていれば Google が 412 を返し、
上書きも削除もしない。移動の検出は H.1 のスナップショットに依存するため、移動先カレンダーが H.1 の
`selected_ids` にない場合は `REJECTED` と記録される（以後何もしない点は同じ）。

## 追従と取り下げ

- 締切や日時が変わり、本人が触っていない仮予定 → 同じ予定を動かす（`update_on_change`）
- commitment が完了・取消・消滅した、または本物の予定がカレンダーに現れた → **未来の**仮予定だけを削除
  （`withdraw_on_resolution`）。過去の仮予定は履歴として残す
- 対象外になっただけ（再分類・期間外など）では削除しない
- 削除の応答が失われても、`withdraw_requested_at` が残るので次回 `WITHDRAWN` と判定する（本人の却下と区別）

## 状態

| 状態 | 意味 |
|---|---|
| `PENDING` | 予約済み。まだ送っていない、または Google に無いと確認済み |
| `INSERTING` | insert 直前のコミット。クラッシュ後は `UNKNOWN` 扱い |
| `UNKNOWN` | insert されたか不明。ID を GET するまで保留 |
| `ACTIVE` | 仮カレンダーにあり、本人は触っていない |
| `CONFIRMED` / `REJECTED` / `USER_EDITED` / `WITHDRAWN` / `FAILED` | 最終。自動では何もしない |

`ct_service.py repropose <commitment_id>` だけが最終状態から戻せる（新しい世代・新しいIDで）。

## Discord への報告

`notify.enabled: true` のとき、まだ報告していない `ACTIVE` の仮予定をまとめて 1 通送る。

```text
カレンダーに仮登録しました（2件）
・10/5(月) 〆17:30 見積書の送付
・10/8(木) 10:00 打ち合わせ
確定するなら自分のカレンダーへ移動、不要なら削除してください。
```

- `notification_id` は対象 commitment の集合から決まり、gateway が重複を抑止する
- 静かな時間帯（既定 23:00–08:00）は送らず、明けてから送る
- 応答喪失・接続不可・送信失敗は**同じIDと本文**で最大 5 回まで再試行。`outcome_unknown` などの保留は再送しない
- 送る経路は Morning Brief と同じソケット・同じ規約。LINE は使わない

## 導入

`install-root.sh` は認可・カレンダー作成・設定変更・timer 有効化を**しない**。最後に手順を表示する。

```sh
sudo bash src/calendar-tentative/install-root.sh
# 1. H.1 の OAuth クライアントを流用（または新規作成）して root-only で配置
# 2. ssh -L 8766:127.0.0.1:8766 <pi> を張って:
sudo /srv/hermes/calendar-tentative/venv/bin/python \
     /srv/hermes/calendar-tentative/app/ct_oauth.py --create-calendar "Hermes 仮登録"
# 3. 表示された ID を /srv/hermes/calendar-tentative/config/config.yaml の calendar.calendar_id へ
#    （H.1 の calendars.selected_ids には入れない — 入れると check が失敗する）
# 4. 確認と予行（Google を呼ばず、何も書かない）
sudo -u hermestentative /srv/hermes/calendar-tentative/venv/bin/python \
     /srv/hermes/calendar-tentative/app/ct_service.py --config /srv/hermes/calendar-tentative/config/config.yaml check
sudo -u hermestentative /srv/hermes/calendar-tentative/venv/bin/python \
     /srv/hermes/calendar-tentative/app/ct_service.py --config /srv/hermes/calendar-tentative/config/config.yaml plan
# 5. 開始
sudo systemctl enable --now hermes-calendar-tentative.timer
```

様子を見たいだけなら `behaviour.mode: dry_run` にすると timer は件数だけを記録し、何も作らない。

## 運用

| コマンド | 出力 |
|---|---|
| `ct_service.py plan` | 作る／動かす／取り下げる予定の一覧（件名あり）。Google を呼ばない |
| `ct_service.py status` | 状態別件数と前回の実行（件名なし） |
| `ct_service.py check` | 設定・資格情報の有無・DB 到達性・H.1 に仮カレンダーが選ばれていないか |
| `ct_service.py repropose <id>` | 却下・取り下げ・失敗したものを新しいIDで再提案 |
| `journalctl -u hermes-calendar-tentative` | 実行ごとの件数（件名は出さない） |

止めるとき: `systemctl disable --now hermes-calendar-tentative.timer`。
仮カレンダーごと不要なら Google カレンダー側でそのカレンダーを削除する。

## 試験

`tests-calendar-tentative/`（92 件 + subtests 25 件、CI の `scripts/check_offline.py` に追加）。
実スキーマ（commitments、H.1 index）で DB を作り、偽の Google（削除済み予定を保持、If-Match、
重複IDの 409、応答喪失、本人の削除・編集・移動を再現）に対して実行する。主な固定点:

- 2 回目の実行は何も作らない／応答喪失した insert は GET で確かめ、二重に作らない
- クラッシュで残った `INSERTING`、重複ID 409、明確な拒否と自分側の障害の区別
- 本人の削除は二度と作らない、移動は `CONFIRMED`、編集は以後触らない、色変更は追従を続ける
- PATCH/DELETE の 412 は本人の勝ち、応答喪失した PATCH/DELETE の正しい決着
- 過去の仮予定は消さない、対象外になっただけでは消さない、件数上限
- 別カレンダーへの付け替え拒否、OAuth 障害時は何も変えない、commitments を変更しない
- Google へのリクエスト形（スコープ、`sendUpdates=none`、If-Match、401 の一度だけの再取得）
- gateway への通知本文に受信者フィールドがない、再試行の上限、静かな時間帯

## 未確認・既知の制約

- **実機・実 Google での検証はしていない。** スコープ、IDの制約、`sendUpdates`、`status` の値は
  Google の公式リファレンスで確認したが、重複IDへの 409 応答と、UI で別カレンダーへ移動した予定が
  同じIDを保つことは実アカウントで未確認。後者が違っても「移動」が `REJECTED` と記録されるだけで、
  作り直しや削除は起きない
- Gmail だけが根拠の行は既定で対象外。必要なら `include_gmail_only: true`
- 予定（`appointment`）の判定は Discord/LINE 抽出器の `appointment_candidate` に依存する。
  それ以外の日時付き commitment は締切として終日で入る
- commitments の分類精度（`commitments/reconcile.py` のキーワード規則）がそのまま選定精度になる
- 繰り返し予定は扱わない
- Hermes のチャットからは仮予定が見えない（仮カレンダーを H.1 に入れないため）。
  「仮登録したもの何？」に答える読み取り専用ツールは次の候補
