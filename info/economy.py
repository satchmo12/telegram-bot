from datetime import datetime
import time
from email.mime import application
from html import escape
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import mention_html

from command_router import register_command
from tool.pagination_helper import (
    Paginator,
    generic_pagination_callback,
    send_paginated_list,
)
from info.storage import iter_group_infos, load_group_info, save_group_info

from utils import (
    bot_datetime_from_timestamp,
    can_use_command,
    get_group_whitelist,
    is_bot_owner,
    is_owner,
    load_json,
    save_json,
    safe_reply,
    is_super_admin,
)


# 支持添加的属性名映射
VALID_ATTRIBUTES = {
    "金币": "balance",
    "积分": "points",
    "体力": "stamina",
    "魅力": "charm",
    "心情": "mood",
    "幸运": "luck",
}

DEFAULT_USER_DATA = {
    "name": None,
    "username": None,
    "balance": 100,
    "points": 0,
    "stamina": 100,
    "charm": 60,
    "mood": 80,
    "luck": 100,
    "hunger": 100,
    "relationship_status": "单身",
    "level": 1,
    "exp": 0,
    "hp": 100,
    "attack": 10,
    "defense": 5,
    "equipment_attack": 0,
    "equipment_defense": 0,
}


# ---------------- 用户数据操作 ---------------- #
def get_richest_users(chat_id: str):
    users = get_all_users(chat_id)
    if not users:
        return []

    # 排序
    sorted_users = sorted(
        users.items(), key=lambda x: x[1].get("balance", 0), reverse=True
    )
    return sorted_users


_USERNAME_UNSET = object()
_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def ensure_user_exists(chat_id, user_id, name=None, telegram_username=_USERNAME_UNSET):
    """Create/update an economy user while retaining its public username."""
    chat_id, user_id = str(chat_id), str(user_id)
    chat = load_group_info(chat_id)
    users = chat.setdefault("users", {})

    changed = False
    if user_id not in users:
        users[user_id] = DEFAULT_USER_DATA.copy()
        changed = True

    user_data = users[user_id]
    if name and user_data.get("name") != name:
        user_data["name"] = name
        changed = True

    # Omitted callers leave the stored username untouched. Passing None from a
    # Telegram User clears a username that the user has removed.
    if telegram_username is not _USERNAME_UNSET:
        normalized_username = (
            str(telegram_username).strip().lstrip("@")
            if telegram_username
            else None
        )
        if user_data.get("username") != normalized_username:
            user_data["username"] = normalized_username
            changed = True

    if changed:
        save_group_info(chat_id, chat)


def _leaderboard_user_link(info: dict, *, silent: bool) -> str:
    """Return a blue profile URL without creating an @-mention.

    Only public Telegram usernames have a regular ``t.me`` URL. Old records
    stored a numeric user ID in ``username``; reject that value and render it as
    text rather than producing a broken link or an inline mention.
    """
    name = info.get("name") or "未知用户"
    safe_name = escape(str(name))
    username = str(info.get("username") or "").strip().lstrip("@")

    if silent or not _TELEGRAM_USERNAME_RE.fullmatch(username):
        return safe_name

    return f'<a href="https://t.me/{escape(username, quote=True)}">{safe_name}</a>'


# Avoid repeatedly reading the same group file for unchanged profiles.
_USER_IDENTITY_CACHE = {}


async def sync_economy_user_identity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persist a sender's name and public username before other handlers run.

    Economy records can be created by check-in, games, invitations, or message
    rewards. This handler makes the first ordinary message from a user enough
    to save their profile; users no longer need to run "用户信息" first.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.is_bot or not chat:
        return

    chat_id = str(chat.id)
    user_id = str(user.id)
    identity = (user.full_name, user.username)
    cache_key = (chat_id, user_id)
    if _USER_IDENTITY_CACHE.get(cache_key) == identity:
        return

    ensure_user_exists(chat_id, user_id, user.full_name, user.username)
    _USER_IDENTITY_CACHE[cache_key] = identity

    # Bound the cache in long-running bots. It is only an I/O optimization.
    if len(_USER_IDENTITY_CACHE) > 50_000:
        _USER_IDENTITY_CACHE.clear()


def get_user_data(chat_id, user_id):
    chat = load_group_info(chat_id)
    return chat.get("users", {}).get(str(user_id), DEFAULT_USER_DATA.copy())


