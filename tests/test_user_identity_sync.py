from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from group.check_sacm import check_name_change
from group.grouplist import record_user


class GroupUserIdentitySyncTests(TestCase):
    def test_username_change_is_saved_and_previous_username_is_retained(self):
        users = {
            "123": {
                "full_name": "Alice",
                "username": "old_handle",
                "username_history": [],
            }
        }

        with patch("group.check_sacm.load_users", return_value=users), patch(
            "group.check_sacm.save_users"
        ) as save_users:
            old_name = check_name_change(
                123,
                "Alice",
                "@new_handle",
                current_chat_id=-100123,
            )

        self.assertEqual(old_name, "")
        self.assertEqual(users["123"]["username"], "new_handle")
        self.assertEqual(users["123"]["username_history"], ["old_handle"])
        save_users.assert_called_once_with(-100123, users)

    def test_removed_username_clears_profile_and_preserves_old_username_once(self):
        users = {
            "123": {
                "full_name": "Alice",
                "username": "old_handle",
                "username_history": ["older_handle"],
            }
        }

        with patch("group.check_sacm.load_users", return_value=users), patch(
            "group.check_sacm.save_users"
        ) as save_users:
            check_name_change(123, "Alice", None, current_chat_id=-100123)

        self.assertIsNone(users["123"]["username"])
        self.assertEqual(
            users["123"]["username_history"],
            ["older_handle", "old_handle"],
        )
        save_users.assert_called_once_with(-100123, users)


class EconomyProfileIdentitySyncTests(IsolatedAsyncioTestCase):
    async def test_group_user_tracker_updates_economy_username(self):
        chat = SimpleNamespace(id=-100123, type="supergroup")
        user = SimpleNamespace(id=123, full_name="Alice", username="new_handle")
        update = SimpleNamespace(effective_chat=chat, effective_user=user)

        with patch("group.grouplist.load_users", return_value={}), patch(
            "group.grouplist.save_users"
        ), patch("group.grouplist.ensure_user_exists") as ensure_user_exists:
            await record_user(update, SimpleNamespace())

        ensure_user_exists.assert_called_once_with(
            -100123,
            123,
            "Alice",
            "new_handle",
        )
