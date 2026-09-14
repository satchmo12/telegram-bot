# -*- coding: utf-8 -*-
from datetime import datetime
import asyncio
from urllib.parse import urlparse
import os
import random
import re
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, TypeHandler, filters
from telegram.error import BadRequest, Forbidden

from channel.channel_config import USER_MESSAGE_FILE
from channel.discussion_mapping import resolve_discussion_message
from utils import BOT_USER_FILE, _can_manage, is_super_admin, load_json, save_json
from admin_permissions import get_delegated_admin_ids, has_admin_permission

PUBLISH_CONFIG_FILE = "config_data/publish_config.json"
ANON_CHAT_FILE = "data/anon_chat.json"
USER_MESSAGE_FILE = "data/user_message_file.json"
BOTTLE_HISTORY_FILE = "data/bottle_history.json"
PENDING_SUBMISSIONS_FILE = "data/pending_submissions.json"

CALLBACK_PREFIX = "publish"
BUTTON_TEXT_MAX_LENGTH = 64
MAX_PUBLISH_BUTTONS = 20
PENDING_PROOF_KEY = "publish_pending_proof_id"
REJECT_REASON_KEY = "publish_reject_reason"
COMMENT_TARGET_KEY = "publish_comment_target"
COMMENT_MAP_FILE = "data/publish_comment_map.json"
KEYWORD_MAP_FILE = "data/publish_keyword_map.json"
KEYWORD_INPUT_KEY = "publish_keyword_search"
KEYWORD_RESULTS_KEY = "publish_keyword_results"
KEYWORD_LABEL_INPUT_KEY = "publish_keyword_label_input"

# =========================
# 配置读写
# =========================

def load_publish_config():
    default = {
        "channel_id": None,
        "review_enabled": False,
        "daily_limit": 0,
        "ads_enabled": False,
        "ads": [],
        "buttons": [],
        # Keep existing bots' public buttons visible until an owner changes
        # this setting in 投稿配置.
        "bottom_buttons_enabled": True,
        # Require an extra proof message before a non-owner submission is sent
        # to the reviewer. It only serves as review evidence and is never posted.
        "proof_required": False,
        # When enabled, reviewer must type a rejection reason for the submitter.
        "reject_reason_required": False,
        # Comments are submitted through review and mirrored to forward_channel_id.
        "comment_forward_enabled": False,
        "forward_channel_id": None,
        # Labels such as 艺名 / 联系方式 used to extract routing keywords.
        "keyword_extract_labels": [],
        # Preserve the existing behavior: one 投稿 action can send multiple posts.
        "continuous_submission_enabled": True,
        # Visibility of normal start-panel buttons controlled by the owner.
        "custom_menu_buttons": {
            "channel_clone": True,
            "telethon_manage": True,
            "bot_channel_config": True,
            "group_config": True,
            "global_ad_config": True,
        },
        "submission_enabled": False,
        "random_view_enabled": False,
    }

    data = load_json(PUBLISH_CONFIG_FILE)
    if not isinstance(data, dict) or not data:
        data = default.copy()
        save_json(PUBLISH_CONFIG_FILE, data)
        return data

    changed = False
    for key, value in default.items():
        if key not in data:
            data[key] = value.copy() if isinstance(value, (list, dict)) else value
            changed = True
    if changed:
        save_json(PUBLISH_CONFIG_FILE, data)

    return data


def save_publish_config(data):
    save_json(PUBLISH_CONFIG_FILE, data)


def _comment_map_key(channel_id, message_id) -> str:
    return f"{channel_id}:{message_id}"


def _load_comment_map() -> dict:
    data = load_json(COMMENT_MAP_FILE)
    return data if isinstance(data, dict) else {}


def _save_comment_map(data: dict) -> None:
    # Keep the mapping bounded; old channel posts no longer need new comments.
    if len(data) > 3000:
        oldest = sorted(
            data.items(),
            key=lambda item: int((item[1] or {}).get("created_at", 0) or 0),
        )[: len(data) - 3000]
        for key, _ in oldest:
            data.pop(key, None)
    save_json(COMMENT_MAP_FILE, data)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _publish_failure_detail(exc: Exception) -> str:
    """Return an actionable review error without hiding the real failure."""
    if isinstance(exc, Forbidden):
        return "机器人在目标频道或讨论组没有发言/发布权限。"
    if isinstance(exc, BadRequest):
        raw = str(exc)
        lower = raw.lower()
        if "chat not found" in lower:
            return "频道或讨论组 ID 不正确，或机器人尚未加入目标频道。"
        if "message to reply not found" in lower:
            return "未找到主频道帖子的讨论组映射。请使用新发布的主频道帖子评论。"
        if "not enough rights" in lower or "forbidden" in lower:
            return "机器人缺少目标频道或讨论组的发言权限。"
        if "message can't be edited" in lower or "message is not modified" in lower:
            return "无法移除主频道帖子的底部按钮以启用原生评论。请确认该帖子由机器人发布。"
        return f"Telegram 返回：{raw[:240]}"
    return str(exc)[:300] or "未知错误"


def _comment_target_from_submission(submission: dict):
    target = submission.get("comment_target")
    return target if isinstance(target, dict) else {}


def _keyword_labels(config: dict) -> list[str]:
    labels = config.get("keyword_extract_labels", [])
    if not isinstance(labels, list):
        return []
    result = []
    for label in labels:
        value = str(label or "").strip()
        if value and value not in result:
            result.append(value[:30])
    return result


def _normalize_routing_keyword(value: str) -> str:
    value = str(value or "").strip()
    # Tags are commonly used for stage names, but searches should use the name.
    if value.startswith("#"):
        value = value[1:].strip()
    return value.lower()


def _extract_routing_keywords(msg, config: dict) -> list[dict]:
    text = str(getattr(msg, "text", None) or getattr(msg, "caption", None) or "")
    if not text:
        return []
    result = []
    for label in _keyword_labels(config):
        pattern = re.compile(
            rf"{re.escape(label)}[：:]?\s*([^\n\r]+)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            raw = match.group(1).strip()
            key = _normalize_routing_keyword(raw)
            if key and not any(item["key"] == key and item["label"] == label for item in result):
                result.append({"label": label, "key": key, "raw": raw})
    return result


def _load_keyword_map() -> dict:
    data = load_json(KEYWORD_MAP_FILE)
    return data if isinstance(data, dict) else {}


def _save_keyword_map(data: dict) -> None:
    # Keep enough history for routing while avoiding unlimited growth.
    for key, records in list(data.items()):
        if not isinstance(records, list):
            data.pop(key, None)
            continue
        data[key] = records[-30:]
    if len(data) > 3000:
        for key in list(data)[: len(data) - 3000]:
            data.pop(key, None)
    save_json(KEYWORD_MAP_FILE, data)


def _register_post_keywords(entries: list[dict], channel_id, message_id) -> None:
    if not entries:
        return
    data = _load_keyword_map()
    now = int(time.time())
    for entry in entries:
        key = entry.get("key")
        if not key:
            continue
        records = data.setdefault(key, [])
        records[:] = [
            item for item in records
            if not (
                str(item.get("channel_id")) == str(channel_id)
                and int(item.get("channel_message_id", 0) or 0) == int(message_id)
                and item.get("label") == entry.get("label")
            )
        ]
        records.append({
            "label": entry.get("label", ""),
            "raw": entry.get("raw", key),
            "channel_id": channel_id,
            "channel_message_id": message_id,
            "created_at": now,
        })
    _save_keyword_map(data)


def _update_keyword_comment_mapping(channel_id, message_id, discussion_chat_id, discussion_message_id) -> None:
    data = _load_keyword_map()
    changed = False
    for records in data.values():
        if not isinstance(records, list):
            continue
        for record in records:
            if (
                str(record.get("channel_id")) == str(channel_id)
                and int(record.get("channel_message_id", 0) or 0) == int(message_id)
            ):
                record["discussion_chat_id"] = discussion_chat_id
                record["discussion_message_id"] = discussion_message_id
                changed = True
    if changed:
        _save_keyword_map(data)


def _find_keyword_routes(query: str) -> list[dict]:
    key = _normalize_routing_keyword(query)
    if not key:
        return []
    data = _load_keyword_map()
    matches = []
    for stored_key, records in data.items():
        if key not in stored_key and stored_key not in key:
            continue
        for record in records or []:
            if isinstance(record, dict) and record.get("discussion_message_id"):
                matches.append({**record, "key": stored_key})
    matches.sort(key=lambda item: int(item.get("created_at", 0) or 0), reverse=True)
    return matches[:10]


def _fallback_channel_message_link(channel_id, message_id):
    channel = str(channel_id or "")
    if channel.startswith("-100") and str(message_id).isdigit():
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return None


async def _route_message_link(context: ContextTypes.DEFAULT_TYPE, route: dict):
    """Build a public link when possible, otherwise use Telegram's private link."""
    channel_id = route.get("channel_id")
    message_id = route.get("channel_message_id")
    try:
        chat = await context.bot.get_chat(channel_id)
        username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
        if username and message_id:
            return f"https://t.me/{username}/{message_id}"
    except Exception:
        pass
    return _fallback_channel_message_link(channel_id, message_id)


def _keyword_route_keyboard(route: dict, link: str, enabled: bool):
    rows = []
    if link:
        rows.append([InlineKeyboardButton("🔗 查看对应消息", url=link)])
    rows.extend(create_post_keyboard(enabled).inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _keyword_settings_text(config: dict) -> str:
    labels = _keyword_labels(config)
    sample = "【艺名】：#丹丹\n【联系方式】：@dandan"
    return (
        "🔑 评论关键词设置\n\n"
        f"当前提取标签：{'、'.join(labels) if labels else '未设置'}\n\n"
        "管理员发布到主频道时，机器人会从内容中提取这些标签对应的值，"
        "并映射到主频道消息 ID 与讨论组评论 ID。\n\n"
        f"示例：\n{sample}\n"
        "设置“艺名”可提取丹丹；设置“联系方式”可提取 @dandan。"
    )


def _keyword_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 设置提取标签", callback_data="publish:keywords_set")],
        [InlineKeyboardButton("🗑 清空标签", callback_data="publish:keywords_clear")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")],
    ])


def _load_pending_submissions() -> dict:
    """Load review queue. JSON storage keeps pending reviews after a restart."""
    data = load_json(PENDING_SUBMISSIONS_FILE)
    return data if isinstance(data, dict) else {}


def _save_pending_submissions(data: dict) -> None:
    save_json(PENDING_SUBMISSIONS_FILE, data)


def _owner_id(context: ContextTypes.DEFAULT_TYPE):
    """Read the owner from this app instead of a process-global multi-bot value."""
    try:
        return int(context.application.bot_data.get("owner_id"))
    except (TypeError, ValueError):
        return None


def _review_keyboard(submission_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 通过并发布",
                callback_data=f"publish:review_approve:{submission_id}",
            ),
            InlineKeyboardButton(
                "❌ 拒绝",
                callback_data=f"publish:review_reject:{submission_id}",
            ),
        ]
    ])


