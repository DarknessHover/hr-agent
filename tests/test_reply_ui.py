from __future__ import annotations

import unittest

from app.reply_ui import (
    all_new_greeting_cards,
    chat_snapshot,
    common_phrase_rows,
    delivered_outbound_message_texts,
    detect_new_greetings,
    latest_directional_text_message,
    new_greeting_cards,
    outbound_message_texts,
)
from app.resume import Screen


XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<hierarchy rotation="0">
  <node text="聊天" resource-id="" class="android.widget.TextView" bounds="[0,0][100,40]" />
  <node text="新招呼" resource-id="com.hpbr.bosszhipin:id/tv_title" class="android.widget.TextView" bounds="[0,40][100,80]" />
  <node text="推荐顺序" resource-id="com.hpbr.bosszhipin:id/tv_recommend_sort" class="android.widget.TextView" bounds="[0,80][100,120]" />
  <node text="时间顺序" resource-id="com.hpbr.bosszhipin:id/tv_time_sort" class="android.widget.TextView" bounds="[100,80][200,120]" />
  <node text="" resource-id="com.hpbr.bosszhipin:id/contact_vp_root" class="android.view.ViewGroup" bounds="[0,0][720,1600]">
    <node text="" resource-id="com.hpbr.bosszhipin:id/recyclerView" class="androidx.recyclerview.widget.RecyclerView" bounds="[0,120][720,1500]">
      <node text="" resource-id="" class="android.view.ViewGroup" clickable="true" bounds="[0,120][720,260]">
        <node text="" resource-id="com.hpbr.bosszhipin:id/badge_view" class="android.widget.FrameLayout" bounds="[20,130][100,210]">
          <node text="2" resource-id="" class="android.widget.TextView" bounds="[70,130][100,160]" />
        </node>
        <node text="脱敏候选人" resource-id="com.hpbr.bosszhipin:id/tv_name" class="android.widget.TextView" bounds="[120,130][260,170]" />
        <node text="公司-职位" resource-id="com.hpbr.bosszhipin:id/tv_position" class="android.widget.TextView" bounds="[260,130][500,170]" />
        <node text="本科 | 6-7K" resource-id="com.hpbr.bosszhipin:id/tv_time" class="android.widget.TextView" bounds="[500,130][700,170]" />
        <node text="脱敏消息" resource-id="com.hpbr.bosszhipin:id/tv_msg" class="android.widget.TextView" bounds="[120,180][700,220]" />
      </node>
    </node>
  </node>
</hierarchy>'''.encode("utf-8")


CHAT_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<hierarchy rotation="0">
  <node resource-id="com.hpbr.bosszhipin:id/cl_title" class="android.view.ViewGroup" bounds="[0,0][720,100]">
    <node text="脱敏候选人" resource-id="com.hpbr.bosszhipin:id/tv_title" class="android.widget.TextView" bounds="[300,10][420,50]" />
    <node text="脱敏岗位" resource-id="com.hpbr.bosszhipin:id/tv_sub_title" class="android.widget.TextView" bounds="[300,50][420,90]" />
  </node>
  <node resource-id="com.hpbr.bosszhipin:id/mRecycleView" class="androidx.recyclerview.widget.RecyclerView" bounds="[0,100][720,1400]">
    <node text="04:45" resource-id="com.hpbr.bosszhipin:id/tv_text" class="android.widget.TextView" bounds="[300,120][420,160]" />
    <node text="10月21日 沟通的职位-脱敏岗位" resource-id="com.hpbr.bosszhipin:id/mBottomPositionView" class="android.widget.TextView" bounds="[20,200][700,240]" />
    <node class="android.widget.LinearLayout" clickable="true" bounds="[0,300][720,450]">
      <node class="android.widget.RelativeLayout" bounds="[20,300][620,430]">
        <node resource-id="com.hpbr.bosszhipin:id/iv_avatar" class="android.widget.ImageView" clickable="true" bounds="[24,350][88,414]" />
        <node text="脱敏入站消息" resource-id="com.hpbr.bosszhipin:id/tv_content_text" class="android.widget.TextView" bounds="[104,300][616,430]" />
      </node>
    </node>
    <node text="09:46" resource-id="com.hpbr.bosszhipin:id/tv_text" class="android.widget.TextView" bounds="[300,460][420,500]" />
    <node class="android.widget.LinearLayout" clickable="true" bounds="[0,510][720,660]">
      <node class="android.widget.RelativeLayout" bounds="[100,510][720,640]">
        <node text="送达" resource-id="com.hpbr.bosszhipin:id/mMsgTvStatus" class="android.widget.TextView" bounds="[120,600][170,630]" />
        <node text="脱敏出站消息" resource-id="com.hpbr.bosszhipin:id/tv_content_text" class="android.widget.TextView" bounds="[104,510][616,640]" />
        <node resource-id="com.hpbr.bosszhipin:id/iv_avatar" class="android.widget.ImageView" clickable="true" bounds="[632,560][696,624]" />
      </node>
    </node>
  </node>
  <node resource-id="com.hpbr.bosszhipin:id/recyclerView" class="androidx.recyclerview.widget.RecyclerView" bounds="[0,1100][720,1500]">
    <node class="android.widget.LinearLayout" bounds="[0,1100][720,1200]">
      <node text="方便发一份你的简历过来吗？" resource-id="com.hpbr.bosszhipin:id/mContentText" class="android.widget.TextView" bounds="[32,1120][588,1180]" />
      <node text="发送" resource-id="com.hpbr.bosszhipin:id/btn_send" class="android.widget.TextView" clickable="true" bounds="[588,1120][688,1180]" />
    </node>
  </node>
</hierarchy>'''.encode("utf-8")


