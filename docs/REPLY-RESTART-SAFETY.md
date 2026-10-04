# 通常Discord返信とMorning Briefの未確認状態

基準commit: `53c22a89b061cae0e6d44cf1af5a9843c881c434`。
先行の[proactive通知のUNKNOWN保留](RESTART-SAFETY.md)とは別の追加修正。
対象は通常Discord DM返信のchunk送信と、Morning Briefの配信確認の解釈に限る。
コードの検証であり、Piへの配備や既存実機DBの変更は含まない。

## 通常返信の危険だった境界

旧版は全chunkを送信した後、まとめてSENTを保存していた。途中停止時にはPENDINGが残り、
一意なoutbound IDがあっても、既に受理されたchunkを再送し得た。
また再処理時にHermesへ再質問すると、同じ返信IDに異なる本文が混ざる可能性があった。

今回は既存のinbound/outbound/attemptテーブルをそのまま使う。

1. 全chunkの本文・件数・順序・宛先・conversationを一度のtransactionで固定する
2. 送信前に一つのchunkをSENDINGとしてcommitする
3. adapterの肯定的な結果をSENT＋attemptとしてcommitしてから次のchunkへ進む
4. 再起動時は固定済み本文を読み、Hermesで再生成しない

| 状態 | 意味と扱い |
|---|---|
| NOT_SENT | 新方式で未送信と確定した予約、またはchannel取得段階の失敗。再開候補 |
| SENDING | adapter呼出し前に記録。稼働中は並行送信不可、stale回復後はUNKNOWN |
| SENT | adapterから当該chunkの肯定的な結果を取得して記録済み。再送しない |
| UNKNOWN | 受理の成否を確認できない。返信全体を保留する |
| 旧PENDING/FAILED/ABANDONED等 | 旧版ではSENDINGを書かず送った可能性がある。attempts=0でも保留 |

自動再開できるのは「先頭から連続したSENT＋残り全てNOT_SENT」だけ。
UNKNOWNが途中にあれば、後続のNOT_SENTも送らない。順序・件数・本文・owner/conversationの
不一致、本文のretention消去も保留する。取消されたリクエストをretryで復活させない。
claimはBEGIN IMMEDIATEで別接続間を直列化する。稼働中のclaimへの重複呼出しはin_flightを返し、
元の会話lockを解除しない。保留となった返信は新しい独立メッセージまで止めない。

Gatewayのinbound UNKNOWNは成功でも確定失敗でもなく、通常キューへ再投入しない。
livenessのreply_unknownで件数を観測できる。手動でUNKNOWNを消して再送する入口は追加しない。

### 保証の範囲

これはGatewayのプロセス停止・アプリ再試行境界での再送抑止であり、外部exactly-onceや
ユーザーの既読を保証しない。依存を固定したdiscord.py 2.7.1にもHTTP 5xx/接続切断時の
内部retryがある。最終HTTP 4xxだけでは、その呼出し中に以前受理された可能性を排除できない。
このため通常返信ではsend呼出し後の失敗をUNKNOWNにし、明確な送信前channel取得失敗だけ
NOT_SENTとする。proactiveも同じ理由でDiscord/LINEともsend開始後の失敗をUNKNOWNへ寄せる。
LINE内部transportも複数回試すため、最終4xxやretry-keyの過去の有効性から長期再送を許さない。
先行版がHTTP応答だけでNOT_SENTにした行も、送信前と証明できるcategoryが無ければUNKNOWNへ。
SDK内部retryそのものとLINE通常reply→pushはこの追加修正では変更しない。マシン全体の電源断・ディスク故障の耐久性も別の検証対象。

## Morning Briefは予約と確認済みを分ける

IN_FLIGHTとcontent_revision_mismatchは「Gatewayに同じIDの予約がある」という証拠で、
配信成功の証拠ではない。どちらもretryを止め、planning.dbのUNKNOWNとhealth=unknownにする。
元のdetail、plan_hash、attemptsは保持し、delivered_at/last_delivery_successを捏造しない。
次の同日triggerも再生成・再送せず保留を返す。通知で保留を繰り返し知らせる処理はない。

旧suppressed行も配信を確認できないため、現在の読取/再実行時には同じ保留として扱う。
新しい配信確認はdelivered、またはduplicate＋detail=already_deliveredに限定する。
確認済みの日のローカル重複抑止は従来どおり維持する。

IN_FLIGHTが後にGateway側でSENTになっても、この変更には受領照合APIがないためローカル保留は
自動解除しない。将来の照合入口には元のID・本文hash・宛先・肯定的receiptを束ねた確認が必要。
タイムアウトをFAILEDに置き換えたり、別本文/別IDを作って保留を迂回してはならない。

## 検証と導入境界

既存の[offline test入口](OFFLINE-TESTS.md)を使う。runtime/lock/workflowは変更しない。

- chunkごとの送信前claim・送信後receipt、各位置の中断、確認済みprefixのみの再開
- 別Pythonプロセスがmock受理直後にos._exitし、新しいGatewayが同じ一時DBを開く試験
- 別接続の並行claim、稼働中の重複呼出し、receipt保存失敗、古いcaller snapshot
- legacy行、本文/件数/owner/conversation変更、UNKNOWNの後続tail、取消、retention消去
- Morning BriefのIN_FLIGHT/content mismatch/旧suppressed保留と、確認済み成功との区別

全て合成fixtureとfake adapter。一時SQLite以外の実機DB、通知API、資格情報を使わない。
child processにもIP/DNS guardを適用する。ローカルでAF_UNIXが制限される場合のpartial runは
exit 2を維持し、完全成功とは扱わない。CIでは3 suiteとUnix socket試験を実行する。

### 旧版との互換性と戻し方

新しいテーブル/列は不要。新旧stateの意味を判別して、旧PENDINGを安全だと推測しない。
これは将来起動時のコード動作であり、このPR作業で既存DBを開いたり変換したりしない。

旧コードはUNKNOWN/NOT_SENTを理解せず返信を再送し得るため、稼働DBのまま旧版へ戻してはならない。
戻す際はまず送信を停止し、対象DBの受領状態と互換性を確認する。UNKNOWNをFAILED/PENDINGへ
一括変換するrollbackは不可。コードのrevertと実機の運用操作は別承認・別検証とする。
