import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from channel import protocol_submission
from channel.channel_config import _new_rule_default
from channel.publish_setting import (
    PENDING_PROOF_KEY,
    PROTOCOL_AUTO_SUBMISSION_KEY,
    _consume_protocol_auto_submission,
    _retry_flood_control,
    handle_protocol_submission_command,
)
from channel.telethon_forwarder import (
    _album_event_is_outgoing,
    _forward_to_submission_bot,
    _retry_telethon_flood,
    _submission_bot_username,
)


class ProtocolSubmissionRuleTests(TestCase):
    def test_new_rule_disables_automatic_submission_by_default(self):
        rule = _new_rule_default("1", source_id=-100123, session_name="worker")
        self.assertFalse(rule["submit_to_bot"])

    def test_submission_target_requires_enabled_rule(self):
        self.assertEqual(_submission_bot_username({"submit_to_bot": False, "_submission_bot_username": "demo_bot"}), "")
        self.assertEqual(_submission_bot_username({"submit_to_bot": True, "_submission_bot_username": "@demo_bot"}), "demo_bot")

    def test_album_outgoing_state_uses_message_when_album_event_has_no_out_flag(self):
        # Telethon Album.Event exposes no ``out`` attribute.  Channel albums
        # posted by the protocol account must therefore inspect their child
        # messages before applying the listen_outgoing rule.
        self.assertTrue(
            _album_event_is_outgoing(
                SimpleNamespace(),
                [SimpleNamespace(out=True), SimpleNamespace(out=True)],
            )
        )
        self.assertFalse(
            _album_event_is_outgoing(
                SimpleNamespace(),
                [SimpleNamespace(out=False)],
            )
        )

    def test_sender_registry_normalizes_sessions(self):
        stored = {}

        def save(_path, value):
            stored.update(value)

        with patch.object(protocol_submission, "save_json", side_effect=save):
            protocol_submission.set_protocol_submission_senders(
                {123: {"sessions": [" worker ", "worker", ""]}, "bad": {"sessions": ["x"]}}
            )

        self.assertEqual(stored["senders"]["123"]["sessions"], ["worker"])
        with patch.object(protocol_submission, "load_json", return_value=stored):
            self.assertTrue(protocol_submission.is_protocol_submission_sender("123"))
            self.assertFalse(protocol_submission.is_protocol_submission_sender("456"))


