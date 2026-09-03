# Phase D.1 — Discord Knowledge Ingestion

ユーザーが運営する2つのDiscord serverを、Hermes Personal Chief of Staff の
**read-only Knowledge Source** として統合するフェーズ。

Hermes から Discord へ送信する機能は作らない。Knowledge Bot は「耳」専用で
あり、「口」(将来の Discord Interface Bot) とは別 Application / 別 Bot /
別 Token を前提に設計している。

```text
Discord Guild A ─┐
Discord Guild B ─┤
                 ▼
       Discord Knowledge Bot        ← 正式Bot Application (self-bot不使用)
       View Channels / Read Message History のみ
                 │ outbound WebSocket のみ (新規inbound port = 0)
                 ▼
       Collector  (host systemd service / 専用user discordknowledge)
                 │  deterministic write only — LLMを呼ばない
                 ▼
       source DB  /srv/hermes/knowledge/discord/messages.db
                 │  index_queue (durable hand-off)
                 ▼
       Indexer    (operator cron)     ─────► status snapshot
                 ▼
       search index  /srv/hermes/hermes-data/knowledge-index/discord/index.db
                 │  Hermes container が見るのはここだけ
                 ▼
       discord_search / discord_get / discord_status   (stdio MCP, read-only)
                 ▼
              Hermes
                 ▼
       Event Layer (既存 events.db / source_type=discord)
```

---

## 1. なぜこの形なのか

### 1.1 Collector は Hermes の credential に触れない

既存の LINE Event classifier (`event_extract.py`) は
`docker compose exec -T hermes hermes -z ...` で分類を行う。つまり
**docker アクセス = Hermes の credential へのアクセス** である。

Collector が同じことをすると2つ壊れる:

- §86 — Gateway の heartbeat が LLM 応答待ちで止まる
- §69 — Collector が Hermes の secret に到達できてしまう

したがって Collector は **deterministic write only** とし、Event 分類は
別プロセス (`discord_event_extract.py`、operator 権限) に完全分離した。

### 1.2 host systemd service + 専用 user

§19 の第一候補どおり host systemd service を採用。理由は spec の列挙
(small footprint / gateway persistence / no inbound port / restart policy /
credential separation) に加えて、**実測した事実** が一つある:

operator アカウントは `docker` group に属している。つまり operator として
常駐させると、Collector プロセスは実質 root 相当の権限を持ち、
root 所有の token file も Hermes の secret も守れない。専用 system user
`discordknowledge`(docker/sudo/adm 非所属)にすることで初めて
§69 / §139 の境界が意味を持つ。

`systemd --user` (Linger=yes が有効なので技術的には可能) を採らなかったのも
同じ理由 — operator の権限を継承してしまう。

### 1.3 Indexer を Collector から分離した

`/srv/hermes/hermes-data` は operator 専用 (`drwx------`)。Collector を
そこへ書けるようにするには、Hermes 本体のデータディレクトリの permission を
緩める必要があり、それは §68 の境界を壊す。

そこで **Collector は source DB と `index_queue` にだけ書き、索引の更新は
operator 権限の Indexer が行う**。副次的な利点として §55 の「source は
更新されたが index は未更新」からの replay が自然に表現でき、
§87 の index lag が実測値として得られる。

### 1.4 LINE の `build_index.py` を再利用しなかった

LINE 側は「全件を一時ファイルへ再構築して `os.replace`」という full rebuild。
これは D.1 の要求を満たせない:

| 節 | 要求 | full rebuild では |
|---|---|---|
| §55 | pending queue から replay | queue が無い |
| §56 | 同一更新の複数適用で重複しない | 毎回作り直すので冪等ではあるが O(全件) |
| §57 | edit で旧本文が検索から消える | 全再構築なら消えるが増分では不可 |
| §58 | delete で本文だけ index から除去 | tombstone を表現できない |

よって Discord の Indexer は **意図的に増分方式** とした。これは LINE からの
逸脱であり、欠落ではない。

一方 **検索の作法は LINE から素直に再利用** した — `mode=ro` 接続、
`len(query) >= 3` で FTS5 trigram、それ未満と 0 件時は LIKE fallback、
`_like_escape` / `_parse_bound` / `_clamp_int`、本文を出さない stderr ログ。

