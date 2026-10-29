"""页面识别、简历字段抽取与多屏拼接。

设计依据：docs/公共-求职者界面侦察文档-v0.3.md
- 简历页是异构 RecyclerView，同一 resource-id 会在不同区块重复出现，
  因此字段必须按「区块标题 + 区块内相对位置」取值，不能按 id 全局取值。
- 采集范围截止于「牛人分析器」；该区块及其之后的内容不采集。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .device import Bounds, parse_bounds

# 简历正文存在三种折叠形状：头部「个人优势」尾部的「查看全部」、区块内
# 「展开全部N个工作经历」这类条目折叠、作品区块尾部的「展开」。真机样本
# 显示 292/340 份简历的 summary 处于折叠态，技能表述可能整体落在折叠段里，
# 因此采集前必须逐屏展开，否则「简历中能否看到某技能」的判断基于残缺文本。
FOLD_MARKER_RE = re.compile(
    r"(查看全部|查看更多|展开更多|展开全部\d{1,3}个[^\s]{0,12}|展开)\s*$"
)

AGE_RE = re.compile(r"(\d+)")
RESUME_ACTIVITY = "BossViewGeekResumePagerActivity"
CHAT_ACTIVITY = "ChatRoomActivity"

# 简历头部字段：这些 id 在头部不与其他区块冲突，可直接按 id 取值
HEADER_FIELD_BY_ID = {
    "tv_geek_name": "display_name",
    "tv_job_and_com": "headline_subtitle",
    "tv_geek_work_status": "work_status",
    "tv_active_status": "active_status",
    "tv_geek_work_year": "work_years",
    "tv_geek_degree": "degree",
    "tv_geek_age": "age",
    "tv_geek_description": "summary",
}
TAG_IDS = {"tv_name", "tv_title"}

EXPECTATION_SECTIONS = {"求职期望", "最近关注"}
EXPECTATION_FIELD_BY_ID = {
    "tv_job_and_city": "job_and_city",
    "tv_salary": "salary",
    "tv_industry": "industry",
}

# 长文本区块：原样保存为 TEXT，不解析内部结构
SECTION_TO_FIELD = {
    "工作经历": "work_experience_text",
    "项目经历": "project_experience_text",
    "教育经历": "education_text",
    "社团/组织经历": "club_experience_text",
    "专业技能": "skills_text",
    "所获荣誉": "honors_text",
    "实习经历": "internship_text",
    "资格证书": "certificate_text",
    "图片作品": "portfolio_image_text",
    "视频作品": "portfolio_video_text",
    "培训经历": "training_text",
    "志愿服务经历": "volunteer_text",
}
STOP_SECTION = "牛人分析器"

# 风控/验证码关键词。命中即认为平台已触发验证或风控，程序必须熔断停机、等人工接管。
# 注意：这里只做「识别 + 停机」，绝不尝试破解或自动通过验证。
RISK_KEYWORDS = (
    "安全验证", "请完成验证", "滑动验证", "拖动滑块", "向右滑动", "滑块验证",
    "验证码", "图形验证", "人机验证", "请输入验证",
    "操作频繁", "操作过于频繁", "访问异常", "请求异常", "账号异常",
    "系统繁忙", "网络繁忙", "服务繁忙", "请稍后再试",
)


@dataclass
class UiNode:
    resource_id: str
    text: str
    cls: str
    clickable: bool
    bounds: Bounds | None
    element: ET.Element

    @property
    def short_id(self) -> str:
        return self.resource_id.split("/")[-1]


class Screen:
    """一次控件树 dump 的结构化视图。"""

    def __init__(self, xml_bytes: bytes) -> None:
        self.root = ET.fromstring(xml_bytes)
        self._parents = {child: parent for parent in self.root.iter() for child in parent}

    def _element_by_id(self, short_id: str) -> ET.Element | None:
        for element in self.root.iter("node"):
            if element.attrib.get("resource-id", "").endswith(short_id):
                return element
        return None

    def nodes(self, under_id: str | None = None) -> list[UiNode]:
        start = self._element_by_id(under_id) if under_id else self.root
        if start is None:
            return []
        result: list[UiNode] = []
        for element in start.iter("node"):
            raw_bounds = element.attrib.get("bounds", "")
            try:
                bounds = parse_bounds(raw_bounds)
            except Exception:
                bounds = None
            result.append(
                UiNode(
                    resource_id=element.attrib.get("resource-id", ""),
                    text=element.attrib.get("text", ""),
                    cls=element.attrib.get("class", ""),
                    clickable=element.attrib.get("clickable") == "true",
                    bounds=bounds,
                    element=element,
                )
            )
        return result

    def clickable_center(self, element: ET.Element) -> tuple[int, int] | None:
        ancestor = self.clickable_ancestor(element)
        if ancestor is None:
            return None
        try:
            return parse_bounds(ancestor.attrib.get("bounds", "")).center
        except Exception:
            return None

    def clickable_ancestor(self, element: ET.Element) -> ET.Element | None:
        """返回节点自身或最近的可点击祖先，供页面适配器绑定同一卡片。"""
        current: ET.Element | None = element
        while current is not None:
            if current.attrib.get("clickable") == "true":
                return current
            current = self._parents.get(current)
        return None


def risk_signal(screen: Screen) -> str | None:
    """扫描当前页可见文本，命中风控/验证码关键词时返回该关键词，否则 None。

    同时扫描 text 与 content-desc 两类属性，避免漏掉 WebView 型验证页。
    """
    for node in screen.nodes():
        for raw in (node.text, node.element.attrib.get("content-desc", "")):
            text = raw.strip()
            if not text:
                continue
            for keyword in RISK_KEYWORDS:
                if keyword in text:
                    return keyword
    return None


# 页面识别器：供 screen_ops 的注册表使用（register_page_kind）。
# 判定顺序即优先级，与历史实现的 if/elif 一致：chat → resume → list。
def detect_chat(focus: str, screen: Screen) -> bool:
    """聊天 / 会话页。"""
    ids = {node.short_id for node in screen.nodes()}
    return CHAT_ACTIVITY in focus or bool({"mMsgTvStatus", "tv_content_text"} & ids)


def detect_resume(focus: str, screen: Screen) -> bool:
    """求职者简历页。"""
    ids = {node.short_id for node in screen.nodes()}
    return RESUME_ACTIVITY in focus or "cl_bottom_actions" in ids


def detect_geek_list(focus: str, screen: Screen) -> bool:
    """牛人列表页。"""
    ids = {node.short_id for node in screen.nodes()}
    return "cl_geek_card" in ids or "tv_geek_name" in ids


def bottom_action(
    screen: Screen, *, container_id: str = "cl_bottom_actions"
) -> tuple[str, tuple[int, int]] | None:
    """页面底部固定动作栏：(按钮文本, 点击落点)。

    `container_id` 参数化：除简历页底部栏外，聊天页发送栏等其它底部栏亦可复用。
    """
    for node in screen.nodes(under_id=container_id):
        if node.text.strip() and node.bounds is not None:
            return node.text.strip(), node.bounds.center
    return None


FIRST_CONTACT_ACTIONS = ("立即沟通",)
"""允许发起首次招呼的底部按钮文案。

