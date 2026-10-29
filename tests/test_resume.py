from __future__ import annotations

import unittest

from app.resume import (
    Screen,
    fold_targets,
    is_first_contact_action,
    is_paid_contact_action,
    resume_content_ready,
)


def _screen(body: str) -> Screen:
    return Screen(
        (
            '<hierarchy><node resource-id="com.hpbr.bosszhipin:id/rv_list" '
            'class="androidx.recyclerview.widget.RecyclerView" bounds="[0,0][720,1500]">'
            + body
            + "</node></hierarchy>"
        ).encode("utf-8")
    )


class ResumeReadinessTests(unittest.TestCase):
    def test_name_only_skeleton_is_not_ready(self) -> None:
        screen = _screen(
            '<node text="脱敏候选人" resource-id="com.hpbr.bosszhipin:id/tv_geek_name" '
            'class="android.widget.TextView" bounds="[0,0][200,50]" />'
            '<node text="" resource-id="com.hpbr.bosszhipin:id/view_desc_1" '
            'class="android.view.View" bounds="[0,60][300,100]" />'
        )
        self.assertFalse(resume_content_ready(screen))

    def test_real_header_field_marks_resume_ready(self) -> None:
        screen = _screen(
            '<node text="1年" resource-id="com.hpbr.bosszhipin:id/tv_geek_work_year" '
            'class="android.widget.TextView" bounds="[0,0][100,50]" />'
        )
        self.assertTrue(resume_content_ready(screen))


class FoldTargetTests(unittest.TestCase):
    """折叠标记形状与落点顺序均取自真机样本。"""

    def test_unclickable_folded_description_probes_bottom_line(self) -> None:
        screen = _screen(
            '<node text="脱敏候选人优势前半段...  查看全部" '
            'resource-id="com.hpbr.bosszhipin:id/tv_geek_description" '
            'class="android.widget.TextView" bounds="[40,300][680,420]" />'
        )
        targets = fold_targets(screen)
        self.assertEqual(1, len(targets))
        self.assertEqual("查看全部", targets[0].marker)
        self.assertEqual(
            ((610, 400), (480, 400), (360, 400), (190, 400)), targets[0].points
        )

    def test_clickable_fold_node_is_probed_first(self) -> None:
        screen = _screen(
            '<node text="脱敏候选人优势前半段...  查看全部" '
            'resource-id="com.hpbr.bosszhipin:id/tv_geek_description" '
            'class="android.widget.TextView" clickable="true" '
            'bounds="[40,300][680,420]" />'
        )
        target = fold_targets(screen)[0]
        self.assertEqual((360, 360), target.points[0])

    def test_standalone_section_marker_uses_node_center(self) -> None:
        screen = _screen(
            '<node text="展开全部4个工作经历" '
            'resource-id="com.hpbr.bosszhipin:id/tv_expand" '
            'class="android.widget.TextView" bounds="[40,700][300,760]" />'
        )
        target = fold_targets(screen)[0]
        self.assertEqual("展开全部4个工作经历", target.marker)
        self.assertEqual(((170, 730),), target.points)

    def test_portfolio_plain_expand_suffix_is_found(self) -> None:
        screen = _screen(
            '<node text="脱敏作品说明，时长在10分钟-20分钟不等，...  展开" '
            'resource-id="com.hpbr.bosszhipin:id/tv_work_desc" '
            'class="android.widget.TextView" bounds="[40,900][680,980]" />'
        )
        target = fold_targets(screen)[0]
        self.assertEqual("展开", target.marker)
        self.assertEqual(960, target.points[0][1])

    def test_full_text_without_marker_returns_nothing(self) -> None:
        screen = _screen(
            '<node text="脱敏候选人完整个人优势全文，含PS与剪映。" '
            'resource-id="com.hpbr.bosszhipin:id/tv_geek_description" '
            'class="android.widget.TextView" bounds="[40,300][680,420]" />'
        )
        self.assertEqual([], fold_targets(screen))

    def test_marker_word_mid_sentence_is_not_a_control(self) -> None:
        screen = _screen(
            '<node text="负责活动展开说明与物料归档工作" '
            'resource-id="com.hpbr.bosszhipin:id/tv_section_content" '
            'class="android.widget.TextView" bounds="[40,300][680,420]" />'
        )
        self.assertEqual([], fold_targets(screen))


class FirstContactActionTests(unittest.TestCase):
    """底部按钮文案闸门：只有「立即沟通」可发；「立即联系牛人」是付费广告入口。"""

    def test_only_free_greeting_label_allows_send(self) -> None:
        self.assertTrue(is_first_contact_action("立即沟通"))
        for label in ("立即联系牛人", "继续沟通", "已标记为不合适（点击取消）", "", None):
            self.assertFalse(is_first_contact_action(label), label)

    def test_paid_contact_label_is_classified_as_ad_entry(self) -> None:
        self.assertTrue(is_paid_contact_action("立即联系牛人"))
        self.assertFalse(is_paid_contact_action("立即沟通"))
        self.assertFalse(is_paid_contact_action("继续沟通"))


if __name__ == "__main__":
    unittest.main()