def save_user_data(chat_id, user_id, user_data):
    chat = load_group_info(chat_id)
    chat.setdefault("users", {})[str(user_id)] = user_data
    save_group_info(chat_id, chat)


# ---------------- 用户信息查询 ---------------- #


def get_all_users(chat_id):
    return load_group_info(chat_id).get("users", {})


def get_balance(chat_id, user_id):
    return get_user_data(chat_id, user_id).get("balance", 100)


def get_points(chat_id, user_id):
    return get_user_data(chat_id, user_id).get("points", 0)


def get_nickname(chat_id, user_id):
    return get_user_data(chat_id, user_id).get("name", f"用户{user_id}")


# ---------------- 用户属性变更 ---------------- #


def change_user_attribute(
    chat_id, user_id, attr_name, delta, max_value=100, min_value=0
):
    user_data = get_user_data(chat_id, user_id)

    if attr_name.startswith("target_"):
        attr_name = attr_name[len("target_") :]

    # Point changes made by action effects also need an auditable transaction.
    if attr_name == "points":
        return change_points(chat_id, user_id, delta, reason="行为效果")

    current = user_data.get(attr_name, DEFAULT_USER_DATA.get(attr_name, 0))

    if attr_name != "balance":
        user_data[attr_name] = max(min_value, min(current + delta, max_value))
    else:
        user_data[attr_name] = max(min_value, current + delta)

    save_user_data(chat_id, user_id, user_data)
    return user_data[attr_name]


def change_balance(chat_id, user_id, amount):
    return change_user_attribute(
        chat_id, user_id, "balance", amount, max_value=9999999999999999999
    )


POINT_LOGS_KEY = "point_logs"
POINT_LOG_PAGE_SIZE = 8