---

## 2. 構成要素

### 2.1 Collector 側 (`/srv/hermes/discord-knowledge/`)

| file | 役割 |
|---|---|
| `app/dk_config.py` | config 読み込みと検証。allowlist 判定 (default deny)。token 取得順序 |
| `app/dk_schema.py` | source DB / index DB の canonical schema、`source_id_for`、additive migration |
| `app/dk_normalize.py` | raw Discord payload → canonical record (純関数、I/O 無し) |
| `app/dk_adapter.py` | library object → raw payload dict (duck-typed、discord import 無し) |
| `app/dk_store.py` | 唯一の書込層。upsert / 順序判定 / tombstone / index_queue |
| `app/dk_backfill.py` | bounded・resumable な履歴取得 |
| `app/collector.py` | Gateway client 本体。intents / health / 単一writer / backpressure |
| `app/discovery.py` | 初期設定用の guild/channel 一覧 (本文は取得しない) |
| `app/indexer.py` | index_queue を drain し索引へ適用 + status snapshot 発行 |
| `bin/deploy.sh` | operator 権限での配備 (root 不要) |
| `bin/index-tick.sh` | cron から呼ぶ indexer 起動 (flock 付き) |
| `bin/dk-status.sh` | operator 側の状態表示 (本文・secret を出さない) |
| `config/config.yaml` | allowlist と各種上限。**token は絶対に入れない** |
| `systemd/discord-knowledge.service` | unit 定義 |
| `install-root.sh` | root が一度だけ実行する冪等インストーラ |

### 2.2 Hermes 側

| path (host) | container | 役割 |
|---|---|---|
| `hermes-data/tools/discord-knowledge/discord_mcp.py` | `/opt/data/...` | stdio MCP (read-only 3 tools) |
| `hermes-data/tools/events/discord_event_extract.py` | — | Event 抽出 (operator 権限の batch) |
| `hermes-data/knowledge-index/discord/index.db` | `/opt/data/...` | 検索索引 |

`hermes-data/config.yaml` の `mcp_servers` に `discord_knowledge` を追加済み
(5行の挿入のみ。既存3サーバーの定義は byte 単位で不変)。

### 2.3 Hermes container から見えないもの

`docker-compose.hermes.yaml` の volume は
`hermes-data:/opt/data` と `bridge-sock:/hermes-coding` の2つだけで、
**`/srv/hermes/knowledge` は mount されていない**。したがって source DB は
Hermes から物理的に到達不能であり、Hermes が見るのは索引だけ。
Bot Token は container の environment にも `.env` にも存在しない。

---

## 3. 設計上の要点

### 3.1 二重 allowlist と default deny

Discord 側の権限境界(2 Guild にしか招待しない / View Channels +
Read Message History のみ)と、Collector 側の allowlist の二重制御。

allowlist が空・placeholder のままなら **何も収集しない**。
「見えるものを全部集める」へ広がることは構造的に起こらない:

- `GuildScope.active` は `enabled and channels` の両方を要求する
- `Config.ready_to_ingest` が偽なら Collector は **接続すらせず** exit 78
- exit 78 は `RestartPreventExitStatus` に登録済みなので再起動 loop にならない

thread は「親 channel が allowlist にあれば継承」。継承は
`include_threads: false` で無効化できる。

### 3.2 source_id

```text
discord:<guild_id>:<channel_id>:<message_id>
```

message_id 単体で global unique だが provenance を明示するためこの形を採る。
Event Layer 側も同じ id をそのまま使うので、`event_id_for` の sha1 が
LINE と衝突しない。**この形式は今後変更しない。**

（既存 LINE extractor は `events.source_id` に prefix 無しの生 message_id を
入れている。README の記述とは異なるが production の事実なので、そこは
触らず Discord 側だけ full id を使う。）

### 3.3 raw / normalized / search_text

| column | 中身 |
|---|---|
| `content` | Discord が送ってきたそのまま。表示と引用はこれ |
| `content_normalized` | mention と custom emoji を可読化したもの |
| `search_text` | `content_normalized` + bounded embed text。索引が食うのはこれ |

