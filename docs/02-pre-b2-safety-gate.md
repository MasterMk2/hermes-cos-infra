# Phase 2 — Pre-B.2 Safety Gate (Context Scanner & Event Layer Audit)

## 目的

Phase B.2に進む前の安全ゲート:

1. `AGENTS.md` / `USER.md` のcontext injection blockをscanner回避表現で解消
2. canonical Chief of Staff rulesが実際にpromptへinjectされることの確認
3. 既存Event Layer実装を調査し、再実装せず再利用できるか判定

## Scanner Investigation

- scanner pattern library (`threat_patterns.py`) を実読しtriggerを特定:
  - `prompt_injection`: `ignore … (previous|all|above|prior) … instructions`
  - `html_comment_injection`: HTML comment内の危険キーワード
- **AGENTS.md**: 防御説明として攻撃phraseを直引用していたのが原因
  → 攻撃phraseを引用しないscanner-safe表現へ書き換え
  + canonical external-knowledge rules(en)を新節として追加
- **USER.md**: 当該警告は既に消滅(現行fileにHTML comment無し) — scanner clean確認
- **MEMORY.md**: HTML comment除去 + canonical security rule複製をpointer化
  (canonical ruleはSOUL.md/AGENTS.mdに集約、mutable memoryに置かない)
- 修正版は**適用前に公式scannerで全scope clean確認**してから置換
- scanner本体は一切変更・無効化していない

## Context Injection Test

- `hermes prompt-size`: AGENTS.mdがcontext tierに読み込まれることを確認
- 行動テスト: 外部Knowledgeの設定変更指示を「歴史的UNTRUSTEDデータ」として
  扱い、canonical sourceをSOUL.md/AGENTS.mdと正答 → PASS
- 修正後のlogsにblocked警告0件

## Event Layer Audit (read-only)

- 既存実装: extractor (deterministic prefilter → LLM分類) + MCP (stdio)
- schema: Phase B.2 target fields を100%備える
- status lifecycle / attention scoring / provenance (source_id→line_get) 実装済み
- cost guard: candidate=0→LLM 0件 / max 100候補・12 calls per run
- state.json / events.db は存在せず(本番未初期化)、fixture testの実行履歴のみ

## Verdict

**A — Nearly Complete** (要件80%以上)。再実装ではなく既存実装の拡張で対応。
