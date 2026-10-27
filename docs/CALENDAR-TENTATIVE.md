# カレンダー自動仮登録（Calendar Tentative Registration）

Hermes がすでに統合している約束・締切（Discord / LINE / Gmail / Calendar →
`events.db` の commitments）のうち、**本人がやること・行くもの**で**日付があり**、
**まだカレンダーに載っていない**ものを、本人の**メインカレンダー（primary）**に
**仮（tentative）の予定**として自動で入れる。件名の先頭に必ず **【仮】** を付ける。

- 承認: 件名の先頭の【仮】を消す
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
                                            │  OAuth scope: calendar.events.owned のみ
                                            ▼
            Google Calendar primary（件名「【仮】…」、private 拡張プロパティ hermes_ct=1）
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
  app/ct_oauth.py     初回認可（root、127.0.0.1 のみ）
  app/ct_service.py   run / plan / status / check / repropose
  systemd/            oneshot service（テンプレート）と 15 分 timer
  install-root.sh     専用アカウント・資格情報ディレクトリ・コード・unit の配置
tests-calendar-tentative/   オフライン試験（scripts/check_offline.py の一部）
```

## 権限の境界 — 新しく増えるもの、増えないもの

これは Hermes に初めて加わる **Google Calendar への書き込み経路**。広げ方を最小にした。

| 項目 | 内容 |
|---|---|
| OAuth スコープ | `https://www.googleapis.com/auth/calendar.events.owned` **のみ**。Google の説明は「自分が所有するカレンダーの予定を参照・作成・変更・削除」。カレンダー自体の作成・削除・共有設定の変更はできない。`ct_oauth.py` は Google が他のスコープを返したらトークンを保存しない |
| 資格情報 | H.1（読み取り専用）とは**別の refresh token**を `/etc/hermes-secrets/google-calendar-tentative/` に置き、systemd `LoadCredential` でのみ渡す。OAuth クライアント（client-id/secret）は H.1 と共用可 |
| 書き込み先 | 設定で `primary` しか受け付けない（他の値は起動前に拒否）。状態DBは最初に使ったカレンダーに紐づき、別のIDを指すと止まる |
| 触る予定 | 自分で作った予定だけ。一覧は private 拡張プロパティ `hermes_ct=1` で絞って取得し、更新・削除は状態DBに記録した予定ID（`hermesct…`）に限る。本人の他の予定は読みも書きもしない（スコープ上は書けるので、この限定はコード側の約束） |
| 招待・通知 | 参加者なし、`sendUpdates=none`。他人に何も届かない |
| 予定の性質 | `status: tentative`、`transparency: transparent`（空き時間を塞がない） |
| commitments | `mode=ro` + `PRAGMA query_only` + systemd `ReadOnlyPaths`。このサービスは commitments を変更できない |
| Gmail インデックス | H.2 の公開スナップショットを `mode=ro` で読む。参照する列はヘッダー・ラベル・スレッドID・送信済みフラグのみで、本文・スニペットは読まない（ファイル単位では本文も読める権限であることに注意） |
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
| `gmail_only` | 根拠が Gmail だけで、`selection.gmail_only: "off"` |
| `gmail_bulk` | Gmail がプロモーション／ソーシャルに分類したメール |
| `gmail_list` | Gmail がフォーラム（メーリングリスト）に分類し、送信者にも宛先リストにも自分が送ったことがないメール |
| `gmail_notification` | Gmail の「更新」、または `no-reply@`・`store-news@` などの自動送信元。`people_and_notifications` なら対象 |
| `gmail_broadcast` | 宛先（To/Cc）が空・`undisclosed-recipients`・送信者自身だけの一斉送信。`people_and_notifications` なら対象 |
| `gmail_uncategorized` | Gmail の分類ラベルがなく、自分とのやり取りもない相手のメール |
| `gmail_draft` | 自分の下書き（送っていない文面）。これで作った仮予定は取り下げる |
| `gmail_discarded` | 削除済み、迷惑メール、ゴミ箱 |
| `gmail_unscreened` | Gmail インデックスが読めない、該当メールがない、送信者アドレスが読めない（判定できないものは入れない） |
| `already_on_calendar` | `calendar_authority`、または Calendar の根拠を持つ（自分の仮予定に由来するものは除く） |
| `matches_existing_event` | H.1 スナップショットに、正規化タイトルが**完全一致**し時刻も合う予定がある（類似度は使わない。自分の仮予定は除く） |
| `own_tentative_entry` | この commitment 自体が、自分の仮予定をカレンダー取り込みが拾ったもの |
| `low_confidence` | `confidence < min_confidence`（既定 0.7） |
| `no_date` / `imprecise_time` | 日付なし、または `week`/`month`/`daypart`/`approximate`/`unknown`。**時刻を推測しない** |
| `past` / `beyond_horizon` | 過ぎた、または `horizon_days`（既定 60 日）より先 |
| `duplicate_source` | 同じ元メッセージを指す別 commitment があり、そちらを残した |

