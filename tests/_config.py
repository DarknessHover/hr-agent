from pathlib import Path

import app.reply_plan as reply_plan


ROOT = Path(__file__).resolve().parents[1]
TEST_PHRASES_PATH = ROOT / "config" / "reply_phrases.example.yaml"


def use_sanitized_phrase_catalog() -> None:
    """Keep offline tests independent from git-ignored business configuration."""

    reply_plan.PHRASES_PATH = TEST_PHRASES_PATH
