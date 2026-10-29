from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from _config import use_sanitized_phrase_catalog
from app.reply_plan import allowed_phrase
from app.reply_store import ReplyStore
from app.reply_ui import NewGreetingCard
from app.device import Bounds


use_sanitized_phrase_catalog()


ROOT = Path(__file__).resolve().parents[1]


class ReplySchemaTests(unittest.TestCase):
    def test_card_meta_enrichment_reuses_conversation(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            store = ReplyStore(
                Path(tempdir) / "reply.sqlite3",
                ROOT / "data" / "schema.sql",
                ROOT / "data" / "reply_schema.sql",
            )
            try:
                common = {
                    "display_name": "脱敏候选人",
                    "position_text": "脱敏公司-美工",
                    "preview_text": "脱敏消息",
                    "unread_count": 1,
                    "unread_signal": "badge_view:1",
                    "point": (100, 100),
                    "card_bounds": Bounds(0, 0, 200, 200),
                }
                first = store.stage_unread_card(
                    NewGreetingCard(meta_text="6-9K", **common), []
                )
                second = store.stage_unread_card(
                    NewGreetingCard(meta_text="大专 | 6-9K", **common), []
                )
                self.assertEqual(first["conversation_id"], second["conversation_id"])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM reply_conversation"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store.connection.execute("SELECT COUNT(*) FROM candidate").fetchone()[0],
                    1,
                )
            finally:
                store.close()

    def test_reply_snapshot_upsert_does_not_require_candidate_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            store = ReplyStore(
                Path(tempdir) / "reply.sqlite3",
                ROOT / "data" / "schema.sql",
                ROOT / "data" / "reply_schema.sql",
            )
            try:
                record = {
                    "display_name": "脱敏候选人",
                    "age": 25,
                    "degree": "大专",
                    "work_years": "3年",
                    "expect_role": "美工",
                    "expect_city": "深圳",
                    "summary": "脱敏简历",
                    "skills_text": "PS",
                }
                common = {
                    "display_name": "脱敏候选人",
                    "preview_text": "脱敏消息",
                    "unread_count": 0,
                    "unread_signal": "no_badge",
                    "meta_text": "大专 | 6-7K",
                    "point": (100, 100),
                    "card_bounds": Bounds(0, 0, 200, 200),
                }
                first = store.stage_unread_card(
                    NewGreetingCard(position_text="脱敏公司-产品专员/助理", **common),
                    [],
                )
                first_saved = store.update_candidate_snapshot(
                    int(first["candidate_id"]), "脱敏候选人", record, "test", []
                )
                second = store.stage_unread_card(
                    NewGreetingCard(position_text="脱敏公司-产品运营", **common),
                    [],
                )
                second_saved = store.update_candidate_snapshot(
                    int(second["candidate_id"]), "脱敏候选人", record, "test", []
                )
                self.assertIsNone(first_saved["fingerprint"])
                self.assertIsNone(first_saved["content_hash"])
                self.assertIsNone(second_saved["fingerprint"])
                self.assertIsNone(second_saved["content_hash"])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM reply_conversation"
                    ).fetchone()[0],
                    2,
                )
            finally:
                store.close()

    def test_message_idempotency_and_candidate_cascade(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            db_path = Path(tempdir) / "reply.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript((ROOT / "data" / "schema.sql").read_text("utf-8"))
            connection.executescript(
                (ROOT / "data" / "reply_schema.sql").read_text("utf-8")
            )

            cursor = connection.execute(
                """
                INSERT INTO candidate(display_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?)
                """,
                ("脱敏候选人", "2026-10-20T00:00:00+08:00", "2026-10-20T00:00:00+08:00"),
            )
            candidate_id = cursor.lastrowid
            cursor = connection.execute(
                """
                INSERT INTO reply_conversation(
                    candidate_id, conversation_fingerprint, list_display_name,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    "conversation:test",
                    "脱敏候选人",
                    "2026-10-20T00:00:00+08:00",
                    "2026-10-20T00:00:00+08:00",
                ),
            )
            conversation_id = cursor.lastrowid
            cursor = connection.execute(
                """
                INSERT INTO inbound_message(
                    conversation_id, message_fingerprint, body_text, observed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    "message:test",
                    "脱敏入站消息",
                    "2026-10-20T00:00:00+08:00",
                ),
            )
            inbound_message_id = cursor.lastrowid
            action_values = (
                conversation_id,
                inbound_message_id,
                "reply:message:test",
                "request_resume",
                "这是一段用于验证数据库约束的脱敏审核理由，不代表任何真实候选人或真实发送内容。",
                "2026-10-20T00:00:00+08:00",
            )
            connection.execute(
                """
                INSERT INTO reply_action(
                    conversation_id, inbound_message_id, idempotency_key,
                    decision, decision_reason, planned_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                action_values,
            )
            connection.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO reply_action(
                        conversation_id, inbound_message_id, idempotency_key,
                        decision, decision_reason, planned_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    action_values,
                )
            connection.rollback()

            connection.execute("DELETE FROM candidate WHERE id = ?", (candidate_id,))
            for table in (
                "candidate",
                "reply_conversation",
                "inbound_message",
                "reply_action",
                "reply_send_attempt",
            ):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.close()

    def test_reconciles_obscured_time_and_closes_send_boundary(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            store = ReplyStore(
                Path(tempdir) / "reply.sqlite3",
                ROOT / "data" / "schema.sql",
                ROOT / "data" / "reply_schema.sql",
            )
            try:
                with store.connection:
                    cursor = store.connection.execute(
                        """
                        INSERT INTO candidate(display_name, first_seen_at, last_seen_at)
                        VALUES (?, ?, ?)
                        """,
                        ("脱敏候选人", "2026-10-21T00:00:00+08:00", "2026-10-21T00:00:00+08:00"),
                    )
                    candidate_id = int(cursor.lastrowid)
                    cursor = store.connection.execute(
                        """
                        INSERT INTO reply_conversation(
                            candidate_id, conversation_fingerprint, list_display_name,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            candidate_id,
                            "conversation:time-reconcile",
                            "脱敏候选人",
                            "2026-10-21T00:00:00+08:00",
                            "2026-10-21T00:00:00+08:00",
                        ),
                    )
                    conversation_id = int(cursor.lastrowid)

                precise = store.stage_inbound_message(
                    conversation_id, "脱敏入站消息", "10月21日 04:45", []
                )
                obscured = store.stage_inbound_message(
                    conversation_id, "脱敏入站消息", "10月21日", []
                )
                fully_obscured = store.stage_inbound_message(
                    conversation_id, "脱敏入站消息", "", []
                )
                self.assertEqual(
                    precise["inbound_message_id"], obscured["inbound_message_id"]
                )
                self.assertEqual(
                    precise["inbound_message_id"],
                    fully_obscured["inbound_message_id"],
                )
                self.assertTrue(obscured["reconciled_observation"])

                planned = store.plan_reply_action(
                    conversation_id,
                    int(precise["inbound_message_id"]),
                    "request_resume",
                    "测" * 100,
                    "config/job_profiles.yaml",
                    "sha256:test",
                )
                phrase = allowed_phrase("p14_address_acceptance")
                store.begin_send(
                    int(planned["reply_action_id"]),
                    conversation_id,
                    int(precise["inbound_message_id"]),
                    phrase["key"],
                    phrase["text"],
                )
                store.cross_send_boundary(int(planned["reply_action_id"]), [])
                first_result = store.finish_send(
                    int(planned["reply_action_id"]), "unknown", []
                )
                self.assertEqual(first_result["state"], "unknown")
                retry = store.authorize_retry_after_no_effect(
                    int(planned["reply_action_id"]),
                    [],
                    "用户确认第一尝试未发送并明确授权重试一次",
                )
                self.assertEqual(retry["state"], "planned")
                store.begin_send(
                    int(planned["reply_action_id"]),
                    conversation_id,
                    int(precise["inbound_message_id"]),
                    phrase["key"],
                    phrase["text"],
                )
                store.cross_send_boundary(int(planned["reply_action_id"]), [])
                store.finish_send(
                    int(planned["reply_action_id"]), "unknown", []
                )
                result = store.reconcile_unknown_as_sent(
                    int(planned["reply_action_id"]),
                    [{"path": "sent.xml", "sha256": "proof", "type": "ui_tree"}],
                    "测试已回读到目标出站气泡及同容器送达状态",
                )
                self.assertEqual(result["state"], "sent")
                attempts = store.connection.execute(
                    "SELECT attempt_no, state FROM reply_send_attempt "
                    "WHERE reply_action_id = ? ORDER BY attempt_no",
                    (int(planned["reply_action_id"]),),
                ).fetchall()
                self.assertEqual(
                    [(row["attempt_no"], row["state"]) for row in attempts],
                    [(1, "unknown"), (2, "sent")],
                )
                with self.assertRaises(ValueError):
                    store.begin_send(
                        int(planned["reply_action_id"]),
                        conversation_id,
                        int(precise["inbound_message_id"]),
                        "p14_address_acceptance",
                        "办公地址确认测试话术",
                    )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