### Gmail だけが根拠の項目（メールの選別）

I.1 では Gmail 由来の行の 95% がメルマガ・キャンペーンだった。そこで Gmail だけが根拠の行は、
H.2 が公開している Gmail インデックスの**ヘッダー・ラベル・スレッド・送信済みフラグだけ**を見て
（本文は読まない）、人からのメールに限って入れる。Discord / LINE / Calendar にも根拠がある行はこの選別を通らない。

判定順（`app/ct_mail.py`）:

| 順 | 条件 | 結果 |
|---:|---|---|
| 1 | インデックスにない | 入れない（`gmail_unscreened`） |
| 2 | 自分の下書き | 入れない（`gmail_draft`） |
| 3 | 削除済み・迷惑メール・ゴミ箱 | 入れない（`gmail_discarded`） |
| 4 | 自分が送ったメール | **入れる** |
| 5 | 自分が書き込んだスレッドで、自分の最初の返信より後に届いたメール | **入れる** |
| 6 | Gmail がプロモーション／ソーシャルに分類 | 入れない（`gmail_bulk`） |
| 7 | 送信者アドレスが読めない | 入れない（`gmail_unscreened`） |
| 8 | 自分が過去にメールを送った相手から | **入れる** |
| 9 | Gmail がフォーラム（メーリングリスト）に分類: 自分が投稿したことのあるリスト宛てなら | **入れる**。それ以外は入れない（`gmail_list`） |
| 10 | Gmail の「更新」、または自動送信元（`no-reply@`・`notifications@`・`store-news@`・`merumaga@`・`news.` 送信ホストなど） | 既定では入れない（`gmail_notification`） |
| 11 | 一斉送信（To/Cc が空・`undisclosed-recipients`・送信者自身だけ） | 既定では入れない（`gmail_broadcast`） |
| 12 | Gmail がメイン（個人）に分類 | **入れる** |
| 13 | Gmail の分類ラベルがない | 入れない（`gmail_uncategorized`） |

- 5 は「自分の最初の返信より後」だけ数える。メルマガに配信停止の返信をしても、そのメルマガ自体は会話にならない
- 自動送信元のアドレスは、返信したことがあっても「やり取りのある相手」にしない
- 6 が 8 より上なのは、一度問い合わせた店でも、Gmail がプロモーションに分類した次のメールは広告だから。
  メーリングリスト（9）が 8 より下なのは、ゼミ・サークル・委員会の人が投稿する場だから
- **13 が重要**: Google は日本のアカウントでは「スマート機能」を既定でオフにしており、そのとき Gmail は
  メールを分類しない（プロモーション等のラベルが付かない）。分類がないことを「個人のメール」とは読まず、
  その場合に通るのは自分が関わったメール（4・5・8・9）だけにした。知らない相手のメールも入れたい場合は
  Gmail の設定でスマート機能をオンにする。`ct_service.py check` が分類ラベル付きの受信メール数を表示し、
  0 件なら警告する
- 送信者アドレスは表示名に記号が入っていても `<...>` から読み取る。1 つの宛先が壊れていても他の宛先は失わない

仮予定の説明欄には「根拠: やり取りのある相手からのメール」のように、どの規則で入れたかを書く。
相手の欄にはメールアドレスや LINE の内部IDを書かない（Gmail の From をそのまま書くと、
メインカレンダーを共有している相手に第三者のアドレスが広がるため）。

`selection.gmail_only` で範囲を選べる（YAML では引用符で囲む。素の `off` は "off" として扱い、素の `yes`/`true` は拒否する）:

| 値 | 入るもの |
|---|---|
| `"off"` | Gmail だけが根拠の行は入れない |
| `"people"`（既定） | 上の 4・5・8・9・12 |
| `"people_and_notifications"` | さらに 10・11（予約確認・申込受付・大学からの一斉案内など） |
| `"all"` | 選別しない |

