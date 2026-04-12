import os
import time
import json
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from command_router import register_command
from info.economy import ensure_user_exists
from utils import get_group_whitelist, is_admin, is_super_admin, load_json, save_json, safe_reply

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


def get_user_join_time(chat_id, user_id) -> int:
    users = load_users(chat_id)
    info = users.get(str(user_id), {})
    if not isinstance(info, dict):
        return 0
    try:
        return int(info.get("join_time", 0) or 0)
    except Exception:
        return 0


def _merge_user_records(dst: dict, src: dict) -> dict:
    if not isinstance(dst, dict):
        return src if isinstance(src, dict) else {}
    if not isinstance(src, dict):
        return dst
    merged = dict(dst)
    dst_seen = int(dst.get("last_seen", 0) or 0)
    src_seen = int(src.get("last_seen", 0) or 0)

    def _pick(field: str) -> str:
        if src_seen > dst_seen:
            return src.get(field) or dst.get(field) or ""
        return dst.get(field) or src.get(field) or ""

    merged["full_name"] = _pick("full_name")
    merged["username"] = _pick("username")
    merged["last_seen"] = max(dst_seen, src_seen)

    history = []
    for v in (dst.get("username_history") or []) + (src.get("username_history") or []):
        if v and v not in history:
            history.append(v)
    merged["username_history"] = history
    return merged


def _load_json_raw(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json_raw(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _merge_group_user_files(src_dir: str, dst_dir: str) -> tuple[int, int, int]:
    if not os.path.isdir(src_dir):
        return 0, 0, 0
    os.makedirs(dst_dir, exist_ok=True)
    files = [f for f in os.listdir(src_dir) if f.endswith(".json")]
    merged_files = 0
    merged_users = 0
    created_files = 0
    for fname in files:
        src_path = os.path.join(src_dir, fname)
        dst_path = os.path.join(dst_dir, fname)
        src_data = _load_json_raw(src_path)
        if not isinstance(src_data, dict):
            continue
        dst_data = _load_json_raw(dst_path)
        dst_data = dst_data if isinstance(dst_data, dict) else {}
        if not dst_data:
            created_files += 1
        changed = False
        for uid, info in src_data.items():
            before = dst_data.get(uid)
            merged = _merge_user_records(before if isinstance(before, dict) else {}, info)
            if merged != before:
                dst_data[uid] = merged
                changed = True
            if before is None:
                merged_users += 1
        if changed:
            _save_json_raw(dst_path, dst_data)
            merged_files += 1
    return merged_files, created_files, merged_users


def _merge_all_group_user_dirs(root_dir: str, dst_dir: str) -> tuple[int, int, int, int]:
    if not os.path.isdir(root_dir):
        return 0, 0, 0, 0
    total_merged_files = 0
    total_created_files = 0
    total_merged_users = 0
    scanned_dirs = 0
    for name in os.listdir(root_dir):
        src_dir = os.path.join(root_dir, name, "group_users")
        if not os.path.isdir(src_dir):
            continue
        scanned_dirs += 1
        mf, cf, mu = _merge_group_user_files(src_dir, dst_dir)
        total_merged_files += mf
        total_created_files += cf
        total_merged_users += mu
    return total_merged_files, total_created_files, total_merged_users, scanned_dirs


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
        "join_time": int(old.get("join_time", 0) or 0),
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
            "join_time": int(time.time()),
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


@register_command("合并用户")
async def merge_group_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "❌ 只有超级管理员才能使用此命令。")
    dst_dir = "data/group_users"
    merged_files, created_files, merged_users, scanned_dirs = _merge_all_group_user_dirs("data", dst_dir)
    await safe_reply(
        update,
        context,
        "✅ 合并完成\n"
        f"来源目录：data/*/group_users\n"
        f"目标目录：{dst_dir}\n"
        f"扫描目录数：{scanned_dirs}\n"
        f"更新文件数：{merged_files}\n"
        f"新建文件数：{created_files}\n"
        f"新增用户数：{merged_users}",
    )


def register_user_tracker_handlers(app):
    # 不拦截 /命令，避免吞掉后续 CommandHandler（如 /sign）
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), record_user))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    # 群用户列表命令
    app.add_handler(CommandHandler("list", list_group_users))
    # 群用户私聊链接命令
    app.add_handler(CommandHandler("user_links", list_group_user_links))
