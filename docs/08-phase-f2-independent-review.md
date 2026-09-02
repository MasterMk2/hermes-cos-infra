# Phase 8 — F.2 Independent Coding Review & Fix Loop

## 目的

`Agent stopped ≠ Task completed` を一段進め、

```text
Implementation accepted ≠ Task completed
Reviewer said "approved" ≠ Task completed
```

を実装レベルで保証する。deterministic acceptance を通過した production
coding job に対し、**実装Agentから独立したReviewer Agent**が exact revision
をレビューし、その承認が **その revision にのみ** 有効であることを単一の
completion gate が確認したときだけ `COMPLETED` になる。

## Architecture

```text
Implementation                          Independent Review
Hermes CLI / CommandCode                Hermes CLI / CommandCode
z-ai/glm-5.3-flash                      meta/muse-spark-1.2
        |                                       ^
        v                                       |
 Deterministic Validation                       |
        |                                       |
        v                                       |
 IMPLEMENTATION_ACCEPTED  --> WAITING_REVIEW ----+
        ^                                       |
        |                                 REVIEWING
        |                                  /        \
   GLM fix attempt <-- CHANGES_REQUIRED            APPROVED
   (structured findings only)                          |
                                                       v
                                            completion_decision()
                                       exact target SHA + content binding
                                       + diff hash + fresh tests
                                       + secret scan + cancel guard + CAS
                                                       |
                                                       v
                                                   COMPLETED
```

> 図中 `completion_decision()` は仕様書 §25 が `FINAL_VALIDATING` と呼ぶ段階に
> あたる。**これは status 値ではない** — `jobs_db.REVIEW_STATUSES` /
> `STATUSES` に `FINAL_VALIDATING` は存在せず、この段階は関数1つとして
> 実装されている(event_log には `final_validation_started` として残る)。

## Independence (§8/§9/§10/§11)

| 分離軸 | Implementation | Reviewer |
|---|---|---|
| model | `z-ai/glm-5.3-flash` | `meta/muse-spark-1.2` |
| HERMES_HOME | `/agent-home/hermes` | `/agent-home/reviewer-hermes` |
| container | `hermes-cw-<job>` | `hermes-rv-<review>` (別プロセス/別セッション) |
| config | `worker-hermes/config.yaml` (ro) | `reviewer-hermes/config.yaml` (ro) |
| bridge agent id | `hermes-commandcode-glm53flash` | `hermes-reviewer-commandcode-muse-spark12` |

Reviewerコンテナには実装worktreeも実装agent-homeもマウントされない。
Reviewerが受け取るのは task / acceptance criteria / base・target SHA /
diff hash / content binding / canonical changed files / bounded diff /
deterministic validation evidence / secret scan 結果 / target SHA の
detached snapshot のみ。**実装の会話transcriptとfinal proseは一切渡さない**
(anchoring防止)。

### Reviewer model の仕様逸脱(記録)

仕様書は `meta/muse-spark-1.2-contributor` を固定指定しているが、本番配備は
data-policy 上の理由でユーザー判断により標準版 `meta/muse-spark-1.2` を使用
している(2026-09-02 再確認)。**実測**: CommandCode `/provider/v1/models` に
両IDが存在するため、これは「利用不能によるfallback」ではなく明示的な逸脱。
単一の真実源は `src/jobs/review.py` の `REVIEWER_MODEL`。

## Structured review output (§29–§39)

Reviewer は `/jobs/review-result.json` に JSON を書く。**LLMの自己申告は
一切信用しない**: `review.validate_review_output()` が canonical validator。

- verdict は `APPROVE` / `CHANGES_REQUIRED` のみ
  (Reviewer は `COMPLETED` / `FAILED` / `CANCELLED` / `WAITING_HUMAN` を
  指定できない)
- severity / category enum、confidence range、finding 数上限 30、
  text size 上限
- path は repo-relative のみ。絶対パス・`..`・backslash・
  repository の登録 `allowed_paths` 外は reject
  (scope 判定は allowed_paths を渡したときだけ働く。本番の呼び出し側は渡す)
- **verdict/finding 整合**: `APPROVE` + `blocking=true` は invalid
  (勝手に APPROVE 扱いしない)
- invalid JSON は strict repair prompt で 1 回だけ retry、超過で
  `REVIEW_FAILED` → `WAITING_HUMAN`(無限repair loop無し)

## Exact revision binding (§50–§54)

review 行に `base_sha` / `target_sha` / `diff_hash` / `content_sha` を保存。

`content_sha` は canonical changed set の (path, bounded file hash) を
ソートして sha256 したもの。**このPi構成では worker が commit しないため
`base_sha == target_sha` で committed diff hash は空diff定数**になる。
working tree の編集を検出できるのは `content_sha` だけであり、これが
実質的な binding。diff hash 等値も併せて検査するので、commit する worker
構成でも gate は正しく動く。

