# Phase 5 — F.1.1 Safety Gate (Isolation Audit & Transport Layer)

## 目的

F.1をF.2へ進める前の分離強化。Coding Agent実行をHermes本体から切り離す
設計準備と、既存前提の実測検証。

## Runtime Audits (要確認項目の実測)

### `--ignore-rules` は無効

`hermes prompt-size` のcompositionがflag有無で**1バイトも変化せず**
(memory 1,721B / user profile 798B / context 1,458B 不変)。
→ container内hermes-as-coderは個人context(SOUL/MEMORY/USER/AGENTS)を
注入する。F.1の前提を訂正。`--safe-mode`でもmemory/profile残存。

### Filesystem reachability = insufficient

container内agent identityから `/opt/data/.env`(mode 600・同owner) /
memories / events.db / knowledge-index / jobs設定 がすべてreadable。

→ **結論: container内での本番Coding Agent実行は廃止**。

## 実装 (既存Supervisorロジック保持)

- **Transport adapter layer**: `LocalFixtureAdapter` (test only) /
  `RemoteWorkerAdapter` (restricted SSH + workerctl, 未接続)
- **Lifecycle移行**: `ACCEPTED → COMPLETED` を
  `IMPLEMENTATION_ACCEPTED (review_status=WAITING_REVIEW)` へ。
  COMPLETED はF.2のIndependent Review後にのみ使用。
  既存fixture jobsのCOMPLETEDはtest artifactとして保存(変換なし)
- **INTERRUPTED**: worker到達不能時の安全状態(job保存、完了扱いしない)
- **Secret scan**: diffに対するdeterministic検査
  (private key / AWS / GitHub PAT / sk- key / credential代入 / .env追加)
  → 検出時はWAITING_HUMAN(pattern idのみ表示、値は出さない)
- **Production guard**: `implementation` jobはlocal fixture adapterでは
  dispatch不可(実測: WAITING_HUMAN "no external worker configured")
- fixture agent名を `fixture-local-hermes` に改名(test専用を明示)

## Tests (全PASS)

- 継続loop新lifecycle: 2 runs → IMPLEMENTATION_ACCEPTED + WAITING_REVIEW
- secret scan: 6パターン検出+clean非検出
- production guard: 未設定workerでdispatch → WAITING_HUMAN
- workerctl selftest: path traversal / symlink escape 拒否(logic level)
- regression: Provider / LINE / Event / API / Dashboard / cron / scanner 全PASS

## Stop Condition (記録)

実worker machine情報と実Coding Agent credentialはユーザー提供待ち。
→ 次Phase F.1.1-Piで同一Pi上の分離構成を採用。
