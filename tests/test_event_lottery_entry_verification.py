import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest

from channel import publish_setting
from group import event_lottery


class EventLotteryEntryVerificationTests(unittest.TestCase):
    def test_new_lottery_entry_verification_defaults_to_disabled_numeric(self):
        record = event_lottery._new_lottery(-100123, 88)

        self.assertEqual(
            record["entry_verification"], {"enabled": False, "method": "numeric"}
        )
        self.assertEqual(
            event_lottery._entry_verification_settings(record),
            {"enabled": False, "method": "numeric"},
        )

    def test_numeric_challenge_is_bound_to_lottery_and_user(self):
        record = event_lottery._new_lottery(-100123, 88)
        question, keyboard = event_lottery._create_numeric_entry_verification_challenge(
            record, 999
        )

        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard for button in row
        ]
        self.assertTrue(question.endswith("= ?"))
        self.assertEqual(len(callbacks), 4)
        self.assertTrue(
            all(
                callback.startswith(f"eventlot:entry_verify:{record['id']}:999:")
                for callback in callbacks
            )
        )
        self.assertTrue(all(len(callback) <= 64 for callback in callbacks))

    def test_enabling_entry_verification_invalidates_existing_participants(self):
        record = event_lottery._new_lottery(-100123, 88)
        record["participants"] = {
            "999": {
                "user_id": 999,
                "eligible": True,
                "entry_verification": {"method": "numeric", "passed_at": 1},
            }
        }

        event_lottery._reset_entry_verification_for_participants(record)

        self.assertFalse(record["participants"]["999"]["eligible"])
        self.assertNotIn("entry_verification", record["participants"]["999"])

    def test_draw_excludes_users_without_required_entry_verification(self):
        class FakeBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, **kwargs):
                self.messages.append(kwargs)

        class FakeContext:
            bot = FakeBot()

        record = event_lottery._new_lottery(-100123, 88)
        record.update(
            {
                "status": "open",
                "draw_at": 1,
                "prizes": [{"name": "奖品", "count": 1}],
                "entry_verification": {"enabled": True, "method": "numeric"},
                "participants": {
                    "1": {"user_id": 1, "name": "未验证用户", "eligible": True},
                    "2": {
                        "user_id": 2,
                        "name": "已验证用户",
                        "eligible": True,
                        "entry_verification": {"method": "numeric", "passed_at": 1},
                    },
                },
            }
        )

        self.assertTrue(asyncio.run(event_lottery._draw_if_due(FakeContext(), record)))
        self.assertEqual(
            record["winners"],
            [{"prize": "奖品", "user_id": 2, "name": "已验证用户"}],
        )

    def test_draw_result_sends_full_cover_card_to_group_and_winner(self):
        class FakeBot:
            def __init__(self):
                self.photos = []
                self.messages = []

            async def send_photo(self, **kwargs):
                self.photos.append(kwargs)

            async def send_message(self, **kwargs):
                self.messages.append(kwargs)

        context = SimpleNamespace(bot=FakeBot())
        record = event_lottery._new_lottery(-100123, 88)
        record.update(
            {
                "status": "open",
                "name": "周末福利",
                "description": "开奖后请联系管理员领取奖品。",
                "draw_at": 1,
                "cover": {"type": "photo", "file_id": "cover-file-id"},
                "requirements": {
                    "username": True,
                    "min_messages": 3,
                    "password": "福利",
                    "comment_rules": [],
                },
                "prizes": [{"name": "奶茶券", "count": 1}],
                "participants": {
                    "2": {"user_id": 2, "name": "中奖用户", "eligible": True},
                },
            }
        )

        self.assertTrue(asyncio.run(event_lottery._draw_if_due(context, record)))

        self.assertEqual({item["chat_id"] for item in context.bot.photos}, {"-100123", 2})
        self.assertEqual({item["photo"] for item in context.bot.photos}, {"cover-file-id"})
        for item in context.bot.photos:
            caption = item["caption"]
            self.assertIn("📮 <b>参与条件</b>", caption)
            self.assertIn("🏆 <b>开奖结果</b>", caption)
            self.assertIn("📜 <b>抽奖说明</b>", caption)
            self.assertIn("📆 <b>开奖时间</b>", caption)
            self.assertIn("奶茶券：中奖用户", caption)
        self.assertEqual(context.bot.messages, [])

    def test_correct_numeric_answer_unlocks_lottery_participation(self):
        record = event_lottery._new_lottery(-100123, 88)
        record.update(
            {
                "status": "open",
                "name": "验证抽奖",
                "draw_at": 2_000_000_000,
                "entry_verification": {"enabled": True, "method": "numeric"},
            }
        )
        data = {"lotteries": {record["id"]: record}}
        _question, keyboard = event_lottery._create_numeric_entry_verification_challenge(
            record, 999
        )
        challenge = event_lottery._entry_verification_challenges[(record["id"], 999)]
        callback_data = next(
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if int(button.text) == challenge["answer"]
        )

        class FakeQuery:
            def __init__(self):
                self.data = callback_data
                self.from_user = SimpleNamespace(
                    id=999, full_name="Test User", username="tester"
                )
                self.message = SimpleNamespace(chat=SimpleNamespace(type="private"))
                self.edited = None

            async def answer(self, *args, **kwargs):
                return None

            async def edit_message_text(self, *args, **kwargs):
                self.edited = (args, kwargs)

        query = FakeQuery()
        old_load, old_save = event_lottery._load, event_lottery._save
        event_lottery._load = lambda _context: data
        event_lottery._save = lambda _context, _data: None
        try:
            context = SimpleNamespace(user_data={})
            asyncio.run(
                event_lottery.event_lottery_callback(
                    SimpleNamespace(callback_query=query), context
                )
            )
        finally:
            event_lottery._load, event_lottery._save = old_load, old_save

        entry = record["participants"]["999"]
        self.assertEqual(entry["entry_verification"]["method"], "numeric")
        self.assertTrue(entry["eligible"])
        self.assertIsNotNone(query.edited)


    def test_manual_participant_is_exempt_from_entry_verification(self):
        record = event_lottery._new_lottery(-100123, 88)
        record["entry_verification"] = {"enabled": True, "method": "numeric"}
        record["participants"] = {
            "999": {"user_id": 999, "manual_override": True, "eligible": True}
        }

        event_lottery._reset_entry_verification_for_participants(record)

        self.assertTrue(event_lottery._entry_verification_passed(record, 999))
        self.assertTrue(record["participants"]["999"]["eligible"])

    def test_participant_manager_can_directly_add_a_user(self):
        record = event_lottery._new_lottery(-100123, 88)
        record["participant_managers"] = {
            "10": {"user_id": 10, "name": "Manager", "username": "manager"}
        }
        data = {"lotteries": {record["id"]: record}}

        class FakeBot:
            async def get_chat_member(self, _chat_id, _user_id):
                return SimpleNamespace(status="member")

            async def delete_message(self, **_kwargs):
                return None

        class FakeMessage:
            text = "999"

            async def reply_text(self, *args, **kwargs):
                self.reply = (args, kwargs)

        message = FakeMessage()
        context = SimpleNamespace(
            bot=FakeBot(),
            user_data={
                event_lottery.DRAFT_KEY: {
                    "id": record["id"],
                    "field": "manual_participant",
                    "participant_manager_input": True,
                    "return_to": "participants",
                }
            },
        )
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(
                id=10, full_name="Manager", username="manager"
            ),
            effective_chat=SimpleNamespace(type="private"),
        )
        old_load, old_save = event_lottery._load, event_lottery._save
        event_lottery._load = lambda _context: data
        event_lottery._save = lambda _context, _data: None
        try:
            self.assertTrue(asyncio.run(event_lottery._handle_editor_input(update, context)))
        finally:
            event_lottery._load, event_lottery._save = old_load, old_save

        entry = record["participants"]["999"]
        self.assertTrue(entry["manual_override"])
        self.assertTrue(entry["eligible"])
        self.assertEqual(entry["added_by"]["user_id"], 10)
        self.assertIn("由 Manager", message.reply[0][0])

    def test_participants_keyboard_exposes_manual_add_and_delegate_controls(self):
        record = event_lottery._new_lottery(-100123, 88)
        callbacks = {
            button.callback_data
            for row in event_lottery._participants_keyboard(
                record, 1, can_manage_delegates=True
            ).inline_keyboard
            for button in row
            if button.callback_data
        }

        self.assertIn(f"eventlot:manual_add:{record['id']}", callbacks)
        self.assertIn(f"eventlot:participant_managers:{record['id']}", callbacks)



