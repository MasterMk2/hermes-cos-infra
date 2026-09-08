"""The declared invented vocabulary for the Phase I.2 planning fixtures.

Every `title=`, `counterparty=` and `summary=` literal used anywhere in
`tests-planning/` must appear here, and `test_privacy_guard.py` fails the suite
if one does not. That is the point: these fixtures were originally built by
copying real rows out of the production profile, and two successive
name-by-name sanitisation passes failed to find everything, because
「grep for the names we happen to know」 cannot find an identifier nobody has
noticed. A real production row cannot be pasted back into a fixture without a
deliberate edit to this file, where it is conspicuous.

Nothing here is real. People are the project placeholders (山田太郎 / 鈴木一郎 /
鈴木 / 高橋 / 担当A-D), organisations are ベンダA / ベンダB / 制作チーム,
addresses are `@example.com` / `@example.invalid` / `.example`, and external
user ids are low-entropy (`u0000…`). Titles are invented office and personal
errands chosen to exercise a *shape* — a title carrying a date, a title
carrying a clock time, a two-character title, a 142-character title, a
newsletter subject — never a real thing the owner has to do.

Adding an entry is a privacy decision. Before you add one, confirm it is not a
real commitment title, a real calendar event, a real person, a real service the
owner uses, or a real appointment — including a medical one, which carries no
name and therefore trips no name-shaped check.

**Author strings; do not harvest them.** This list was first assembled by
reading the existing fixtures, and one entry that arrived that way was a
verbatim production title — so the allowlist certified precisely the class of
string it exists to catch, and the file's own name argued that it was fine. An
allowlist derived from contaminated input inherits the contamination. Two
things now stop that recurring:
`DenylistTests.test_the_invented_vocabulary_does_not_intersect_the_denylist`
checks every entry here against the untracked denylist of known real strings,
and `test_this_file_contains_no_denylisted_string` checks this file directly.
Both skip with a reason when the denylist is not on the machine, so run them
with it present before adding anything.
"""

from __future__ import annotations

