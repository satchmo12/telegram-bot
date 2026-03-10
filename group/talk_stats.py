# talk_stats.py
import os
import re
import time
from html import escape
from collections import deque

from telegram import ChatPermissions, Update
from telegram.ext import MessageHandler, CommandHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.helpers import mention_html
from datetime import datetime, timedelta
import calendar
from command_router import register_command
from tool.pagination_helper import generic_pagination_callback, send_paginated_list
from utils import get_group_whitelist, is_admin, is_bot_admin, load_json, save_json, safe_reply

DATA_FILE = "data/talk_count.json"
FREQ_SHORT_WINDOW_SECONDS = 10
FREQ_LONG_WINDOW_SECONDS = 60
DEFAULT_MAX_MSG_PER_MINUTE = 10  # 默认每分钟最多 10 条
AUTO_MUTE_SECONDS = 60    # 超阈值后自动禁言 60 秒

# 全局缓存，用于回调分页使用
CHAT_TALK_COUNTS = {}  # 结构: { chat_id: { mode: [(name, user_id, count), ...] } }
USER_FREQ_CACHE = {}  # key: f"{chat_id}:{user_id}" -> deque[timestamp]
SPAM_WARN_CACHE = {}  # key: f"{chat_id}:{user_id}" -> last_warn_ts
SPAM_MUTE_UNTIL = {}  # key: f"{chat_id}:{user_id}" -> ts
SEEN_MESSAGES = {}  # key: f"{chat_id}:{message_id}" -> ts