「立即联系牛人」不在此列：人工核验确认它是平台付费广告入口，走这条路径联系候选人
需要额外付费，不属于本项目的招呼动作。其余非「立即沟通」文案（如「继续沟通」
「已标记为不合适」）同样拦截。
"""

PAID_CONTACT_ACTION = "立即联系牛人"
"""付费广告入口文案：命中即跳过，不发送，也不记为已有会话。"""


def is_first_contact_action(label: str | None) -> bool:
    """按钮文案是否代表「可发起免费首次招呼」。"""
    return label in FIRST_CONTACT_ACTIONS


def is_paid_contact_action(label: str | None) -> bool:
    """按钮文案是否为平台付费广告入口。"""
    return label == PAID_CONTACT_ACTION


def resume_identity(screen: Screen) -> str | None:
    """简历页上可读的候选人姓名（顶栏优先，其次内容头）。

    用于原地发送前确认「当前页 == 目标候选人」这一最后一道闸。
    """
    for node in screen.nodes(under_id="cl_title"):
        if node.short_id == "tv_title" and node.text.strip():
            return node.text.strip()
    for node in screen.nodes(under_id="rv_list"):
        if node.short_id == "tv_geek_name" and node.text.strip():
            return node.text.strip()
    return None


def resume_content_ready(screen: Screen) -> bool:
    """正文已脱离加载骨架，至少出现姓名以外的一项真实简历字段。"""
    for node in screen.nodes(under_id="rv_list"):
        text = node.text.strip()
        if not text:
            continue
        if node.short_id == "tv_section_title":
            return True
        if node.short_id in HEADER_FIELD_BY_ID and node.short_id != "tv_geek_name":
            return True
        if node.short_id in EXPECTATION_FIELD_BY_ID:
            return True
    return False


@dataclass(frozen=True)
class FoldTarget:
    """一处折叠控件及其候选展开落点（按探测顺序排列）。"""

    marker: str
    points: tuple[tuple[int, int], ...]


def fold_targets(screen: Screen, *, container_id: str = "rv_list") -> list[FoldTarget]:
    """当前屏中仍处于折叠状态的展开目标，按探测顺序给出落点。

    两种形状的取证不同：

    - 独立标记节点（如「展开全部4个工作经历」）整块可点，节点中心已验证生效；
    - 正文后缀标记（「…  查看全部」「…  展开」）由 TextView 内的 ClickableSpan
      处理，节点与祖先都不可点击。真机对照显示该 span 的横向位置随末行文字长度
      移动：同一条 `(right-70, bottom-20)` 在一份简历上把正文从 72 字展开到 232 字，
      在另一份简历上 120ms tap 与 200ms press 都无变化。因此沿末行从右向左做有界
      探测，由调用方在每次点击后校验可见形状是否变化，而不是假定固定内缩。
    """
    targets: list[FoldTarget] = []
    seen: set[tuple[int, int]] = set()
    for node in screen.nodes(under_id=container_id):
        text = node.text.strip()
        if not text or node.bounds is None:
            continue
        match = FOLD_MARKER_RE.search(text)
        if match is None:
            continue
        marker = match.group(1)
        bounds = node.bounds
        points: list[tuple[int, int]] = []
        clickable = screen.clickable_center(node.element)
        if clickable is not None:
            points.append(clickable)
        if text == marker:
            points.append(bounds.center)
        else:
            y = max(bounds.top + 10, bounds.bottom - 20)
            for raw_x in (
                bounds.right - 70,
                bounds.right - 200,
                (bounds.left + bounds.right) // 2,
                bounds.left + 150,
            ):
                points.append((max(bounds.left + 20, min(bounds.right - 20, raw_x)), y))
        ordered: list[tuple[int, int]] = []
        for point in points:
            if point not in ordered:
                ordered.append(point)
        if ordered[0] in seen:
            continue
        seen.add(ordered[0])
        targets.append(FoldTarget(marker=marker, points=tuple(ordered)))
    return targets


def list_cards(
    screen: Screen, *, container_id: str = "rv_list", name_id: str = "tv_geek_name"
) -> list[tuple[str, tuple[int, int]]]:
    """列表页当前屏可见的卡片：(名称, 点击落点)。

    落点取名称节点自身中心：实机验证卡片中心落点无效（BOSS 不响应），
    姓名中心可稳定进入求职者页。
    `container_id` / `name_id` 参数化后，其它列表页（如「新招呼」）可复用同一解析器。
    """
    cards: list[tuple[str, tuple[int, int]]] = []
    for node in screen.nodes(under_id=container_id):
        if node.short_id != name_id or not node.text.strip():
            continue
        if node.bounds is None:
            continue
        cards.append((node.text.strip(), node.bounds.center))
    return cards


def viewport_signature(
    screen: Screen, *, container_id: str = "rv_list"
) -> tuple[tuple[str, str], ...]:
    """当前屏正文的可见文本签名，用于判断滑动后视图是否已变化（触底）。

    `container_id` 参数化后，其它可滚动列表（如「新招呼」）亦可复用。
    """
    return tuple(
        (node.short_id, node.text.strip())
        for node in screen.nodes(under_id=container_id)
        if node.text.strip()
    )


class ResumeStitcher:
    """把一位候选人简历的连续多屏 dump 合并成一条记录。"""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.expectation: dict[str, str] = {}
        self.tags: list[str] = []
        self.sections: dict[str, list[str]] = {}
        self.sections_seen: list[str] = []
        self.reached_stop = False
        self.source_activity = ""
        self._current_section: str | None = None
        self._seen_lines: set[tuple[str, str]] = set()

    def feed(self, screen: Screen) -> None:
        if self.reached_stop:
            return
        for node in screen.nodes(under_id="rv_list"):
            short = node.short_id
            text = node.text.strip()

            if short == "tv_section_title" and text:
                if text not in self.sections_seen:
                    self.sections_seen.append(text)
                if text == STOP_SECTION:
                    self.reached_stop = True
                    return
                self._current_section = text
                self.sections.setdefault(text, [])
                continue

            if not text:
                continue

            if self._current_section is None:
                self._feed_header(node, short, text)
            elif self._current_section in EXPECTATION_SECTIONS:
                if short in EXPECTATION_FIELD_BY_ID:
                    self.expectation[EXPECTATION_FIELD_BY_ID[short]] = text
            else:
                self._append_line(self._current_section, text)

    def _feed_header(self, node: UiNode, short: str, text: str) -> None:
        if short in HEADER_FIELD_BY_ID:
            self.fields[HEADER_FIELD_BY_ID[short]] = text
        elif short in TAG_IDS:
            self._add_tag(text)

    def _add_tag(self, text: str) -> None:
        if text and text not in self.tags and text != self.fields.get("display_name"):
            self.tags.append(text)

    def _append_line(self, section: str, text: str) -> None:
        key = (section, text)
        if key in self._seen_lines:
            return
        self._seen_lines.add(key)
        self.sections[section].append(text)

    def collected_line_count(self) -> int:
        return len(self._seen_lines)

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {}
        record.update(self.fields)
        age_text = self.fields.get("age", "")
        age_match = AGE_RE.search(age_text)
        record["age"] = int(age_match.group(1)) if age_match else None

        job_and_city = self.expectation.get("job_and_city", "")
        if "，" in job_and_city:
            role, city = job_and_city.split("，", 1)
            record["expect_role"] = role.strip()
            record["expect_city"] = city.strip()
        elif job_and_city:
            record["expect_role"] = job_and_city.strip()
            record["expect_city"] = None
        else:
            record["expect_role"] = None
            record["expect_city"] = None
        record["expect_salary"] = self.expectation.get("salary") or None
        industry = self.expectation.get("industry", "")
        record["expect_industry"] = (
            industry.removeprefix("期望行业：").strip()
            if industry.startswith("期望行业：")
            else None
        )
        record["tags"] = " / ".join(self.tags) or None

        for section, column in SECTION_TO_FIELD.items():
            lines = self.sections.get(section, [])
            record[column] = "\n".join(lines) or None
        return record
