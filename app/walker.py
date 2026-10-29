"""牛人列表遍历骨架（ListWalker）。

把「导航到列表 → 读屏 → 选下一张未处理卡片 → 进入详情 → 采集落盘」这段公共流程
从控制层抽出；此前 `run` 与 `collect` 两条命令各自实现了一遍这段循环。

设计要点：

- **只承载公共骨架**：配额、是否发送、评语、收工文案等模块特有编排仍留在控制层，
  由调用方在骨架之上编排。
- **依赖注入**：卡片选择器（`cards_fn`）与采集函数（`collect_fn`）由调用方注入，
  便于「新招呼」等其它列表页复用同一骨架。
- **不改变既有行为**：选卡防重、停滞下滑上限、`skipped_by_name` / `finish` / `persisted`
  等事件的名称与文案与历史实现完全一致。

其它列表页（如「新招呼」）复用方式：注册自己的页面识别器（`screen_ops.register_page_kind`），
并传入自己的 `cards_fn` / `collect_fn` 构造一个新的 `ListWalker` 即可。
"""

from __future__ import annotations

from typing import Callable

from .device import Device
from .logging import RunLogger
from .resume import list_cards
from .screen_ops import (
    CardFn,
    enter_candidate_page,
    go_back_to,
    page_kind,
    read_screen,
    scroll_down,
)

# 采集函数签名：与 resume_collector.collect_resume 一致。
CollectFn = Callable[[Device, RunLogger, int, str], tuple[dict, str, list]]


class ListWalker:
    """通用列表遍历骨架。"""

    def __init__(
        self,
        device: Device,
        logger: RunLogger,
        store,
        *,
        cards_fn: CardFn = list_cards,
        collect_fn: CollectFn | None = None,
        max_stagnant_scrolls: int = 3,
        enter_attempts: int = 3,
        enter_timeout: float = 12.0,
        empty_reason: str = "列表无新对象",
    ) -> None:
        self.device = device
        self.logger = logger
        self.store = store
        self.cards_fn = cards_fn
        self.collect_fn = collect_fn
        self.max_stagnant_scrolls = max_stagnant_scrolls
        self.enter_attempts = enter_attempts
        self.enter_timeout = enter_timeout
        self.empty_reason = empty_reason
        # 骨架自维护的状态：已处理集合、列表连续无新对象的滑动计数
        self.processed: set[str] = set()
        self.stagnant = 0

    # ---- 阶段 1：确保处于目标列表 ----------------------------------------
    def ensure_list(self, target: str = "list") -> None:
        """确保处于目标列表页；必要时拉起 BOSS 并回退到列表锚点。"""
        if not self.device.boss_foreground():
            self.logger.event("launch_boss")
            self.device.launch_boss()
        go_back_to(self.device, target)

    # ---- 阶段 2：选下一张可处理卡片 --------------------------------------
    def next_card(
        self, *, skip_seen: bool = True
    ) -> tuple[str, tuple[int, int]] | None:
        """返回下一张可处理卡片；列表连续无新对象达上限时返回 None。

        防重顺序与历史实现一致：先跳过已处理、再按姓名查库（`seen_by_name`），
        再按终态（`greeting_finalized_for`）跳过；命中即记 `skipped_by_name`。
        """
        while True:
            focus, screen = read_screen(self.device)
            if page_kind(focus, screen) != "list":
                go_back_to(self.device, "list")
                continue
            target = None
            for card in self.cards_fn(screen):
                card_name = card[0]
                if card_name in self.processed:
                    continue
                if skip_seen and self.store.seen_by_name(card_name):
                    self.processed.add(card_name)
                    self.logger.event(
                        "skipped_by_name", name=card_name, reason="库中已存在"
                    )
                    continue
                if self.store.greeting_finalized_for(card_name):
                    self.processed.add(card_name)
                    self.logger.event(
                        "skipped_by_name", name=card_name, reason="已封口"
                    )
                    continue
                target = card
                break

            if target is not None:
                self.stagnant = 0
                return target

            scroll_down(self.device, self.logger)
            self.stagnant += 1
            if self.stagnant >= self.max_stagnant_scrolls:
                self.logger.event("finish", reason=self.empty_reason)
                return None

    # ---- 阶段 3：进入详情并采集落盘 --------------------------------------
    def capture(
        self, card: tuple[str, tuple[int, int]], max_scrolls: int
    ) -> dict[str, object] | None:
        """进入卡片 → 多屏采集 → 落盘；未进入详情页时返回 None（后续由调用方决定）。"""
        name, point = card
        entered = enter_candidate_page(
            self.device,
            self.logger,
            name,
            point,
            self.enter_attempts,
            self.enter_timeout,
        )
        if entered is None:
            return None
        if self.collect_fn is None:
            raise RuntimeError("ListWalker 未注入 collect_fn，无法采集")
        record, source_activity, evidence = self.collect_fn(
            self.device, self.logger, max_scrolls, name
        )
        saved = self.store.save_snapshot(record, source_activity, evidence)
        self.processed.add(name)
        self.logger.event(
            "persisted",
            candidate_id=saved["candidate_id"],
            created=saved["created"],
            fingerprint=saved["fingerprint"],
            display_name=record.get("display_name"),
        )
        return {
            "candidate_id": int(saved["candidate_id"]),
            "name": name,
            "record": record,
        }
