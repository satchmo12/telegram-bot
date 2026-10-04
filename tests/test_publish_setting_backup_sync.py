from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram import InputMediaPhoto

from channel import publish_setting


class BackupAlbumSyncTests(IsolatedAsyncioTestCase):
    async def test_album_copy_records_one_mapping_for_each_message(self):
        copied_messages = [
            SimpleNamespace(message_id=201),
            SimpleNamespace(message_id=202),
            SimpleNamespace(message_id=203),
        ]
        context = SimpleNamespace(
            bot=SimpleNamespace(copy_messages=AsyncMock(return_value=copied_messages))
        )
        config = {"backup_channel_id": -200}

        with patch.object(
            publish_setting,
            "_record_backup_post_mappings",
        ) as record_mappings:
            copied = await publish_setting._mirror_main_post_to_backup(
                context,
                config,
                -100,
                101,
                main_message_ids=[101, 102, 103],
            )

        self.assertIs(copied, copied_messages[0])
        context.bot.copy_messages.assert_awaited_once_with(
            chat_id=-200,
            from_chat_id=-100,
            message_ids=[101, 102, 103],
        )
        record_mappings.assert_called_once_with(
            -100,
            -200,
            [(101, 201), (102, 202), (103, 203)],
        )

    async def test_photo_edit_replaces_the_mapped_backup_media(self):
        source_message = SimpleNamespace(
            message_id=102,
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="full")],
            video=None,
            caption="updated caption",
            caption_entities=(),
            text=None,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(edit_message_media=AsyncMock())
        )
        config = {"channel_id": -100, "backup_channel_id": -200}

        with patch.object(
            publish_setting,
            "_backup_post_mapping",
            return_value={"backup_channel_id": -200, "backup_message_id": 202},
        ):
            synced = await publish_setting._sync_main_post_edit_to_backup(
                context,
                source_message,
                config,
            )

        self.assertTrue(synced)
        context.bot.edit_message_media.assert_awaited_once()
        kwargs = context.bot.edit_message_media.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -200)
        self.assertEqual(kwargs["message_id"], 202)
        self.assertIsInstance(kwargs["media"], InputMediaPhoto)
        self.assertEqual(kwargs["media"].media, "full")
        self.assertEqual(kwargs["media"].caption, "updated caption")

    async def test_legacy_album_mapping_is_persisted_with_its_inferred_target(self):
        source_message = SimpleNamespace(
            message_id=102,
            media_group_id="album-1",
            photo=[SimpleNamespace(file_id="full")],
            video=None,
            caption="updated caption",
            caption_entities=(),
            text=None,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(edit_message_media=AsyncMock())
        )
        config = {"channel_id": -100, "backup_channel_id": -200}

        with patch.object(publish_setting, "_backup_post_mapping", return_value={}), patch.object(
            publish_setting,
            "_collect_cloned_history_mappings",
            return_value={},
        ), patch.object(
            publish_setting,
            "_infer_legacy_album_backup_mapping",
            return_value={"backup_channel_id": -200, "backup_message_id": 202},
        ), patch.object(publish_setting, "_record_backup_post_mapping") as record_mapping:
            synced = await publish_setting._sync_main_post_edit_to_backup(
                context,
                source_message,
                config,
            )

        self.assertTrue(synced)
        record_mapping.assert_called_once_with(-100, 102, -200, 202)
        self.assertEqual(
            context.bot.edit_message_media.await_args.kwargs["message_id"],
            202,
        )

    async def test_telethon_backup_replaces_media_from_a_bot_api_album_item(self):
        source_message = SimpleNamespace(
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="full")],
            video=None,
            caption="first image caption",
            caption_html="first image caption",
        )
        downloaded_file = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"new-image"))
        )
        forward_client = SimpleNamespace(edit_message=AsyncMock())
        context = SimpleNamespace(
            bot=SimpleNamespace(get_file=AsyncMock(return_value=downloaded_file))
        )

        with patch.object(
            publish_setting,
            "_get_backup_telethon_client",
            new=AsyncMock(return_value=forward_client),
        ):
            await publish_setting._edit_backup_post_via_telethon(
                context,
                {},
                -200,
                201,
                source_message,
            )

        context.bot.get_file.assert_awaited_once_with("full")
        forward_client.edit_message.assert_awaited_once()
        args = forward_client.edit_message.await_args.args
        kwargs = forward_client.edit_message.await_args.kwargs
        self.assertEqual(args[:3], (-200, 201, "first image caption"))
        self.assertEqual(kwargs["file"].getvalue(), b"new-image")
        self.assertEqual(kwargs["file"].name, "edited_photo.jpg")
        self.assertEqual(kwargs["parse_mode"], "html")
