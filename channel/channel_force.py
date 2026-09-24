import json
import os
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from command_router import register_command
from typing import Optional
from utils import get_group_whitelist, load_json, save_json
from group.grouplist import get_user_join_time
from group.mute_registry import add_mute, remove_mute


DATA_FILE = "config_data/force_subscribe.json"
MUTE_FILE = "data/force_subscribe_mute.json"

# 用户提醒冷却
user_warn_cooldown = {}


def _normalize_target(value: str) -> str:
    target = str(value or "").strip()
    if target.startswith("https://t.me/"):
        target = "@" + target.rsplit("/", 1)[-1].strip()
    elif target.startswith("t.me/"):
        target = "@" + target.rsplit("/", 1)[-1].strip()
    if target and not target.startswith("@"):
        target = f"@{target.lstrip('@')}"
    # Public @usernames are required so Telegram can render a join button.
    return target if len(target) > 1 else ""


def parse_force_subscribe_targets(value) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").replace("，", ",").replace("\n", ",").split(",")
    targets = []
    for raw in values:
        for part in str(raw or "").replace("，", ",").replace("\n", ",").split(","):
            target = _normalize_target(part)
            if target and target not in targets:
                targets.append(target)
    return targets[:10]


def get_force_subscribe_targets(chat_id: str) -> list[str]:
    data = load_json(DATA_FILE)
    if not isinstance(data, dict):
        return []
    return parse_force_subscribe_targets(data.get(str(chat_id), []))


def set_force_subscribe_targets(chat_id: str, targets) -> None:
    data = load_json(DATA_FILE)
    if not isinstance(data, dict):
        data = {}
    normalized = parse_force_subscribe_targets(targets)
    if normalized:
        data[str(chat_id)] = normalized
    else:
        data.pop(str(chat_id), None)
    save_json(DATA_FILE, data)


def _record_targets(record: dict) -> list[str]:
    if not isinstance(record, dict):
        return []
    return parse_force_subscribe_targets(record.get("targets") or record.get("channel") or [])



def _full_send_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
    )

def _load_mute_data() -> dict:
    data = load_json(MUTE_FILE)
    return data if isinstance(data, dict) else {}

def _save_mute_data(data: dict):
    save_json(MUTE_FILE, data)

def _record_mute(chat_id: str, user_id: int, targets: list[str]):
    data = _load_mute_data()
    chat_bucket = data.setdefault(chat_id, {})
    chat_bucket[str(user_id)] = {
        "targets": parse_force_subscribe_targets(targets),
        "ts": int(time.time()),
    }
    _save_mute_data(data)

def _remove_mute_record(chat_id: str, user_id: int):
    data = _load_mute_data()
    chat_bucket = data.get(chat_id)
    if not isinstance(chat_bucket, dict):
        return
    if str(user_id) in chat_bucket:
        chat_bucket.pop(str(user_id), None)
        if not chat_bucket:
            data.pop(chat_id, None)
        _save_mute_data(data)

def _get_mute_record(chat_id: str, user_id: int) -> Optional[dict]:
    data = _load_mute_data()
    chat_bucket = data.get(chat_id)
    if not isinstance(chat_bucket, dict):
        return None
    return chat_bucket.get(str(user_id))


# ========= 设置强制频道 =========
@register_command("设置频道")
async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat.type.endswith("group"):
        return

    if not context.args:
        await update.message.reply_text("用法：/setchannel @频道用户名")
        return

    targets = parse_force_subscribe_targets(context.args)
    if not targets:
        return await update.message.reply_text("用法：/setchannel @频道或群组，可一次填写多个")
    chat_id = str(update.effective_chat.id)
    set_force_subscribe_targets(chat_id, targets)
    await update.message.reply_text(f"✅ 已开启强制关注：{'、'.join(targets)}")


# ========= 关闭强制 =========

