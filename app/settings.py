"""Local machine and authorized-device settings.

Operating-system environment variables take precedence over the project-root
``.env`` file.  The loader intentionally supports only simple ``KEY=VALUE``
lines so the project keeps its standard-library-only runtime dependency.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DEVICE_ENV_KEYS = (
    "HR_AGENT_ADB_PATH",
    "HR_AGENT_DEVICE_SERIAL",
    "HR_AGENT_DEVICE_MANUFACTURER",
    "HR_AGENT_DEVICE_MODEL",
    "HR_AGENT_DEVICE_CODENAME",
)


class SettingsError(ValueError):
    """Local settings are missing or malformed."""


@dataclass(frozen=True)
class DeviceSettings:
    adb_path: Path
    serial: str
    manufacturer: str
    model: str
    codename: str

    @property
    def expected_identity(self) -> dict[str, str]:
        return {
            "state": "device",
            "manufacturer": self.manufacturer,
            "model": self.model,
            "device": self.codename,
        }


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Read a small, non-expanding dotenv subset from ``path``."""

    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise SettingsError(f"{path}:{line_number} 缺少 '='")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_KEY_RE.fullmatch(key):
            raise SettingsError(f"{path}:{line_number} 环境变量名无效：{key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def device_settings(
    *,
    env_path: Path = ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> DeviceSettings:
    """Resolve and validate the authorized machine/device binding."""

    resolved = read_env_file(env_path)
    resolved.update(dict(os.environ if environ is None else environ))
    missing = [key for key in DEVICE_ENV_KEYS if not resolved.get(key, "").strip()]
    if missing:
        names = ", ".join(missing)
        raise SettingsError(
            f"缺少设备环境变量：{names}；请复制 .env.example 为 .env 后填写"
        )
    return DeviceSettings(
        adb_path=Path(os.path.expandvars(resolved["HR_AGENT_ADB_PATH"])).expanduser(),
        serial=resolved["HR_AGENT_DEVICE_SERIAL"].strip(),
        manufacturer=resolved["HR_AGENT_DEVICE_MANUFACTURER"].strip(),
        model=resolved["HR_AGENT_DEVICE_MODEL"].strip(),
        codename=resolved["HR_AGENT_DEVICE_CODENAME"].strip(),
    )