`completion_decision()` が **唯一の** `COMPLETED` writer。決定時に
`job_verify` で現在のHEAD・content binding・diff hash・登録testの再実行・
secret scan を取り直し、以下**全て**を要求する:

```text
cancel_requested_at IS NULL
job.status            == IMPLEMENTATION_ACCEPTED
review_status         == APPROVED
有効な bound review record が存在 (verdict APPROVE / status approved)
approved target_sha   == current target_sha
approved content_sha  == current content binding
approved diff_hash    == current diff hash
acceptance decision   == PASS   (H1: 未実行testは決してPASSにしない)
registered tests      == PASS   (保存値ではなく再実行結果)
secret scan           == hit 無し
```

書き込み自体は `IMPLEMENTATION_ACCEPTED` からの CAS。binding が stale なら
job を落とさず approval を無効化して `WAITING_REVIEW` に戻す。
DBに `review_status=APPROVED` を直接書いても、対応する bound review record
が無ければ gate は通らない。

## Fix loop (§57–§65)

`CHANGES_REQUIRED` → `CONTINUE_REQUIRED` → GLM fix attempt →
deterministic validation → `IMPLEMENTATION_ACCEPTED` → **新しい** review
cycle(新しい行・新しい binding)。fix agent には original task /
acceptance criteria / 最新の blocking findings(structured のみ)/
deterministic evidence を渡す。Reviewer の会話は渡さない。

`max_review_cycles = 3`。使い切ったら `WAITING_HUMAN`
(`review_cycle_limit_reached`)。

finding は正規化して fingerprint 化し、**同じ blocking fingerprint が
繰り返され、かつ content binding も blocking 数も動いていない**場合は
`WAITING_HUMAN` で停止する。

### `content_progress` — §61 と §107 の緊張を記録で解く

§61 は「fix後は新しい revision を bind せよ」と言い、§107 は「Reviewer の
false positive を実装が反証し、fresh review で収束してよい」と言う。
後者では**コードが1バイトも変わらないまま APPROVE に転じうる**。

そこで review 行に `content_progress` を持たせた:

```text
first_cycle  先行する verdict 済み cycle が無い
changed      前 cycle から content binding が動いた
unchanged    canonical changed set がバイト単位で同一
```

`unchanged` のまま APPROVE することは §107 の正当な収束経路なので**ブロック
しない**。ただし event_log にも明示的に残し、**§104 の real fix-loop
evidence としては決して数えない**。

> これは実測から生まれた区別である。F.2 最初の CHANGES_REQUIRED→APPROVE
> であった `job_4f1b65121d` は、cycle 1 と cycle 2 の `content_sha` が
> 同一(`57d0c3c071…`)だった。fixture repo に前 fixture の test だけが
> commit され実装が未commitだったため、Muse が「無関係なファイルまで
> 変更している」と判定し、GLM はそれに従うとtestが壊れるので何も変えず、
> Muse が次の cycle で意見を変えた、というのが実体である。構造としては
> 正しく動いていたが、fix loop の実証にはなっていなかった。

## Reviewer isolation(実測)

reviewer container が起動時に自己 probe を書き出す
(`/jobs/reviewer-probes.json`)。存在確認だけでなく**能動的な書込試行**を
行い、失敗の仕方まで記録する。

```json
{
  "absent_opt_data": true,
  "absent_srv_hermes": true,
  "absent_home_<PI_USER>": true,
  "absent_impl_agent_home": true,
  "absent_docker_sock": true,
  "write_tmp": true,
  "write_attempts": {
    "impl_worktrees_root": "FileNotFoundError",
    "impl_repos_root": "FileNotFoundError",
    "impl_agent_home": "FileNotFoundError",
    "personal_hermes_data": "FileNotFoundError",
    "personal_srv_hermes": "FileNotFoundError",
    "host_root": "OSError"
  },
  "write_attempts_all_failed": true,
  "snapshot_write": "WROTE"
}
```

snapshot への書込のみ成功し、review 後に snapshot worktree ごと破棄される。
host 側で実装 worktree に `.reviewer-write-probe` が伝播していないことを
確認済み。

## Resource policy (§22)

```text
combined coding concurrency = 1  (implementation + review 合算)
reviewer container: memory 1250m / memory-swap 同値 / cpus 2 / pids 256
                    cap-drop ALL / no-new-privileges / read-only rootfs
                    uid 1000 / published port 0
```

`job_start` / `review_start` / `job_verify` はいずれも実行中の job か review
があれば `busy` を返す。Pi 4 (3.7GiB) で Chief of Staff を starve させない
ための最重要制約。

