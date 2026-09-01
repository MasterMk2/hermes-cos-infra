# Phase 3 — B.2 Production Event Layer Activation

## 目的

既存Event Layerを本番LINEデータで有効化し、精度確認・不足機能の最小追加を行う。
(再実装禁止 / 新規events.dbの並行作成禁止)

## Production Run

- backfill: **直近14日のみ** (1,640 records中 fresh 43 / skipped_old 1,597)
- deterministic candidates: 4件 (prefilter通過率0.24%)
- LLM分類: 3 events作成 (open 2 / candidate 1) + 1候補はtimeout
- candidate=0 → LLM call 0件を実測 / max calls制限維持

## Quality Review (本文非表示)

- actor/type/date accuracy 3/3: 「9/2 24時」→ 9/3 00:00 JST 変換を正しく実施
  (message timestamp基準)
- false positives: 無し(境界事例は低confidence→candidate+human reviewで保守処理)
- false negative: 窓内no_candidate 39件を全sample → 広告・ノイズのみ、
  明確なcommitment/deadline漏れ無し

## 追加実装 (既存event_mcp.py拡張)

- `event_search` (query/type/status/actor/project_id/date range, max 100)
- `event_mark_cancelled` (resolved/cancelled/ignoredの意味論をREADME明記)
- `event_link_project` (project存在検証、link専用)
- `event_status` counts-only強化
- **重大bug修正**: classifier失敗時にcandidateがprocessed扱いで消失する欠陥
  → `failed:N` retry ledger + 3連続失敗でabandoned + wave全滅時はstrike不進
- fixture testのstate.json上書き問題を修正

## Provenance E2E

`event_get → source_id → line_get → original context` をagentで実測 → PASS

## Morning Review Integration

cron promptは既に `event_list_open` を統合済み。dry runで
Project + Events両セクション、provenance言及、secrets無しを確認。

## Incident Transparency

初回run後のretry修正適用時に `no_candidate` 判定の誤りで窓外再分類が1回発生。
即時停止 → 窓外events 9件削除 → ledger復元 → 根本修正で完治。
LINE原文・既存events・他データへの影響なし。この経験がretry ledger設計の根拠。

## Result

**Phase B.2 completed** — LINE → incremental extraction → LLM分類 → events.db →
MCP → attention layer → Morning Review → resolve/snooze/cancel が本番成立。
