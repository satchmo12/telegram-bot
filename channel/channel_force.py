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

def _record_mute(chat_id: str, user_id: int, channel_username: str):
    data = _load_mute_data()
    chat_bucket = data.setdefault(chat_id, {})
    chat_bucket[str(user_id)] = {
        "channel": channel_username,
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

    channel_username = context.args[0]
    chat_id = str(update.effective_chat.id)

    data = load_json(DATA_FILE)
    data[chat_id] = channel_username
    save_json(DATA_FILE,data)

    await update.message.reply_text(
        f"✅ 已开启强制关注 {channel_username}"
    )


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

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    group_config = get_group_whitelist(context).get(chat_id, {})
    if not isinstance(group_config, dict) or not group_config.get("force_subscribe", False):
        return

    data = load_json(DATA_FILE)

    # 未设置强制
    if chat_id not in data:
        return

    channel_username = data[chat_id]
    apply_new_only = bool(group_config.get("force_subscribe_new_only", True))
    force_set_ts = int(group_config.get("force_subscribe_set_ts", 0) or 0)
    if apply_new_only and force_set_ts > 0:
        join_time = get_user_join_time(update.effective_chat.id, user_id)
        if join_time <= 0 or join_time <= force_set_ts:
            return

    try:
        group_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        if group_member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            return
        member = await context.bot.get_chat_member(channel_username, user_id)
    except Exception as e:
        print("检测失败：", e)
        return

    if member.status in ["left", "kicked"]:
        # 冷却机制
        now = time.time()
        if user_id in user_warn_cooldown:
            if now - user_warn_cooldown[user_id] < 30:
                return

        user_warn_cooldown[user_id] = now

        # 禁言并记录
        try:
            await context.bot.restrict_chat_member(
                update.effective_chat.id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
            _record_mute(chat_id, user_id, channel_username)
            name = group_member.user.full_name if group_member and group_member.user else ""
            add_mute(chat_id, user_id, name, source="force_subscribe")
        except Exception as e:
            print("禁言失败：", e)

        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 点击关注频道",
                    url=f"https://t.me/{channel_username.replace('@','')}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ 我已关注，解除禁言",
                    callback_data=f"force_subscribe_check|{chat_id}|{user_id}",
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            # 先发送提示，避免删除后 reply 失败
            user = update.effective_user
            mention = user.full_name if user else "该用户"
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ {mention} 请先关注频道后再发言！关注后点击下方按钮解除禁言。",
                reply_markup=reply_markup,
            )
        except Exception as e:
            print("发送提示失败：", e)

        try:
            await update.message.delete()
        except Exception as e:
            # 删除失败也不影响提示
            print("删除消息失败：", e)


async def _try_unmute_if_followed(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, channel_username: str) -> bool:
    try:
        member = await context.bot.get_chat_member(channel_username, user_id)
    except Exception as e:
        print("检测失败：", e)
        return False

    if member.status in ["left", "kicked"]:
        return False

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

    channel_username = record.get("channel")
    if not channel_username:
        await query.answer("记录异常，请联系管理员。", show_alert=True)
        return

    ok = await _try_unmute_if_followed(context, chat_id, target_user_id, channel_username)
    if ok:
        _remove_mute_record(str(chat_id), target_user_id)
        remove_mute(str(chat_id), target_user_id)
        try:
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            print("移除按钮失败：", e)
        await query.answer("✅ 已解除禁言", show_alert=True)
    else:
        await query.answer("⚠️ 检测到仍未关注，请先关注频道。", show_alert=True)


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
        for uid_str, info in list(users.items()):
            try:
                user_id = int(uid_str)
            except Exception:
                continue
            if not force_on:
                ok = await _unmute_user(context, chat_id, user_id)
                if ok:
                    _remove_mute_record(chat_id_str, user_id)
                    remove_mute(chat_id_str, user_id)
                continue

            channel_username = (info or {}).get("channel")
            if not channel_username:
                continue
            ok = await _try_unmute_if_followed(context, chat_id, user_id, channel_username)
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
