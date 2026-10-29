from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.review import (
    EVALUATION_COMMENT_MAX_CHARS,
    EVALUATION_COMMENT_MIN_CHARS,
    REVIEW_MAX_OUTPUT_TOKENS,
    ReviewPolicyError,
    evaluation_comment_advisory,
    validate_evaluation_comment,
)
from app.store import Store


ROOT = Path(__file__).resolve().parents[1]


class ReviewPolicyTests(unittest.TestCase):
    def test_policy_constants(self) -> None:
        self.assertEqual(REVIEW_MAX_OUTPUT_TOKENS, 512)
        self.assertEqual(EVALUATION_COMMENT_MIN_CHARS, 100)
        self.assertEqual(EVALUATION_COMMENT_MAX_CHARS, 300)

    def test_accepts_boundaries_and_strips_outer_whitespace(self) -> None:
        self.assertEqual(validate_evaluation_comment("甲" * 100), "甲" * 100)
        self.assertEqual(
            validate_evaluation_comment("  " + "乙" * 300 + "\n"),
            "乙" * 300,
        )

    def test_length_is_advisory_but_empty_comment_is_rejected(self) -> None:
        for comment in ("甲" * 99, "乙" * 301, "丙" * 350):
            with self.subTest(length=len(comment.strip())):
                self.assertEqual(validate_evaluation_comment(comment), comment)
                self.assertIsNotNone(evaluation_comment_advisory(comment))
        self.assertIsNone(evaluation_comment_advisory("丁" * 300))
        with self.assertRaises(ReviewPolicyError):
            validate_evaluation_comment("   ")


class StoreReviewPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.store = Store(
            Path(self.tempdir.name) / "test.sqlite3",
            ROOT / "data" / "schema.sql",
        )
        saved = self.store.save_snapshot(
            {
                "display_name": "测试候选人",
                "age": 25,
                "work_years": "3年",
                "degree": "本科",
                "expect_role": "短视频运营",
                "expect_city": "深圳",
            },
            "test.activity",
            [],
        )
        self.candidate_id = int(saved["candidate_id"])

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_store_persists_comment_above_recommended_max(self) -> None:
        comment = "超" * 350
        self.store.set_evaluation(
            self.candidate_id,
            "greet",
            comment,
            "job_profiles.yaml",
            "test",
        )
        row = self.store.find_by_id(self.candidate_id)
        self.assertEqual(row["eval_decision"], "greet")
        self.assertEqual(row["eval_reasons_text"], comment)

    def test_store_persists_normalized_short_comment(self) -> None:
        comment = "  简短但有效  "
        normalized = comment.strip()
        self.store.set_evaluation(
            self.candidate_id,
            "greet",
            comment,
            "job_profiles.yaml",
            "test",
        )
        row = self.store.find_by_id(self.candidate_id)
        self.assertEqual(row["eval_reasons_text"], normalized)


if __name__ == "__main__":
    unittest.main()
