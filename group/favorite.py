import os
from datetime import datetime

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler

from command_router import register_command
from utils import load_json, save_json

FAVORITE_FILE = "data/favorites.json"


def get_user_favorites(user_id: str):
    data = load_json(FAVORITE_FILE)
    if user_id not in data:
        data[user_id] = []
    return data


@register_command("收藏")
async def favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message.reply_to_message:
        await update.message.reply_text("请回复一条消息后发送【收藏】。")
        return
    reply = update.message.reply_to_message

    user_id = str(update.effective_user.id)

    data = get_user_favorites(user_id)

    item = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    # 文本
    if reply.text:
        item["type"] = "text"
        item["text"] = reply.text

    # 图片
    elif reply.photo:
        item["type"] = "photo"
        item["file_id"] = reply.photo[-1].file_id
        item["caption"] = reply.caption or ""

    # 语音
    elif reply.voice:
        item["type"] = "voice"
        item["file_id"] = reply.voice.file_id
        item["duration"] = reply.voice.duration

    # 视频
    elif reply.video:
        item["type"] = "video"
        item["file_id"] = reply.video.file_id
        item["caption"] = reply.caption or ""

    # 文件
    elif reply.document:
        item["type"] = "document"
        item["file_id"] = reply.document.file_id
        item["file_name"] = reply.document.file_name

    # 音乐
    elif reply.audio:
        item["type"] = "audio"
        item["file_id"] = reply.audio.file_id
        item["title"] = reply.audio.title
        item["performer"] = reply.audio.performer

    # GIF
    elif reply.animation:
        item["type"] = "animation"
        item["file_id"] = reply.animation.file_id

    # 贴纸
    elif reply.sticker:
        item["type"] = "sticker"
        item["file_id"] = reply.sticker.file_id
        item["emoji"] = reply.sticker.emoji

    else:
        await update.message.reply_text("暂不支持收藏这种消息类型。")
        return

    data[user_id].append(item)

    save_json(FAVORITE_FILE, data)

    await update.message.reply_text(
        f"⭐ 收藏成功！\n编号：{len(data[user_id])}"
    )


@register_command("收藏夹")
async def favorite_list(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = str(update.effective_user.id)

    data = load_json(FAVORITE_FILE)

    if user_id not in data or len(data[user_id]) == 0:
        await update.message.reply_text("收藏夹为空。")
        return

    text = "⭐ 收藏夹\n\n"

    for i, item in enumerate(data[user_id], start=1):

        icon = {
            "text": "📝",
            "photo": "🖼",
            "voice": "🎤",
            "video": "🎥",
            "document": "📄",
            "audio": "🎵",
            "animation": "🎞",
            "sticker": "😀",
        }.get(item["type"], "📦")

        text += f"{i}. {icon} {item['type']}   {item['time']}\n"

    await update.message.reply_text(text)


@register_command("查看收藏")
async def favorite_show(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text("用法：查看收藏 编号")
        return

    index = int(context.args[0]) - 1

    user_id = str(update.effective_user.id)

    data = load_json(FAVORITE_FILE)

    if user_id not in data:
        return

    if index < 0 or index >= len(data[user_id]):
        await update.message.reply_text("编号不存在")
        return

    item = data[user_id][index]

    t = item["type"]

    if t == "text":
        await update.message.reply_text(item["text"])

    elif t == "photo":
        await context.bot.send_photo(
            update.effective_chat.id,
            photo=item["file_id"],
            caption=item.get("caption", "")
        )

    elif t == "voice":
        await context.bot.send_voice(
            update.effective_chat.id,
            voice=item["file_id"]
        )

    elif t == "video":
        await context.bot.send_video(
            update.effective_chat.id,
            video=item["file_id"],
            caption=item.get("caption", "")
        )

    elif t == "document":
        await context.bot.send_document(
            update.effective_chat.id,
            document=item["file_id"]
        )

    elif t == "audio":
        await context.bot.send_audio(
            update.effective_chat.id,
            audio=item["file_id"]
        )

    elif t == "animation":
        await context.bot.send_animation(
            update.effective_chat.id,
            animation=item["file_id"]
        )

    elif t == "sticker":
        await context.bot.send_sticker(
            update.effective_chat.id,
            sticker=item["file_id"]
        )


@register_command("删除收藏")
async def favorite_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text("用法：删除收藏 编号")
        return

    index = int(context.args[0]) - 1

    user_id = str(update.effective_user.id)

    data = load_json(FAVORITE_FILE)

    if user_id not in data:
        return

    if index < 0 or index >= len(data[user_id]):
        await update.message.reply_text("编号不存在")
        return

    data[user_id].pop(index)

    save_json(FAVORITE_FILE, data)

    await update.message.reply_text("🗑 收藏已删除。")
    
def register_favorite(app):

    app.add_handler(CommandHandler("favorite", favorite))
    app.add_handler(CommandHandler("favorite_list", favorite_list))
    app.add_handler(CommandHandler("favorite_show", favorite_show))
    
    app.add_handler(CommandHandler("favorite_delete", favorite_delete))
