"""Regression coverage for collection updates after channel-post edits."""

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Update

import channel.publish_setting as publish
import channel.publish_roster as roster


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

    def test_teacher_categories_are_stably_sorted_by_pinyin(self):
        """Deleting a newer post must not change category-button indexes."""
        config = {
            "collection_display_group_label": "城市",
            "collection_display_subgroup_label": "区域",
        }
        posts = [
            {"channel_id": -1001, "message_id": 30, "created_at": 30, "fields": {"城市": ["上海"], "区域": ["徐汇"]}},
            {"channel_id": -1001, "message_id": 20, "created_at": 20, "fields": {"城市": ["北京"], "区域": ["朝阳"]}},
            {"channel_id": -1001, "message_id": 10, "created_at": 10, "fields": {"城市": ["上海"], "区域": ["浦东"]}},
        ]

        with patch.object(roster, "_load_collection_display_posts", return_value=posts):
            catalog = publish._teacher_list_catalog(config)

        self.assertEqual([item["name"] for item in catalog], ["北京", "上海"])
        self.assertEqual(
            [item["name"] for item in catalog[1]["secondary"]],
            ["浦东", "徐汇"],
        )

        filtered = publish._filter_collection_display_posts(posts, config, "")
        self.assertEqual(
            [(item["primary"], item["secondary"]) for item in filtered],
            [("北京", "朝阳"), ("上海", "浦东"), ("上海", "徐汇")],
        )

    def test_delete_menu_offers_delete_all_for_the_current_category(self):
        config = {
            "collection_display_text": "收录老师",
            "collection_display_group_label": "城市",
            "collection_display_subgroup_label": "区域",
            "collection_display_label": "艺名",
        }
        catalog = [{
            "name": "上海",
            "secondary": [{
                "name": "徐汇",
                "posts": [
                    {"channel_id": -1001, "message_id": 1, "fields": {"艺名": ["甲"]}},
                    {"channel_id": -1001, "message_id": 2, "fields": {"艺名": ["乙"]}},
                ],
            }],
        }]
        _text, keyboard = publish._teacher_list_delete_menu_payload(
            config, catalog, 0, 0, 0
        )
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("teacher_list:delete_all_confirm:0:0:0", callbacks)

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
        with patch.object(roster, "_load_collection_display_posts", return_value=updated_items):
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