mention の解決は payload 自身の `mentions` 配列から行うので、
member cache も `GUILD_MEMBERS` intent も要らない。
未知の channel mention は id のまま残す(推測しない)。

### 3.4 編集順序 (§38 / §105)

Discord に revision counter は無いが `edited_timestamp` は message ごとに
単調で、original には存在しない。両者を UTC ISO-8601 に正規化すれば
文字列比較がそのまま世代比較になる。

```text
incoming_generation < stored_generation  →  skipped_stale
```

これにより「gateway が届けた edit を、それ以前に発行された backfill page が
上書きする」事故が起きない。

partial payload (`MESSAGE_UPDATE` が変更分しか運ばない場合) は
`normalize_message(..., partial=True)` が **payload に存在した key だけ** を
返すので、NULL 上書きが構造的に起こらない。

**「完全な payload かどうか」の判定を間違えると、この防御は無効になる。**
当初は `content` の有無だけで判定していたが、`content` はあるが `author` が
無い MESSAGE_UPDATE がありうる。それを完全とみなすと
`normalize_message(partial=False)` が `author_id=None` を含む全列を返し、
既知の発言者を消してしまう。判定は

```python
partial = not all(k in data for k in ("author", "type", "content"))
```

とし、partial なら REST で完全な message を取り直す。取り直せなければ
partial のまま適用する(取れなかったことで編集自体を失わない)。
`dk_store` 側にも backstop として `author_id` / `author_name` を
「payload の None では消さない列」に入れてある。
テストは handler → writer → 行の継ぎ目を跨いで検証する。

registry 由来の列 (guild_name / channel_name / channel_type / thread_*
/ parent_channel_id / created_*) は、Collector が知らないだけの `None` で
既知の値を消さない。

### 3.5 削除の意味論 (§39-§42)

削除時に残すのは identity と時刻だけ:

```text
source_id / message_id / guild_id / channel_id / created_at / deleted_at
```

本文・attachment metadata・embed・search_text は source DB からも消し、
Indexer が FTS 行を削除する。`discord_get` は
`content_state: "deleted"` の tombstone を返す。

**削除は terminal**。後から来た payload が本文を復活させることはない
(`skipped_deleted`)。逆に「一度も取り込んでいない message の削除」も
tombstone を残すので、遅れて届いた backfill page が復活させることもない。

そして **message の削除は obligation の取消ではない**。由来 message が
消えても Event を自動で resolved / ignored / cancelled にしない。

### 3.6 Gateway を止めない (§84 / §86 / §87)

```text
gateway event → allowlist 判定 → normalize → bounded queue → return
```

単一の writer task が queue を drain し、SQLite 適用は
`asyncio.to_thread` で worker thread に出す。したがって disk I/O も
遅い batch も event loop (= heartbeat) を止めない。

queue は `maxsize=5000` の bounded。満杯時は **落とさず待つ**
(backpressure)。深さと high-water mark は status に出る。

raw event (`on_raw_message_edit` / `on_raw_message_delete` /
`on_raw_bulk_message_delete`) を使うので、プロセス起動前の古い message への
編集・削除も取りこぼさない。

### 3.7 health は「最後の自己申告」であって生存確認ではない

`collector_health` / `gateway_connected` は Collector 自身が 30 秒ごとに
書く値を Indexer がそのまま転記したもの。Collector が死んでも Indexer は
最後の値を publish し続けるので、そのままでは **死んだ Collector が
永久に CONNECTED に見える**。

そこで Collector の `updated_at` も転記し(`collector_state_updated_at`)、
`discord_status` がその経過秒数と `collector_state_stale` を返す。
publish 間隔 30 秒に対し 120 秒(3回分の取りこぼし)を超えたら stale。
値が無い/壊れている場合は `None` を返す — **「不明」を healthy と
読み替えない**。

AGENTS.md に「`collector_health` が異常なら『Discord に無い』と断定せず
収集の状態を先に述べる」と書いた以上、health が実際に劣化しうることが
必要だった。実測: 本番索引に対して `collector_state_stale: true`,
`collector_state_age_seconds: 1889`(Collector 未起動のため)。

### 3.8 credential