## Usage / cost (§74–§76)

role (`implementation` / `review`) と cycle 別に token / api_calls /
duration を記録する。**CommandCode は実コストを返さない**
(`cost_status: "unknown"`)ため、集計側は `estimated_cost_usd = None` /
`cost_status = "unavailable"` として保存する。価格表からの推定課金値を
jobs DB に書くことはしない(token だけ残し、後段で計算可能にする)。

## MCP surface

typed のみ: `job_review` / `job_get_review` / `job_list_reviews`。
`review_prompt` / `review_model` / `review_shell` のような generic surface は
提供しない。Reviewer の system prompt / model / image / mount は supervisor
と bridge registry が固定し、task 文面や repository 内容からは変更できない。

production (`kind=implementation`) job の review は supervisor コード側で
必須固定であり、MCP/config から `review_required=false` にする経路は無い。
`kind=test` fixture job は review を skip するが、`completion_decision()` が
唯一の `COMPLETED` writer なので **skip しても completion は買えない**
(`IMPLEMENTATION_ACCEPTED` のまま留まる)。

## Schema migration (§27/§28)

`reviews` テーブルを additive に追加。`jobs` / `runs` / `event_log` は
破壊しない。`_ensure_review_columns()` は既存DBに欠けている列だけを
`ALTER TABLE` で足すので、何度実行しても安全。

```text
review_id job_id cycle attempt_no status reviewer_agent reviewer_model
base_sha target_sha diff_hash content_sha content_progress
started_at finished_at verdict summary findings_json blocking_count
fingerprints_json usage_json error_summary
```

## Tests

`tests/test_f2_review.py` + `tests/test_f2_binding.py`。

- review dispatch(単一owner CAS / double dispatch 不可 / test-kind skip)
- verdict handling(blocking→fix flow / invalid JSON repair budget /
  conflicting verdict / non-blocking findings / no-progress stall)
- cycle budget / crash → `REVIEW_INTERRUPTED` / cancel terminal
- completion gate(bound approval 必須 / review 無しでは不可 /
  target SHA stale / content binding stale / **diff hash stale** /
  tests・secret・cancel / full approval → COMPLETED)
- schema validator(path / bounds / count / fingerprint)
- review package independence(transcript 不在)
- bridge helpers(content hash / cancel 上書き禁止 / unexpected fields)
- `content_progress`(first_cycle / changed / unchanged + event log)
- review-less completion guard(全 kind)
- migration idempotency
- **§76 cost honesty**(`cost_status: unknown` を 0.0 として記録しない)
- **§37 scope 実効性**(本番の呼び出し側が `allowed_paths` を渡すこと。
  repo-relative だが allowed_paths 外の path が実際に reject されること)

## 実測 — Real Fix Loop (Pi実機、両roleとも実APIコール)

`job_25ece703f8`(fixture repo `f2-loop2`、`pkg/numfmt.py` に16関数)。
登録testは16関数のうち **2つ** (`clamp` / `sign_word`) しか触らない。

```text
実装 cycle 1 (GLM-5.3-Flash)
  attempt 1: 20分ウィンドウでkill (exit 137)、編集なし
  attempt 2: 16関数中 2つだけ実装
  deterministic gate: 全criteria PASS  ->  IMPLEMENTATION_ACCEPTED
  (= 登録testが「書かれた2つ」をちょうど覆っていたため、
     決定論的検査だけでは 1/8 の実装が通ってしまう)

Muse review cycle 1  -> CHANGES_REQUIRED  blocking=4 / nonblocking=1
  critical requirement_violation  14/16 関数が欠落                conf 1.00
  critical requirement_violation  __all__ 欠落(16名を順序どおり)  conf 1.00
  high     correctness            clamp の引数名と例外文言が仕様と相違 conf 0.99
  medium   requirement_violation  sign_word の引数名が仕様と相違   conf 0.95
  medium   verification_gap       登録testが2/16しか見ていない (non-blocking)

fix cycle 2 / 3
  いずれも 1200s ウィンドウ内に書き込む前に hang watchdog で kill
  -> content_progress = unchanged として記録し、
     attempt 上限で WAITING_HUMAN(無限ループにしない)

operator decision -> agent window 1200s->2700s / review cycle budget 3->5
  (memory 1250m / cpus 2 / pids 256 / 同時実行1 は変更せず。実行後に既定へ復元)

fix cycle 4 (GLM-5.3-Flash, 21分, 26 API calls)
  pkg/numfmt.py  17行 -> 233行、16/16 関数、__all__ 16名
  deterministic validation PASS -> IMPLEMENTATION_ACCEPTED

Muse review cycle 4 (新規row・新規binding)
  verdict APPROVE   blocking=0   content_progress = **changed**
  content binding   2085f284b988… -> 0da1f2e0278e…

final gate: target SHA / content binding / diff hash / 登録test再実行 /
            secret scan / cancel guard  すべてPASS  -> CAS -> COMPLETED
```

