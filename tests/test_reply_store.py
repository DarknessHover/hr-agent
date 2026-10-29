from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _config import use_sanitized_phrase_catalog
from app.reply_plan import allowed_phrase
from app.reply_store import ReplyStore


use_sanitized_phrase_catalog()


ROOT = Path(__file__).resolve().parents[1]


class ManualSendReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.store = ReplyStore(
            Path(self.temp_dir.name) / "reply.sqlite3",
            ROOT / "data" / "schema.sql",
            ROOT / "data" / "reply_schema.sql",
        )
        with self.store.connection:
            candidate_id = self.store.connection.execute(
                """
                INSERT INTO candidate(display_name, first_seen_at, last_seen_at)
                VALUES ('测试候选人', '2026-10-21T00:00:00+08:00',
                        '2026-10-21T00:00:00+08:00')
                """
            ).lastrowid
            conversation_id = self.store.connection.execute(
                """
                INSERT INTO reply_conversation(
                    candidate_id, conversation_fingerprint, list_display_name,
                    first_seen_at, last_seen_at
                ) VALUES (?, 'conversation-fingerprint', '测试候选人',
                          '2026-10-21T00:00:00+08:00',
                          '2026-10-21T00:00:00+08:00')
                """,
                (candidate_id,),
            ).lastrowid
            self.conversation_id = int(conversation_id)
            message_id = self.store.connection.execute(
                """
                INSERT INTO inbound_message(
                    conversation_id, message_fingerprint, body_text,
                    direction, observed_at
                ) VALUES (?, 'message-fingerprint', '你好', 'inbound',
                          '2026-10-21T00:00:00+08:00')
                """,
                (conversation_id,),
            ).lastrowid
            self.message_id = int(message_id)
            self.action_id = self.store.connection.execute(
                """
                INSERT INTO reply_action(
                    conversation_id, inbound_message_id, idempotency_key,
                    decision, decision_reason, phrase_key, phrase_text,
                    state, planned_at
                ) VALUES (?, ?, 'idempotency-key', 'request_resume', ?,
                          'p02_invite_interview', ?, 'planned',
                          '2026-10-21T00:00:00+08:00')
                """,
                (conversation_id, message_id, "合适" * 50, "请来面试"),
            ).lastrowid

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_manual_send_creates_distinct_sent_attempt(self) -> None:
        result = self.store.record_manual_sent(
            self.action_id,
            [{"type": "ui_tree", "path": "proof.xml", "sha256": "abc"}],
            "用户手工发送，右侧气泡与送达状态已确认",
        )

        self.assertEqual(result["state"], "sent")
        self.assertTrue(result["manual_send"])
        action = self.store.reply_action(self.action_id)
        self.assertEqual(action["state"], "sent")
        attempt = self.store.connection.execute(
            "SELECT * FROM reply_send_attempt WHERE reply_action_id = ?",
            (self.action_id,),
        ).fetchone()
        self.assertEqual(attempt["state"], "sent")
        self.assertIn("用户手工发送", attempt["resolution_note"])

    def test_manual_send_rejects_already_sent_action(self) -> None:
        self.store.record_manual_sent(self.action_id, [], "首次追认")
        with self.assertRaisesRegex(ValueError, "planned/unknown"):
            self.store.record_manual_sent(self.action_id, [], "重复追认")

    def test_sendable_action_accepts_any_configured_phrase(self) -> None:
        phrase = allowed_phrase("p09_work_schedule")
        result = self.store.begin_send(
            self.action_id,
            self.conversation_id,
            self.message_id,
            phrase["key"],
            phrase["text"],
        )
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["phrase_key"], "p09_work_schedule")

    def test_sendable_action_rejects_phrase_outside_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "不在允许清单"):
            self.store.begin_send(
                self.action_id,
                self.conversation_id,
                self.message_id,
                "not_configured",
                "任意正文",
            )

    def test_sendable_action_rejects_modified_phrase_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "正文与允许清单不一致"):
            self.store.begin_send(
                self.action_id,
                self.conversation_id,
                self.message_id,
                "p09_work_schedule",
                "被修改的工作时间话术",
            )

    def test_compatibility_decision_must_match_phrase_intent(self) -> None:
        phrase = allowed_phrase("p13_reject")
        with self.assertRaisesRegex(ValueError, "兼容决策分类"):
            self.store.begin_send(
                self.action_id,
                self.conversation_id,
                self.message_id,
                phrase["key"],
                phrase["text"],
            )

    def test_needs_review_is_persisted_as_non_send_state(self) -> None:
        with self.store.connection:
            self.store.connection.execute(
                "DELETE FROM reply_action WHERE id = ?", (self.action_id,)
            )
        long_reason = "复" * 350
        planned = self.store.plan_reply_action(
            self.conversation_id,
            self.message_id,
            "needs_review",
            long_reason,
            "config/job_profiles.yaml",
            "sha256:test",
        )
        self.assertEqual(planned["state"], "needs_review")
        action = self.store.reply_action(int(planned["reply_action_id"]))
        self.assertEqual(action["decision"], "needs_review")
        self.assertEqual(action["state"], "needs_review")
        self.assertEqual(action["decision_reason"], long_reason)
        with self.assertRaisesRegex(ValueError, "不允许发送"):
            self.store.begin_send(
                int(planned["reply_action_id"]),
                self.conversation_id,
                self.message_id,
                "p14_address_acceptance",
                "测试话术",
            )


if __name__ == "__main__":
    unittest.main()
