"""Regression coverage for collection updates after channel-post edits."""

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Update

import channel.publish_setting as publish


class PublishSettingCollectionTests(unittest.TestCase):
    def setUp(self):
        publish._TEACHER_LIST_FILTER_CACHE.clear()

    def tearDown(self):
        publish._TEACHER_LIST_FILTER_CACHE.clear()

    def test_edited_channel_post_replaces_old_collected_fields(self):
        """An edit removes old values and records the post's new fields."""
        config = {
            "channel_id": -1003878817420,
            "keyword_extract_labels": ["艺名", "联系", "城市"],
            "checkin_enabled": False,
        }
        stored_keywords = {
            "旧艺名": [
                {
                    "label": "艺名",
                    "raw": "旧艺名",
                    "channel_id": -1003878817420,
                    "channel_message_id": 77,
                    "created_at": 1,
                }
            ],
            "@old_contact": [
                {
                    "label": "联系",
                    "raw": "@old_contact",
                    "channel_id": -1003878817420,
                    "channel_message_id": 77,
                    "created_at": 1,
                }
            ],
        }

        def load_keywords():
            return copy.deepcopy(stored_keywords)

        def save_keywords(value):
            stored_keywords.clear()
            stored_keywords.update(copy.deepcopy(value))

        update = Update.de_json(
            {
                "update_id": 1,
                "edited_channel_post": {
                    "message_id": 77,
                    "date": 1,
                    "edit_date": 2,
                    "chat": {
                        "id": -1003878817420,
                        "type": "channel",
                        "title": "test channel",
                    },
                    "text": "艺名：新艺名\n联系：@new_contact\n城市：上海",
                },
            },
            None,
        )

        async def process_edit():
            with (
                patch.object(publish, "load_publish_config", return_value=config),
                patch.object(publish, "_load_keyword_map", side_effect=load_keywords),
                patch.object(publish, "_save_keyword_map", side_effect=save_keywords),
                patch.object(publish, "_load_comment_map", return_value={}),
                patch.object(publish, "_load_authoritative_main_post_content", return_value=None),
                patch.object(publish, "_update_comment_report_position"),
                patch.object(publish, "_register_checkin_post"),
                patch.object(publish, "_sync_main_post_edit_to_backup"),
            ):
                await publish._capture_comment_source_message(
                    update, SimpleNamespace()
                )

        asyncio.run(process_edit())

        self.assertNotIn("旧艺名", stored_keywords)
        self.assertNotIn("@old_contact", stored_keywords)
        self.assertEqual(stored_keywords["新艺名"][0]["raw"], "新艺名")
        self.assertEqual(stored_keywords["@new_contact"][0]["raw"], "@new_contact")
        self.assertEqual(stored_keywords["上海"][0]["raw"], "上海")

    def test_report_link_alias_uses_existing_report_for_same_post(self):
        """A historical formatter link remains valid after a contact change."""
        reports_data = {
            "reports": {
                "yunsu3t": {
                    "channel_id": -1003878817420,
                    "message_id": 1237,
                    "updated_at": 10,
                    "subjects": [{"label": "联系", "value": "@Yunsu3T"}],
                    "comments": [{"content": "existing history"}],
                }
            },
            "aliases": {},
        }

        with (
            patch.object(
                publish, "_load_comment_reports", return_value=reports_data
            ),
            patch.object(publish, "_save_comment_reports"),
        ):
            changed = publish._sync_report_link_aliases_for_post(
                -1003878817420,
                1237,
                "个人报告：https://t.me/youbingma_bot?start=report_ys520521",
            )

        self.assertEqual(changed, 1)
        self.assertEqual(reports_data["aliases"]["ys520521"], "yunsu3t")

    def test_teacher_filter_keeps_same_secondary_category_together(self):
        """Interleaved source posts must render one section per secondary group."""
        config = {
            "collection_display_group_label": "城市",
            "collection_display_subgroup_label": "区域",
            "collection_display_label": "艺名",
        }
        posts = [
            {"channel_id": -1001, "message_id": 1, "fields": {"城市": ["上海"], "区域": ["浦东"], "艺名": ["甲"]}},
            {"channel_id": -1001, "message_id": 2, "fields": {"城市": ["上海"], "区域": ["徐汇"], "艺名": ["乙"]}},
            {"channel_id": -1001, "message_id": 3, "fields": {"城市": ["上海"], "区域": ["浦东"], "艺名": ["丙"]}},
        ]

        items = publish._filter_collection_display_posts(posts, config, "上海")

        self.assertEqual(
            [(item["primary"], item["secondary"]) for item in items],
            [("上海", "浦东"), ("上海", "浦东"), ("上海", "徐汇")],
        )
        token = publish._store_teacher_filter_result(config, "上海", items)
        text, _entities, _keyboard = publish._teacher_list_filtered_payload(
            config, token, publish._TEACHER_LIST_FILTER_CACHE[token], page=0
        )
        self.assertEqual(text.count("〔浦东〕"), 1)
        self.assertLess(text.index("1. 甲"), text.index("2. 丙"))
        self.assertLess(text.index("2. 丙"), text.index("〔徐汇〕"))

    def test_teacher_category_filter_requires_at_least_two_characters(self):
        config = {
            "collection_display_group_label": "城市",
            "collection_display_subgroup_label": "区域",
        }
        posts = [
            {
                "channel_id": -1001,
                "message_id": 1,
                "fields": {"城市": ["上海"], "区域": ["闵行区"]},
            }
        ]

        self.assertEqual(len(publish._filter_collection_display_posts(posts, config, "上海")), 1)
        self.assertEqual(len(publish._filter_collection_display_posts(posts, config, "闵行")), 1)
        self.assertEqual(publish._filter_collection_display_posts(posts, config, "上"), [])
        self.assertEqual(publish._filter_collection_display_posts(posts, config, "海"), [])

    def test_saved_teacher_filter_uses_current_collection_values(self):
        """Paging an open filtered roster must not render its old snapshot."""
        config = {
            "channel_id": -1003878817420,
            "collection_display_group_label": "城市",
            "collection_display_subgroup_label": "区域",
        }
        old_items = [
            {
                "channel_id": -1003878817420,
                "message_id": 77,
                "fields": {"城市": ["北京"], "区域": ["朝阳"], "艺名": ["旧艺名"]},
            }
        ]
        token = publish._store_teacher_filter_result(
            config,
            "上海",
            publish._filter_collection_display_posts(old_items, config, "上海"),
        )
        state = publish._get_teacher_filter_result(token)
        self.assertEqual(state["items"], [])

        updated_items = [
            {
                "channel_id": -1003878817420,
                "message_id": 77,
                "fields": {"城市": ["上海"], "区域": ["浦东"], "艺名": ["新艺名"]},
            }
        ]
        with patch.object(publish, "_load_collection_display_posts", return_value=updated_items):
            refreshed = publish._refresh_teacher_filter_result(config, state)

        self.assertEqual(len(refreshed["items"]), 1)
        self.assertEqual(refreshed["items"][0]["post"]["fields"]["艺名"], ["新艺名"])


