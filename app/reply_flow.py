"""2号状态机骨架：单候选人全链路与一页批量编排。

本模块只负责把已验证的原语串成稳定状态机，不负责语义判断：
- 采集、规划、发送、回读仍由 reply_main / reply_store / reply_ui 完成；
- 外部 Agent 给出 action（send/hold）、允许 phrase_key 与非空理由（建议100–300字）；
- 默认 dry-run，只有 execute=True 才跨越发送边界。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import reply_main as primitives
from .reply_plan import REPLY_ACTIONS, load_allowed_phrases, plan_new_greeting
from .reply_store import ReplyStore
from .reply_ui import all_new_greeting_cards, common_phrase_rows
from .screen_ops import StopRun, page_kind


def load_decisions(path: Path | str) -> dict[str, dict[str, object]]:
    """读取批量决策：{姓名: {action, phrase_key?, comment, max_scrolls?}}。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("decisions 必须是 {姓名: {action, phrase_key?, comment}} 对象")
    catalog = load_allowed_phrases()
    decisions: dict[str, dict[str, object]] = {}
    for raw_name, raw_spec in payload.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_spec, dict):
            raise ValueError(f"无效决策条目：{raw_name!r}")
        action = str(raw_spec.get("action") or "").strip()
        phrase_key = str(raw_spec.get("phrase_key") or "").strip()
        comment = str(raw_spec.get("comment") or "").strip()
        if action not in REPLY_ACTIONS:
            raise ValueError(f"{name} 的 action 必须是 {list(REPLY_ACTIONS)}")
        if not comment:
            raise ValueError(f"{name} 缺少 comment")
        if action == "send" and phrase_key not in catalog:
            raise ValueError(f"{name} 的 phrase_key 不在允许清单中：{phrase_key!r}")
        if action == "hold" and phrase_key:
            raise ValueError(f"{name} 的 hold 动作不得绑定 phrase_key")
        decision: dict[str, object] = {
            "action": action,
            "phrase_key": phrase_key or None,
            "comment": comment,
        }
        if raw_spec.get("max_scrolls"):
            decision["max_scrolls"] = int(raw_spec["max_scrolls"])
        decisions[name] = decision
    return decisions


def _phrase_order() -> list[str]:
    return [item["text"] for item in load_allowed_phrases().values()]


