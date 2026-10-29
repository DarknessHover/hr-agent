"""2号状态机页面适配器：消息页、新招呼页与会话卡片。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .device import Bounds, parse_bounds
from .resume import Screen
from .screen_ops import register_page_kind


@dataclass(frozen=True)
class NewGreetingCard:
    display_name: str
    position_text: str
    meta_text: str
    preview_text: str
    unread_count: int
    unread_signal: str
    point: tuple[int, int]
    card_bounds: Bounds

    @property
    def conversation_fingerprint(self) -> str:
        payload = {
            "platform": "boss_zhipin",
            "display_name": self.display_name,
            "position_text": self.position_text,
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChatSnapshot:
    display_name: str
    subtitle: str
    latest_inbound_text: str
    visible_time_text: str
    avatar_point: tuple[int, int]


@dataclass(frozen=True)
class DirectionalTextMessage:
    direction: str
    text: str
    top: int


@dataclass(frozen=True)
class CommonPhraseRow:
    phrase_text: str
    send_point: tuple[int, int]
    row_bounds: Bounds


CHAT_DATE_RE = re.compile(r"^(\d{1,2}月\d{1,2}日)")


def _texts(screen: Screen) -> set[str]:
    return {node.text.strip() for node in screen.nodes() if node.text.strip()}


def detect_message_hub(_focus: str, screen: Screen) -> bool:
    ids = {node.short_id for node in screen.nodes()}
    texts = _texts(screen)
    return "contact_vp_root" in ids and "聊天" in texts and "新招呼" in texts


def detect_new_greetings(focus: str, screen: Screen) -> bool:
    """BOSS 14.170 的会话列表页。

    该版本移除了 tv_recommend_sort / tv_time_sort，全部、新招呼、仅沟通
    共用同一个会话列表容器；只要消息中心与可滚动会话列表同时存在，就按
    会话列表页处理，供 open/return 复用。
    """
    ids = {node.short_id for node in screen.nodes()}
    return (
        detect_message_hub(focus, screen)
        and "recyclerView" in ids
        and "tv_name" in ids
    )


def _descendant_by_id(element, short_id: str):
    return next(
        (
            child
            for child in element.iter("node")
            if child.attrib.get("resource-id", "").endswith("/" + short_id)
        ),
        None,
    )


def _text(element) -> str:
    return element.attrib.get("text", "").strip() if element is not None else ""


def _new_greeting_cards(
    screen: Screen, *, include_without_badge: bool
) -> list[NewGreetingCard]:
    cards: list[NewGreetingCard] = []
    for name_node in screen.nodes(under_id="recyclerView"):
        if name_node.short_id != "tv_name" or not name_node.text.strip():
            continue
        card = screen.clickable_ancestor(name_node.element)
        if card is None:
            continue
        badge = _descendant_by_id(card, "badge_view")
        unread_text = ""
        if badge is not None:
            unread_text = next(
                (
                    child.attrib.get("text", "").strip()
                    for child in badge.iter("node")
                    if child.attrib.get("text", "").strip().isdigit()
                ),
                "",
            )
        if not unread_text and not include_without_badge:
            continue
        try:
            card_bounds = parse_bounds(card.attrib.get("bounds", ""))
        except Exception:
            continue
        if name_node.bounds is None:
            continue
        cards.append(
            NewGreetingCard(
                display_name=name_node.text.strip(),
                position_text=_text(_descendant_by_id(card, "tv_position")),
                meta_text=_text(_descendant_by_id(card, "tv_time")),
                preview_text=_text(_descendant_by_id(card, "tv_msg")),
                unread_count=int(unread_text or 0),
                unread_signal=("badge_view:" + unread_text if unread_text else "no_badge"),
                # 与1号状态机一样由页面适配器提供落点、公共层只负责
                # 注入触摸。姓名中心是两部列表状态机共同的稳定语义锚点。
                point=name_node.bounds.center,
                card_bounds=card_bounds,
            )
        )
    return cards


def new_greeting_cards(screen: Screen) -> list[NewGreetingCard]:
    """返回当前屏中可证明未读的卡片，未读角标必须与姓名同属一个卡片。"""
    return _new_greeting_cards(screen, include_without_badge=False)


def all_new_greeting_cards(screen: Screen) -> list[NewGreetingCard]:
    """返回当前屏全部新招呼卡片；未读信号只作为观测字段保留。"""
    return _new_greeting_cards(screen, include_without_badge=True)


def chat_snapshot(screen: Screen) -> ChatSnapshot:
    """读取聊天身份与最新一条可证明为入站的文本消息。

    入站方向不按气泡颜色猜测：消息正文和左侧 ``iv_avatar`` 必须同属
    一个 clickable 消息容器。右侧头像或缺少头像的正文均不作为待回复消息。
    """
    title = chat_display_name(screen)
    subtitle = next(
        (
            node.text.strip()
            for node in screen.nodes(under_id="cl_title")
            if node.short_id == "tv_sub_title" and node.text.strip()
        ),
        "",
    )
    if not title:
        raise ValueError("聊天页缺少会话标题")

    inbound: list[tuple[int, str, tuple[int, int]]] = []
    message_tops: list[int] = []
    for node in screen.nodes(under_id="mRecycleView"):
        if node.short_id != "tv_content_text" or not node.text.strip():
            continue
        item = screen.clickable_ancestor(node.element)
        if item is None:
            continue
        avatar = _descendant_by_id(item, "iv_avatar")
        if avatar is None:
            continue
        try:
            item_bounds = parse_bounds(item.attrib.get("bounds", ""))
            avatar_bounds = parse_bounds(avatar.attrib.get("bounds", ""))
        except Exception:
            continue
        message_tops.append(item_bounds.top)
        # BOSS 入站头像在消息容器左半侧；出站头像位于右侧。
        if avatar_bounds.center[0] >= item_bounds.center[0]:
            continue
        inbound.append((item_bounds.top, node.text.strip(), avatar_bounds.center))
    if not inbound:
        raise ValueError("聊天页没有可证明方向的入站文本消息")
    inbound_top, body, avatar_point = max(inbound, key=lambda item: item[0])

    # 时间节点属于它后面的第一条消息，不能简单取屏幕最靠下的时间。
    # 否则发送新消息后，出站消息上方的新时间会被错误绑定给旧入站消息。
    previous_message_top = max(
        (top for top in message_tops if top < inbound_top), default=-1
    )

    time_nodes = [
        (node.bounds.top, node.text.strip())
        for node in screen.nodes(under_id="mRecycleView")
        if (
            node.short_id == "tv_text"
            and node.bounds is not None
            and node.text.strip()
            and previous_message_top < node.bounds.top < inbound_top
        )
    ]
    visible_time = max(time_nodes, default=(0, ""), key=lambda item: item[0])[1]
    date_nodes: list[tuple[int, str]] = []
    for node in screen.nodes(under_id="mRecycleView"):
        if node.short_id != "mBottomPositionView" or node.bounds is None:
            continue
        if not previous_message_top < node.bounds.top < inbound_top:
            continue
        match = CHAT_DATE_RE.match(node.text.strip())
        if match:
            date_nodes.append((node.bounds.top, match.group(1)))
    date_text = max(date_nodes, default=(0, ""), key=lambda item: item[0])[1]
    visible_time_text = " ".join(part for part in (date_text, visible_time) if part)
    return ChatSnapshot(
        display_name=title,
        subtitle=subtitle,
        latest_inbound_text=body,
        visible_time_text=visible_time_text,
        avatar_point=avatar_point,
    )


def chat_display_name(screen: Screen) -> str:
    """读取聊天页顶部姓名；底部面板遮住消息时仍可用于身份校验。"""
    return next(
        (
            node.text.strip()
            for node in screen.nodes(under_id="cl_title")
            if node.short_id == "tv_title" and node.text.strip()
        ),
        "",
    )


def directional_text_messages(screen: Screen) -> list[DirectionalTextMessage]:
    """返回当前聊天屏内方向可证明的文本消息，按屏幕顺序排列。"""
    messages: list[DirectionalTextMessage] = []
    for node in screen.nodes(under_id="mRecycleView"):
        if node.short_id != "tv_content_text" or not node.text.strip():
            continue
        item = screen.clickable_ancestor(node.element)
        if item is None:
            continue
        avatar = _descendant_by_id(item, "iv_avatar")
        if avatar is None:
            continue
        try:
            item_bounds = parse_bounds(item.attrib.get("bounds", ""))
            avatar_bounds = parse_bounds(avatar.attrib.get("bounds", ""))
        except Exception:
            continue
        if avatar_bounds.center[0] < item_bounds.center[0]:
            direction = "inbound"
        elif avatar_bounds.center[0] > item_bounds.center[0]:
            direction = "outbound"
        else:
            continue
        messages.append(
            DirectionalTextMessage(direction, node.text.strip(), item_bounds.top)
        )
    return sorted(messages, key=lambda message: message.top)


def latest_directional_text_message(
    screen: Screen,
) -> DirectionalTextMessage | None:
    """返回当前屏最靠下且方向可证明的文本消息。"""
    messages = directional_text_messages(screen)
    return messages[-1] if messages else None


def outbound_message_texts(screen: Screen) -> list[str]:
    """返回当前聊天记录中可证明为右侧出站的文本消息，按屏幕顺序排列。"""
    return [
        message.text
        for message in directional_text_messages(screen)
        if message.direction == "outbound"
    ]


def delivered_outbound_message_texts(screen: Screen) -> list[str]:
    """返回同一右侧消息容器内带有“送达”状态的出站文本。"""
    delivered: list[tuple[int, str]] = []
    for node in screen.nodes(under_id="mRecycleView"):
        if node.short_id != "tv_content_text" or not node.text.strip():
            continue
        item = screen.clickable_ancestor(node.element)
        if item is None:
            continue
        avatar = _descendant_by_id(item, "iv_avatar")
        status = _descendant_by_id(item, "mMsgTvStatus")
        if avatar is None or status is None or _text(status) != "送达":
            continue
        try:
            item_bounds = parse_bounds(item.attrib.get("bounds", ""))
            avatar_bounds = parse_bounds(avatar.attrib.get("bounds", ""))
        except Exception:
            continue
        if avatar_bounds.center[0] <= item_bounds.center[0]:
            continue
        delivered.append((item_bounds.top, node.text.strip()))
    return [text for _, text in sorted(delivered)]


def common_phrase_rows(screen: Screen) -> list[CommonPhraseRow]:
    """把常用语正文与同一行的“发送”按钮绑定，避免按坐标猜测。"""
    rows: list[CommonPhraseRow] = []
    for node in screen.nodes(under_id="recyclerView"):
        if node.short_id != "mContentText" or not node.text.strip():
            continue
        row = node.element.getparent() if hasattr(node.element, "getparent") else None
        # ElementTree 没有 getparent；常用语正文的直接父节点可从树中反查。
        if row is None:
            row = next(
                (
                    parent
                    for parent in screen.root.iter("node")
                    if node.element in list(parent)
                ),
                None,
            )
        if row is None:
            continue
        send = _descendant_by_id(row, "btn_send")
        if send is None:
            continue
        try:
            row_bounds = parse_bounds(row.attrib.get("bounds", ""))
            send_bounds = parse_bounds(send.attrib.get("bounds", ""))
        except Exception:
            continue
        rows.append(
            CommonPhraseRow(
                phrase_text=node.text.strip(),
                send_point=send_bounds.center,
                row_bounds=row_bounds,
            )
        )
    return rows


def common_words_point(screen: Screen) -> tuple[int, int]:
    """返回聊天输入栏左侧“常用语”切换按钮的可点击祖先中心。

    面板关闭时正文控件为 ``mCommonText``；展开后同一按钮只暴露
    ``mCommonIcon``。两种形态都必须支持，否则关闭面板会误用系统返回键。
    """
    node = next(
        (
            node
            for node in screen.nodes()
            if node.short_id in ("mCommonText", "mCommonIcon")
        ),
        None,
    )
    if node is None:
        raise ValueError("聊天页缺少常用语切换按钮 mCommonText/mCommonIcon")
    point = screen.clickable_center(node.element)
    if point is None:
        raise ValueError("常用语按钮没有可点击祖先")
    return point


# 新招呼是消息页的特化形态，必须先注册以获得更高判定优先级。
register_page_kind("new_greetings", detect_new_greetings)
register_page_kind("message_hub", detect_message_hub)