class ProtocolSubmissionAsyncTests(IsolatedAsyncioTestCase):
    async def test_bot_api_flood_text_is_retried(self):
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Flood control exceeded. Retry in 5 seconds")
            return "published"

        with patch("channel.publish_setting.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await _retry_flood_control(operation, action="复制投稿到频道")

        self.assertEqual(result, "published")
        self.assertEqual(calls, 2)
        sleep.assert_awaited_once_with(5.25)

    async def test_flood_wait_error_is_retried_after_requested_delay(self):
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Flood control exceeded. Retry in 5 seconds")
            return "sent"

        with patch("channel.telethon_forwarder.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await _retry_telethon_flood(operation, action="测试发送")

        self.assertEqual(result, "sent")
        self.assertEqual(calls, 2)
        sleep.assert_awaited_once_with(5.25)

    async def test_forward_arms_bot_then_forwards_original_message(self):
        client = SimpleNamespace(
            send_message=AsyncMock(),
            forward_messages=AsyncMock(return_value="forwarded"),
        )
        message = SimpleNamespace(id=88)
        with patch("channel.telethon_forwarder.asyncio.sleep", new=AsyncMock()):
            result = await _forward_to_submission_bot(client, "@demo_bot", message)

        self.assertEqual(result, "forwarded")
        client.send_message.assert_awaited_once_with("@demo_bot", "/protocol_submit")
        client.forward_messages.assert_awaited_once_with("@demo_bot", message)

    async def test_stopped_history_command_is_not_armed(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(type="private"),
            message=message,
        )
        context = SimpleNamespace(user_data={}, args=["cancelled-request"])
        with (
            patch("channel.publish_setting.is_protocol_submission_sender", return_value=True),
            patch("channel.publish_setting.is_protocol_history_request_active", return_value=False),
        ):
            await handle_protocol_submission_command(update, context)

        self.assertNotIn("waiting_post", context.user_data)
        self.assertNotIn(PROTOCOL_AUTO_SUBMISSION_KEY, context.user_data)
        message.reply_text.assert_awaited_once()

    async def test_bound_history_command_arms_the_matching_protocol_account(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(type="private"),
            message=message,
        )
        context = SimpleNamespace(user_data={}, args=["history-request"])
        with (
            patch("channel.publish_setting.is_protocol_history_request_active", return_value=True),
            patch("channel.publish_setting.is_protocol_history_request_sender", return_value=True),
        ):
            await handle_protocol_submission_command(update, context)

        self.assertTrue(context.user_data["waiting_post"])
        self.assertTrue(context.user_data[PROTOCOL_AUTO_SUBMISSION_KEY])
        message.reply_text.assert_not_awaited()

    async def test_multiple_arm_commands_keep_one_permit_per_forwarded_post(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(type="private"),
            message=message,
        )
        context = SimpleNamespace(user_data={})
        with patch(
            "channel.publish_setting.is_protocol_submission_sender", return_value=True
        ):
            await handle_protocol_submission_command(update, context)
            await handle_protocol_submission_command(update, context)

        # The old boolean marker could represent only one of these two arm
        # commands.  A FIFO permit queue lets both following forwards retain
        # their automatic-owner treatment.
        self.assertEqual(len(context.user_data[PROTOCOL_AUTO_SUBMISSION_KEY]), 2)
        self.assertTrue(_consume_protocol_auto_submission(context))
        self.assertTrue(_consume_protocol_auto_submission(context))
        self.assertFalse(_consume_protocol_auto_submission(context))

    async def test_trusted_protocol_command_arms_one_automatic_submission(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(type="private"),
            message=message,
        )
        context = SimpleNamespace(
            user_data={PENDING_PROOF_KEY: "old", "publish_keyword_search": True}
        )
        with patch(
            "channel.publish_setting.is_protocol_submission_sender", return_value=True
        ):
            await handle_protocol_submission_command(update, context)

        self.assertTrue(context.user_data["waiting_post"])
        self.assertTrue(context.user_data[PROTOCOL_AUTO_SUBMISSION_KEY])
        self.assertTrue(context.user_data["post_no_name"])
        self.assertNotIn(PENDING_PROOF_KEY, context.user_data)
        message.reply_text.assert_not_awaited()

class ProtocolSubmissionHistoryTests(IsolatedAsyncioTestCase):
    async def test_history_submission_respects_start_and_end_message_ids(self):
        from channel.telethon_forwarder import _forward_history_to_submission_bot

        messages = [
            SimpleNamespace(id=index, message=f"post {index}", entities=[], media=None, grouped_id=None)
            for index in range(1, 6)
        ]

        class Client:
            async def iter_messages(self, _source_id, **kwargs):
                min_id = kwargs.get("min_id") or 0
                max_id = kwargs.get("max_id")
                for item in messages:
                    if item.id <= min_id or (max_id is not None and item.id >= max_id):
                        continue
                    yield item

        # A previous task reached a much higher message ID. A newly chosen
        # lower interval must still run instead of being hidden by that cursor.
        state = {
            "keys": {
                "worker:-1001:submission:demo_bot": {"history_max_id": 9999}
            }
        }
        rule = {
            "submit_to_bot": True,
            "_submission_bot_username": "demo_bot",
            "filter": "all",
            "start_id": "2",
            "end_id": "4",
        }
        forward = AsyncMock()
        with (
            patch("channel.telethon_forwarder._forward_to_submission_bot", new=forward),
            patch("channel.telethon_forwarder.asyncio.sleep", new=AsyncMock()),
        ):
            await _forward_history_to_submission_bot(
                Client(), session_name="worker", source_id=-1001, rule=rule, state=state
            )

        self.assertEqual([call.args[2].id for call in forward.await_args_list], [2, 3, 4])
