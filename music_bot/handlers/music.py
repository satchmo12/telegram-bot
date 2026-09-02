from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from command_router import register_command


def create_music_handlers(music_service):

    @register_command("点歌")
    async def music_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not update.message:
            return

        if not context.args:
            await update.message.reply_text(
                "🎵 用法：/点歌 歌曲名"
            )
            return

        keyword = " ".join(context.args)

        await update.message.reply_text(
            f"🔍 正在搜索：{keyword}"
        )

        try:
            songs = await music_service.search(keyword, 10)
        except Exception as e:
            print(f"Music search error: {e}")
            await update.message.reply_text(
                "❌ 搜索失败，请稍后再试"
            )
            return

        if not songs:
            await update.message.reply_text(
                "❌ 没有找到歌曲"
            )
            return

        keyboard = []

        for i, song in enumerate(songs):
            text = f"{i + 1}. {song['name']} - {song['artist']}"

            keyboard.append([
                InlineKeyboardButton(
                    text,
                    callback_data=f"music:{song['provider_song_id']}",
                )
            ])

        await update.message.reply_text(
            f"🎵 搜索结果：{keyword}\n\n点击歌曲加入播放队列：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        
    

    # async def music_callback(
    #     update: Update,
    #     context: ContextTypes.DEFAULT_TYPE,
    # ):
    #     query = update.callback_query

    #     if not query or not query.message:
    #         return

    #     await query.answer()

    #     action, value = query.data.split(":", 1)

    #     if action != "music":
    #         return

    #     chat_id = query.message.chat.id

    #     # =========================
    #     # 跳过
    #     # =========================
    #     if value == "skip":
    #         song = await music_service.skip(chat_id)

    #         await query.edit_message_text(
    #             f"⏭ 已跳过\n"
    #             f"🎵 {song.name} - {song.artist}"
    #             if song
    #             else "📭 没有下一首"
    #         )
    #         return

    #     # =========================
    #     # 清空
    #     # =========================
    #     if value == "clear":
    #         await music_service.clear_queue(chat_id)

    #         await query.edit_message_text(
    #             "🗑 播放队列已清空"
    #         )
    #         return

    #     # =========================
    #     # 加入播放队列
    #     # =========================
    #     user_id = query.from_user.id

    #     song = await music_service.add_to_queue(
    #         chat_id,
    #         value,
    #         user_id,
    #     )

    #     if not song:
    #         await query.edit_message_text(
    #             "❌ 歌曲已经失效，请重新搜索"
    #         )
    #         return

    #     queue = music_service.get_queue(chat_id)

    #     # add_to_queue 返回的是 dict
    #     await query.edit_message_text(
    #         f"🎵 {song['name']} - {song['artist']}\n\n"
    #         f"✅ 已加入播放队列\n"
    #         f"📋 当前排队：{queue.size()} 首"
    #     )

    #     # 当前没有播放，自动播放
    #     if not music_service.player.is_playing(chat_id):
    #         await music_service.play_next(chat_id)
    
    

    # =========================
    # 歌单
    # =========================
    @register_command("歌单")
    async def queue_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not update.message:
            return

        chat_id = update.effective_chat.id
        queue = music_service.get_queue(chat_id)

        lines = []

        # 当前播放
        if queue.current:
            lines.append(
                f"🎵 正在播放\n"
                f"{queue.current.name} - {queue.current.artist}"
            )

        # 排队歌曲
        items = queue.list()

        if items:
            lines.append("\n📋 播放队列")

            for i, song in enumerate(items, 1):
                lines.append(
                    f"{i}. {song.name} - {song.artist}"
                )

        if not lines:
            lines.append("📭 播放队列为空")

        keyboard = [[
            InlineKeyboardButton(
                "⏭ 下一首",
                callback_data="music:skip",
            ),
            InlineKeyboardButton(
                "🗑 清空",
                callback_data="music:clear",
            ),
        ]]

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # =========================
    # 跳过
    # =========================
    @register_command("跳过")
    async def skip_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        chat_id = update.effective_chat.id

        song = await music_service.skip(chat_id)

        await update.message.reply_text(
            f"⏭ 下一首：{song.name} - {song.artist}"
            if song
            else "📭 没有下一首"
        )

    # =========================
    # 暂停
    # =========================
    @register_command("暂停")
    async def pause_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        await music_service.pause(
            update.effective_chat.id
        )

        await update.message.reply_text(
            "⏸ 已暂停"
        )

    # =========================
    # 继续
    # =========================
    @register_command("继续")
    async def resume_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        await music_service.resume(
            update.effective_chat.id
        )

        await update.message.reply_text(
            "▶️ 已继续"
        )

    # =========================
    # 清空
    # =========================
    @register_command("清空")
    async def clear_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        await music_service.clear_queue(
            update.effective_chat.id
        )

        await update.message.reply_text(
            "🗑 队列已清空"
        )

    # =========================
    # 离开
    # =========================
    @register_command("离开")
    async def leave_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        await music_service.leave(
            update.effective_chat.id
        )

        await update.message.reply_text(
            "👋 已离开语音聊天"
        )
        
    async def music_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        query = update.callback_query

        if not query or not query.message:
            return

        await query.answer()

        action, value = query.data.split(":", 1)

        if action != "music":
            return

        chat_id = query.message.chat.id

        # =========================
        # 搜索结果 → 点击歌曲
        # =========================
        await query.edit_message_text(
            "⏳ 正在准备歌曲，请稍候..."
        )

        try:
            song = await music_service.get_song(value)

            if not song:
                await query.edit_message_text(
                    "❌ 歌曲已失效，请重新搜索"
                )
                return

            # 下载歌曲
            audio_file = await music_service.download(song)

            if not audio_file:
                await query.edit_message_text(
                    "❌ 歌曲下载失败"
                )
                return

            try:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=song["name"],
                    performer=song["artist"],
                    caption="来自satchmo的爱"
                )
            finally:
                audio_file.close()

            try:
                await query.delete_message()
            except Exception as e:
                print(f"Music result cleanup error: {e}")

        except Exception as e:
            print(f"Music send error: {e}")

            await query.edit_message_text(
                "❌ 歌曲发送失败，请稍后再试"
            )

    return [
        CommandHandler("music", music_command),
        CommandHandler("playlist", queue_command),
        CommandHandler("skip", skip_command),
        CommandHandler("pause", pause_command),
        CommandHandler("resume", resume_command),
        CommandHandler("clear", clear_command),
        CommandHandler("leave", leave_command),

        CallbackQueryHandler(
            music_callback,
            pattern=r"^music:",
        ),
    ]