#: Every `title=` literal in tests-planning. f-string placeholders included verbatim.
INVENTED_TITLES = frozenset({
    "  空白と「記号」を含む題名  ",
    "09時",
    "14時締切の提出",
    "1日超過",
    "1時間後の予定",
    "20時",
    "8/31まで:配布文の確認とメール送信、連絡先の割り振り",
    "8日超過",
    "A",
    "B",
    "C",
    "Discord と LINE",
    "Discord のみ",
    "SampleService Station Offline",
    "UTC 15:30 の締切",
    "UTC 深夜の打ち合わせ",
    "W",
    "X",
    "Z 表記の締切",
    "x",
    "{cid} の予定",
    "{cid} の締切",
    "{cid} の表現",
    "{state}の項目",
    "【明日11時開催】新機能の体験会のご案内",
    "【本日限定】キャンペーンのお知らせ",
    "【第2回】クラスアワー（資料配布）",
    "あ",
    "そのまま返る締切",
    "アンケートの依頼（本日締切）",
    "カレンダー側",
    "ベンダB見積書の再提出は今週中か確認",
    "メールと会話の両方に出た締切",
    "メール由来だが時間の入った予定",
    "メール由来の予定らしきもの",
    "メール由来の作業らしきもの",
    "ロゴ掲載可否の回答",
    "中間",
    "予定{i}",
    "今日",
    "今日の締切",
    "今日中のタスク{i}",
    "今週中に学務へ書類を出す",
    "今週中の作業",
    "会議資料の最終確認",
    "健康診断の予約",
    "備品の在庫を確認する",
    "先に作成",
    "全社研修",
    "共同作業の締切",
    "別の相手",
    "別件A",
    "別件B",
    "前提待ちの作業",
    "午前の予定",
    "午前の回",
    "午後の回",
    "参加メンバー依頼と名簿送付要否の確認",
    "受講申込書を提出",
    "合宿",
    "同じ依頼（もう片方）",
    "同じ依頼（片方）",
    "同じ相手 {day}",
    "同じ長さの題名",
    "同着A",
    "同着B",
    "土曜夜の都合確認への回答",
    "地域の交流会",
    "壊れた日付",
    "夕方の予定",
    "夜の打ち合わせ",
    "夜の締切",
    "夜間の見学",
    "定例の勉強会",
    "定例テスト",
    "定例ミーティング",
    "定例会の出欠をメンバーに確認",
    "定例会の出欠・予定をメンバーに確認",
    "定例会の開催",
    "当時の3日以内",
    "当時は8日先",
    "待{i}",
    "後から作成",
    "打ち合わせ",
    "担当が曖昧な依頼",
    "掲示物の確認・承認",
    "掲示物設置（9/17 13:00〜）の確認と素材提供",
    "放置された確認",
    "新着記事のお知らせ [2026/08/{day}]",
    "日付あり",
    "日付だけの締切",
    "日付なし",
    "日付なしの作業",
    "日付なしの作業 {index}",
    "明日",
    "昨日依頼した確認",
    "昨日締切",
    "時刻ありの共同予定",
    "最も古い",
    "最も新しい",
    "最近の確認",
    "朝の予定",
    "期限つきの作業",
    "期限切れの作業",
    "期限超過{i}",
    "来週の締切",
    "来週以降の日程調整・都合確認",
    "根拠が多い方",
    "根拠が少ない方",
    "案は了承、実施日を改めて連絡する約束",
    "棚",
    "棚{i}",
    "棚卸し表の提出",
    "次{i}",
    "監査日の翌日が期限の作業",
    "相手なし {day}",
    "相手側の作業",
    "空欄 {day}",
    "第2回 クラスアワー",
    "終日の予定",
    "終日の共同予定",
    "締切 {hour}",
    "締切 {hour}時",
    "見積書の再提出を依頼",
    "証跡側",
    "証跡由来A",
    "証跡由来B",
    "資料の受け渡し",
    "近日の締切",
    "返事待ちの件",
    "返答待ち",
    "追加された締切",
    "通常のタスク",
    "週末の予定・都合確認への回答",
    "重複した予定",
    "重複した予定（詳細つき）",
    "長",
    "長い会議",
    "長く止まっている確認",
    "高橋さんに9/1(火)18時の来訪を伝える約束",
    "高橋宛:予算修正は今週中(部内の要望)",
})

#: Every `counterparty=` literal in tests-planning.
INVENTED_COUNTERPARTIES = frozenset({
    "   ",
    "\"SampleService Support\" <support@sample.example>",
    "\"週刊ダイジェスト編集部\" <news@example.invalid>",
    "\"運営事務局\" <mail@example.invalid>",
    "\"配信事務局\" <news@example.invalid>",
    "u0000000000000000000000000000",
    "山田太郎(やまだたろう)",
    "担当",
    "担当A",
    "担当B",
    "担当C",
    "担当D",
    "鈴木一郎",
    "鈴木一郎(すずきいちろう)",
})

#: Every `summary=` literal in tests-planning.
INVENTED_SUMMARIES = frozenset({
})

#: The illustrative titles quoted in `src/planning/renderer.py`'s module
#: docstring. That file is DEPLOYED SOURCE — `install-root.sh` copies it into
#: `/srv/hermes/planning/app/planning/` and into both MCP trees — so a real
#: title quoted there is published, which is exactly what happened.
DEPLOYED_SOURCE_EXAMPLES = frozenset({
    # The two shapes the grounding checks have to tolerate: a title carrying a
    # date, and a title carrying a clock time.
    "高橋さんに9/1(火)18時の来訪を伝える約束",
    "先方の都合が合えば15:30開催を依頼",
    # The grounding-gate attack the docstring reproduces: an invented task
    # appended to a real item's line.
    "・10:00 定例の勉強会、および学費を今日中に振り込むこと",
})

__all__ = [
    "INVENTED_TITLES",
    "INVENTED_COUNTERPARTIES",
    "INVENTED_SUMMARIES",
    "DEPLOYED_SOURCE_EXAMPLES",
]
