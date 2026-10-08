import json
import os
import re
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

from command_router import register_command
from typing import Optional
from utils import get_group_whitelist, load_json, safe_reply, save_json
from group.grouplist import get_user_join_time
from group.mute_registry import add_mute, remove_mute


DATA_FILE = "config_data/force_subscribe.json"
MUTE_FILE = "data/force_subscribe_mute.json"

# 用户提醒冷却
user_warn_cooldown = {}

# A button click is an immediate manual check. These two fallbacks also release
# members who follow the required channel but never click that button.
FORCE_SUBSCRIBE_SWEEP_INTERVAL_SECONDS = 60
# Do not emit the same unusable-target warning for every group message/sweep.
FORCE_SUBSCRIBE_TARGET_ERROR_LOG_INTERVAL_SECONDS = 10 * 60
_target_error_logged_at: dict[str, float] = {}
_TARGET_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


def _normalize_target(value: str) -> str:
    target = str(value or "").strip()
    # Accept a pasted @https://t.me/name as well as normal URLs/usernames.
    candidate = target[1:].strip() if target.startswith("@") else target
    if candidate.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        candidate = candidate.rsplit("/", 1)[-1].strip()
    candidate = candidate.split("?", 1)[0].strip().lstrip("@")
    if not candidate:
        return ""
    target = f"@{candidate}"
    # Public @usernames are required so Telegram can render a join button and
    # Telegram can resolve the target through getChatMember.
    return target if _TARGET_USERNAME_RE.fullmatch(target) else ""


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


def _is_unverifiable_force_subscribe_target_error(exc: Exception) -> bool:
    """Whether an API error means this configured target cannot be checked."""
    detail = str(exc).lower()
    markers = (
        "participant_id_invalid",
        "chat not found",
        "username not found",
        "bot is not a member",
        "bot was kicked",
        "forbidden",
    )
    return isinstance(exc, (BadRequest, Forbidden)) and any(
        marker in detail for marker in markers
    )


def _log_force_subscribe_target_error(target: str, exc: Exception) -> None:
    """Log unusable target configuration at most once per cooldown period."""
    now = time.monotonic()
    last_logged = _target_error_logged_at.get(target)
    if (
        last_logged is not None
        and now - last_logged < FORCE_SUBSCRIBE_TARGET_ERROR_LOG_INTERVAL_SECONDS
    ):
        return
    _target_error_logged_at[target] = now
    print(
        "强制关注目标不可验证（本次已跳过） "
        f"target={target}: {exc}。请确认目标存在，且机器人已加入并具有查询成员权限。"
    )


async def _missing_force_subscribe_targets(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, targets: list[str]
):
    """Return valid required targets the user has not joined.

    Explicit target-configuration errors (for example ``Participant_id_invalid``)
    are skipped instead of repeatedly logging and keeping users muted forever.
    Transient API failures still return ``None`` so no membership decision is
    made from incomplete information.
    """
    missing = []
    for target in targets:
        try:
            member = await context.bot.get_chat_member(target, user_id)
        except Exception as exc:
            if _is_unverifiable_force_subscribe_target_error(exc):
                _log_force_subscribe_target_error(target, exc)
                continue
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

    # A linked channel (or an admin sending as that channel) is not a normal
    # group member and cannot complete a follow requirement. Never delete those
    # channel-originated posts through the group force-subscribe flow.
    sender_chat = getattr(update.message, "sender_chat", None)
    if getattr(sender_chat, "type", "") == "channel":
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
    except Exception as exc:
        # Do not warn or delete a message unless the restriction actually took
        # effect. This protects channel owners/admins and any other protected
        # member Telegram refuses to restrict.
        print("禁言失败，跳过撤回消息：", exc)
        return

    _record_mute(chat_id, user_id, missing_targets)
    name = group_member.user.full_name if group_member and group_member.user else ""
    add_mute(chat_id, user_id, name, source="force_subscribe")

    reply_markup = await _force_subscribe_keyboard(
        context, missing_targets, chat_id, user_id
    )
    try:
        user = update.effective_user
        mention = user.full_name if user else "该用户"
        
   
        
        await safe_reply(
                update, 
                context, 
                text=f"⚠️ {mention} 请先关注下方全部频道或群组后再发言！关注后点击下方按钮解除禁言。",             
                reply_markup=reply_markup,
        )
        
        # await context.bot.send_message(
        #     chat_id=update.effective_chat.id,
        #     text=f"⚠️ {mention} 请先关注下方全部频道或群组后再发言！关注后点击下方按钮解除禁言。",
        #     reply_markup=reply_markup,
        # )
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
        # try:
        #     if query.message:
        #         # The warning is no longer useful after the user has followed
        #         # every required target and been unmuted, so remove it too.
        #         await query.message.delete()
        # except Exception as exc:
        #     # Deletion can fail in old messages or where Telegram denies it;
        #     # at least remove the now-stale action button in that case.
        #     print("删除强制关注提示失败：", exc)
        #     try:
        #         if query.message:
        #             await query.message.edit_reply_markup(reply_markup=None)
        #     except Exception as markup_exc:
        #         print("移除按钮失败：", markup_exc)
        await query.answer("✅ 已解除禁言", show_alert=True)
    else:
        await query.answer("⚠️ 检测到仍未关注全部频道或群组，请完成关注后再试。", show_alert=True)


