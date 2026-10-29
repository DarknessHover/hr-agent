from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.settings import SettingsError, device_settings, read_env_file


ROOT = Path(__file__).resolve().parents[1]


class SettingsTests(unittest.TestCase):
    def test_reads_dotenv_and_process_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            path = Path(tempdir) / ".env"
            path.write_text(
                "\n".join(
                    (
                        "HR_AGENT_ADB_PATH=C:\\from-file\\adb.exe",
                        "HR_AGENT_DEVICE_SERIAL=file-serial",
                        "HR_AGENT_DEVICE_MANUFACTURER=file-maker",
                        "HR_AGENT_DEVICE_MODEL=file-model",
                        "HR_AGENT_DEVICE_CODENAME=file-device",
                    )
                ),
                encoding="utf-8",
            )
            settings = device_settings(
                env_path=path,
                environ={"HR_AGENT_DEVICE_SERIAL": "process-serial"},
            )
        self.assertEqual(settings.serial, "process-serial")
        self.assertEqual(settings.model, "file-model")
        self.assertEqual(settings.adb_path, Path(r"C:\from-file\adb.exe"))

    def test_missing_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            path = Path(tempdir) / ".env"
            path.write_text("HR_AGENT_DEVICE_SERIAL=only-one-value", encoding="utf-8")
            with self.assertRaisesRegex(SettingsError, "缺少设备环境变量"):
                device_settings(env_path=path, environ={})

    def test_rejects_malformed_line(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tempdir:
            path = Path(tempdir) / ".env"
            path.write_text("NOT_AN_ASSIGNMENT", encoding="utf-8")
            with self.assertRaisesRegex(SettingsError, "缺少 '='"):
                read_env_file(path)


if __name__ == "__main__":
    unittest.main()
