"""Agent 审核节点的评语契约。

评语长度只提供质量建议，不参与落库授权。去除首尾空白后非空是唯一硬约束；
外部 Agent 即使输出超过建议上限的完整评语，也应保留原文而不是强制重写。
"""

from __future__ import annotations


# 为建议上限300字留出足够输出预算；外部 Agent 的既有结果不受此预算约束。
REVIEW_MAX_OUTPUT_TOKENS = 512
# 兼容现有调用方的常量名；二者现在表示建议范围，不是拒绝边界。
EVALUATION_COMMENT_MIN_CHARS = 100
EVALUATION_COMMENT_MAX_CHARS = 300


class ReviewPolicyError(ValueError):
    """评语不满足最小可落库契约。"""


def validate_evaluation_comment(comment: str) -> str:
    """去除首尾空白；只拒绝空评语，不按长度拒绝或要求重写。"""
    normalized = comment.strip()
    if not normalized:
        raise ReviewPolicyError("Agent 评语不能为空")
    return normalized


def evaluation_comment_advisory(comment: str) -> str | None:
    """返回非阻断的长度建议；建议范围内返回 ``None``。"""
    normalized = validate_evaluation_comment(comment)
    length = len(normalized)
    if length < EVALUATION_COMMENT_MIN_CHARS:
        return (
            f"评语当前为{length}字，建议不少于"
            f"{EVALUATION_COMMENT_MIN_CHARS}字；本次仍允许入库"
        )
    if length > EVALUATION_COMMENT_MAX_CHARS:
        return (
            f"评语当前为{length}字，建议不超过"
            f"{EVALUATION_COMMENT_MAX_CHARS}字；本次仍允许入库"
        )
    return None
