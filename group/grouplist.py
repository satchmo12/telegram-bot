import os
import time
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from command_router import register_command
from info.economy import ensure_user_exists
from utils import get_group_whitelist, is_admin, load_json, save_json, safe_reply

USER_DIR = "data/group_users"
os.makedirs(USER_DIR, exist_ok=True)


def get_group_file(chat_id):
    return os.path.join(USER_DIR, f"{chat_id}.json")


def load_users(chat_id):
    path = get_group_file(chat_id)
    data = load_json(path)
    return data if isinstance(data, dict) else {}


def save_users(chat_id, users):
    path = get_group_file(chat_id)
    save_json(path, users)


async def record_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user or chat.type not in ("group", "supergroup"):
        return

    users = load_users(chat.id)
    uid = str(user.id)

    old = users.get(uid, {})

    new_username = user.username
    new_full_name = user.full_name

    username_changed = old.get("username") != new_username
    name_changed = old.get("full_name") != new_full_name

    # username 变更历史（可选）
    history = old.get("username_history", [])

    if username_changed and old.get("username"):
        history.append(old.get("username"))

    users[uid] = {
        "full_name": new_full_name,
        "username": new_username,
        "username_history": history,
        "last_seen": int(time.time()),
    }

    save_users(chat.id, users)

    # 你原本的逻辑
    ensure_user_exists(chat.id, user.id, new_full_name)

    # # 日志（可删）
    # if username_changed:
    #     print(
    #         f"[USERNAME CHANGE] chat={chat.id} "
    #         f"user={uid} {old.get('username')} -> {new_username}"
    #     )


async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    users = load_users(chat.id)

    for member in update.message.new_chat_members:
        uid = str(member.id)
        users[uid] = {
            "full_name": member.full_name,
            "username": member.username,
            "username_history": [],
            "last_seen": int(time.time()),
        }

    save_users(chat.id, users)


# 群用户列表命令
@register_command("群用户")
async def list_group_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await safe_reply(update, context,"❌ 只有管理员才能使用此命令。")

    chat = update.effective_chat
    if not chat or chat.type not in ["group", "supergroup"]:
        return await safe_reply(update, context,"只能在群组中使用该命令。")

    users = load_users(chat.id)
    if not users:
        return await safe_reply(update, context,"尚未记录任何用户。")
    chat_id = str(chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))

    msg = "📋 当前记录的群用户列表：\n"
    for uid, info in users.items():
        if isinstance(info, dict):
            if is_silent:
                display_name = info.get("username") or info.get("full_name", "未知")
            else:
                display_name = (
                    f"@{info['username']}"
                    if info.get("username")
                    else info.get("full_name", "未知")
                )
            msg += f"- {display_name}（ID: {uid}）\n"
        else:
            # 保险兼容旧结构（字符串）
            msg += f"- {info}（ID: {uid}）\n"

    await safe_reply(update, context,msg)


# 新命令：群用户私聊链接
@register_command("私聊链接")
async def list_group_user_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await safe_reply(update, context,"❌ 只有管理员才能使用此命令。")
    chat = update.effective_chat
    if not chat or chat.type not in ["group", "supergroup"]:
        return await safe_reply(update, context,"只能在群组中使用该命令。")

    users = load_users(chat.id)
    if not users:
        return await safe_reply(update, context,"尚未记录任何用户。")

    msg = "🔗 当前群用户私聊链接：\n"
    for uid, info in users.items():
        if isinstance(info, dict):
            username = info.get("username")
            full_name = info.get("full_name", "未知")
            if username:
                link = f"https://t.me/{username}"
                msg += f"- {full_name}：{link}\n"
            else:
                msg += f"- {full_name}：无 username\n"
        else:
            # 兼容旧结构（字符串）
            msg += f"- {info}：无 username\n"

    await safe_reply(update, context, msg)


def register_user_tracker_handlers(app):
    # 不拦截 /命令，避免吞掉后续 CommandHandler（如 /sign）
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), record_user))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    # 群用户列表命令
    app.add_handler(CommandHandler("list", list_group_users))
    # 群用户私聊链接命令
    app.add_handler(CommandHandler("user_links", list_group_user_links))
