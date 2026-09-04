# Phase G.1 — Unified Channel Gateway

LINE 公式アカウントと Discord のダイレクトメッセージから Hermes に話しかけ、
その答えを同じ経路で返すための受け口。

このドキュメントは**実装が何を保証するか**と、**測定して分かった制約**を記録する。
手順書ではなく、設計判断の根拠を残すためのもの。

---

## 1. ゲートウェイは「エージェント」ではない

Gateway がやることは 6 つだけ。

```
認証する / 正規化する / 待ち行列に入れる / 経路を決める / 送る / 監査に残す
```

やらないこと（これは仕様ではなく、コード上そもそも到達できない）:

- ホストの管理・シェル実行・デプロイ・バージョン管理の操作
- コーディングジョブの作成やディスパッチ
- チャットから任意のツールや MCP を呼ぶこと

最後の 1 つは Gateway 側だけでなく **Hermes 側の capability profile** でも塞いである
（`platform_toolsets.api_server` の明示的な許可リスト）。実際に破れないことは
敵対的に確認した — エージェントに「シェルを実行できるツールを `tool_search` で探して
呼べ」と指示すると `NO_SHELL_TOOL_FOUND` を返し、「コーディングジョブを作れ」には
`CANNOT_CREATE_JOB` を返した。

## 2. 2 体の Discord ボットは別物

| | Knowledge Bot (D.1) | Interface Bot (G.1) |
|---|---|---|
| 役割 | 耳だけ | 口と耳 |
| 読む対象 | ギルドのチャンネル | **DM のみ** |
| intents | 33281 (GUILDS + GUILD_MESSAGES + MESSAGE_CONTENT) | **4096 (DIRECT_MESSAGES)** |
| 発言 | しない | owner にのみ返す |
| Application | 別 | 別 |
| トークン | 別 | 別 |

Interface Bot が **MESSAGE_CONTENT (32768) を要求していない**のは、
Discord が「特権 intent なしでも本文を渡す 4 例外」を文書化しており、
そのうちの 1 つが *Content in DMs with the app* だから。
DM しか使わないので、特権 intent は要らない。
（`docs.discord.com/developers/events/gateway`、2026-09-03 確認）

intents 値は起動時に `REQUIRED_INTENTS_VALUE` と突き合わせて検証する。
将来の編集で intent が広がったら、黙って権限が増えるのではなく**起動に失敗する**。

## 3. 信頼できる封筒とユーザー文字列は構造的に別

Hermes への `POST /v1/runs` は 2 つのフィールドを別々に埋める。

```
instructions ← ゲートウェイが組み立てた封筒（プラットフォーム、認証結果、会話 ID…）
input        ← ユーザーが打った文字列。ここにしか入らない
```

`build_envelope(message)` は**テキスト由来の引数を一切取らない**。
関数シグネチャ自体をテストで固定してあるので、後から「便利な」引数を
足すと落ちる。

つまり `authenticated_owner: true` と打っても変わるのは `input` の中身だけで、
封筒は動かない。これは実際に敵対的な文字列 5 種を投げて確認している
(`test_authority.py::TestEnvelopeIsUnforgeable`)。

## 4. 署名は「届いたバイト列」に対して検証する

`verify_signature(raw_body: bytes, ...)` は **bytes しか受け取らない**。
一度 `json.loads` した呼び出し側は、再シリアライズしないと渡せない —
そして再シリアライズしたバイト列は署名が一致しない。
その性質自体をテストで固定してある。

リバースプロキシ側でも同じ制約がある。**ボディを書き換えるディレクティブを
足すと全通信が 403 になる。**

## 5. 測定した制約 — reply は無料、push は従量

これが機能全体の律速になる。実機で 5 種類のプロンプトを測った（2026-09-03）。

| プロンプト | 応答時間 | 経路 |
|---|---:|---|
| 挨拶 | 9.3s | reply（無料） |
| 記憶の参照 | 23.4s | reply（無料） |
| Discord 知識検索 | 49.6s | push（従量） |
| 短い相談 | 62.5s | push（従量） |
| 複数ツール横断 | 88.3s | push（従量） |

