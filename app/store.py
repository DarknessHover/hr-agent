"""单表 candidate 落盘（唯一写入口）。

设计依据：data/schema.sql（v2）
- 求职者信息存放在 candidate 一张表内，长文本区块原样存 TEXT。
- 重采 upsert 白名单：再次扫到同一候选人时，只更新可见事实列与 last_seen_at；
  greeting_*（尤其 sent/unknown 终态）与 first_seen_at 只读，不得被覆盖。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .review import ReviewPolicyError, validate_evaluation_comment

PLATFORM = "boss_zhipin"
CAPTURE_VERSION = 1

# 可见事实列：允许在重采时被更新
VISIBLE_COLUMNS = (
    "display_name", "age", "work_status", "active_status", "work_years",
    "degree", "headline_subtitle", "summary", "tags",
    "expect_role", "expect_city", "expect_salary", "expect_industry",
    "work_experience_text", "project_experience_text", "education_text",
    "club_experience_text", "skills_text", "honors_text",
    "internship_text", "certificate_text", "portfolio_image_text",
    "portfolio_video_text", "training_text", "volunteer_text",
    "source_activity", "capture_version", "content_hash", "raw_capture_json",
    "evidence_json", "last_seen_at",
)

# 结构升级：对已存在的库补齐新增列（保留既有行，不改动既有数据）
ADDED_COLUMNS = (
    ("internship_text", "TEXT"),
    ("certificate_text", "TEXT"),
    ("portfolio_image_text", "TEXT"),
    ("portfolio_video_text", "TEXT"),
    ("training_text", "TEXT"),
    ("volunteer_text", "TEXT"),
)

FINGERPRINT_FIELDS = ("display_name", "age", "work_years", "degree",
                      "expect_role", "expect_city")


class StoreError(RuntimeError):
    """落盘契约不满足。"""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compute_fingerprint(record: dict[str, object]) -> str | None:
    """由可见字段生成临时指纹。

    在职候选人没有学校/专业/毕业年份时，指纹会退化为近似"仅姓名"，
    这里要求除姓名外至少再有 2 个可用字段，否则返回 None（身份存疑）。
    """
    usable = {
        field: record.get(field)
        for field in FINGERPRINT_FIELDS
        if record.get(field) not in (None, "")
    }
    if "display_name" not in usable or len(usable) < 3:
        return None
    usable["platform"] = PLATFORM
    return sha256_text(canonical_json(usable))


class Store:
    def __init__(self, database_path: Path, schema_path: Path) -> None:
        self.database_path = database_path
        self.schema_path = schema_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(schema_path.read_text(encoding="utf-8"))
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """对已存在的库补齐新增列；既有行保留，新列为 NULL。"""
        existing = {
            row[1] for row in self.connection.execute("PRAGMA table_info(candidate)")
        }
        missing = [
            (name, coltype)
            for name, coltype in ADDED_COLUMNS
            if name not in existing
        ]
        if not missing:
            return
        with self.connection:
            for name, coltype in missing:
                self.connection.execute(
                    f"ALTER TABLE candidate ADD COLUMN {name} {coltype}"
                )

    def close(self) -> None:
        self.connection.close()

    def find_by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM candidate WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()

    def find_by_id(self, candidate_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM candidate WHERE id = ?", (candidate_id,)
        ).fetchone()

    def greeting_finalized_for(self, display_name: str) -> bool:
        """按姓名软匹配，判断该候选人是否已封口（已触达/已有会话）。

        仅用于选卡阶段预跳过，避免重复进入已打招呼者的简历页。
        """
        row = self.connection.execute(
            """
            SELECT 1 FROM candidate
            WHERE display_name = ?
              AND greeting_state IN ('sent', 'unknown', 'session_exists')
            LIMIT 1
            """,
            (display_name,),
        ).fetchone()
        return row is not None

    def seen_by_name(self, display_name: str) -> bool:
        """该姓名是否已在库中（已采集过，不再重复进入其简历页）。"""
        row = self.connection.execute(
            "SELECT 1 FROM candidate WHERE display_name = ? LIMIT 1",
            (display_name,),
        ).fetchone()
        return row is not None

    def approved_pending(self) -> list[sqlite3.Row]:
        """已由 Agent 判定为 greet、且尚未发送的候选人。"""
        return self.connection.execute(
            "SELECT * FROM candidate WHERE eval_decision = 'greet' "
            "AND greeting_state = 'pending' ORDER BY id"
        ).fetchall()

    def set_evaluation(
        self,
        candidate_id: int,
        decision: str,
        comment: str,
        profile_key: str | None = None,
        profile_version: str | None = None,
    ) -> None:
        """写入 Agent 判断；评语只要求非空，建议长度由上层提示。"""
        if decision not in ("greet", "do_not_greet", "needs_review"):
            raise StoreError(f"未知决策：{decision}")
        if self.find_by_id(candidate_id) is None:
            raise StoreError(f"候选人不存在：{candidate_id}")
        try:
            comment = validate_evaluation_comment(comment)
        except ReviewPolicyError as exc:
            raise StoreError(str(exc)) from exc
        with self.connection:
            self.connection.execute(
                """
                UPDATE candidate
                SET eval_job_profile_key = ?, eval_job_profile_version = ?,
                    eval_decision = ?, eval_reasons_text = ?,
                    eval_evaluated_at = ?, eval_decision_source = ?
                WHERE id = ?
                """,
                (
                    profile_key, profile_version, decision, comment,
                    now_iso(), "agent", candidate_id,
                ),
            )

    def save_snapshot(
        self,
        record: dict[str, object],
        source_activity: str,
        evidence: list[dict[str, str]],
    ) -> dict[str, object]:
        """写入或更新一条候选人记录，返回 {candidate_id, created, fingerprint}。"""
        timestamp = now_iso()
        payload = dict(record)
        payload["source_activity"] = source_activity
        payload["capture_version"] = CAPTURE_VERSION
        payload["raw_capture_json"] = canonical_json(record)
        payload["evidence_json"] = canonical_json(evidence)
        payload["last_seen_at"] = timestamp

        fingerprint = compute_fingerprint(record)
        existing = (
            self.find_by_fingerprint(fingerprint) if fingerprint else None
        )

        with self.connection:
            if existing is not None:
                values = {column: payload.get(column) for column in VISIBLE_COLUMNS}
                assignments = ", ".join(f"{column} = ?" for column in VISIBLE_COLUMNS)
                self.connection.execute(
                    f"UPDATE candidate SET {assignments} WHERE id = ?",
                    [*values.values(), existing["id"]],
                )
                return {"candidate_id": existing["id"], "created": False,
                        "fingerprint": fingerprint}

            payload.setdefault("platform", PLATFORM)
            payload["fingerprint"] = fingerprint
            payload["greeting_state"] = "pending"
            payload["greeting_planned_at"] = timestamp
            payload["first_seen_at"] = timestamp
            columns = [
                "platform", "platform_candidate_id", "fingerprint",
                *VISIBLE_COLUMNS, "greeting_state", "greeting_planned_at",
                "first_seen_at",
            ]
            values = [payload.get(column) for column in columns]
            placeholders = ", ".join("?" for _ in columns)
            try:
                cursor = self.connection.execute(
                    f"INSERT INTO candidate ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                if "content_hash" not in str(exc):
                    raise
                row = self.connection.execute(
                    "SELECT id FROM candidate WHERE content_hash = ?",
                    (payload.get("content_hash"),),
                ).fetchone()
                if row is None:
                    raise
                return {"candidate_id": row["id"], "created": False,
                        "fingerprint": fingerprint}
            return {"candidate_id": cursor.lastrowid, "created": True,
                    "fingerprint": fingerprint}

    def mark_greeting(
        self,
        candidate_id: int,
        state: str,
        phrase: str | None = None,
        evidence: list[dict[str, str]] | None = None,
    ) -> None:
        timestamp = now_iso()
        with self.connection:
            row = self.find_by_id(candidate_id)
            if row is None:
                raise StoreError(f"候选人不存在：{candidate_id}")
            if row["greeting_state"] in ("sent", "unknown", "aborted") and state != row["greeting_state"]:
                raise StoreError(
                    f"拒绝改写已封口的招呼状态：{row['greeting_state']} → {state}"
                )
            boundary = timestamp if state in ("sent", "unknown") else row["greeting_boundary_at"]
            verified = timestamp if state == "sent" else row["greeting_verified_at"]
            self.connection.execute(
                """
                UPDATE candidate
                SET greeting_state = ?, greeting_phrase = COALESCE(?, greeting_phrase),
                    greeting_boundary_at = ?, greeting_verified_at = ?,
                    evidence_json = COALESCE(?, evidence_json), last_seen_at = ?
                WHERE id = ?
                """,
                (
                    state, phrase, boundary, verified,
                    canonical_json(evidence) if evidence else None,
                    timestamp, candidate_id,
                ),
            )
