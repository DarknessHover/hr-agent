"""项目级标准CLI路由。

稳定入口是 ``python -m app``。原有 ``app.main`` 和 ``app.reply_main``
继续保留，避免中断已经验证的恢复命令。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument(
        "--evidence",
        choices=("off", "minimal", "debug"),
        default="minimal",
        help="XML/PNG证据等级；事件日志始终保留",
    )
    parser.add_argument(
        "target",
        choices=("doctor", "state1", "state2", "debug"),
        help="状态机或诊断入口",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parsed = build_parser().parse_args(argv)
    evidence = parsed.evidence
    target = parsed.target
    remainder = list(parsed.args)

    if target == "doctor":
        if remainder:
            raise SystemExit("doctor 不接受额外参数")
        from .main import main as state1_main

        return state1_main(["--evidence", evidence, "selfcheck"])

    if target == "debug":
        if not remainder or remainder[0] not in ("state1", "state2"):
            raise SystemExit("debug 后必须指定 state1 或 state2")
        target = remainder.pop(0)
        evidence = "debug"

    routed = ["--evidence", evidence, *remainder]
    if target == "state1":
        from .main import main as state1_main

        return state1_main(routed)
    if target == "state2":
        from .reply_main import main as state2_main

        return state2_main(routed)
    raise SystemExit(f"未知入口：{target}")
