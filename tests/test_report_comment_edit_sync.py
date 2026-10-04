from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from channel import publish_setting


class ReportCommentEditSyncTests(IsolatedAsyncioTestCase):
    async def test_sync_updates_forward_main_and_backup_comment_copies(self):
        context = SimpleNamespace(
            bot=SimpleNamespace(edit_message_text=AsyncMock())
        )
        comment = {
            "forward_channel_id": -1001,
            "forward_message_id": 101,
            "main_discussion_chat_id": -1002,
            "main_discussion_message_id": 102,
            "backup_discussion_chat_id": -1003,
            "backup_discussion_message_id": 103,
        }

        synced, untracked, failed = await publish_setting._sync_report_comment_edit(
            context,
            comment,
            "管理员修订后的评论",
        )

        self.assertEqual((synced, untracked, failed), (3, 0, 0))
        self.assertEqual(context.bot.edit_message_text.await_count, 3)
        self.assertEqual(
            {
                (call.kwargs["chat_id"], call.kwargs["message_id"])
                for call in context.bot.edit_message_text.await_args_list
            },
            {(-1001, 101), (-1002, 102), (-1003, 103)},
        )

    async def test_new_comment_record_keeps_all_public_message_ids(self):
        report = {
            "channel_id": -10010,
            "message_id": 77,
            "comments": [],
        }
        data = {"reports": {"report-1": report}, "aliases": {}}
        submission = {
            "comment_target": {"channel_id": -10010, "message_id": 77},
            "report_author": "管理员",
            "report_content": "原评论",
            "forward_channel_id": -10011,
            "main_discussion_chat_id": -10012,
            "main_discussion_message_id": 78,
            "backup_discussion_chat_id": -10013,
            "backup_discussion_message_id": 79,
        }

        with patch.object(publish_setting, "_load_comment_reports", return_value=data), patch.object(
            publish_setting,
            "_save_comment_reports",
        ) as save_reports:
            publish_setting._append_report_comment(
                submission,
                SimpleNamespace(message_id=80),
            )

        self.assertEqual(report["comments"], [{
            "author": "管理员",
            "content": "原评论",
            "forward_channel_id": -10011,
            "forward_message_id": 80,
            "main_discussion_chat_id": -10012,
            "main_discussion_message_id": 78,
            "backup_discussion_chat_id": -10013,
            "backup_discussion_message_id": 79,
            "created_at": report["comments"][0]["created_at"],
        }])
        save_reports.assert_called_once_with(data)

    async def test_editor_saves_content_and_syncs_recorded_targets(self):
        comment = {
            "author": "管理员",
            "content": "旧评论",
            "forward_channel_id": -1001,
            "forward_message_id": 101,
        }
        data = {
            "reports": {
                "report-1": {"comments": [comment]},
            },
            "aliases": {},
        }
        message = SimpleNamespace(
            text="新评论",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(
            user_data={
                publish_setting.REPORT_COMMENT_EDIT_KEY: {
                    "report_id": "report-1",
                    "page": 1,
                    "index": 0,
                }
            },
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        with patch.object(publish_setting, "_can_manage_report_comments", return_value=True), patch.object(
            publish_setting, "_load_comment_reports", return_value=data
        ), patch.object(publish_setting, "_save_comment_reports") as save_reports, patch.object(
            publish_setting,
            "_sync_report_comment_edit",
            new=AsyncMock(return_value=(1, 2, 0)),
        ) as sync_comment, patch.object(
            publish_setting,
            "_report_comment_detail_view",
            return_value=("", None),
        ):
            handled = await publish_setting._handle_report_comment_edit_input(update, context)

        self.assertTrue(handled)
        self.assertEqual(comment["content"], "新评论")
        save_reports.assert_called_once_with(data)
        sync_comment.assert_awaited_once_with(context, comment, "新评论")
        message.reply_text.assert_awaited_once_with(
            "✅ 评论内容已修改，并同步到 1 个位置。\n"
            "⚠️ 2 个历史位置未保存消息定位，无法安全同步。"
        )