async def clear_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    data = load_json(DATA_FILE)

    if chat_id in data:
        del data[chat_id]
        save_json(DATA_FILE, data)
        await update.message.reply_text("✅ 已关闭强制关注")
    else:
        await update.message.reply_text("当前未开启强制关注")


# ========= 发言检测 =========

async def _target_display_name(context: ContextTypes.DEFAULT_TYPE, target: str) -> str:
    try:
        chat = await context.bot.get_chat(target)
        return str(getattr(chat, "title", "") or getattr(chat, "username", "") or target)
    except Exception:
        return target


async def _missing_force_subscribe_targets(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, targets: list[str]
):
    """Return missing targets, or None if Telegram could not verify a target."""
    missing = []
    for target in targets:
        try:
            member = await context.bot.get_chat_member(target, user_id)
        except Exception as exc:
            print(f"强制关注检测失败 target={target}: {exc}")
            return None
        if member.status in {"left", "kicked"}:
            missing.append(target)
    return missing


async def _force_subscribe_keyboard(
    context: ContextTypes.DEFAULT_TYPE, targets: list[str], chat_id: str, user_id: int
) -> InlineKeyboardMarkup:
    rows = []
    for target in targets:
        name = await _target_display_name(context, target)
        rows.append([
            InlineKeyboardButton(
                f"📢 关注/加入 {name[:48]}",
                url=f"https://t.me/{target.lstrip('@')}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "✅ 我已全部关注/加入，解除禁言",
            callback_data=f"force_subscribe_check|{chat_id}|{user_id}",
        )
    ])
    return InlineKeyboardMarkup(rows)


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    group_config = get_group_whitelist(context).get(chat_id, {})
    if not isinstance(group_config, dict) or not group_config.get("force_subscribe", False):
        return

    targets = get_force_subscribe_targets(chat_id)
    if not targets:
        return

    apply_new_only = bool(group_config.get("force_subscribe_new_only", False))
    force_set_ts = int(group_config.get("force_subscribe_set_ts", 0) or 0)
    if apply_new_only and force_set_ts > 0:
        join_time = get_user_join_time(update.effective_chat.id, user_id)
        if join_time <= 0 or join_time <= force_set_ts:
            return

    try:
        group_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        if group_member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            return
    except Exception as exc:
        print("群成员检测失败：", exc)
        return

    missing_targets = await _missing_force_subscribe_targets(context, user_id, targets)
    if not missing_targets:
        return

    now = time.time()
    if now - user_warn_cooldown.get((chat_id, user_id), 0) < 30:
        return
    user_warn_cooldown[(chat_id, user_id)] = now

    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        _record_mute(chat_id, user_id, targets)
        name = group_member.user.full_name if group_member and group_member.user else ""
        add_mute(chat_id, user_id, name, source="force_subscribe")
    except Exception as exc:
        print("禁言失败：", exc)

    reply_markup = await _force_subscribe_keyboard(context, targets, chat_id, user_id)
    try:
        user = update.effective_user
        mention = user.full_name if user else "该用户"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ {mention} 请先关注下方全部频道或群组后再发言！关注后点击下方按钮解除禁言。",
            reply_markup=reply_markup,
        )
    except Exception as exc:
        print("发送提示失败：", exc)

    try:
        await update.message.delete()
    except Exception as exc:
        print("删除消息失败：", exc)


async def _try_unmute_if_followed(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, targets: list[str]
) -> bool:
    missing_targets = await _missing_force_subscribe_targets(context, user_id, targets)
    if missing_targets is None or missing_targets:
        return False
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=_full_send_permissions(),
        )
    except Exception as exc:
        print("解除禁言失败：", exc)
        return False
    return True


async def _unmute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=_full_send_permissions(),
        )
    except Exception as e:
        print("解除禁言失败：", e)
        return False
    return True