中央値 49.6s、最大 88.3s。**5 件中 3 件が reply 枠を超えた。**

- LINE の reply トークンは 60 秒・1 回限り。reply は**無料**。
- push は無料プラン（日本）で**月 200 通**。
- 受理（202 を返すまで）は 0.01〜0.06 秒。LINE の 2 秒予算は非同期設計で余裕。

この測定を受けて `REPLY_TOKEN_SAFETY_MARGIN_SECONDS` を 15 → **8 秒**に下げた。
根拠は非対称性: reply が期限切れなら 400 が返るだけで即 push に切り替わるので、
**楽観的に試すコストはほぼゼロ、悲観的に諦めるコストは必ず 1 通**。

### 課金単位は「送信先の人数」— オブジェクト数ではない

これは見積もりが 5 倍ずれうる箇所なので、公式ドキュメントで確認した
（developers.line.biz/en/docs/messaging-api/pricing/、2026-09-03）。

> "The number of messages is counted by the number of people you send a message to."

5 個のメッセージオブジェクトを 1 人に push しても **1 通**。
長い回答を 5 分割しても課金は 1 通で済む。

だから `deliver_line` は分割した全 chunk を**1 回の API 呼び出しにまとめる**。
chunk ごとに 5 回 push すると 5 通分かかるので、これは最適化ではなく
費用そのものの設計。

あわせて確認できたこと:

- reply は「カウントされない送信方法」に明記されている（無料）。
- ブロック済みユーザーや存在しない user ID への送信はカウントされない。

### 運用上の含意

月 200 通 ≒ **1 日 6.6 通**。上の分布（約 6 割が push）のまま使うと、
**1 日あたり 11 往復前後**で無料枠に触れる。
`bin/gw-status.sh` が月ごとの push 実績を表示するので、まずそこを見ること。

足りなければ有料プランに上げれば済む話で、この制約は設計の欠陥ではない。
ただし**チャネルを作る前に知っておくべき数字**なので先頭に書いてある。

## 6. 落ちても失わない

受信ハンドラは「署名検証 → 1 行コミット → 200」までしかやらない。
Hermes を呼ぶのは worker で、両者は**コミット済みの DB 行**でしか繋がっていない。

- **enqueue に失敗したら 500 を返す。** LINE は 200 を再送しない。
  行がコミットされていない状態で 200 を返すと、そのメッセージは永久に消える。
- 再送・重複は `platform_message_id` で潰す。`deliveryContext.isRedelivery` は
  監査用で、重複判定には使わない（メッセージ ID の方が確実）。
- worker が死んだ行は `recover_stale` がリースの期限切れで回収する。
- 送信は「chunk ごとに 1 行予約」。同じ chunk を二度送れない。
- LINE の push 再送キーは `outbound_id` から **uuid5 で決定的に導出**する。
  ランダムな uuid4 だと、クラッシュ後の再送が LINE から見て別物になり、
  409 による重複排除が効かず、**同じ答えが 2 通届いて 2 通分課金される**。

## 7. 公開面はこの 1 パスだけ

```
[WAN] :443
  → [router NAPT]
  → <PROXY_LAN_IP>:30443  (<PROXY_HOST>)
  → <PUBLIC_DOMAIN>/line/webhook
  → <PI_LAN_IP>:8790     (channel-gateway.service)
```

- Hermes API (8642) と Dashboard は**公開しない**。新しい受信ポートも開けない。
- Caddy には POST 以外を 405 で弾くガードを入れてある。
- 追加ブロックは適用前に**実機の `caddy validate` で検証済み**
  （`Valid configuration`、`/line/webhook` → 8790 の展開も確認）。
