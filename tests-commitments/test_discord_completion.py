"""Evidence-based completion from direct Discord replies."""

import sqlite3
import unittest

from commitments.discord_completion import (
    apply_completion, candidates, confirmed_reply,
)
from commitments.models import Commitment, CommitmentEvidence
from commitments.store import get_commitment, init_db, save_commitment


class DiscordCompletionTest(unittest.TestCase):
    def setUp(self):
        self.events = sqlite3.connect(":memory:")
        self.discord = sqlite3.connect(":memory:")
        self.addCleanup(self.events.close)
        self.addCleanup(self.discord.close)
        init_db(self.events)
        self.discord.execute("""
            CREATE TABLE messages (
                message_id TEXT, author_id TEXT, channel_id TEXT, guild_id TEXT,
                created_at TEXT, created_ts INTEGER, deleted_at TEXT,
                reply_to_message_id TEXT, content TEXT
            )
        """)
        cmt = Commitment(
            commitment_id="cmt_fixture", title="資料提出の予定",
            responsibility="SELF", work_state="ACTIONABLE",
            due_at="2026-09-09T18:00:00+09:00",
            evidence=[CommitmentEvidence(
                source_type="discord", source_id="discord:guild:channel:original",
                observed_at="2026-09-08T10:00:00+00:00",
            )],
        )
        save_commitment(self.events, cmt)
        self.message("original", "author", "channel", "2026-09-08T10:00:00+00:00",
                     1, None, "明日提出します")

    def message(self, mid, author, channel, when, ts, reply_to, content):
        self.discord.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, author, channel, "guild", when, ts, None, reply_to, content),
        )
        self.discord.commit()

    def test_explicit_direct_reply_records_completion_and_is_retry_safe(self):
        self.message("reply", "author", "channel", "2026-09-09T08:00:00+00:00",
                     2, "original", "提出しました")
        found = candidates(self.events, self.discord)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["basis"], "explicit_direct_reply")
        self.assertTrue(apply_completion(self.events, found[0]))
        self.assertFalse(apply_completion(self.events, found[0]))
        saved = get_commitment(self.events, "cmt_fixture")
        self.assertEqual(saved.work_state, "DONE")
        self.assertEqual(saved.resolved_at, "2026-09-09T08:00:00+00:00")
        completions = [e for e in saved.evidence if e.evidence_role == "completion"]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].source_id, "discord:guild:channel:reply")

    def test_unrelated_and_question_replies_do_not_auto_complete(self):
        self.message("other", "other-author", "channel", "2026-09-09T08:00:00+00:00",
                     2, "original", "提出しました")
        self.message("wrong-channel", "author", "other-channel",
                     "2026-09-09T08:01:00+00:00", 3, "original", "提出しました")
        self.message("question", "author", "channel", "2026-09-09T08:02:00+00:00",
                     4, "original", "提出しましたか？")
        self.message("negative", "author", "channel", "2026-09-09T08:02:30+00:00",
                     5, "original", "未完了です")
        self.message("ordinary", "author", "channel", "2026-09-09T08:03:00+00:00",
                     6, None, "提出しました")
        self.assertEqual(candidates(self.events, self.discord), [])
        self.assertIsNone(confirmed_reply(self.events, self.discord,
                                          "cmt_fixture", "other"))

    def test_later_uncertain_reply_blocks_automatic_completion(self):
        self.message("done", "author", "channel", "2026-09-09T08:00:00+00:00",
                     2, "original", "提出しました")
        self.message("correction", "author", "channel",
                     "2026-09-09T08:05:00+00:00", 3, "original", "まだ提出していません")
        self.assertEqual(candidates(self.events, self.discord), [])

    def test_shared_source_requires_individual_review(self):
        another = Commitment(
            commitment_id="cmt_second", title="別の予定",
            responsibility="SELF", work_state="ACTIONABLE",
            due_at="2026-09-09T18:00:00+09:00",
            evidence=[CommitmentEvidence(
                source_type="discord", source_id="discord:guild:channel:original",
                observed_at="2026-09-08T10:00:00+00:00",
            )],
        )
        save_commitment(self.events, another)
        self.message("done", "author", "channel", "2026-09-09T08:00:00+00:00",
                     2, "original", "提出しました")
        self.assertEqual(candidates(self.events, self.discord), [])

    def test_operator_can_confirm_a_direct_outcome_report(self):
        self.message("result", "author", "channel", "2026-09-09T08:00:00+00:00",
                     2, "original", "点検の結果、異常はありませんでした")
        self.assertEqual(candidates(self.events, self.discord), [])
        reviewed = confirmed_reply(self.events, self.discord, "cmt_fixture", "result")
        self.assertIsNotNone(reviewed)
        self.assertEqual(reviewed["basis"], "operator_verified_direct_reply")
        self.assertTrue(apply_completion(self.events, reviewed))
        self.assertEqual(get_commitment(self.events, "cmt_fixture").work_state, "DONE")
        retried = confirmed_reply(self.events, self.discord, "cmt_fixture", "result")
        self.assertIsNotNone(retried)
        self.assertFalse(apply_completion(self.events, retried))


if __name__ == "__main__":
    unittest.main()
