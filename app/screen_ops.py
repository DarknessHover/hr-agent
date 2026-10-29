"""跨模块通用页面原语。

只承载「设备 / 页面」层面的通用动作，不理解任何业务语义：

- **页面分类注册表** `page_kind`：各业务模块通过 `register_page_kind` 注册自己的
  页面识别器；内置 chat / resume / list 三类（顺序与历史实现的 if/elif 一致）。
- **读屏** `read_screen`：读控件树并做风控熔断（命中验证/风控关键词即停机）。
- **等待页面** `wait_for_kind`：轮询直到目标页面出现。
- **回到目标页** `go_back_to`：只允许在已知页面之间按返回键，页面无法归类时拒绝盲按。
- **受控下滑** `scroll_down`：固定时长压住 RecyclerView 惯性，保证每屏都读到区块标题。
- **点击落定** `tap_settled`：读屏后先等待页面落定，规避 `uiautomator dump` 吞掉触摸。
- **证据与文件名工具** `evidence_entry` / `safe_name`。

新增业务模块（如「回复新招呼」）只需注册自己的页面识别器与卡片选择器，
本模块与 `walker.ListWalker` 即可复用。
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable

from .device import Device, DeviceError
from .logging import RunLogger
from .resume import (
    Screen,
    detect_chat,
    detect_geek_list,
    detect_resume,
    list_cards,
    risk_signal,
)

TAP_SETTLE_SECONDS = 2.0
SCROLL_DURATION_MS = 800
SCROLL_START_RATIO = 0.72
SCROLL_END_RATIO = 0.40

PageDetector = Callable[[str, Screen], bool]
CardFn = Callable[..., list[tuple[str, tuple[int, int]]]]


class StopRun(RuntimeError):
    """需要整轮停机并保留现场。"""


# ---- 页面分类注册表 -----------------------------------------------------
PAGE_KIND_DETECTORS: list[tuple[str, PageDetector]] = []


def register_page_kind(kind: str, detector: PageDetector) -> None:
    """注册一个页面识别器。**注册顺序即判定优先级**（先注册者先匹配）。"""
    PAGE_KIND_DETECTORS.append((kind, detector))


def known_page_kinds() -> set[str]:
    """当前已注册的全部页面类型，供 `go_back_to` 判断「可安全按返回键」。"""
    return {kind for kind, _ in PAGE_KIND_DETECTORS}


def page_kind(focus: str, screen: Screen) -> str:
    """按注册顺序判定当前页面类型；都不匹配返回 `unknown`。"""
    for kind, detector in PAGE_KIND_DETECTORS:
        if detector(focus, screen):
            return kind
    return "unknown"


# 内置三类页面（顺序与历史 if/elif 实现一致：chat → resume → list）
register_page_kind("chat", detect_chat)
register_page_kind("resume", detect_resume)
register_page_kind("list", detect_geek_list)


def tap_settled(
    device: Device,
    logger: RunLogger,
    point: tuple[int, int],
    label: str,
    *,
    gesture: str = "tap",
    duration_ms: int | None = None,
) -> None:
    """注入触摸。

    实机验证：`uiautomator dump` 会短暂吞掉紧随其后的触摸注入，因此读屏
    之后、落点之前必须先等待页面落定，否则点击会被丢弃。
    """
    time.sleep(TAP_SETTLE_SECONDS)
    logger.event(
        "tap", label=label, point=point, gesture=gesture, duration_ms=duration_ms
    )
    if gesture == "tap":
        device.tap(*point, hold_ms=duration_ms or 80)
    elif gesture == "press":
        device.press(*point, duration_ms=duration_ms or 200)
    elif gesture == "system_tap":
        device.system_tap(*point)
    elif gesture == "mouse_tap":
        device.mouse_tap(*point)
    elif gesture == "micro_tap":
        device.micro_tap(*point, duration_ms=duration_ms or 60)
    else:
        raise ValueError(f"未知触摸策略：{gesture!r}")


# ---- 读屏与页面等待 -----------------------------------------------------
def read_screen(device: Device) -> tuple[str, Screen]:
    """读屏。

    读屏同时做风控熔断：一旦当前页出现验证码/风控提示，立即停机，
    绝不在该页做任何点击、滑动或返回动作。
    """
    focus = device.focused_window()
    screen = Screen(device.dump_ui())
    keyword = risk_signal(screen)
    if keyword:
        raise StopRun(
            f"检测到风控/验证页（命中关键词：{keyword!r}）；已熔断停机，等待人工接管"
        )
    return focus, screen


def wait_for_kind(
    device: Device, target: str, timeout: float = 6.0
) -> tuple[str, Screen] | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            focus, screen = read_screen(device)
        except DeviceError:
            time.sleep(0.4)
            continue
        last = (focus, screen)
        if page_kind(focus, screen) == target:
            return last
        time.sleep(0.4)
    return last if last and page_kind(*last) == target else None


def go_back_to(device: Device, target: str, max_backs: int = 4) -> tuple[str, Screen]:
    """回到目标页。

    只允许在**已注册的已知页面**之间按返回键；遇到无法归类的页面
    （弹窗、验证页、异常页）时**拒绝盲按返回键**，直接熔断停机留证。
    新增业务模块注册新页面类型后，这里会自动放行新页面之间的返回。
    """
    allowed = known_page_kinds()
    for _ in range(max_backs):
        focus, screen = read_screen(device)
        kind = page_kind(focus, screen)
        if kind == target:
            return focus, screen
        if kind not in allowed:
            raise StopRun(
                f"页面无法归类（{kind}），拒绝盲按返回键；熔断停机留证：{focus}"
            )
        device.back()
        time.sleep(1.0)
    focus, screen = read_screen(device)
    raise StopRun(f"返回 {target} 失败；当前: {page_kind(focus, screen)} | {focus}")


def enter_candidate_page(
    device: Device,
    logger: RunLogger,
    name: str,
    point: tuple[int, int],
    attempts: int,
    timeout: float,
    *,
    focus_kind: str = "resume",
    cards_fn: CardFn = list_cards,
) -> tuple[str, Screen] | None:
    """点击卡片进入详情页。

    仅导航点击允许重试（每次重新定位卡片）；不涉及任何不可逆动作。
    `focus_kind` / `cards_fn` 参数化后，其他列表页（如「新招呼」）可复用。
    """
    current = point
    for attempt in range(1, attempts + 1):
        logger.event("enter_candidate", name=name, point=current, attempt=attempt)
        tap_settled(device, logger, current, f"enter:{name}")
        entered = wait_for_kind(device, focus_kind, timeout=timeout)
        if entered is not None:
            return entered
        retry_focus, retry_screen = read_screen(device)
        if page_kind(retry_focus, retry_screen) != "list":
            return None
        relocated = next(
            (card for card in cards_fn(retry_screen) if card[0] == name), None
        )
        if relocated is None:
            return None
        current = relocated[1]
    return None


def scroll_down(
    device: Device,
    logger: RunLogger,
    *,
    start_ratio: float = SCROLL_START_RATIO,
    end_ratio: float = SCROLL_END_RATIO,
    duration_ms: int = SCROLL_DURATION_MS,
) -> None:
    """受控下滑（方向与节奏可参数化）。

    BOSS 的 RecyclerView 会对快速滑动产生惯性甩动，一步跳过整段区块，
    因此用较长的滑动时长（默认 800ms）压住惯性，保证每屏都能读到区块标题。
    """
    width, height = device.screen_size()
    center_x = width // 2
    start_y = round(height * start_ratio)
    end_y = round(height * end_ratio)
    device.swipe(center_x, start_y, center_x, end_y, duration_ms)
    logger.event("scrolled_down")
    time.sleep(1.0)


# ---- 证据与文件名工具 ----------------------------------------------------
def evidence_entry(path: Path) -> dict[str, str]:
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "type": "screenshot" if path.suffix == ".png" else "ui_tree",
    }


def safe_name(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]", "_", name).strip("_") or "candidate"
