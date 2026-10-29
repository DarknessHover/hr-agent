"""2号状态机专属仓储：点击前先持久化新招呼会话事实。"""

from __future__ import annotations

import json
import sqlite3
import hashlib
import re
from pathlib import Path

from .reply_ui import NewGreetingCard
from .reply_plan import allowed_phrase
from .review import validate_evaluation_comment
from .store import (
    CAPTURE_VERSION,
    VISIBLE_COLUMNS,
    canonical_json,
    now_iso,
)

SENDABLE_REPLY_DECISIONS = frozenset({"request_resume", "reject"})


class ReplyStore:
    def __init__(self, database_path: Path, schema_path: Path, reply_schema_path: Path):
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(schema_path.read_text(encoding="utf-8"))
        self.connection.executescript(reply_schema_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.connection.close()

    def conversation(self, conversation_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM reply_conversation WHERE id = ?", (conversation_id,)
        ).fetchone()

    def reply_action(self, reply_action_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM reply_action WHERE id = ?", (reply_action_id,)
        ).fetchone()

    def send_attempt_count(self, reply_action_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM reply_send_attempt WHERE reply_action_id = ?",
                (reply_action_id,),
            ).fetchone()[0]
        )

    def stage_greeting_card(
        self, card: NewGreetingCard, evidence: list[dict[str, str]]
    ) -> dict[str, int | bool | str]:
        """在打开聊天前幂等保存卡片；未读角标仅作为观测事实。"""
        now = now_iso()
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        fingerprint = card.conversation_fingerprint
        with self.connection:
            reconciled_empty_duplicate_ids: list[int] = []
            existing = self.connection.execute(
                "SELECT id, candidate_id FROM reply_conversation "
                "WHERE conversation_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is None:
                # 旧版指纹曾包含 meta_text；平台会在首次打开后把学历补入
                # meta_text，使同一会话从“6-9K”变为“大专 | 6-9K”。按姓名和
                # 列表职位回收旧记录，并只清除完全没有消息、动作或简历快照的
                # 空占位副本。任何承载业务数据的歧义记录都保留，拒绝猜测合并。
                stable_matches = self.connection.execute(
                    """
                    SELECT rc.id, rc.candidate_id,
                           (SELECT COUNT(*) FROM inbound_message im
                            WHERE im.conversation_id = rc.id) AS message_count,
                           (SELECT COUNT(*) FROM reply_action ra
                            WHERE ra.conversation_id = rc.id) AS action_count,
                           c.content_hash
                    FROM reply_conversation rc
                    JOIN candidate c ON c.id = rc.candidate_id
                    WHERE rc.list_display_name = ?
                      AND COALESCE(rc.job_title, '') = ?
                    ORDER BY (SELECT COUNT(*) FROM reply_action ra
                              WHERE ra.conversation_id = rc.id) DESC,
                             (SELECT COUNT(*) FROM inbound_message im
                              WHERE im.conversation_id = rc.id) DESC,
                             (c.content_hash IS NOT NULL) DESC,
                             rc.id ASC
                    """,
                    (card.display_name, card.position_text),
                ).fetchall()
                if stable_matches:
                    existing = stable_matches[0]
                    for duplicate in stable_matches[1:]:
                        if (
                            int(duplicate["message_count"]) == 0
                            and int(duplicate["action_count"]) == 0
                            and duplicate["content_hash"] is None
                        ):
                            duplicate_id = int(duplicate["id"])
                            duplicate_candidate_id = int(duplicate["candidate_id"])
                            self.connection.execute(
                                "DELETE FROM reply_conversation WHERE id = ?",
                                (duplicate_id,),
                            )
                            self.connection.execute(
                                """
                                DELETE FROM candidate
                                WHERE id = ?
                                  AND NOT EXISTS (
                                      SELECT 1 FROM reply_conversation
                                      WHERE candidate_id = ?
                                  )
                                """,
                                (duplicate_candidate_id, duplicate_candidate_id),
                            )
                            reconciled_empty_duplicate_ids.append(duplicate_id)
            if existing is not None:
                self.connection.execute(
                    """
                    UPDATE reply_conversation
                    SET conversation_fingerprint = ?, job_title = ?,
                        unread_count_before_open = ?, unread_signal = ?,
                        evidence_json = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        fingerprint,
                        card.position_text,
                        card.unread_count,
                        card.unread_signal,
                        evidence_json,
                        now,
                        existing["id"],
                    ),
                )
                return {
                    "candidate_id": int(existing["candidate_id"]),
                    "conversation_id": int(existing["id"]),
                    "created": False,
                    "conversation_fingerprint": fingerprint,
                    "reconciled_empty_duplicate_ids": reconciled_empty_duplicate_ids,
                }

            cursor = self.connection.execute(
                """
                INSERT INTO candidate(display_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?)
                """,
                (card.display_name, now, now),
            )
            candidate_id = int(cursor.lastrowid)
            cursor = self.connection.execute(
                """
                INSERT INTO reply_conversation(
                    candidate_id, conversation_fingerprint, list_display_name,
                    job_title, unread_count_before_open, unread_signal,
                    evidence_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    fingerprint,
                    card.display_name,
                    card.position_text,
                    card.unread_count,
                    card.unread_signal,
                    evidence_json,
                    now,
                    now,
                ),
            )
            return {
                "candidate_id": candidate_id,
                "conversation_id": int(cursor.lastrowid),
                "created": True,
                "conversation_fingerprint": fingerprint,
            }

    def stage_unread_card(
        self, card: NewGreetingCard, evidence: list[dict[str, str]]
    ) -> dict[str, int | bool | str]:
        """兼容旧调用名；新代码应使用 :meth:`stage_greeting_card`。"""
        return self.stage_greeting_card(card, evidence)

    def action_states_for_card(self, card: NewGreetingCard) -> tuple[str, ...]:
        """返回该列表会话已有动作状态，供列表级预筛避免无意义重开。

        聊天页右侧己方气泡仍是最终幂等事实源；这里仅用于跳过本地已知
        ``planned/pending/sent/unknown`` 会话，不能替代进入聊天后的气泡闸门。
        """
        rows = self.connection.execute(
            """
            SELECT DISTINCT ra.state
            FROM reply_conversation rc
            JOIN reply_action ra ON ra.conversation_id = rc.id
            WHERE rc.conversation_fingerprint = ?
               OR (rc.list_display_name = ? AND COALESCE(rc.job_title, '') = ?)
            ORDER BY ra.state
            """,
            (
                card.conversation_fingerprint,
                card.display_name,
                card.position_text,
            ),
        ).fetchall()
        return tuple(str(row["state"]) for row in rows)

    def stage_inbound_message(
        self,
        conversation_id: int,
        body_text: str,
        visible_time_text: str,
        evidence: list[dict[str, str]],
    ) -> dict[str, int | bool | str]:
        """按会话+可见日期时间+正文幂等保存一条入站消息。"""
        conversation = self.conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"会话不存在：{conversation_id}")
        body_text = body_text.strip()
        if not body_text:
            raise ValueError("入站消息正文不能为空")
        fingerprint_payload = {
            "platform": "boss_zhipin",
            "conversation_fingerprint": conversation["conversation_fingerprint"],
            "visible_time_text": visible_time_text.strip(),
            "body_text": body_text,
            "direction": "inbound",
        }
        message_fingerprint = hashlib.sha256(
            canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        evidence_json = canonical_json(evidence)
        now = now_iso()
        with self.connection:
            # 底部常用语面板会压缩聊天 RecyclerView，使原本可见的 HH:MM
            # 时间节点消失。此时同一条正文只能读到日期；若直接把日期参与哈希，
            # 会把同一消息误建成第二条。仅在“正文相同、日期相同、至少一侧缺少
            # 时分”时视为同一次观察；两侧都有不同时分时仍保留为不同消息。
            candidates = self.connection.execute(
                "SELECT id, conversation_id, message_fingerprint, visible_time_text "
                "FROM inbound_message WHERE conversation_id = ? AND body_text = ?",
                (conversation_id, body_text),
            ).fetchall()
            compatible = [
                row
                for row in candidates
                if _same_message_observation(
                    str(row["visible_time_text"] or ""), visible_time_text
                )
            ]
            if compatible:
                existing = max(
                    compatible,
                    key=lambda row: (
                        _time_precision(str(row["visible_time_text"] or "")),
                        -int(row["id"]),
                    ),
                )
                return {
                    "inbound_message_id": int(existing["id"]),
                    "message_fingerprint": str(existing["message_fingerprint"]),
                    "created": False,
                    "reconciled_observation": (
                        str(existing["message_fingerprint"]) != message_fingerprint
                    ),
                }
            cursor = self.connection.execute(
                """
                INSERT INTO inbound_message(
                    conversation_id, message_fingerprint, visible_time_text,
                    body_text, direction, evidence_json, observed_at
                ) VALUES (?, ?, ?, ?, 'inbound', ?, ?)
                """,
                (
                    conversation_id,
                    message_fingerprint,
                    visible_time_text.strip() or None,
                    body_text,
                    evidence_json,
                    now,
                ),
            )
            return {
                "inbound_message_id": int(cursor.lastrowid),
                "message_fingerprint": message_fingerprint,
                "created": True,
            }

    def begin_send(
        self,
        reply_action_id: int,
        conversation_id: int,
        inbound_message_id: int,
        phrase_key: str,
        phrase_text: str,
    ) -> dict[str, object]:
        """绑定已逐字核验的话术并进入 pending；尚未跨越发送边界。"""
        phrase_key = phrase_key.strip()
        phrase_text = phrase_text.strip()
        if not phrase_key or not phrase_text:
            raise ValueError("话术键和正文不能为空")
        with self.connection:
            action = self.reply_action(reply_action_id)
            if action is None:
                raise ValueError(f"回复动作不存在：{reply_action_id}")
            if (
                int(action["conversation_id"]) != conversation_id
                or int(action["inbound_message_id"]) != inbound_message_id
            ):
                raise ValueError("回复动作与会话/入站消息不一致")
            if action["send_boundary_at"] is not None:
                raise ValueError("回复动作已跨越发送边界，禁止再次发送")
            decision = str(action["decision"])
            if decision not in SENDABLE_REPLY_DECISIONS:
                raise ValueError(f"回复决策不允许发送：{decision}")
            configured_phrase = allowed_phrase(phrase_key)
            if phrase_text != configured_phrase["text"]:
                raise ValueError("话术正文与允许清单不一致")
            expected_decision = (
                "reject" if configured_phrase["intent"] == "reject" else "request_resume"
            )
            if decision != expected_decision:
                raise ValueError(
                    "兼容决策分类与话术意图不一致："
                    f"decision={decision}, intent={configured_phrase['intent']}"
                )
            state = str(action["state"])
            if state not in ("planned", "pending"):
                raise ValueError(f"回复动作状态不允许发送：{state}")
            if state == "pending" and (
                action["phrase_key"] != phrase_key
                or action["phrase_text"] != phrase_text
            ):
                raise ValueError("pending 动作已绑定其它话术，拒绝覆盖")
            self.connection.execute(
                """
                UPDATE reply_action
                SET phrase_key = ?, phrase_text = ?, state = 'pending'
                WHERE id = ?
                """,
                (phrase_key, phrase_text, reply_action_id),
            )
        return {
            "reply_action_id": reply_action_id,
            "state": "pending",
            "phrase_key": phrase_key,
            "phrase_text": phrase_text,
        }

    def cross_send_boundary(
        self, reply_action_id: int, evidence: list[dict[str, str]]
    ) -> str:
        """在触摸发送按钮前持久化不可重试边界。"""
        now = now_iso()
        with self.connection:
            action = self.reply_action(reply_action_id)
            if action is None:
                raise ValueError(f"回复动作不存在：{reply_action_id}")
            if str(action["decision"]) not in SENDABLE_REPLY_DECISIONS:
                raise ValueError("人工复核动作禁止跨越发送边界")
            if str(action["state"]) != "pending":
                raise ValueError("仅 pending 动作可以跨越发送边界")
            if action["send_boundary_at"] is not None:
                raise ValueError("发送边界已存在，禁止重复触摸发送")
            attempt_no = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM reply_send_attempt "
                    "WHERE reply_action_id = ?",
                    (reply_action_id,),
                ).fetchone()[0]
            ) + 1
            self.connection.execute(
                """
                INSERT INTO reply_send_attempt(
                    reply_action_id, attempt_no, phrase_key, phrase_text,
                    state, send_boundary_at, evidence_json
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    reply_action_id,
                    attempt_no,
                    str(action["phrase_key"]),
                    str(action["phrase_text"]),
                    now,
                    canonical_json(evidence),
                ),
            )
            self.connection.execute(
                """
                UPDATE reply_action
                SET send_boundary_at = ?, evidence_json = ?
                WHERE id = ?
                """,
                (now, canonical_json(evidence), reply_action_id),
            )
        return now

    def authorize_retry_after_no_effect(
        self,
        reply_action_id: int,
        evidence: list[dict[str, str]],
        authorization_note: str,
    ) -> dict[str, object]:
        """在用户明确授权且已有“未发送”证据时保留旧尝试并解锁一次重试。"""
        authorization_note = authorization_note.strip()
        if not authorization_note:
            raise ValueError("重试授权说明不能为空")
        now = now_iso()
        with self.connection:
            action = self.reply_action(reply_action_id)
            if action is None:
                raise ValueError(f"回复动作不存在：{reply_action_id}")
            if str(action["decision"]) not in SENDABLE_REPLY_DECISIONS:
                raise ValueError("人工复核动作禁止授权发送重试")
            if str(action["state"]) != "unknown" or action["send_boundary_at"] is None:
                raise ValueError("只有已跨边界的 unknown 动作可以人工授权重试")
            if not action["phrase_key"] or not action["phrase_text"]:
                raise ValueError("unknown 动作缺少已绑定话术")

            latest = self.connection.execute(
                """
                SELECT * FROM reply_send_attempt
                WHERE reply_action_id = ? ORDER BY attempt_no DESC LIMIT 1
                """,
                (reply_action_id,),
            ).fetchone()
            historical = []
            if action["evidence_json"]:
                try:
                    historical = json.loads(str(action["evidence_json"]))
                except json.JSONDecodeError:
                    historical = []
            combined_evidence = [*historical, *evidence]
            if latest is None:
                attempt_no = 1
                self.connection.execute(
                    """
                    INSERT INTO reply_send_attempt(
                        reply_action_id, attempt_no, phrase_key, phrase_text,
                        state, send_boundary_at, verified_at, evidence_json,
                        resolution_note, retry_authorized_at
                    ) VALUES (?, 1, ?, ?, 'unknown', ?, NULL, ?, ?, ?)
                    """,
                    (
                        reply_action_id,
                        str(action["phrase_key"]),
                        str(action["phrase_text"]),
                        str(action["send_boundary_at"]),
                        canonical_json(combined_evidence),
                        authorization_note,
                        now,
                    ),
                )
            else:
                attempt_no = int(latest["attempt_no"])
                if str(latest["state"]) != "unknown":
                    raise ValueError("最近一次尝试不是 unknown，拒绝重试")
                self.connection.execute(
                    """
                    UPDATE reply_send_attempt
                    SET evidence_json = ?, resolution_note = ?,
                        retry_authorized_at = ?
                    WHERE id = ?
                    """,
                    (
                        canonical_json(combined_evidence),
                        authorization_note,
                        now,
                        int(latest["id"]),
                    ),
                )
            self.connection.execute(
                """
                UPDATE reply_action
                SET state = 'planned', send_boundary_at = NULL,
                    verified_at = NULL, evidence_json = ?
                WHERE id = ?
                """,
                (canonical_json(evidence), reply_action_id),
            )
        return {
            "reply_action_id": reply_action_id,
            "resolved_attempt_no": attempt_no,
            "state": "planned",
            "retry_authorized_at": now,
        }

    def finish_send(
        self,
        reply_action_id: int,
        state: str,
        evidence: list[dict[str, str]],
    ) -> dict[str, object]:
        """根据发送后回读把 pending 封口为 sent 或 unknown。"""
        if state not in ("sent", "unknown"):
            raise ValueError(f"未知发送终态：{state}")
        now = now_iso()
        with self.connection:
            action = self.reply_action(reply_action_id)
            if action is None:
                raise ValueError(f"回复动作不存在：{reply_action_id}")
            if str(action["state"]) != "pending":
                raise ValueError(f"回复动作不是 pending：{action['state']}")
            if action["send_boundary_at"] is None:
                raise ValueError("尚未跨越发送边界，拒绝封口")
            attempt = self.connection.execute(
                """
                SELECT * FROM reply_send_attempt
                WHERE reply_action_id = ? AND state = 'pending'
                ORDER BY attempt_no DESC LIMIT 1
                """,
                (reply_action_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError("缺少 pending 发送尝试记录，拒绝封口")
            self.connection.execute(
                """
                UPDATE reply_send_attempt
                SET state = ?, verified_at = ?, evidence_json = ?
                WHERE id = ?
                """,
                (
                    state,
                    now if state == "sent" else None,
                    canonical_json(evidence),
                    int(attempt["id"]),
                ),
            )
            self.connection.execute(
                """
                UPDATE reply_action
                SET state = ?, verified_at = ?, evidence_json = ?
                WHERE id = ?
                """,
                (
                    state,
                    now if state == "sent" else None,
                    canonical_json(evidence),
                    reply_action_id,
                ),
            )
        return {
            "reply_action_id": reply_action_id,
            "state": state,
            "verified_at": now if state == "sent" else None,
        }

    def reconcile_unknown_as_sent(
        self,
        reply_action_id: int,
        evidence: list[dict[str, str]],
        resolution_note: str,
    ) -> dict[str, object]:
        """以聊天页“目标出站气泡+送达”证据追认最近 unknown 尝试。"""
        resolution_note = resolution_note.strip()
        if not resolution_note:
            raise ValueError("发送追认说明不能为空")
        now = now_iso()
        with self.connection:
            action = self.reply_action(reply_action_id)
            if action is None:
                raise ValueError(f"回复动作不存在：{reply_action_id}")
            if str(action["state"]) != "unknown" or action["send_boundary_at"] is None:
                raise ValueError("只有已跨边界的 unknown 动作可以追认为 sent")
            if not action["phrase_key"] or not action["phrase_text"]:
                raise ValueError("unknown 动作缺少已绑定话术")
            latest = self.connection.execute(
                """
                SELECT * FROM reply_send_attempt
                WHERE reply_action_id = ? ORDER BY attempt_no DESC LIMIT 1
                """,
                (reply_action_id,),
            ).fetchone()
            if latest is None or str(latest["state"]) != "unknown":
                raise ValueError("最近一次发送尝试不是 unknown，拒绝追认")
            if (
                str(latest["phrase_key"]) != str(action["phrase_key"])
                or str(latest["phrase_text"]) != str(action["phrase_text"])
            ):
                raise ValueError("最近一次尝试与回复动作绑定的话术不一致")
            evidence_json = canonical_json(evidence)
            self.connection.execute(
                """
                UPDATE reply_send_attempt
                SET state = 'sent', verified_at = ?, evidence_json = ?,
                    resolution_note = ?
                WHERE id = ?
                """,
                (now, evidence_json, resolution_note, int(latest["id"])),
            )
            self.connection.execute(
                """
                UPDATE reply_action
                SET state = 'sent', verified_at = ?, evidence_json = ?
                WHERE id = ?
                """,
                (now, evidence_json, reply_action_id),
            )
        return {
            "reply_action_id": reply_action_id,
            "reconciled_attempt_no": int(latest["attempt_no"]),
            "state": "sent",
            "verified_at": now,
        }

    def record_manual_sent(
        self,
        reply_action_id: int,
        evidence: list[dict[str, str]],
        resolution_note: str,
    ) -> dict[str, object]:
        """记录用户在真机界面手工完成、随后由气泡与送达状态证明的发送。

        手工点击没有 Agent 可观测的精确跨界时刻，因此用追认时刻作为保守的
        ``send_boundary_at``，并单独新增一次发送尝试，避免把此前的 unknown
        自动触摸伪装成成功。
        """
        resolution_note = resolution_note.strip()
        if not resolution_note:
            raise ValueError("人工发送追认说明不能为空")
        now = now_iso()
        with self.connection:
            action = self.reply_action(reply_action_id)
            if action is None:
                raise ValueError(f"回复动作不存在：{reply_action_id}")
            if str(action["decision"]) not in SENDABLE_REPLY_DECISIONS:
                raise ValueError("人工复核动作禁止记录人工发送")
            if str(action["state"]) not in ("planned", "unknown"):
                raise ValueError(
                    "只有 planned/unknown 动作可以记录人工发送："
                    f"{action['state']}"
                )
            if not action["phrase_key"] or not action["phrase_text"]:
                raise ValueError("回复动作缺少已绑定话术")
            attempt_no = self.send_attempt_count(reply_action_id) + 1
            evidence_json = canonical_json(evidence)
            self.connection.execute(
                """
                INSERT INTO reply_send_attempt(
                    reply_action_id, attempt_no, phrase_key, phrase_text,
                    state, send_boundary_at, verified_at, evidence_json,
                    resolution_note, retry_authorized_at
                ) VALUES (?, ?, ?, ?, 'sent', ?, ?, ?, ?, NULL)
                """,
                (
                    reply_action_id,
                    attempt_no,
                    str(action["phrase_key"]),
                    str(action["phrase_text"]),
                    now,
                    now,
                    evidence_json,
                    resolution_note,
                ),
            )
            self.connection.execute(
                """
                UPDATE reply_action
                SET state = 'sent', send_boundary_at = ?, verified_at = ?,
                    evidence_json = ?
                WHERE id = ?
                """,
                (now, now, evidence_json, reply_action_id),
            )
        return {
            "reply_action_id": reply_action_id,
            "recorded_attempt_no": attempt_no,
            "state": "sent",
            "verified_at": now,
            "manual_send": True,
        }

    def update_candidate_snapshot(
        self,
        candidate_id: int,
        expected_display_name: str,
        record: dict[str, object],
        source_activity: str,
        evidence: list[dict[str, str]],
    ) -> dict[str, object]:
        """把复用采集器得到的简历写回点击前创建的候选人占位行。"""
        row = self.connection.execute(
            "SELECT * FROM candidate WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"候选人不存在：{candidate_id}")
        actual_name = str(record.get("display_name") or "").strip()
        if not actual_name or actual_name != expected_display_name:
            raise ValueError(
                f"简历身份不一致：expected={expected_display_name!r}, actual={actual_name!r}"
            )
        now = now_iso()
        raw_capture = canonical_json(record)
        payload = dict(record)
        payload.update(
            {
                "source_activity": source_activity,
                "capture_version": CAPTURE_VERSION,
                # 2号以会话/消息为幂等单位，candidate 只保存简历快照；
                # 不写 fingerprint/content_hash，避免平台改写职位后同一人
                # 在占位行上产生无意义的唯一约束冲突。
                "content_hash": None,
                "raw_capture_json": raw_capture,
                "evidence_json": canonical_json(evidence),
                "last_seen_at": now,
            }
        )
        with self.connection:
            values = {column: payload.get(column) for column in VISIBLE_COLUMNS}
            assignments = ", ".join(f"{column} = ?" for column in VISIBLE_COLUMNS)
            self.connection.execute(
                f"UPDATE candidate SET fingerprint = NULL, {assignments} WHERE id = ?",
                [*values.values(), candidate_id],
            )
        return {
            "candidate_id": candidate_id,
            "fingerprint": None,
            "content_hash": None,
        }

    def plan_reply_action(
        self,
        conversation_id: int,
        inbound_message_id: int,
        decision: str,
        decision_reason: str,
        profile_key: str,
        profile_version: str,
    ) -> dict[str, int | bool | str]:
        """在任何话术选择或发送动作前建立消息级幂等记录。"""
        if decision not in ("request_resume", "reject", "needs_review"):
            raise ValueError(f"未知回复决策：{decision}")
        decision_reason = validate_evaluation_comment(decision_reason)
        conversation = self.conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"会话不存在：{conversation_id}")
        message = self.connection.execute(
            "SELECT * FROM inbound_message WHERE id = ?", (inbound_message_id,)
        ).fetchone()
        if message is None or int(message["conversation_id"]) != conversation_id:
            raise ValueError("入站消息不存在或不属于目标会话")
        idempotency_key = hashlib.sha256(
            ("reply:" + str(message["message_fingerprint"])).encode("utf-8")
        ).hexdigest()
        now = now_iso()
        candidate_decision = {
            "request_resume": "greet",
            "reject": "do_not_greet",
            "needs_review": "needs_review",
        }[decision]
        action_state = "needs_review" if decision == "needs_review" else "planned"
        with self.connection:
            existing = self.connection.execute(
                "SELECT * FROM reply_action WHERE inbound_message_id = ?",
                (inbound_message_id,),
            ).fetchone()
            if existing is not None:
                immutable = (
                    existing["idempotency_key"],
                    existing["decision"],
                    existing["decision_reason"],
                )
                expected = (idempotency_key, decision, decision_reason)
                if immutable != expected:
                    if str(existing["idempotency_key"]) != idempotency_key:
                        raise ValueError("该入站消息的幂等键发生变化，拒绝覆盖")
                    if (
                        str(existing["state"]) not in ("planned", "needs_review")
                        or existing["send_boundary_at"] is not None
                        or existing["phrase_key"] is not None
                    ):
                        raise ValueError("回复计划已绑定话术或跨越边界，拒绝修订")
                    self.connection.execute(
                        """
                        UPDATE reply_action
                        SET profile_key = ?, profile_version = ?, decision = ?,
                            decision_reason = ?, state = ?, planned_at = ?
                        WHERE id = ?
                        """,
                        (
                            profile_key,
                            profile_version,
                            decision,
                            decision_reason,
                            action_state,
                            now,
                            int(existing["id"]),
                        ),
                    )
                    self.connection.execute(
                        """
                        UPDATE candidate
                        SET eval_job_profile_key = ?, eval_job_profile_version = ?,
                            eval_decision = ?, eval_reasons_text = ?,
                            eval_evaluated_at = ?, eval_decision_source = ?,
                            last_seen_at = ?
                        WHERE id = ?
                        """,
                        (
                            profile_key,
                            profile_version,
                            candidate_decision,
                            decision_reason,
                            now,
                            "agent",
                            now,
                            int(conversation["candidate_id"]),
                        ),
                    )
                    return {
                        "reply_action_id": int(existing["id"]),
                        "idempotency_key": idempotency_key,
                        "state": action_state,
                        "created": False,
                        "revised": True,
                    }
                existing_state = str(existing["state"])
                if (
                    decision == "needs_review"
                    and existing_state == "planned"
                    and existing["send_boundary_at"] is None
                    and existing["phrase_key"] is None
                ):
                    self.connection.execute(
                        "UPDATE reply_action SET state = 'needs_review' WHERE id = ?",
                        (int(existing["id"]),),
                    )
                    existing_state = "needs_review"
                return {
                    "reply_action_id": int(existing["id"]),
                    "idempotency_key": idempotency_key,
                    "state": existing_state,
                    "created": False,
                    "revised": False,
                }
            cursor = self.connection.execute(
                """
                INSERT INTO reply_action(
                    conversation_id, inbound_message_id, idempotency_key,
                    profile_key, profile_version, decision, decision_reason,
                    state, planned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    inbound_message_id,
                    idempotency_key,
                    profile_key,
                    profile_version,
                    decision,
                    decision_reason,
                    action_state,
                    now,
                ),
            )
            self.connection.execute(
                """
                UPDATE candidate
                SET eval_job_profile_key = ?, eval_job_profile_version = ?,
                    eval_decision = ?, eval_reasons_text = ?,
                    eval_evaluated_at = ?, eval_decision_source = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    profile_key,
                    profile_version,
                    candidate_decision,
                    decision_reason,
                    now,
                    "agent",
                    now,
                    int(conversation["candidate_id"]),
                ),
            )
            return {
                "reply_action_id": int(cursor.lastrowid),
                "idempotency_key": idempotency_key,
                "state": action_state,
                "created": True,
            }


_DATE_RE = re.compile(r"^(\d{1,2}月\d{1,2}日)")
_CLOCK_RE = re.compile(r"(?:^|\s)(\d{1,2}:\d{2})(?:$|\s)")


def _time_parts(value: str) -> tuple[str, str]:
    normalized = " ".join(value.strip().split())
    date = (_DATE_RE.search(normalized).group(1) if _DATE_RE.search(normalized) else "")
    clock = (
        _CLOCK_RE.search(normalized).group(1) if _CLOCK_RE.search(normalized) else ""
    )
    return date, clock


def _time_precision(value: str) -> int:
    date, clock = _time_parts(value)
    return int(bool(date)) + int(bool(clock))


def _same_message_observation(existing: str, current: str) -> bool:
    existing_normalized = " ".join(existing.strip().split())
    current_normalized = " ".join(current.strip().split())
    if existing_normalized == current_normalized:
        return True
    # 面板完全展开后日期和时分节点都会离开控件树。此时没有足够证据
    # 区分同正文的重复发送，只能把它视为现有消息的一次低精度观察；优先
    # 复用带日期/时分的记录，禁止凭空创建一个更低精度的新消息。
    if not existing_normalized or not current_normalized:
        return True
    existing_date, existing_clock = _time_parts(existing_normalized)
    current_date, current_clock = _time_parts(current_normalized)
    return bool(
        existing_date
        and existing_date == current_date
        and (not existing_clock or not current_clock)
    )