def ensure_phrase_visible(
    device,
    logger,
    phrase_text: str,
    *,
    max_pages: int = 12,
) -> None:
    """把目标话术滚到当前屏；方向由配置顺序与当前可见行决定。"""
    order = _phrase_order()
    if phrase_text not in order:
        raise StopRun("目标话术不在当前配置清单中")
    target = order.index(phrase_text)
    for index in range(max_pages):
        focus, screen, _ = primitives._capture(
            device, logger, f"ensure_phrase_{index:02d}"
        )
        if page_kind(focus, screen) != "chat":
            raise StopRun("常用语查找期间离开聊天页")
        rows = common_phrase_rows(screen)
        if any(row.phrase_text == phrase_text for row in rows):
            return
        if not rows:
            raise StopRun("常用语面板未打开或没有可发送行")
        visible = [
            order.index(row.phrase_text)
            for row in rows
            if row.phrase_text in order
        ]
        if not visible:
            raise StopRun("常用语行与当前配置无法逐字匹配")
        bounds = primitives._phrase_list_bounds(screen)
        x = min(bounds.right - 180, bounds.left + 140)
        margin = max(72, min(140, bounds.height // 4))
        if target < min(visible):
            device.swipe(
                x, bounds.top + margin, x, bounds.bottom - margin, duration_ms=900
            )
        else:
            device.swipe(
                x, bounds.bottom - margin, x, bounds.top + margin, duration_ms=900
            )
        time.sleep(1.0)
    raise StopRun(f"未能在常用语面板中定位话术：{phrase_text!r}")


def _return_to_list(
    device,
    logger,
    expected_absent_display_name: str | None = None,
) -> None:
    try:
        primitives.return_to_new_greetings(
            device,
            logger,
            expected_absent_display_name,
        )
    except Exception as exc:
        logger.event(
            "return_list_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        if expected_absent_display_name:
            raise


def collect_candidate(
    device,
    logger,
    store: ReplyStore,
    display_name: str,
    *,
    max_scrolls: int = 20,
) -> dict[str, object]:
    """打开指定候选人，读取并落库简历，然后回到会话列表。"""
    opened = primitives.open_named(device, logger, store, display_name)
    try:
        captured = primitives.capture_candidate_resume(
            device,
            logger,
            store,
            int(opened["conversation_id"]),
            max_scrolls,
        )
        return {
            "display_name": display_name,
            "conversation_id": opened["conversation_id"],
            "candidate_id": opened["candidate_id"],
            "inbound_message_id": captured["message"]["inbound_message_id"],
            "record": captured["record"],
            "saved": captured["saved"],
        }
    finally:
        _return_to_list(device, logger)


def run_candidate(
    device,
    logger,
    store: ReplyStore,
    display_name: str,
    action: str,
    phrase_key: str | None,
    comment: str,
    *,
    execute: bool = False,
    max_scrolls: int = 20,
) -> dict[str, object]:
    """单个候选人的完整状态机：打开 → 简历 → 规划 → 发送 → 回列表。"""
    plan = plan_new_greeting(
        action,
        comment,
        phrase_key=phrase_key,
    )

    opened = primitives.open_named(device, logger, store, display_name)
    expected_absent_display_name: str | None = None
    try:
        captured = primitives.capture_candidate_resume(
            device,
            logger,
            store,
            int(opened["conversation_id"]),
            max_scrolls,
        )
        conversation_id = int(opened["conversation_id"])
        inbound_message_id = int(captured["message"]["inbound_message_id"])
        planned = primitives.plan_action(
            logger,
            store,
            conversation_id,
            inbound_message_id,
            plan.decision,
            plan.reason,
        )
        action_id = int(planned["reply_action_id"])
        summary: dict[str, object] = {
            "display_name": display_name,
            "conversation_id": conversation_id,
            "candidate_id": opened["candidate_id"],
            "inbound_message_id": inbound_message_id,
            "reply_action_id": action_id,
            "decision": plan.decision,
            "action": plan.action,
            "phrase_key": plan.phrase_key,
            "intent": plan.intent,
            "execute": execute,
            "state": planned["state"],
        }
        if plan.action == "hold" or not execute:
            logger.event("run_one_skipped", **summary)
            return summary

        phrase = primitives.load_phrase(str(plan.phrase_key))
        primitives.inspect_phrases(device, logger, store, conversation_id)
        ensure_phrase_visible(device, logger, phrase["text"])
        sent = primitives.send_planned_reply(
            device,
            logger,
            store,
            conversation_id,
            inbound_message_id,
            action_id,
            str(plan.phrase_key),
            "tap",
        )
        if sent["state"] == "sent":
            expected_absent_display_name = display_name
        summary.update(
            {
                "state": sent["state"],
                "verified_at": sent.get("verified_at"),
                "phrase_text": sent.get("phrase_text"),
            }
        )
        logger.event("run_one_done", **summary)
        return summary
    finally:
        _return_to_list(device, logger, expected_absent_display_name)


def run_page(
    device,
    logger,
    store: ReplyStore,
    decisions: dict[str, dict[str, object]],
    *,
    max_cards: int = 8,
    execute: bool = False,
    default_max_scrolls: int = 20,
) -> list[dict[str, object]]:
    """按当前会话列表从上到下处理已提供决策的候选人。"""
    results: list[dict[str, object]] = []
    processed: set[str] = set()
    while len(results) < max_cards:
        focus, screen, _ = primitives._capture(device, logger, "run_page_list")
        kind = page_kind(focus, screen)
        if kind != "new_greetings":
            raise StopRun(f"当前不是会话列表：{kind!r}")
        cards = all_new_greeting_cards(screen)
        selected = next(
            (
                card
                for card in cards
                if card.display_name in decisions
                and card.display_name not in processed
            ),
            None,
        )
        if selected is None:
            break
        processed.add(selected.display_name)
        spec = decisions[selected.display_name]
        results.append(
            run_candidate(
                device,
                logger,
                store,
                selected.display_name,
                str(spec["action"]),
                str(spec["phrase_key"]) if spec.get("phrase_key") else None,
                str(spec["comment"]),
                execute=execute,
                max_scrolls=int(spec.get("max_scrolls", default_max_scrolls)),
            )
        )
    missing = [name for name in decisions if name not in processed]
    logger.event(
        "run_page_done",
        count=len(results),
        names=[str(result["display_name"]) for result in results],
        execute=execute,
        unprocessed=missing,
    )
    return results