- **反映は `caddy reload` ではできない。** この Caddyfile は `admin off` を
  設定しており、admin API (localhost:2019) は接続拒否になる。実際に試すと
  `dial tcp [::1]:2019: connect: connection refused` で失敗する。
  反映はコンテナの再起動で行う（`RestartPolicy=unless-stopped`、
  compose project `<PROXY_PROJECT>` / service `caddy`）。
  再起動中は <PUBLIC_DOMAIN> 配下の全サービスが一瞬落ちる。

既存の `<EXISTING_LINE_DOMAIN>`（医薬祭の公式LINE取り込み口、`<EXISTING_LINE_ADDR>`）とは
**別の LINE チャネル**を使う。1 つのチャネルに設定できる Webhook URL は
1 つだけなので、既存チャネルの資格情報を流用すると
**既存の取り込みが無言で止まる**。

## 8. 資格情報の扱い

4 つとも同じ経路: systemd の `LoadCredential` が root として 0400 のファイルを読み、
unit 専用の tmpfs に複製を置く。サービスユーザーは `/etc/hermes-secrets` を
そもそも読めない。環境変数にもコマンドラインにも載らない
（`/proc/<pid>/cmdline` は誰でも読める）。

設定ファイルに資格情報を書くと**起動時エラー**になる。パーサが拒否する。

Gateway は Hermes の `.env` を読めない。`ProtectSystem=strict` は読み取りを
止めないので、unit に `InaccessiblePaths=/srv/hermes/hermes-data` を入れて
**そのディレクトリを存在しないことにしてある**。

## 9. このフェーズで見つけて直した実バグ

テストを書いたことで見つかったもの。すべて修正済み。

1. **redaction が無言で無効化されていた。**
   `security_patterns.py` は `src/` にあり `app/` の隣には無いため、
   `except ImportError: SECRET_PATTERNS = []` が発動して
   正準パターン 6 件が 0 件になっていた。秘密情報のマスクという
   セキュリティ制御が fail-open していた。
   → 探索パスを 2 箇所に広げ、見つからなければ**例外で落とす**ように変更。
   実測で 0 件 → 6 件に回復したことを確認。

2. **空の答えが空メッセージとして送信されうる。**
   `chunk_text("")` が `[""]` を返し、呼び出し側の `if not chunks` を
   すり抜けていた。プラットフォームは空メッセージを 400 で拒否する。
   → `[]` を返すよう修正。

3. **push 再送キーがランダムだった。**（§6 に前述）

4. **`recover_stale` の戻り値判定が常に真。**
   dict を返すのに `if recovered:` で判定していたため、
   何も回収していなくても 5 分ごとに警告ログが出ていた。

5. **`listen_host` のループバック限定が実環境と矛盾。**
   リバースプロキシは**別ホスト**にあるので、ループバック固定では届かない。
   → 「インターネットから到達可能でないこと」という本来の性質で書き直し
   （`is_global` を拒否、ワイルドカード `0.0.0.0` も拒否）。

6. **`hermes.base_url` が実機と食い違っていた。**
   Hermes は Docker の publish 指定により LAN アドレスにしかバインドされて
   おらず、サンプルの `127.0.0.1` では**全リクエストが接続失敗**する。
   E2E まで気づけず、「いま応答できません」だけが延々返る状態だった。

   ```
   LISTEN  <PI_LAN_IP>:8642        (0.0.0.0 でも 127.0.0.1 でもない)
   127.0.0.1:8642/health      -> URLError: Connection refused
   <PI_LAN_IP>:8642/health  -> 200 {"status": "ok", ...}
   ```

   値を直すだけでは同じことが繰り返されるので、**`--check` に到達性検査を
   追加**した。サンプルはパーサが拒否するプレースホルダを持たせ、
   「もっともらしい既定値」を継承できないようにした。

## 9.1 権限境界がクラッシュになっていた 2 件

どちらも「実機で `--check` を実際に叩いたから」見つかった。
テストでは出ない。**設計どおりに権限が働いた結果**がトレースバックだった。

