"""HR 端招聘 Agent 原型 · 有限循环状态机与 CLI。

主链（S00–S90）中插入了「Agent 判断节点」，因此拆成步进式三段：
    S00 自检 → S10 确保列表 → S20/S21 扫描选卡 → S30 进入求职者页
    → S31 读取并落盘（collect，不发送）
    → ★ Agent 判断：对照 config/job_profiles.yaml 写评语与决策（建议100–300字，decide）
    → S51 点击「立即沟通」（唯一不可逆动作）→ S52 回读（greet，仅对已批准者）
    → S60 返回列表 → S70 下滑 → S80 收工

用法：
    python -m app.main selfcheck
    python -m app.main scan-list
    python -m app.main read-resume
    python -m app.main collect --max-candidates 3          # 只采集落盘，不发送
    python -m app.main decide --candidate-id 5 --decision greet --comment "评语…"
    python -m app.main greet --max-greetings 20             # 仅发送已批准者
    python -m app.main run --max-greetings 5 --execute      # 一条龙（无判断节点，直接发送）
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

from .device import Device, DeviceError
from .logging import FLOW_PROACTIVE_GREETING, RunLogger
from .resume import (
    Screen,
    bottom_action,
    is_first_contact_action,
    is_paid_contact_action,
    list_cards,
    resume_identity,
)
from .resume_collector import collect_resume
from .review import evaluation_comment_advisory
from .screen_ops import (
    StopRun,
    enter_candidate_page,
    evidence_entry,
    go_back_to,
    page_kind,
    read_screen,
    scroll_down,
    tap_settled,
    wait_for_kind,
)
from .settings import SettingsError
from .store import Store, StoreError
from .walker import ListWalker

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "hr_agent_v2.sqlite3"
SCHEMA_PATH = ROOT / "data" / "schema.sql"

# 发送是唯一不可逆动作：只允许「可验证未发送」时的受控重试（见 _handle_greeting）。
SEND_CHAT_TIMEOUT = 8.0
SEND_READBACK_TIMEOUT = 25.0
SEND_RETRY_INTERVAL = 2.5
DEFAULT_SEND_ATTEMPTS = 3


def _stop_with_evidence(
    device: Device | None, logger: RunLogger, exc: Exception
) -> int:
    """停机收尾：记录原因，并尽力保存「停机现场」（最后已知页面）供人工排查。

    留证本身失败（例如设备已掉线）不得掩盖停机原因，因此单独兜底并记事件。
    """
    logger.event("stop", reason=str(exc))
    print(f"STOP: {exc}")
    if device is None:
        return 2
    stamp = time.strftime("%H%M%S")
    try:
        if logger.should_capture(critical=True):
            logger.event(
                "stop_evidence",
                evidence_kind="ui_tree",
                path=str(
                    logger.save_evidence(f"stop_{stamp}.xml", device.dump_ui())
                ),
            )
    except Exception as capture_error:
        logger.event(
            "stop_evidence_failed", evidence_kind="ui_tree", reason=str(capture_error)
        )
    try:
        if logger.should_capture(critical=True):
            logger.event(
                "stop_evidence",
                evidence_kind="screenshot",
                path=str(
                    logger.save_evidence(f"stop_{stamp}.png", device.screenshot())
                ),
            )
    except Exception as capture_error:
        logger.event(
            "stop_evidence_failed", evidence_kind="screenshot", reason=str(capture_error)
        )
    return 2


# 通用页面原语（tap_settled / read_screen / wait_for_kind / go_back_to /
# enter_candidate_page / scroll_down / evidence_entry / safe_name）已抽取到
# app/screen_ops.py，本模块统一 import 复用。


def read_back_greeting(screen: Screen) -> tuple[str | None, bool]:
    phrase: str | None = None
    delivered = False
    for node in screen.nodes():
        if node.short_id == "tv_content_text" and node.text.strip():
            phrase = node.text.strip()
        if node.short_id == "mMsgTvStatus" and "送达" in node.text:
            delivered = True
    return phrase, delivered


# ---- 命令 ---------------------------------------------------------------
def cmd_selfcheck(device: Device, logger: RunLogger) -> None:
    identity = device.verify_identity()
    logger.event("device_verified", **identity)
    logger.event("boss_installed", package=device.require_boss_installed())
    logger.event("schema_exists", path=str(SCHEMA_PATH), exists=SCHEMA_PATH.is_file())
    logger.event("selfcheck_ok")


def cmd_scan_list(device: Device, logger: RunLogger) -> None:
    device.verify_identity()
    focus, screen = read_screen(device)
    kind = page_kind(focus, screen)
    cards = list_cards(screen)
    logger.event("scan_list", page=kind, focus=focus, cards=[name for name, _ in cards])
    print(f"页面={kind} 卡片={[name for name, _ in cards]}")


def cmd_read_resume(device: Device, logger: RunLogger, max_scrolls: int) -> None:
    device.verify_identity()
    focus, screen = read_screen(device)
    if page_kind(focus, screen) != "resume":
        logger.event("not_resume", page=page_kind(focus, screen))
        raise StopRun("当前不在简历页；请先定位到求职者界面")
    record, _, _ = collect_resume(
        device, logger, max_scrolls, resume_identity(screen) or "current"
    )
    for key, value in record.items():
        print(f"{key}: {value}")


def cmd_run(device: Device, logger: RunLogger, args: argparse.Namespace) -> int:
    execute = getattr(args, "execute", False)
    skip_seen = args.command == "collect"
    identity = device.verify_identity()
    logger.event("device_verified", **identity)
    device.require_boss_installed()
    store = Store(Path(args.db), SCHEMA_PATH)
    logger.event("store_ready", db=str(args.db), execute=execute)

    walker = ListWalker(
        device, logger, store,
        collect_fn=collect_resume,
        max_stagnant_scrolls=args.max_stagnant_scrolls,
        enter_attempts=args.enter_attempts,
        enter_timeout=args.enter_timeout,
        empty_reason="列表无新对象",
    )
    try:
        walker.ensure_list()

        greeted = 0
        candidates_seen = 0
        while candidates_seen < args.max_candidates and (
            (not execute) or greeted < args.max_greetings
        ):
            target = walker.next_card(skip_seen=skip_seen)
            if target is None:
                break

            name, point = target
            candidates_seen += 1
            captured = walker.capture(target, args.max_scrolls)
            if captured is None:
                cur_focus, cur_screen = read_screen(device)
                kind = page_kind(cur_focus, cur_screen)
                logger.event("enter_failed", name=name, page=kind, focus=cur_focus)
                raise StopRun(f"点击候选人 {name} 后未进入简历页；当前页面={kind}")

            candidate_id = int(captured["candidate_id"])
            record = captured["record"]
            if not execute:
                print("---- 判断素材 ----")
                print(f"candidate_id: {candidate_id}")
                for key, value in record.items():
                    print(f"{key}: {value}")
                print("---- 素材结束 ----")

            greeted += _handle_greeting(
                device, logger, store, candidate_id, name, execute,
                args.max_send_attempts,
            )
            go_back_to(device, "list")
            if args.cooldown_max > 0:
                delay = random.uniform(args.cooldown_min, args.cooldown_max)
                logger.event("cooldown", seconds=round(delay, 1))
                time.sleep(delay)
        else:
            logger.event("finish", reason="达到配额/上限", greeted=greeted)
        logger.event("run_done", greeted=greeted, candidates_seen=candidates_seen)
        return 0
    except (DeviceError, StopRun, StoreError) as exc:
        return _stop_with_evidence(device, logger, exc)
    finally:
        store.close()


def _still_greetable(
    device: Device, name: str
) -> tuple[str, str | None, tuple[int, int] | None]:
    """判断「上一次点击确实没生效」，返回 (状态, 底部按钮文案, 落点)。

    只有同时满足下面三点，才说明上次点击没有建立会话、允许重试：
      1) 仍在简历页；
      2) 页面姓名 == 目标姓名；
      3) 底部按钮仍是可发起首次招呼的文案（「立即沟通」或匿名卡片的「立即联系牛人」）。
    其余情况（按钮变「继续沟通」、页面离开简历页、身份不符、读屏失败）一律不可重试。
    """
    try:
        focus, screen = read_screen(device)
    except DeviceError:
        return "unreadable", None, None
    kind = page_kind(focus, screen)
    if kind != "resume":
        return f"page:{kind}", None, None
    if resume_identity(screen) != name:
        return "identity_mismatch", None, None
    action = bottom_action(screen)
    if action is None or not is_first_contact_action(action[0]):
        return "bottom_changed", action[0] if action else None, None
    return "greetable", action[0], action[1]


def _handle_greeting(
    device: Device,
    logger: RunLogger,
    store: Store,
    candidate_id: int,
    name: str,
    execute: bool,
    max_send_attempts: int = DEFAULT_SEND_ATTEMPTS,
) -> int:
    """执行单个候选人的首次招呼，返回本轮新增成功数（0/1）。

    发送是唯一不可逆动作，「重试」只允许一种**可证明未生效**的情形：
    上一跳没有进入会话页，且当前仍在同一位目标候选人的简历页、底部按钮仍是「立即沟通」。
    任一不满足即封口为 unknown，不做任何猜测点击。
    """
    focus, screen = read_screen(device)
    action = bottom_action(screen)
    label, point = action if action else (None, None)

    row = store.find_by_id(candidate_id)
    if row is not None and row["greeting_state"] in ("sent", "unknown", "session_exists"):
        logger.event("skip_greeting", name=name, state=row["greeting_state"])
        return 0
    if is_paid_contact_action(label):
        # 平台付费广告入口：不属于可发的招呼，记为 aborted 而非已有会话。
        store.mark_greeting(candidate_id, "aborted")
        logger.event("paid_contact_card_skipped", name=name, bottom_label=label)
        return 0
    if not is_first_contact_action(label):
        store.mark_greeting(candidate_id, "session_exists")
        logger.event("session_exists", name=name, bottom_label=label)
        return 0
    if not execute:
        logger.event("dry_run_skip_send", name=name, bottom_label=label)
        return 0

    for attempt in range(1, max_send_attempts + 1):
        logger.event("send_click", name=name, point=point, attempt=attempt)
        tap_settled(device, logger, point, f"send:{name}")
        chat = wait_for_kind(device, "chat", timeout=SEND_CHAT_TIMEOUT)
        if chat is not None:
            phrase, delivered = read_back_greeting(chat[1])
            # 网络较慢时，会话页可能先出现、招呼消息稍后才渲染；
            # 在封口前再等一个短窗口，避免把「已发出但晚到」误判为 unknown。
            readback_deadline = time.monotonic() + SEND_READBACK_TIMEOUT
            while not delivered and time.monotonic() < readback_deadline:
                time.sleep(1.0)
                try:
                    focus2, screen2 = read_screen(device)
                except DeviceError:
                    break
                if page_kind(focus2, screen2) != "chat":
                    break
                phrase_now, delivered = read_back_greeting(screen2)
                if phrase_now:
                    phrase = phrase_now
            evidence = []
            if logger.should_capture(critical=True):
                evidence = [
                    evidence_entry(
                        logger.save_evidence(
                            f"chat_{name}.png", device.screenshot()
                        )
                    )
                ]
            if delivered:
                store.mark_greeting(candidate_id, "sent", phrase=phrase, evidence=evidence)
                logger.event("greeting_sent", name=name, phrase=phrase, attempt=attempt)
                return 1
            store.mark_greeting(candidate_id, "unknown", phrase=phrase, evidence=evidence)
            logger.event(
                "greeting_unknown", name=name, phrase=phrase,
                reason="未回读到送达", attempt=attempt,
            )
            return 0

        verdict, label_now, point_now = _still_greetable(device, name)
        if verdict != "greetable":
            store.mark_greeting(candidate_id, "unknown")
            logger.event(
                "greeting_unknown", name=name, attempt=attempt,
                reason=f"点击后未进入会话页且不可重试（{verdict}）",
            )
            return 0
        if attempt < max_send_attempts:
            logger.event(
                "send_retry", name=name, next_attempt=attempt + 1,
                bottom_label=label_now, reason="已证明上次点击未建立会话",
            )
            point = point_now
            time.sleep(SEND_RETRY_INTERVAL)

    store.mark_greeting(candidate_id, "unknown")
    logger.event(
        "greeting_unknown", name=name,
        reason=f"连续 {max_send_attempts} 次点击均未进入会话页",
    )
    return 0


def cmd_decide(logger: RunLogger, args: argparse.Namespace) -> int:
    """写入 Agent 判断；评语非空即可落库，建议100–300字。"""
    store = Store(Path(args.db), SCHEMA_PATH)
    try:
        store.set_evaluation(
            args.candidate_id,
            args.decision,
            args.comment,
            args.profile_key,
            args.profile_version,
        )
        row = store.find_by_id(args.candidate_id)
        comment_advisory = evaluation_comment_advisory(row["eval_reasons_text"])
        logger.event(
            "eval_recorded",
            candidate_id=args.candidate_id,
            display_name=row["display_name"],
            decision=args.decision,
            comment_len=len(row["eval_reasons_text"]),
            comment_length_advisory=comment_advisory,
            greeting_state=row["greeting_state"],
        )
        if comment_advisory:
            print(f"提示：{comment_advisory}")
        if args.decision == "greet":
            print(f"已批准 #{args.candidate_id} {row['display_name']} → 可执行 greet 发送")
        else:
            print(f"已记录 #{args.candidate_id} {row['display_name']} → {args.decision}")
        return 0
    finally:
        store.close()


def cmd_collect(device: Device, logger: RunLogger, args: argparse.Namespace) -> int:
    """采集一位候选人后**停在简历页**等待 Agent 决策（不返回列表）。"""
    identity = device.verify_identity()
    logger.event("device_verified", **identity)
    device.require_boss_installed()
    store = Store(Path(args.db), SCHEMA_PATH)
    walker = ListWalker(
        device, logger, store,
        collect_fn=collect_resume,
        max_stagnant_scrolls=args.max_stagnant_scrolls,
        enter_attempts=args.enter_attempts,
        enter_timeout=args.enter_timeout,
        empty_reason="列表无新候选人",
    )
    try:
        walker.ensure_list()

        target = walker.next_card()
        if target is None:
            return 0

        name, point = target
        captured = walker.capture(target, args.max_scrolls)
        if captured is None:
            logger.event("enter_failed", name=name)
            return 2

        candidate_id = int(captured["candidate_id"])
        record = captured["record"]
        _, tail_screen = read_screen(device)
        action = bottom_action(tail_screen)
        label = action[0] if action else None
        logger.event(
            "awaiting_decision",
            candidate_id=candidate_id,
            display_name=name,
            bottom_label=label,
            on_page=resume_identity(tail_screen),
        )
        print("---- 判断素材 ----")
        print(f"candidate_id: {candidate_id}")
        for key, value in record.items():
            print(f"{key}: {value}")
        print(f"bottom_label: {label}")
        print("---- 素材结束 ----")
        return 0
    except (DeviceError, StopRun, StoreError) as exc:
        return _stop_with_evidence(device, logger, exc)
    finally:
        store.close()


def _leave_resume(device: Device, logger: RunLogger) -> None:
    """从简历页返回牛人列表（尽力而为，失败只记日志）。"""
    try:
        go_back_to(device, "list")
    except StopRun as exc:
        logger.event("leave_resume_failed", reason=str(exc))


def cmd_greet(device: Device, logger: RunLogger, args: argparse.Namespace) -> int:
    """结束当前候选人：已批准且页面身份相符 → 原地发送；否则直接返回列表。"""
    identity = device.verify_identity()
    logger.event("device_verified", **identity)
    device.require_boss_installed()
    store = Store(Path(args.db), SCHEMA_PATH)
    try:
        row = store.find_by_id(args.candidate_id)
        if row is None:
            raise StopRun(f"候选人不存在：{args.candidate_id}")
        name = row["display_name"]

        if row["greeting_state"] in ("sent", "unknown", "session_exists"):
            logger.event("greet_noop", name=name, reason="已封口")
            _leave_resume(device, logger)
            return 0
        if row["eval_decision"] != "greet":
            logger.event("greet_skipped", name=name, decision=row["eval_decision"],
                         reason="未通过，直接返回")
            _leave_resume(device, logger)
            return 0

        focus, screen = read_screen(device)
        if page_kind(focus, screen) == "resume":
            on_page = resume_identity(screen)
            if on_page is None:
                raise StopRun("无法确认当前简历页身份；停机留证，不做猜测点击")
            if on_page != name:
                raise StopRun(
                    f"当前简历页为 {on_page!r}，目标为 {name!r}；停机留证"
                )
            logger.event("greet_in_place", name=name, candidate_id=args.candidate_id)
            result = _handle_greeting(
                device, logger, store, args.candidate_id, name, True,
                args.max_send_attempts,
            )
            _leave_resume(device, logger)
            logger.event("run_done", command="greet", greeted=result)
            return result

        logger.event("greet_fallback_list", name=name)
        go_back_to(device, "list")
        stagnant = 0
        while True:
            focus, screen = read_screen(device)
            if page_kind(focus, screen) != "list":
                go_back_to(device, "list")
                continue
            card = next((c for c in list_cards(screen) if c[0] == name), None)
            if card is None:
                scroll_down(device, logger)
                stagnant += 1
                if stagnant >= args.max_stagnant_scrolls:
                    logger.event("finish", reason="列表未定位到目标候选人")
                    return 0
                continue
            _, point = card
            entered = enter_candidate_page(
                device, logger, name, point, args.enter_attempts, args.enter_timeout
            )
            if entered is None:
                logger.event("greet_skipped", name=name, reason="未进入简历页")
                go_back_to(device, "list")
                return 0
            result = _handle_greeting(
                device, logger, store, args.candidate_id, name, True,
                args.max_send_attempts,
            )
            _leave_resume(device, logger)
            logger.event("run_done", command="greet", greeted=result)
            return result
    except (DeviceError, StopRun) as exc:
        return _stop_with_evidence(device, logger, exc)
    finally:
        store.close()


def _add_navigation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-candidates", type=int, default=20,
                        help="本轮最多进入的候选人页数（硬上限）")
    parser.add_argument("--max-scrolls", type=int, default=20,
                        help="单个简历页滑动次数的纯安全上限（主停止条件为读到牛人分析器或触底）")
    parser.add_argument("--max-stagnant-scrolls", type=int, default=3,
                        help="列表连续无新对象的滚动次数上限")
    parser.add_argument("--enter-timeout", type=float, default=12.0,
                        help="点击候选人后等待简历页出现的秒数")
    parser.add_argument("--enter-attempts", type=int, default=3,
                        help="进入简历页的导航点击最多重试次数（仅导航，不含发送）")
    parser.add_argument("--max-send-attempts", type=int, default=DEFAULT_SEND_ATTEMPTS,
                        help="发送点击的最多尝试次数；仅当「仍在目标简历页且按钮仍为立即沟通」时才重试")
    parser.add_argument("--cooldown-min", type=float, default=0.0,
                        help="候选人之间的最短冷却秒数")
    parser.add_argument("--cooldown-max", type=float, default=0.0,
                        help="候选人之间的最长冷却秒数；大于 0 才生效")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adb",
        help="覆盖 HR_AGENT_ADB_PATH；设备身份仍以环境变量中的准入值为准",
    )
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--evidence",
        choices=("off", "minimal", "debug"),
        default="minimal",
        help="证据等级；正式运行默认 minimal",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selfcheck", help="校验设备身份与安装包")
    sub.add_parser("scan-list", help="打印当前屏可见候选人卡片")
    read_parser = sub.add_parser("read-resume", help="读取当前简历页并打印，不落盘")
    read_parser.add_argument("--max-scrolls", type=int, default=20)

    run_parser = sub.add_parser(
        "run", help="一条龙：采集后直接按 --execute 决定是否发送（默认 dry-run）"
    )
    run_parser.add_argument("--max-greetings", type=int, default=1,
                            help="本轮成功招呼配额（人工决定）")
    _add_navigation_args(run_parser)
    run_parser.add_argument("--execute", action="store_true",
                            help="实际点击发送；缺省为 dry-run")

    collect_parser = sub.add_parser(
        "collect", help="只采集落盘、不发送（Agent 评估前的采集阶段）"
    )
    _add_navigation_args(collect_parser)

    greet_parser = sub.add_parser(
        "greet", help="结束当前候选人：已批准则原地发送，否则直接返回列表"
    )
    greet_parser.add_argument("--candidate-id", type=int, required=True)
    _add_navigation_args(greet_parser)

    decide_parser = sub.add_parser("decide", help="写入 Agent 判断：决策 + 评语")
    decide_parser.add_argument("--candidate-id", type=int, required=True)
    decide_parser.add_argument(
        "--decision", required=True,
        choices=("greet", "do_not_greet", "needs_review"),
    )
    decide_parser.add_argument(
        "--comment", required=True, help="非空评语；建议100–300字，超出仍可入库"
    )
    decide_parser.add_argument("--profile-key", default="job_profiles.yaml")
    decide_parser.add_argument("--profile-version", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = RunLogger(
        args.command,
        flow=FLOW_PROACTIVE_GREETING,
        evidence_level=args.evidence,
    )
    device: Device | None = None
    try:
        if args.command == "decide":
            return cmd_decide(logger, args)
        device = Device(adb=Path(args.adb) if args.adb else None)
        if args.command == "selfcheck":
            cmd_selfcheck(device, logger)
            return 0
        if args.command == "scan-list":
            cmd_scan_list(device, logger)
            return 0
        if args.command == "read-resume":
            cmd_read_resume(device, logger, args.max_scrolls)
            return 0
        if args.command == "run":
            return cmd_run(device, logger, args)
        if args.command == "collect":
            return cmd_collect(device, logger, args)
        if args.command == "greet":
            return cmd_greet(device, logger, args)
        raise SystemExit(f"未知命令：{args.command}")
    except (DeviceError, SettingsError, StopRun, StoreError) as exc:
        return _stop_with_evidence(device, logger, exc)


if __name__ == "__main__":
    raise SystemExit(main())