Gmail インデックスが読めないときは、Gmail だけが根拠の行を入れない（`all` を除く）。

### 予定の形

| 元データ | 仮予定 |
|---|---|
| 締切（`day`/`date` 精度） | その日（現地日付）の終日予定「【仮】〆 件名」 |
| 締切（`minute`/`exact_datetime`） | 終日予定「【仮】〆17:30 件名」 |
| `appointment_candidate`（時刻あり） | その時刻から `default_duration_minutes`（既定 60 分）の予定「【仮】件名」 |
| `appointment_candidate`（日付のみ） | 終日予定「【仮】件名」 |
| `scheduled_at` を持つもの | その枠どおり |

説明欄には出典の種類（Discord・Gmail など）、期限、相手、承認/却下の方法、`ref: <commitment_id>` だけを書く。
メッセージ本文は書かない。

仮予定は `status: tentative`・`transparency: transparent`（空き時間として表示）で作る。
【仮】を消して承認したあとも、この 2 つは本人が変えるまでそのまま残る。

## 自分の仮予定を「本物の予定」と取り違えない

メインカレンダーは H.1 のスナップショットにも入り、カレンダー取り込み（commitments の backfill）が
そこから commitment を作る。仮予定がそのまま戻ってくるので、次の 3 か所で予定IDの接頭辞
`hermesct` を見て自分のものを除く（`normalize_title` は先頭の【仮】を落とすため、件名だけでは区別できない）。

- H.1 スナップショット: 自分の仮予定は「既にある予定」に数えない（数えると仮予定が自分を取り下げる）
- commitment の根拠: `gcal:<カレンダー>:hermesct…` の Calendar 根拠は根拠に数えない。
  その commitment に統合で移った `calendar_authority` も、他に Calendar の根拠がなければ無視する
- 仮予定から作られた commitment そのもの（根拠がそれだけ、または件名が【仮】で始まる）は
  `own_tentative_entry` として提案しない（【仮】の上に【仮】を重ねない）

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
| 削除 | 一覧から消えた／`cancelled` | `REJECTED`。二度と作らない |
| 件名の【仮】を消す | etag が変わり、件名が【仮】で始まらない | `CONFIRMED`。以後触らない |
| 内容を編集（【仮】は残す） | etag が変わり、件名・説明・日時が当方の記録と一致しない | `USER_EDITED`。以後触らない |
| 別のカレンダーへ移動 | primary から消えた | `REJECTED` と同じ扱い（以後触らない） |
| 色だけ変更 | etag は変わったが内容は一致 | そのまま `ACTIVE` |

更新（PATCH）と取り下げ（DELETE）は `If-Match: <etag>` 付き。本人が先に触っていれば Google が 412 を返し、
上書きも削除もしない（この競合で触られた予定は、【仮】の有無にかかわらず `USER_EDITED`）。

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
| `ACTIVE` | カレンダーにあり、本人は触っていない |
| `CONFIRMED` / `REJECTED` / `USER_EDITED` / `WITHDRAWN` / `FAILED` | 最終。自動では何もしない |

`ct_service.py repropose <commitment_id>` だけが最終状態から戻せる（新しい世代・新しいIDで）。

## Discord への報告

`notify.enabled: true` のとき、まだ報告していない `ACTIVE` の仮予定をまとめて 1 通送る。

```text
カレンダーに仮登録しました（2件）
・10/5(月) 〆17:30 見積書の送付
・10/8(木) 10:00 打ち合わせ
確定するならタイトル先頭の【仮】を消し、不要なら削除してください。
```

- `notification_id` は対象 commitment の集合から決まり、gateway が重複を抑止する
- 静かな時間帯（既定 23:00–08:00）は送らず、明けてから送る
- 応答喪失・接続不可・送信失敗は**同じIDと本文**で最大 5 回まで再試行。`outcome_unknown` などの保留は再送しない
- 送る経路は Morning Brief と同じソケット・同じ規約。LINE は使わない

## 導入

`install-root.sh` は認可・設定変更・timer 有効化を**しない**。最後に手順を表示する。

