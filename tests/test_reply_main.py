from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.reply_main import (
    _select_send_strategy,
    _send_strategies_for_y,
    capture_chat,
    send_planned_reply,
)
from app.screen_ops import StopRun


class ReplySendStrategyTests(unittest.TestCase):
    def test_default_strategy_is_tap_then_press_then_system_tap(self) -> None:
        self.assertEqual(
            _send_strategies_for_y(1376),
            (
                ("tap", 120, (0, 0)),
                ("press", 200, (0, 0)),
                ("system_tap", 0, (0, 0)),
            ),
        )

    def test_strategy_is_independent_of_button_y(self) -> None:
        self.assertEqual(_send_strategies_for_y(1170), _send_strategies_for_y(1472))

    def test_explicit_strategy_override_ignores_historical_attempt_index(self) -> None:
        self.assertEqual(
            _select_send_strategy(1273, 9, "tap"),
            ("tap", 120, (0, 0)),
        )

    def test_capture_chat_relies_on_latest_inbound_not_history_scan(self) -> None:
        snapshot = SimpleNamespace(
            display_name="脱敏候选人",
            subtitle="",
            latest_inbound_text="候选人新招呼",
            visible_time_text="10:00",
            avatar_point=(1, 2),
        )
        store = Mock()
        store.conversation.return_value = {"list_display_name": "脱敏候选人"}
        store.stage_inbound_message.return_value = {
            "inbound_message_id": 2,
            "message_fingerprint": "test",
            "created": True,
        }
        with (
            patch("app.reply_main._capture", return_value=("focus", object(), [])),
            patch("app.reply_main.page_kind", return_value="chat"),
            patch("app.reply_main.chat_snapshot", return_value=snapshot),
            patch(
                "app.reply_main.outbound_message_texts",
                side_effect=AssertionError("capture_chat 不应扫描历史己方消息"),
            ),
        ):
            _, message = capture_chat(object(), Mock(), store, 1)

        self.assertEqual(message["inbound_message_id"], 2)
        store.stage_inbound_message.assert_called_once_with(
            1, "候选人新招呼", "10:00", []
        )

    def test_send_entry_rejects_needs_review_decision(self) -> None:
        store = Mock()
        store.conversation.return_value = {"list_display_name": "脱敏候选人"}
        store.reply_action.return_value = {
            "state": "planned",
            "decision": "needs_review",
            "conversation_id": 1,
            "inbound_message_id": 2,
        }
        with self.assertRaisesRegex(StopRun, "不允许发送"):
            send_planned_reply(object(), Mock(), store, 1, 2, 3, "p14_address_acceptance")


if __name__ == "__main__":
    unittest.main()