def _is_chat_silent(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> bool:
    cfg = get_group_whitelist(context).get(str(chat_id), {})
    return bool(cfg.get("silent", False))


def _get_user_freq_queue(chat_id: str, user_id: str) -> deque:
    key = f"{chat_id}:{user_id}"
    q = USER_FREQ_CACHE.get(key)
    if q is None:
        q = deque()
        USER_FREQ_CACHE[key] = q
    return q


def _cleanup_queue(q: deque, now_ts: float):
    cutoff = now_ts - FREQ_LONG_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()


def _count_in_window(q: deque, now_ts: float, window_seconds: int) -> int:
    cutoff = now_ts - window_seconds
    return sum(1 for t in q if t >= cutoff)


def _mark_message_seen(chat_id: str, message_id: int, now_ts: float) -> bool:
    key = f"{chat_id}:{message_id}"
    if key in SEEN_MESSAGES:
        return False
    SEEN_MESSAGES[key] = now_ts

    # 控制内存：清理 120 秒前的消息
    expire = now_ts - 120
    stale_keys = [k for k, ts in SEEN_MESSAGES.items() if ts < expire]
    for k in stale_keys:
        SEEN_MESSAGES.pop(k, None)
    return True

# 加载数据
def load_talk_data():
    data = load_json(DATA_FILE)
    return data if isinstance(data, dict) else {}


# 保存数据
def save_talk_data(data):
    save_json(DATA_FILE, data)


# 消息计数
async def count_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type == "private":
        return
    if not update.effective_user or update.effective_user.is_bot:
        return

    now = datetime.now()
    now_ts = time.time()
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    user_id = str(user.id)
    if update.message.message_id and not _mark_message_seen(
        chat_id, update.message.message_id, now_ts
    ):
        return
    date_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    data = load_talk_data()
    # print(f"📊 记录发言：{chat_id} - {user.full_name}")
    if chat_id not in data:
        data[chat_id] = {}

    if user_id not in data[chat_id]:
        data[chat_id][user_id] = {"name": user.full_name, "daily": {}, "monthly": {}}

    user_data = data[chat_id][user_id]
    user_data["name"] = user.full_name  # 更新最新名字

    # 增加每日计数
    user_data["daily"][date_key] = user_data["daily"].get(date_key, 0) + 1
    
    # 保留最近 31 天记录
    if len(user_data["daily"]) > 31:
        # 按日期升序排序，取最后 31 个
        sorted_keys = sorted(user_data["daily"].keys())
        keep_keys = sorted_keys[-31:]
        user_data["daily"] = {k: user_data["daily"][k] for k in keep_keys}
    
    
    # 增加每月计数
    user_data["monthly"][month_key] = user_data["monthly"].get(month_key, 0) + 1
    

    save_talk_data(data)

    # ===== 实时频率统计（用于防刷屏）=====
    q = _get_user_freq_queue(chat_id, user_id)
    q.append(now_ts)
    _cleanup_queue(q, now_ts)

    per_minute_count = _count_in_window(q, now_ts, FREQ_LONG_WINDOW_SECONDS)
    group_config = get_group_whitelist(context).get(chat_id, {})
    spam_limit_enabled = bool(group_config.get("spam_limit", False))
    if not spam_limit_enabled:
        return
    try:
        max_msg_per_minute = int(
            group_config.get("spam_limit_max_per_minute", DEFAULT_MAX_MSG_PER_MINUTE)
        )
    except Exception:
        max_msg_per_minute = DEFAULT_MAX_MSG_PER_MINUTE
    if max_msg_per_minute < 1:
        max_msg_per_minute = DEFAULT_MAX_MSG_PER_MINUTE

    spam_key = f"{chat_id}:{user_id}"
    muted_until = SPAM_MUTE_UNTIL.get(spam_key, 0.0)
    if now_ts <= muted_until:
        return

    # 超过每分钟阈值（第 11 条触发）
    if per_minute_count > max_msg_per_minute:
        if await is_admin(update, context):
            return
        if update.effective_chat.type != "supergroup":
            return
        if not await is_bot_admin(update, context):
            return
        try:
            until_date = update.message.date + timedelta(seconds=AUTO_MUTE_SECONDS)
            await context.bot.restrict_chat_member(
                update.effective_chat.id,
                int(user_id),
                ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
            SPAM_MUTE_UNTIL[spam_key] = now_ts + AUTO_MUTE_SECONDS
            await safe_reply(
                update,
                context,
                (
                    f"🚫 {user.full_name} 1分钟内发言超过 {max_msg_per_minute} 条，"
                    f"已禁言 {AUTO_MUTE_SECONDS} 秒。"
                ),
            )
        except Exception:
            return
    
# @register_command("发言排行", "发言统计")
# async def talk_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     args = context.args
#     now = datetime.now()
#     today = now.strftime("%Y-%m-%d")
#     this_month = now.strftime("%Y-%m")
#     yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

#     # 默认统计今天
#     mode = "today"

#     if args:
#         if args[0] in ["月", "month"]:
#             mode = "month"
#         elif args[0] in ["昨日", "yesterday"]:
#             mode = "yesterday"
#         elif args[0] in ["周", "week"]:
#             mode = "week"

#     chat_id = str(update.effective_chat.id)
#     data = load_talk_data()

#     if chat_id not in data:
#         output = "暂无发言记录。"
#         if update.message:
#             await update.message.reply_text(output)
#         else:
#             await context.bot.send_message(
#                 chat_id=update.effective_chat.id, text=output
#             )
#         return

#     counts = []
#     for user_id, user_data in data[chat_id].items():
#         name = user_data["name"]
#         count = 0

#         if mode == "today":
#             count = user_data["daily"].get(today, 0)
#         elif mode == "yesterday":
#             count = user_data["daily"].get(yesterday, 0)
#         elif mode == "month":
#             count = user_data["monthly"].get(this_month, 0)
#         elif mode == "week":
#             # 获取本周周一的日期
#             start_of_week = now - timedelta(days=now.weekday())
#             for i in range((now - start_of_week).days + 1):
#                 day_str = (start_of_week + timedelta(days=i)).strftime("%Y-%m-%d")
#                 count += user_data["daily"].get(day_str, 0)

#         if count > 0:
#             counts.append((name, user_id, count))

#     if not counts:
#         output = "暂无发言记录。"
#         if update.message:
#             await update.message.reply_text(output)
#         else:
#             await context.bot.send_message(
#                 chat_id=update.effective_chat.id, text=output
#             )
#         return

#     counts.sort(key=lambda x: x[2], reverse=True)

#     if mode == "today":
#         title = "📅 今日发言排行榜："
#     elif mode == "yesterday":
#         title = "📅 昨日发言排行榜："
#     elif mode == "week":
#         title = "📅 本周发言排行榜："
#     else:
#         title = "📅 本月发言排行榜："

#     msg = [title]
#     for i, (name, user_id, count) in enumerate(counts[:10], 1):
#         mention = mention_html(user_id, name or "未知用户")
#         msg.append(f"{i}. {mention}：{count} 条")

#     output = "\n".join(msg)
#     if update.message:
#         await safe_reply(update, context, output, True)
#     else:
#         await context.bot.send_message(chat_id=update.effective_chat.id, text=output)


# ====== 发言排行榜命令 ======
@register_command("发言排行", "发言统计")
async def talk_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    mode = "today"
    if args:
        if args[0] in ["月", "month"]:
            mode = "month"
        elif args[0] in ["昨日", "yesterday"]:
            mode = "yesterday"
        elif args[0] in ["周", "week"]:
            mode = "week"

    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)
    data = load_talk_data()  # 你的原始数据加载函数

    if chat_id not in data:
        output = "暂无发言记录。"
        if update.message:
            await update.message.reply_text(output)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=output)
        return

    counts = []
    for user_id, user_data in data[chat_id].items():
        name = user_data.get("name") or "未知用户"
        count = 0

        if mode == "today":
            count = user_data["daily"].get(today, 0)
        elif mode == "yesterday":
            count = user_data["daily"].get(yesterday, 0)
        elif mode == "month":
            count = user_data["monthly"].get(this_month, 0)
        elif mode == "week":
            start_of_week = now - timedelta(days=now.weekday())
            for i in range((now - start_of_week).days + 1):
                day_str = (start_of_week + timedelta(days=i)).strftime("%Y-%m-%d")
                count += user_data["daily"].get(day_str, 0)

        if count > 0:
            counts.append((name, user_id, count))

    if not counts:
        output = "暂无发言记录。"
        if update.message:
            await update.message.reply_text(output)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=output)
        return

    counts.sort(key=lambda x: x[2], reverse=True)

    # 缓存排行榜，用于翻页回调
    if chat_id not in CHAT_TALK_COUNTS:
        CHAT_TALK_COUNTS[chat_id] = {}
    CHAT_TALK_COUNTS[chat_id][mode] = counts

    # 设置标题
    title_map = {
        "today": "📅 今日发言排行榜",
        "yesterday": "📅 昨日发言排行榜",
        "week": "📅 本周发言排行榜",
        "month": "📅 本月发言排行榜"
    }
    title = title_map.get(mode, "📅 发言排行榜")

    # 构造分页列表（HTML mention + 序号）
    items = []
    for i, (name, user_id, count) in enumerate(counts):
        safe_name = escape(name or "未知用户")
        if is_silent:
            items.append(f"{i+1}. {safe_name} - {count} 条")
        else:
            items.append(f"{i+1}. {mention_html(user_id, name or '未知用户')} - {count} 条")

    await send_paginated_list(
        update,
        context,
        items,
        page=1,
        prefix=f"talktop_{mode}",
        title=title
    )