def _normalize_points(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _point_logs_for_user(group_data: dict, user_id) -> list[dict]:
    """Return the mutable, newest-first point history for one group user."""
    logs_by_user = group_data.setdefault(POINT_LOGS_KEY, {})
    if not isinstance(logs_by_user, dict):
        logs_by_user = {}
        group_data[POINT_LOGS_KEY] = logs_by_user
    logs = logs_by_user.setdefault(str(user_id), [])
    if not isinstance(logs, list):
        logs = []
        logs_by_user[str(user_id)] = logs
    return logs


def get_point_logs(chat_id, user_id) -> list[dict]:
    """Load a user's point transactions in reverse chronological order."""
    group_data = load_group_info(chat_id)
    raw_logs = group_data.get(POINT_LOGS_KEY, {})
    logs = raw_logs.get(str(user_id), []) if isinstance(raw_logs, dict) else []
    valid_logs = [item for item in logs if isinstance(item, dict)]
    return sorted(
        valid_logs,
        key=lambda item: _normalize_points(item.get("timestamp")),
        reverse=True,
    )


def change_points(
    chat_id,
    user_id,
    amount,
    reason: str = "积分变动",
    *,
    name=None,
    telegram_username=_USERNAME_UNSET,
):
    """Change points, optionally refresh identity data, and persist a transaction."""
    chat_id, user_id = str(chat_id), str(user_id)
    requested_delta = _normalize_points(amount)
    group_data = load_group_info(chat_id)
    users = group_data.setdefault("users", {})
    if not isinstance(users, dict):
        users = {}
        group_data["users"] = users

    user_data = users.get(user_id)
    if not isinstance(user_data, dict):
        user_data = DEFAULT_USER_DATA.copy()
        users[user_id] = user_data

    if name:
        user_data["name"] = name
    if telegram_username is not _USERNAME_UNSET:
        user_data["username"] = (
            str(telegram_username).strip().lstrip("@")
            if telegram_username
            else None
        )

    old_balance = _normalize_points(user_data.get("points"))
    new_balance = max(0, min(old_balance + requested_delta, 999999))
    actual_delta = new_balance - old_balance
    user_data["points"] = new_balance

    if actual_delta:
        logs = _point_logs_for_user(group_data, user_id)
        # Insert at the front so entries created during the same second still
        # display in true newest-to-oldest order.
        logs.insert(0, {
            "timestamp": int(time.time()),
            "reason": str(reason or "积分变动").strip()[:80] or "积分变动",
            "amount": actual_delta,
            "balance": new_balance,
        })

    save_group_info(chat_id, group_data)
    return new_balance


def _point_log_keyboard(chat_id, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"pointslog:{chat_id}:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"pointslog:{chat_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def _point_log_group_title(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
) -> str:
    """Resolve the latest accessible group title for the point-log header."""
    configured = get_group_whitelist(context).get(str(chat_id), {})
    configured_title = str((configured or {}).get("title") or "").strip()
    try:
        chat = await context.bot.get_chat(int(chat_id))
        title = str(getattr(chat, "title", "") or "").strip()
        if title:
            return title
    except Exception:
        pass
    return configured_title or f"群 {chat_id}"


def _point_log_view(
    chat_id,
    user_id,
    page: int,
    group_title: str = "",
    context: ContextTypes.DEFAULT_TYPE = None,
) -> tuple[str, InlineKeyboardMarkup]:
    logs = get_point_logs(chat_id, user_id)
    total = len(logs)
    total_pages = max(1, (total + POINT_LOG_PAGE_SIZE - 1) // POINT_LOG_PAGE_SIZE)
    page = max(1, min(int(page or 1), total_pages))
    current_balance = get_points(chat_id, user_id)
    lines = [
        "📜 积分流水",
        f"群聊：{group_title or f'群 {chat_id}'}",
        f"当前积分：{current_balance} 分",
        f"第 {page}/{total_pages} 页 · 共 {total} 条",
        "",
    ]
    if not logs:
        lines.append("暂无积分流水。新产生的积分变动会显示在这里。")
    else:
        start = (page - 1) * POINT_LOG_PAGE_SIZE
        for item in logs[start : start + POINT_LOG_PAGE_SIZE]:
            timestamp = _normalize_points(item.get("timestamp"))
            when = (
                # Use this bot's configured timezone (for example, Asia/Shanghai
                # for Beijing time), rather than the host machine's timezone.
                bot_datetime_from_timestamp(timestamp, context).strftime("%Y-%m-%d %H:%M")
                if timestamp > 0
                else "未知时间"
            )
            delta = _normalize_points(item.get("amount"))
            sign = "+" if delta > 0 else ""
            reason = str(item.get("reason") or "积分变动")[:80]
            balance = _normalize_points(item.get("balance"))
            lines.extend([
                f"{when} {reason}：{sign}{delta} 分 · 余额：{balance} 分",
            ])
    return "\n".join(lines).rstrip(), _point_log_keyboard(str(chat_id), page, total_pages)


async def handle_points_log_start_parameter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parameter: str,
) -> bool:
    """Open a group's point log from the private-chat deep link."""
    if not isinstance(parameter, str) or not parameter.startswith("pointslog_"):
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    try:
        chat_id = int(parameter.removeprefix("pointslog_"))
    except (TypeError, ValueError):
        if update.message:
            await update.message.reply_text("❗ 积分流水链接无效。")
        return True
    if not update.effective_user or not update.message:
        return True
    group_title = await _point_log_group_title(context, chat_id)
    text, markup = _point_log_view(
        chat_id,
        update.effective_user.id,
        1,
        group_title,
        context,
    )
    await update.message.reply_text(text, reply_markup=markup)
    return True


async def points_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    try:
        _prefix, chat_id, page_text = query.data.split(":", 2)
        page = int(page_text)
    except (TypeError, ValueError):
        return await query.answer("积分流水参数无效。", show_alert=True)
    if not query.message or query.message.chat.type != "private":
        return await query.answer("请在与机器人的私聊中查看积分流水。", show_alert=True)
    group_title = await _point_log_group_title(context, chat_id)
    text, markup = _point_log_view(
        chat_id,
        query.from_user.id,
        page,
        group_title,
        context,
    )
    await query.answer()
    await query.edit_message_text(text, reply_markup=markup)


# ---------------- 每日恢复 ---------------- #


def give_daily_stamina_to_all():
    for chat_id, chat_info in iter_group_infos():
        users = chat_info.get("users", {})
        changed = False
        for user_data in users.values():
            user_data["stamina"] = min(100, user_data.get("stamina", 100) + 20)
            user_data["charm"] = min(100, user_data.get("charm", 60) + 2)
            user_data["hunger"] = min(100, user_data.get("hunger", 100) - 10)
            changed = True
        if changed:
            save_group_info(chat_id, chat_info)
    print(f"✅ [{datetime.now():%Y-%m-%d %H:%M:%S}] 所有用户体力已恢复 20")


# ---------------- 指令处理 ---------------- #
@register_command("用户信息", "我的信息", "好友信息", "查看信息")
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id

    # 判断是否回复了其他用户
    if update.message and update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        ensure_user_exists(chat_id, target.id, target.full_name, target.username)
        info = get_user_data(chat_id, target.id)
        title = "👤 用户信息"
        name = info.get("name", target.full_name)
    else:
        ensure_user_exists(chat_id, user.id, user.full_name, user.username)
        info = get_user_data(chat_id, user.id)
        title = "👤 个人信息"
        name = info.get("name", user.full_name)

    msg = (
        f"{title}\n"
        f"👑 昵称：{name}\n"
        f"⭐ 等级：{info.get('level', 1)}\n"
        f"💰 金币：{info.get('balance')} 枚\n"
        f"🏅 积分：{info.get('points')} 分\n"
        f"💪 体力：{info.get('stamina')}\n"
        f"✨ 魅力：{info.get('charm')}\n"
        f"😊 心情：{info.get('mood')}\n"
        f"🍀 幸运：{info.get('luck')}\n"
        f"🍗 饥饿：{info.get('hunger')}\n"
        f"💘 状态：{info.get('relationship_status')}"
    )

    await safe_reply(update, context, msg)


@register_command("我的金币", "金币")
async def check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id
    ensure_user_exists(chat_id, user.id, user.full_name, user.username)
    balance = get_balance(chat_id, user.id)
    await safe_reply(
        update, context, f"💰 {user.first_name}，你当前的金币：{balance} 枚"
    )


@register_command("我的积分", "积分")
async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    text = (update.effective_message.text or "").strip()

    # 获取群配置
    group_config = get_group_whitelist(context).get(chat_id, {})
    
    # 积分开关没开，不相应
    if not bool(group_config.get('points_enabled', False)):
        return 

    # 获取积分别名
    points_alias = str(
        group_config.get("points_alias") or ""
    ).strip()
    

    # 如果设置了别名，只响应别名
    if points_alias:
        if text != points_alias:
            return
    else:
        # 没有设置别名，响应默认命令
        if text != "我的积分":
            return

    ensure_user_exists(
        chat_id,
        user.id,
        user.full_name,
        user.username,
    )

    points = get_points(chat_id, user.id)

    bot_username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not bot_username:
        try:
            bot_username = str((await context.bot.get_me()).username or "").strip().lstrip("@")
        except Exception:
            bot_username = ""

    reply_markup = None
    if bot_username:
        log_url = f"https://t.me/{bot_username}?start=pointslog_{chat_id}"
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📜 积分流水", url=log_url)
        ]])

    await safe_reply(
        update,
        context,
        text=f"🏅 当前{points_alias or '积分'}：{points} 分",
        reply_markup=reply_markup,
        auto_delete_seconds=30,
    )

