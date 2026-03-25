# group_logger.py

from telegram import Update
from telegram.ext import ChatMemberHandler, ContextTypes
import time
from utils import load_json, save_json, INVITE_BOT_USERS_FILE

GROUPS_FILE = "data/groups.json"

async def log_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.effective_chat
    if chat.type not in ["group", "supergroup"]:
        return

    # 安全加载
    groups = load_json(GROUPS_FILE) or {}
    chat_id_str = str(chat.id)

    title = chat.title or ""
    username = chat.username or ""

    if chat_id_str not in groups:
        # 新群记录
        groups[chat_id_str] = {
            "title": title,
            "username": username,
            "type": chat.type,
            "enabled": True,
            "bot_in_group": True,
            "recommend": True,
            "exposure": 0,
            "recommend_last_ts": 0,
            "verify": False,
            "silent": False,
            "ad_filter": False,
            "ad_push_enabled": False,
            "ad_push_mode": "interval",
            "ad_push_interval_min": 120,
            "ad_push_text": "",
            "ad_push_times": "",
            "manor": False,
            "chengyu_game": False,
            "welcome": False,
            "learning_enabled": True,   # 默认启用学习
            "reply_enabled": False,     # 默认不自动回复
            "active_speak_enabled": False,  # 默认不主动说话
            "active_speak_interval_min": 120,
        }
        print(f"✅ 已记录新群: {title} ({chat.id})")
    else:
        # 已有群，检查并更新信息
        updated = False
        group = groups[chat_id_str]

        if group.get("title", "") != title:
            group["title"] = title
            updated = True
        if group.get("username", "") != username:
            group["username"] = username
            updated = True
        if group.get("bot_in_group", True) is not True:
            group["bot_in_group"] = True
            updated = True
        if "learning_enabled" not in group:
            group["learning_enabled"] = True
            updated = True
        if "reply_enabled" not in group:
            group["reply_enabled"] = False
            updated = True
        if "active_speak_enabled" not in group:
            group["active_speak_enabled"] = False
            updated = True
        if "active_speak_interval_min" not in group:
            group["active_speak_interval_min"] = 120
            updated = True

        if updated:
            print(f"🔄 群信息更新: {title} ({chat.id})")

    # 保存到 JSON
    save_json(GROUPS_FILE, groups)


def _is_bot_in_group_status(status: str) -> bool:
    return status in {"member", "administrator", "creator", "restricted"}


def _load_invite_bot_users() -> dict:
    data = load_json(INVITE_BOT_USERS_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("rewarded_groups", {})
    data.setdefault("users", {})
    return data


def _save_invite_bot_users(data: dict):
    save_json(INVITE_BOT_USERS_FILE, data)


def _reward_inviter(chat_id: int, inviter):
    if not inviter or getattr(inviter, "is_bot", False):
        return
    data = _load_invite_bot_users()
    rewarded = data.get("rewarded_groups", {})
    users = data.get("users", {})
    chat_id_str = str(chat_id)

    # 已奖励过该群，不再重复奖励（防止拉进-踢出-再拉）
    if chat_id_str in rewarded:
        return

    user_id_str = str(inviter.id)
    user = users.get(user_id_str, {})
    user["username"] = inviter.full_name or user.get("username", "未知用户")
    try:
        user["exposure"] = int(user.get("exposure", 0)) + 50
    except Exception:
        user["exposure"] = 50
    groups = user.get("groups", [])
    if not isinstance(groups, list):
        groups = []
    if chat_id_str not in groups:
        groups.append(chat_id_str)
    user["groups"] = groups
    users[user_id_str] = user

    rewarded[chat_id_str] = {
        "user_id": inviter.id,
        "username": inviter.full_name or "未知用户",
        "ts": int(time.time()),
    }
    data["users"] = users
    data["rewarded_groups"] = rewarded
    _save_invite_bot_users(data)


async def track_bot_group_membership(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """监听机器人在群内状态变化，维护 groups.json 的 bot_in_group 字段。"""
    if not update.my_chat_member:
        return

    chat = update.my_chat_member.chat
    if not chat or chat.type not in ["group", "supergroup"]:
        return

    old_status = update.my_chat_member.old_chat_member.status
    new_status = update.my_chat_member.new_chat_member.status

    # 状态没变化不处理
    if old_status == new_status:
        return

    in_group = _is_bot_in_group_status(new_status)
    was_in_group = _is_bot_in_group_status(old_status)
    # 如果被禁言（restricted 且不能发言），视为不在群
    if new_status == "restricted":
        can_send = bool(
            getattr(update.my_chat_member.new_chat_member, "can_send_messages", False)
        )
        if not can_send:
            in_group = False
    groups = load_json(GROUPS_FILE) or {}
    if not isinstance(groups, dict):
        groups = {}

    chat_id_str = str(chat.id)
    cfg = groups.get(chat_id_str, {})
    if not isinstance(cfg, dict):
        cfg = {}

    changed = False
    if cfg.get("title", "") != (chat.title or ""):
        cfg["title"] = chat.title or ""
        changed = True
    if cfg.get("username", "") != (chat.username or ""):
        cfg["username"] = chat.username or ""
        changed = True
    prev_type = cfg.get("type", "")
    if prev_type != chat.type:
        cfg["type"] = chat.type
        changed = True
    if bool(cfg.get("bot_in_group", True)) != in_group:
        cfg["bot_in_group"] = in_group
        changed = True
    if in_group and not was_in_group and chat.type == "supergroup":
        inviter = getattr(update.my_chat_member, "from_user", None)
        _reward_inviter(chat.id, inviter)
    elif in_group and chat.type == "supergroup" and prev_type != "supergroup":
        # 普通群升级成超级群时也奖励（仅一次，内部有去重）
        inviter = getattr(update.my_chat_member, "from_user", None)
        _reward_inviter(chat.id, inviter)

    if changed:
        groups[chat_id_str] = cfg
        save_json(GROUPS_FILE, groups)
        print(f"🤖 机器人群状态变更: {chat.title}({chat.id}) -> bot_in_group={in_group}")


def register_group_logger_handlers(app):
    app.add_handler(
        ChatMemberHandler(
            track_bot_group_membership, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER
        )
    )