# ====== 分页回调处理 ======
async def talk_top_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pattern = r"^talktop_(\w+)_(\d+)$"
    match = re.match(pattern, query.data)
    if not match:
        return

    mode, page = match.groups()
    page = int(page)
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)
    counts = CHAT_TALK_COUNTS.get(chat_id, {}).get(mode, [])

    if not counts:
        await query.message.edit_text("暂无发言记录。")
        return

    # 构造分页列表（HTML mention + 序号）
    items = []
    for i, (name, user_id, count) in enumerate(counts):
        safe_name = escape(name or "未知用户")
        if is_silent:
            items.append(f"{i+1}. {safe_name} - {count} 条")
        else:
            items.append(f"{i+1}. {mention_html(user_id, name or '未知用户')} - {count} 条")

    title_map = {
        "today": "📅 今日发言排行榜",
        "yesterday": "📅 昨日发言排行榜",
        "week": "📅 本周发言排行榜",
        "month": "📅 本月发言排行榜"
    }
    title = title_map.get(mode, "📅 发言排行榜")

    await send_paginated_list(update, context, items, page=page, prefix=f"talktop_{mode}", title=title)


@register_command("发言频率", "频率")
async def talk_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    target_user = (
        update.message.reply_to_message.from_user
        if update.message and update.message.reply_to_message
        else update.effective_user
    )
    if not target_user:
        return

    now_ts = time.time()
    chat_id = str(update.effective_chat.id)
    user_id = str(target_user.id)
    q = _get_user_freq_queue(chat_id, user_id)
    _cleanup_queue(q, now_ts)

    count_10s = _count_in_window(q, now_ts, FREQ_SHORT_WINDOW_SECONDS)
    count_60s = _count_in_window(q, now_ts, FREQ_LONG_WINDOW_SECONDS)
    per_min = count_60s

    await safe_reply(
        update,
        context,
        (
            f"📈 发言频率 - {target_user.full_name}\n"
            f"最近 {FREQ_SHORT_WINDOW_SECONDS} 秒：{count_10s} 条\n"
            f"最近 {FREQ_LONG_WINDOW_SECONDS} 秒：{count_60s} 条\n"
            f"当前频率：约 {per_min} 条/分钟"
        ),
    )


# 注册
def register_talk_handlers(app):
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), count_message))
    app.add_handler(CommandHandler("talktop", talk_top))
    app.add_handler(CommandHandler("talkfreq", talk_frequency))
     # 财富排行榜分页回调
    app.add_handler(
        CallbackQueryHandler(
            talk_top_pagination_callback,
            pattern=r"^talktop_"          # 只匹配 talk_top 相关分页按钮
        )
    )
    