```text
1. systemd LoadCredential  →  $CREDENTIALS_DIRECTORY/discord-knowledge-token
2. fallback: /etc/hermes-secrets/discord-knowledge-token (root:root 0400)
```

**token を受け取る CLI option は存在しない**。`/proc/<pid>/cmdline` は
world readable なので、argv 経由の受け渡しは §15 / §96 を静かに破る。
config file に token を書くと `parse_config` が拒否する。

---

## 4. 実測 (measured, not assumed)

すべて Pi 実機 (Debian 13 arm64 / Python 3.13.5 / SQLite 3.46.1 host,
3.53.4 container) 上での計測。

### 4.1 intents

```text
discord.Intents: guilds + guild_messages + message_content = 33281
members=False presences=False typing=False dm_messages=False
guild_reactions=False voice_states=False
```

`33281 = 1 + 512 + 32768` — §7 の目標と完全一致。

### 4.2 systemd sandbox で DNS/TLS が通ること

`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` +
`SystemCallFilter=@system-service` を `systemd-run` で再現して実測:

```text
getaddrinfo: ('162.159.130.234', 443)
tls ok: TLSv1.3
```

glibc の netlink による interface 探索は失敗しても素直に degrade するため
**AF_NETLINK は不要**。推測ではなく計測結果。

### 4.3 日本語検索と短クエリ

trigram tokenizer は 3 文字未満をそもそも索引化しない。実測:

| query | FTS5 trigram | LIKE fallback |
|---|---|---|
| `打ち合わせ` (5) | hit | — |
| `来週の` (3) | hit | — |
| `水曜` (2) | **0件** | hit |
| `OK` (2) | **0件** | hit |
| `水` / `O` (1) | **0件** | hit |

日本語では `水曜` `会議` `予定` のような2文字が普通の検索語なので、
LIKE fallback は例外処理ではなく **常用経路**。MCP 側で
`len(query) >= 3` のゲートと 0 件時 fallback の両方を持つのはこのため。

### 4.4 メモリ

```text
discord.py + collector 全モジュール import 直後: RSS 46.1 MiB
unit の MemoryMax: 400M
Pi 全体: 3795 MiB 中 1134 MiB 使用 / 2661 MiB available (Collector 停止時)
```

常駐時の実測 RSS は Collector を起動できていないため **未計測**。

### 4.5 storage baseline

```text
source DB : 102,400 bytes (空、schema のみ)
index DB  :  57,344 bytes (空、schema のみ)
```

---

## 5. テスト

| suite | 実行環境 | 件数 | 対象 |
|---|---|---|---|
| `test_normalize.py` | Pi host python3 | 22 | §100 正規化 / §107 provenance / §109 injection |
| `test_allowlist_store.py` | Pi host python3 | 33 | §101 allowlist / §102 dedupe / §105 順序 / §39-42 削除 / §44 backfill |
| `test_pipeline.py` | Pi host python3 | 19 | §55-58 索引 / §103 edit / §104 delete / §106 検索 |
| `test_collector.py` | Pi venv (discord.py 必要) | 17 | §7/§35 intents / §36 create経路 / §37 partial判定 / §39-42 delete経路 / §84-87 writer・排出 |
| `test_mcp_container.py` | **Hermes container の実 mcp** | 32 | §59-67 tool surface / §106 / §107 / §137 / health の陳腐化検出 |
| **合計** | | **123** | host 91 + container 32 |
| indexer 検証 (agent) | Pi | 77 checks | crash replay / 冪等性 / status snapshot |
| event extractor 検証 (agent) | Pi | 63 checks | §75-83 候補判定 / actor / 冪等性 / 人間の決定の保護 |

MCP のテストは **stub ではなく container の実 `mcp` パッケージ** で走らせて
いる。フィクスチャ索引も手書き SQL ではなく
normalize → source DB → index_queue → indexer という本番経路で生成した。

回帰:

```text
F.2 coding subsystem : 129 tests OK (無変更)
LINE / Event / Jobs  : MCP 経由で status 応答を確認
```

---

## 6. Spec からの逸脱と理由

