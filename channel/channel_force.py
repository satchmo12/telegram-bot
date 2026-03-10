import json
import os
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from command_router import register_command
from utils import load_json, save_json


DATA_FILE = "data/force_subscribe.json"

# 用户提醒冷却
user_warn_cooldown = {}



# ========= 设置强制频道 =========
@register_command("设置频道")
async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat.type.endswith("group"):
        return

    if not context.args:
        await update.message.reply_text("用法：/setchannel @频道用户名")
        return

    channel_username = context.args[0]
    chat_id = str(update.effective_chat.id)

    data = load_json(DATA_FILE)
    data[chat_id] = channel_username
    save_json(DATA_FILE,data)

    await update.message.reply_text(
        f"✅ 已开启强制关注 {channel_username}"
    )


# ========= 关闭强制 =========

async def clear_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    data = load_json(DATA_FILE)

    if chat_id in data:
        del data[chat_id]
        save_json(DATA_FILE, data)
        await update.message.reply_text("✅ 已关闭强制关注")
    else:
        await update.message.reply_text("当前未开启强制关注")


# ========= 发言检测 =========

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    data = load_json(DATA_FILE)

    # 未设置强制
    if chat_id not in data:
        return

    channel_username = data[chat_id]

    try:
        member = await context.bot.get_chat_member(channel_username, user_id)

        if member.status in ["left", "kicked"]:
            await update.message.delete()

            # 冷却机制
            now = time.time()
            if user_id in user_warn_cooldown:
                if now - user_warn_cooldown[user_id] < 30:
                    return

            user_warn_cooldown[user_id] = now

            keyboard = [
                [
                    InlineKeyboardButton(
                        "📢 点击关注频道",
                        url=f"https://t.me/{channel_username.replace('@','')}",
                    )
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "⚠️ 请先关注频道后再发言！",
                reply_markup=reply_markup,
            )

    except Exception as e:
        print("检测失败：", e)


# ========= 主程序 =========

def register_handle_force_handlers(app):
    app.add_handler(CommandHandler("setchannel", set_channel))
    app.add_handler(CommandHandler("clearchannel", clear_channel))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), check_message))


