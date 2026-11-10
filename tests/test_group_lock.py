"""Tests for group lock settings, command matching, and daily schedule windows."""

import unittest

from group import group_setting as settings


class GroupLockScheduleTests(unittest.TestCase):
    def test_accepts_midnight_as_24_00_and_over_night_window(self):
        schedule = settings._parse_group_lock_schedule("24:00 8:00")

        self.assertEqual(schedule, ("24:00", "08:00"))
        self.assertTrue(
            settings._is_group_lock_schedule_locked("24:00", "08:00", 0)
        )
        self.assertTrue(
            settings._is_group_lock_schedule_locked("24:00", "08:00", 7 * 60 + 59)
        )
        self.assertFalse(
            settings._is_group_lock_schedule_locked("24:00", "08:00", 8 * 60)
        )
        self.assertFalse(
            settings._is_group_lock_schedule_locked("24:00", "08:00", 23 * 60)
        )

    def test_supports_a_same_day_lock_window(self):
        self.assertTrue(
            settings._is_group_lock_schedule_locked("09:00", "18:00", 9 * 60)
        )
        self.assertTrue(
            settings._is_group_lock_schedule_locked("09:00", "18:00", 17 * 60 + 59)
        )
        self.assertFalse(
            settings._is_group_lock_schedule_locked("09:00", "18:00", 18 * 60)
        )

    def test_rejects_invalid_or_zero_length_schedule(self):
        self.assertIsNone(settings._parse_group_lock_schedule("24:30 08:00"))
        self.assertIsNone(settings._parse_group_lock_schedule("22:00"))
        self.assertIsNone(settings._parse_group_lock_schedule("08:00 08:00"))

    def test_configured_commands_accept_slash_and_bot_username(self):
        cfg = {
            "group_lock_command": "闭群",
            "group_unlock_command": "开群",
        }

        self.assertTrue(settings._group_lock_command_action("/闭群@demo_bot", cfg))
        self.assertFalse(settings._group_lock_command_action("开群", cfg))
        self.assertIsNone(settings._group_lock_command_action("闭群 现在", cfg))

    def test_lock_settings_keyboard_exposes_all_requested_configuration(self):
        markup = settings._group_lock_settings_keyboard("-1001234567890", {})
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }

        self.assertIn("gcfg:group_lock_edit:-1001234567890:command", callbacks)
        self.assertIn("gcfg:group_lock_edit:-1001234567890:unlock_command", callbacks)
        self.assertIn("gcfg:group_lock_edit:-1001234567890:message", callbacks)
        self.assertIn("gcfg:group_lock_edit:-1001234567890:unlock_message", callbacks)
        self.assertIn("gcfg:group_lock_edit:-1001234567890:schedule", callbacks)


if __name__ == "__main__":
    unittest.main()

class GroupLockApplyTests(unittest.TestCase):
    def test_lock_then_unlock_restores_saved_default_permissions(self):
        import asyncio
        from types import SimpleNamespace
        from telegram import ChatPermissions

        class Bot:
            id = 123

            def __init__(self):
                self.permission_updates = []
                self.notices = []

            async def get_chat(self, _chat_id):
                return SimpleNamespace(
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_photos=True,
                        can_invite_users=True,
                    )
                )

            async def set_chat_permissions(self, **kwargs):
                self.permission_updates.append(kwargs)

            async def send_message(self, **kwargs):
                self.notices.append(kwargs)

        async def apply_both():
            bot = Bot()
            context = SimpleNamespace(bot=bot)
            cfg = {}
            locked_result = await settings._apply_group_lock_state(
                context, -1001234567890, cfg, locked=True
            )
            unlocked_result = await settings._apply_group_lock_state(
                context, -1001234567890, cfg, locked=False
            )
            return bot, cfg, locked_result, unlocked_result

        bot, cfg, locked_result, unlocked_result = asyncio.run(apply_both())

        self.assertEqual(locked_result, (True, "已锁群。"))
        self.assertEqual(unlocked_result, (True, "已解锁群。"))
        self.assertFalse(cfg["group_lock_locked"])
        self.assertTrue(cfg["group_lock_restore_permissions"]["can_send_messages"])
        self.assertTrue(cfg["group_lock_restore_permissions"]["can_invite_users"])
        self.assertFalse(bot.permission_updates[0]["permissions"].can_send_messages)
        self.assertTrue(bot.permission_updates[1]["permissions"].can_send_messages)
        self.assertTrue(bot.permission_updates[1]["permissions"].can_invite_users)
        self.assertTrue(
            all(update["use_independent_chat_permissions"] for update in bot.permission_updates)
        )