async def force_subscribe_membership_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Immediately recheck muted users when they join a required target.

    A user can subscribe from the channel link without returning to press
    “我已关注”. When Telegram delivers that target's ``chat_member`` update,
    verify all required targets and automatically lift the restriction.
    """
    member_update = getattr(update, "chat_member", None)
    if not member_update:
        return

    target = _normalize_target(getattr(getattr(member_update, "chat", None), "username", ""))
    if not target:
        # Force-subscribe targets are public @usernames, so an update without a
        # username cannot match a configured target.
        return

    new_member = getattr(member_update, "new_chat_member", None)
    user = getattr(new_member, "user", None)
    user_id = getattr(user, "id", None)
    status = str(getattr(new_member, "status", "") or "").lower()
    if not user_id or status in {"left", "kicked"}:
        return

    mute_data = _load_mute_data()
    for group_chat_id_str, users in list(mute_data.items()):
        if not isinstance(users, dict):
            continue
        record = users.get(str(user_id))
        targets = _record_targets(record)
        if not targets or target.lower() not in {item.lower() for item in targets}:
            continue
        try:
            group_chat_id = int(group_chat_id_str)
        except (TypeError, ValueError):
            continue

        if await _try_unmute_if_followed(context, group_chat_id, user_id, targets):
            _remove_mute_record(group_chat_id_str, user_id)
            remove_mute(group_chat_id_str, user_id)
            print(
                "强制关注自动解除禁言 "
                f"group={group_chat_id} user={user_id} target={target}"
            )


async def force_subscribe_sweep(context: ContextTypes.DEFAULT_TYPE):
    data = _load_mute_data()
    if not isinstance(data, dict) or not data:
        return
    group_cfg_map = get_group_whitelist(context)
    for chat_id_str, users in list(data.items()):
        if not isinstance(users, dict):
            continue
        try:
            chat_id = int(chat_id_str)
        except (TypeError, ValueError):
            continue
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
    # Required channels/groups send a chat_member update when a muted user
    # joins. Use it for immediate unmute; the periodic sweep remains a fallback.
    app.add_handler(
        ChatMemberHandler(
            force_subscribe_membership_update,
            chat_member_types=ChatMemberHandler.CHAT_MEMBER,
        ),
        group=13,
    )
    # 置于更前的 group，避免被同组 handler 阻断
    app.add_handler(
        MessageHandler(filters.ALL & (~filters.COMMAND), check_message),
        group=-10,
    )
    # Fallback for membership updates Telegram did not deliver: check once per
    # minute so users who already followed do not remain muted for 10 minutes.
    app.job_queue.run_repeating(
        force_subscribe_sweep,
        interval=FORCE_SUBSCRIBE_SWEEP_INTERVAL_SECONDS,
        first=FORCE_SUBSCRIBE_SWEEP_INTERVAL_SECONDS,
    )
