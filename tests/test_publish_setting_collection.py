"""Regression coverage for collection updates after channel-post edits."""

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