def format_rich_item(i, item):
    uid, info = item
    name = info.get("name", f"用户{uid}")
    mention = mention_html(
        uid, name or "未知用户"
    )  # 这里返回 <a href="tg://user?id=...">name</a>
    balance = info.get("balance", 0)
    return f"{i}. {mention} - 💵 {balance} 金币"


def format_rich_item_plain(i, item):
    uid, info = item
    name = escape(info.get("name", f"用户{uid}") or "未知用户")
    balance = info.get("balance", 0)
    return f"{i}. {name} - 💵 {balance} 金币"


async def send_paginated_list(
    update, context, items, page=1, prefix="page", format_item=None, title="列表"
):
    if title == "💰 财富排行榜":
        items = [
            item for item in items
            if item[1].get("balance", 0) != 0
        ]
    else:
        items = [
            item for item in items
            if item[1].get("points", 0) != 0
        ]
        
    paginator = Paginator(items)
    page = max(1, min(page, paginator.total_pages))
    page_items = paginator.get_page(page)
    format_item = format_item or (lambda i, x: f"{i}. {x}")

    text_lines = [f"📖 {title}（第 {page}/{paginator.total_pages} 页）:"]
    for i, item in enumerate(page_items, start=(page - 1) * paginator.page_size + 1):
        text_lines.append(format_item(i, item))

    markup = paginator.build_keyboard(prefix, page)
    text = "\n".join(text_lines)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(
            text, reply_markup=markup, parse_mode="HTML",  # 🔥 这里必须加 parse_mode
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_html(text, reply_markup=markup,
                                        disable_web_page_preview=True,)


@register_command("财富排行")
async def top_richest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    users = get_all_users(chat_id)

    if not users:
        return await safe_reply(update, context, "目前还没有任何人的金币记录。")

    sorted_users = sorted(
        users.items(), key=lambda x: x[1].get("balance", 0), reverse=True
    )

    await send_paginated_list(
        update=update,
        context=context,
        items=sorted_users,
        page=1,
        prefix="rich",
        title="💰 财富排行榜",
        format_item=(format_rich_item_plain if is_silent else format_rich_item),
    )


async def rich_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # 解析页码
    match = re.match(r"^rich_(\d+)$", query.data)
    if not match:
        return
    page = int(match.group(1))

    # 获取用户数据并排序
    chat_id = str(query.message.chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    users = get_all_users(chat_id)
    if not users:
        return await query.message.edit_text("目前还没有任何人的金币记录。")

    sorted_users = sorted(
        users.items(), key=lambda x: x[1].get("balance", 0), reverse=True
    )

    # 发送分页列表
    await send_paginated_list(
        update=update,
        context=context,
        items=sorted_users,
        page=page,
        prefix="rich",
        title="💰 财富排行榜",
        format_item=(format_rich_item_plain if is_silent else format_rich_item),
    )


@register_command("魅力排行")
async def top_charm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    users = get_all_users(chat_id)

    if not users:
        return await safe_reply(update, context, "目前还没有任何人的魅力记录。")

    sorted_users = sorted(
        users.items(), key=lambda x: x[1].get("charm", 0), reverse=True
    )

    lines = ["魅力排行榜："]
    for i, (uid, info) in enumerate(sorted_users[:20], start=1):
        name = info.get("name", f"用户{uid}")
        if is_silent:
            safe_name = escape(name or "未知用户")
            lines.append(f"{i}. {safe_name} - 💵 {info.get('charm', 0)} ")
        else:
            mention = mention_html(uid, name or "未知用户")
            lines.append(f"{i}. {mention} - 💵 {info.get('charm', 0)} ")

    await update.message.reply_html("\n".join(lines), disable_web_page_preview=True)


@register_command("积分排行", "邀请积分排名", "邀请积分排行")
async def top_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    text = (update.effective_message.text or "").strip()

    # 获取群配置
    group_config = get_group_whitelist(context).get(chat_id, {})
    
    # 邀请积分没开，不相应
    if not bool(group_config.get('points_enabled', False)):
        return 

    # 获取积分别名
    points_alias = str(
        group_config.get("points_alias") or ""
    ).strip()


    # 如果设置了别名，只响应别名
    if points_alias:
        if text != points_alias + "排名" and text != points_alias + "排行":
            return
            
            
    await show_rank(
        update=update,
        context=context,
        field="points",
        title=f"🏆 {points_alias or '积分'}排行榜",
        prefix="points",
        format_factory=get_points_formatter,
        empty_text="目前还没有任何人的积分记录。",
    )
    
    
async def rank_pagination_rich_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    match = re.match(r"^(rich|points)_(\d+)$", query.data)
    if not match:
        return

    prefix, page = match.group(1), int(match.group(2))

    chat_id = str(query.message.chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))

    users = get_all_users(chat_id)
    if not users:
        return await query.message.edit_text("暂无数据")

    field = "balance" if prefix == "rich" else "points"

    sorted_users = sorted(
        users.items(),
        key=lambda x: x[1].get(field, 0),
        reverse=True
    )

    await send_paginated_list(
        update=update,
        context=context,
        items=sorted_users,
        page=page,
        prefix=prefix,
        title="💰 财富排行榜" if prefix == "rich" else "🏆 积分排行榜",
        format_item=(get_rich_formatter(is_silent)
                     if prefix == "rich"
                     else get_points_formatter(is_silent)),
    )
    
# ---------------- 注册 ----------------
# 个人信息
@register_command("增加")
async def add_info_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    

    if  not await  can_use_command(context, user.id, chat_id):
        return

    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请【回复】你要加属性的用户的消息。")

    target_user = update.message.reply_to_message.from_user
    args = context.args

    if len(args) != 2:
        return await safe_reply(
            update, context, "用法：/加 <金币/体力/魅力/心情> <数量>"
        )

    attr_name, value_str = args[0], args[1]

    if attr_name not in VALID_ATTRIBUTES:
        return await safe_reply(
            update, context, f"属性必须是：{'、'.join(VALID_ATTRIBUTES.keys())}"
        )

    if not value_str.lstrip("-").isdigit():
        return await safe_reply(update, context, "数量必须是整数。")

    value = int(value_str)
    if value == 0:
        return await safe_reply(update, context, "数量不能为 0。")

    attr_key = VALID_ATTRIBUTES[attr_name]
    if attr_key == "points":
        current_value = change_points(
            chat_id,
            target_user.id,
            value,
            reason="管理员调整积分",
        )
    else:
        data = get_user_data(chat_id, target_user.id)
        data[attr_key] = data.get(attr_key, 0) + value
        save_user_data(chat_id, target_user.id, data)
        current_value = data[attr_key]

    await safe_reply(
        update,
        context,
        f"已给 {target_user.full_name} 添加 {value} 点「{attr_name}」。当前{attr_name}为：{current_value}",
        True,
    )


def clean_point(chat_id: str):
    chat = load_group_info(chat_id)
    users = chat.get("users", {})
    if not users:
        return False, "没有用户数据"
    for user_id, user_data in users.items():
        points = _normalize_points(user_data.get("points"))
        if points > 0:
            change_points(chat_id, user_id, -points, reason="管理员清空积分")
    return True, "✅ 所有用户积分已清零"



async def show_rank(
    update,
    context,
    field: str,
    title: str,
    prefix: str,
    format_factory,
    empty_text: str,
):
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))

    users = get_all_users(chat_id)

    if not users:
        return await safe_reply(update, context, empty_text)

    sorted_users = sorted(
        users.items(),
        key=lambda x: x[1].get(field, 0),
        reverse=True
    )

    await send_paginated_list(
        update=update,
        context=context,
        items=sorted_users,
        page=1,
        prefix=prefix,
        title=title,
        format_item=format_factory(is_silent),
    )

