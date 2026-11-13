"""Tests for configurable advertising URL buttons."""
import unittest
from types import SimpleNamespace

from chat import my_bot
from group import group_setting as settings


class AdPushButtonsTests(unittest.TestCase):
    def test_normalization_allows_twenty_and_keeps_style_and_member_emoji(self):
        raw = [
            {
                "text": f"按钮 {index}",
                "url": f"https://example.com/{index}",
                "style": "PRIMARY" if index == 1 else "invalid",
                "icon_custom_emoji_id": "5203996991054432397" if index == 1 else "not-an-id",
            }
            for index in range(1, settings.MAX_AD_BUTTONS + 2)
        ]

        buttons = settings._normalize_ad_buttons(raw)

        self.assertEqual(len(buttons), settings.MAX_AD_BUTTONS)
        self.assertEqual(buttons[0]["style"], "primary")
        self.assertEqual(buttons[0]["icon_custom_emoji_id"], "5203996991054432397")
        self.assertNotIn("style", buttons[1])
        self.assertNotIn("icon_custom_emoji_id", buttons[1])

    def test_sent_markup_passes_color_and_member_emoji_to_bot_api(self):
        markup = settings._global_ad_reply_markup({
            "buttons": [{
                "text": "联系客服",
                "url": "https://t.me/example",
                "style": "success",
                "icon_custom_emoji_id": "5203996991054432397",
            }]
        })

        button = markup.inline_keyboard[0][0]
        rendered = button.to_dict()
        self.assertEqual(rendered["style"], "success")
        self.assertEqual(rendered["icon_custom_emoji_id"], "5203996991054432397")

    def test_scheduled_sender_preserves_color_and_member_emoji(self):
        markup = my_bot._ad_reply_markup({
            "buttons": [{
                "text": "联系客服",
                "url": "https://t.me/example",
                "style": "danger",
                "icon_custom_emoji_id": "5203996991054432397",
            }]
        })

        rendered = markup.inline_keyboard[0][0].to_dict()
        self.assertEqual(rendered["style"], "danger")
        self.assertEqual(rendered["icon_custom_emoji_id"], "5203996991054432397")

    def test_scheduled_sender_allows_the_configured_button_limit(self):
        markup = my_bot._ad_reply_markup({
            "buttons": [
                {"text": f"按钮 {index}", "url": f"https://example.com/{index}"}
                for index in range(1, settings.MAX_AD_BUTTONS + 2)
            ]
        })

        self.assertEqual(len(markup.inline_keyboard), settings.MAX_AD_BUTTONS)

    def test_global_manager_has_individual_edit_and_delete_actions(self):
        ad = {
            "id": 7,
            "buttons": [{"text": "加入频道", "url": "https://t.me/example"}],
        }
        markup = settings._global_ad_button_manager_markup(ad)
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }

        self.assertIn("gcfg:global_ad_button_open:7:1", callbacks)
        self.assertIn("gcfg:global_ad_button_delete:7:1", callbacks)
        self.assertIn("gcfg:global_ad_button_add:7", callbacks)
        self.assertIn("gcfg:global_ad_button_bulk:7", callbacks)

    def test_group_detail_exposes_text_url_color_and_member_emoji_controls(self):
        ad = {
            "id": 2,
            "buttons": [{"text": "联系", "url": "https://t.me/example"}],
        }
        button = ad["buttons"][0]
        markup = settings._group_ad_button_detail_markup("-100123456", ad, 1, button)
        callbacks = {
            item.callback_data
            for row in markup.inline_keyboard
            for item in row
            if item.callback_data
        }

        self.assertIn("gcfg:ad_multi_button_field:-100123456:2:1:text", callbacks)
        self.assertIn("gcfg:ad_multi_button_field:-100123456:2:1:url", callbacks)
        self.assertIn("gcfg:ad_multi_button_field:-100123456:2:1:emoji", callbacks)
        self.assertIn("gcfg:ad_multi_button_style_menu:-100123456:2:1", callbacks)

    def test_custom_emoji_id_is_read_from_sent_member_emoji(self):
        message = SimpleNamespace(
            entities=[SimpleNamespace(type="custom_emoji", custom_emoji_id="5253944419870062295")],
            caption_entities=[],
        )
        self.assertEqual(
            settings._custom_emoji_id_from_message(message),
            "5253944419870062295",
        )


if __name__ == "__main__":
    unittest.main()
