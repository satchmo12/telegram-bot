"""Tests for the shared URL-button layout editor and renderer."""
import unittest

from inline_button_settings import (
    apply_per_row_layout,
    build_reply_markup,
    button_rows,
    move_button,
    normalize_buttons,
    parse_button_lines,
    set_button_row_mode,
    swap_buttons,
)
from channel import publish_setting
from group import group_setting as settings


class InlineButtonLayoutTests(unittest.TestCase):
    def setUp(self):
        self.buttons = [
            {"text": "第一", "url": "https://example.com/1"},
            {"text": "第二", "url": "https://example.com/2"},
            {"text": "第三", "url": "https://example.com/3"},
        ]

    def test_legacy_buttons_remain_one_per_row(self):
        rows = button_rows(self.buttons)
        self.assertEqual([[button[0] for button in row] for row in rows], [[1], [2], [3]])

    def test_per_row_preset_renders_multiple_buttons_on_one_row(self):
        self.assertTrue(apply_per_row_layout(self.buttons, 2))

        rows = button_rows(self.buttons)
        self.assertEqual([[item[0] for item in row] for row in rows], [[1, 2], [3]])
        markup = build_reply_markup(self.buttons)
        self.assertEqual([len(row) for row in markup.inline_keyboard], [2, 1])

    def test_button_can_join_and_leave_the_previous_row(self):
        self.assertTrue(set_button_row_mode(self.buttons, 2, same_row=True))
        self.assertEqual([[item[0] for item in row] for row in button_rows(self.buttons)], [[1, 2], [3]])

        self.assertTrue(set_button_row_mode(self.buttons, 2, same_row=False))
        self.assertEqual([[item[0] for item in row] for row in button_rows(self.buttons)], [[1], [2], [3]])

    def test_swap_and_adjacent_move_change_button_order(self):
        self.assertTrue(swap_buttons(self.buttons, 1, 3))
        self.assertEqual([button["text"] for button in self.buttons], ["第三", "第二", "第一"])

        self.assertEqual(move_button(self.buttons, 3, "previous"), 2)
        self.assertEqual([button["text"] for button in self.buttons], ["第三", "第一", "第二"])

    def test_reordering_keeps_row_boundaries_at_their_positions(self):
        apply_per_row_layout(self.buttons, 2)
        self.assertTrue(swap_buttons(self.buttons, 1, 3))

        self.assertEqual([button["text"] for button in self.buttons], ["第三", "第二", "第一"])
        self.assertEqual(
            [[item[0] for item in row] for row in button_rows(self.buttons)],
            [[1, 2], [3]],
        )

    def test_bulk_text_allows_same_row_marker(self):
        buttons = parse_button_lines(
            "第一 | https://example.com/1\n"
            "第二 | https://example.com/2 | 同行\n"
            "第三 | tg://user?id=1"
        )
        self.assertEqual([[item[0] for item in row] for row in button_rows(buttons)], [[1, 2], [3]])

    def test_publish_button_settings_use_the_same_layout_editor_and_renderer(self):
        config = {"buttons": self.buttons, "bottom_buttons_enabled": True}
        callbacks = {
            button.callback_data
            for row in publish_setting.publish_button_settings_keyboard(config).inline_keyboard
            for button in row
        }
        self.assertIn("publish:button_layout:buttons", callbacks)

        apply_per_row_layout(config["buttons"], 2)
        markup = publish_setting.publish_buttons_keyboard(config)
        self.assertEqual([len(row) for row in markup.inline_keyboard], [2, 1])

    def test_ad_manager_exposes_layout_actions_for_global_and_group_ads(self):
        ad = {"id": 7, "buttons": self.buttons}
        global_callbacks = {
            button.callback_data
            for row in settings._global_ad_button_manager_markup(ad).inline_keyboard
            for button in row
        }
        group_callbacks = {
            button.callback_data
            for row in settings._group_ad_button_manager_markup("-100123456", ad).inline_keyboard
            for button in row
        }

        self.assertIn("gcfg:global_ad_button_layout:7", global_callbacks)
        self.assertIn("gcfg:ad_multi_button_layout:-100123456:7", group_callbacks)


if __name__ == "__main__":
    unittest.main()