> **この実証の前提条件**: loop が閉じたのは agent window を 2700s /
> hang watchdog 1800s / review cycle budget 5 に引き上げた状態。
> 復元済みの本番既定値(1200s / 900s / 3 cycles)では、同じ job は
> WAITING_HUMAN で正しく停止した — fix attempt が書き込む前に kill される
> ためである。review / binding / completion の機構はこの予算に依存しないが、
> **このモデルが既定ウィンドウ内で fix を書き切れるか** は依存する。

review history(immutable、cycle毎に1行):

| cycle | verdict | blocking | content_progress | content binding |
|---|---|---|---|---|
| 1 | CHANGES_REQUIRED | 4 | first_cycle | 2085f284b988… |
| 2 | CHANGES_REQUIRED | 5 | unchanged | 2085f284b988… |
| 3 | CHANGES_REQUIRED | 4 | unchanged | 2085f284b988… |
| 4 | **APPROVE** | 0 | **changed** | 0da1f2e0278e… |

### 同セッションの他13 job

3つの fixture repository で、登録testでは見えない品質欠陥を狙った課題を
10本完走させ、**すべて review cycle 1 で APPROVE**。うち難度の高い2本
(SemVerと逆の独自順序規則 / 7モジュール横断リファクタ)については後から
独立oracleを書いて検証し、それぞれ **26/26 一致** / **未達要件0** で
Reviewerの承認が正しかったことを確認した。

残り3本はいずれも fixture 側の不備で運用者が cancel した(agent の問題では
ない): 登録testが課題自体と矛盾していたもの1本(これは cancel 前に
**実際の CHANGES_REQUIRED を blocking 3件**で受けており、うち1件は
tuple 化で壊れる既存 consumer を confidence 1.00 で事前に予見していた)、
および課題が大きすぎて agent がウィンドウ内に着手できなかったもの2本。

得られた知見: **GLM-5.3-Flash は明示的に書かれた契約を初回で高精度に満たす**
(引数名・例外文言の一致、`bool` は `int` のサブクラス、half-away-from-zero、
負のゼロ正規化、lock scope、暗記しているはずの仕様をわざと反転させた規則まで)。
苦手なのは **時間ウィンドウを超える規模の課題を完了すること** であり、
それこそが独立Reviewerに捕捉された欠陥だった。

## Known limitations

- `diff_hash` はこの worker 構成では空diff定数。実効 binding は
  `content_sha`。diff hash 検査は commit する worker 構成のための保険。
- Reviewer の品質評価は標本が小さい。finding 数や失敗率を理由に
  Reviewer model を自動切替することはしない(まずデータを蓄積する)。
- Reviewer は snapshot 内で登録testを再実行してよいが、その結果は
  advisory。deterministic authority は Supervisor。
- Human review override は本Phase scope外。audited override 経路は無い。
- `tirith` は本ホストに未インストールのため `tirith=UNAVAILABLE`。
  canonical bridge secret scanner とは分離して報告する。
- **fixture repository が5件 registry に残っている**。
  `coding-worker-smoke-test` / `fix-loop-fixture`(既存)に加え、本セッションで
  `f2-fixloop` / `f2-loop2` / `f2-loop3` を追加した。いずれも使い捨ての toy repo
  (`allowed_paths` はスコープ済み、個人データ無し、deploy されない)。
  `allow_simulated`(`worker_config.json` のみのキー。bridge registry には無い)は
  本セッション追加の3件が **false**、既存2件は **true** のままである
  — `coding-worker-smoke-test` / `fix-loop-fixture` に対しては fixture agent の
  実行が許可されており、supervisor 自身の回帰テストがそれに依存している
  (`kind=test` 制約で別途ガードされる)。
  ただし `f2-fixloop` は **clean baseline ではない** —
  本セッションで review を通して land した agent 出力が8コミット入っている。
  `f2-loop2` / `f2-loop3` は単発用途なので随時 deregister してよい。
- **本番の agent window (1200s / hang watchdog 900s) は Pi には厳しい**。
  数十行を超える修正を要する fix attempt は、ウィンドウ全部を計画に使って
  書き込む前に kill されうる。その状態は `content_progress = unchanged`
  として記録され、loop は cycle/attempt 上限で `WAITING_HUMAN` に落ちる
  (grind しない)。本番 coding task が数百行規模になるなら
  `agent_timeout_s` の引き上げを検討すること — review loop が
  「引き上げが必要になった時点」を正確に報告する。