```sh
sudo bash src/calendar-tentative/install-root.sh
# 1. H.1 の OAuth クライアントを流用（または新規作成）して root-only で配置
# 2. ssh -L 8766:127.0.0.1:8766 <pi> を張って認可（スコープは calendar.events.owned のみ）:
sudo /srv/hermes/calendar-tentative/venv/bin/python \
     /srv/hermes/calendar-tentative/app/ct_oauth.py
# 3. 設定は calendar.calendar_id: primary・summary_prefix: "【仮】" のまま。
#    最初は behaviour.mode: dry_run と小さい limits.max_creates_per_run で様子を見る
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
| `ct_service.py check` | 設定・資格情報の有無・DB 到達性・H.1 スナップショット内の自分の仮予定の件数 |
| `ct_service.py repropose <id>` | 却下・取り下げ・失敗したものを新しいIDで再提案 |
| `journalctl -u hermes-calendar-tentative` | 実行ごとの件数（件名は出さない） |

止めるとき: `systemctl disable --now hermes-calendar-tentative.timer`。
残った仮予定は Google カレンダーで「【仮】」を検索して消せる。

## 試験

`tests-calendar-tentative/`（142 件、CI の `scripts/check_offline.py` に追加）。
実スキーマ（commitments、H.1 index）で DB を作り、偽の Google（削除済み予定を保持、If-Match、
重複IDの 409、応答喪失、本人の削除・編集・移動を再現）に対して実行する。主な固定点:

- 2 回目の実行は何も作らない／応答喪失した insert は GET で確かめ、二重に作らない
- クラッシュで残った `INSERTING`、重複ID 409、明確な拒否と自分側の障害の区別
- 本人の削除は二度と作らない、【仮】を消したら `CONFIRMED`、編集は以後触らない、色変更は追従を続ける
- 自分の仮予定が H.1・backfill・統合を経て戻ってきても、取り下げも二重提案もしない
  （`is_own_event` を無効にするとこれらの試験が落ちることを確認済み）。本人の他の予定には一切触らない
- PATCH/DELETE の 412 は本人の勝ち、応答喪失した PATCH/DELETE の正しい決着
- 過去の仮予定は消さない、対象外になっただけでは消さない、件数上限
- primary 以外の設定と、旧サブカレンダー用の状態DBの流用を拒否。OAuth 障害時は何も変えない、commitments を変更しない
- Google へのリクエスト形（スコープ、`sendUpdates=none`、If-Match、401 の一度だけの再取得）
- gateway への通知本文に受信者フィールドがない、再試行の上限、静かな時間帯

## 未確認・既知の制約

- **実機・実 Google での検証はしていない。** スコープ、IDの制約、`sendUpdates`、`status` の値、
  `privateExtendedProperty` での絞り込みは Google の公式リファレンスで確認したが、重複IDへの 409 応答は
  実アカウントで未確認。H.1 が Google の予定IDをそのまま `event_id` に保存することはコード
  （`cal_normalize.py`）で確認した
- `calendar.events.owned` は本人の予定を全部書き換えられるスコープ。触るのを自分の仮予定に限るのは
  コードの側（private 拡張プロパティでの一覧、状態DBの予定IDだけを PATCH/DELETE）
- 仮予定の commitment を手で統合するとき、仮予定由来の commitment を残す側にしない。
  そうすると元の commitment が置き換え済み（`superseded`）になって仮予定が取り下げられ、
  【仮】を消して確定した予定由来なら、Discord などの根拠が移った残る側が改めて仮登録されうる。
  仮予定由来の commitment は `commitments.admin backfill` を手で実行したときにしかできない
- Gmail だけが根拠の行は既定で「人からのメール」だけ。予約確認や大学からの一斉案内は
  `gmail_only: "people_and_notifications"` で入る
- 実メールボックスで Gmail の分類ラベルが付いているか（スマート機能がオンか）は未確認。
  オフなら、知らない相手のメールは入らない（`check` で件数を確認できる）
- 送信済みメールは H.2 の初回取り込み期間（既定 30 日）より前のものがないため、それより古い
  やり取りの相手は「やり取りのある相手」として扱われない
- Gmail の下書きが H.2 の抽出でイベント化される上流の問題は、この変更では直していない
  （仮登録側では下書きを根拠にしない）
- 予定（`appointment`）の判定は Discord/LINE 抽出器の `appointment_candidate` に依存する。
  それ以外の日時付き commitment は締切として終日で入る
- commitments の分類精度（`commitments/reconcile.py` のキーワード規則）がそのまま選定精度になる
- 繰り返し予定は扱わない
- 仮予定は primary にあるので、H.1 経由で Hermes のチャットからも「【仮】…」として見える。
  チャットから仮予定を作ったり消したりする経路はない
