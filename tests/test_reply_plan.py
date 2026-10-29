from __future__ import annotations

import unittest

from _config import use_sanitized_phrase_catalog
from app.reply_plan import (
    NEW_GREETING_DEFAULT_PHRASE_KEY,
    allowed_phrase,
    load_allowed_phrases,
    plan_new_greeting,
)


use_sanitized_phrase_catalog()


class ReplyPlanTests(unittest.TestCase):
    def test_send_accepts_any_configured_phrase(self) -> None:
        phrase = allowed_phrase("p09_work_schedule")
        plan = plan_new_greeting(
            "send",
            "候选人询问工作时间",
            phrase_key=phrase["key"],
        )
        self.assertEqual(plan.action, "send")
        self.assertEqual(plan.decision, "request_resume")
        self.assertEqual(plan.phrase_key, "p09_work_schedule")
        self.assertEqual(plan.intent, "job_details")

    def test_reject_intent_uses_compatible_reject_storage_value(self) -> None:
        phrase = allowed_phrase("p13_reject")
        plan = plan_new_greeting(
            "send",
            "简历不符合岗位要求",
            phrase_key=phrase["key"],
        )
        self.assertEqual(plan.action, "send")
        self.assertEqual(plan.decision, "reject")
        self.assertEqual(plan.phrase_key, "p13_reject")

    def test_hold_is_a_non_send_plan(self) -> None:
        plan = plan_new_greeting("hold", "没有安全匹配的话术")
        self.assertEqual(plan.action, "hold")
        self.assertEqual(plan.decision, "needs_review")
        self.assertIsNone(plan.phrase_key)
        self.assertIsNone(plan.intent)

    def test_send_requires_phrase_and_intent(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须绑定允许话术"):
            plan_new_greeting("send", "理由")

    def test_catalog_is_complete_and_location_is_only_a_default_hint(self) -> None:
        catalog = load_allowed_phrases()
        self.assertIn(NEW_GREETING_DEFAULT_PHRASE_KEY, catalog)
        self.assertIn("p01_check_jianying_ps", catalog)
        self.assertGreater(len(catalog), 3)


if __name__ == "__main__":
    unittest.main()