```
PermissionError: [Errno 13] Permission denied:
  '/etc/hermes-secrets/channel-gateway-hermes-key'      ← 1 件目
  '/srv/hermes/channel-gateway/logs/gateway.log'        ← 2 件目
```

**1 件目**: `/etc/hermes-secrets` は 0700 root:root。サービスユーザーが
読めないのが正しい（systemd が root として読み、専用コピーを渡す）。
ところが Python 3.13 の `Path.is_file()` は `PermissionError` を握りつぶさず
伝播する。

→ `probe_secret()` が全てのファイル操作を包み、
`readable` / `present-but-unreadable` / `missing` を返す。
**後ろ 2 つは意味が正反対**なので区別する — `missing` は障害、
`unreadable` は手動実行時の正常な境界。
閉じたディレクトリでは存在しない名前も EACCES を返すため、
非 root では両者を**原理的に判別できない**。そう明記して root での
再実行を促す。

**2 件目**: `logs/` は 0750 サービスユーザー所有。`setup_logging` が
stderr より先にログファイルを開いていたため、**診断ツールがログを
書けないという理由で死んだ**。

→ stderr を先に、常に接続。ファイルは失敗したら 1 行警告して継続。
systemd 下では journal に全行残るので損失はない。
`run_check` の DB オープンも同様に包み、非 root では報告のみ、
root で開けない場合だけ本物の障害として扱う。

**この 2 件が示す一般則**:
権限境界が正しく働いた結果がトレースバックになってはいけない。
診断ツールが、診断対象の権限に負けたら意味がない。

## 9.2 本番起動でしか出なかった 2 件

### sqlite 接続がスレッドを越えていた

初回の本番起動で発覚。**これが一番危険な形の障害だった。**

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be
used in that same thread.
```

`worker` / `handle_health` / `process` のセッション取得 / `maintenance` が、
イベントループのスレッドで接続を作って `asyncio.to_thread` に渡していた。

webhook 受信だけは接続をスレッド内で作っていたため**正常に動いた**。
結果として:

- LINE には 200 を返し続ける（受理は成功し、再送もされない）
- worker は毎回死ぬので、**受信したメッセージは永久に処理されない**
- `/healthz` は 500

つまり「外から見ると健全、実際には何も処理していない」状態。
再送で救われることもない。

→ `_in_thread()` がスレッド内で接続を開き、1 回の store 呼び出しを実行し、
commit して閉じる。接続がスレッド境界を越える箇所はゼロ。
`sqlite3.Row` は値を保持するので、close 後も読める。

`check_same_thread=False` は**あえて使わなかった** —
チェックを黙らせるだけで共有が安全になるわけではなく、共有自体が誤りだから。

**なぜテストで出なかったか**: 既存テストはすべて store を同一スレッドで
同期呼び出ししており、スレッド処理が一度も実行されていなかった。
スレッド経路を通るテストを 6 件追加した。
修正前パターンが実際に例外を出すことも実測で確認している。

### 状態表示が無言で空を返していた

`gw-status.sh` は `sqlite3` コマンドに依存していたが、
**対象ホストに入っていない**。全セクションが空欄になり、
理由は表示されなかった — 運用者から見た「稼働中のゲートウェイの姿」が
静かに、もっともらしく間違っていた。

→ 実体を `app/gw_status.py` に移し、venv の Python（サービス本体が使うので
必ず存在する）で問い合わせる。読めない場合は理由と、
どのアカウントで実行すべきかを印字する。空なら「(なし)」と明示する。

あわせて、**配信結果をログに出していなかった**ことも直した。
最初の実メッセージの journal には `processing` しか無く、
「答えが届いたか」という最も重要な事実が残っていなかった。
いまは `delivered <id> platform=… mode=reply|push ok=… chars=…` を出す
（本文は出さない）。

実測（3 アカウントから `--check`、いずれも Traceback 0）:

| 実行者 | state db | 資格情報 |
|---|---|---|
| root | openable | `readable` / `missing` を判定できる |
| channelgateway | openable | `present-but-unreadable` |
| 一般ユーザー | not openable（報告のみ） | `present-but-unreadable` |

## 10. テスト

```
tests-gateway/  181 tests
  test_line_webhook.py   署名・イベント・正規化・UTF-16 長
  test_authority.py      owner 許可リスト・封筒の偽造不能性・リクエスト検疫
  test_store.py          重複排除・順序・並行度・クラッシュ回収・保持期間
  test_delivery.py       reply/push 判定・再送キー・分割・マスク
  test_config.py         bind 先・資格情報の拒否・到達範囲
  test_discord.py        Interface Bot アダプタ（DM 受理・ギルド無視・
                         ボット無視・非 owner・空本文・分割/マスク）
  test_service.py        Webhook ハンドラ（署名ゲート・サイズ制限・
                         非 owner の黙殺・DB 障害時の 500・行からの復元）
