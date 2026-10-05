# 通常LINE返信の固定batchと受理結果の保留

基準commit: `6046a29500c0c90d8ea6a0473a67766ce2f195b1`。
先行の[通常Discord返信](REPLY-RESTART-SAFETY.md)に続く、LINE reply/push境界の限定修正。
実LINE送信、Pi配備、既存実機DB、認証設定の変更は含まない。

## 公式の保証を混同しない

2026-10-05に確認したLINE公式ドキュメント:

- reply tokenは一度だけ利用できる。受信後1分を超えた利用は保証されず、再配信にも
  使用済み・イベントから20分の制約がある。Invalid reply tokenは期限切れと使用済みを
  区別しない。400には不正なmessage objectも含まれる。
  [Reply API / token](https://developers.line.biz/en/reference/messaging-api/nojs/#send-reply-message)
- retry-keyはpush等の対応API専用で、最初のrequestから24時間。同じ本文と宛先で再試行する。
  200/409はLINEによるrequest受理の証拠で、ユーザーへの到達や既読の保証ではない。
  [Retry仕様](https://developers.line.biz/en/docs/messaging-api/retrying-api-request/)

この実装のSENT/RESPONDEDはprovider受理を表す。到達確認のAPIを追加したものではない。

## 再現した問題

合成HTTP応答を実LineClientへ渡す試験で、修正前は次の6ケースが失敗した。

1. reply受理後のtimeout → 内部retryで使用済み400 → push、という重複経路
2. reply受理後・SENT保存前の停止 → 次回は時刻によってpushへ切替
3. push受理後の停止 → 24時間を超えて同じretry-keyを再利用
4. 同じIDの本文を変えて送信 → 元のrequestの409を新本文の成功扱い
5. replyの400を一律にtoken期限切れと扱いpushへ切替
6. replyの409までpushの重複受理と同様に成功扱い

## 新しい送信境界

LINEは最大5messageを一つのAPI requestとして送る。Discordのchunk単位再開とは別で、
全messageを一つのbatchとして扱う。既存のinbound/outbound/delivery_attemptsテーブルを使い、
新しいtable/columnは増やさない。

- 本文・宛先・conversation・mode・件数・順序を一度に固定し、新規予約をNOT_SENTにする
- 別接続間のwrite transactionでbatch全体をSENDINGへclaimし、started attemptをcommitしてから送る
- replyは一度だけ試す。送信開始後の400・timeout・結果不明からpushへ切り替えない
- 初回未送信だけ、token有無・受信時刻・event時刻でreply/pushを選ぶ。8秒marginは維持する
- 固定済みmodeは時刻が変わっても変更しない。再起動後は本文もHermesで再生成しない
- push内部の短いretryは同一本文・同一宛先・同一keyで5xx/transportのみ。4xx/429は再試行しない
- pushの409はkey付きpush、24時間内、accepted-request-idありの場合だけ受理として扱う
- retry windowは最初のdurable attemptから計算し、timeout分を残せない/過去24時間超/
  clockが開始前へ戻った場合は送信しない。初回時刻を新しくして期限を延ばさない
- 受理receiptと全chunkのSENTを一つのtransactionで保存する

SENDINGのまま停止したbatch、または送信後の曖昧な結果はUNKNOWNで保留する。
経過時間・回数を理由にFAILEDやNOT_SENTへ変えない。旧PENDING/FAILED等も未送信の証拠として
引き継がない。UNKNOWNから自動再送する入口はない。24時間内の再起動後再送も今回は許可しない。

ローカル事前検査の不正batch・tokenなし・ownerなしは、HTTPを開始していないという明示的な
結果に限ってNOT_SENTとして記録できる。最終HTTP statusだけではこの権限を得られない。
並行claim、stale回復、遅い旧settleは先行修正と同じwrite lock/保留原則で扱う。

## 件数と文字数

LINE用の整形をUTF-16単位にし、設定が大きくても1message最大5000単位、batch最大5件に制限。
超過時の省略を末尾で明示し、整形後の実送信payloadと保存するbatchを一致させる。
下位のpayload builder/clientは6件目を黙って切り捨てず、送信前に拒否する。
同じ整形をproactive LINEにも適用し、共通clientの409証拠確認も共通化する。

## 検証と残る境界

既存のhash固定依存・offline check/CIを継続する。
`tests-gateway/test_line_recovery.py`は次を含む:

- 上記6ケースの修正前失敗と修正後成功
- 初回reply/push選択、肯定的200と条件付きpush409、同一keyの短いretry、24時間境界
- 5件/UTF-16/省略の実payloadとDB一致、下位層の不正batch拒否
- 旧行、変更本文/件数/owner/conversation、固定mode、取消、並行batch claim
- receipt書込失敗、遅いreceiptでUNKNOWNが消えないこと、未送信batchの再生成なし再開
- reply/pushそれぞれ別Pythonプロセスがmock受理直後にos._exitし、再起動後は送らない試験

全て合成fixture、一時DB、fake HTTP。child processにもIP/DNS guardを適用する。
ローカルAF_UNIX制限下のpartial checkはexit 2、完全CIは別途確認する。

手動reconcile、到達/既読追跡、配備や実機DBの変換は未実装/未実施。
providerとローカルDBを同一transactionにはできず、外部exactly-onceや電源断耐久性は保証しない。
旧コードはUNKNOWN/NOT_SENTを理解しないので、稼働DBのまま旧版へdowngradeしない。
戻す場合は送信停止と受領状態・DB互換性の確認が必要で、UNKNOWNの一括resetは不可。