class ReplyUiTests(unittest.TestCase):
    def test_detects_and_binds_unread_card(self) -> None:
        screen = Screen(XML)
        self.assertTrue(detect_new_greetings("MainActivity", screen))
        cards = new_greeting_cards(screen)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].display_name, "脱敏候选人")
        self.assertEqual(cards[0].unread_count, 2)
        self.assertEqual(cards[0].preview_text, "脱敏消息")
        self.assertEqual(cards[0].point, (190, 150))

    def test_detects_session_list_without_legacy_sort_controls(self) -> None:
        xml = b"\n".join(
            line
            for line in XML.splitlines()
            if b"tv_recommend_sort" not in line and b"tv_time_sort" not in line
        )
        self.assertTrue(detect_new_greetings("MainActivity", Screen(xml)))

    def test_message_hub_without_cards_is_not_session_list(self) -> None:
        xml = b"\n".join(
            line for line in XML.splitlines() if b"tv_name" not in line
        )
        self.assertFalse(detect_new_greetings("MainActivity", Screen(xml)))

    def test_explicit_target_can_bind_card_without_unread_badge(self) -> None:
        xml = XML.replace(
            b'<node text="" resource-id="com.hpbr.bosszhipin:id/badge_view" class="android.widget.FrameLayout" bounds="[20,130][100,210]">\n          <node text="2" resource-id="" class="android.widget.TextView" bounds="[70,130][100,160]" />\n        </node>\n        ',
            b"",
        )
        screen = Screen(xml)
        self.assertEqual(new_greeting_cards(screen), [])
        cards = all_new_greeting_cards(screen)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].display_name, "脱敏候选人")
        self.assertEqual(cards[0].unread_count, 0)
        self.assertEqual(cards[0].unread_signal, "no_badge")

    def test_conversation_fingerprint_ignores_mutable_meta_text(self) -> None:
        unread = new_greeting_cards(Screen(XML))[0]
        enriched_xml = XML.replace(b"\xe6\x9c\xac\xe7\xa7\x91 | 6-7K", b"\xe5\xa4\xa7\xe4\xb8\x93 | 6-7K")
        enriched = new_greeting_cards(Screen(enriched_xml))[0]
        self.assertNotEqual(unread.meta_text, enriched.meta_text)
        self.assertEqual(
            unread.conversation_fingerprint,
            enriched.conversation_fingerprint,
        )

    def test_parses_chat_identity_and_left_avatar_inbound_message(self) -> None:
        snapshot = chat_snapshot(Screen(CHAT_XML))
        self.assertEqual(snapshot.display_name, "脱敏候选人")
        self.assertEqual(snapshot.subtitle, "脱敏岗位")
        self.assertEqual(snapshot.latest_inbound_text, "脱敏入站消息")
        self.assertEqual(snapshot.visible_time_text, "10月21日 04:45")
        self.assertEqual(snapshot.avatar_point, (56, 382))

    def test_binds_common_phrase_to_send_button_and_reads_outbound(self) -> None:
        screen = Screen(CHAT_XML)
        rows = common_phrase_rows(screen)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].phrase_text, "方便发一份你的简历过来吗？")
        self.assertEqual(rows[0].send_point, (638, 1150))
        self.assertEqual(outbound_message_texts(screen), ["脱敏出站消息"])
        self.assertEqual(delivered_outbound_message_texts(screen), ["脱敏出站消息"])

    def test_latest_directional_message_uses_bottom_message_direction(self) -> None:
        latest = latest_directional_text_message(Screen(CHAT_XML))
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.direction, "outbound")
        self.assertEqual(latest.text, "脱敏出站消息")

        newer_inbound = b'''\n    <node class="android.widget.LinearLayout" clickable="true" bounds="[0,700][720,850]">
      <node class="android.widget.RelativeLayout" bounds="[20,700][620,830]">
        <node resource-id="com.hpbr.bosszhipin:id/iv_avatar" class="android.widget.ImageView" clickable="true" bounds="[24,750][88,814]" />
        <node text="new inbound" resource-id="com.hpbr.bosszhipin:id/tv_content_text" class="android.widget.TextView" bounds="[104,700][616,830]" />
      </node>
    </node>'''
        marker = b'\n  </node>\n  <node resource-id="com.hpbr.bosszhipin:id/recyclerView"'
        screen = Screen(CHAT_XML.replace(marker, newer_inbound + marker, 1))
        latest = latest_directional_text_message(screen)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.direction, "inbound")
        self.assertEqual(latest.text, "new inbound")


if __name__ == "__main__":
    unittest.main()
