"""两部状态机共享的完整简历采集服务。

本模块只理解“当前已在某位候选人的简历页”这一前置条件，不理解牛人列表、
新招呼列表、首次招呼或回复动作。调用方负责进入简历页、提供预期姓名，以及
在采集完成后决定停留、返回或继续业务状态机。
"""

from __future__ import annotations

import time

from .device import Device
from .logging import RunLogger
from .resume import (
    ResumeStitcher,
    Screen,
    fold_targets,
    resume_content_ready,
    resume_identity,
    viewport_signature,
)
from .screen_ops import (
    StopRun,
    evidence_entry,
    page_kind,
    read_screen,
    safe_name,
    scroll_down,
    tap_settled,
)

MAX_EXPANSIONS_PER_SCREEN = 4


def _visible_shape(screen: Screen) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (node.short_id, node.text.strip(), node.element.attrib.get("bounds", ""))
        for node in screen.nodes()
        if node.text.strip()
    )


def expand_folded(
    device: Device, logger: RunLogger, name: str, screen: Screen
) -> Screen:
    """逐屏展开折叠正文，返回展开后的最后一屏控件树。

    每个候选落点都重新读屏并比较可见文本形状，只有形状变化才认定展开生效，
    坐标不跨读屏复用。一处目标的全部探测点都无变化时记 `fold_expand_stuck`
    并继续采集，让残缺证据留在事件日志里，而不是把整轮停在一次无效触摸上。
    """
    current = screen
    for _ in range(MAX_EXPANSIONS_PER_SCREEN):
        targets = fold_targets(current)
        if not targets:
            return current
        target = targets[0]
        before = _visible_shape(current)
        for point in target.points:
            tap_settled(
                device,
                logger,
                point,
                f"expand_fold:{name}:{target.marker}",
                gesture="tap",
                duration_ms=120,
            )
            time.sleep(1.0)
            focus, refreshed = read_screen(device)
            if page_kind(focus, refreshed) != "resume":
                raise StopRun("展开折叠正文时离开简历页")
            if resume_identity(refreshed) != name:
                raise StopRun("展开折叠正文时候选人身份发生漂移")
            if _visible_shape(refreshed) != before:
                logger.event(
                    "fold_expanded",
                    display_name=name,
                    marker=target.marker,
                    point=point,
                )
                current = refreshed
                break
            logger.event(
                "fold_expand_no_effect",
                display_name=name,
                marker=target.marker,
                point=point,
            )
        else:
            logger.event("fold_expand_stuck", display_name=name, marker=target.marker)
            return current
    return current


def collect_resume(
    device: Device, logger: RunLogger, max_scrolls: int, name: str
) -> tuple[dict[str, object], str, list[dict[str, str]]]:
    """读取当前简历页的多屏内容，返回记录、来源窗口和证据列表。

    主停止条件是读到“牛人分析器”区块，或滑动后视图不再变化（触底）；
    ``max_scrolls`` 仅作安全上限。页面类型和候选人姓名在正文加载阶段都会
    逐次校验，避免骨架页或身份漂移污染任一状态机的数据库。
    """
    ready_deadline = time.monotonic() + 60.0
    while time.monotonic() < ready_deadline:
        focus, ready_screen = read_screen(device)
        if page_kind(focus, ready_screen) != "resume":
            raise StopRun("等待简历正文加载时离开了简历页")
        if resume_identity(ready_screen) != name:
            raise StopRun("等待简历正文加载时候选人身份发生漂移")
        if resume_content_ready(ready_screen):
            logger.event("resume_content_ready", display_name=name)
            break
        time.sleep(0.8)
    else:
        slug = safe_name(name)
        timeout_evidence: list[dict[str, str]] = []
        if logger.should_capture(critical=True):
            xml_path = logger.save_evidence(
                f"resume_loading_timeout_{slug}.xml", device.dump_ui()
            )
            png_path = logger.save_evidence(
                f"resume_loading_timeout_{slug}.png", device.screenshot()
            )
            timeout_evidence = [evidence_entry(xml_path), evidence_entry(png_path)]
        logger.event(
            "resume_loading_timeout",
            display_name=name,
            evidence=timeout_evidence,
        )
        raise StopRun("简历正文60秒内未脱离加载骨架，拒绝采集空快照")

    stitcher = ResumeStitcher()
    evidence: list[dict[str, str]] = []
    source_activity = ""
    slug = safe_name(name)
    previous_signature: tuple[tuple[str, str], ...] | None = None
    lines = 0
    scrolls = 0
    reached_bottom = False
    for step in range(max_scrolls + 1):
        focus, screen = read_screen(device)
        if page_kind(focus, screen) != "resume":
            break
        if not source_activity:
            source_activity = focus
        screen = expand_folded(device, logger, name, screen)
        if step == 0 and logger.should_capture():
            xml_path = logger.save_evidence(
                f"resume_top_{slug}.xml", device.dump_ui()
            )
            evidence.append(evidence_entry(xml_path))
            png_path = logger.save_evidence(
                f"resume_top_{slug}.png", device.screenshot()
            )
            evidence.append(evidence_entry(png_path))
        signature = viewport_signature(screen)
        stitcher.feed(screen)
        lines = stitcher.collected_line_count()
        if stitcher.reached_stop:
            logger.event("resume_reached_stop_section", section="牛人分析器")
            break
        if previous_signature is not None and signature == previous_signature:
            reached_bottom = True
            logger.event("resume_reached_bottom", scrolls=scrolls)
            if logger.should_capture():
                png_path = logger.save_evidence(
                    f"resume_bottom_{slug}.png", device.screenshot()
                )
                evidence.append(evidence_entry(png_path))
            break
        previous_signature = signature
        if step == max_scrolls:
            logger.event("resume_scroll_cap", cap=max_scrolls)
            break
        scroll_down(device, logger)
        scrolls += 1
    record = stitcher.to_record()
    logger.event(
        "resume_collected",
        display_name=record.get("display_name"),
        lines=lines,
        scrolls=scrolls,
        reached_bottom=reached_bottom,
        sections_seen=stitcher.sections_seen,
        source_activity=source_activity,
    )
    return record, source_activity, evidence