def get_rich_formatter(is_silent: bool):
    def fmt(i, item):
        uid, info = item

        name = info.get("name") or f"用户{uid}"
        balance = info.get("balance", 0)
        
        if not balance:
            return ""

        if is_silent:
            name = escape(name)
            return f"{i}. {name} - 💵 {balance} 金币"
        else:
            mention = mention_html(uid, name)
            return f"{i}. {mention} - 💵 {balance} 金币"

    return fmt


def get_points_formatter(is_silent: bool):
    def fmt(i, item):
        _uid, info = item
        points = info.get("points", 0)

        if not points:
            return ""

        user_link = _leaderboard_user_link(info, silent=is_silent)
        medals = {
            1: "🥇",
            2: "🥈",
            3: "🥉",
        }
        rank = medals.get(i, "")

        return f"{i} {user_link} - {rank} {points} 积分"

    return fmt

def register_economy_handlers(app):

    # Run before command/game handlers so newly created economy records include
    # the sender's public username from their very first group message.
    app.add_handler(
        MessageHandler(filters.ALL, sync_economy_user_identity),
        group=-1,
    )

    app.add_handler(CommandHandler("user_info", show_profile))
    app.add_handler(CommandHandler("balance", check_balance))
    app.add_handler(CommandHandler("point", my_points))
    app.add_handler(CommandHandler("toprichest", top_richest))
    app.add_handler(CommandHandler("top_charm", top_charm))
    app.add_handler(CommandHandler("top_points", top_points))
    app.add_handler(CommandHandler("add_info", add_info_profile))
    app.add_handler(CallbackQueryHandler(points_log_callback, pattern=r"^pointslog:-?\d+:\d+$"))
    # 财富排行榜分页回调
    app.add_handler(
        CallbackQueryHandler(rich_pagination_callback, pattern=r"^rich_\d+$")
    )
    
     # 财富排行榜分页回调
    app.add_handler(
        CallbackQueryHandler(rank_pagination_rich_callback, pattern=r"^points_\d+$")
    )