class PublishSettingKeywordSearchTests(unittest.TestCase):
    @staticmethod
    def _route(index: int) -> dict:
        return {
            "label": "标签",
            "raw": f"目标-{index}",
            "key": f"目标-{index}",
            "channel_id": -1001234567890,
            "channel_message_id": index + 1,
            "discussion_message_id": index + 100,
            "created_at": index,
        }

    def test_keyword_route_search_keeps_all_unique_matches(self):
        """A broad search is no longer capped at the first 30 results."""
        records = [self._route(index) for index in range(35)]
        # Duplicate index 0 must still be presented only once because the user
        # selects a post, not an individual extracted field record.
        records.append({**self._route(0), "label": "区域", "created_at": 999})

        with patch.object(
            publish,
            "load_publish_config",
            return_value={"channel_id": -1001234567890},
        ):
            routes = publish._find_keyword_routes(
                "目标",
                keyword_data={"目标": records},
            )

        self.assertEqual(len(routes), 35)
        self.assertEqual(routes[0]["channel_message_id"], 1)

    def test_keyword_map_save_keeps_more_than_thirty_records_per_key(self):
        """Pagination must not be undermined by index-save truncation."""
        data = {
            "热门关键词": [self._route(index) for index in range(35)],
        }

        with patch.object(publish, "save_json") as save_json:
            publish._save_keyword_map(data)

        self.assertEqual(len(data["热门关键词"]), 35)
        save_json.assert_called_once_with(publish.KEYWORD_MAP_FILE, data)

    def test_post_result_keyboard_paginates_without_changing_indexes(self):
        routes = [
            {**self._route(index), "display_channel": "主频道"}
            for index in range(17)
        ]

        markup = publish._keyword_post_results_keyboard(routes, page=2)
        buttons = [button for row in markup.inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons if button.callback_data]

        self.assertIn("publish:keyword_post_pick:8", callbacks)
        self.assertIn("publish:keyword_post_pick:15", callbacks)
        self.assertIn("publish:keyword_post_page:1", callbacks)
        self.assertIn("publish:keyword_post_page:3", callbacks)
        self.assertNotIn("publish:keyword_post_pick:16", callbacks)

    def test_main_channel_display_mode_does_not_truncate_results(self):
        routes = [self._route(index) for index in range(25)]

        displayed = publish._keyword_post_display_routes(
            routes,
            {
                "channel_id": -1001234567890,
                "keyword_post_search_display_mode": "main",
            },
        )

        self.assertEqual(len(displayed), 25)


