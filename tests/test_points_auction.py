import asyncio
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from telegram.ext import ApplicationHandlerStop

from group import points_auction as auction


class PointsAuctionRulesTests(TestCase):
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


if __name__ == "__main__":
    import unittest
    unittest.main()
