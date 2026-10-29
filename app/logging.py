"""统一日志：按状态机分区生成运行目录。

    log/<flow>/<时间戳>_<标签>/
        events.jsonl   结构化事件（供程序化分析）
        run.log        可读文本（供人工排查，与 jsonl 一一对应）
        evidence/      截图与控件树留证

"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "log"
FLOW_PROACTIVE_GREETING = "proactive_greeting"
FLOW_REPLY_NEW_GREETINGS = "reply_new_greetings"
ALLOWED_LOG_FLOWS = frozenset(
    {FLOW_PROACTIVE_GREETING, FLOW_REPLY_NEW_GREETINGS}
)
EVIDENCE_LEVELS = frozenset({"off", "minimal", "debug"})


class RunLogger:
    """一次运行的日志器，同时写结构化 JSONL 与可读文本。"""

    def __init__(
        self,
        tag: str,
        echo: bool = True,
        *,
        flow: str = FLOW_PROACTIVE_GREETING,
        evidence_level: str | None = None,
    ) -> None:
        if flow not in ALLOWED_LOG_FLOWS:
            raise ValueError(
                f"未知日志分区 {flow!r}；允许值：{sorted(ALLOWED_LOG_FLOWS)!r}"
            )
        evidence_level = (
            evidence_level or os.environ.get("HR_AGENT_EVIDENCE", "minimal")
        ).strip().lower()
        if evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(
                f"未知证据等级 {evidence_level!r}；允许值：{sorted(EVIDENCE_LEVELS)!r}"
            )
        flow_dir = LOG_DIR / flow
        flow_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        self.flow = flow
        self.dir = flow_dir / f"{stamp}_{tag}"
        self.evidence_dir = self.dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.dir / "events.jsonl"
        self.text_path = self.dir / "run.log"
        self.echo = echo
        self.evidence_level = evidence_level
        self.event(
            "run_start",
            flow=flow,
            tag=tag,
            dir=str(self.dir),
            evidence_level=evidence_level,
        )

    def should_capture(self, *, critical: bool = False) -> bool:
        """返回是否保存XML/PNG等大体积证据。"""
        if self.evidence_level == "debug":
            return True
        return self.evidence_level == "minimal" and critical

    def _timestamp(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    def event(self, kind: str, **fields: object) -> None:
        record = {"ts": self._timestamp(), "kind": kind}
        record.update(fields)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        extras = " ".join(
            f"{key}={value!r}" for key, value in fields.items() if value not in ("", None)
        )
        line = f"[{record['ts']}] {kind} {extras}".rstrip()
        with self.text_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self.echo:
            try:
                print(line)
            except UnicodeEncodeError:
                encoding = sys.stdout.encoding or "ascii"
                safe_line = line.encode(encoding, "backslashreplace").decode(encoding)
                print(safe_line)

    def save_evidence(self, name: str, data: bytes) -> Path:
        path = self.evidence_dir / name
        path.write_bytes(data)
        return path
