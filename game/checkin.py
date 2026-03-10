from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from datetime import datetime
import os
import json
from command_router import register_command
from info.economy import change_points, get_points
from utils import CHECKIN_FILE, load_json, safe_reply, save_json


@register_command("签到")
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    today = datetime.utcnow().strftime("%Y-%m-%d")

    checkin_data = load_json(CHECKIN_FILE)

    if chat_id not in checkin_data:
        checkin_data[chat_id] = {}
    if today not in checkin_data[chat_id]:
        checkin_data[chat_id][today] = {}

    if str(user.id) in checkin_data[chat_id][today]:
        return await safe_reply(update, context,
            f"✅ {user.first_name}，你今天已经签到过了！"
        )

    # 记录签到
    checkin_data[chat_id][today][str(user.id)] = user.full_name
    save_json(CHECKIN_FILE, checkin_data)

    # 加积分
    change_points(chat_id, user.id, 2)
    points = get_points(chat_id, user.id)
    await safe_reply(update, context,
        f"🎉 签到成功，{user.full_name}！你获得了 2 积分，当前积分：{points} 🎯"
    )


@register_command("签到统计")
async def checkin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    data = load_json(CHECKIN_FILE)

    if chat_id not in data or today not in data[chat_id] or not data[chat_id][today]:
        return await safe_reply(update, context,"📭 今天还没有人签到。")

    names = list(data[chat_id][today].values())
    text = f"📋 今日已签到 {len(names)} 人：\n" + "\n".join(
        f"- {name}" for name in names
    )
    await safe_reply(update, context,text)


def register_checkin_handlers(app):
    app.add_handler(CommandHandler("qiandao", checkin))
    app.add_handler(CommandHandler("sate", checkin_status))