def _submission_author_text(msg) -> str:
    user = getattr(msg, "from_user", None)
    if not user:
        return "未知用户"
    name = " ".join(
        part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if part
    ).strip()
    username = getattr(user, "username", None)
    if username:
        return f"{name or '用户'} (@{username}, ID: {user.id})"
    return f"{name or '用户'} (ID: {user.id})"


def _append_published_submission(
    msg,
    published_message_id: int,
    published_channel_id=None,
) -> None:
    """Store a published submission for the existing random-view feature."""
    user = getattr(msg, "from_user", None)
    data = _load_cannel_message()
    data.append({
        "user_id": getattr(user, "id", None),
        "user_chat_id": msg.chat_id,
        "username": getattr(user, "username", None),
        "user_message_id": msg.message_id,
        "channel_message_id": published_message_id,
        "channel_id": published_channel_id,
        "publish_time": int(time.time()),
    })
    save_json(USER_MESSAGE_FILE, data)


def _review_prompt_text(submission: dict) -> str:
    proof_status = "已上传" if submission.get("proof_message_id") else "未要求"
    return (
        "📝 收到新的投稿，请审核。\n"
        f"投稿人：{submission.get('author', '未知用户')}\n"
        f"审核凭证：{proof_status}\n"
        f"投稿编号：{submission.get('id', '未知')}"
    )


def _reject_reason_cancel_keyboard(submission_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ 取消拒绝",
                callback_data=f"publish:review_reject_cancel:{submission_id}",
            )
        ]
    ])


async def _send_submission_for_review(
    context: ContextTypes.DEFAULT_TYPE,
    submission_id: str,
    submission: dict,
):
    """Forward the submission and optional proof to owner, then send controls."""
    owner_id = _owner_id(context)
    if owner_id is None:
        raise RuntimeError("未配置机器人所有者")

    await context.bot.forward_message(
        chat_id=owner_id,
        from_chat_id=submission["user_chat_id"],
        message_id=submission["user_message_id"],
    )
    if submission.get("proof_message_id"):
        await context.bot.forward_message(
            chat_id=owner_id,
            from_chat_id=submission["proof_chat_id"],
            message_id=submission["proof_message_id"],
        )

    # Send review work to the owner and every delegated reviewer. Each recipient
    # gets independent controls; the persisted submission state prevents duplicate publishing.
    reviewer_ids = {int(owner_id), *get_delegated_admin_ids(context, "submission_review")}
    prompt = _review_prompt_text({**submission, "id": submission_id})
    failures = []
    for reviewer_id in reviewer_ids:
        try:
            await context.bot.send_message(
                chat_id=reviewer_id,
                text=prompt,
                reply_markup=_review_keyboard(submission_id),
            )
        except Exception as exc:
            failures.append(str(exc))
    if len(failures) == len(reviewer_ids):
        raise RuntimeError("所有审核管理员均无法接收审核通知，请先让管理员私聊机器人。")


async def _finalize_rejection(
    context: ContextTypes.DEFAULT_TYPE,
    submission_id: str,
    submission: dict,
    reason: str = "",
):
    submission["status"] = "rejected"
    submission["reviewed_at"] = int(time.time())
    if reason:
        submission["reject_reason"] = reason

    pending = _load_pending_submissions()
    pending[submission_id] = submission
    _save_pending_submissions(pending)

    notice = "❌ 很抱歉，您的投稿未通过审核。"
    if reason:
        notice += f"\n\n审核原因：{reason}"
    try:
        await context.bot.send_message(chat_id=submission["user_chat_id"], text=notice)
    except Exception as exc:
        print("投稿拒绝通知失败:", exc)


