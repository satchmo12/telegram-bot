from telegram import ChatPermissions, Update
from telegram.ext import ContextTypes
from telegram.helpers import mention_html
from telegram import Update, ChatMember
from telegram.ext import ChatMemberHandler, ContextTypes

from info.economy import ensure_user_exists
from group.admin import ban_user
from group.grouplist import load_users, save_users
from forward.message_forward import get_group_list
from utils import get_group_whitelist, group_allowed, load_json

USER_DIR = "data/group_users"


@group_allowed
async def check_and_restrict_scam_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    display_name = user.full_name  # 你也可以用 first_name

    old_name = check_name_change(user.id, display_name, current_chat_id=chat.id)
    if old_name:
        # 自动更新名字
        ensure_user_exists(chat.id, user.id, display_name)
        await update.message.reply_text(
            f"📛 用户昵称已修改：\n旧昵称：{old_name}\n新昵称：{display_name}"
        )

    # 忽略非群组和机器人的自身
    if not chat or not chat.type.endswith("group"):
        return
    if not user or user.id == context.bot.id:
        return


    if chat.type != "supergroup":
        # await update.message.reply_text("❗此功能只能在超级群中使用。")
        return

    try:
        # 判断 scam 或 fake
        if getattr(user, "is_scam", False) or getattr(user, "is_fake", False):
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(update.message.date.timestamp()) + 3600,
            )
            group_cfg = get_group_whitelist(context).get(str(chat.id), {})
            is_silent = bool(group_cfg.get("silent", False))
            if is_silent:
                await update.message.reply_text(
                    f"⚠️ 用户 {user.full_name} 被标记为诈骗账户，已限制发言。"
                )
            else:
                await update.message.reply_html(
                    f"⚠️ 用户 {mention_html(user.id, user.full_name)} 被标记为诈骗账户，已限制发言。"
                )
        # if getattr(user, "full_name")

    except Exception as e:
        print(f"检查 scam 用户失败: {e}")


def check_name_change(id: int, new_name: str, new_username: str = None, current_chat_id=None) -> str:
    """
    检查用户昵称是否发生变化，如果变化则更新 JSON 并返回旧昵称
    同时静默更新 username 和 username_history（不返回）
    仅适用于新数据结构
    """
    check_chats = list(get_group_list().keys())
    if current_chat_id and current_chat_id not in check_chats:
        check_chats.append(current_chat_id)  # 确保当前群也检查

    for chat_id in check_chats:
        try:
            users = load_users(chat_id)
            info = users.get(str(id))
            if not info:
                continue

            old_name = info.get("full_name", "")

            # 昵称变更
            if old_name != new_name:
                info["full_name"] = new_name

            # username 静默更新
            old_username = info.get("username")
            if new_username and new_username != old_username:
                if old_username:
                    info.setdefault("username_history", []).append(old_username)
                info["username"] = new_username

            users[str(id)] = info
            save_users(chat_id, users)

            if old_name != new_name:
                return old_name  # 只有昵称变更时返回旧昵称

        except Exception as e:
            print(f"❌ 获取群 {chat_id} 用户失败: {e}")

    return ""

async def left_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    old_status = update.chat_member.old_chat_member.status
    new_status = update.chat_member.new_chat_member.status
    user = update.chat_member.new_chat_member.user
    chat_id = update.chat_member.chat.id

    # 用户加入群（自己加入或被拉入）
    if old_status in ["left", "kicked"] and new_status in ["member", "administrator"]:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎉 {user.full_name} 加入了群！"
        )

    # 用户离开群（自己退出或被踢）
    elif old_status in ["member", "administrator"] and new_status in ["left", "kicked"]:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"👋 {user.full_name} 离开了群！"
        )

    # 用户身份变化（普通成员 <-> 管理员）
    elif old_status != new_status:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚡ {user.full_name} 的身份从 {old_status} 变为 {new_status}"
        )
