"""Visibility rules for shared and per-clone Telethon sessions."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from channel import telethon_login as login


class TelethonSessionVisibilityTests(unittest.TestCase):
    def test_shared_main_requires_super_admin_even_with_protocol_permission(self):
        user = SimpleNamespace(id=1001, username="clone_owner")
        context = SimpleNamespace()

        with (
            patch.object(login, "has_admin_permission", return_value=True),
            patch.object(login, "is_super_admin", return_value=False),
        ):
            self.assertFalse(login._can_access_session(context, user, "main"))
            self.assertTrue(login._can_access_session(context, user, "personal"))

        with patch.object(login, "is_super_admin", return_value=True):
            self.assertTrue(login._can_access_session(context, user, "main"))

    def test_clone_owner_does_not_see_shared_main_but_super_admin_does(self):
        with tempfile.TemporaryDirectory() as root:
            clone_dir = os.path.join(root, "clone")
            shared_dir = os.path.join(root, "shared")
            os.makedirs(clone_dir)
            os.makedirs(shared_dir)
            open(os.path.join(clone_dir, "personal.session"), "w").close()
            open(os.path.join(shared_dir, "main.session"), "w").close()

            context = SimpleNamespace()
            clone_owner = SimpleNamespace(id=1001, username="clone_owner")
            super_admin = SimpleNamespace(id=9999, username="root")

            def sessions_dir(_context, session_name=None):
                return shared_dir if session_name == "main" else clone_dir

            with (
                patch.object(login, "_get_sessions_dir", side_effect=sessions_dir),
                patch.object(login, "has_admin_permission", return_value=True),
                patch.object(login, "is_super_admin", side_effect=lambda uid: uid == 9999),
            ):
                self.assertEqual(
                    login._list_session_names(context, clone_owner),
                    ["personal"],
                )
                self.assertEqual(
                    login._list_session_names(context, super_admin),
                    ["main", "personal"],
                )

    def test_regular_user_only_sees_sessions_they_can_access(self):
        with tempfile.TemporaryDirectory() as root:
            clone_dir = os.path.join(root, "clone")
            os.makedirs(clone_dir)
            open(os.path.join(clone_dir, "owned.session"), "w").close()
            open(os.path.join(clone_dir, "other.session"), "w").close()
            context = SimpleNamespace()
            user = SimpleNamespace(id=1001, username="member")

            with (
                patch.object(login, "_get_sessions_dir", return_value=clone_dir),
                patch.object(login, "has_admin_permission", return_value=False),
                patch.object(login, "is_super_admin", return_value=False),
                patch.object(
                    login,
                    "_can_access_session",
                    side_effect=lambda _ctx, _user, name: name == "owned",
                ),
            ):
                self.assertEqual(login._list_session_names(context, user), ["owned"])


if __name__ == "__main__":
    unittest.main()
