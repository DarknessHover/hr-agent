"""2号状态机控制入口：全量扫描新招呼并安全回复单条会话。"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from .device import Device, DeviceError, parse_bounds
from .logging import FLOW_REPLY_NEW_GREETINGS, RunLogger
from .reply_plan import allowed_phrase, plan_new_greeting
from .reply_store import ReplyStore, SENDABLE_REPLY_DECISIONS
from .review import evaluation_comment_advisory
from .resume_collector import collect_resume
from .reply_ui import (
    ChatSnapshot,
    CommonPhraseRow,
    NewGreetingCard,
    all_new_greeting_cards,
    chat_display_name,
    chat_snapshot,
    common_phrase_rows,
    common_words_point,
    delivered_outbound_message_texts,
    new_greeting_cards,
    outbound_message_texts,
)  # 同时注册页面类型
from .resume import Screen, resume_identity, risk_signal
from .screen_ops import (
    StopRun,
    evidence_entry,
    go_back_to,
    page_kind,
    read_screen,
    tap_settled,
    wait_for_kind,
)


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = ROOT / "data" / "reply_agent.sqlite3"
SCHEMA_PATH = ROOT / "data" / "schema.sql"
REPLY_SCHEMA_PATH = ROOT / "data" / "reply_schema.sql"
PROFILE_PATH = ROOT / "config" / "job_profiles.yaml"
PHRASES_PATH = ROOT / "config" / "reply_phrases.yaml"


def _capture(
    device: Device, logger: RunLogger, stem: str
) -> tuple[str, Screen, list[dict[str, str]]]:
    focus = device.require_boss_foreground()
    xml = device.dump_ui()
    screen = Screen(xml)
    keyword = risk_signal(screen)
    if keyword:
        raise StopRun(f"检测到风控/验证信号：{keyword!r}")
    critical_tokens = (
        "before_send",
        "after_send",
        "retry_no_effect",
        "reconciliation",
        "manual_sent",
    )
    if not logger.should_capture(
        critical=any(token in stem for token in critical_tokens)
    ):
        return focus, screen, []
    xml_path = logger.save_evidence(stem + ".xml", xml)
    png_path = logger.save_evidence(stem + ".png", device.screenshot())
    return focus, screen, [evidence_entry(xml_path), evidence_entry(png_path)]


def _card_payload(card: NewGreetingCard) -> dict[str, object]:
    return {
        "display_name": card.display_name,
        "position_text": card.position_text,
        "meta_text": card.meta_text,
        "preview_text": card.preview_text,
        "unread_count": card.unread_count,
        "unread_signal": card.unread_signal,
        "point": card.point,
        "conversation_fingerprint": card.conversation_fingerprint,
    }


def _chat_payload(snapshot: ChatSnapshot) -> dict[str, object]:
    return {
        "display_name": snapshot.display_name,
        "subtitle": snapshot.subtitle,
        "latest_inbound_text": snapshot.latest_inbound_text,
        "visible_time_text": snapshot.visible_time_text,
        "avatar_point": snapshot.avatar_point,
    }


def capture_chat(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
) -> tuple[ChatSnapshot, dict[str, object]]:
    focus, screen, evidence = _capture(device, logger, "chat_current")
    if page_kind(focus, screen) != "chat":
        raise StopRun("当前不是聊天页，拒绝读取入站消息")
    conversation = store.conversation(conversation_id)
    if conversation is None:
        raise StopRun(f"会话不存在：{conversation_id}")
    try:
        snapshot = chat_snapshot(screen)
    except ValueError as exc:
        raise StopRun(str(exc)) from exc
    expected = str(conversation["list_display_name"])
    if snapshot.display_name != expected:
        raise StopRun(
            f"聊天身份不一致：expected={expected!r}, actual={snapshot.display_name!r}"
        )
    message = store.stage_inbound_message(
        conversation_id,
        snapshot.latest_inbound_text,
        snapshot.visible_time_text,
        evidence,
    )
    logger.event(
        "inbound_message_staged",
        conversation_id=conversation_id,
        **_chat_payload(snapshot),
        **message,
    )
    return snapshot, {**message, "evidence": evidence}


def capture_candidate_resume(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    max_scrolls: int,
) -> dict[str, object]:
    snapshot, message = capture_chat(
        device,
        logger,
        store,
        conversation_id,
    )
    conversation = store.conversation(conversation_id)
    assert conversation is not None
    candidate_id = int(conversation["candidate_id"])

    entered = None
    # 真机回放显示：聊天气泡左侧头像对 120ms tap 经常无响应，随后
    # 200ms press 才能稳定进入简历页。把已验证策略放在首位，既减少
    # 一次 12 秒的无效等待，也保留 120ms tap 作为机型/UI 抖动回退。
    # 2026-10-21 连续真机样本：200ms press 多次无效，随后120ms
    # DOWN/UP成功进入同一简历页。优先短触摸，减少每人一次超时等待。
    avatar_strategies = (("tap", 120), ("press", 200))
    for attempt, (gesture, duration_ms) in enumerate(avatar_strategies, start=1):
        logger.event(
            "open_resume_from_chat",
            conversation_id=conversation_id,
            display_name=snapshot.display_name,
            avatar_point=snapshot.avatar_point,
            attempt=attempt,
        )
        tap_settled(
            device,
            logger,
            snapshot.avatar_point,
            f"open_resume:{snapshot.display_name}",
            gesture=gesture,
            duration_ms=duration_ms,
        )
        entered = wait_for_kind(device, "resume", timeout=12.0)
        if entered is not None:
            break
        retry_focus, retry_screen = read_screen(device)
        if page_kind(retry_focus, retry_screen) != "chat":
            raise StopRun("点击头像后进入未知页面，拒绝重试")
        retry_snapshot = chat_snapshot(retry_screen)
        if retry_snapshot.display_name != snapshot.display_name:
            raise StopRun("头像点击重试前聊天身份发生漂移")
        snapshot = retry_snapshot
    if entered is None:
        raise StopRun("两次受控头像点击后仍未进入简历页")

    _, resume_screen = entered
    on_page = resume_identity(resume_screen)
    if on_page != snapshot.display_name:
        raise StopRun(
            f"简历身份不一致：expected={snapshot.display_name!r}, actual={on_page!r}"
        )
    record, source_activity, resume_evidence = collect_resume(
        device, logger, max_scrolls, snapshot.display_name
    )
    saved = store.update_candidate_snapshot(
        candidate_id,
        snapshot.display_name,
        record,
        source_activity,
        resume_evidence,
    )
    logger.event("reply_candidate_persisted", conversation_id=conversation_id, **saved)

    chat_focus, chat_screen = go_back_to(device, "chat", max_backs=2)
    returned = chat_snapshot(chat_screen)
    if returned.display_name != snapshot.display_name:
        raise StopRun("从简历返回后聊天身份发生漂移")
    logger.event(
        "returned_to_chat",
        conversation_id=conversation_id,
        display_name=returned.display_name,
        focus=chat_focus,
    )
    return {
        "conversation_id": conversation_id,
        "candidate_id": candidate_id,
        "message": message,
        "record": record,
        "saved": saved,
    }


def inspect_phrases(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
) -> dict[str, object]:
    snapshot, message = capture_chat(
        device,
        logger,
        store,
        conversation_id,
    )
    panel_screen = None
    evidence: list[dict[str, str]] = []
    point = (0, 0)
    # 2026-10-22 连续 8 名候选人均以 120ms tap 打开面板；200ms press 仅作回退。
    strategies = (("tap", 120), ("press", 200))
    for attempt, (gesture, duration_ms) in enumerate(strategies, start=1):
        _, screen, before_evidence = _capture(
            device, logger, f"before_common_words_{attempt:02d}"
        )
        try:
            point = common_words_point(screen)
        except ValueError as exc:
            raise StopRun(str(exc)) from exc
        before_signature = tuple(
            (node.short_id, node.text.strip(), node.element.attrib.get("bounds", ""))
            for node in screen.nodes()
        )
        tap_settled(
            device,
            logger,
            point,
            f"open_common_words:attempt_{attempt}",
            gesture=gesture,
            duration_ms=duration_ms,
        )
        # 常用语面板仍属于 ChatRoomActivity，不依赖 page_kind 变化。
        time.sleep(2.0)
        focus, candidate_screen, after_evidence = _capture(
            device, logger, f"common_words_after_{attempt:02d}"
        )
        if page_kind(focus, candidate_screen) != "chat":
            raise StopRun("打开常用语后离开聊天页，停机留证")
        after_signature = tuple(
            (node.short_id, node.text.strip(), node.element.attrib.get("bounds", ""))
            for node in candidate_screen.nodes()
        )
        if after_signature != before_signature and common_phrase_rows(candidate_screen):
            panel_screen = candidate_screen
            evidence = after_evidence
            logger.event(
                "common_words_touch_succeeded",
                attempt=attempt,
                gesture=gesture,
                duration_ms=duration_ms,
                before_evidence=before_evidence,
                after_evidence=after_evidence,
            )
            break
        logger.event(
            "common_words_touch_no_effect",
            attempt=attempt,
            gesture=gesture,
            duration_ms=duration_ms,
            before_evidence=before_evidence,
            after_evidence=after_evidence,
        )
    if panel_screen is None:
        raise StopRun("两种受控触摸均未打开常用语面板")
    texts = [node.text.strip() for node in panel_screen.nodes() if node.text.strip()]
    logger.event(
        "common_words_panel_opened",
        conversation_id=conversation_id,
        display_name=snapshot.display_name,
        point=point,
        visible_text_count=len(texts),
        evidence=evidence,
    )
    return {"chat": _chat_payload(snapshot), "message": message, "texts": texts}


def plan_action(
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    inbound_message_id: int,
    decision: str,
    comment: str,
) -> dict[str, object]:
    profile_version = "sha256:" + hashlib.sha256(PROFILE_PATH.read_bytes()).hexdigest()
    planned = store.plan_reply_action(
        conversation_id,
        inbound_message_id,
        decision,
        comment,
        str(PROFILE_PATH.relative_to(ROOT)).replace("\\", "/"),
        profile_version,
    )
    normalized_comment = comment.strip()
    comment_advisory = evaluation_comment_advisory(normalized_comment)
    logger.event(
        "reply_action_planned",
        conversation_id=conversation_id,
        inbound_message_id=inbound_message_id,
        decision=decision,
        comment_len=len(normalized_comment),
        comment_length_advisory=comment_advisory,
        profile_version=profile_version,
        **planned,
    )
    return planned


def load_phrase(phrase_key: str) -> dict[str, str]:
    """从统一允许清单读取话术，并把配置错误转换为状态机停机。"""
    try:
        return allowed_phrase(phrase_key, PHRASES_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StopRun(str(exc)) from exc


def _send_strategies_for_y(y: int) -> tuple[tuple[str, int, tuple[int, int]], ...]:
    """当前真机默认的发送手势序列；y 保留给未来的区域策略。

    2026-10-22 连续 8 次地址话术真机发送均以 120ms tap 成功，因此移除
    mouse/micro 等实验分支，只保留 tap → press → system_tap 三级回退。
    """
    _ = y
    return (
        ("tap", 120, (0, 0)),
        ("press", 200, (0, 0)),
        ("system_tap", 0, (0, 0)),
    )


def _select_send_strategy(
    y: int, prior_attempts: int, gesture_override: str | None = None
) -> tuple[str, int, tuple[int, int]]:
    """选择一次发送策略；显式覆盖只能命中预定义安全策略。"""
    strategies = _send_strategies_for_y(y)
    if gesture_override:
        matches = [item for item in strategies if item[0] == gesture_override]
        if not matches:
            raise StopRun(f"当前按钮区域不允许发送策略：{gesture_override!r}")
        return matches[0]
    return strategies[min(prior_attempts, len(strategies) - 1)]


def _phrase_list_bounds(screen: Screen):
    node = next(
        (node for node in screen.nodes() if node.short_id == "recyclerView"), None
    )
    if node is None:
        raise StopRun("当前聊天页没有打开常用语列表")
    try:
        return parse_bounds(node.element.attrib.get("bounds", ""))
    except (DeviceError, StopRun, ValueError) as exc:
        raise StopRun("无法解析常用语列表边界") from exc


def locate_phrase_in_panel(
    device: Device,
    logger: RunLogger,
    expected_display_name: str,
    phrase_text: str,
    *,
    max_scrolls: int = 12,
) -> tuple[CommonPhraseRow, list[dict[str, str]]]:
    """在已打开的常用语面板中逐屏查找，并返回正文绑定的发送按钮。"""
    seen: set[tuple[str, ...]] = set()
    stagnant_scrolls = 0
    for index in range(max_scrolls + 1):
        _, screen, evidence = _capture(device, logger, f"phrase_page_{index:02d}")
        actual_display_name = chat_display_name(screen)
        if actual_display_name != expected_display_name:
            raise StopRun(
                "常用语查找期间聊天身份漂移："
                f"expected={expected_display_name!r}, actual={actual_display_name!r}"
            )
        rows = common_phrase_rows(screen)
        signature = tuple(row.phrase_text for row in rows)
        logger.event(
            "phrase_page_read",
            index=index,
            display_name=actual_display_name,
            phrases=signature,
            evidence=evidence,
        )
        match = next((row for row in rows if row.phrase_text == phrase_text), None)
        if match is not None:
            return match, evidence
        if not rows:
            raise StopRun("常用语面板没有可绑定正文与发送按钮的行")
        bounds = _phrase_list_bounds(screen)
        if index == max_scrolls:
            break
        # 常用语 RecyclerView 即使处于底部小面板中也能独立滚动。触碰其顶部
        # 把手/按钮会把整个面板展开并改变后续坐标，因此所有滑动严格限制在
        # RecyclerView 内部，并避开右侧“发送”按钮。
        x = min(bounds.right - 180, bounds.left + 140)
        margin = max(72, min(140, bounds.height // 4))
        start_y = bounds.bottom - margin
        end_y = bounds.top + margin
        if start_y <= end_y:
            raise StopRun("常用语列表内部可滑动区域过小")
        if signature in seen:
            stagnant_scrolls += 1
        else:
            seen.add(signature)
            stagnant_scrolls = 0
        if stagnant_scrolls == 0:
            device.swipe(x, start_y, x, end_y, duration_ms=900)
            scroll_gesture = "internal_slow_swipe"
        elif stagnant_scrolls == 1:
            device.drag(
                x,
                start_y,
                x,
                end_y,
                duration_ms=1000,
                steps=12,
            )
            scroll_gesture = "internal_slow_drag"
        else:
            raise StopRun("两种常用语内部慢速滚动均未改变内容")
        logger.event(
            "phrase_panel_scrolled",
            index=index,
            start=(x, start_y),
            end=(x, end_y),
            gesture=scroll_gesture,
            stagnant_scrolls=stagnant_scrolls,
        )
        time.sleep(1.0)
    raise StopRun(f"常用语面板中未找到逐字匹配的话术：{phrase_text!r}")


def close_common_words_panel(
    device: Device,
    logger: RunLogger,
    screen: Screen,
    *,
    label: str,
) -> tuple[str, Screen, list[dict[str, str]]]:
    """通过输入栏常用语图标收起面板；禁止用系统返回键退出聊天。"""
    current = screen
    for attempt, (gesture, duration_ms) in enumerate(
        (("tap", 120), ("press", 200)), start=1
    ):
        try:
            point = common_words_point(current)
        except ValueError as exc:
            raise StopRun(str(exc)) from exc
        tap_settled(
            device,
            logger,
            point,
            f"{label}:attempt_{attempt}",
            gesture=gesture,
            duration_ms=duration_ms,
        )
        focus, current, evidence = _capture(
            device, logger, f"{label}_after_{attempt:02d}"
        )
        if page_kind(focus, current) != "chat":
            raise StopRun("关闭常用语面板时离开聊天页，停机留证")
        if not common_phrase_rows(current):
            logger.event(
                "common_words_panel_closed",
                attempt=attempt,
                gesture=gesture,
                duration_ms=duration_ms,
                evidence=evidence,
            )
            return focus, current, evidence
    raise StopRun("两种受控触摸均未关闭常用语面板")


def send_planned_reply(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    inbound_message_id: int,
    reply_action_id: int,
    phrase_key: str,
    send_gesture_override: str | None = None,
) -> dict[str, object]:
    """发送一个已规划动作；跨越边界后只回读，绝不重试发送。"""
    conversation = store.conversation(conversation_id)
    action = store.reply_action(reply_action_id)
    if conversation is None or action is None:
        raise StopRun("会话或回复动作不存在")
    if str(action["state"]) != "planned":
        raise StopRun(
            "只有 planned 动作可以进入发送流程："
            f"reply_action_id={reply_action_id}, state={action['state']}"
        )
    if str(action["decision"]) not in SENDABLE_REPLY_DECISIONS:
        raise StopRun(
            f"回复决策不允许发送：decision={action['decision']}"
        )
    if (
        int(action["conversation_id"]) != conversation_id
        or int(action["inbound_message_id"]) != inbound_message_id
    ):
        raise StopRun("回复动作与指定会话/入站消息不一致")
    phrase = load_phrase(phrase_key)
    # phrase_key 必须来自完整允许清单；仓储 begin_send 会再次逐字核验正文。

    expected_name = str(conversation["list_display_name"])
    focus, context_screen, context_evidence = _capture(
        device, logger, "send_context"
    )
    if page_kind(focus, context_screen) != "chat":
        raise StopRun("发送前当前不是聊天页")
    if chat_display_name(context_screen) != expected_name:
        raise StopRun("发送前聊天身份与回复动作不一致")
    # 常用语面板展开时会遮住屏幕底部的最新消息，此时可见的最靠下入站消息
    # 可能属于更早的一轮。面板存在时以动作绑定的消息与逐字话术为准，不再
    # 用被遮挡的可见消息反推“最新”；面板关闭时仍执行严格的消息漂移检查。
    if common_phrase_rows(context_screen):
        logger.event(
            "send_context_inbound_obscured_by_phrase_panel",
            conversation_id=conversation_id,
            inbound_message_id=inbound_message_id,
            display_name=expected_name,
            evidence=context_evidence,
        )
    else:
        try:
            context_snapshot = chat_snapshot(context_screen)
        except ValueError as exc:
            raise StopRun("发送前既无法读取入站消息，也未检测到常用语面板") from exc
        current_message = store.stage_inbound_message(
            conversation_id,
            context_snapshot.latest_inbound_text,
            context_snapshot.visible_time_text,
            context_evidence,
        )
        if int(current_message["inbound_message_id"]) != inbound_message_id:
            raise StopRun(
                "发送前最新入站消息不是计划绑定消息："
                f"expected={inbound_message_id}, "
                f"actual={current_message['inbound_message_id']}"
            )
    row, _ = locate_phrase_in_panel(
        device, logger, expected_name, phrase["text"]
    )

    # 点击前最后一次重新读屏、重新绑定同一行，任何坐标都不跨观察复用。
    _, pre_screen, pre_evidence = _capture(device, logger, "before_send")
    if chat_display_name(pre_screen) != expected_name:
        raise StopRun("发送前聊天身份发生漂移")
    if common_phrase_rows(pre_screen):
        logger.event(
            "before_send_inbound_obscured_by_phrase_panel",
            conversation_id=conversation_id,
            inbound_message_id=inbound_message_id,
            display_name=expected_name,
            evidence=pre_evidence,
        )
    else:
        try:
            pre_snapshot = chat_snapshot(pre_screen)
        except ValueError as exc:
            raise StopRun("发送前入站消息不可见且常用语面板状态异常") from exc
        staged = store.stage_inbound_message(
            conversation_id,
            pre_snapshot.latest_inbound_text,
            pre_snapshot.visible_time_text,
            pre_evidence,
        )
        if int(staged["inbound_message_id"]) != inbound_message_id:
            raise StopRun("发送前最新入站消息发生变化")
    row = next(
        (
            candidate
            for candidate in common_phrase_rows(pre_screen)
            if candidate.phrase_text == phrase["text"]
        ),
        None,
    )
    if row is None:
        raise StopRun("发送前逐字话术或对应按钮已不在当前屏")

    pending = store.begin_send(
        reply_action_id,
        conversation_id,
        inbound_message_id,
        phrase["key"],
        phrase["text"],
    )
    prior_attempts = store.send_attempt_count(reply_action_id)
    # 每次仅在“完整聊天回读证明没有产生目标出站气泡”且用户明确授权后
    # 才会回到 planned。真机样本显示：面板底部按钮（约 y>=1450）对
    # 200ms press 的首击成功率较好，而中上部按钮近期连续吞掉长按，
    # 120ms 短触摸反而成功。因此按即时绑定的按钮纵向位置选择首手势；
    # 重试仍必须改变参数，避免机械重复同一无效动作。
    # 中心点连续无效后，只在同一个已绑定 btn_send 的内部换点；按钮宽
    # 100px，±24px 仍有余量，不会越界触碰正文或相邻行。
    send_gesture, send_duration_ms, send_offset = _select_send_strategy(
        row.send_point[1], prior_attempts, send_gesture_override
    )
    send_point = (
        row.send_point[0] + send_offset[0],
        row.send_point[1] + send_offset[1],
    )
    boundary = store.cross_send_boundary(reply_action_id, pre_evidence)
    logger.event(
        "send_boundary_crossed",
        **pending,
        send_boundary_at=boundary,
        send_point=send_point,
        send_point_center=row.send_point,
        send_offset=send_offset,
        send_gesture=send_gesture,
        send_duration_ms=send_duration_ms,
        send_strategy_override=send_gesture_override,
        evidence=pre_evidence,
    )
    try:
        # 不可逆点击采用比普通导航更保守的4秒落定窗口：tap_settled 内部
        # 还会等待2秒，这里额外等待2秒，降低 dump 后触摸被吞的概率。
        time.sleep(2.0)
        tap_settled(
            device,
            logger,
            send_point,
            f"send_common_phrase:{phrase['key']}",
            # 发送按钮优先复用实机已验证成功的单命令同点按压；若有“无出站”
            # 铁证且用户授权重试，下一次再改用更长的 DOWN/UP。
            gesture=send_gesture,
            duration_ms=send_duration_ms,
        )
    except Exception as exc:
        store.finish_send(reply_action_id, "unknown", pre_evidence)
        raise StopRun(
            "发送触摸异常；动作已保守封口为 unknown"
        ) from exc

    post_evidence: list[dict[str, str]] = []
    for attempt in range(1, 4):
        time.sleep(1.0)
        try:
            _, post_screen, evidence = _capture(
                device, logger, f"after_send_{attempt:02d}"
            )
        except Exception:
            continue
        post_evidence.extend(evidence)
        if chat_display_name(post_screen) != expected_name:
            break
        if phrase["text"] in outbound_message_texts(post_screen):
            result = store.finish_send(reply_action_id, "sent", post_evidence)
            logger.event(
                "reply_sent_verified",
                conversation_id=conversation_id,
                inbound_message_id=inbound_message_id,
                phrase_key=phrase["key"],
                phrase_text=phrase["text"],
                evidence=post_evidence,
                **result,
            )
            return {
                **result,
                "conversation_id": conversation_id,
                "inbound_message_id": inbound_message_id,
                "display_name": expected_name,
                "phrase_key": phrase["key"],
                "phrase_text": phrase["text"],
            }
    store.finish_send(reply_action_id, "unknown", post_evidence or pre_evidence)
    raise StopRun("已触摸发送但未能可靠回读出站气泡；状态已封口为 unknown")


def authorize_retry(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    reply_action_id: int,
    authorization_note: str,
) -> dict[str, object]:
    """用当前完整聊天证据确认目标话术未出站，再保留旧尝试并解锁重试。"""
    conversation = store.conversation(conversation_id)
    action = store.reply_action(reply_action_id)
    if conversation is None or action is None:
        raise StopRun("会话或回复动作不存在")
    if int(action["conversation_id"]) != conversation_id:
        raise StopRun("回复动作不属于指定会话")
    phrase_text = str(action["phrase_text"] or "")
    if not phrase_text:
        raise StopRun("unknown 动作没有已绑定话术")
    focus, screen, evidence = _capture(device, logger, "retry_no_effect_proof")
    if page_kind(focus, screen) != "chat":
        raise StopRun("重试授权取证时当前不是聊天页")
    if common_phrase_rows(screen):
        logger.event(
            "closing_phrase_panel_for_retry_proof",
            conversation_id=conversation_id,
            evidence=evidence,
        )
        focus, screen, evidence = close_common_words_panel(
            device,
            logger,
            screen,
            label="retry_no_effect_full_chat",
        )
    snapshot = chat_snapshot(screen)
    expected_name = str(conversation["list_display_name"])
    if snapshot.display_name != expected_name:
        raise StopRun("重试授权取证时聊天身份不一致")
    if phrase_text in outbound_message_texts(screen):
        raise StopRun("完整聊天记录已出现目标话术，拒绝授权重试")
    result = store.authorize_retry_after_no_effect(
        reply_action_id, evidence, authorization_note
    )
    logger.event(
        "reply_retry_authorized",
        conversation_id=conversation_id,
        display_name=expected_name,
        phrase_text=phrase_text,
        evidence=evidence,
        **result,
    )
    return {**result, "conversation_id": conversation_id, "display_name": expected_name}


def authorize_retry_with_open_panel(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    reply_action_id: int,
    authorization_note: str,
) -> dict[str, object]:
    """人工已打开面板时，就地回读身份、面板和目标气泡后解锁重试。"""
    conversation = store.conversation(conversation_id)
    action = store.reply_action(reply_action_id)
    if conversation is None or action is None:
        raise StopRun("会话或回复动作不存在")
    if int(action["conversation_id"]) != conversation_id:
        raise StopRun("回复动作不属于指定会话")
    phrase_text = str(action["phrase_text"] or "")
    if not phrase_text:
        raise StopRun("unknown 动作没有已绑定话术")
    focus, screen, evidence = _capture(device, logger, "retry_open_panel_proof")
    if page_kind(focus, screen) != "chat":
        raise StopRun("面板重试取证时当前不是聊天页")
    rows = common_phrase_rows(screen)
    if not rows:
        raise StopRun("用户声明面板已打开，但控件树中没有常用语行")
    expected_name = str(conversation["list_display_name"])
    if chat_display_name(screen) != expected_name:
        raise StopRun("面板重试取证时聊天身份不一致")
    if phrase_text in outbound_message_texts(screen):
        raise StopRun("可见聊天记录已出现目标右侧气泡，拒绝授权重试")
    result = store.authorize_retry_after_no_effect(
        reply_action_id, evidence, authorization_note
    )
    logger.event(
        "reply_retry_authorized_with_open_panel",
        conversation_id=conversation_id,
        display_name=expected_name,
        phrase_text=phrase_text,
        visible_phrases=[row.phrase_text for row in rows],
        evidence=evidence,
        **result,
    )
    return {**result, "conversation_id": conversation_id, "display_name": expected_name}


def reconcile_sent(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    reply_action_id: int,
) -> dict[str, object]:
    """回读聊天页，并把有“送达”铁证的最近尝试追认为 sent。

    进程可能在发送触摸后、回读前被终止，数据库因而遗留 ``pending``。
    追认开始时先把这种跨边界动作保守封口为 ``unknown``；后续只在完整
    聊天页同时看到目标右侧气泡和同容器“送达”时，才提升为 ``sent``。
    """
    conversation = store.conversation(conversation_id)
    action = store.reply_action(reply_action_id)
    if conversation is None or action is None:
        raise StopRun("会话或回复动作不存在")
    if int(action["conversation_id"]) != conversation_id:
        raise StopRun("回复动作不属于指定会话")
    action_state = str(action["state"])
    if action_state not in ("pending", "unknown"):
        raise StopRun(f"只有 pending/unknown 动作可以发送追认：{action_state}")
    phrase_text = str(action["phrase_text"] or "")
    if not phrase_text:
        raise StopRun("unknown 动作没有已绑定话术")
    focus, screen, evidence = _capture(device, logger, "sent_reconciliation")
    if page_kind(focus, screen) != "chat":
        raise StopRun("发送追认取证时当前不是聊天页")
    if action_state == "pending":
        store.finish_send(reply_action_id, "unknown", evidence)
        logger.event(
            "orphaned_pending_send_sealed_unknown",
            conversation_id=conversation_id,
            reply_action_id=reply_action_id,
            evidence=evidence,
        )
    if common_phrase_rows(screen):
        logger.event(
            "closing_phrase_panel_for_sent_reconciliation",
            conversation_id=conversation_id,
            evidence=evidence,
        )
        focus, screen, evidence = close_common_words_panel(
            device,
            logger,
            screen,
            label="sent_reconciliation_full_chat",
        )
    snapshot = chat_snapshot(screen)
    expected_name = str(conversation["list_display_name"])
    if snapshot.display_name != expected_name:
        raise StopRun("发送追认取证时聊天身份不一致")
    if phrase_text not in outbound_message_texts(screen):
        raise StopRun("当前聊天记录中没有逐字一致的目标出站气泡")
    if phrase_text not in delivered_outbound_message_texts(screen):
        raise StopRun("目标出站气泡尚无同容器“送达”状态，拒绝追认")
    result = store.reconcile_unknown_as_sent(
        reply_action_id,
        evidence,
        "当前聊天身份、目标右侧出站气泡及同容器送达状态均已逐项回读确认",
    )
    logger.event(
        "reply_sent_reconciled",
        conversation_id=conversation_id,
        display_name=expected_name,
        phrase_text=phrase_text,
        delivery_status="送达",
        evidence=evidence,
        **result,
    )
    return {**result, "conversation_id": conversation_id, "display_name": expected_name}


def reconcile_manual_sent(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    conversation_id: int,
    reply_action_id: int,
) -> dict[str, object]:
    """只凭当前聊天页的目标右侧气泡和“送达”状态追认人工发送。"""
    conversation = store.conversation(conversation_id)
    action = store.reply_action(reply_action_id)
    if conversation is None or action is None:
        raise StopRun("会话或回复动作不存在")
    if int(action["conversation_id"]) != conversation_id:
        raise StopRun("回复动作不属于指定会话")
    if str(action["state"]) not in ("planned", "unknown"):
        raise StopRun(
            f"只有 planned/unknown 动作可以追认人工发送：{action['state']}"
        )
    phrase_text = str(action["phrase_text"] or "")
    if not phrase_text:
        raise StopRun("回复动作没有已绑定话术")
    focus, screen, evidence = _capture(device, logger, "manual_sent_reconciliation")
    if page_kind(focus, screen) != "chat":
        raise StopRun("人工发送追认时当前不是聊天页")
    expected_name = str(conversation["list_display_name"])
    if chat_display_name(screen) != expected_name:
        raise StopRun("人工发送追认时聊天身份不一致")
    if phrase_text not in outbound_message_texts(screen):
        raise StopRun("当前聊天记录中没有逐字一致的目标右侧气泡")
    if phrase_text not in delivered_outbound_message_texts(screen):
        raise StopRun("目标右侧气泡没有同容器“送达”状态，拒绝追认")
    result = store.record_manual_sent(
        reply_action_id,
        evidence,
        "用户在真机常用语面板手工点击右侧发送按钮；聊天身份、目标右侧气泡及同容器送达状态均已回读确认",
    )
    logger.event(
        "manual_reply_sent_reconciled",
        conversation_id=conversation_id,
        display_name=expected_name,
        phrase_text=phrase_text,
        evidence=evidence,
        **result,
    )
    return {**result, "conversation_id": conversation_id, "display_name": expected_name}


def scan(device: Device, logger: RunLogger) -> list[NewGreetingCard]:
    focus, screen, _ = _capture(device, logger, "new_greetings")
    kind = page_kind(focus, screen)
    if kind != "new_greetings":
        raise StopRun(f"当前不是新招呼列表：kind={kind!r}, focus={focus}")
    cards = all_new_greeting_cards(screen)
    logger.event(
        "greeting_cards_scanned",
        count=len(cards),
        unread_count=sum(card.unread_count > 0 for card in cards),
    )
    return cards


def scroll_new_greetings(device: Device, logger: RunLogger) -> dict[str, object]:
    """只在已确认的新招呼列表控件内部执行一次慢速向下遍历。"""
    focus, screen, before_evidence = _capture(
        device, logger, "new_greetings_before_scroll"
    )
    if page_kind(focus, screen) != "new_greetings":
        raise StopRun("当前不是新招呼列表，拒绝滑动")
    container = next(
        (node for node in screen.nodes() if node.short_id == "recyclerView"), None
    )
    if container is None or container.element.attrib.get("scrollable") != "true":
        raise StopRun("新招呼列表缺少可滚动容器")
    bounds = parse_bounds(container.element.attrib.get("bounds", ""))
    before_cards = all_new_greeting_cards(screen)
    before_signature = tuple(
        (card.conversation_fingerprint, card.point) for card in before_cards
    )
    width = bounds.right - bounds.left
    height = bounds.bottom - bounds.top
    x = bounds.left + max(80, int(width * 0.28))
    start_y = bounds.top + int(height * 0.78)
    end_y = bounds.top + int(height * 0.34)
    device.swipe(x, start_y, x, end_y, duration_ms=900)
    time.sleep(2.0)
    after_focus, after_screen, after_evidence = _capture(
        device, logger, "new_greetings_after_scroll"
    )
    if page_kind(after_focus, after_screen) != "new_greetings":
        raise StopRun("新招呼列表滑动后页面发生漂移")
    after_cards = all_new_greeting_cards(after_screen)
    after_signature = tuple(
        (card.conversation_fingerprint, card.point) for card in after_cards
    )
    moved = after_signature != before_signature
    cards = all_new_greeting_cards(after_screen)
    logger.event(
        "new_greetings_scrolled",
        start=(x, start_y),
        end=(x, end_y),
        duration_ms=900,
        moved=moved,
        card_count=len(cards),
        unread_count=sum(card.unread_count > 0 for card in cards),
        display_names=[card.display_name for card in cards],
        before_evidence=before_evidence,
        after_evidence=after_evidence,
    )
    return {
        "moved": moved,
        "cards": [_card_payload(card) for card in cards],
        "evidence": after_evidence,
    }


def return_to_new_greetings(
    device: Device,
    logger: RunLogger,
    expected_absent_display_name: str | None = None,
) -> dict[str, object]:
    """从已知业务页安全返回新招呼列表，并留存列表回读证据。"""
    go_back_to(device, "new_greetings", max_backs=3)
    focus, screen, evidence = _capture(device, logger, "new_greetings_returned")
    if page_kind(focus, screen) != "new_greetings":
        raise StopRun("返回后未处于新招呼列表")
    cards = all_new_greeting_cards(screen)
    expected_absent = (expected_absent_display_name or "").strip()
    still_present = [
        card.display_name for card in cards if card.display_name == expected_absent
    ]
    if expected_absent and still_present:
        raise StopRun(
            "已发送会话返回后仍出现在新招呼列表："
            f"{expected_absent!r}；拒绝宣称已迁移至仅沟通"
        )
    logger.event(
        "returned_to_new_greetings",
        card_count=len(cards),
        unread_count=sum(card.unread_count > 0 for card in cards),
        display_names=[card.display_name for card in cards],
        expected_absent_display_name=expected_absent or None,
        migrated_to_only_communication=(True if expected_absent else None),
        evidence=evidence,
    )
    return {
        "page_kind": "new_greetings",
        "cards": [_card_payload(card) for card in cards],
        "expected_absent_display_name": expected_absent or None,
        "migrated_to_only_communication": (True if expected_absent else None),
        "evidence": evidence,
    }


def _open_selected_card(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    selected: NewGreetingCard,
    evidence: list[dict[str, str]],
    *,
    allow_without_badge: bool,
) -> dict[str, object]:
    staged = store.stage_greeting_card(selected, evidence)
    logger.event(
        "greeting_card_staged",
        explicit_without_badge=allow_without_badge and selected.unread_count == 0,
        **{**_card_payload(selected), **staged},
    )

    # 导航点击允许受控重试。每次都重新读屏并按指纹定位，只有仍处于同一
    # 新招呼列表时才会换一种触摸注入方式；不会复用旧坐标盲点。
    entered = None
    # 2026-10-22 连续 8 名候选人均以 120ms tap 进入聊天；200ms press 仅作回退。
    strategies = (("tap", 120), ("press", 200))
    relocated = selected
    for attempt, (gesture, duration_ms) in enumerate(strategies, start=1):
        focus, refreshed, before_evidence = _capture(
            device, logger, f"before_open_relocated_{attempt:02d}"
        )
        if page_kind(focus, refreshed) != "new_greetings":
            raise StopRun("点击前页面发生漂移")
        refreshed_cards = (
            all_new_greeting_cards(refreshed)
            if allow_without_badge
            else new_greeting_cards(refreshed)
        )
        relocated = next(
            (
                card
                for card in refreshed_cards
                if card.conversation_fingerprint == selected.conversation_fingerprint
            ),
            None,
        )
        if relocated is None:
            raise StopRun("点击前未能重新定位同一目标会话")
        tap_settled(
            device,
            logger,
            relocated.point,
            f"open_greeting:{relocated.display_name}:attempt_{attempt}",
            gesture=gesture,
            duration_ms=duration_ms,
        )
        entered = wait_for_kind(device, "chat", timeout=8.0)
        if entered is not None:
            break
        after_focus, after_screen, after_evidence = _capture(
            device, logger, f"after_open_no_effect_{attempt:02d}"
        )
        after_kind = page_kind(after_focus, after_screen)
        logger.event(
            "open_greeting_no_effect",
            attempt=attempt,
            gesture=gesture,
            duration_ms=duration_ms,
            page_kind=after_kind,
            focus=after_focus,
            before_evidence=before_evidence,
            after_evidence=after_evidence,
        )
        if after_kind != "new_greetings":
            raise StopRun(f"导航点击后进入非目标页面 {after_kind!r}，停止重试")
    if entered is None:
        raise StopRun("两种受控触摸均未进入目标聊天页")
    chat_focus, chat_screen, chat_evidence = _capture(device, logger, "chat_opened")
    try:
        opened_display_name = chat_display_name(chat_screen)
    except ValueError as exc:
        raise StopRun(str(exc)) from exc
    if opened_display_name != relocated.display_name:
        raise StopRun(
            "打开聊天后身份不一致："
            f"expected={relocated.display_name!r}, actual={opened_display_name!r}"
        )
    logger.event(
        "chat_opened",
        candidate_id=staged["candidate_id"],
        conversation_id=staged["conversation_id"],
        display_name=relocated.display_name,
        focus=chat_focus,
        evidence=chat_evidence,
    )
    return {
        **staged,
        "card": _card_payload(relocated),
        "chat_kind": page_kind(chat_focus, chat_screen),
        "chat_evidence": chat_evidence,
    }


def open_first(device: Device, logger: RunLogger, store: ReplyStore) -> dict[str, object]:
    focus, screen, evidence = _capture(device, logger, "before_open")
    if page_kind(focus, screen) != "new_greetings":
        raise StopRun("当前不是新招呼列表，拒绝打开会话")
    cards = all_new_greeting_cards(screen)
    selected = next(
        (card for card in cards if not store.action_states_for_card(card)), None
    )
    if selected is None:
        raise StopRun("当前屏没有本地数据库尚未处理的新招呼会话")
    return _open_selected_card(
        device, logger, store, selected, evidence, allow_without_badge=True
    )


def open_named(
    device: Device,
    logger: RunLogger,
    store: ReplyStore,
    display_name: str,
) -> dict[str, object]:
    """打开用户明确点名的当前屏会话；已读与未读使用同一逻辑。"""
    focus, screen, evidence = _capture(device, logger, "before_open_named")
    if page_kind(focus, screen) != "new_greetings":
        raise StopRun("当前不是新招呼列表，拒绝打开指定会话")
    matches = [
        card
        for card in all_new_greeting_cards(screen)
        if card.display_name == display_name.strip()
    ]
    if len(matches) != 1:
        raise StopRun(
            f"指定姓名必须在当前屏唯一精确命中：{display_name!r}，实际 {len(matches)} 条"
        )
    logger.event(
        "explicit_greeting_target_authorized",
        display_name=display_name.strip(),
        unread_count=matches[0].unread_count,
        unread_signal=matches[0].unread_signal,
    )
    return _open_selected_card(
        device, logger, store, matches[0], evidence, allow_without_badge=True
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        choices=("off", "minimal", "debug"),
        default="minimal",
        help="证据等级；正式运行默认 minimal",
    )
    parser.add_argument(
        "command",
        choices=(
            "collect-one", "run-one", "run-page",
            "scan", "scroll-list", "return-list", "open-one", "open-named",
            "capture-chat", "capture-resume", "inspect-phrases",
            "plan-action", "locate-phrase", "send-planned",
            "authorize-retry", "reconcile-sent", "authorize-retry-panel",
            "reconcile-manual-sent",
        ),
    )
    parser.add_argument("--conversation-id", type=int)
    parser.add_argument("--inbound-message-id", type=int)
    parser.add_argument("--action", choices=("send", "hold"))
    parser.add_argument("--comment")
    parser.add_argument("--reply-action-id", type=int)
    parser.add_argument("--phrase-key")
    parser.add_argument("--decisions")
    parser.add_argument("--max-cards", type=int, default=8)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run-one/run-page 默认 dry-run；带此开关才真实发送",
    )
    parser.add_argument(
        "--send-gesture",
        choices=("tap", "press", "system_tap"),
        help="仅用于获授权的输入机制A/B；必须命中预定义发送策略",
    )
    parser.add_argument("--authorization-note")
    parser.add_argument("--display-name")
    parser.add_argument("--expected-absent-display-name")
    parser.add_argument("--max-scrolls", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = RunLogger(
        args.command,
        flow=FLOW_REPLY_NEW_GREETINGS,
        evidence_level=args.evidence,
    )
    device: Device | None = None
    store: ReplyStore | None = None
    try:
        device = Device()
        store = ReplyStore(DATABASE_PATH, SCHEMA_PATH, REPLY_SCHEMA_PATH)
        device.verify_identity()
        if args.command in ("collect-one", "run-one", "run-page"):
            from .reply_flow import (
                collect_candidate,
                load_decisions,
                run_candidate,
                run_page,
            )

            if args.command == "collect-one":
                if not args.display_name:
                    raise StopRun("collect-one 必须提供 --display-name")
                result: object = collect_candidate(
                    device,
                    logger,
                    store,
                    args.display_name,
                    max_scrolls=args.max_scrolls,
                )
            elif args.command == "run-one":
                if not args.display_name or not args.action or not args.comment:
                    raise StopRun(
                        "run-one 必须提供 --display-name、--action 和 --comment"
                    )
                if args.action == "send" and not args.phrase_key:
                    raise StopRun("run-one 的 send 动作必须提供 --phrase-key")
                if args.action == "hold" and args.phrase_key:
                    raise StopRun("run-one 的 hold 动作不得提供 --phrase-key")
                result = run_candidate(
                    device,
                    logger,
                    store,
                    args.display_name,
                    args.action,
                    args.phrase_key,
                    args.comment,
                    execute=args.execute,
                    max_scrolls=args.max_scrolls,
                )
            else:
                if not args.decisions:
                    raise StopRun("run-page 必须提供 --decisions")
                result = run_page(
                    device,
                    logger,
                    store,
                    load_decisions(args.decisions),
                    max_cards=args.max_cards,
                    execute=args.execute,
                    default_max_scrolls=args.max_scrolls,
                )
        elif args.command == "scan":
            result = [_card_payload(card) for card in scan(device, logger)]
        elif args.command == "scroll-list":
            result = scroll_new_greetings(device, logger)
        elif args.command == "return-list":
            result = return_to_new_greetings(
                device,
                logger,
                args.expected_absent_display_name,
            )
        elif args.command == "open-one":
            result = open_first(device, logger, store)
        elif args.command == "open-named":
            if not args.display_name:
                raise StopRun("open-named 必须提供 --display-name")
            result = open_named(device, logger, store, args.display_name)
        elif args.command == "capture-chat":
            if args.conversation_id is None:
                raise StopRun("capture-chat 必须提供 --conversation-id")
            snapshot, message = capture_chat(
                device, logger, store, args.conversation_id
            )
            result = {"chat": _chat_payload(snapshot), "message": message}
        elif args.command == "capture-resume":
            if args.conversation_id is None:
                raise StopRun("capture-resume 必须提供 --conversation-id")
            result = capture_candidate_resume(
                device,
                logger,
                store,
                args.conversation_id,
                args.max_scrolls,
            )
        elif args.command == "inspect-phrases":
            if args.conversation_id is None:
                raise StopRun("inspect-phrases 必须提供 --conversation-id")
            result = inspect_phrases(device, logger, store, args.conversation_id)
        elif args.command == "plan-action":
            if (
                args.conversation_id is None
                or args.inbound_message_id is None
                or args.action is None
                or args.comment is None
            ):
                raise StopRun(
                    "plan-action 必须提供 --conversation-id、--inbound-message-id、"
                    "--action 和 --comment"
                )
            if args.action == "send" and not args.phrase_key:
                raise StopRun("plan-action 的 send 动作必须提供 --phrase-key")
            if args.action == "hold" and args.phrase_key:
                raise StopRun("plan-action 的 hold 动作不得提供 --phrase-key")
            reply_plan = plan_new_greeting(
                args.action,
                args.comment,
                phrase_key=args.phrase_key,
            )
            result = plan_action(
                logger,
                store,
                args.conversation_id,
                args.inbound_message_id,
                reply_plan.decision,
                reply_plan.reason,
            )
            result.update(
                {
                    "action": reply_plan.action,
                    "phrase_key": reply_plan.phrase_key,
                    "intent": reply_plan.intent,
                }
            )
        elif args.command == "locate-phrase":
            if args.conversation_id is None or not args.phrase_key:
                raise StopRun("locate-phrase 必须提供 --conversation-id 和 --phrase-key")
            conversation = store.conversation(args.conversation_id)
            if conversation is None:
                raise StopRun(f"会话不存在：{args.conversation_id}")
            phrase = load_phrase(args.phrase_key)
            row, evidence = locate_phrase_in_panel(
                device,
                logger,
                str(conversation["list_display_name"]),
                phrase["text"],
            )
            result = {
                "phrase": phrase,
                "send_point": row.send_point,
                "row_bounds": row.row_bounds.__dict__,
                "evidence": evidence,
            }
        elif args.command == "authorize-retry":
            if (
                args.conversation_id is None
                or args.reply_action_id is None
                or not args.authorization_note
            ):
                raise StopRun(
                    "authorize-retry 必须提供 --conversation-id、"
                    "--reply-action-id 和 --authorization-note"
                )
            result = authorize_retry(
                device,
                logger,
                store,
                args.conversation_id,
                args.reply_action_id,
                args.authorization_note,
            )
        elif args.command == "authorize-retry-panel":
            if (
                args.conversation_id is None
                or args.reply_action_id is None
                or not args.authorization_note
            ):
                raise StopRun(
                    "authorize-retry-panel 必须提供 --conversation-id、"
                    "--reply-action-id 和 --authorization-note"
                )
            result = authorize_retry_with_open_panel(
                device,
                logger,
                store,
                args.conversation_id,
                args.reply_action_id,
                args.authorization_note,
            )
        elif args.command == "reconcile-sent":
            if args.conversation_id is None or args.reply_action_id is None:
                raise StopRun(
                    "reconcile-sent 必须提供 --conversation-id 和 --reply-action-id"
                )
            result = reconcile_sent(
                device,
                logger,
                store,
                args.conversation_id,
                args.reply_action_id,
            )
        elif args.command == "reconcile-manual-sent":
            if args.conversation_id is None or args.reply_action_id is None:
                raise StopRun(
                    "reconcile-manual-sent 必须提供 --conversation-id 和 "
                    "--reply-action-id"
                )
            result = reconcile_manual_sent(
                device,
                logger,
                store,
                args.conversation_id,
                args.reply_action_id,
            )
        elif args.command == "send-planned":
            if (
                args.conversation_id is None
                or args.inbound_message_id is None
                or args.reply_action_id is None
                or not args.phrase_key
            ):
                raise StopRun(
                    "send-planned 必须提供 --conversation-id、--inbound-message-id、"
                    "--reply-action-id 和 --phrase-key"
                )
            result = send_planned_reply(
                device,
                logger,
                store,
                args.conversation_id,
                args.inbound_message_id,
                args.reply_action_id,
                args.phrase_key,
                args.send_gesture,
            )
        else:
            raise StopRun(f"未知命令：{args.command!r}")
        # Windows 控制台可能仍是 GBK；转义非 ASCII 只影响展示，不改变落库文本。
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        logger.event("stop", error=str(exc), error_type=type(exc).__name__)
        if device is not None and logger.should_capture(critical=True):
            stamp = time.strftime("%H%M%S")
            try:
                xml_path = logger.save_evidence(
                    f"stop_{stamp}.xml", device.dump_ui()
                )
                png_path = logger.save_evidence(
                    f"stop_{stamp}.png", device.screenshot()
                )
                logger.event(
                    "stop_evidence",
                    evidence=[evidence_entry(xml_path), evidence_entry(png_path)],
                )
            except Exception as capture_error:
                logger.event(
                    "stop_evidence_failed",
                    reason=str(capture_error),
                    error_type=type(capture_error).__name__,
                )
        raise
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
