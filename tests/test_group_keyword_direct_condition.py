import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from channel import publish_setting as publish


class GroupKeywordDirectConditionTests(unittest.TestCase):
    CONFIG = {
        "channel_id": -100123,
        "keyword_group_reply_enabled": True,
        "keyword_group_reply_rules": [
            {
                "id": 1,
                "input_label": "艺名",
                "reply_label": "",
                "display_text": "点击联系美丽的“{keyword}”老师",
                "default_text": "",
                "button_text": "点击约课{keyword}",
                "enabled": True,
            }
        ],
    }
    KEYWORD_DATA = {
        "静静": [
            {
                "label": "艺名",
                "raw": "#静静",
                "channel_id": -100123,
                "channel_message_id": 88,
            }
        ],
        "大胸": [
            {
                "label": "标签",
                "raw": "#大胸",
                "channel_id": -100123,
                "channel_message_id": 88,
            }
        ],
    }

    def setUp(self):
        publish._GROUP_KEYWORD_REPLY_CACHE.clear()

    def tearDown(self):
        publish._GROUP_KEYWORD_REPLY_CACHE.clear()

    def _update(self, text: str):
        return SimpleNamespace(
            message=SimpleNamespace(text=text),
            effective_chat=SimpleNamespace(type="supergroup"),
            effective_user=SimpleNamespace(is_bot=False),
        )

    def test_direct_condition_finds_the_primary_teacher_value(self):
        with (
            patch.object(publish, "load_publish_config", return_value=self.CONFIG),
            patch.object(publish, "_load_keyword_map", return_value=self.KEYWORD_DATA),
        ):
            values = publish._find_group_keyword_condition_values("大胸", "艺名")

        self.assertEqual(
            values,
            [
                {
                    "value": "静静",
                    "channel_id": -100123,
                    "message_id": 88,
                }
            ],
        )

    def test_bare_category_returns_the_same_roster_as_teacher_prefix(self):
        """“浦东” is an alias for “老师 浦东”, not a keyword-reply query."""
        config = {
            **self.CONFIG,
            "keyword_group_reply_enabled": False,
            "collection_display_enabled": True,
            "collection_display_group_label": "城市",
            "collection_display_subgroup_label": "区域",
        }
        posts = [
            {
                "channel_id": -100123,
                "message_id": 88,
                "fields": {"城市": ["上海"], "区域": ["浦东"], "艺名": ["静静"]},
            }
        ]

        async def run():
            roster_reply = AsyncMock()
            with (
                patch.object(publish, "load_publish_config", return_value=config),
                patch.object(publish, "_load_collection_display_posts", return_value=posts),
                patch.object(publish, "_reply_collection_display", new=roster_reply),
            ):
                self.assertTrue(
                    await publish._handle_checkin_group_message(
                        self._update("浦东"), SimpleNamespace()
                    )
                )
                bare_filter = roster_reply.await_args.kwargs["filter_text"]

                roster_reply.reset_mock()
                self.assertTrue(
                    await publish._handle_checkin_group_message(
                        self._update("老师 浦东"), SimpleNamespace()
                    )
                )
                prefixed_filter = roster_reply.await_args.kwargs["filter_text"]

            self.assertEqual(bare_filter, "浦东")
            self.assertEqual(prefixed_filter, bare_filter)

        asyncio.run(run())

    def test_group_keyword_values_are_not_capped_at_twenty(self):
        records = [
            {
                "label": "艺名",
                "raw": f"#老师{index}",
                "channel_id": -100123,
                "channel_message_id": index,
            }
            for index in range(1, 22)
        ]
        with (
            patch.object(publish, "load_publish_config", return_value=self.CONFIG),
            patch.object(publish, "_load_keyword_map", return_value={"静静": records}),
        ):
            values = publish._find_group_keyword_reply_values("静静", "艺名", "")

        self.assertEqual(len(values), 21)

    def test_group_keyword_reply_paginates_after_ten_and_is_not_deleted(self):
        values = [
            {
                "value": f"老师{index}",
                "channel_id": -100123,
                "message_id": index,
            }
            for index in range(1, 12)
        ]

        async def route_link(_context, route):
            return f"https://t.me/test/{route['channel_message_id']}"

        async def run():
            safe_reply = AsyncMock()
            with (
                patch.object(publish, "load_publish_config", return_value=self.CONFIG),
                patch.object(publish, "_find_group_keyword_reply_values", return_value=values),
                patch.object(publish, "_route_message_link", new=route_link),
                patch.object(publish, "safe_reply", new=safe_reply),
            ):
                self.assertTrue(
                    await publish._handle_group_keyword_reply(
                        self._update("静静"), SimpleNamespace()
                    )
                )

            first_page_text = safe_reply.await_args.args[2]
            first_page_markup = safe_reply.await_args.kwargs["reply_markup"]
            self.assertIn("共找到 <b>11</b> 条结果 · 当前第 1/2 页", first_page_text)
            self.assertEqual(len(first_page_markup.inline_keyboard), 11)
            self.assertEqual(safe_reply.await_args.kwargs["auto_delete_seconds"], 0)

            next_button = first_page_markup.inline_keyboard[-1][0]
            _prefix, _action, token, page = next_button.callback_data.split(":")
            second_page_text, second_page_markup = publish._group_keyword_reply_payload(
                token,
                publish._get_group_keyword_reply_results(token),
                int(page),
            )
            self.assertIn("共找到 <b>11</b> 条结果 · 当前第 2/2 页", second_page_text)
            self.assertNotIn("11.", second_page_text)
            self.assertEqual(len(second_page_markup.inline_keyboard), 2)

        asyncio.run(run())

    def test_direct_condition_replies_only_when_index_has_a_match(self):
        async def run():
            safe_reply = AsyncMock()
            with (
                patch.object(publish, "load_publish_config", return_value=self.CONFIG),
                patch.object(publish, "_load_keyword_map", return_value=self.KEYWORD_DATA),
                patch.object(
                    publish, "_route_message_link", new=AsyncMock(return_value="https://t.me/test/88")
                ),
                patch.object(publish, "safe_reply", new=safe_reply),
            ):
                handled = await publish._handle_group_keyword_reply(
                    self._update("大胸"), SimpleNamespace()
                )
                self.assertTrue(handled)
                self.assertIn("🔎 共找到 <b>1</b> 条结果 · 当前第 1/1 页", safe_reply.await_args.args[2])
                self.assertNotIn("老师：静静", safe_reply.await_args.args[2])
                self.assertEqual(safe_reply.await_args.kwargs["auto_delete_seconds"], 0)
                button = safe_reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
                self.assertEqual(button.text, "点击约课静静")

                safe_reply.reset_mock()
                handled = await publish._handle_group_keyword_reply(
                    self._update("不存在的条件"), SimpleNamespace()
                )
                self.assertFalse(handled)
                safe_reply.assert_not_awaited()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
