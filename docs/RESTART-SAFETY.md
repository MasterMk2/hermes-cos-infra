# 通知の結果不明を再起動後も保持する

基準: `640d9db1044a68612e2b9fdf0204a68ebef9ea0f`。2026-10-04の最初の実装修正。
対象は **proactive通知のGateway → Attention / Morning Brief**。
本番Pi、認証値、送信先、Bot権限、配備設定を変更するものではない。

## すでにある機能と、今回埋める穴

Hermesにはすでにevents/commitments、Attention、Planning、Gatewayの永続キューがある。
`src/jobs/jobs_db.py`にもジョブ・実行・成果物への参照・取消意図・レビュー状態がある。
「記憶・仕事の状態管理・通知判断が全て欠けている」という前提では進めない。

今回再現したのは、プロバイダが通知を受理した後、GatewayがSENTを保存する前に停止する
ケース。旧`recover_stuck_proactive()`はstale SENDINGをPENDINGに戻したため、同じ通知が
再び送られ得た。送信中のtimeout、途中まで成功した複数chunkも結果を確定できない。
delivery IDの一意制約だけでは、外部の副作用とローカルDB更新を同一transactionにはできない。

## 状態の意味

| 状態 | 意味 | 自動再試行 |
|---|---|---|
| PENDING、attempts=0 | adapterに渡す前 | 可 |
| SENDING | 送信を開始し、結果をまだ記録していない | 並行送信しない |
| SENT | 全chunkについて肯定的な結果を得た | duplicate応答のみ |
| NOT_SENT | 全chunk未送信と分かる事前拒否/明確なnegative receipt | 同じ本文・同じ宛先の再試行可 |
| UNKNOWN | 結果不明、部分送信、または中断した送信 | **不可。確認待ち** |
| 旧FAILED / attempts>0の旧PENDING | 旧版では部分送信/結果不明と安全な失敗を区別していなかった | claim時にUNKNOWNへ移す |

- stale SENDINGはDiscord/LINEともUNKNOWNへ。経過時間・試行回数を非送信の証拠にしない
- 新規のNOT_SENTは事前拒否、または送信数0の明確な400/401/403/404/429応答に限定する
- timeout、5xx、未認識の結果、部分送信はUNKNOWN。取消で処理が中断してもSENDINGが残る
- LINEの内部transportの短いretry-key付き再試行は維持するが、長い停止後の期限を推測しない
- 同じIDで本文が変われば拒否し、記録済み宛先が現在設定と違っても拒否する
- リクエストにapproved / force / recipient_id等を加えても未知のfieldとして拒否する
- unknown時のquota_consumedは確定値として返さない。Attentionの予算予約はRESERVEDのまま

APIはUNKNOWNをHTTP 409、`status=outcome_unknown`として返す。これは、接続応答を失っただけの
transport `unknown`や、成功済みの`duplicate`とは別の結果。
Gatewayのlivenessには`proactive_unknown`を追加する。既存statusコマンドの状態別表にも表示される。

AttentionはUNKNOWNをDBに保持し、通常の再試行一覧にも、新しいscanの同じ通知にも戻さない。
他の元から指定されたchannelで実際に成功した場合だけ、通知全体はDELIVEREDとなる。
各channelの未知の結果は試行記録に残し、未確定のLINE予約をSPENTやRELEASEDに変えない。
これによって送信先を新たに追加することはない。

Morning BriefもUNKNOWNを記録し、同日の次回起動では生成/配送を行わない。
healthはunknownで、last_delivery_successを更新しない。成功や重複抑止成功へ丸めない。

## 代償と、手動確認の次の設計

不明な通知を送り直さないため、実際には未送信だった通知も確認まで保留される。
本変更にはforce-send、リセット、承認を代行するAPIはない。
既存のGateway/Attention statusとmorning-brief-healthを読むだけで保留を把握できる。

次の段階の手動reconcileは、次の契約を先にレビューする必要がある。

1. delivery ID・channel・本文revision・記録済み宛先・試行時刻を特定する
2. プロバイダのreceipt/本人側の履歴など、送信の有無を判別できる証拠を提示する
3. 確認済み送信と未送信を別の操作にする。不明のままFAILED/SENTへ変えない
4. 再送が必要でも元の対象・本文・有効な権限に限定する。変更は新しい行動として承認を確認する
5. 判断者・根拠・時刻を監査に残す。待ち時間、回数、retry payloadの自己申告を権限にしない

手動確認待ちを知らせるための新しい通知は追加していない。保留自体を通知ループにしない。

## 範囲外と適用ゲート

- 通常の受信メッセージへの返信、LINE reply→push切替は別経路。本変更で全面的なexactly-onceを主張しない
- 汎用タスクのapproval/artifact契約、出典付き長期記憶、Reflection、成長ログは未実装の差分として残る
- 非緊急通知を会話中に遅らせる判断や、新しいcommunity発言権限は追加しない
- AgentricAI対応表のFR-AC-03/05、FR-CT-04、FR-OB-01/02、NFR-02の一部を補強するだけで、全件完了とはしない
- Pi導入前に、GitHubにない9月10日以後のローカル変更を回収/比較する。CI成功はPi健全性の証明ではない

ソースのrollbackは可能だが、**UNKNOWN/NOT_SENTを持つDBのまま旧Gatewayへ戻してはならない**。
旧版のclaimは未知の状態を再送し得る。稼働中のdowngradeは配送を停止した保守手順と、
保留行を再送可能へ戻さない互換策のレビューが必要。本PRからその操作は行わない。

## 検証と再現

- `tests-gateway/test_proactive_recovery.py`: 実SQLiteを閉じて再接続し、受理直後の停止、
  timeout、部分送信、取消、旧状態、繰り返し要求、本文/宛先/承認payloadを検証。transportのみfake
- Planning/Attentionは結果分類・永続hold・成功時刻を更新しないこと・予算予約の保持を検証
- 実Discord/LINE/API/LLM、実資料、実Piは使用しない

開発環境とCIの共通入口は[OFFLINE-TESTS.md](OFFLINE-TESTS.md)。新規のCIはread-only権限で
3suiteを実行する。プライベートdenylistがない検査はskipを表示し、公開安全性の全面合格としない。