```

`test_service.py` は DB のコミット失敗を実際に注入して、
**200 ではなく 500 が返ること**を確認する。LINE は 200 を再送しないので、
ここを取り違えるとメッセージが恒久的に消える。

実行:

```
PYTHONPATH="src/channel-gateway/app:tests-gateway" \
  python3 -m unittest discover -s tests-gateway -p "test_*.py"
```

## 11. 実機での検証結果 (2026-09-04)

実際の LINE 公式アカウントと Discord Interface Bot で往復を確認した。

```
受信  discord  RESPONDED  1      送信  discord  dm    SENT  1
      line     RESPONDED  2            line     push  SENT  2
エラー: なし
```

| 項目 | 結果 |
|---|---|
| LINE 往復 | ✓ webhook → 署名検証 → キュー → Hermes → 配信(HTTP 200) → 監査 |
| Discord DM 往復 | ✓ 受信 11:14:00 → 配信 11:14:16（16 秒、mode=dm） |
| **intents 4096 で DM 本文が届くか** | **✓ 実証**（§9.3 参照） |
| 公開面 | `POST /line/webhook` → 403（署名なし）、`GET` → 405 |
| 既存の医薬祭取り込み | ✓ 無傷（`<EXISTING_LINE_DOMAIN>` → 403 のまま） |
| 権限制限 | ✓ シェル探索は `NO_SHELL_TOOL_FOUND`、ジョブ作成は `CANNOT_CREATE_JOB` |

### reply ではなく push になった

LINE の 2 件はどちらも **push（従量）**。2 件目は受信 22:50:27 → 応答 22:52:02 の
**95 秒**で、reply トークンの 60 秒を超えている。

事前測定（中央値 49.6s・最大 88.3s）どおりの結果で、
**実使用では大半が push になる**ことが確定した。
無料枠は月 200 通 ≒ 1 日 6.6 通。常用するなら有料プランを検討する材料になる。

## 9.3 Discord の DM が一度も処理されていなかった

```
AttributeError: module 'gw_store' has no attribute 'open_db'
   gw_discord.py   con = store.open_db(self.db_path)
```

`open_db` は `gw_schema` にある。**すべての DM がここで落ちていた。**
`on_message` が例外を握る設計なので、ボットは接続を保ったまま
ログを 1 行出すだけ — 外から見ると健全で、何も答えないサービスだった。

**出荷まで残った理由**: `gw_discord.py` には `_handle` を通すテストが
1 件も無く、純粋関数の `canonical_from_discord` しか覆っていなかった。
14 件追加し、**修正前は 5 件が失敗、修正後は全通過**することを実機で確認した。

**この失敗が同時に証明したこと**: 例外が出たのは空本文チェックの
**次の行**だった。つまり `canonical.text` は空ではなかった —
**intents 4096 で DM 本文は届く**。設計上で唯一残っていた
未検証の仮定が、皮肉にもこのバグによって実証された。

同種の誤り（モジュール違いの属性参照）が他に無いことは、
全ファイルの `alias.attr` を実際に import して解決し、確認済み（未解決 0 件）。
