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

    async def test_profile_child_back_returns_to_that_profile_settings_menu(self):
        root_config = {
            "channel_id": -100111,
            "submission_targets": [
                {"id": 2, "name": "备用投稿频道", "channel_id": -100222, "enabled": True},
            ],
        }
        profile_config = {"channel_id": -100222}
        query = SimpleNamespace(
            data="publish:back",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            user_data={publish_setting.PUBLISH_SETTINGS_PROFILE_KEY: 2}
        )

        with (
            patch.object(publish_setting, "load_publish_config", return_value=root_config),
            patch.object(publish_setting, "load_publish_profile", return_value=profile_config),
        ):
            await publish_setting._handle_callback(
                SimpleNamespace(callback_query=query), context
            )

        self.assertEqual(
            context.user_data[publish_setting.PUBLISH_SETTINGS_PROFILE_KEY], 2
        )
        self.assertEqual(
            query.edit_message_text.await_args.args[0], "⚙️ 投稿配置 · 备用投稿频道"
        )
        callbacks = [
            button.callback_data
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("publish:channel", callbacks)
        self.assertNotIn("publish:profile_open:2", callbacks)

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

    def test_bot_publish_rule_accepts_decorated_labels_and_hash_separator(self):
        config = {
            "bot_publish_rule_enabled": True,
            "bot_publish_rule_fields": ["艺名", "区域", "联系"],
        }
        result = publish_setting._apply_bot_publish_rule(
            "🍃艺名 #玩具\n🌎区域 #徐汇区\n\n📡联系：@pflszy006",
            config,
        )
        self.assertEqual(result, "艺名：#玩具\n区域：#徐汇区\n联系：@pflszy006")

    def test_original_info_keeps_raw_content_and_deduplicates_username(self):
        records = {
            "legacy-token": {
                "text": "艺名：旧内容\n联系：@pflszy006",
                "created_at": 1,
            },
            "unrelated": {"text": "联系：@another_user", "created_at": 2},
        }
        message = SimpleNamespace(
            text="🍃艺名 #玩具\n📡联系：@pflszy006\n\n保留原始换行",
            caption=None,
            entities=[SimpleNamespace(to_dict=lambda: {"type": "bold", "offset": 0, "length": 2})],
            chat_id=42,
            message_id=99,
        )
        with (
            patch.object(publish_setting, "_load_original_publish_info", return_value=records),
            patch.object(publish_setting, "_save_original_publish_info") as save,
        ):
            token = publish_setting._store_original_publish_info(message, channel_id=-100111)

        self.assertEqual(token, "user_pflszy006")
        self.assertNotIn("legacy-token", records)
        self.assertEqual(records[token]["text"], message.text)
        self.assertEqual(records[token]["entities"], [{"type": "bold", "offset": 0, "length": 2}])
        self.assertEqual(records[token]["username"], "pflszy006")
        self.assertIn("unrelated", records)
        save.assert_called_once_with(records)

    def test_new_publish_profile_does_not_inherit_root_backup_channel(self):
        root = {
            "channel_id": -100111,
            "backup_channel_id": -100222,
            "backup_channel_routes": {"-100111": -100222},
            "backup_channel_use_telethon": True,
            "backup_channel_session": "old-session",
            "backup_channel_listen_session": "old-listen",
            "backup_channel_forward_session": "old-forward",
            "submission_targets": [
                {"id": 1, "name": "默认频道", "channel_id": -100111, "enabled": True},
                {"id": 2, "name": "新频道", "channel_id": -100333, "enabled": True},
            ],
        }
        with (
            patch.object(publish_setting, "load_publish_config", return_value=root),
            patch.object(publish_setting, "load_json", return_value={}),
        ):
            profile = publish_setting.load_publish_profile(2)

        self.assertEqual(profile["channel_id"], -100333)
        self.assertIsNone(profile["backup_channel_id"])
        self.assertEqual(profile["backup_channel_routes"], {})
        self.assertFalse(profile["backup_channel_use_telethon"])
        self.assertEqual(profile["backup_channel_session"], "")

    def test_changing_main_channel_clears_active_backup_but_keeps_old_route(self):
        config = {
            "channel_id": -100111,
            "backup_channel_id": -100222,
            "backup_channel_routes": {},
            "backup_channel_use_telethon": True,
            "backup_channel_session": "old-session",
            "backup_channel_listen_session": "old-listen",
            "backup_channel_forward_session": "old-forward",
        }
        publish_setting._change_main_channel_without_inheriting_backup(
            config, -100111, -100333
        )

        self.assertEqual(config["channel_id"], -100333)
        self.assertIsNone(config["backup_channel_id"])
        self.assertEqual(config["backup_channel_routes"], {"-100111": -100222})
        self.assertFalse(config["backup_channel_use_telethon"])
        self.assertEqual(config["backup_channel_session"], "")

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

    async def test_super_admin_direct_publish_clears_stale_comment_flow(self):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(
            user_data={
                "waiting_post": True,
                "publish_comment_target": {"channel_id": -100111, "message_id": 7},
                "publish_keyword_search": True,
                "publish_pending_proof_id": "stale-proof",
            },
            application=SimpleNamespace(bot_data={"owner_id": 42}),
        )
        config = {"channel_id": -100111, "submission_targets": []}

        with (
            patch.object(publish_setting, "is_super_admin", return_value=True),
            patch.object(publish_setting, "_is_submission_administrator", return_value=True),
            patch.object(publish_setting, "publish_message", new=AsyncMock()) as publish,
        ):
            await publish_setting._start_publish_flow(update, context, config)

        self.assertFalse(context.user_data["waiting_post"])
        self.assertNotIn("publish_comment_target", context.user_data)
        self.assertNotIn("publish_keyword_search", context.user_data)
        self.assertNotIn("publish_pending_proof_id", context.user_data)
        publish.assert_awaited_once_with(update, context)

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
