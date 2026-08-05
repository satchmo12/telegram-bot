from datetime import datetime
import json
import os
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

from command_router import register_command
from utils import load_json, save_json

NOTE_FILE = "data/notes.json"


@register_command("笔记")
async def note(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message

    # 优先保存回复的消息
    if message.reply_to_message:
        reply_msg = message.reply_to_message

        if reply_msg.text:
            content = reply_msg.text
        elif reply_msg.caption:
            content = reply_msg.caption
        else:
            await message.reply_text("❌ 回复的消息没有可保存的文字内容。")
            return

    else:
        if not context.args:
            await message.reply_text(
                "用法：\n"
                "笔记 今天下午开会\n"
                "或回复一条消息后发送：笔记"
            )
            return

    content = " ".join(context.args)
        

    notes = load_json(NOTE_FILE)

    user_id = str(update.effective_user.id)

    if user_id not in notes:
        notes[user_id] = []

    notes[user_id].append({
    "content": content,
    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
})

    save_json(NOTE_FILE, notes)

    await update.message.reply_text(
        f"✅ 已保存\n\n编号：{len(notes[user_id])}\n内容：{content}"
    )

@register_command("笔记本")
async def notes(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = str(update.effective_user.id)

    data = load_json(NOTE_FILE)

    if user_id not in data or len(data[user_id]) == 0:
        await update.message.reply_text("暂无笔记")
        return

    text = "📝 我的记事本\n\n"

    for i, item in enumerate(data[user_id], start=1):
        text += (
        f"{i}. {item['content']}\n"
        f"📅 {item['time']}\n\n"
    )

    await update.message.reply_text(text)

@register_command("删除笔记")
async def delete_note(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text("/del 编号")
        return

    try:
        index = int(context.args[0]) - 1
    except:
        await update.message.reply_text("编号错误")
        return

    user_id = str(update.effective_user.id)

    data = load_json(NOTE_FILE)

    if user_id not in data:
        await update.message.reply_text("没有任何笔记")
        return

    if index < 0 or index >= len(data[user_id]):
        await update.message.reply_text("编号不存在")
        return

    removed = data[user_id].pop(index)

    save_json(NOTE_FILE, notes)

    await update.message.reply_text(
        f"🗑 已删除：\n{removed}"
    )
@register_command("编辑笔记")
async def edit_note(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text(
            "用法：\n/edit 编号"
        )
        return

    try:
        index = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("编号错误")
        return

    user_id = str(update.effective_user.id)

    data = load_json(NOTE_FILE)

    if user_id not in data:
        await update.message.reply_text("暂无笔记")
        return

    if index < 0 or index >= len(data[user_id]):
        await update.message.reply_text("编号不存在")
        return

    context.user_data["editing_note"] = index

    await update.message.reply_text(
        f"正在编辑第 {index+1} 条：\n\n"
        f"{data[user_id][index]}\n\n"
        "请直接发送新的内容。"
    )

async def receive_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if "editing_note" not in context.user_data:
        return

    index = context.user_data.pop("editing_note")

    user_id = str(update.effective_user.id)

    data = load_json(NOTE_FILE)

    if user_id not in data:
        return

    old = data[user_id][index]

    new = update.message.text

    data[user_id][index] = new
    data[user_id][index]["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json(NOTE_FILE, notes)

    await update.message.reply_text(
        f"✏️ 修改成功\n\n"
        f"原内容：{old}\n\n"
        f"新内容：{new}"
    )

def register_note(app):

    app.add_handler(CommandHandler("note", note))
    app.add_handler(CommandHandler("notes", notes))
    app.add_handler(CommandHandler("del", delete_note))
    
    app.add_handler(CommandHandler("edit", edit_note))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_edit,
        )
    )