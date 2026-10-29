from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import logging as app_logging


ROOT = Path(__file__).resolve().parents[1]


class LogPartitionTests(unittest.TestCase):
    def test_proactive_and_reply_runs_are_physically_separated(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            with patch.object(app_logging, "LOG_DIR", Path(tempdir)):
                proactive = app_logging.RunLogger(
                    "selfcheck",
                    echo=False,
                    flow=app_logging.FLOW_PROACTIVE_GREETING,
                )
                reply = app_logging.RunLogger(
                    "selfcheck",
                    echo=False,
                    flow=app_logging.FLOW_REPLY_NEW_GREETINGS,
                )

            self.assertEqual(
                proactive.dir.parent.name,
                app_logging.FLOW_PROACTIVE_GREETING,
            )
            self.assertEqual(
                reply.dir.parent.name,
                app_logging.FLOW_REPLY_NEW_GREETINGS,
            )
            self.assertNotEqual(proactive.dir.parent, reply.dir.parent)

    def test_unknown_flow_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            app_logging.RunLogger("test", echo=False, flow="unknown")

    def test_evidence_levels(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            with patch.object(app_logging, "LOG_DIR", Path(tempdir)):
                minimal = app_logging.RunLogger("m", echo=False)
                debug = app_logging.RunLogger(
                    "d", echo=False, evidence_level="debug"
                )
                off = app_logging.RunLogger("o", echo=False, evidence_level="off")
            self.assertFalse(minimal.should_capture())
            self.assertTrue(minimal.should_capture(critical=True))
            self.assertTrue(debug.should_capture())
            self.assertFalse(off.should_capture(critical=True))


if __name__ == "__main__":
    unittest.main()
