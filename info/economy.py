from datetime import datetime
from email.mime import application
from html import escape
import re
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.helpers import mention_html

from command_router import register_command
from tool.pagination_helper import (
    Paginator,
    generic_pagination_callback,
    send_paginated_list,
)
from utils import INFO_FILE, get_group_whitelist, load_json, save_json, safe_reply, is_super_admin 


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


def ensure_user_exists(chat_id, user_id, username=None):
    data = load_json(INFO_FILE)
    chat_id, user_id = str(chat_id), str(user_id)
    users = data.setdefault(chat_id, {}).setdefault("users", {})

    if user_id not in users:
        users[user_id] = DEFAULT_USER_DATA.copy()
    if username:
        users[user_id]["name"] = username

    users[user_id]["username"] = str(user_id)

    save_json(INFO_FILE, data)


def get_user_data(chat_id, user_id):
    data = load_json(INFO_FILE)
    return (
        data.get(str(chat_id), {})
        .get("users", {})
        .get(str(user_id), DEFAULT_USER_DATA.copy())
    )


def save_user_data(chat_id, user_id, user_data):
    data = load_json(INFO_FILE)
    chat = data.setdefault(str(chat_id), {})
    users = chat.setdefault("users", {})
    users[str(user_id)] = user_data
    save_json(INFO_FILE, data)


# ---------------- 用户信息查询 ---------------- #


def get_all_users(chat_id):
    return load_json(INFO_FILE).get(str(chat_id), {}).get("users", {})


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


def change_points(chat_id, user_id, amount):
    return change_user_attribute(chat_id, user_id, "points", amount, max_value=999999)


# ---------------- 每日恢复 ---------------- #


def give_daily_stamina_to_all():
    data = load_json(INFO_FILE)
    for chat_id, chat_info in data.items():
        users = chat_info.get("users", {})
        for user_id, user_data in users.items():
            user_data["stamina"] = min(100, user_data.get("stamina", 100) + 20)
            user_data["charm"] = min(100, user_data.get("charm", 60) + 2)
            user_data["hunger"] = min(100, user_data.get("hunger", 100) - 10)

            if user_data["hunger"] < 20 and user_data["mood"] < 40:
                # 生病状态 需要就医
                pass

    save_json(INFO_FILE, data)
    print(f"✅ [{datetime.now():%Y-%m-%d %H:%M:%S}] 所有用户体力已恢复 20")


# ---------------- 指令处理 ---------------- #
@register_command("用户信息", "我的信息", "好友信息", "查看信息")
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id

    # 判断是否回复了其他用户
    if update.message and update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        ensure_user_exists(chat_id, target.id, target.full_name)
        info = get_user_data(chat_id, target.id)
        title = "👤 用户信息"
        name = info.get("name", target.full_name)
    else:
        ensure_user_exists(chat_id, user.id, user.full_name)
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


@register_command("我的金币")
async def check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id
    ensure_user_exists(chat_id, user.id, user.full_name)
    balance = get_balance(chat_id, user.id)
    await safe_reply(
        update, context, f"💰 {user.first_name}，你当前的金币：{balance} 枚"
    )


@register_command("我的积分")
async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id
    ensure_user_exists(chat_id, user.id, user.full_name)
    points = get_points(chat_id, user.id)
    await safe_reply(update, context,f"🏅 当前积分：{points} 分")




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
            text, reply_markup=markup, parse_mode="HTML"  # 🔥 这里必须加 parse_mode
        )
    else:
        await update.message.reply_html(text, reply_markup=markup)


@register_command("财富排行")
async def top_richest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    users = get_all_users(chat_id)

    if not users:
        return await safe_reply(update, context,"目前还没有任何人的金币记录。")

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
        return await safe_reply(update, context,"目前还没有任何人的魅力记录。")

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


# ---------------- 注册 ----------------
# 个人信息
@register_command("增加")
async def add_info_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)

    if not is_super_admin(user.id):
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
    data = get_user_data(chat_id, target_user.id)
    data[attr_key] = data.get(attr_key, 0) + value
    save_user_data(chat_id, target_user.id, data)

    await safe_reply(
        update,
        context,
        f"已给 {target_user.full_name} 添加 {value} 点「{attr_name}」。当前{attr_name}为：{data[attr_key]}",
        True,
    )


def register_economy_handlers(app):

    app.add_handler(CommandHandler("user_info", show_profile))
    app.add_handler(CommandHandler("balance", check_balance))
    app.add_handler(CommandHandler("point", my_points))
    app.add_handler(CommandHandler("toprichest", top_richest))
    app.add_handler(CommandHandler("top_charm", top_charm))
    app.add_handler(CommandHandler("add_info", add_info_profile))
    # 财富排行榜分页回调
    app.add_handler(
        CallbackQueryHandler(rich_pagination_callback, pattern=r"^rich_\d+$")
    )
