import os
import re
import time
from datetime import datetime

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from command_router import register_command
from tool.pagination_helper import send_paginated_list
from utils import (
    SPECIAL_FOLLOW_FILE,
    _resolve_json_path,
    group_allowed,
    is_admin,
    load_json,
    safe_reply,
    save_json,
)

# ---------------- 配置 ----------------
GROUP_LOG_DIR = "data/group_logs"
_notify_cd: dict[tuple[int, int], float] = {}
COOLDOWN = 60 * 5  # 5 分钟


# ---------------- 特别关注 ----------------
def get_special_follow() -> dict:
    raw = load_json(SPECIAL_FOLLOW_FILE)
    result: dict[int, dict[int, set[int]]] = {}
    for chat_id, watchers in raw.items():
        result[int(chat_id)] = {
            int(watcher): set(follow_list) for watcher, follow_list in watchers.items()
        }
    return result


def save_special_follow(data: dict[int, dict[int, set[int]]]):
    out = {
        str(chat_id): {
            str(watcher): list(follow_set) for watcher, follow_set in watchers.items()
        }
        for chat_id, watchers in data.items()
    }
    save_json(SPECIAL_FOLLOW_FILE, out)


@group_allowed
@register_command("关注", "特别关心")
async def follow_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message:
        await safe_reply(update, context, "请回复要关注的那个人的消息")
        return

    target = msg.reply_to_message.from_user
    if not target or target.is_bot:
        await safe_reply(update, context, "不能关注机器人")
        return

    chat_id = msg.chat_id
    watcher_id = msg.from_user.id

    if target.id == watcher_id:
        await safe_reply(update, context, "🙅 不能关注自己")
        return

    data = get_special_follow()
    follow_set = data.setdefault(chat_id, {}).setdefault(watcher_id, set())

    if target.id in follow_set:
        await safe_reply(update, context, f"⚠️ 已经关注 {target.full_name}")
        return

    follow_set.add(target.id)
    save_special_follow(data)
    await safe_reply(update, context, f"✅ 已关注 {target.full_name}")


@group_allowed
@register_command("取关", "取消关注")
async def unfollow_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message:
        await safe_reply(update, context, "请回复要取关的那个人的消息")
        return

    target = msg.reply_to_message.from_user
    if not target or target.is_bot:
        await safe_reply(update, context, "不能取关机器人")
        return

    chat_id = msg.chat_id
    watcher_id = msg.from_user.id
    if target.id == watcher_id:
        await safe_reply(update, context, "🙅 不能取关自己")
        return

    data = get_special_follow()
    follow_set = data.get(chat_id, {}).get(watcher_id, set())

    if target.id not in follow_set:
        await safe_reply(update, context, f"⚠️ 你没有关注 {target.full_name}")
        return

    follow_set.remove(target.id)
    save_special_follow(data)
    await safe_reply(update, context, f"❌ 已取关 {target.full_name}")


@group_allowed
@register_command("视奸列表")
async def list_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watcher_id = update.effective_user.id
    data = get_special_follow()
    follow_set = data.get(chat_id, {}).get(watcher_id, set())

    if not follow_set:
        await safe_reply(update, context, "📭 当前没有关注任何人")
        return

    names = []
    for uid in follow_set:
        try:
            user = await context.bot.get_chat(uid)
            names.append(user.full_name)
        except:
            names.append(str(uid))
    await safe_reply(update, context, "👀 当前关注：\n" + "\n".join(names))


async def watch_special_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat_id
    speaker_id = msg.from_user.id
    data = get_special_follow()
    watchers = data.get(chat_id)
    if not watchers:
        return
    for watcher_id, follow_set in watchers.items():
        if speaker_id not in follow_set:
            continue
        key = (chat_id, watcher_id)
        now = time.time()
        if now - _notify_cd.get(key, 0) < COOLDOWN:
            continue
        _notify_cd[key] = now
        mention = f"<a href='tg://user?id={watcher_id}'>@你</a>"
        await safe_reply(
            update,
            context,
            f"{mention}\n🔔 {msg.from_user.full_name} 发消息了",
            html=True,
        )


# ---------------- 群消息记录 ----------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = msg.chat_id
    user = msg.from_user
    timestamp = int(msg.date.timestamp())
    date_str = msg.date.strftime("%Y-%m-%d")
    datetime_str = msg.date.strftime("%Y-%m-%d %H:%M:%S")

    group_folder = os.path.join(GROUP_LOG_DIR, str(chat_id))
    os.makedirs(group_folder, exist_ok=True)
    file_path = os.path.join(group_folder, f"{date_str}.json")

    data = load_json(file_path)
    data.setdefault("messages", [])
    data["messages"].append(
        {
            "message_id": msg.message_id,
            "user_id": user.id,
            "user_name": user.full_name,
            "text": msg.text,
            "timestamp": timestamp,
            "date": date_str,
            "datetime": datetime_str,
        }
    )
    save_json(file_path, data)


def _get_today_chat_items(chat_id: str) -> list[str]:
    date_str = datetime.now().strftime("%Y-%m-%d")
    # _resolve_json_path
    file_path = os.path.join(GROUP_LOG_DIR, chat_id, f"{date_str}.json")
    file_path =  _resolve_json_path(file_path)
    if not os.path.exists(file_path):
        return []

    data = load_json(file_path)
    messages = data.get("messages", [])
    if not messages:
        return []

    return [f"[{m['datetime']}] {m['user_name']}：{m['text']}" for m in reversed(messages)]


# ---------------- 今天群聊 ----------------
@register_command("今天群聊")
async def cmd_today_group_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not await is_admin(update, context):
        await safe_reply(update, context, "⚠️ 仅管理员可用")
        return

    chat_id = str(update.effective_chat.id)
    items = _get_today_chat_items(chat_id)
    if not items:
        await safe_reply(update, context, "📭 今天还没有群聊天记录")
        return

    prefix = f"chatlog_{chat_id}"  # ✅ 每个群单独前缀

    # 第一次发送第一页
    await send_paginated_list(
        update, context, items, page=1, prefix=prefix, title="📖 今日群聊记录"
    )


# ---------------- 分页回调 ----------------
async def send_today_group_log_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # callback_data 格式: chatlog_<chat_id>_<page>
    pattern = r"^chatlog_(\d+)_(\d+)$"
    match = re.match(pattern, query.data)
    if not match:
        return

    chat_id, page = match.groups()
    page = int(page)

    items = _get_today_chat_items(chat_id)
    if not items:
        await query.message.edit_text("📭 今天还没有群聊天记录")
        return

    prefix = f"chatlog_{chat_id}"  # 与发送列表时一致

    # ✅ 发送对应页
    await send_paginated_list(
        update, context, items, page=page, prefix=prefix, title="📖 今日群聊记录"
    )


# ---------------- 注册回调 ----------------
def register_group_care_handlers(app):
    app.add_handler(CommandHandler("list_follow", list_follow))
    app.add_handler(CommandHandler("unfollow_user", unfollow_user))
    app.add_handler(CommandHandler("follow_user", follow_user))
    # 分页回调，匹配 chatlog_<chat_id>_<page>
    app.add_handler(
        CallbackQueryHandler(send_today_group_log_page, pattern=r"^chatlog_\d+_\d+$")
    )