async def _capture_comment_source_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Capture linked-discussion copies when Telegram sends the Bot API update."""
    msg = update.effective_message
    if not msg:
        return
    sender_chat = getattr(msg, "sender_chat", None)
    origin = getattr(msg, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin else None
    legacy_forward_chat = getattr(msg, "forward_from_chat", None)
    channel_id = (
        getattr(sender_chat, "id", None)
        or getattr(origin_chat, "id", None)
        or getattr(legacy_forward_chat, "id", None)
    )
    channel_message_id = (
        getattr(origin, "message_id", None)
        or getattr(msg, "forward_from_message_id", None)
    )
    if channel_id is None or channel_message_id is None:
        return
    config = load_publish_config()
    if not bool(config.get("comment_forward_enabled", False)) or str(channel_id) != str(config.get("channel_id")):
        return
    records = _load_comment_map()
    records[_comment_map_key(channel_id, channel_message_id)] = {
        "discussion_chat_id": msg.chat_id,
        "discussion_message_id": msg.message_id,
        "created_at": int(time.time()),
    }
    _save_comment_map(records)
    _update_keyword_comment_mapping(channel_id, channel_message_id, msg.chat_id, msg.message_id)
    print(f"✅ 已通过 Bot API 写入 discussion 映射 {channel_id}/{channel_message_id} -> {msg.chat_id}/{msg.message_id}")


async def _start_comment_submission(query, context: ContextTypes.DEFAULT_TYPE, config: dict):
    parts = query.data.split(":")
    if len(parts) != 4:
        return await query.answer("评论数据无效。", show_alert=True)
    if not bool(config.get("comment_forward_enabled", False)):
        return await query.answer("评论并转发功能未开启。", show_alert=True)

    channel_id = _as_int(parts[2])
    message_id = _as_int(parts[3])
    if channel_id is None or message_id is None:
        return await query.answer("评论数据无效。", show_alert=True)
    if str(channel_id) != str(config.get("channel_id")):
        return await query.answer("该帖子不是当前主频道的帖子。", show_alert=True)

    user = query.from_user
    context.user_data[COMMENT_TARGET_KEY] = {
        "channel_id": channel_id,
        "message_id": message_id,
    }
    context.user_data["waiting_post"] = True
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "💬 请发送要评论的内容。\n"
                "内容会按投稿流程审核；通过后会评论到该帖子下，并发布到转发频道。"
            ),
        )
    except Exception:
        context.user_data.pop(COMMENT_TARGET_KEY, None)
        context.user_data["waiting_post"] = False
        return await query.answer(
            "请先私聊机器人并发送 /start，再点击评论。",
            show_alert=True,
        )
    return await query.answer("请到与机器人的私聊发送评论内容。", show_alert=True)


async def _comment_deep_link(
    context: ContextTypes.DEFAULT_TYPE,
    channel_id,
    message_id,
):
    """Create a Telegram start link so the comment button opens the bot directly."""
    username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not username:
        try:
            me = await context.bot.get_me()
            username = str(getattr(me, "username", "") or "").strip().lstrip("@")
        except Exception as exc:
            print("获取机器人用户名失败，评论按钮将使用回调模式:", exc)
            return None
    if not username:
        return None
    parameter = f"comment_{channel_id}_{message_id}"
    return f"https://t.me/{username}?start={parameter}"


async def handle_comment_start_parameter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parameter: str,
) -> bool:
    """Start a comment submission from a Telegram deep link parameter."""
    if not isinstance(parameter, str) or not parameter.startswith("comment_"):
        return False
    parts = parameter.split("_", 2)
    if len(parts) != 3:
        return False
    channel_id = _as_int(parts[1])
    message_id = _as_int(parts[2])
    config = load_publish_config()
    if (
        channel_id is None
        or message_id is None
        or not bool(config.get("comment_forward_enabled", False))
        or str(channel_id) != str(config.get("channel_id"))
    ):
        if update.message:
            await update.message.reply_text("❗ 该评论入口已失效或评论并转发功能未开启。")
        return True

    context.user_data[COMMENT_TARGET_KEY] = {
        "channel_id": channel_id,
        "message_id": message_id,
    }
    context.user_data["waiting_post"] = True
    if update.message:
        await update.message.reply_text(
            "💬 请发送要评论的内容。\n"
            "内容会按投稿流程审核；通过后会评论到该帖子下，并发布到转发频道。"
        )
    return True


async def _resolve_discussion_mapping_in_background(
    context: ContextTypes.DEFAULT_TYPE, channel_id: int, message_id: int
) -> None:
    """Give Bot API a short chance, then use Telethon only as a fallback."""
    await asyncio.sleep(1.5)
    key = _comment_map_key(channel_id, message_id)
    if _load_comment_map().get(key):
        return
    mapping = await resolve_discussion_message(context, channel_id, message_id)
    if not mapping:
        return
    discussion_chat_id, discussion_message_id = mapping
    records = _load_comment_map()
    records[key] = {
        "discussion_chat_id": discussion_chat_id,
        "discussion_message_id": discussion_message_id,
        "created_at": int(time.time()),
    }
    _save_comment_map(records)
    _update_keyword_comment_mapping(
        channel_id, message_id, discussion_chat_id, discussion_message_id
    )
    print(
        "✅ 已通过协议号补齐 discussion 映射 "
        f"{channel_id}/{message_id} -> {discussion_chat_id}/{discussion_message_id}"
    )


def _schedule_discussion_mapping(
    context: ContextTypes.DEFAULT_TYPE, channel_id, message_id
) -> None:
    try:
        channel_id, message_id = int(channel_id), int(message_id)
    except (TypeError, ValueError):
        return
    context.application.create_task(
        _resolve_discussion_mapping_in_background(context, channel_id, message_id),
        name=f"discussion-map:{channel_id}:{message_id}",
    )


async def _copy_submission_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict,
    target_channel_id,
    config: dict,
    *,
    add_comment_button: bool = False,
):
    """Copy original user content to a channel and optionally append comment action."""
    # The main post cannot have inline markup: Telegram otherwise hides its
    # native comment thread.
    published = await context.bot.copy_message(
        chat_id=target_channel_id,
        from_chat_id=submission["user_chat_id"],
        message_id=submission["user_message_id"],
        reply_markup=None if add_comment_button else publish_buttons_keyboard(config),
    )
    if submission.get("keyword_entries"):
        _register_post_keywords(
            submission["keyword_entries"],
            target_channel_id,
            published.message_id,
        )
    if add_comment_button:
        # Publishing must stay fast. Bot API mapping is immediate when its
        # automatic-forward update arrives; otherwise the protocol lookup runs
        # in the background and never blocks the success response.
        _schedule_discussion_mapping(context, target_channel_id, published.message_id)
        # 暂时停用“参与讨论请点击下方按钮”辅助消息及其评论按钮。
        # 主频道正文保持无 InlineKeyboard，避免影响 Telegram 原生评论显示。
        pass
    return published


async def _publish_comment_and_forward(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict,
    config: dict,
):
    """Post approved content as a linked-discussion comment and mirror it onward."""
    target = _comment_target_from_submission(submission)
    main_channel_id = _as_int(target.get("channel_id"))
    main_message_id = _as_int(target.get("message_id"))
    forward_channel_id = _as_int(config.get("forward_channel_id"))
    if main_channel_id is None or main_message_id is None:
        raise RuntimeError("评论目标无效")
    if forward_channel_id is None:
        raise RuntimeError("未设置转发频道")

    mapping = _load_comment_map().get(_comment_map_key(main_channel_id, main_message_id))
    if not isinstance(mapping, dict):
        raise RuntimeError(
            "未找到主频道帖子的讨论组映射。请确认主频道已绑定讨论组，机器人是讨论组管理员。"
        )

    discussion_chat_id = mapping.get("discussion_chat_id")
    discussion_message_id = mapping.get("discussion_message_id")
    if discussion_chat_id is None or discussion_message_id is None:
        raise RuntimeError("评论讨论组映射无效")

    # Main posts created in comment mode have no inline markup, so this reply
    # is shown as a native comment below the original channel post.

    await context.bot.copy_message(
        chat_id=discussion_chat_id,
        from_chat_id=submission["user_chat_id"],
        message_id=submission["user_message_id"],
        reply_to_message_id=discussion_message_id,
    )
    return await _copy_submission_to_channel(
        context,
        submission,
        forward_channel_id,
        config,
    )


async def _handle_review_callback(query, context: ContextTypes.DEFAULT_TYPE, config: dict):
    """Approve, reject, or collect a rejection reason for a submission."""
    parts = query.data.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        return await query.answer("审核数据无效。", show_alert=True)

    if not has_admin_permission(context, query.from_user.id, "submission_review"):
        return await query.answer("你没有稿件审核权限。", show_alert=True)

    action, submission_id = parts[1], parts[2]
    pending = _load_pending_submissions()
    submission = pending.get(submission_id)
    if not isinstance(submission, dict):
        return await query.answer("该投稿不存在或已清理。", show_alert=True)

    status = submission.get("status", "pending")
    if status != "pending":
        status_text = {
            "awaiting_proof": "仍在等待凭证",
            "approved": "已通过",
            "rejected": "已拒绝",
            "publishing": "正在发布中",
        }.get(status, "已处理")
        return await query.answer(f"该投稿{status_text}，请勿重复处理。", show_alert=True)

    if action == "review_reject_cancel":
        reject_stage = (context.user_data or {}).get(REJECT_REASON_KEY)
        if isinstance(reject_stage, dict) and reject_stage.get("submission_id") == submission_id:
            context.user_data.pop(REJECT_REASON_KEY, None)
        await query.answer("已取消拒绝")
        return await query.edit_message_text(
            _review_prompt_text({**submission, "id": submission_id}),
            reply_markup=_review_keyboard(submission_id),
        )

    if action == "review_approve":
        channel_id = config.get("channel_id")
        if not channel_id:
            return await query.answer("未配置发布频道，无法发布。", show_alert=True)

        # Persist a transient state first to prevent two owner clicks from publishing twice.
        submission["status"] = "publishing"
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        submission_kind = str(submission.get("submission_kind", "main"))
        target_channel_id = _as_int(submission.get("target_channel_id")) or channel_id
        try:
            # Only original submission content is published; proof is review-only.
            if submission_kind == "comment":
                published = await _publish_comment_and_forward(context, submission, config)
                target_channel_id = _as_int(config.get("forward_channel_id"))
            else:
                published = await _copy_submission_to_channel(
                    context,
                    submission,
                    target_channel_id,
                    config,
                )
        except Exception as exc:
            submission["status"] = "pending"
            pending[submission_id] = submission
            _save_pending_submissions(pending)
            detail = _publish_failure_detail(exc)
            print("审核投稿发布失败:", exc)
            try:
                await query.message.reply_text(f"❌ 发布失败详情：\n{detail}")
            except Exception as notify_exc:
                print("发送发布失败详情失败:", notify_exc)
            return await query.answer("发布失败，详情已发送。", show_alert=True)

        submission["status"] = "approved"
        submission["reviewed_at"] = int(time.time())
        submission["channel_message_id"] = published.message_id
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        data = _load_cannel_message()
        data.append({
            "user_id": submission.get("user_id"),
            "user_chat_id": submission["user_chat_id"],
            "username": submission.get("username"),
            "user_message_id": submission["user_message_id"],
            "channel_message_id": published.message_id,
            "channel_id": target_channel_id,
            "publish_time": int(time.time()),
        })
        save_json(USER_MESSAGE_FILE, data)

        try:
            await context.bot.send_message(
                chat_id=submission["user_chat_id"],
                text="✅ 您的投稿已审核通过，并已发布到频道。",
            )
        except Exception as exc:
            print("投稿通过通知失败:", exc)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            print("更新审核消息失败:", exc)
        await query.answer("投稿已通过并发布。")
        return await query.message.reply_text("✅ 已通过投稿并发布到频道。")

    if action == "review_reject":
        if bool(config.get("reject_reason_required", False)):
            review_chat = getattr(getattr(query.message, "chat", None), "id", None)
            context.user_data[REJECT_REASON_KEY] = {
                "submission_id": submission_id,
                "review_chat_id": review_chat,
                "review_message_id": query.message.message_id,
            }
            await query.answer()
            return await query.edit_message_text(
                "❌ 请发送拒绝原因。\n\n"
                "该原因会通知投稿人；发送“取消”可返回审核。",
                reply_markup=_reject_reason_cancel_keyboard(submission_id),
            )

        await _finalize_rejection(context, submission_id, submission)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            print("更新审核消息失败:", exc)
        await query.answer("投稿已拒绝。")
        return await query.message.reply_text("❌ 已拒绝投稿，已通知投稿人。")

    return await query.answer("未知审核操作。", show_alert=True)


def _publish_buttons(config: dict) -> list[dict]:
    """Return valid custom buttons from the current publish configuration."""
    buttons = config.get("buttons", []) if isinstance(config, dict) else []
    if not isinstance(buttons, list):
        return []
    return [button for button in buttons if isinstance(button, dict)]


def _button_settings_text(config: dict) -> str:
    buttons = _publish_buttons(config)
    lines = [
        "🔘 投稿按钮设置",
        "投稿发布到频道时，会在消息下方附加这些链接按钮。",
        "",
    ]
    if not buttons:
        lines.append("当前未设置按钮。")
    else:
        lines.append(f"当前已设置 {len(buttons)} 个按钮：")
        for button in buttons:
            lines.append(
                f"#{button.get('id')} {button.get('text', '未命名')}\n{button.get('url', '')}"
            )
    return "\n".join(lines)


def publish_buttons_keyboard(
    config: dict,
    *,
    comment_channel_id=None,
    comment_message_id=None,
    comment_url: str = None,
):
    """Build buttons for a published post, including an optional comment action."""
    rows = []
    if bool(config.get("bottom_buttons_enabled", True)):
        for button in _publish_buttons(config):
            button_text = str(button.get("text", "")).strip()
            button_url = str(button.get("url", "")).strip()
            if button_text and button_url:
                rows.append([InlineKeyboardButton(button_text, url=button_url)])

    # 不展示评论按钮
    # if (
    #     bool(config.get("comment_forward_enabled", False))
    #     and comment_channel_id is not None
    #     and comment_message_id is not None
    # ):
    #     rows.append([
    #         InlineKeyboardButton(
    #             "💬 评论",
    #             url=comment_url,
    #         )
    #         if comment_url
    #         else InlineKeyboardButton(
    #             "💬 评论",
    #             callback_data=(
    #                 f"publish:comment:{comment_channel_id}:{comment_message_id}"
    #             ),
    #         )
    #     ])
    return InlineKeyboardMarkup(rows) if rows else None


def publish_button_settings_keyboard(config: dict):
    buttons = _publish_buttons(config)
    rows = [[InlineKeyboardButton("➕ 添加按钮", callback_data="publish:button_add")]]
    if buttons:
        rows.extend(
            [
                [InlineKeyboardButton("📝 修改按钮", callback_data="publish:button_edit")],
                [InlineKeyboardButton("🗑 删除按钮", callback_data="publish:button_delete")],
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


def _button_selection_keyboard(config: dict, action: str):
    rows = []
    for button in _publish_buttons(config):
        button_id = button.get("id")
        button_text = str(button.get("text", "未命名"))[:40]
        prefix = "📝" if action == "edit" else "🗑"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{prefix} #{button_id} {button_text}",
                    callback_data=f"publish:button_{action}_{button_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:buttons")])
    return InlineKeyboardMarkup(rows)


def _normalize_button_url(value: str):
    url = (value or "").strip()
    if url.startswith("t.me/"):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "tg"} or not parsed.netloc and parsed.scheme != "tg":
        return None
    return url


def _clear_button_input(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("publish_button_input", None)


CUSTOM_USER_MENU_OPTIONS = (
    ("submission", "📝 我要投稿"),
    ("random_view", "🎲 随机查看"),
    ("channel_clone", "📣 克隆频道"),
    ("telethon_manage", "📱 管理协议号"),
    ("bot_channel_config", "📣 机器人配置"),
    ("group_config", "👥 群配置"),
    ("global_ad_config", "📢 全群广告配置"),
)


def _custom_button_visible(config: dict, key: str) -> bool:
    if key == "submission":
        return bool(config.get("submission_enabled", True))
    if key == "random_view":
        return bool(config.get("random_view_enabled", True))
    values = config.get("custom_menu_buttons", {})
    return bool(values.get(key, True)) if isinstance(values, dict) else True


def _set_custom_button_visible(config: dict, key: str, visible: bool) -> bool:
    if key == "submission":
        config["submission_enabled"] = visible
        return True
    if key == "random_view":
        config["random_view_enabled"] = visible
        return True
    if key not in {item[0] for item in CUSTOM_USER_MENU_OPTIONS}:
        return False
    values = config.get("custom_menu_buttons")
    if not isinstance(values, dict):
        values = {}
        config["custom_menu_buttons"] = values
    values[key] = visible
    return True


def _custom_user_buttons_text(config: dict) -> str:
    lines = [
        "🧩 用户按钮显示设置",
        "",
        "以下规则只限制普通用户；机器人所有者和高级管理员始终显示全部按钮：",
    ]
    for key, label in CUSTOM_USER_MENU_OPTIONS:
        lines.append(f"{'✅' if _custom_button_visible(config, key) else '🚫'} {label}")
    return "\n".join(lines)


def _custom_user_buttons_keyboard(config: dict):
    rows = []
    for key, label in CUSTOM_USER_MENU_OPTIONS:
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if _custom_button_visible(config, key) else '🚫'} {label}",
                callback_data=f"publish:custom_toggle:{key}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


# =========================
# 键盘
# =========================
def publish_setting_keyboard(config: dict):
    bottom_buttons_enabled = bool(config.get("bottom_buttons_enabled", True))
    proof_required = bool(config.get("proof_required", False))
    reject_reason_required = bool(config.get("reject_reason_required", False))
    comment_forward_enabled = bool(config.get("comment_forward_enabled", False))
    continuous_submission_enabled = bool(config.get("continuous_submission_enabled", True))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 发布频道", callback_data="publish:channel")],
        [InlineKeyboardButton("📝 审核设置", callback_data="publish:review")],
        # [InlineKeyboardButton("📊 每日发布上限", callback_data="publish:limit")],
        # [InlineKeyboardButton("📣 广告管理", callback_data="publish:ads")],
        [InlineKeyboardButton("🔘 底部按钮设置", callback_data="publish:buttons")],
        [InlineKeyboardButton("🔑 关键词设置", callback_data="publish:keywords")],
        [
            InlineKeyboardButton(
                f"{'✅' if bottom_buttons_enabled else '🚫'} 显示底部按钮",
                callback_data="publish:toggle_bottom_buttons",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if proof_required else '🚫'} 审核凭证",
                callback_data="publish:toggle_proof_required",
            ),
            InlineKeyboardButton(
                f"{'✅' if reject_reason_required else '🚫'} 拒绝原因",
                callback_data="publish:toggle_reject_reason_required",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if comment_forward_enabled else '🚫'} 评论并转发",
                callback_data="publish:toggle_comment_forward",
            ),
            InlineKeyboardButton(
                "📨 转发频道",
                callback_data="publish:forward_channel",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if continuous_submission_enabled else '🚫'} 连续投稿",
                callback_data="publish:toggle_continuous_submission",
            )
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="start:back")]
    ])


def publish_channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 修改频道", callback_data="publish:set_channel")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_review_keyboard(enabled):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ 已开启' if enabled else '❌ 已关闭'}",
                callback_data="publish:toggle_review"
            )
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_limit_keyboard(limit):
    text = "♾ 不限制" if limit <= 0 else str(limit)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"当前：{text}", callback_data="noop")],
        [InlineKeyboardButton("✏️ 修改上限", callback_data="publish:set_limit")],
        [InlineKeyboardButton("🚫 不限制", callback_data="publish:unlimit")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_ads_keyboard(enabled):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ 已启用' if enabled else '❌ 已禁用'}",
                callback_data="publish:toggle_ads"
            )
        ],
        [InlineKeyboardButton("➕ 添加广告", callback_data="publish:add_ad")],
        [InlineKeyboardButton("📝 编辑广告", callback_data="publish:edit_ad")],
        [InlineKeyboardButton("🗑 删除广告", callback_data="publish:delete_ad")],
        [InlineKeyboardButton("📋 广告列表", callback_data="publish:list_ad")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


# =========================
# 回调处理
# =========================

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith("publish:"):
        return

    config = load_publish_config()
    channel_id = config.get("channel_id")
    action = query.data.split(":")[1]

    if action in {"review_approve", "review_reject", "review_reject_cancel"}:
        return await _handle_review_callback(query, context, config)

    if action == "comment":
        return await _start_comment_submission(query, context, config)

    if action == "keyword_pick":
        results = (context.user_data or {}).get(KEYWORD_RESULTS_KEY, [])
        try:
            result_index = int(query.data.split(":", 2)[2])
            route = results[result_index]
        except (ValueError, IndexError, TypeError):
            return await query.answer("关键词结果已失效，请重新搜索。", show_alert=True)
        context.user_data.pop(KEYWORD_RESULTS_KEY, None)
        context.user_data.pop(KEYWORD_INPUT_KEY, None)
        context.user_data[COMMENT_TARGET_KEY] = {
            "channel_id": route["channel_id"],
            "message_id": route["channel_message_id"],
        }
        context.user_data["waiting_post"] = True
        link = await _route_message_link(context, route)
        await query.answer()
        return await query.edit_message_text(
            f"✅ 已选择 {route.get('label', '关键词')}：{route.get('raw', route.get('key'))}。\n"
            "请发送投稿内容，审核通过后会评论到对应帖子下，并发布到转发频道。",
            reply_markup=_keyword_route_keyboard(
                route,
                link,
                context.user_data.get("post_no_name", True),
            ),
        )

    # All remaining actions below are publishing configuration actions.  Do not
    # rely on the hidden start-menu button: callbacks can be forged manually.
    public_actions = {"publish", "channel_message", "bottle_prev", "bottle_next", "accept_friend", "reject_friend", "back"}
    if action not in public_actions and not has_admin_permission(
        context, query.from_user.id, "submission_config"
    ):
        return await query.answer("你没有投稿配置权限。", show_alert=True)

    await query.answer()
    
    if action == "publishset":
        help_text = "📣 请设置发布的频道"       
        await query.edit_message_text(
            help_text,
            reply_markup=publish_setting_keyboard(config)
        )

    if action == "toggle_submission":
        config["submission_enabled"] = not bool(config.get("submission_enabled", True))
        save_publish_config(config)
        return await query.edit_message_text(
            f"投稿开关已{'开启' if config['submission_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_continuous_submission":
        config["continuous_submission_enabled"] = not bool(
            config.get("continuous_submission_enabled", True)
        )
        save_publish_config(config)
        mode_text = "一次投稿完成后可继续发送" if config["continuous_submission_enabled"] else "一次投稿完成后自动结束"
        return await query.edit_message_text(
            f"连续投稿已{'开启' if config['continuous_submission_enabled'] else '关闭'}。\n{mode_text}",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "custom_buttons":
        if not has_admin_permission(context, query.from_user.id, "submission_config"):
            return await query.answer("你没有投稿配置权限。", show_alert=True)
        
        return await query.edit_message_text(
            _custom_user_buttons_text(config),
            reply_markup=_custom_user_buttons_keyboard(config),
        )

    if action == "custom_toggle":
        if not has_admin_permission(context, query.from_user.id, "submission_config"):
            return await query.answer("你没有投稿配置权限。", show_alert=True)
            
        key = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if not key:
            return await query.answer("按钮配置无效。", show_alert=True)
        current = _custom_button_visible(config, key)
        if not _set_custom_button_visible(config, key, not current):
            return await query.answer("按钮配置无效。", show_alert=True)
        save_publish_config(config)
        return await query.edit_message_text(
            _custom_user_buttons_text(config),
            reply_markup=_custom_user_buttons_keyboard(config),
        )

    if action == "toggle_random_view":
        config["random_view_enabled"] = not bool(config.get("random_view_enabled", True))
        save_publish_config(config)
        return await query.edit_message_text(
            f"随机查看开关已{'开启' if config['random_view_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_bottom_buttons":
        config["bottom_buttons_enabled"] = not bool(
            config.get("bottom_buttons_enabled", True)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"发布底部按钮已{'显示' if config['bottom_buttons_enabled'] else '隐藏'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_proof_required":
        config["proof_required"] = not bool(config.get("proof_required", False))
        save_publish_config(config)
        return await query.edit_message_text(
            f"审核凭证已{'开启' if config['proof_required'] else '关闭'}。"
            "开启后，普通投稿人需上传凭证才会提交审核。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_reject_reason_required":
        config["reject_reason_required"] = not bool(
            config.get("reject_reason_required", False)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"拒绝原因已{'开启' if config['reject_reason_required'] else '关闭'}。"
            "开启后，审核人拒绝投稿时需要填写原因。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_comment_forward":
        config["comment_forward_enabled"] = not bool(
            config.get("comment_forward_enabled", False)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"评论并转发已{'开启' if config['comment_forward_enabled'] else '关闭'}。"
            "开启后，管理员发布到主频道的帖子会显示评论按钮。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "keywords":
        return await query.edit_message_text(
            _keyword_settings_text(config),
            reply_markup=_keyword_settings_keyboard(),
        )

    if action == "keywords_set":
        context.user_data[KEYWORD_LABEL_INPUT_KEY] = True
        return await query.edit_message_text(
            "请输入需要提取的字段标签，多个用逗号或换行分隔。\n"
            "例如：艺名, 联系方式",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回", callback_data="publish:keywords")]
            ]),
        )

    if action == "keywords_clear":
        config["keyword_extract_labels"] = []
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 已清空关键词提取标签。\n\n" + _keyword_settings_text(config),
            reply_markup=_keyword_settings_keyboard(),
        )

    if action == "forward_channel":
        context.user_data["waiting_forward_channel_id"] = True
        target = config.get("forward_channel_id")
        return await query.edit_message_text(
            f"📨 当前转发频道：{target if target else '未设置'}\n\n"
            "请输入转发频道 ID，例如：-1001234567890",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
            ]),
        )

    if action == "channel":
        
        text = (
            f"📢 当前频道：\n{channel_id}"
            if channel_id
            else "📢 当前未设置发布频道。请将频道任意一条消息转发给机器人，获取频道 ID 后输入即可设置。"
        )

        return await query.edit_message_text(
            text,
            reply_markup=publish_channel_keyboard()
        )

    if action == "set_channel":
        context.user_data["waiting_channel_id"] = True

        return await query.edit_message_text(
            "请输入频道ID\n\n例如：\n-1001234567890"
        )

    if action == "review":
        return await query.edit_message_text(
            f"📝 审核状态：{'开启' if config['review_enabled'] else '关闭'}",
            reply_markup=publish_review_keyboard(
                config["review_enabled"]
            )
        )

    if action == "toggle_review":
        config["review_enabled"] = not config["review_enabled"]
        save_publish_config(config)

        return await query.edit_message_text(
            f"📝 审核状态：{'开启' if config['review_enabled'] else '关闭'}",
            reply_markup=publish_review_keyboard(
                config["review_enabled"]
            )
        )

    if action == "limit":
        limit = config["daily_limit"]

        return await query.edit_message_text(
            f"📊 当前每日发布上限：{'♾ 不限制' if limit <= 0 else limit}",
            reply_markup=publish_limit_keyboard(limit)
        )

    if action == "set_limit":
        context.user_data["waiting_limit"] = True

        return await query.edit_message_text(
            "请输入每日发布上限数字"
        )

    if action == "unlimit":
        config["daily_limit"] = 0
        save_publish_config(config)

        return await query.edit_message_text(
            "✅ 已设置为不限制",
            reply_markup=publish_limit_keyboard(0)
        )

    if action == "ads":
        return await query.edit_message_text(
            f"📣 广告状态：{'开启' if config['ads_enabled'] else '关闭'}",
            reply_markup=publish_ads_keyboard(
                config["ads_enabled"]
            )
        )

    if action == "toggle_ads":
        config["ads_enabled"] = not config["ads_enabled"]
        save_publish_config(config)

        return await query.edit_message_text(
            f"📣 广告状态：{'开启' if config['ads_enabled'] else '关闭'}",
            reply_markup=publish_ads_keyboard(
                config["ads_enabled"]
            )
        )

    if action == "add_ad":

        context.user_data["waiting_add_ad"] = True

        return await query.edit_message_text(
            "请输入广告内容：\n\n例如：\n欢迎加入交流群 https://t.me/xxx"
        )
    if action == "list_ad":

        ads = config.get("ads", [])

        if not ads:
            return await query.edit_message_text(
                "暂无广告"
            )

        lines = ["📋 广告列表", ""]
        rows = []
        
        for ad in ads:
            lines.append(
                f"#{ad['id']} {'✅' if ad['enabled'] else '❌'}"
            )

            rows.append([
                InlineKeyboardButton(
                    f"{'✅' if ad['enabled'] else '❌'} #{ad['id']}",
                    callback_data=f"publish:toggle_ad_{ad['id']}"
                )
            ])

        return await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows)
        )

    if action == "delete_ad":

        ads = config.get("ads", [])

        if not ads:
            return await query.edit_message_text("暂无广告")

        rows = []

        for ad in ads:
            rows.append([
                InlineKeyboardButton(
                    f"🗑 删除 #{ad['id']}",
                    callback_data=f"publish:delete_ad_{ad['id']}"
                )
            ])

        rows.append([
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="publish:ads"
            )
        ])

        return await query.edit_message_text(
            "请选择要删除的广告",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    if action.startswith("delete_ad_"):

        ad_id = int(action.replace("delete_ad_", ""))

        ads = config.get("ads", [])

        ads = [x for x in ads if x["id"] != ad_id]

        config["ads"] = ads

        save_publish_config(config)

        return await query.edit_message_text(
            "✅ 删除成功"
        )
    if action == "edit_ad":

        ads = config.get("ads", [])

        rows = []

        for ad in ads:
            rows.append([
                InlineKeyboardButton(
                    f"📝 #{ad['id']}",
                    callback_data=f"publish:edit_ad_{ad['id']}"
                )
            ])

        return await query.edit_message_text(
            "选择广告",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    if action.startswith("edit_ad_"):

        ad_id = int(action.replace("edit_ad_", ""))

        context.user_data["edit_ad_id"] = ad_id
        context.user_data["waiting_edit_ad"] = True

        return await query.edit_message_text(
            f"请输入新的广告内容\n广告ID:{ad_id}"
        )
    
    if action.startswith("toggle_ad_"):

        ad_id = int(action.replace("toggle_ad_", ""))

        for ad in config["ads"]:
            if ad["id"] == ad_id:
                ad["enabled"] = not ad["enabled"]
                break
        
        save_publish_config(config)
        
        await query.edit_message_text(
        "📋 广告列表（点击切换启用状态）",
        reply_markup=build_ads_list_keyboard(config["ads"])
    ) 
        return
        
    if action == "buttons":
        _clear_button_input(context)
        return await query.edit_message_text(
            _button_settings_text(config),
            reply_markup=publish_button_settings_keyboard(config),
        )

    if action == "button_add":
        buttons = _publish_buttons(config)
        if len(buttons) >= MAX_PUBLISH_BUTTONS:
            return await query.edit_message_text(
                f"最多只能设置 {MAX_PUBLISH_BUTTONS} 个投稿按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        context.user_data["waiting_post"] = False
        context.user_data["publish_button_input"] = {"mode": "add", "step": "text"}
        return await query.edit_message_text(
            "请输入按钮文字（最多 64 个字符）。\n例如：加入交流群"
        )

    if action == "button_edit":
        buttons = _publish_buttons(config)
        if not buttons:
            return await query.edit_message_text(
                "当前未设置按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        return await query.edit_message_text(
            "请选择要修改的按钮：",
            reply_markup=_button_selection_keyboard(config, "edit"),
        )

    if action.startswith("button_edit_"):
        try:
            button_id = int(action.removeprefix("button_edit_"))
        except ValueError:
            return await query.answer("按钮数据无效", show_alert=True)
        if not any(button.get("id") == button_id for button in _publish_buttons(config)):
            return await query.answer("按钮不存在或已删除", show_alert=True)
        context.user_data["waiting_post"] = False
        context.user_data["publish_button_input"] = {
            "mode": "edit",
            "step": "text",
            "button_id": button_id,
        }
        return await query.edit_message_text(
            "请输入新的按钮文字（最多 64 个字符）。"
        )

    if action == "button_delete":
        buttons = _publish_buttons(config)
        if not buttons:
            return await query.edit_message_text(
                "当前未设置按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        return await query.edit_message_text(
            "请选择要删除的按钮：",
            reply_markup=_button_selection_keyboard(config, "delete"),
        )

    if action.startswith("button_delete_"):
        try:
            button_id = int(action.removeprefix("button_delete_"))
        except ValueError:
            return await query.answer("按钮数据无效", show_alert=True)
        buttons = _publish_buttons(config)
        updated_buttons = [button for button in buttons if button.get("id") != button_id]
        if len(updated_buttons) == len(buttons):
            return await query.answer("按钮不存在或已删除", show_alert=True)
        config["buttons"] = updated_buttons
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 按钮已删除。\n\n" + _button_settings_text(config),
            reply_markup=publish_button_settings_keyboard(config),
        )

    if action == "back":
        return await query.edit_message_text(
            "⚙️ 发布设置",
            reply_markup=publish_setting_keyboard(config)
        )
    
    if action == "publish":
        owner_id = _owner_id(context)
        if (
            bool(config.get("comment_forward_enabled", False))
            and query.from_user.id != owner_id
            and not isinstance(context.user_data.get(COMMENT_TARGET_KEY), dict)
        ):
            if not _keyword_labels(config):
                return await query.edit_message_text(
                    "❗ 当前未配置关键词提取标签，请联系管理员在投稿设置中配置。"
                )
            context.user_data[KEYWORD_INPUT_KEY] = True
            context.user_data["waiting_post"] = False
            return await query.edit_message_text(
                "🔎 请输入要查询的关键词（例如：@×××或名字）。\n"
                "找到对应帖子后，再发送投稿内容。"
            )
        await publish_message(update, context)
        
    if action == "channel_message":
        
        context.user_data["reply_bottle"] = True
        
        user_id = query.from_user.id

        posts = _load_cannel_message()

        history_data = _load_bottle_history()

        user_key = str(user_id)

        if user_key not in history_data:
            history_data[user_key] = {
                "history": [],
                "index": -1
            }

        user_info = history_data[user_key]

        history = user_info["history"]

        available_posts = [
            p for p in posts
            if p["user_id"] != user_id
            and p["channel_message_id"] not in history
        ]

        if not available_posts:
            await query.message.reply_text("没有更多资源了")
            return

        post = random.choice(available_posts)

        history.append(post["channel_message_id"])

        user_info["index"] = len(history) - 1

        _save_bottle_history(history_data)

        await send_bottle(
            context,
            user_id,
            post.get("channel_id") or channel_id,
            post,
        )

    if action == "global_ad_toggle":
       
        enabled = not context.user_data["post_no_name"]
        
        await query.answer("✅ 已更新", show_alert=False)
        
        
        help_text = "📣 请发送您要的内容。\n\n支持文字、图片、视频等消息。 点击返回停止发送"
    
        context.user_data["post_no_name"] = enabled
        
        await query.edit_message_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )

    if action == "bottle_next":
        user_id = query.from_user.id

        posts = _load_cannel_message()

        history_data = _load_bottle_history()

        user_info = history_data.get(str(user_id))

        if not user_info:
            await query.message.reply_text("请先请求一个资源")
            return

        history = user_info["history"]
        index = user_info["index"]

        # 已浏览历史里还有下一条
        if index < len(history) - 1:
            index += 1
            user_info["index"] = index
            _save_bottle_history(history_data)
            message_id = history[index]
            post = next(
                (item for item in posts if item.get("channel_message_id") == message_id),
                None,
            )
        else:
            available_posts = [
                p for p in posts
                if p["user_id"] != user_id
                and p["channel_message_id"] not in history
            ]
            if not available_posts:
                await query.message.reply_text("没有更多资源了")
                return

            post = random.choice(available_posts)
            history.append(post["channel_message_id"])
            user_info["index"] = len(history) - 1
            _save_bottle_history(history_data)
            message_id = post["channel_message_id"]

        await query.message.delete()
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=(post or {}).get("channel_id") or channel_id,
            message_id=message_id,
            reply_markup=query.message.reply_markup,
        )

    if action == "bottle_prev":

        user_id = query.from_user.id

        history_data = _load_bottle_history()

        user_info = history_data.get(str(user_id))

        if not user_info:
            return

        index = user_info["index"]

        if index <= 0:
            await query.message.reply_text(
                "已经是第一条了"
            )
            return

        index -= 1

        user_info["index"] = index

        _save_bottle_history(history_data)

        message_id = user_info["history"][index]
        post = next(
            (item for item in _load_cannel_message() if item.get("channel_message_id") == message_id),
            None,
        )

        await query.message.delete()
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=(post or {}).get("channel_id") or channel_id,
            message_id=message_id,
            reply_markup=query.message.reply_markup,
        )
    
    if action == "add_friend":

        target_user_id = int(query.data.split(":")[2])
        target_user_id = int(target_user_id)
        from_user_id = query.from_user.id
        
        history_data = _load_bottle_history()
        user_key = str(from_user_id)
        friend_applied = history_data[user_key].setdefault(
            "friend_applied",
            {}
        )
        
        if str(target_user_id) in friend_applied:
            await query.message.reply_text(
                "你已经发送过好友申请了"
            )
            return
        
        accepter = get_user(from_user_id)
        
        await context.bot.send_message(
            chat_id=target_user_id,
            text= (
                f"@{accepter['username']}  想认识你" 
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ 同意",
                        callback_data=f"publish:accept_friend:{from_user_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ 拒绝",
                        callback_data=f"publish:reject_friend:{from_user_id}"
                    )
                ]
            ])
        )
        
        await query.message.reply_text(
            "好友申请发送成功"
        )
        
        friend_applied[str(target_user_id)] = True
        _save_bottle_history(history_data)
    if action == "accept_friend":
        requester_id = int(query.data.split(":")[2])

        accepter_id = query.from_user.id

        print("申请人:", requester_id)
        print("同意人:", accepter_id)

        # 从数据库读取双方信息
        requester = get_user(requester_id)
        accepter = get_user(accepter_id)

        # 通知申请人
        await context.bot.send_message(
            chat_id=requester_id,
            text=(
                "🎉 对方已同意交换联系方式\n\n"
                f"用户名：@{accepter['username']}"
            )
        )

        await query.message.delete()
         
        # 通知同意人
        await query.message.reply_text(
            f"已向对方发送你的联系方式，对方联系方式：@{requester['username']}"
        )
        
       
    
    if action == "reject_friend":
        requester_id = int(query.data.split(":")[2])

        await context.bot.send_message(
            chat_id=requester_id,
            text="❌ 对方拒绝了你的好友申请"
        )

        await query.message.delete()
        
        await query.message.reply_text("已拒绝")
        
async def publish_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["waiting_post"] = True
        
    enabled = context.user_data.get("post_no_name", True)
    
    help_text = "📣 请发送您要投稿的内容。\n\n支持文字、图片、视频等消息。"
    
    context.user_data["post_no_name"] = enabled
    
    if query:
        
        await query.edit_message_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )
        
    else:
        await update.message.reply_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )
    
def create_post_keyboard(enabled: bool):
    rows = [
        [
            # InlineKeyboardButton(
            #     f"{'✅' if enabled else '🚫'} 匿名模式",
            #     callback_data=f"{CALLBACK_PREFIX}:global_ad_toggle",
            # ),
            InlineKeyboardButton(
                "✅ 继续发",
                callback_data="publish:publish",
            ),
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="start:back",
            ),
        ]
    ]
    return InlineKeyboardMarkup(rows)


def _finish_submission_if_needed(context: ContextTypes.DEFAULT_TYPE, config: dict) -> None:
    """End the current submission session when continuous submission is off."""
    if bool(config.get("continuous_submission_enabled", True)):
        return
    context.user_data["waiting_post"] = False
    context.user_data.pop(PENDING_PROOF_KEY, None)
    context.user_data.pop(COMMENT_TARGET_KEY, None)


async def handle_wall_publish(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_post"):
        return

    msg = update.message
    if not msg or not msg.from_user:
        return

    config = load_publish_config()
    main_channel_id = _as_int(config.get("channel_id"))
    if main_channel_id is None:
        await msg.reply_text("✅ 暂未配置发布频道，感谢参与！")
        return

    # The next user message is the proof for an already saved submission.
    proof_submission_id = context.user_data.get(PENDING_PROOF_KEY)
    if proof_submission_id:
        pending = _load_pending_submissions()
        submission = pending.get(proof_submission_id)
        if not isinstance(submission, dict) or submission.get("status") != "awaiting_proof":
            context.user_data.pop(PENDING_PROOF_KEY, None)
            return await msg.reply_text("❗ 未找到待上传凭证的投稿，请重新发起投稿。")

        submission["proof_chat_id"] = msg.chat_id
        submission["proof_message_id"] = msg.message_id
        submission["proof_uploaded_at"] = int(time.time())
        submission["status"] = "pending"
        pending[proof_submission_id] = submission
        _save_pending_submissions(pending)
        try:
            await _send_submission_for_review(context, proof_submission_id, submission)
        except Exception as exc:
            submission.pop("proof_chat_id", None)
            submission.pop("proof_message_id", None)
            submission.pop("proof_uploaded_at", None)
            submission["status"] = "awaiting_proof"
            pending[proof_submission_id] = submission
            _save_pending_submissions(pending)
            print("转发投稿及凭证审核失败:", exc)
            await msg.reply_text(f"❌ 提交审核失败：{exc}")
            return

        context.user_data.pop(PENDING_PROOF_KEY, None)
        _finish_submission_if_needed(context, config)
        await msg.reply_text("🕒 凭证已收到，投稿已提交，等待管理员审核。")
        return

    owner_id = _owner_id(context)
    is_owner_submission = bool(
        msg.from_user.id == owner_id
        or has_admin_permission(context, msg.from_user.id, "submission_config")
    )
    comment_target = context.user_data.pop(COMMENT_TARGET_KEY, None)
    comment_target = comment_target if isinstance(comment_target, dict) else None
    comment_forward_enabled = bool(config.get("comment_forward_enabled", False))

    submission_kind = "main"
    target_channel_id = main_channel_id
    if comment_forward_enabled and comment_target:
        submission_kind = "comment"
    elif comment_forward_enabled and not is_owner_submission:
        submission_kind = "forward"
        target_channel_id = _as_int(config.get("forward_channel_id"))
        if target_channel_id is None:
            await msg.reply_text("❌ 未设置转发频道，暂时无法提交投稿。")
            return

    # Non-owner submissions use review when it is enabled.
    if bool(config.get("review_enabled", False)) and not is_owner_submission:
        if owner_id is None:
            await msg.reply_text("❌ 未配置机器人所有者，暂时无法提交审核。")
            return

        submission_id = uuid.uuid4().hex[:16]
        submission = {
            "status": "pending",
            "user_id": msg.from_user.id,
            "user_chat_id": msg.chat_id,
            "username": msg.from_user.username,
            "author": _submission_author_text(msg),
            "user_message_id": msg.message_id,
            "submitted_at": int(time.time()),
            "submission_kind": submission_kind,
            "target_channel_id": target_channel_id,
        }
        if comment_target:
            submission["comment_target"] = comment_target

        pending = _load_pending_submissions()
        if bool(config.get("proof_required", False)):
            submission["status"] = "awaiting_proof"
            pending[submission_id] = submission
            _save_pending_submissions(pending)
            context.user_data[PENDING_PROOF_KEY] = submission_id
            await msg.reply_text(
                "📎 请继续上传审核凭证。\n"
                "支持文字、图片、视频、文件等；凭证仅供审核，不会发布到频道。"
            )
            return

        pending[submission_id] = submission
        _save_pending_submissions(pending)
        try:
            await _send_submission_for_review(context, submission_id, submission)
        except Exception as exc:
            pending.pop(submission_id, None)
            _save_pending_submissions(pending)
            print("转发投稿审核失败:", exc)
            await msg.reply_text(f"❌ 提交审核失败：{exc}")
            return

        _finish_submission_if_needed(context, config)
        await msg.reply_text("🕒 投稿已提交，等待管理员审核。")
        return

    # Owner's normal post stays in main channel and receives a comment button.
    submission = {
        "user_chat_id": msg.chat_id,
        "user_message_id": msg.message_id,
        "keyword_entries": (
            _extract_routing_keywords(msg, config)
            if comment_forward_enabled and is_owner_submission and submission_kind == "main"
            else []
        ),
    }
    try:
        if submission_kind == "comment":
            published = await _publish_comment_and_forward(context, {
                **submission,
                "comment_target": comment_target,
            }, config)
            target_channel_id = _as_int(config.get("forward_channel_id"))
        else:
            published = await _copy_submission_to_channel(
                context,
                submission,
                target_channel_id,
                config,
                add_comment_button=(
                    comment_forward_enabled
                    and is_owner_submission
                    and submission_kind == "main"
                ),
            )
        _append_published_submission(msg, published.message_id, target_channel_id)
        _finish_submission_if_needed(context, config)
        await msg.reply_text("✅ 发送成功")
    except Exception as exc:
        print("投稿失败:", exc)
        await msg.reply_text(f"❌ 发送失败：{exc}")

# =========================
# 文本输入处理
# =========================

async def _handle_keyword_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not (context.user_data or {}).get(KEYWORD_INPUT_KEY):
        return False
    msg = update.message
    if not msg or not msg.text:
        if msg:
            await msg.reply_text("❗ 请发送文字关键词。")
        return True

    query = msg.text.strip()
    if query in {"取消", "返回"}:
        context.user_data.pop(KEYWORD_INPUT_KEY, None)
        await msg.reply_text("✅ 已取消关键词搜索。")
        return True
    routes = _find_keyword_routes(query)
    if not routes:
        await msg.reply_text(
            "❗ 未找到可评论的对应帖子。请检查关键词，或等待管理员发布带关键词的新帖子后重试。"
        )
        return True

    if len(routes) == 1:
        route = routes[0]
        context.user_data.pop(KEYWORD_INPUT_KEY, None)
        context.user_data[COMMENT_TARGET_KEY] = {
            "channel_id": route["channel_id"],
            "message_id": route["channel_message_id"],
        }
        context.user_data["waiting_post"] = True
        link = await _route_message_link(context, route)
        return await msg.reply_text(
            f"✅ 已匹配 {route.get('label', '关键词')}：{route.get('raw', route.get('key'))}。\n"
            "请继续发送投稿内容，审核通过后会评论到对应帖子下，并发布到转发频道。",
            reply_markup=_keyword_route_keyboard(
                route,
                link,
                context.user_data.get("post_no_name", True),
            ),
        )

    context.user_data[KEYWORD_RESULTS_KEY] = routes
    rows = []
    for index, route in enumerate(routes):
        label = str(route.get("label", "关键词"))[:12]
        value = str(route.get("raw", route.get("key", "")))[:30]
        rows.append([
            InlineKeyboardButton(
                f"选择 {label}：{value}",
                callback_data=f"publish:keyword_pick:{index}",
            )
        ])
        link = await _route_message_link(context, route)
        if link:
            rows.append([InlineKeyboardButton(f"🔗 查看 {value}", url=link)])
    return await msg.reply_text("找到多个对应帖子，请选择：", reply_markup=InlineKeyboardMarkup(rows))


async def _handle_reject_reason_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    stage = (context.user_data or {}).get(REJECT_REASON_KEY)
    if not isinstance(stage, dict):
        return False

    msg = update.message
    if not msg:
        return True
    if not update.effective_user or not has_admin_permission(
        context, update.effective_user.id, "submission_review"
    ):
        context.user_data.pop(REJECT_REASON_KEY, None)
        return False
    if not msg.text:
        await msg.reply_text("❗ 请发送文字形式的拒绝原因，或发送“取消”。")
        return True

    submission_id = stage.get("submission_id")
    text = msg.text.strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(REJECT_REASON_KEY, None)
        pending = _load_pending_submissions()
        submission = pending.get(submission_id)
        try:
            if isinstance(submission, dict) and stage.get("review_chat_id"):
                await context.bot.edit_message_text(
                    chat_id=stage["review_chat_id"],
                    message_id=stage["review_message_id"],
                    text=_review_prompt_text({**submission, "id": submission_id}),
                    reply_markup=_review_keyboard(submission_id),
                )
        except Exception as exc:
            print("恢复审核消息失败:", exc)
        await msg.reply_text("✅ 已取消拒绝，投稿仍可继续审核。")
        return True

    if not text:
        await msg.reply_text("❗ 拒绝原因不能为空。")
        return True
    if len(text) > 3000:
        await msg.reply_text("❗ 拒绝原因不能超过 3000 个字符。")
        return True

    pending = _load_pending_submissions()
    submission = pending.get(submission_id)
    if not isinstance(submission, dict) or submission.get("status") != "pending":
        context.user_data.pop(REJECT_REASON_KEY, None)
        await msg.reply_text("❗ 该投稿已处理或不存在。")
        return True

    await _finalize_rejection(context, submission_id, submission, text)
    context.user_data.pop(REJECT_REASON_KEY, None)
    try:
        if stage.get("review_chat_id"):
            await context.bot.edit_message_text(
                chat_id=stage["review_chat_id"],
                message_id=stage["review_message_id"],
                text="❌ 投稿已拒绝，已通知投稿人。",
            )
    except Exception as exc:
        print("更新审核拒绝消息失败:", exc)
    await msg.reply_text("❌ 已拒绝投稿，已通知投稿人拒绝原因。")
    return True

async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_keyword_search_input(update, context):
        return
    if await _handle_reject_reason_input(update, context):
        return

    await handle_wall_publish(update, context)

    if not update.message.text:
        return
    
    config = load_publish_config()

    if context.user_data.get(KEYWORD_LABEL_INPUT_KEY):
        labels = [
            value.strip()[:30]
            for value in re.split(r"[,，\n]+", update.message.text or "")
            if value.strip()
        ]
        if not labels:
            return await update.message.reply_text("❗ 请至少输入一个标签，例如：艺名")
        context.user_data.pop(KEYWORD_LABEL_INPUT_KEY, None)
        config["keyword_extract_labels"] = list(dict.fromkeys(labels))[:20]
        save_publish_config(config)
        return await update.message.reply_text(
            "✅ 关键词提取标签已保存。\n\n" + _keyword_settings_text(config),
            reply_markup=_keyword_settings_keyboard(),
        )

    button_input = context.user_data.get("publish_button_input")
    if isinstance(button_input, dict):
        value = update.message.text.strip()
        if button_input.get("step") == "text":
            if not value:
                return await update.message.reply_text("❗ 按钮文字不能为空，请重新输入。")
            if len(value) > BUTTON_TEXT_MAX_LENGTH:
                return await update.message.reply_text(
                    f"❗ 按钮文字不能超过 {BUTTON_TEXT_MAX_LENGTH} 个字符，请重新输入。"
                )
            button_input["text"] = value
            button_input["step"] = "url"
            return await update.message.reply_text(
                "请输入按钮链接。\n支持 https://、http://、tg:// 或 t.me/ 开头的链接。"
            )

        if button_input.get("step") == "url":
            url = _normalize_button_url(value)
            if not url:
                return await update.message.reply_text(
                    "❗ 链接格式不正确，请输入完整链接，例如：https://t.me/example"
                )

            buttons = _publish_buttons(config)
            if button_input.get("mode") == "edit":
                button_id = button_input.get("button_id")
                for button in buttons:
                    if button.get("id") == button_id:
                        button["text"] = button_input["text"]
                        button["url"] = url
                        break
                else:
                    _clear_button_input(context)
                    return await update.message.reply_text("❗ 按钮不存在或已删除。")
                result_text = "✅ 按钮修改成功。"
            else:
                if len(buttons) >= MAX_PUBLISH_BUTTONS:
                    _clear_button_input(context)
                    return await update.message.reply_text(
                        f"❗ 最多只能设置 {MAX_PUBLISH_BUTTONS} 个投稿按钮。"
                    )
                button_id = max(
                    (int(button.get("id", 0) or 0) for button in buttons), default=0
                ) + 1
                buttons.append({"id": button_id, "text": button_input["text"], "url": url})
                config["buttons"] = buttons
                result_text = "✅ 按钮添加成功。"

            save_publish_config(config)
            _clear_button_input(context)
            return await update.message.reply_text(
                result_text + "\n\n" + _button_settings_text(config),
                reply_markup=publish_button_settings_keyboard(config),
            )

    if context.user_data.get("waiting_forward_channel_id"):
        context.user_data["waiting_forward_channel_id"] = False
        try:
            forward_channel_id = int(update.message.text.strip())
        except (TypeError, ValueError):
            return await update.message.reply_text(
                "❗ 频道 ID 格式错误，例如：-1001234567890"
            )
        config["forward_channel_id"] = forward_channel_id
        save_publish_config(config)
        return await update.message.reply_text(
            f"✅ 已保存转发频道：{forward_channel_id}",
            reply_markup=publish_setting_keyboard(config),
        )

    if context.user_data.get("waiting_channel_id"):
        context.user_data["waiting_channel_id"] = False

        channel_id = update.message.text.strip()
        config["channel_id"] = int(channel_id)

        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 已保存频道：{channel_id}"
        )

    if context.user_data.get("waiting_limit"):
        context.user_data["waiting_limit"] = False

        limit = int(update.message.text.strip())

 
        config["daily_limit"] = limit

        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 已设置每日上限：{limit}"
        )
    if context.user_data.get("waiting_add_ad"):

        context.user_data["waiting_add_ad"] = False


        ads = config.get("ads", [])

        ad_id = max([x["id"] for x in ads], default=0) + 1

        ads.append({
            "id": ad_id,
            "title": f"广告{ad_id}",
            "content": update.message.text,
            "enabled": True
        })

        config["ads"] = ads
        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 广告添加成功\nID: {ad_id}"
        )
    if context.user_data.get("waiting_edit_ad"):

        context.user_data["waiting_edit_ad"] = False

        ad_id = context.user_data.pop("edit_ad_id")



        for ad in config["ads"]:
            if ad["id"] == ad_id:
                ad["content"] = update.message.text
                break

        save_publish_config(config)

        return await update.message.reply_text(
            "✅ 广告修改成功"
        )
        
def build_ads_list_keyboard(ads):
    rows = []

    for ad in ads:
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if ad['enabled'] else '❌'} #{ad['id']} {ad['content'][:20]}",
                callback_data=f"publish:toggle_ad_{ad['id']}"
            )
        ])

    rows.append([
        InlineKeyboardButton("⬅️ 返回", callback_data="publish:ads")
    ])

    return InlineKeyboardMarkup(rows)   
    
def get_user(user_id):
    users = load_json(BOT_USER_FILE)
    return users.get(str(user_id))       

def _load_bottle_history():
    data = load_json(BOTTLE_HISTORY_FILE)
    return data if isinstance(data, dict) else {}

def _save_bottle_history(data):
    save_json(BOTTLE_HISTORY_FILE, data)

def get_user_bottle_data(user_id):
    data = _load_bottle_history()

    user_key = str(user_id)

    if user_key not in data:
        data[user_key] = {
            "history": [],
            "index": -1
        }

    return data

async def send_bottle(
    context,
    chat_id,
    channel_id,
    post
):

    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ 上一条",
                callback_data="publish:bottle_prev"
            ),
            # InlineKeyboardButton(
            #     "👤 添加好友",
            #     callback_data=f"publish:add_friend:{post['user_id']}"
            # ),
            InlineKeyboardButton(
                "➡️ 下一条",
                callback_data="publish:bottle_next"
            ),
        ]
    ]

    await context.bot.copy_message(
        chat_id=chat_id,
        from_chat_id=channel_id,
        message_id=post["channel_message_id"],
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def _load_cannel_message() -> list:
    data = load_json(USER_MESSAGE_FILE)
    return data if isinstance(data, list) else []

    
# =========================
# 注册
# =========================

def register_publish_setting_handlers(app):
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern=r"^publish:.+"))
    # Linked discussion groups receive main-channel posts as automatic forwards.
    # Keep the correspondence so approved user comments can reply below the post.
    # Use TypeHandler instead of MessageHandler: Telegram can deliver a linked
    # discussion copy as message, edited_message, channel_post or
    # edited_channel_post. MessageHandler filters may skip one of those shapes.
    # The callback returns immediately for all unrelated updates.
    app.add_handler(
        TypeHandler(Update, _capture_comment_source_message),
        # Must run before generic group handlers that may stop processing.
        group=-940,
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (~filters.COMMAND)
            & ~filters.UpdateType.BUSINESS_MESSAGE,
            _handle_text_input,
        ),
        group=10,
    )
