import os
import time
import json
from typing import Optional
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from telegram.helpers import mention_html
from command_router import register_command
from info.economy import ensure_user_exists
from utils import get_group_whitelist, is_admin, is_super_admin, load_json, save_json, safe_reply

USER_DIR = "data/group_users"
LAST_SEEN_SAVE_INTERVAL = 60
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
    if not isinstance(old, dict):
        old = {}

    new_username = user.username
    new_full_name = user.full_name

    username_changed = old.get("username") != new_username
    name_changed = old.get("full_name") != new_full_name
    now_ts = int(time.time())
    last_seen = int(old.get("last_seen", 0) or 0) if isinstance(old, dict) else 0
    should_save = (
        not isinstance(old, dict)
        or not old
        or username_changed
        or name_changed
        or now_ts - last_seen >= LAST_SEEN_SAVE_INTERVAL
    )

    # username 变更历史（可选）
    history = old.get("username_history", [])

    if username_changed and old.get("username"):
        history.append(old.get("username"))

    if should_save:
        users[uid] = {
            "full_name": new_full_name,
            "username": new_username,
            "username_history": history,
            "join_time": int(old.get("join_time", 0) or 0),
            "last_seen": now_ts,
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
        now_ts = int(time.time())
        old = users.get(uid, {})
        if not isinstance(old, dict):
            old = {}
        join_time = int(old.get("join_time", 0) or 0)
        if join_time <= 0:
            join_time = now_ts
        users[uid] = {
            "full_name": member.full_name,
            "username": member.username,
            "username_history": [],
            "join_time": join_time,
            "last_seen": now_ts,
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


def _inactive_days_from_args(args) -> Optional[int]:
    if len(args or []) != 1:
        return None
    try:
        days = int(args[0])
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= 3650 else None


async def _can_manage_inactive_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """仅允许群主或超级管理员执行僵尸号操作。"""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type not in {"group", "supergroup"}:
        return False
    if is_super_admin(user.id):
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {"creator", "owner"}
    except Exception:
        return False


def _last_active_at(info: dict) -> int:
    """兼容早期记录；未发言成员以入群时间作为可追踪起点。"""
    try:
        return int(info.get("last_seen", 0) or info.get("join_time", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


async def _find_inactive_members(
    chat_id: int,
    days: int,
    context: ContextTypes.DEFAULT_TYPE,
    on_inactive_batch=None,
) -> list[tuple[int, dict]]:
    """只返回仍在群内且非管理员/机器人的已记录成员。"""
    cutoff = int(time.time()) - days * 24 * 60 * 60
    users = load_users(chat_id)
    inactive = []
    for user_id, info in users.items():
        if not isinstance(info, dict) or _last_active_at(info) > cutoff:
            continue
        try:
            numeric_user_id = int(user_id)
        except (TypeError, ValueError):
            continue
        if is_super_admin(numeric_user_id):
            continue
        try:
            member = await context.bot.get_chat_member(chat_id, numeric_user_id)
        except Exception:
            # 已离群或无法查询的账号不在扫描和清理范围内。
            continue
        if getattr(member.user, "is_bot", False):
            continue
        if member.status not in {"member", "restricted"}:
            # 管理员、群主以及不在群内的用户均不处理。
            continue
        inactive.append((numeric_user_id, info))
        # 扫描过程中每凑满 10 人立即输出，避免成员较多时长时间没有反馈。
        if on_inactive_batch and len(inactive) % 10 == 0:
            await on_inactive_batch(inactive[-10:])
    return inactive


def _inactive_mentions(members: list[tuple[int, dict]]) -> list[str]:
    mentions = []
    for user_id, info in members:
        name = str(info.get("full_name") or info.get("username") or user_id)
        mentions.append(mention_html(user_id, name))
    return mentions


@register_command("扫描僵尸号", "scaninactive")
async def scan_inactive_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """扫描指定天数内未发言、且仍在群内的已记录成员。"""
    chat = update.effective_chat
    days = _inactive_days_from_args(context.args)
    if not chat or chat.type not in {"group", "supergroup"}:
        return await safe_reply(update, context, "请在群组中使用此命令。")
    if not await _can_manage_inactive_members(update, context):
        return await safe_reply(update, context, "❌ 仅群主或超级管理员可使用。")
    if days is None:
        return await safe_reply(
            update,
            context,
            "用法：扫描僵尸号 天数\n例如：扫描僵尸号 30\n"
            "隐私模式下请使用：/scaninactive 30",
        )

    await safe_reply(update, context, f"🔎 开始扫描近 {days} 天未发言成员，请稍候…")

    async def send_inactive_batch(batch: list[tuple[int, dict]]):
        await safe_reply(
            update,
            context,
            "🔎 扫描到 10 位符合条件的成员：\n"
            + " ".join(_inactive_mentions(batch)),
            html=True,
        )

    inactive = await _find_inactive_members(
        chat.id, days, context, on_inactive_batch=send_inactive_batch
    )
    if not inactive:
        return await safe_reply(
            update, context, f"✅ 未发现连续 {days} 天未发言的已记录群成员。"
        )

    # 前面的整批已经实时发送，仅补发最后不足 10 人的一批。
    remaining_count = len(inactive) % 10
    if remaining_count:
        await safe_reply(
            update,
            context,
            f"🔎 扫描结束，最后 {remaining_count} 位符合条件的成员：\n"
            + " ".join(_inactive_mentions(inactive[-remaining_count:])),
            html=True,
        )
    await safe_reply(
        update,
        context,
        f"✅ 扫描完成：共发现 {len(inactive)} 位连续 {days} 天未发言的成员。",
    )


@register_command("清理僵尸号", "cleaninactive")
async def clean_inactive_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移出指定天数内未发言的已记录普通成员。"""
    chat = update.effective_chat
    days = _inactive_days_from_args(context.args)
    if not chat or chat.type not in {"group", "supergroup"}:
        return await safe_reply(update, context, "请在群组中使用此命令。")
    if not await _can_manage_inactive_members(update, context):
        return await safe_reply(update, context, "❌ 仅群主或超级管理员可使用。")
    if days is None:
        return await safe_reply(
            update,
            context,
            "用法：清理僵尸号 天数\n例如：清理僵尸号 30\n"
            "隐私模式下请使用：/cleaninactive 30",
        )

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if not getattr(bot_member, "can_restrict_members", False):
            return await safe_reply(update, context, "❌ 机器人没有移除成员权限。")
    except Exception:
        return await safe_reply(update, context, "❌ 无法确认机器人是否拥有移除成员权限。")

    await safe_reply(update, context, f"🧹 开始核对近 {days} 天未发言成员，请稍候…")
    inactive = await _find_inactive_members(chat.id, days, context)
    if not inactive:
        return await safe_reply(
            update, context, f"✅ 没有可清理的连续 {days} 天未发言成员。"
        )

    removed = 0
    failed = 0
    for user_id, _ in inactive:
        try:
            # ban 后立即 unban 是 Telegram 的“踢出群”操作，用户仍可通过邀请链接再次加入。
            await context.bot.ban_chat_member(chat.id, user_id)
            await context.bot.unban_chat_member(chat.id, user_id, only_if_banned=True)
            removed += 1
        except Exception:
            failed += 1

    await safe_reply(
        update,
        context,
        f"🧹 清理完成：已移出 {removed} 人，失败 {failed} 人（扫描条件：连续 {days} 天未发言）。",
    )


def register_user_tracker_handlers(app):
    # 不拦截 /命令，避免吞掉后续 CommandHandler（如 /sign）
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), record_user))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    # 群用户列表命令
    app.add_handler(CommandHandler("list", list_group_users))
    # 群用户私聊链接命令
    app.add_handler(CommandHandler("user_links", list_group_user_links))
