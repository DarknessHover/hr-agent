"""设备能力层：封装 adb，只允许访问本地环境声明的准入设备。

能力方法不理解候选人、不理解话术业务含义，只做读屏、点击、滑动、截图与目标应用检查。
"""

from __future__ import annotations

import re
import subprocess
import time
import math
from dataclasses import dataclass
from pathlib import Path

from .settings import DeviceSettings, device_settings


BOSS_PACKAGE = "com.hpbr.bosszhipin"
BOSS_MAIN_ACTIVITY = "com.hpbr.bosszhpin.boss.resume2.normal.BossViewGeekResumePagerActivity"
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


class DeviceError(RuntimeError):
    """设备身份或安全契约不满足。"""


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def height(self) -> int:
        return self.bottom - self.top


def parse_bounds(raw: str) -> Bounds:
    match = BOUNDS_RE.fullmatch(raw or "")
    if not match:
        raise DeviceError(f"无法解析控件边界：{raw!r}")
    left, top, right, bottom = (int(value) for value in match.groups())
    if right <= left or bottom <= top:
        raise DeviceError(f"控件边界无有效面积：{raw!r}")
    return Bounds(left, top, right, bottom)


class Device:
    def __init__(
        self,
        adb: Path | None = None,
        serial: str | None = None,
        settings: DeviceSettings | None = None,
    ) -> None:
        runtime = settings or device_settings()
        selected_serial = serial or runtime.serial
        selected_adb = adb or runtime.adb_path
        if selected_serial != runtime.serial:
            raise DeviceError(
                f"拒绝访问未准入设备 {selected_serial!r}；"
                f"本地环境只允许 {runtime.serial!r}"
            )
        if not selected_adb.is_file():
            raise DeviceError(f"找不到 adb：{selected_adb}")
        self.adb = selected_adb
        self.serial = selected_serial
        self.expected_identity = runtime.expected_identity

    # ---- adb 原语 -------------------------------------------------------
    def _run(self, *args: str, text: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(self.adb), "-s", self.serial, *args],
            capture_output=True,
            text=text,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
            raise DeviceError(
                f"ADB 命令失败（exit={result.returncode}）：{' '.join(args)}\n{stderr.strip()}"
            )
        return result

    def shell_text(self, *args: str) -> str:
        return self._run("shell", *args, text=True).stdout.strip()

    # ---- 身份与环境校验 -------------------------------------------------
    def verify_identity(self) -> dict[str, str]:
        identity = {
            "serial": self.serial,
            "state": self._run("get-state", text=True).stdout.strip(),
            "manufacturer": self.shell_text("getprop", "ro.product.manufacturer"),
            "model": self.shell_text("getprop", "ro.product.model"),
            "device": self.shell_text("getprop", "ro.product.device"),
        }
        mismatches = [
            f"{key}: expected={value!r}, actual={identity[key]!r}"
            for key, value in self.expected_identity.items()
            if identity.get(key) != value
        ]
        if mismatches:
            raise DeviceError("设备身份校验失败；拒绝继续：\n" + "\n".join(mismatches))
        return identity

    def require_boss_installed(self) -> str:
        output = self.shell_text("pm", "list", "packages", BOSS_PACKAGE)
        packages = [
            line.removeprefix("package:").strip()
            for line in output.splitlines()
            if line.startswith("package:")
        ]
        if packages != [BOSS_PACKAGE]:
            raise DeviceError(
                f"BOSS 安装包必须唯一精确命中 {BOSS_PACKAGE!r}；实际：{packages!r}"
            )
        return packages[0]

    def focused_window(self) -> str:
        for line in self.shell_text("dumpsys", "window").splitlines():
            if "mCurrentFocus=" in line:
                return line.strip()
        raise DeviceError("无法读取 mCurrentFocus")

    def boss_foreground(self) -> bool:
        return BOSS_PACKAGE in self.focused_window()

    def require_boss_foreground(self) -> str:
        focus = self.focused_window()
        if BOSS_PACKAGE not in focus:
            raise DeviceError(f"BOSS 不在前台；拒绝界面动作：{focus}")
        return focus

    def launch_boss(self, timeout: float = 10.0) -> str:
        package = self.require_boss_installed()
        self._run(
            "shell", "monkey", "-p", package, "-c",
            "android.intent.category.LAUNCHER", "1", text=True,
        )
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            last = self.focused_window()
            if BOSS_PACKAGE in last:
                return last
            time.sleep(0.5)
        raise DeviceError(f"BOSS 已请求拉起，但 {timeout:.0f}s 内未成为前台：{last}")

    # ---- 读屏 ----------------------------------------------------------
    def dump_ui(self) -> bytes:
        raw = self._run("exec-out", "uiautomator", "dump", "/dev/tty", text=False).stdout
        start = raw.find(b"<?xml")
        end = raw.find(b"</hierarchy>")
        if start < 0 or end < 0:
            raise DeviceError("uiautomator 输出中没有完整 XML")
        return raw[start : end + len(b"</hierarchy>")]

    def screenshot(self) -> bytes:
        png = self._run("exec-out", "screencap", "-p", text=False).stdout
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DeviceError("设备截图不是有效 PNG")
        return png

    def screen_size(self) -> tuple[int, int]:
        output = self.shell_text("wm", "size")
        matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", output)
        if not matches:
            raise DeviceError(f"无法读取屏幕尺寸：{output!r}")
        width, height = matches[-1]
        return int(width), int(height)

    # ---- 动作 ----------------------------------------------------------
    def tap(self, x: int, y: int, hold_ms: int = 80) -> None:
        """轻触。

        BOSS 直聘对 adb `input tap` 的合成点击经常不响应，实机验证需用
        成对的 motionevent DOWN/UP（保持数十毫秒）才稳定生效。
        """
        if not 1 <= hold_ms <= 500:
            raise DeviceError(f"轻触时长必须在 1–500ms：{hold_ms}")
        self._run(
            "shell", "input", "touchscreen", "motionevent", "DOWN", str(x), str(y),
            text=True,
        )
        try:
            time.sleep(hold_ms / 1000)
        finally:
            self._run(
                "shell", "input", "touchscreen", "motionevent", "UP", str(x), str(y),
                text=True,
            )

    def system_tap(self, x: int, y: int) -> None:
        """使用Android单命令tap注入一次完整点击。

        常规 ``tap`` 的分离DOWN/UP适合多数BOSS页面；常用语RecyclerView
        的发送按钮在真机上可能拒绝分离事件，因此只把本方法作为控件级回退。
        """
        self._run(
            "shell", "input", "touchscreen", "tap", str(x), str(y), text=True
        )

    def mouse_tap(self, x: int, y: int) -> None:
        """以 Android mouse 事件源注入单次点击。

        BOSS 常用语行内发送按钮对 ``touchscreen`` 合成事件存在真机不响应
        样本；mouse 源保留同一坐标，但改变 MotionEvent source，便于做可审计
        的输入机制 A/B 测试。它仍是 ADB 注入，不等同于 scrcpy UHID。
        """
        self._run("shell", "input", "mouse", "tap", str(x), str(y), text=True)

    def micro_tap(self, x: int, y: int, duration_ms: int = 60) -> None:
        """以1px短划注入单命令点击序列，供RecyclerView按钮回退。"""
        if not 20 <= duration_ms <= 150:
            raise DeviceError(f"微动轻触时长必须在 20–150ms：{duration_ms}")
        self._run(
            "shell", "input", "touchscreen", "swipe",
            str(x), str(y), str(x + 1), str(y), str(duration_ms), text=True,
        )

    def press(self, x: int, y: int, duration_ms: int = 200) -> None:
        """同点按下—抬起。

        某些 BOSS ViewPager 页签会丢弃由两次 adb shell 调用组成的
        ``motionevent DOWN/UP``，但能稳定响应单条同点 ``swipe``。保留
        ``tap`` 作为1号状态机的成熟默认策略，由页面适配器显式选择本方法。
        """
        if not 1 <= duration_ms <= 1000:
            raise DeviceError(f"按压时长必须在 1–1000ms：{duration_ms}")
        self._run(
            "shell", "input", "touchscreen", "swipe",
            str(x), str(y), str(x), str(y), str(duration_ms), text=True,
        )

    def key(self, code: str) -> None:
        self._run("shell", "input", "keyevent", code, text=True)

    def back(self) -> None:
        self.key("KEYCODE_BACK")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        if not 50 <= duration_ms <= 1500:
            raise DeviceError(f"滑动时长必须在 50–1500ms：{duration_ms}")
        self._run(
            "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms), text=True,
        )

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 800,
        steps: int = 10,
    ) -> None:
        """用连续 DOWN/MOVE/UP 注入一条慢速拖动轨迹。

        BOSS 的聊天常用语底部面板会忽略单条 ``input swipe``，但和
        ``tap`` 一样能响应完整的触摸事件流。该原语只补足注入方式，不包含
        任何页面或业务语义。
        """
        if not 100 <= duration_ms <= 2000:
            raise DeviceError(f"拖动时长必须在 100–2000ms：{duration_ms}")
        if not 2 <= steps <= 30:
            raise DeviceError(f"拖动步数必须在 2–30：{steps}")
        self._run(
            "shell", "input", "touchscreen", "motionevent", "DOWN",
            str(x1), str(y1), text=True,
        )
        step_seconds = duration_ms / steps / 1000
        try:
            for index in range(1, steps + 1):
                ratio = index / steps
                x = x1 + math.trunc((x2 - x1) * ratio)
                y = y1 + math.trunc((y2 - y1) * ratio)
                time.sleep(step_seconds)
                self._run(
                    "shell", "input", "touchscreen", "motionevent", "MOVE",
                    str(x), str(y), text=True,
                )
        finally:
            self._run(
                "shell", "input", "touchscreen", "motionevent", "UP",
                str(x2), str(y2), text=True,
            )

    def scroll_at(self, x: int, y: int, vertical: float) -> None:
        """在指定坐标注入无点击的鼠标滚轮事件。

        BOSS 聊天常用语底部面板会拦截手指 swipe，但其中的 RecyclerView
        能正确消费 mouse 来源的 ACTION_SCROLL。负值向列表后方滚动。
        """
        if not -20 <= vertical <= 20 or vertical == 0:
            raise DeviceError(f"纵向滚动量必须在 -20–20 且不能为0：{vertical}")
        self._run(
            "shell",
            "input",
            "mouse",
            "scroll",
            str(x),
            str(y),
            "--axis",
            f"VSCROLL,{vertical:g}",
            text=True,
        )
