from __future__ import annotations

import unittest
from unittest.mock import patch

from app import cli


class CliTests(unittest.TestCase):
    def test_routes_state1_with_minimal_evidence(self) -> None:
        with patch("app.main.main", return_value=0) as routed:
            self.assertEqual(cli.main(["state1", "selfcheck"]), 0)
        routed.assert_called_once_with(["--evidence", "minimal", "selfcheck"])

    def test_debug_forces_debug_evidence(self) -> None:
        with patch("app.reply_main.main", return_value=0) as routed:
            self.assertEqual(cli.main(["debug", "state2", "scan"]), 0)
        routed.assert_called_once_with(["--evidence", "debug", "scan"])

    def test_doctor_routes_to_state1_selfcheck(self) -> None:
        with patch("app.main.main", return_value=0) as routed:
            self.assertEqual(cli.main(["doctor"]), 0)
        routed.assert_called_once_with(["--evidence", "minimal", "selfcheck"])


if __name__ == "__main__":
    unittest.main()