| 節 | 逸脱 | 理由 |
|---|---|---|
| §53/§54 | 索引を LINE の full rebuild ではなく増分方式にした | §55-58 が full rebuild では実装不能。LINE 側は無改造 |
| §16 | LoadCredential を第一候補、root所有 file を fallback として **両方**実装 | 環境で LoadCredential が使えるので第一候補だが、手動起動時の経路も要る |
| §71 | `events.source_id` に full prefixed id を使う (LINE は生 message_id) | 既存 LINE 行の形式は production の事実なので触らない。Discord 側だけ prefix 付き |
| §32 | embed の URL / image を保存しない | 「必要な text metadata だけ」の最も保守的な読み。title/description/fields/author/footer の text のみ |

---

## 7. 本番稼働と実測結果

2026-09-03 に credential gate を通過し、本番稼働に入った。実測サマリと
Acceptance Criteria の検証結果は `docs/D1-STATUS-REPORT.md` を参照。

要点:

```text
収集範囲   : 3 Guild / 116 channel (仕様は2 Guild — ユーザー判断による逸脱)
取り込み   : 1,685 件 (うち thread 由来 259)、本文あり 92.9%
backfill   : 165/165 channel complete、429 応答 0、14日窓を取得時点基準で厳守
Event      : 候補 85 → LLM 85 call → Event 66 件。actor 判定 66/66 一致
collector  : cgroup memory 43.0 MiB / 上限 400 MiB、swap 0、再起動 0
テスト     : host 94 + container 32 = 126、実データ不変条件 23/23
```

## 8. 配備後の敵対的監査で判明した設計上の穴

配備完了後に 6 レンズの read-only 監査を行い、各所見を別エージェントに
反証させた。設計として記録しておく価値があるのは次の2点。

### 8.1 forum を allowlist しても履歴が 1 件も入らなかった

`ForumChannel` には `history` が無い(Discord の forum は投稿そのものを
持たず、各投稿はスレッド)。`backfill_channel` は `hasattr(channel,
"history")` で早期 return していたが、**エラー文字列を生成した直後に
return** しており、`submit` にも `log` にも到達していなかった。

結果として:

- 17 の allowlisted forum に `backfill_state` 行が無い
- `last_error` も無く、log 行も無い
- status が公開する `allowed_channels` は 116、`backfill_state` は 103 で、
  差分 13 の説明がどこにも無い

つまり **「完全・エラー0の backfill」と見分けがつかなかった**。
Forbidden 分岐は `submit` していたので、記録する意図はあったのに
この分岐だけが漏れていた。

修正は2つ:

1. 全ての早期 return を `_record()` 経由にし、必ず state と log を残す
2. `backfill_all` が allowlisted channel 配下のスレッド(active + archived、
   public/private)を列挙して backfill する。1 parent あたり 50 件が上限

修正後、**スレッド由来 259 件**を新規取得し、未記録の channel は 17 → 0、
forum 17ch は `container_channel` として理由付きで記録されるようになった。

**教訓**: 「何も起きなかった」と「何も記録しなかった」は status 上で
区別がつかない。エラーを計算しておいて捨てる分岐は、沈黙よりも悪い。

### 8.2 メッセージ数の probe ではステータス板を検出できない

収集範囲を決めるとき、channel ごとの流量を実測して bot 中継を除外した
(単一 channel が 1,566 件/日 = 全体の 86%)。しかしこの手法では
**「メッセージは数件だが bot が同じ行を編集し続ける」ステータス板**を
取りこぼす。件数上は最も静かな channel に見える。

起動2分で同一行が revision 2 に達するのを実測して初めて発見し、除外規則を
「人間の発言が0で、名前に status を含む channel」まで拡張した。

**教訓**: 流量を測るなら、作成だけでなく更新も測る必要がある。

## 9. 既知の限界

- 常駐の長期実測はまだ無い(稼働数時間規模)
- allowlist 内の古いメッセージが編集されると、backfill の 14日窓の外でも
  取り込まれる。編集は「今その内容が使われている」証拠なので取り込むのが
  正しいと判断したが、corpus が窓の外へ広がりうる点は事実として記録する
- forum スレッド列挙は 1 parent あたり 50 件が上限
- reaction / voice / attachment binary は D.1 のスコープ外
- 監査所見のうち 4 件は実行上限により未検証
