import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from channel import telethon_ai_reply as protocol_ai


class ProtocolAiCommentTests(unittest.TestCase):
    def setUp(self):
        protocol_ai._message_history.clear()
        protocol_ai._last_reply_time.clear()
        protocol_ai._reply_history.clear()
        protocol_ai._processing_chats.clear()

    def test_comment_switch_is_saved_without_changing_normal_ai_switch(self):
        data = {"sessions": {}}

        with (
            patch.object(protocol_ai, "_load_config", return_value=data),
            patch.object(protocol_ai, "_save_config") as save_config,
        ):
            settings = protocol_ai.set_group_ai_comment_enabled(
                "demo_bot", "session_a", -100123, True
            )

        self.assertTrue(settings["channel_comment_enabled"])
        self.assertFalse(settings["enabled"])
        self.assertTrue(
            data["sessions"]["session_a"]["groups"]["-100123"]
            ["channel_comment_enabled"]
        )
        save_config.assert_called_once_with("demo_bot", data)

    def test_comment_only_session_starts_listener(self):
        data = {
            "sessions": {
                "comment_session": {
                    "groups": {"-100123": {"channel_comment_enabled": True}}
                },
                "off_session": {"groups": {"-100456": {"enabled": False}}},
            }
        }
        with patch.object(protocol_ai, "_load_config", return_value=data):
            enabled_sessions = protocol_ai.get_enabled_sessions("demo_bot")

        self.assertEqual(enabled_sessions, {"comment_session"})

    def test_detects_forwarded_channel_posts_only(self):
        from_channel = SimpleNamespace(
            fwd_from=SimpleNamespace(channel_post=42),
            forward=None,
            post=False,
        )
        from_user = SimpleNamespace(
            fwd_from=SimpleNamespace(from_id=SimpleNamespace(user_id=7)),
            forward=None,
            post=False,
        )

        self.assertTrue(protocol_ai._is_channel_post_message(from_channel))
        self.assertFalse(protocol_ai._is_channel_post_message(from_user))

    def test_enabled_comment_replies_to_the_channel_post(self):
        message = SimpleNamespace(
            id=77,
            message="这是频道发布的新消息",
            fwd_from=SimpleNamespace(channel_post=88),
            forward=None,
            post=False,
        )
        event = SimpleNamespace(
            is_group=True,
            chat_id=-100123,
            message=message,
            raw_text=message.message,
            out=False,
            sender_id=9,
        )
        event.get_sender = AsyncMock(return_value=SimpleNamespace(bot=False))
        client = SimpleNamespace(send_message=AsyncMock())
        settings = {
            "enabled": False,
            "channel_comment_enabled": True,
            "probability_percent": 50,
            "min_interval_sec": 0,
            "max_replies_per_hour": 1000,
        }

        with (
            patch.object(protocol_ai, "get_group_settings", return_value=settings),
            patch.object(protocol_ai, "ask_ollama", new=AsyncMock(return_value="值得关注")) as ask,
            patch.object(protocol_ai.asyncio, "sleep", new=AsyncMock()),
            patch.object(protocol_ai.random, "uniform", return_value=0),
            patch("builtins.print"),
        ):
            asyncio.run(
                protocol_ai.handle_protocol_group_message(
                    "demo_bot", "session_a", client, event
                )
            )

        ask.assert_awaited_once_with(
            "用户9: 这是频道发布的新消息",
            "这是频道发布的新消息",
            mode="channel_comment",
        )
        client.send_message.assert_awaited_once_with(
            -100123,
            "值得关注",
            reply_to=77,
            link_preview=False,
        )

    def test_comment_switch_does_not_react_to_regular_group_messages(self):
        message = SimpleNamespace(
            id=77,
            message="普通群消息",
            fwd_from=None,
            forward=None,
            post=False,
        )
        event = SimpleNamespace(
            is_group=True,
            chat_id=-100123,
            message=message,
            raw_text=message.message,
            out=False,
            sender_id=9,
        )
        event.get_sender = AsyncMock(return_value=SimpleNamespace(bot=False))
        client = SimpleNamespace(send_message=AsyncMock())
        settings = {
            "enabled": False,
            "channel_comment_enabled": True,
            "probability_percent": 50,
            "min_interval_sec": 0,
            "max_replies_per_hour": 1000,
        }

        with (
            patch.object(protocol_ai, "get_group_settings", return_value=settings),
            patch.object(protocol_ai, "ask_ollama", new=AsyncMock()) as ask,
        ):
            asyncio.run(
                protocol_ai.handle_protocol_group_message(
                    "demo_bot", "session_a", client, event
                )
            )

        ask.assert_not_awaited()
        client.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
