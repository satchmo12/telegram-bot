import asyncio
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from telegram.ext import ApplicationHandlerStop

from group import points_auction as auction


class PointsAuctionRulesTests(TestCase):
    def test_new_auction_disables_pinning_by_default(self):
        self.assertFalse(auction._new_auction(-100123, 1)["pin_messages"])

    def test_increment_defaults_to_one_and_configured_increment_sets_minimum(self):
        record = auction._new_auction(-100123, 1)
        self.assertEqual(record["bid_increment"], 1)
        self.assertEqual(auction._minimum_next_bid(record), 1)

        record["bid_increment"] = 10
        record["starting_bid"] = 50
        record["bids"] = {"2": {"user_id": 2, "amount": 100, "first_bid_at": 1}}
        self.assertEqual(auction._minimum_next_bid(record), 110)

    def test_qualifying_bid_extends_current_end_time_every_time(self):
        record = auction._new_auction(-100123, 1)
        record.update({"end_at": 1_000, "extend_before_min": 5, "extend_by_min": 2})

        self.assertEqual(auction._extend_end_time_after_bid(record, 800), 2)
        self.assertEqual(record["end_at"], 1_120)
        # The next bid still falls in the final five-minute window relative to
        # the newly extended end time, so it extends the auction again.
        self.assertEqual(auction._extend_end_time_after_bid(record, 900), 2)
        self.assertEqual(record["end_at"], 1_240)

    def test_reply_lookup_only_accepts_the_published_auction_message(self):
        record = auction._new_auction(-100123, 1)
        record.update({
            "status": "open",
            "post_messages": [{"chat_id": -100123, "message_id": 456}],
        })
        data = {"auctions": {record["id"]: record}}
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100123),
            reply_to_message=SimpleNamespace(message_id=456),
        )
        self.assertIs(auction._auction_replied_to_record(data, message), record)

        message.reply_to_message.message_id = 999
        self.assertIsNone(auction._auction_replied_to_record(data, message))

    def test_successful_bid_respects_increment_and_extends_end(self):
        now = 1_000
        record = auction._new_auction(-100123, 1)
        record.update({
            "status": "open",
            "name": "测试拍卖",
            "end_at": now + 60,
            "starting_bid": 50,
            "bid_increment": 10,
            "extend_before_min": 5,
            "extend_by_min": 2,
            "bids": {"1": {"user_id": 1, "amount": 100, "first_bid_at": now - 1}},
        })
        data = {"auctions": {record["id"]: record}}
        user = SimpleNamespace(id=2, full_name="竞拍者", username="bidder")
        changes = []
        saves = []

        async def refresh(_context, _record):
            return None

        with (
            patch.object(auction.time, "time", return_value=now),
            patch.object(auction, "_load", return_value=data),
            patch.object(auction, "_save", side_effect=lambda _context, saved: saves.append(saved)),
            patch.object(auction, "get_points", return_value=1_000),
            patch.object(auction, "change_points", side_effect=lambda *args, **kwargs: changes.append((args, kwargs))),
            patch.object(auction, "_refresh_posts", side_effect=refresh),
            patch.object(auction, "_timestamp_text", return_value="延时后的结束时间"),
        ):
            success, message = asyncio.run(auction._submit_bid(SimpleNamespace(), record, user, 110))

        self.assertTrue(success)
        self.assertEqual(record["bids"]["2"]["amount"], 110)
        self.assertEqual(record["end_at"], now + 180)
        self.assertEqual(changes[0][0][2], -110)
        self.assertEqual(len(saves), 2)
        self.assertIn("已触发延时", message)

    def test_group_reply_with_number_submits_a_bid(self):
        record = auction._new_auction(-100123, 1)
        record.update({
            "status": "open",
            "post_messages": [{"chat_id": -100123, "message_id": 456}],
        })
        replies = []

        class Message:
            chat = SimpleNamespace(id=-100123)
            reply_to_message = SimpleNamespace(message_id=456)
            text = "120"

            async def reply_text(self, text):
                replies.append(text)

        update = SimpleNamespace(
            message=Message(),
            effective_user=SimpleNamespace(id=2),
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
        )

        async def submit(_context, submitted_record, user, amount):
            self.assertIs(submitted_record, record)
            self.assertEqual(user.id, 2)
            self.assertEqual(amount, 120)
            return True, "✅ 出价成功"

        with (
            patch.object(auction, "_load", return_value={"auctions": {record["id"]: record}}),
            patch.object(auction, "_submit_bid", side_effect=submit),
        ):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auction.auction_group_reply_bid(update, SimpleNamespace()))

        self.assertEqual(replies, ["✅ 出价成功"])

    def test_publish_pins_auction_and_retains_a_direct_message_link(self):
        record = auction._new_auction(-1001234567890, 1)
        record["pin_messages"] = True
        pins = []

        class Bot:
            async def pin_chat_message(self, **kwargs):
                pins.append(kwargs)

        async def send_post(_context, _record):
            return SimpleNamespace(
                message_id=456,
                chat=SimpleNamespace(username="auction_group"),
            )

        with patch.object(auction, "_send_post", side_effect=send_post):
            result = asyncio.run(auction._publish(record, SimpleNamespace(bot=Bot())))

        self.assertTrue(result)
        self.assertEqual(pins, [{
            "chat_id": -1001234567890,
            "message_id": 456,
            "disable_notification": True,
        }])
        self.assertEqual(
            auction._auction_message_link(record, record["post_messages"][0]),
            "https://t.me/auction_group/456",
        )

    def test_settlement_pins_the_result_when_pinning_is_enabled(self):
        record = auction._new_auction(-100123, 1)
        record.update({"status": "open", "name": "测试拍卖", "pin_messages": True})
        pins = []

        class Bot:
            async def pin_chat_message(self, **kwargs):
                pins.append(kwargs)

        async def refresh(_context, _record):
            return None

        async def send_result(_context, _record, _result, *, detail_text=""):
            return SimpleNamespace(message_id=789)

        with (
            patch.object(auction, "_refresh_posts", side_effect=refresh),
            patch.object(auction, "_send_settlement_announcement", side_effect=send_result),
        ):
            asyncio.run(auction._settle(record, SimpleNamespace(bot=Bot())))

        self.assertEqual(pins, [{
            "chat_id": -100123,
            "message_id": 789,
            "disable_notification": True,
        }])

    def test_group_auction_command_returns_one_button_per_open_auction(self):
        now = 1_000
        first = auction._new_auction(-100123, 1)
        first.update({
            "status": "open", "name": "第一个拍卖", "end_at": now + 120,
            "post_messages": [{"chat_id": -100123, "message_id": 11}],
        })
        second = auction._new_auction(-100123, 1)
        second.update({
            "status": "open", "name": "第二个拍卖", "end_at": now + 60,
            "post_messages": [{"chat_id": -100123, "message_id": 22}],
        })
        replies = []

        class Message:
            async def reply_text(self, text, reply_markup=None):
                replies.append((text, reply_markup))

        update = SimpleNamespace(
            message=Message(),
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        data = {"auctions": {first["id"]: first, second["id"]: second}}

        with (
            patch.object(auction, "_load", return_value=data),
            patch.object(auction.time, "time", return_value=now),
        ):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auction.auction_group_command(update, SimpleNamespace()))

        self.assertEqual(replies[0][0], "当前进行中的拍卖：")
        buttons = [row[0] for row in replies[0][1].inline_keyboard]
        self.assertEqual([button.text for button in buttons], ["💎 第二个拍卖", "💎 第一个拍卖"])
        self.assertEqual([button.url for button in buttons], [
            "https://t.me/c/123/22", "https://t.me/c/123/11",
        ])


if __name__ == "__main__":
    import unittest
    unittest.main()
