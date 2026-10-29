"""公共回复计划契约与允许话术目录。

外部 Agent 负责理解语境并提交 ``send/hold + phrase_key + reason``；本模块
只验证动作与允许话术，不把业务语境写成不断扩张的固定 outcome 枚举。2号
和后续3号可以复用同一契约，各自提供不同的语境与策略提示。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ReplyPlanAction = Literal["send", "hold"]


@dataclass(frozen=True)
class ReplyPlan:
    action: ReplyPlanAction
    phrase_key: str | None
    intent: str | None
    reason: str
    decision: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("回复计划理由不能为空")
        if self.action == "send" and not self.phrase_key:
            raise ValueError("发送计划必须绑定允许话术")
        if self.action == "send" and not self.intent:
            raise ValueError("发送计划必须记录话术意图")
        if self.action == "hold" and (
            self.phrase_key is not None or self.intent is not None
        ):
            raise ValueError("人工挂起计划不得绑定话术或意图")
        if self.action == "hold" and self.decision != "needs_review":
            raise ValueError("hold 动作必须使用 needs_review 兼容分类")
        if self.action == "send":
            expected = "reject" if self.intent == "reject" else "request_resume"
            if self.decision != expected:
                raise ValueError("发送计划的兼容分类与话术意图不一致")


ROOT = Path(__file__).resolve().parents[1]
PHRASES_PATH = ROOT / "config" / "reply_phrases.yaml"
REPLY_ACTIONS = ("send", "hold")
NEW_GREETING_DEFAULT_PHRASE_KEY = "p14_address_acceptance"


def load_allowed_phrases(path: Path | None = None) -> dict[str, dict[str, str]]:
    """读取并严格校验允许话术目录，返回以稳定键索引的只读副本语义。"""
    path = path or PHRASES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    phrases = payload.get("phrases")
    if not isinstance(phrases, list):
        raise ValueError("reply_phrases.yaml 缺少 phrases 列表")
    if payload.get("observed_count") != len(phrases):
        raise ValueError("reply_phrases.yaml 的 observed_count 与列表长度不一致")
    catalog: dict[str, dict[str, str]] = {}
    for raw in phrases:
        if not isinstance(raw, dict):
            raise ValueError("reply_phrases.yaml 存在非对象话术")
        key = str(raw.get("key") or "").strip()
        intent = str(raw.get("intent") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not key or not intent or not text:
            raise ValueError("reply_phrases.yaml 的话术缺少 key/intent/text")
        if key in catalog:
            raise ValueError(f"reply_phrases.yaml 存在重复话术键：{key!r}")
        catalog[key] = {"key": key, "intent": intent, "text": text}
    return catalog


def allowed_phrase(phrase_key: str, path: Path | None = None) -> dict[str, str]:
    """按稳定键读取一条允许话术；未知键拒绝进入回复计划。"""
    normalized = phrase_key.strip()
    phrase = load_allowed_phrases(path).get(normalized)
    if phrase is None:
        raise ValueError(f"话术键不在允许清单中：{phrase_key!r}")
    return phrase


def plan_new_greeting(
    action: str,
    reason: str,
    *,
    phrase_key: str | None = None,
) -> ReplyPlan:
    """建立受约束的新招呼回复计划，不解释或硬编码具体聊天语境。

    ``decision`` 仍写入当前数据库的兼容枚举：拒绝意图写 ``reject``，其它
    发送写 ``request_resume``，人工挂起写 ``needs_review``。精确语义由
    ``phrase_key`` 和允许话术目录表达。
    """
    normalized_action = action.strip()
    if normalized_action not in REPLY_ACTIONS:
        raise ValueError(f"未知回复动作：{action!r}")
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("回复计划理由不能为空")
    if normalized_action == "hold":
        if phrase_key is not None:
            raise ValueError("hold 动作不得绑定话术")
        return ReplyPlan(
            action="hold",
            phrase_key=None,
            intent=None,
            reason=normalized_reason,
            decision="needs_review",
        )
    normalized_key = str(phrase_key or "").strip()
    if not normalized_key:
        raise ValueError("send 动作必须绑定允许话术")
    phrase = allowed_phrase(normalized_key)
    normalized_intent = phrase["intent"]
    decision = "reject" if normalized_intent == "reject" else "request_resume"
    return ReplyPlan(
        action="send",
        phrase_key=normalized_key,
        intent=normalized_intent,
        reason=normalized_reason,
        decision=decision,
    )