async def force_subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        _, chat_id_str, user_id_str = query.data.split("|", 2)
        chat_id = int(chat_id_str)
        target_user_id = int(user_id_str)
    except Exception:
        return

    if query.from_user.id != target_user_id:
        await query.answer("仅本人可操作。", show_alert=True)
        return

    record = _get_mute_record(str(chat_id), target_user_id)
    if not record:
        await query.answer("已解除或未记录。", show_alert=True)
        return

    targets = _record_targets(record)
    if not targets:
        await query.answer("记录异常，请联系管理员。", show_alert=True)
        return

    ok = await _try_unmute_if_followed(context, chat_id, target_user_id, targets)
    if ok:
        _remove_mute_record(str(chat_id), target_user_id)
        remove_mute(str(chat_id), target_user_id)
        try:
            if query.message:
                # The warning is no longer useful after the user has followed
                # every required target and been unmuted, so remove it too.
                await query.message.delete()
        except Exception as exc:
            # Deletion can fail in old messages or where Telegram denies it;
            # at least remove the now-stale action button in that case.
            print("删除强制关注提示失败：", exc)
            try:
                if query.message:
                    await query.message.edit_reply_markup(reply_markup=None)
            except Exception as markup_exc:
                print("移除按钮失败：", markup_exc)
        await query.answer("✅ 已解除禁言", show_alert=True)
    else:
        await query.answer("⚠️ 检测到仍未关注全部频道或群组，请完成关注后再试。", show_alert=True)


async def force_subscribe_sweep(context: ContextTypes.DEFAULT_TYPE):
    data = _load_mute_data()
    if not isinstance(data, dict) or not data:
        return
    group_cfg_map = get_group_whitelist(context)
    for chat_id_str, users in list(data.items()):
        if not isinstance(users, dict):
            continue
        chat_id = int(chat_id_str)
        group_cfg = group_cfg_map.get(chat_id_str, {})
        force_on = bool(group_cfg.get("force_subscribe", False)) if isinstance(group_cfg, dict) else False
        configured_targets = get_force_subscribe_targets(chat_id_str)
        for uid_str, info in list(users.items()):
            try:
                user_id = int(uid_str)
            except Exception:
                continue
            if not force_on or not configured_targets:
                ok = await _unmute_user(context, chat_id, user_id)
                if ok:
                    _remove_mute_record(chat_id_str, user_id)
                    remove_mute(chat_id_str, user_id)
                continue

            record = info if isinstance(info, dict) else {}
            record_targets = _record_targets(record) or configured_targets
            if not record_targets:
                continue
            ok = await _try_unmute_if_followed(context, chat_id, user_id, record_targets)
            if ok:
                _remove_mute_record(chat_id_str, user_id)
                remove_mute(chat_id_str, user_id)


async def unmute_force_subscribe_chat(context: ContextTypes.DEFAULT_TYPE, chat_id_str: str):
    data = _load_mute_data()
    users = data.get(chat_id_str)
    if not isinstance(users, dict):
        return
    chat_id = int(chat_id_str)
    for uid_str in list(users.keys()):
        try:
            user_id = int(uid_str)
        except Exception:
            continue
        ok = await _unmute_user(context, chat_id, user_id)
        if ok:
            _remove_mute_record(chat_id_str, user_id)
            remove_mute(chat_id_str, user_id)


# ========= 主程序 =========

def register_handle_force_handlers(app):
    app.add_handler(CommandHandler("setchannel", set_channel))
    app.add_handler(CommandHandler("clearchannel", clear_channel))
    app.add_handler(CallbackQueryHandler(force_subscribe_callback, pattern=r"^force_subscribe_check\|"))
    # 置于更前的 group，避免被同组 handler 阻断
    app.add_handler(
        MessageHandler(filters.ALL & (~filters.COMMAND), check_message),
        group=-10,
    )
    # 10 分钟扫一次，自动解除已关注用户
    app.job_queue.run_repeating(force_subscribe_sweep, interval=600, first=600)
