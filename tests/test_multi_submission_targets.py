"""Tests for administrator selection among multiple submission destinations."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from channel import publish_setting


class SubmissionTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {
            "channel_id": -100111,
            "submission_targets": [
                {"id": 1, "name": "主频道", "channel_id": -100111, "enabled": True},
                {"id": 2, "name": "备用投稿频道", "channel_id": -100222, "enabled": True},
            ],
        }

    def test_targets_are_normalized_and_selection_is_session_scoped(self):
        targets = publish_setting._submission_targets(self.config)
        self.assertEqual([target["id"] for target in targets], [1, 2])

        context = SimpleNamespace(user_data={"selected_submission_target_id": 2})
        selected = publish_setting._selected_submission_target(self.config, context)
        self.assertEqual(selected["channel_id"], -100222)

    def test_target_manager_returns_to_the_top_level_publish_config_selector(self):
        keyboard = publish_setting._submission_targets_keyboard(self.config)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("publish:publishset", callbacks)
        self.assertNotIn("publish:channel", callbacks)

    def test_target_selection_keyboard_contains_each_enabled_channel(self):
        keyboard = publish_setting._submission_target_selection_keyboard(
            publish_setting._submission_targets(self.config)
        )
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("publish:submission_target_choose:1", callbacks)
        self.assertIn("publish:submission_target_choose:2", callbacks)

    def test_bot_publish_rule_keeps_only_configured_field_lines(self):
        config = {
            "bot_publish_rule_enabled": True,
            "bot_publish_rule_fields": ["名字", "区域", "电报号"],
        }
        result = publish_setting._apply_bot_publish_rule(
            "介绍：忽略\n名字: 小雅\n区域：浦东\n电报号: @xiaoya\n价格：忽略",
            config,
        )
        self.assertEqual(result, "名字：小雅\n区域：浦东\n电报号：@xiaoya")

    def test_disabled_bot_publish_rule_does_not_change_content(self):
        self.assertIsNone(
            publish_setting._apply_bot_publish_rule(
                "名字：小雅", {"bot_publish_rule_enabled": False, "bot_publish_rule_fields": ["名字"]}
            )
        )

    async def test_admin_is_prompted_to_choose_target_before_submission(self):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(user_data={})

        with patch.object(publish_setting, "_is_submission_administrator", return_value=True):
            await publish_setting._start_publish_flow(update, context, self.config)

        query.edit_message_text.assert_awaited_once()
        kwargs = query.edit_message_text.await_args.kwargs
        self.assertIn("请选择本次投稿", query.edit_message_text.await_args.args[0])
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("publish:submission_target_choose:1", callbacks)
        self.assertNotIn("selected_submission_target_id", context.user_data)

    def test_continuous_submission_keeps_choice_but_one_shot_clears_it(self):
        context = SimpleNamespace(user_data={
            "waiting_post": True,
            "selected_submission_target_id": 2,
        })
        publish_setting._finish_submission_if_needed(
            context, {"continuous_submission_enabled": True}
        )
        self.assertEqual(context.user_data["selected_submission_target_id"], 2)

        publish_setting._finish_submission_if_needed(
            context, {"continuous_submission_enabled": False}
        )
        self.assertNotIn("selected_submission_target_id", context.user_data)


if __name__ == "__main__":
    unittest.main()