# Kept outside the original classes so the failover fixture stays focused.
class PublishSettingPromotionRecoveryTests(unittest.TestCase):
    def setUp(self):
        publish._PROMOTED_BACKUP_REPAIR_CACHE.clear()

    def tearDown(self):
        publish._PROMOTED_BACKUP_REPAIR_CACHE.clear()

    def test_submission_search_allows_missing_discussion_mapping(self):
        """A promoted post remains submittable through forwarding fallback."""
        msg = SimpleNamespace(text="小艺", reply_text=AsyncMock())
        update = SimpleNamespace(message=msg)
        context = SimpleNamespace(user_data={publish.KEYWORD_INPUT_KEY: True})
        route = {
            "label": "艺名",
            "raw": "小艺",
            "channel_id": -100202,
            "channel_message_id": 7,
        }

        async def search():
            with (
                patch.object(
                    publish,
                    "_restore_missing_discussion_mappings_for_keyword",
                    new=AsyncMock(return_value=(0, 1)),
                ),
                patch.object(publish, "_find_keyword_routes", return_value=[route]) as find_routes,
                patch.object(
                    publish, "_route_message_link", new=AsyncMock(return_value="https://t.me/c/202/7")
                ),
            ):
                consumed = await publish._handle_keyword_search_input(update, context)
                return consumed, find_routes

        consumed, find_routes = asyncio.run(search())
        self.assertTrue(consumed)
        find_routes.assert_called_once_with("小艺", require_discussion=False)
        self.assertEqual(
            context.user_data[publish.COMMENT_TARGET_KEY],
            {"channel_id": -100202, "message_id": 7},
        )
        msg.reply_text.assert_awaited_once()

    def test_missing_discussion_mapping_still_forwards_submission(self):
        """No native comment is required when the promoted post has no mapping."""
        submission = {
            "user_chat_id": 12345,
            "user_message_id": 99,
            "comment_target": {"channel_id": -100202, "message_id": 7},
        }
        config = {"forward_channel_id": -100303}
        forwarded = SimpleNamespace(message_id=88, scheduled=False)

        async def publish_comment():
            with (
                patch.object(publish, "_load_comment_map", return_value={}),
                patch.object(
                    publish,
                    "_refresh_discussion_mapping_for_post",
                    new=AsyncMock(
                        side_effect=RuntimeError(
                            "未能重新获取该主帖的讨论组消息。"
                        )
                    ),
                ) as refresh_mapping,
                patch.object(
                    publish,
                    "_copy_submission_to_channel",
                    new=AsyncMock(return_value=forwarded),
                ) as copy_to_channel,
                patch.object(publish, "_append_report_comment") as append_report,
            ):
                result = await publish._publish_comment_and_forward(
                    SimpleNamespace(), submission, config
                )
                return result, refresh_mapping, copy_to_channel, append_report

        result, refresh_mapping, copy_to_channel, append_report = asyncio.run(
            publish_comment()
        )

        self.assertIs(result, forwarded)
        refresh_mapping.assert_awaited_once()
        copy_to_channel.assert_awaited_once()
        self.assertEqual(copy_to_channel.await_args.args[2], -100303)
        append_report.assert_called_once_with(submission, forwarded)
        self.assertTrue(submission["comment_report_only"])
        self.assertIn("讨论组消息", submission["comment_report_only_reason"])

    def test_promoting_backup_updates_default_submission_target(self):
        """Promotion must not leave normal submissions in the retired channel."""
        config = {
            "channel_id": -100101,
            "backup_channel_id": -100202,
            "submission_targets": [{
                "id": 1,
                "name": "默认频道",
                "channel_id": -100101,
                "enabled": True,
            }],
        }
        checkin_data = {"posts": {}}

        with (
            patch.object(publish, "_promotable_backup_post_pairs", return_value=[]),
            patch.object(publish, "_load_comment_reports", return_value={"reports": {}}),
            patch.object(publish, "_load_keyword_map", return_value={}),
            patch.object(publish, "_load_comment_map", return_value={}),
            patch.object(publish, "_load_checkin_posts", return_value=checkin_data),
            patch.object(publish, "load_json", return_value=[]),
            patch.object(publish, "_save_comment_reports"),
            patch.object(publish, "_save_keyword_map"),
            patch.object(publish, "save_json"),
            patch.object(publish, "save_publish_config"),
        ):
            result = publish._promote_backup_channel_to_main(config)

        self.assertEqual(result["new_channel_id"], -100202)
        self.assertEqual(config["channel_id"], -100202)
        self.assertIsNone(config["backup_channel_id"])
        self.assertEqual(config["submission_targets"][0]["channel_id"], -100202)
        self.assertEqual(config["backup_channel_routes"]["-100101"], -100202)

    def test_backup_resolver_uses_the_source_channels_saved_pair(self):
        """Each main channel resolves its own backup without numeric branches."""
        config = {
            "channel_id": -100202,
            "backup_channel_id": -100303,
            "backup_channel_routes": {
                "-100101": -100202,
                "-100202": -100303,
            },
        }

        self.assertEqual(publish._backup_channel_id(config, -100101), -100202)
        self.assertEqual(publish._backup_channel_id(config, -100202), -100303)
        self.assertIsNone(publish._backup_channel_id(config, -100404))

    def test_empty_telethon_post_is_forwarded_instead_of_dropped(self):
        """Unsupported empty posts use a native forward compatibility path."""
        source = SimpleNamespace(message="", media=None)
        listen_client = SimpleNamespace(get_messages=AsyncMock(return_value=source))
        forward_client = SimpleNamespace(
            forward_messages=AsyncMock(return_value=[SimpleNamespace(id=7)])
        )
        context = SimpleNamespace()

        async def mirror():
            with (
                patch.object(
                    publish,
                    "_get_backup_telethon_client",
                    new=AsyncMock(side_effect=[listen_client, forward_client]),
                ),
                patch.object(
                    publish,
                    "_resolve_backup_telethon_target",
                    new=AsyncMock(return_value="backup-target"),
                ),
            ):
                return await publish._mirror_main_post_via_telethon(
                    context,
                    {},
                    -100101,
                    99,
                    -100202,
                )

        result = asyncio.run(mirror())

        self.assertEqual(result[0].id, 7)
        forward_client.forward_messages.assert_awaited_once_with(
            "backup-target", [source]
        )

    def test_protocol_backup_failure_falls_back_to_bot_copy(self):
        """A transient protocol-account failure must not drop the mirror."""
        config = {
            "channel_id": -100101,
            "backup_channel_id": -100202,
            "backup_channel_use_telethon": True,
        }
        copy_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
        context = SimpleNamespace(bot=SimpleNamespace(copy_message=copy_message))

        async def mirror():
            with (
                patch.object(
                    publish,
                    "_mirror_main_post_via_telethon",
                    new=AsyncMock(side_effect=RuntimeError("session unavailable")),
                ),
                patch.object(publish, "_record_backup_post_mappings") as record_mapping,
            ):
                result = await publish._mirror_main_post_to_backup(
                    context, config, -100101, 99
                )
            return result, record_mapping

        result, record_mapping = asyncio.run(mirror())

        self.assertEqual(result.message_id, 7)
        copy_message.assert_awaited_once_with(
            chat_id=-100202, from_chat_id=-100101, message_id=99
        )
        record_mapping.assert_called_once_with(-100101, -100202, [(99, 7)])

    def test_checkin_uses_the_configured_backup_channel(self):
        """Check-in eligibility and online rows must use the backup source."""
        config = {
            "channel_id": -100101,
            "backup_channel_id": -100202,
            "checkin_channel": "backup",
        }
        now_ts = 100
        posts = [
            {
                "channel_id": -100101,
                "message_id": 1,
                "eligible_usernames": ["old_artist"],
                "checkins": {"1": {"expires_at": now_ts + 60}},
            },
            {
                "channel_id": -100202,
                "message_id": 7,
                "eligible_usernames": ["backup_artist"],
                "checkins": {"2": {"expires_at": now_ts + 60}},
            },
        ]

        online = publish._current_main_online_checkin_posts(posts, config, now_ts)

        self.assertEqual(publish._checkin_channel_id(config), -100202)
        self.assertEqual([(post["channel_id"], post["message_id"]) for post in online], [(-100202, 7)])

    def test_keyword_search_uses_the_configured_backup_source(self):
        """Private keyword search must not fall back to stale main indexes."""
        config = {
            "channel_id": -100101,
            "backup_channel_id": -100202,
            "keyword_search_channel": "backup",
            "keyword_post_search_display_mode": "main",
        }
        keyword_data = {
            "小艺": [
                {
                    "label": "艺名",
                    "raw": "小艺",
                    "channel_id": -100101,
                    "channel_message_id": 1,
                    "created_at": 1,
                },
                {
                    "label": "艺名",
                    "raw": "小艺",
                    "channel_id": -100202,
                    "channel_message_id": 7,
                    "created_at": 2,
                },
            ]
        }

        with patch.object(publish, "load_publish_config", return_value=config):
            routes = publish._find_keyword_routes(
                "小艺", require_discussion=False, keyword_data=keyword_data
            )
            display_routes = publish._keyword_post_display_routes(routes, config)

        self.assertEqual([(route["channel_id"], route["channel_message_id"]) for route in routes], [(-100202, 7)])
        self.assertEqual([(route["channel_id"], route["channel_message_id"]) for route in display_routes], [(-100202, 7)])
        self.assertEqual(display_routes[0]["display_channel"], "备用频道")

    def test_backup_roster_mode_links_to_the_configured_backup_channel(self):
        """The roster may intentionally link to mirrors instead of main posts."""
        config = {
            "channel_id": -100101,
            "backup_channel_id": -100202,
            "collection_display_channel": "backup",
            "collection_display_label": "艺名",
        }
        keyword_data = {
            "小艺": [{
                "label": "艺名",
                "raw": "小艺",
                "channel_id": -100202,
                "channel_message_id": 7,
                "created_at": 1,
            }]
        }

        with (
            patch.object(roster, "_load_keyword_map", return_value=keyword_data),
            patch.object(roster, "_load_checkin_posts", return_value={"posts": {}}),
        ):
            posts = publish._load_collection_display_posts(config)

        self.assertEqual(publish._collection_display_channel_id(config), -100202)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["channel_id"], -100202)
        self.assertEqual(posts[0]["message_id"], 7)

    def test_promoted_backup_repairs_teacher_roster_indexes(self):
        """A recorded backup promotion must restore ``老师列表`` immediately."""
        old_channel_id = -100101
        promoted_channel_id = -100202
        config = {
            "channel_id": promoted_channel_id,
            "channel_change_history": [{
                "old_channel_id": old_channel_id,
                "new_channel_id": promoted_channel_id,
            }],
            "collection_display_label": "艺名",
            "collection_display_group_label": "区域",
            "collection_display_subgroup_label": "课费",
        }
        keyword_data = {
            "小艺": [{
                "label": "艺名",
                "raw": "小艺",
                "channel_id": old_channel_id,
                "channel_message_id": 99,
                "created_at": 1,
            }],
            "上海": [{
                "label": "区域",
                "raw": "上海",
                "channel_id": old_channel_id,
                "channel_message_id": 99,
                "created_at": 1,
            }],
            "1000": [{
                "label": "课费",
                "raw": "1000",
                "channel_id": old_channel_id,
                "channel_message_id": 99,
                "created_at": 1,
            }],
        }
        checkin_data = {
            "posts": {
                f"{old_channel_id}:99": {
                    "channel_id": old_channel_id,
                    "message_id": 99,
                    "fields": {"艺名": ["小艺"]},
                    "checkins": {},
                }
            }
        }
        reports_data = {"reports": {}}
        user_posts = []
        backup_map = {
            "mappings": {
                f"{old_channel_id}:99": {
                    "backup_channel_id": promoted_channel_id,
                    "backup_message_id": 7,
                }
            }
        }

        with (
            patch.object(publish, "_load_backup_post_map", return_value=backup_map),
            patch.object(publish, "_load_comment_reports", return_value=reports_data),
            patch.object(publish, "_save_comment_reports"),
            patch.object(publish, "_load_keyword_map", return_value=keyword_data),
            patch.object(publish, "_save_keyword_map"),
            patch.object(publish, "_load_checkin_posts", return_value=checkin_data),
            patch.object(publish, "_save_checkin_posts"),
            patch.object(publish, "_load_comment_map", return_value={}),
            patch.object(publish, "load_json", return_value=user_posts),
            patch.object(publish, "save_json"),
        ):
            catalog = publish._teacher_list_catalog(config)

        self.assertEqual(keyword_data["小艺"][0]["channel_id"], promoted_channel_id)
        self.assertEqual(keyword_data["小艺"][0]["channel_message_id"], 7)
        self.assertNotIn(f"{old_channel_id}:99", checkin_data["posts"])
        self.assertIn(f"{promoted_channel_id}:7", checkin_data["posts"])
        self.assertEqual(catalog[0]["name"], "上海")
        self.assertEqual(catalog[0]["secondary"][0]["name"], "1000")
        self.assertEqual(catalog[0]["secondary"][0]["posts"][0]["message_id"], 7)


if __name__ == "__main__":
    unittest.main()