class PublishSettingInputSearchPriorityTests(unittest.TestCase):
    def test_active_channel_id_prompt_blocks_global_input_search(self):
        """A channel-ID setting must own the next text even when search is global."""
        message = SimpleNamespace(text="-1001234567890", reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(user_data={"waiting_channel_id": True})

        async def attempt_search():
            with patch.object(
                publish,
                "load_publish_config",
                return_value={"input_search_enabled": True},
            ):
                return await publish._handle_keyword_post_search_input(update, context)

        handled = asyncio.run(attempt_search())

        self.assertFalse(handled)
        message.reply_text.assert_not_awaited()

    def test_channel_id_prompt_is_processed_before_private_forwarding(self):
        message = SimpleNamespace(text="-1001234567890", reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(type="private"),
        )
        config = {"input_search_enabled": True, "channel_id": -1001111111111}
        context = SimpleNamespace(user_data={"waiting_channel_id": True})

        async def process_text():
            with (
                patch.object(publish, "load_publish_config", return_value=config),
                patch.object(publish, "save_publish_config") as save_config,
                patch.object(publish, "_record_publish_channel_change"),
            ):
                with self.assertRaises(publish.ApplicationHandlerStop):
                    await publish._keyword_search_interceptor(update, context)
                return save_config

        save_config = asyncio.run(process_text())

        self.assertEqual(config["channel_id"], -1001234567890)
        self.assertFalse(context.user_data["waiting_channel_id"])
        save_config.assert_called_once()
        self.assertIn("✅ 已保存频道", message.reply_text.await_args.args[0])


class PublishSettingReportDeletionTests(unittest.TestCase):
    """Regression coverage for report comments deleted by an administrator."""

    @staticmethod
    def _approved_comment_submission() -> dict:
        return {
            "status": "approved",
            "submission_kind": "comment",
            "report_author": "测试用户",
            "report_content": "给 @x9988778 的评论",
            "forward_channel_id": -1009876543210,
            "channel_message_id": 321,
            "reviewed_at": 1_700_000_000,
            "comment_target": {
                "channel_id": -1001234567890,
                "message_id": 456,
            },
        }

    @classmethod
    def _report(cls, comment: dict) -> dict:
        return {
            "channel_id": -1001234567890,
            "message_id": 456,
            "subjects": [{"label": "联系方式", "value": "@x9988778"}],
            "comments": [comment],
        }

    def test_deleted_comment_is_not_restored_when_report_link_is_reopened(self):
        """Backfill from approved submissions must honor report deletions."""
        submission = self._approved_comment_submission()
        comment = publish._report_comment_from_submission(submission)
        report = self._report(comment)

        removed = report["comments"].pop()
        self.assertTrue(publish._mark_report_comment_deleted(report, removed))

        with (
            patch.object(publish, "_keyword_post_refs_for_report_usernames", return_value=[]),
            patch.object(publish, "_load_pending_submissions", return_value={"comment-1": submission}),
        ):
            changed = publish._backfill_report_comments(report, "x9988778")

        self.assertEqual(changed, 0)
        self.assertEqual(report["comments"], [])
        self.assertTrue(publish._is_report_comment_deleted(report, comment))

    def test_deleted_comment_is_not_readded_by_late_publish_completion(self):
        """A concurrent/late approval write must honor the same tombstone."""
        submission = self._approved_comment_submission()
        comment = publish._report_comment_from_submission(submission)
        report = self._report(comment)
        publish._mark_report_comment_deleted(report, report["comments"].pop())
        data = {"reports": {"x9988778": report}, "aliases": {}}

        with (
            patch.object(publish, "_load_comment_reports", return_value=data),
            patch.object(publish, "_save_comment_reports") as save_reports,
        ):
            publish._append_report_comment(
                submission,
                SimpleNamespace(message_id=submission["channel_message_id"]),
            )

        self.assertEqual(report["comments"], [])
        save_reports.assert_not_called()

    def test_delete_sync_removes_original_and_forwarding_copies(self):
        """Deleting a report row also removes every tracked Telegram copy."""
        comment = {
            "main_discussion_chat_id": -1000000000001,
            "main_discussion_message_id": 101,
            "backup_discussion_chat_id": -1000000000002,
            "backup_discussion_message_id": 202,
            "forward_channel_id": -1000000000003,
            "forward_message_id": 303,
        }
        calls = []

        async def delete_message(*, chat_id, message_id):
            calls.append((chat_id, message_id))

        async def delete_all_copies():
            return await publish._sync_report_comment_deletion(
                SimpleNamespace(bot=SimpleNamespace(delete_message=delete_message)),
                comment,
            )

        deleted, untracked, failed = asyncio.run(delete_all_copies())

        self.assertEqual((deleted, untracked, failed), (3, 0, 0))
        self.assertEqual(
            calls,
            [
                (-1000000000001, 101),  # native reply below the original post
                (-1000000000002, 202),  # optional backup-post reply
                (-1000000000003, 303),  # optional forwarding-channel copy
            ],
        )


if __name__ == "__main__":
    unittest.main()
