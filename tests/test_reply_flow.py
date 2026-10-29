from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _config import use_sanitized_phrase_catalog
from app.reply_flow import load_decisions, run_page


use_sanitized_phrase_catalog()


class ReplyFlowDecisionTests(unittest.TestCase):
    def test_loads_send_and_hold_decisions(self) -> None:
        payload = {
            "脱敏候选人A": {
                "action": "send",
                "phrase_key": "p09_work_schedule",
                "comment": "候选人询问工作时间",
            },
            "脱敏候选人B": {"action": "hold", "comment": "没有匹配话术"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            decisions = load_decisions(path)
        self.assertEqual(decisions["脱敏候选人A"]["action"], "send")
        self.assertEqual(decisions["脱敏候选人A"]["phrase_key"], "p09_work_schedule")
        self.assertEqual(decisions["脱敏候选人B"]["action"], "hold")
        self.assertIsNone(decisions["脱敏候选人B"]["phrase_key"])

    def test_rejects_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.json"
            path.write_text(
                json.dumps({"脱敏候选人": {"action": "reply", "comment": "x"}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_decisions(path)

    def test_rejects_unknown_phrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.json"
            path.write_text(
                json.dumps(
                    {
                        "脱敏候选人": {
                            "action": "send",
                            "comment": "甲" * 100,
                            "phrase_key": "not_configured",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不在允许清单"):
                load_decisions(path)

    def test_hold_rejects_phrase_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.json"
            path.write_text(
                json.dumps(
                    {
                        "脱敏候选人": {
                            "action": "hold",
                            "comment": "转人工",
                            "phrase_key": "p14_address_acceptance",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不得绑定"):
                load_decisions(path)

    def test_run_page_processes_each_selected_new_greeting(self) -> None:
        cards = [
            SimpleNamespace(display_name="脱敏候选人A"),
            SimpleNamespace(display_name="脱敏候选人B"),
        ]
        decisions = {
            "脱敏候选人A": {
                "action": "send",
                "phrase_key": "p14_address_acceptance",
                "comment": "甲" * 100,
            },
            "脱敏候选人B": {
                "action": "send",
                "phrase_key": "p09_work_schedule",
                "comment": "乙" * 100,
            },
        }
        logger = Mock()
        with (
            patch("app.reply_flow.primitives._capture", return_value=("focus", object(), [])),
            patch("app.reply_flow.page_kind", return_value="new_greetings"),
            patch("app.reply_flow.all_new_greeting_cards", return_value=cards),
            patch(
                "app.reply_flow.run_candidate",
                side_effect=[
                    {"display_name": "脱敏候选人A", "state": "planned"},
                    {"display_name": "脱敏候选人B", "state": "planned"},
                ],
            ) as run_candidate,
        ):
            results = run_page(object(), logger, object(), decisions)

        self.assertEqual(run_candidate.call_count, 2)
        self.assertEqual(run_candidate.call_args_list[0].args[4], "send")
        self.assertEqual(
            run_candidate.call_args_list[0].args[5], "p14_address_acceptance"
        )
        self.assertEqual(
            [result["display_name"] for result in results],
            ["脱敏候选人A", "脱敏候选人B"],
        )


if __name__ == "__main__":
    unittest.main()
