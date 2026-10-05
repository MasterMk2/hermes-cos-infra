# AgentricAI → Hermes: 要件統合と次の実装順序

初回照合: 2026-10-04。現行コード追随: 2026-10-05。機能全体の完成報告ではなく、重複開発を止めるための設計統合。

## 決定と今回の範囲

AgentricAI の新規開発を Hermes に一本化する。実装・要件差分の正本は
`MasterMk2/hermes-cos-infra`、Pi の運用記録は `MasterMk2/hermes-pi-setup` に置く。
AgentricAI は原要件と設計判断を参照する入口として残す。リポジトリの削除・改名・
archive、既存Botの権限変更、データ移行、配備はこの変更に含めない。

- AgentricAI の独立した W1〜W12、別Bot作成、Postgres基盤の新設をそのまま再開しない
- Hermes の既存collector、Gateway、commitments、Attention、Planningを先に照合する
- 元要件の独自価値は「出典に遡れる記憶」「振り返り」「人が理解できる成長ログ」
- 既存の個人秘書機能を、コミュニティ自律参加まで実装済みとは呼ばない
- この文書は I.3 開始、追加支出、新しい収集/送信先、認証/権限拡張の承認ではない

## 1. 調べた実体と証拠の限界

| 対象 | 固定したrevision | 確認内容 |
|---|---|---|
| AgentricAI | [64a6e1a](https://github.com/MasterMk2/AgentricAI/commit/64a6e1aa7449f8f571b9e60baa2a7eb714c86985) | srcはversionのみ、テストはimportのみ。要件定義0.3のBot/記憶/Reflectionは実装済みではない |
| Hermes infra | [0445c7c8](https://github.com/MasterMk2/hermes-cos-infra/commit/0445c7c8b7da7b52553eccf3f156b0882e6a8438) | 既存collector/commitments等と、PR #3〜#5のGateway・Attention・Planning送信境界修正 |
| Hermes Pi記録 | [f3b378e](https://github.com/MasterMk2/hermes-pi-setup/commit/f3b378eb6a39e721ef6e1d585ec7e76e35b313b6) | I.2と後続ローカル作業の過去の報告。現在のPi実測ではない |

[AgentricAI原要件0.3](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md)の第5章・第6章を全IDで追跡する。
GitHubにないHermes本体の記憶実装、Piの実行状態、9月10日記録の後続ローカル変更は
未確認。「証拠なし」は「どこにも実装がない」という断定ではない。
既存のI.2を作り直さず、[状況照合の正本](STATUS-RECONCILIATION.md)と
[Pi記録の照合PR](https://github.com/MasterMk2/hermes-pi-setup/pull/1)の残件を先に確認する。
初回基準 `640d9db` と当時の595件等は状況照合の履歴欄に残す。以下の1,109件は別のSHA・3-suiteの検証であり、統合全要件の合格数ではない。

### 送信境界で今回までに確認できたこと

- [PR #3](https://github.com/MasterMk2/hermes-cos-infra/pull/3) は proactive / Attention / Planning の結果不明を永続化して保留する
- [PR #4](https://github.com/MasterMk2/hermes-cos-infra/pull/4) は通常Discordの固定chunk・宛先と確認済みreceiptを保存する。UNKNOWNが一つでもあれば返信全体を保留し、確認済み先頭＋確実な未送信tailだけを継続する。Morning BriefのIN_FLIGHT/本文不一致を成功にしない
- [PR #5](https://github.com/MasterMk2/hermes-cos-infra/pull/5) は通常LINEの固定batch・宛先・reply/push mode・件数を保存する。reply開始後の不明結果から自動pushしない。409は有効期間内の固定push intentと受理IDがある場合だけ受理として扱う
- [0445c7c8 の offline CI](https://github.com/MasterMk2/hermes-cos-infra/actions/runs/37247078869) は Ubuntu 24.04 / Python 3.12.14・3.13.15 のそれぞれで Gateway 344 + Attention 158 + Planning 607 = **1,109 passed**、別に **1,188 subtests passed / 3 skipped**。3件の外部private denylistは未検証。再現入口は [OFFLINE-TESTS.md](OFFLINE-TESTS.md)

これはFR-AC-03/05/07の部分的証拠を追加する。68要件の状態ラベルは変更しない。
FR-IN-06/NFR-02は受信側の要件であり、この送信側修正で完了とはしない。
UNKNOWNの手動reconcile/UI、端末到達/既読追跡は未実装。SENTはprovider受理のみ。
SDK内部retryを含めたexactly-onceは保証せず、再起動やretryで認可は増えない。
実機への配備・実DB移行・現在の自然配信は未確認。旧版への状態DB互換を確かめずrollbackしない。
記憶契約、Reflection、bounded adaptation、コミュニティ参加判断の残件は変わらない。

## 2. 再利用するものと、同一ではないもの

| 領域 | 既存の再利用候補 | 残す差分 |
|---|---|---|
| Discord取り込み | D.1 allowlist、編集/削除、thread/返信/添付メタデータ | 受信から永続化までの障害窓、derived記憶への削除伝播 |
| 検索と出典 | Discord FTS5とsource ID、commitment evidence | 6種の記憶、ベクトル検索、observed/derived/declaredの全制約は別 |
| 通信 | 既存Discord DM/LINEのowner限定処理・返信と重複抑止 | guild発言、リアクション、第三者宛返信は既存の権限外。非owner Discord DMの記録・保持は下記参照 |
| 通知判断 | Attentionの通知ポリシーと抑止、LINE通数予算 | コミュニティ3段triage、0〜5自律レベル、LLM/API金額台帳は別 |
| 日次出力 | Planningの事実ベースMorning Briefとgrounding | Reflection、strategy更新、関心履歴、成長ログは別 |
| 安全/監査 | source/request ID、送信先固定、mention抑止、secret redaction、隔離 | 新記憶のscope、遅延注入、削除、提案の版管理までの全経路を検証する |

朝ブリーフは「今日すべきこと」、成長ログは「何を学び、何を変える候補にしたか」。
同じ日次処理という理由だけで統合完了にしない。通知も別々に増やさず、将来必要なら
既存通知の重複抑止・本人宛配送を使う。ただし今回、新しい通知は有効にしない。

## 3. 統合しても動かさない境界

### Discordコミュニティと本人向け秘書は別の公開範囲

`collector.py` は読み取り専用。`gw_discord.py` の処理・返信は owner のDMに限定され、
guild intentを持たず、`AllowedMentions.none()`を使う。`gw_proactive.py` はrecipientを指定できない。
ただし **非owner DMを保存しない境界ではない**。現行 `_handle` は認可判定の前に
`record_inbound` を呼び、非owner DMの送信者ID・会話ID・本文もSQLiteへ保存してから、
queue投入・Hermes処理・返信を拒否する（E12）。既存 `retention.days` は既定14日で、
maintenanceの `purge_expired` が期限超過の本文/tokenを消すが、重複抑止用のID行は残る。
実機での設定値・purge実行・第三者データの有無は未確認。第三者記録の最小化、
IDの保持期間と削除要求への対応はR1の未決契約であり、今回の文書で適切さを承認しない。
AgentricAI のUC-02/UC-03（会話への参加/リアクション）を実現するためにこれらを緩めない。
個人秘書を先行させ、コミュニティ参加を残すかはR4の判断事項とする。採用するとしても、
対象、参加者周知、データ/権限分離、停止方法を別レビューするまで無効のまま。

### 記憶と学習の不変条件

原要件§10.3、§13.6〜13.7、§16〜17の次を維持し、新経路には合成テストを要求する。

1. observed / derived / declaredを混同しない。derivedは有効なobserved出典必須。
   自己生成物だけの再学習や、出典が失効した派生物の再利用を認めない
2. 人格・不変ルールは人間のみが変更する。ReflectionはSystem Promptや自分のコード、
   配備設定、権限、ツールallowlistを書き換えない
3. 関心/関係性の数値は決定論的なbounded update、減衰、最小証拠数、履歴を持つ。
   trustや親しさは認可・scope・承認条件を変えない。属性/心理プロファイリングは持ち込まない
4. public/guild/channel/dm/operatorは読み出しと出力の両方で制限する。
   個人秘書のGmail/LINE/予定情報をコミュニティ記憶や成長ログへ混ぜない
5. 外部テキストと記憶内のテキストはUntrusted Data。権限判定は決定論的・fail-closed。
   データが自らを承認者と名乗っても、認可された本人にはならない
6. 原文保存の要件は削除要求の例外を含む。削除/tombstoneから派生物、索引、キャッシュ、
   監査表示、バックアップ保持までの影響を明示する。開発用の会話を本番記憶へ移さない
7. 元SRSのDangerousは承認しても実行しない。§7.1 Level 4の「承認必須」という記述より
   §17の禁止境界を優先する。Hermesの既存隔離coding workerを、SNS/Reflectionに開放しない

### 旧構成を無条件には移植しない

- PostgreSQL+pgvector/Alembic、APScheduler、Windows Dockerという旧構成は、
  HermesのSQLite、既存systemd、権限分離を置き換える根拠ではない
- 単一の製品/意思決定入口を保つことと、分離済みプロセス・資格情報を一体化することは別
- 旧月額3,000〜5,000円はAgentricAI単体の設計制約として引き継ぐ。Hermesとの合算範囲、
  契約/モデルの実単価は未確定。LINE通数予算をLLM費用上限の実装済み証拠にしない
- 元要件のX/LINE/Discord仕様・料金の古い記載は現在値として採用しない。
  使う前に提供元のAPI文書/実契約を再確認する。今回API仕様や権限は変更しない

## 4. 一本化したロードマップ

R番号はこの統合の作業順を指す。Hermesの既存Phase番号を振り直さない。
「完了」はこの文書で実装済みに読み替えない。各実装PRで証拠を追加して更新する。R0の送信境界修正はR1の記憶契約を完了させるものではない。

| 順 | 作業/状態 | 完了条件と依存 |
|---|---|---|
| R0 | 基盤の現状照合。文書統合と送信境界修正はコード反映済み、運用残件は未完了 | 後続ローカルworktree/差分を安全に回収しGitHubと比較。既存Hermes本体の記憶能力も調査。自然な朝の実配信とI/O/起動の証拠を別々に記録 |
| R1 | 記憶・出典・scope契約。設計/未実装 | 既存source IDを使う最小schema/adapter案。出典なし/失効出典、scope越境、削除伝播、クラッシュ再実行、遅延注入を合成データで検証。既存非owner DM記録の最小化・ID保持も決める。保存方式は測定後に判断。R0のコード実体確認に依存 |
| R2 | 読み取り入力から振り返り候補と成長ログ。未実装 | R1の検証済みデータだけで、日本語の事実/解釈/変更候補/根拠/保留を区別する。まずoffline・無送信・無自動適用。相関ID、用途別費用、上限超過時縮退をテスト |
| R3 | bounded adaptationと整理。後続、未実装 | R2のレビュー後にstrategyのprobation/active/retired、版とrollback、declared優先を実装。数値更新は決定論。4週間相当の合成入力で急変/証拠不足を検証し、実データ評価は別の承認済み段階にする |
| R4 | コミュニティ参加の要否判断。保留 | 個人秘書のみか、同一製品内に分離したcommunity用途も残すかを決める。後者は対象/周知/自律レベル/停止/予算/安全境界の設計承認が先。第三のBotを今作らない |
| D1 | X、OpenTelemetry、高度UI等。延期 | 価値、費用、提供元のAPI文書を再確認した個別要件があるときのみ。原要件のPhase 2/3をそのまま開始しない |

元W1/W2の汎用基盤・通信を重複実装する代わりにR0で照合し、W3をR1、W4/W5をR2/R3、
W6/W7の安全/監査/費用を各段の合格条件へ移す。W8以降のコミュニティ投入はR4で保留。
原要件ではFR-RF-04はMVP、末尾の最小構成ではConsolidationはPhase 2となっている。
今回はその差を勝手に「不要」にせず、R3の順序決定時に解消する。

## 5. 受け入れ条件を引き継ぐ

| 原要件の評価 | 統合後の扱い |
|---|---|
| A2/A5 | 人物差と関心の滑らかさはR3で合成評価。本人秘書用途に合う評価対象を先に定義 |
| A6 | R2から実単価・用途別費用を測る。新しい費用枠を自動承認しない |
| A7/T1〜T11 | 直接/間接/遅延注入、タグ脱出、scope越境、属性推測、秘密漏洩、spam、SSRF、trust、なりすましを新経路に対して実行 |
| A9/A10 | 切断復旧・耐久性・停止/降格は隔離環境で検証。既存本番の強制切断/再起動を代用にしない |
| A1/A3/A4/A8 | 実会話での長期想起、妥当な方針変化、実際の行動変化、うるささは運用評価。合成試験や朝ブリーフ合格で代替しない |

UC-01〜06は取り込み/通信/想起に対応し、コミュニティ発言はR4依存。
UC-07〜10はR2/R3、UC-11〜14はR1/R3/R4、UC-15〜18は各段の安全/障害試験に残す。
ゲート1（機能・安全）とゲート2（実運用4週間）は混同せず、今回どちらも合格扱いにしない。

## 6. 全要件の対応表

[機械可読版](AGENTRICAI-TRACEABILITY.json)が原文要件、行固定の出典、対象トラック、
既存コードへのリンク、残件を保持する。対象はFR 56件、
NFR 12件、計68件。分類は reuse_candidate: 4、partial: 25、deferred: 3、unverified_gap: 29、decision_required: 7。
reuse_candidateも「全要件達成」ではなく、既存経路を確認して使うという意味。

| ID | 分類 | 次の場所 | 未完了条件 |
|---|---|---|---|
| [FR-IN-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L248) | reuse_candidate | R0 | 既存の許可済み読み取り専用collectorを再利用する。新たな収集範囲・Bot権限は追加しない。現在の稼働は別途確認する。 |
| [FR-IN-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L249) | partial | R1 | 既存の正規化モデルはあるが、AgentricAIのeventsスキーマと同一ではない。source IDからの対応契約を定義する。 |
| [FR-IN-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L250) | partial | R1 | Discordの編集・削除/tombstone経路はある。新しいderived記憶までの削除・失効伝播は未検証。 |
| [FR-IN-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L251) | reuse_candidate | R0 | 既存の許可済み読み取り専用collectorを再利用する。新たな収集範囲・Bot権限は追加しない。現在の稼働は別途確認する。 |
| [FR-IN-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L252) | partial | R1 | 添付・embedメタデータはある。URL専用抽出と新しい記憶への引用形を確認する。本文の自動取得は有効にしない。 |
| [FR-IN-06](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L253) | partial | R1 | allowlist先行とキュー/永続化はあるが、collectorのメモリキューを含むため受信直後の全クラッシュ耐久性を達成済みとしない。PR #3〜#5の送信側再起動安全性を、この受信側要件の達成証明にしない。 |
| [FR-IN-07](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L254) | deferred | D1 | X/公開SNSまたはOpenTelemetryは後続候補。利用価値、提供元のAPI文書、費用、対象範囲を確認するまで追加しない。 |
| [FR-IN-08](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L255) | reuse_candidate | R0 | 既存LINE署名検証と正規化を再利用する。許可済みowner経路に限定し、LINE全体の収集へ拡張しない。 |
| [FR-IN-09](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L256) | reuse_candidate | R0 | 既存の許可済み読み取り専用collectorを再利用する。新たな収集範囲・Bot権限は追加しない。現在の稼働は別途確認する。 |
| [FR-DE-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L262) | unverified_gap | R4 | Attentionの本人向け通知判定はコミュニティ参加の3段triageではない。公開先の必要性を決めてから別途適合性を検証する。 |
| [FR-DE-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L263) | partial | R4 | 既存通知の判定ログ、抑止、上限を再利用候補とする。コミュニティ全体/会話単位の発言制御の証明ではない。 |
| [FR-DE-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L264) | unverified_gap | R4 | Attentionの本人向け通知判定はコミュニティ参加の3段triageではない。公開先の必要性を決めてから別途適合性を検証する。 |
| [FR-DE-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L265) | decision_required | R4 | 元の0〜5レベルをHermesの既存権限と同一視しない。本人通知とコミュニティ自発行動を分けた停止/降格の意味を先に決める。 |
| [FR-DE-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L266) | partial | R4 | 既存通知の判定ログ、抑止、上限を再利用候補とする。コミュニティ全体/会話単位の発言制御の証明ではない。 |
| [FR-DE-06](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L267) | unverified_gap | R4 | Attentionの本人向け通知判定はコミュニティ参加の3段triageではない。公開先の必要性を決めてから別途適合性を検証する。 |
| [FR-AC-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L273) | decision_required | R4 | Knowledge Botは読み取り専用、Interface Botは本人DM専用。guild送信/リアクションを既存Botに追加しない。 |
| [FR-AC-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L274) | partial | R1 | mention抑止、redaction、grounding等はあるが、元のP1〜P11全体を満たす証明ではない。新しい記憶/行動経路はfail-closedの検証が必要。 |
| [FR-AC-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L275) | partial | R1 | PR #3〜#5で既存owner配送の固定intent・結果記録・UNKNOWN保留・プロセス境界/並行claim/receipt失敗を合成検証済み。Reflectionとの全経路相関、手動reconcile、実機評価は残る。SENTはprovider受理のみ。SDK内部retryを含む全媒体exactly-onceは保証しない。 |
| [FR-AC-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L276) | decision_required | R4 | 元SRSのDangerousは禁止のまま。Hermesの隔離coding worker権限をコミュニティ/Reflectionへ継承させない。一般的な承認キューと同一視しない。 |
| [FR-AC-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L277) | partial | R1 | PR #3〜#5で既存owner配送の固定intent・結果記録・UNKNOWN保留・プロセス境界/並行claim/receipt失敗を合成検証済み。Reflectionとの全経路相関、手動reconcile、実機評価は残る。SENTはprovider受理のみ。SDK内部retryを含む全媒体exactly-onceは保証しない。 |
| [FR-AC-06](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L278) | deferred | D1 | X/公開SNSまたはOpenTelemetryは後続候補。利用価値、提供元のAPI文書、費用、対象範囲を確認するまで追加しない。 |
| [FR-AC-07](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L279) | partial | R0 | 本人向けLINEは固定batch/modeを保存し、reply開始後の不明結果から自動pushしない経路を合成検証済み。到達/既読追跡・現在の実機評価は未実装/未確認。任意の第三者宛送信は対応済みに含めない。 |
| [FR-AC-08](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L280) | decision_required | R4 | 元SRSのDM原則禁止とHermesの本人DM主経路は用途が異なる。既存owner通信は維持し、第三者DMは新たに許可しない。 |
| [FR-ME-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L286) | partial | R1 | 原情報の保存はあるが全記憶の無期限保存を新たに保証しない。保持期間、削除要求、派生物の失効を同時に定義する。Discord非owner DMも既存Gatewayに本文/IDを保存する。本文の期限削除後もID行が残るため、第三者記録の最小化と保持/削除契約は未決。 |
| [FR-ME-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L287) | unverified_gap | R1 | 既存source/evidence参照を出発点とし、observed/derived/declared、出典制約、訂正・削除伝播、想起監査を新しい契約として検証する。commitmentを汎用記憶へ読み替えない。 |
| [FR-ME-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L288) | unverified_gap | R3 | 人物関係・自己記憶・strategyの同等実装は今回の2リポジトリで確認できない。Hermes本体/後続ローカル変更の調査後、必要な差分だけを設計する。 |
| [FR-ME-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L289) | unverified_gap | R1 | 既存source/evidence参照を出発点とし、observed/derived/declared、出典制約、訂正・削除伝播、想起監査を新しい契約として検証する。commitmentを汎用記憶へ読み替えない。 |
| [FR-ME-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L290) | unverified_gap | R3 | 人物関係・自己記憶・strategyの同等実装は今回の2リポジトリで確認できない。Hermes本体/後続ローカル変更の調査後、必要な差分だけを設計する。 |
| [FR-ME-06](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L291) | unverified_gap | R3 | 人物関係・自己記憶・strategyの同等実装は今回の2リポジトリで確認できない。Hermes本体/後続ローカル変更の調査後、必要な差分だけを設計する。 |
| [FR-ME-07](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L292) | unverified_gap | R1 | 既存source/evidence参照を出発点とし、observed/derived/declared、出典制約、訂正・削除伝播、想起監査を新しい契約として検証する。commitmentを汎用記憶へ読み替えない。 |
| [FR-ME-08](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L293) | partial | R1 | Discord検索はSQLite FTS5。意味ベクトル検索+条件検索の同等実装とは言えない。検索品質を測ってから保存先/方式を決める。 |
| [FR-ME-09](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L294) | unverified_gap | R1 | 既存source/evidence参照を出発点とし、observed/derived/declared、出典制約、訂正・削除伝播、想起監査を新しい契約として検証する。commitmentを汎用記憶へ読み替えない。 |
| [FR-ME-10](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L295) | unverified_gap | R1 | 既存source/evidence参照を出発点とし、observed/derived/declared、出典制約、訂正・削除伝播、想起監査を新しい契約として検証する。commitmentを汎用記憶へ読み替えない。 |
| [FR-ME-11](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L296) | partial | R1 | owner通信と読み取り索引の分離はある。新しいderived記憶のpublic/guild/channel/dm/operator制約を想起と出力の両方で保証する必要がある。owner限定は処理/返信の境界であり、非owner DMを保存しない保証ではない。 |
| [FR-ME-12](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L297) | unverified_gap | R1 | 既存source/evidence参照を出発点とし、observed/derived/declared、出典制約、訂正・削除伝播、想起監査を新しい契約として検証する。commitmentを汎用記憶へ読み替えない。 |
| [FR-IS-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L303) | unverified_gap | R3 | 関心/関係性の決定論的bounded update、最小証拠、減衰、履歴、declared不変性は未証明。数値から権限を変えず、合成データで先に評価する。 |
| [FR-IS-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L304) | unverified_gap | R3 | 関心/関係性の決定論的bounded update、最小証拠、減衰、履歴、declared不変性は未証明。数値から権限を変えず、合成データで先に評価する。 |
| [FR-IS-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L305) | unverified_gap | R3 | 関心/関係性の決定論的bounded update、最小証拠、減衰、履歴、declared不変性は未証明。数値から権限を変えず、合成データで先に評価する。 |
| [FR-IS-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L306) | unverified_gap | R3 | 関心/関係性の決定論的bounded update、最小証拠、減衰、履歴、declared不変性は未証明。数値から権限を変えず、合成データで先に評価する。 |
| [FR-IS-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L307) | unverified_gap | R3 | 関心/関係性の決定論的bounded update、最小証拠、減衰、履歴、declared不変性は未証明。数値から権限を変えず、合成データで先に評価する。 |
| [FR-RF-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L313) | unverified_gap | R2 | Morning Briefは今後の予定/約束の提示であり日次Reflectionではない。根拠付きの振り返り提案と成長ログをまず無送信・無自動適用で評価する。 |
| [FR-RF-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L314) | unverified_gap | R3 | 出典付きstrategy、上限/試用/退役/版管理、週次Consolidationは未証明。元SRS内のMVP/P2の優先度差も実装前に解消する。 |
| [FR-RF-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L315) | unverified_gap | R3 | 出典付きstrategy、上限/試用/退役/版管理、週次Consolidationは未証明。元SRS内のMVP/P2の優先度差も実装前に解消する。 |
| [FR-RF-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L316) | unverified_gap | R3 | 出典付きstrategy、上限/試用/退役/版管理、週次Consolidationは未証明。元SRS内のMVP/P2の優先度差も実装前に解消する。 |
| [FR-RF-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L317) | unverified_gap | R2 | Morning Briefは今後の予定/約束の提示であり日次Reflectionではない。根拠付きの振り返り提案と成長ログをまず無送信・無自動適用で評価する。 |
| [FR-RF-06](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L318) | unverified_gap | R2 | Morning Briefは今後の予定/約束の提示であり日次Reflectionではない。根拠付きの振り返り提案と成長ログをまず無送信・無自動適用で評価する。 |
| [FR-RF-07](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L319) | unverified_gap | R3 | 出典付きstrategy、上限/試用/退役/版管理、週次Consolidationは未証明。元SRS内のMVP/P2の優先度差も実装前に解消する。 |
| [FR-CT-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L325) | decision_required | R4 | 元の0〜5レベルをHermesの既存権限と同一視しない。本人通知とコミュニティ自発行動を分けた停止/降格の意味を先に決める。 |
| [FR-CT-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L326) | unverified_gap | R3 | coding jobレビューは会話/記憶変更の承認キューではない。payload固定、期限切れ、対象の再検証、取消を含む専用契約が必要。 |
| [FR-CT-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L327) | unverified_gap | R2 | AttentionのLINE通数予算はLLM/API金額の台帳ではない。モデル/用途別tokens・費用と日次/月次上限、失敗時の縮退を別に検証する。 |
| [FR-CT-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L328) | partial | R4 | 既存owner通知の重複抑止/ポリシーはあるが、SNS bot間ループ、反復、LLM費用による降格/手動解除の全契約は未証明。 |
| [FR-CT-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L329) | partial | R0 | サンプル設定とコードはGit管理済み。実配備設定・秘密情報は別であり、全設定をGitへコピーしない。変更履歴は秘密を含めず追跡する。 |
| [FR-OB-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L335) | partial | R2 | 既存request/source/evidenceと監査を再利用する。想起→判断→振り返り→提案までの全経路相関は未証明。本文/個人情報をログへ複製しない。 |
| [FR-OB-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L336) | partial | R2 | 既存request/source/evidenceと監査を再利用する。想起→判断→振り返り→提案までの全経路相関は未証明。本文/個人情報をログへ複製しない。 |
| [FR-OB-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L337) | unverified_gap | R2 | AttentionのLINE通数予算はLLM/API金額の台帳ではない。モデル/用途別tokens・費用と日次/月次上限、失敗時の縮退を別に検証する。 |
| [FR-OB-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L338) | deferred | D1 | X/公開SNSまたはOpenTelemetryは後続候補。利用価値、提供元のAPI文書、費用、対象範囲を確認するまで追加しない。 |
| [NFR-01](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L346) | partial | R0 | 配備/隔離のコードと過去記録はあるが、現在の再起動5分以内、日次バックアップからの全状態復元は未測定。本番を確認だけのために再起動しない。 |
| [NFR-02](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L347) | partial | R1 | 受信から耐久化までの障害窓を明示し、合成データでcrash/replayを試験する。本番DBを強制killしない。送信側の固定batch/UNKNOWN保留を追加したが、collectorを含む受信直後の全耐久性は未証明。 |
| [NFR-03](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L348) | unverified_gap | R2 | median 10秒/p95 30秒は新しい経路で未測定。既存過去記録も達成証明にはならない。負荷/モデル別に測る。 |
| [NFR-04](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L349) | unverified_gap | R2 | 旧3,000〜5,000円/月はAgentricAI単体の設計上限。Hermesとの合算範囲と実単価を確認し、追加支出の許可としない。 |
| [NFR-05](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L350) | partial | R1 | adapter分離とrenderer選択経路はある。新媒体がadapterだけで済む保証、全Hermesで2プロバイダ疎通は未検証。 |
| [NFR-06](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L351) | partial | R1 | BridgeのYAML/環境変数でmodel/binary等を選択するコードと、既存provider fallbackの過去記録がある。汎用provider解決はこのsnapshot外のHermes本体に依存し、資格情報・API/MCP互換性を含めた2プロバイダの設定のみでの切替/復帰は今回未再現。媒体adapter交換とは区別する。 |
| [NFR-07](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L352) | decision_required | R1 | PostgreSQL+pgvector/Alembic/1DBという旧構成を既存SQLite・権限分離へ強制しない。必要な差分と運用負担をADRで比較して決める。 |
| [NFR-08](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L353) | partial | R2 | request/source/evidenceの既存監査から新しい想起・振り返りまで追跡できるかをUC-14で検証する。 |
| [NFR-09](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L354) | partial | R1 | 既存の防御/テストを維持し、新しい記憶経由の遅延注入とscope越境を含むT1〜T11を追加する。既存テストの存在だけで統合合格としない。 |
| [NFR-10](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L355) | partial | R1 | 既存の防御/テストを維持し、新しい記憶経由の遅延注入とscope越境を含むT1〜T11を追加する。既存テストの存在だけで統合合格としない。 |
| [NFR-11](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L356) | partial | R0 | 配備/隔離のコードと過去記録はあるが、現在の再起動5分以内、日次バックアップからの全状態復元は未測定。本番を確認だけのために再起動しない。 |
| [NFR-12](https://github.com/MasterMk2/AgentricAI/blob/64a6e1aa7449f8f571b9e60baa2a7eb714c86985/docs/要件定義.md#L357) | decision_required | R1 | PostgreSQL+pgvector/Alembic/1DBという旧構成を既存SQLite・権限分離へ強制しない。必要な差分と運用負担をADRで比較して決める。 |

## 7. 実コードへの参照

- **E1**: [collector.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/discord-knowledge/app/collector.py), [dk_normalize.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/discord-knowledge/app/dk_normalize.py), [dk_store.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/discord-knowledge/app/dk_store.py)
- **E2**: [indexer.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/discord-knowledge/app/indexer.py), [discord_mcp.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/discord-knowledge/mcp/discord_mcp.py)
- **E3**: [gw_discord.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_discord.py), [gw_canonical.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_canonical.py), [gw_service.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_service.py)
- **E4**: [gw_proactive.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_proactive.py), [gw_store.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_store.py), [gw_format.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_format.py)
- **E5**: [att_engine.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/attention/app/att_engine.py), [att_policy.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/attention/app/att_policy.py), [att_budget.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/attention/app/att_budget.py), [att_store.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/attention/app/att_store.py)
- **E6**: [models.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/commitments/models.py), [reconcile.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/commitments/reconcile.py), [store.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/commitments/store.py)
- **E7**: [planner.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/planning/planner.py), [renderer.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/planning/renderer.py), [brief_service.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/planning/brief_service.py), [test_renderer_injection.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-planning/test_renderer_injection.py), [test_privacy_guard.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-planning/test_privacy_guard.py)
- **E8**: [README.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/worker/README.md), [17-phase-h32s-antigravity-security-closeout.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/docs/17-phase-h32s-antigravity-security-closeout.md)
- **E9**: [gw_line.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_line.py), [gw_canonical.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_canonical.py), [gw_service.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_service.py)
- **E10**: [RESTART-SAFETY.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/docs/RESTART-SAFETY.md), [REPLY-RESTART-SAFETY.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/docs/REPLY-RESTART-SAFETY.md), [LINE-RESTART-SAFETY.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/docs/LINE-RESTART-SAFETY.md), [test_delivery.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-gateway/test_delivery.py), [test_line_recovery.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-gateway/test_line_recovery.py), [test_brief_delivery.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-planning/test_brief_delivery.py)
- **E11**: [OFFLINE-TESTS.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/docs/OFFLINE-TESTS.md), [offline-tests.yml](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/.github/workflows/offline-tests.yml), [37247078869](https://github.com/MasterMk2/hermes-cos-infra/actions/runs/37247078869)
- **E12**: [gw_discord.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_discord.py), [gw_store.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_store.py), [gw_config.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/channel-gateway/app/gw_config.py)
- **E13**: [bridge_config.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/src/antigravity-bridge/app/bridge_config.py), [16-phase-h32-constrained-antigravity-backend.md](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/docs/16-phase-h32-constrained-antigravity-backend.md), [test_bridge_config.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-antigravity/test_bridge_config.py), [test_model_resilience.py](https://github.com/MasterMk2/hermes-cos-infra/blob/0445c7c8b7da7b52553eccf3f156b0882e6a8438/tests-github/test_model_resilience.py)

### LLM provider設定と通信adapterを分ける

NFR-06の根拠はE13。`bridge_config.load_config` はYAML/環境変数からmodel・binary等を選ぶ。
H.3.2文書のfallback成功は当時の実機記録。`test_model_resilience.py` はsnapshot外の
`agent` / `hermes_cli` をimportし、今回の3-suite CIの対象でもない。したがって
現行2プロバイダの設定のみでの差替え・復帰や、資格情報/API/MCP互換性を検証済みとはしない。
NFR-05の媒体adapter分離とは別に、既存Hermes本体の対応実体と切替契約を確認する。

## 8. 文書統合の検証範囲

- 原要件のFR/NFR全IDについて重複なし・欠落なしを機械的に照合
- 対応表のsource行、参照するファイルと固定revision、相対リンクを検査
- AgentricAIの既存import smoke、ruff check、ruff format --checkを実行
- 変更対象は文書と要件データのみ。src、依存、サービス設定、workflowは変更しない
- 現行コードの3-suite offline CIは上記SHAの証拠。今回の文書headでも既存CIを実行する。全Hermes回帰、Pi、LLM API、実Discord/LINE、権限、現在の自然配信は未検証

次の実装判断はR0のコード回収後に行う。コミュニティ参加を残すかの判断はR4まで待てる。
それまで安全に進められるのは出典/範囲契約とoffline評価の設計であり、公開範囲の拡張ではない。