class EventLotteryRequirementsNavigationTests(unittest.TestCase):
    def test_primary_password_input_returns_to_requirements_panel(self):
        record = event_lottery._new_lottery(-100123, 88)
        data = {"lotteries": {record["id"]: record}}

        class FakeBot:
            async def delete_message(self, **_kwargs):
                return None

        class FakeQuery:
            def __init__(self):
                self.data = f"eventlot:field:{record['id']}:password"
                self.from_user = SimpleNamespace(id=88)
                self.message = SimpleNamespace(
                    chat=SimpleNamespace(id=88, type="private"), message_id=12
                )
                self.edited = None

            async def answer(self, *_args, **_kwargs):
                return None

            async def edit_message_text(self, *args, **kwargs):
                self.edited = (args, kwargs)

        class FakeMessage:
            text = "entry-code"

            def __init__(self):
                self.replies = []

            async def reply_text(self, *args, **kwargs):
                self.replies.append((args, kwargs))

        async def can_manage(*_args, **_kwargs):
            return True

        context = SimpleNamespace(bot=FakeBot(), user_data={})
        query = FakeQuery()
        old_load = event_lottery._load
        old_save = event_lottery._save
        old_can_manage = event_lottery._can_manage
        event_lottery._load = lambda _context: data
        event_lottery._save = lambda _context, _data: None
        event_lottery._can_manage = can_manage
        try:
            asyncio.run(
                event_lottery.event_lottery_callback(
                    SimpleNamespace(callback_query=query), context
                )
            )
            self.assertEqual(
                context.user_data[event_lottery.DRAFT_KEY]["return_to"],
                "main_requirements",
            )
            prompt_callbacks = {
                button.callback_data
                for row in query.edited[1]["reply_markup"].inline_keyboard
                for button in row
            }
            self.assertIn(f"eventlot:req_main:{record['id']}", prompt_callbacks)

            message = FakeMessage()
            update = SimpleNamespace(
                message=message,
                effective_user=SimpleNamespace(id=88, full_name="Owner", username="owner"),
                effective_chat=SimpleNamespace(type="private"),
            )
            self.assertTrue(
                asyncio.run(event_lottery._handle_editor_input(update, context))
            )
        finally:
            event_lottery._load = old_load
            event_lottery._save = old_save
            event_lottery._can_manage = old_can_manage

        self.assertEqual(record["requirements"]["password"], "entry-code")
        self.assertNotIn(event_lottery.DRAFT_KEY, context.user_data)
        self.assertEqual(message.replies[-1][0][0], "🎰 抽奖发布群的参与条件")
        returned_callbacks = {
            button.callback_data
            for row in message.replies[-1][1]["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertIn(f"eventlot:field:{record['id']}:password", returned_callbacks)


class EventLotteryCommentRuleTests(unittest.TestCase):
    def test_every_comment_rule_must_match_before_participant_is_eligible(self):
        record = event_lottery._new_lottery(-100123, 88)
        record.update({
            "status": "open",
            "published_at": 1,
            "requirements": {
                "username": False,
                "min_messages": 0,
                "password": "",
                "comment_rules": [
                    {"key": "飞机号", "value": "iwoai"},
                    {"key": "邀请码", "value": "abc"},
                ],
            },
            "participants": {
                "99": {
                    "user_id": 99,
                    "name": "Commenter",
                    "username": "commenter",
                    "registered_at": 1,
                    "eligible": False,
                    "passed_passwords": [],
                }
            },
        })
        data = {"lotteries": {record["id"]: record}}

        class FakeMessage:
            def __init__(self, text):
                self.text = text
                self.date = datetime.now()

        context = SimpleNamespace(user_data={})
        user = SimpleNamespace(
            id=99, full_name="Commenter", username="commenter", is_bot=False
        )
        old_load, old_save = event_lottery._load, event_lottery._save
        event_lottery._load = lambda _context: data
        event_lottery._save = lambda _context, _data: None
        try:
            first_update = SimpleNamespace(
                effective_message=FakeMessage("飞机号：iwoai"),
                effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
                effective_user=user,
            )
            asyncio.run(event_lottery.event_lottery_message_counter(first_update, context))
            self.assertFalse(event_lottery._comment_rules_completed(record, 99))
            self.assertFalse(record["participants"]["99"]["eligible"])

            second_update = SimpleNamespace(
                effective_message=FakeMessage("邀请码=abc"),
                effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
                effective_user=user,
            )
            asyncio.run(event_lottery.event_lottery_message_counter(second_update, context))
        finally:
            event_lottery._load, event_lottery._save = old_load, old_save

        self.assertTrue(event_lottery._comment_rules_completed(record, 99))
        self.assertTrue(record["participants"]["99"]["eligible"])

    def test_comment_rule_requires_key_and_value_in_the_same_comment(self):
        record = event_lottery._new_lottery(-100123, 88)
        record["requirements"]["comment_rules"] = [
            {"key": "飞机号", "value": "iwoai"}
        ]

        self.assertFalse(
            event_lottery._record_comment_rule_matches(record, 99, "只填写了飞机号")
        )
        self.assertFalse(event_lottery._comment_rules_completed(record, 99))
        self.assertTrue(
            event_lottery._record_comment_rule_matches(record, 99, "飞机号：iwoai")
        )
        self.assertTrue(event_lottery._comment_rules_completed(record, 99))


class EventLotteryPreviewTests(unittest.TestCase):
    def test_preview_shows_close_and_publish_controls(self):
        record = event_lottery._new_lottery(-100123, 88)
        data = {"lotteries": {record["id"]: record}}

        class FakeBot:
            async def send_message(self, **kwargs):
                self.sent = kwargs

        class FakeQuery:
            def __init__(self):
                self.data = f"eventlot:preview:{record['id']}"
                self.from_user = SimpleNamespace(id=88)
                self.message = SimpleNamespace(chat_id=88)

            async def answer(self, *_args, **_kwargs):
                return None

        async def can_manage(*_args, **_kwargs):
            return True

        context = SimpleNamespace(bot=FakeBot(), user_data={})
        old_load, old_can_manage = event_lottery._load, event_lottery._can_manage
        event_lottery._load = lambda _context: data
        event_lottery._can_manage = can_manage
        try:
            asyncio.run(
                event_lottery.event_lottery_callback(
                    SimpleNamespace(callback_query=FakeQuery()), context
                )
            )
        finally:
            event_lottery._load, event_lottery._can_manage = old_load, old_can_manage

        callbacks = {
            button.callback_data
            for row in context.bot.sent["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(
            callbacks,
            {
                f"eventlot:preview_close:{record['id']}",
                f"eventlot:preview_publish:{record['id']}",
            },
        )

    def test_preview_publish_deletes_preview_and_publishes_lottery(self):
        record = event_lottery._new_lottery(-100123, 88)
        record.update({
            "name": "预览发布抽奖",
            "prizes": [{"name": "奖品", "count": 1}],
            "draw_at": 2_000_000_000,
        })
        data = {"lotteries": {record["id"]: record}}

        class FakeBot:
            username = "lottery_bot"

            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)

        class FakeMessage:
            chat_id = 88

            def __init__(self):
                self.deleted = False

            async def delete(self):
                self.deleted = True

        class FakeQuery:
            def __init__(self):
                self.data = f"eventlot:preview_publish:{record['id']}"
                self.from_user = SimpleNamespace(id=88)
                self.message = FakeMessage()
                self.answers = []

            async def answer(self, *args, **kwargs):
                self.answers.append((args, kwargs))

        async def can_manage(*_args, **_kwargs):
            return True

        context = SimpleNamespace(bot=FakeBot(), user_data={})
        query = FakeQuery()
        old_load = event_lottery._load
        old_save = event_lottery._save
        old_can_manage = event_lottery._can_manage
        event_lottery._load = lambda _context: data
        event_lottery._save = lambda _context, _data: None
        event_lottery._can_manage = can_manage
        try:
            asyncio.run(
                event_lottery.event_lottery_callback(
                    SimpleNamespace(callback_query=query), context
                )
            )
        finally:
            event_lottery._load = old_load
            event_lottery._save = old_save
            event_lottery._can_manage = old_can_manage

        self.assertEqual(record["status"], "open")
        self.assertTrue(query.message.deleted)
        self.assertEqual(context.bot.sent[0]["chat_id"], str(record["chat_id"]))
        self.assertIn("预览已关闭", query.answers[-1][0][0])


class EventLotteryHandlerRegistrationTests(unittest.TestCase):
    def test_counter_runs_before_general_group_text_interceptors(self):
        class FakeJobQueue:
            def run_repeating(self, *_args, **_kwargs):
                return None

        class FakeApp:
            def __init__(self):
                self.handlers = []
                self.job_queue = FakeJobQueue()

            def add_handler(self, handler, group=0):
                self.handlers.append((handler, group))

        app = FakeApp()
        event_lottery.register_event_lottery_handlers(app)
        counter_group = next(
            group
            for handler, group in app.handlers
            if handler.callback is event_lottery.event_lottery_message_counter
        )

        self.assertEqual(counter_group, -29)


class EventLotteryCompletionNoticeTests(unittest.TestCase):
    def test_message_threshold_completion_sends_one_private_notice(self):
        record = event_lottery._new_lottery(-100123, 88)
        record.update({
            "status": "open",
            "published_at": 1,
            "name": "发言达标抽奖",
            "requirements": {
                "username": False,
                "min_messages": 1,
                "password": "",
                "comment_rules": [],
            },
            "participants": {
                "99": {
                    "user_id": 99,
                    "name": "Speaker",
                    "username": "speaker",
                    "registered_at": 1,
                    "eligible": False,
                    "passed_passwords": [],
                }
            },
        })
        data = {"lotteries": {record["id"]: record}}

        class FakeBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, **kwargs):
                self.messages.append(kwargs)

        class FakeMessage:
            def __init__(self):
                self.text = "普通发言"
                self.date = datetime.now()

        context = SimpleNamespace(bot=FakeBot(), user_data={})
        user = SimpleNamespace(
            id=99, full_name="Speaker", username="speaker", is_bot=False
        )
        update = SimpleNamespace(
            effective_message=FakeMessage(),
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=user,
        )
        old_load, old_save = event_lottery._load, event_lottery._save
        event_lottery._load = lambda _context: data
        event_lottery._save = lambda _context, _data: None
        try:
            asyncio.run(event_lottery.event_lottery_message_counter(update, context))
            self.assertFalse(record["participants"]["99"]["eligible"])
            self.assertEqual(context.bot.messages, [])

            asyncio.run(event_lottery.event_lottery_message_counter(update, context))
            self.assertTrue(record["participants"]["99"]["eligible"])
            self.assertEqual(len(context.bot.messages), 1)
            self.assertIn("已完成全部参与条件", context.bot.messages[0]["text"])

            asyncio.run(event_lottery.event_lottery_message_counter(update, context))
        finally:
            event_lottery._load, event_lottery._save = old_load, old_save

        self.assertEqual(len(context.bot.messages), 1)


class PrivateInputRoutingRegistrationTests(unittest.TestCase):
    def test_submission_input_handler_runs_before_private_forwarding(self):
        class FakeApp:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append((handler, group))

        app = FakeApp()
        publish_setting.register_publish_setting_handlers(app)
        submission_input_group = next(
            group
            for handler, group in app.handlers
            if handler.callback is publish_setting._handle_text_input
        )

        self.assertEqual(submission_input_group, -17)


if __name__ == "__main__":
    unittest.main()
