from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from html import escape

from command_router import register_command
from utils import load_json, save_json, safe_reply

INVITE_STATS_FILE = "data/invite_stats.json"
INVITE_LINK_MAP_FILE = "data/invite_link_map.json"


def load_invite_stats() -> dict:
    data = load_json(INVITE_STATS_FILE)
    if not isinstance(data, dict):
        return {}
    for group_data in data.values():
        for user_data in group_data.values():
            user_data["invitees"] = set(user_data.get("invitees", []))
    return data


def save_invite_stats(data: dict):
    data_copy = {
        gid: {
            uid: {
                "username": info.get("username", "未知用户"),
                "count": int(info.get("count", 0)),
                "invitees": list(info.get("invitees", set())),
            }
            for uid, info in users.items()
        }
        for gid, users in data.items()
    }
    save_json(INVITE_STATS_FILE, data_copy)


def load_invite_link_map() -> dict:
    data = load_json(INVITE_LINK_MAP_FILE)
    return data if isinstance(data, dict) else {}


def save_invite_link_map(data: dict):
    save_json(INVITE_LINK_MAP_FILE, data)


def get_user_invite_count(stats_data: dict, chat_id: int, user_id: int) -> int:
    group_stats = stats_data.get(str(chat_id), {})
    user_stats = group_stats.get(str(user_id), {})
    try:
        return int(user_stats.get("count", 0))
    except Exception:
        return 0


def format_personal_link_text(display_name: str, link: str, total_count: int) -> str:
    safe_name = escape(display_name or "用户")
    safe_link = escape(link or "")
    return (
        f"🔗 {safe_name} 您的专属链接:\n"
        f"<code>{safe_link}</code>\n"
        "(点击复制)\n\n"
        f"👉 当前总共邀请 {int(total_count)} 人"
    )


def update_invite_stats_by_user(
    stats_data: dict,
    chat_id: int,
    inviter_id: int,
    inviter_name: str,
    new_member_ids: list[int],
):
    group_id = str(chat_id)
    inviter_id_str = str(inviter_id)

    group_stats = stats_data.setdefault(group_id, {})
    stat = group_stats.setdefault(
        inviter_id_str,
        {"username": inviter_name or "未知用户", "count": 0, "invitees": set()},
    )

    stat["username"] = inviter_name or stat.get("username", "未知用户")

    for uid in new_member_ids:
        if uid not in stat["invitees"]:
            stat["invitees"].add(uid)
            stat["count"] += 1

    save_invite_stats(stats_data)


@register_command("邀请链接")
async def create_personal_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return await safe_reply(update, context, "⚠️ 该命令只能在群里使用。")

    user = update.effective_user
    chat_key = str(chat.id)
    stats_data = load_invite_stats()
    invited_count = get_user_invite_count(stats_data, chat.id, user.id)
    link_map_data = load_invite_link_map()
    group_link_map = link_map_data.setdefault(chat_key, {})

    # 同群同用户复用已有链接，避免每次生成新链接
    existing_link = None
    existing_created_at = -1
    for link, info in group_link_map.items():
        if int(info.get("inviter_id", 0)) != int(user.id):
            continue
        ts = int(info.get("created_at", 0))
        if ts >= existing_created_at:
            existing_created_at = ts
            existing_link = link

    if existing_link:
        msg = format_personal_link_text(user.full_name, existing_link, invited_count)
        return await safe_reply(update, context, msg, html=True)

    # 机器人无创建邀请链接权限时，静默跳过（多机器人同群场景）
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        can_invite = bool(getattr(bot_member, "can_invite_users", False))
        if not can_invite:
            return
    except Exception:
        return

    try:
        link_obj = await context.bot.create_chat_invite_link(
            chat_id=chat.id,
            name=f"inviter:{user.id}",
        )
    except Exception as e:
        return await safe_reply(update, context, f"❌ 生成链接失败：{e}")

    group_link_map[link_obj.invite_link] = {
        "inviter_id": user.id,
        "inviter_name": user.full_name,
        "created_at": int(update.message.date.timestamp()) if update.message.date else 0,
    }
    save_invite_link_map(link_map_data)

    msg = format_personal_link_text(user.full_name, link_obj.invite_link, invited_count)
    await safe_reply(update, context, msg, html=True)


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    chat_id = update.effective_chat.id
    new_member_ids = [u.id for u in update.message.new_chat_members if u]
    if not new_member_ids:
        return

    used_invite_link = None
    invite_link_obj = getattr(update.message, "invite_link", None)
    if invite_link_obj:
        used_invite_link = getattr(invite_link_obj, "invite_link", None)

    link_map_data = load_invite_link_map()
    stats_data = load_invite_stats()

    if used_invite_link:
        group_link_map = link_map_data.get(str(chat_id), {})
        owner_info = group_link_map.get(used_invite_link)
        if owner_info:
            inviter_id = int(owner_info.get("inviter_id", 0))
            inviter_name = owner_info.get("inviter_name", "未知用户")
            if inviter_id:
                update_invite_stats_by_user(
                    stats_data, chat_id, inviter_id, inviter_name, new_member_ids
                )
                return

    inviter = update.message.from_user
    if inviter and any(uid != inviter.id for uid in new_member_ids):
        update_invite_stats_by_user(
            stats_data, chat_id, inviter.id, inviter.full_name, new_member_ids
        )


@register_command("邀请统计")
async def show_invites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in ("administrator", "creator"):
        return await safe_reply(update, context, "🚫 你没有权限查看邀请统计。")

    stats_data = load_invite_stats()
    group_stats = stats_data.get(chat_id)
    if not group_stats:
        return await safe_reply(update, context, "暂无该群的邀请数据。")

    sorted_stats = sorted(
        group_stats.items(), key=lambda x: x[1].get("count", 0), reverse=True
    )

    msg_lines = ["📊 本群邀请排行榜："]
    for i, (_, info) in enumerate(sorted_stats[:10], start=1):
        msg_lines.append(f"{i}. {info['username']} — 邀请 {info['count']} 人")

    await safe_reply(update, context, "\n".join(msg_lines))


def register_invite_handlers(app):
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(CommandHandler("invites", show_invites))
    app.add_handler(CommandHandler("link", create_personal_invite_link))
