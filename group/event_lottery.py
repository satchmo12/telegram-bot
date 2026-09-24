"""Configurable scheduled group-event lotteries.

The feature is intentionally separate from the existing points/talk lotteries:
these events are published posts with explicit prizes, entry requirements and a
scheduled draw time.
"""
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from forward.message_forward import build_message_payload
from group.grouplist import load_users
from utils import (
    bot_datetime_from_timestamp,
    bot_now,
    get_bot_path,
    get_bot_timezone,
    get_group_whitelist,
    load_json,
    save_json,
)

CALLBACK_PREFIX = "eventlot"
LOTTERY_FILE = "data/event_lotteries.json"
DRAFT_KEY = "event_lottery_draft"
JOIN_KEY = "event_lottery_join"
MAX_PRIZES = 20
MAX_TARGETS = 10


def _file(context: ContextTypes.DEFAULT_TYPE) -> str:
    return get_bot_path(context, LOTTERY_FILE)


def _load(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = load_json(_file(context))
    if not isinstance(data, dict):
        data = {}
    records = data.get("lotteries")
    if not isinstance(records, dict):
        records = {}
    data["lotteries"] = records
    return data


def _save(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    save_json(_file(context), data)


def _record(data: dict, lottery_id: str) -> Optional[dict]:
    record = data.get("lotteries", {}).get(str(lottery_id))
    return record if isinstance(record, dict) else None


def _new_lottery(chat_id: int, owner_id: int) -> dict:
    now = int(time.time())
    return {
        "id": uuid.uuid4().hex[:12],
        "chat_id": int(chat_id),
        "owner_id": int(owner_id),
        "status": "draft",  # draft / scheduled / open / drawn
        "name": "",
        "description": "",
        "publish_at": 0,  # 0 means publish immediately
        "draw_at": 0,
        "prizes": [],
        "requirements": {
            "username": False,
            "min_messages": 0,
            "password": "",
        },
        # target items: {target: "@public_name", password: "optional"}
        "follow_targets": [],
        # extra locations to receive the published lottery post.
        "sync_targets": [],
        "cover": None,
        "participants": {},
        # Per-lottery messages counted only after this lottery is published.
        # chat_id -> user_id -> count
        "message_counts": {},
        "published_at": 0,
        "drawn_at": 0,
        "created_at": now,
        "updated_at": now,
    }


def _display_target(value: str) -> str:
    return str(value or "").strip() or "未设置"


def _normalize_target(value: str) -> str:
    target = str(value or "").strip()
    if target.startswith("https://t.me/"):
        target = "@" + target.rsplit("/", 1)[-1]
    elif target.startswith("t.me/"):
        target = "@" + target.rsplit("/", 1)[-1]
    if target and not target.startswith("@"):
        target = "@" + target.lstrip("@")
    return target if len(target) > 1 else ""


def _target_items(value: str) -> list[str]:
    result = []
    for raw in str(value or "").replace("，", ",").replace("\n", ",").split(","):
        item = _normalize_target(raw)
        if item and item not in result:
            result.append(item)
    return result


def _target_requirements(item: dict) -> dict:
    """Read per-follow-target requirements and migrate the legacy password field."""
    raw = item.get("requirements") if isinstance(item, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "username": bool(raw.get("username", False)),
        "min_messages": max(0, int(raw.get("min_messages", 0) or 0)),
        "password": str(raw.get("password") or item.get("password") or "").strip()[:80],
    }


def _set_target_requirements(item: dict, requirements: dict) -> dict:
    normalized = {
        "username": bool(requirements.get("username", False)),
        "min_messages": max(0, int(requirements.get("min_messages", 0) or 0)),
        "password": str(requirements.get("password") or "").strip()[:80],
    }
    item["requirements"] = normalized
    # Keep old saved records compatible, but all new settings use requirements.
    item.pop("password", None)
    return normalized


def _target_conditions_text(item: dict) -> list[str]:
    req = _target_requirements(item)
    lines = []
    if req["username"]:
        lines.append("└ 🪪 必须设置用户名")
    if _target_is_group(item):
        if req["min_messages"] > 0:
            lines.append(f"└ 🎟️ 本群发言大于 {req['min_messages']} 条")
        if req["password"]:
            lines.append(f"└ 🔑 发送口令﹝{req['password']}﹞")
    return lines


def _target_chat_type(item: dict) -> str:
    value = str((item or {}).get("chat_type") or "").lower()
    return value if value in {"channel", "group", "supergroup"} else ""


def _target_is_group(item: dict) -> bool:
    return _target_chat_type(item) in {"group", "supergroup"}


async def _get_admin_target_chat(context: ContextTypes.DEFAULT_TYPE, target: str):
    """Return the target chat only when this bot is an administrator there."""
    try:
        chat = await context.bot.get_chat(target)
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
    except Exception:
        return None
    status = getattr(member, "status", "")
    status = getattr(status, "value", status)
    if str(status).lower() not in {"creator", "owner", "administrator"}:
        return None
    return chat


async def _refresh_target_chat_type(context: ContextTypes.DEFAULT_TYPE, item: dict) -> bool:
    """Populate target type for older lottery records created before this field."""
    target = str((item or {}).get("target") or "")
    if not target:
        return False
    try:
        chat = await context.bot.get_chat(target)
    except Exception:
        return False
    chat_type = getattr(chat, "type", "")
    chat_type = getattr(chat_type, "value", chat_type)
    chat_type = str(chat_type).lower()
    if chat_type not in {"channel", "group", "supergroup"}:
        return False
    chat_id = getattr(chat, "id", None)
    changed = item.get("chat_type") != chat_type
    if chat_id is not None and str(item.get("chat_id") or "") != str(int(chat_id)):
        item["chat_id"] = int(chat_id)
        changed = True
    if not changed:
        return False
    item["chat_type"] = chat_type
    # Channels cannot enforce chat activity or a group password.
    if chat_type == "channel":
        req = _target_requirements(item)
        req["min_messages"] = 0
        req["password"] = ""
        _set_target_requirements(item, req)
    return True


def _timestamp_text(ts: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not ts:
        return "未设置"
    return bot_datetime_from_timestamp(ts, context).strftime("%Y-%m-%d %H:%M")


def _format_lottery(record: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    name = str(record.get("name") or "未命名抽奖")
    req = record.get("requirements") if isinstance(record.get("requirements"), dict) else {}
    lines = [f"🎰 <b>{name}</b>", "", "📮 <b>参与条件</b>"]
    has_conditions = False
    if req.get("username"):
        lines.append("🪪 必须设置用户名")
        has_conditions = True
    min_messages = int(req.get("min_messages", 0) or 0)
    if min_messages > 0:
        lines.append(f"🎟️ 发言数大于 {min_messages} 条")
        has_conditions = True
    password = str(req.get("password") or "").strip()
    if password:
        lines.append(f"🔑 发送口令﹝{password}﹞")
        has_conditions = True
    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        target = _display_target(item.get("target"))
        lines.append(f"🎫 加入-{target.lstrip('@')}")
        lines.extend(_target_conditions_text(item))
        has_conditions = True
    if not has_conditions:
        lines.append("无额外条件")

    lines.extend(["", "🎁 <b>奖品名单</b>"])
    prizes = record.get("prizes", []) or []
    if prizes:
        for prize in prizes:
            if isinstance(prize, dict):
                lines.append(f"💰️ {prize.get('name', '未命名奖品')} × <b>{int(prize.get('count', 1) or 1)}</b>")
    else:
        lines.append("暂未设置奖品")

    lines.extend(["", "📜 <b>抽奖说明</b>", str(record.get("description") or "未填写"), "", "📆 <b>开奖时间</b>"])
    lines.append(f"• {get_bot_timezone(context)} { _timestamp_text(int(record.get('draw_at', 0) or 0), context) }")
    return "\n".join(lines)


def _editor_text(record: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    req = record.get("requirements", {})
    return "\n".join([
        "🎰 抽奖编辑",
        f"状态：{_status_label(record)}",
        f"名称：{record.get('name') or '未设置'}",
        f"说明：{'已设置' if record.get('description') else '未设置'}",
        f"发布时间：{'立即发布' if not record.get('publish_at') else _timestamp_text(int(record['publish_at']), context)}",
        f"开奖时间：{_timestamp_text(int(record.get('draw_at', 0) or 0), context)}",
        f"奖品：{len(record.get('prizes', []))} 项",
        f"用户名：{'需要' if req.get('username') else '不要求'} | 发言：{int(req.get('min_messages', 0) or 0)} 条 | 口令：{'已设置' if req.get('password') else '未设置'}",
        f"关注目标：{len(record.get('follow_targets', []))} 个 | 同步位置：{len(record.get('sync_targets', []))} 个",
        f"封面：{'已设置' if isinstance(record.get('cover'), dict) else '未设置'}",
    ])


def _editor_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ 抽奖名称", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:name"),
            InlineKeyboardButton("📜 抽奖说明", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:description"),
        ],
        [
            InlineKeyboardButton("📅 发布时间", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:publish_at"),
            InlineKeyboardButton("⏰ 开奖时间", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:draw_at"),
        ],
        [
            InlineKeyboardButton("🎁 奖品设置", callback_data=f"{CALLBACK_PREFIX}:prizes:{lid}"),
            InlineKeyboardButton("📮 参与条件", callback_data=f"{CALLBACK_PREFIX}:requirements:{lid}"),
        ],
        [
            InlineKeyboardButton("🖼 抽奖封面", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:cover"),
            InlineKeyboardButton("📢 关注频道/群组", callback_data=f"{CALLBACK_PREFIX}:targets:{lid}:follow"),
        ],
        [
            InlineKeyboardButton("📍 同步位置", callback_data=f"{CALLBACK_PREFIX}:targets:{lid}:sync"),
            InlineKeyboardButton("👁 抽奖预览", callback_data=f"{CALLBACK_PREFIX}:preview:{lid}"),
        ],
        [
            InlineKeyboardButton("✅ 完成发布", callback_data=f"{CALLBACK_PREFIX}:publish:{lid}"),
            InlineKeyboardButton("❌ 放弃草稿", callback_data=f"{CALLBACK_PREFIX}:discard:{lid}"),
        ],
        [InlineKeyboardButton("⬅️ 返回抽奖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")],
    ])


def _prizes_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = []
    for index, prize in enumerate(record.get("prizes", [])):
        if isinstance(prize, dict):
            rows.append([InlineKeyboardButton(
                f"🗑 {prize.get('name', '未命名')} × {prize.get('count', 1)}",
                callback_data=f"{CALLBACK_PREFIX}:prize_delete:{lid}:{index}",
            )])
    if len(record.get("prizes", [])) < MAX_PRIZES:
        rows.append([InlineKeyboardButton("➕ 添加奖品", callback_data=f"{CALLBACK_PREFIX}:prize_add:{lid}")])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _requirements_keyboard(record: dict) -> InlineKeyboardMarkup:
    """Requirements for the primary lottery group."""
    lid = record["id"]
    req = record.get("requirements", {})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅' if req.get('username') else '🚫'} 必须有用户名",
            callback_data=f"{CALLBACK_PREFIX}:req_toggle:{lid}:username",
        )],
        [
            InlineKeyboardButton("🎟️ 发言条数", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:min_messages"),
            InlineKeyboardButton("🔑 参与口令", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:password"),
        ],
        [InlineKeyboardButton("⬅️ 返回群组选择", callback_data=f"{CALLBACK_PREFIX}:requirements:{lid}")],
    ])


def _requirements_selector_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = [[InlineKeyboardButton(
        "🎰 抽奖发布群（参与条件）",
        callback_data=f"{CALLBACK_PREFIX}:req_main:{lid}",
    )]]
    for index, item in enumerate(record.get("follow_targets", []) or []):
        if isinstance(item, dict):
            rows.append([InlineKeyboardButton(
                f"📢 {str(item.get('target') or '未设置')[:45]}",
                callback_data=f"{CALLBACK_PREFIX}:target_requirements:{lid}:{index}",
            )])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _target_requirements_keyboard(record: dict, index: int) -> InlineKeyboardMarkup:
    lid = record["id"]
    item = (record.get("follow_targets") or [])[index]
    req = _target_requirements(item)
    rows = [[InlineKeyboardButton(
        f"{'✅' if req['username'] else '🚫'} 必须有用户名",
        callback_data=f"{CALLBACK_PREFIX}:target_req_toggle:{lid}:{index}:username",
    )]]
    if _target_is_group(item):
        rows.append([
            InlineKeyboardButton("🎟️ 本群发言条数", callback_data=f"{CALLBACK_PREFIX}:target_req_field:{lid}:{index}:min_messages"),
            InlineKeyboardButton("🔑 本群参与口令", callback_data=f"{CALLBACK_PREFIX}:target_req_field:{lid}:{index}:password"),
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回群组选择", callback_data=f"{CALLBACK_PREFIX}:requirements:{lid}")])
    return InlineKeyboardMarkup(rows)


def _target_requirements_text(record: dict, index: int) -> str:
    item = (record.get("follow_targets") or [])[index]
    req = _target_requirements(item)
    is_group = _target_is_group(item)
    target_type = "群组" if is_group else "频道"
    lines = [
        f"📢 {item.get('target') or '未设置'} 的参与条件",
        f"类型：{target_type}",
        f"用户名：{'需要' if req['username'] else '不要求'}",
    ]
    if is_group:
        lines.extend([
            f"本群发言：{req['min_messages']} 条",
            f"口令：{'已设置' if req['password'] else '未设置'}",
        ])
    else:
        lines.append("频道不支持设置发言条数和参与口令。")
    return "\n".join(lines)


def _sync_targets_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = []
    sync_values = list(record.get("sync_targets", []) or [])
    for index, value in enumerate(sync_values):
        rows.append([InlineKeyboardButton(
            f"🗑 {value}", callback_data=f"{CALLBACK_PREFIX}:target_delete:{lid}:sync:{index}"
        )])
    for index, item in enumerate(record.get("follow_targets", []) or []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "")
        if target and target not in sync_values:
            rows.append([InlineKeyboardButton(
                f"➕ 同步到 {target}", callback_data=f"{CALLBACK_PREFIX}:sync_pick:{lid}:{index}"
            )])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _targets_keyboard(record: dict, kind: str) -> InlineKeyboardMarkup:
    if kind == "sync":
        return _sync_targets_keyboard(record)
    lid = record["id"]
    rows = []
    for index, item in enumerate(record.get("follow_targets", []) or []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "未设置")
        rows.append([
            InlineKeyboardButton(
                f"⚙️ {target}", callback_data=f"{CALLBACK_PREFIX}:target_requirements:{lid}:{index}"
            ),
            InlineKeyboardButton(
                "🗑 删除", callback_data=f"{CALLBACK_PREFIX}:target_delete:{lid}:follow:{index}"
            ),
        ])
    if len(record.get("follow_targets", []) or []) < MAX_TARGETS:
        rows.append([InlineKeyboardButton("➕ 添加关注频道/群组", callback_data=f"{CALLBACK_PREFIX}:target_add:{lid}:follow")])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _start_editor_input_stage(query, context: ContextTypes.DEFAULT_TYPE, lottery_id: str, field: str) -> None:
    """Remember the bot prompt so it can be removed after a successful input."""
    prompt = query.message
    context.user_data[DRAFT_KEY] = {
        "id": lottery_id,
        "field": field,
        "prompt_chat_id": getattr(getattr(prompt, "chat", None), "id", None),
        "prompt_message_id": getattr(prompt, "message_id", None),
    }


async def _delete_editor_input_prompt(context: ContextTypes.DEFAULT_TYPE, stage: dict) -> None:
    """Remove the temporary “please enter …” message and its back button."""
    chat_id = stage.get("prompt_chat_id")
    message_id = stage.get("prompt_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        # The setting itself has succeeded; deletion is best effort only.
        print(f"删除抽奖输入提示失败 chat={chat_id} message={message_id}: {exc}")


async def _can_manage(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return getattr(member, "status", "") in {"creator", "owner", "administrator"}


def _records_for_chat(data: dict, chat_id: int) -> list[dict]:
    records = [
        item for item in data.get("lotteries", {}).values()
        if isinstance(item, dict) and int(item.get("chat_id", 0) or 0) == int(chat_id)
    ]
    return sorted(records, key=lambda item: int(item.get("created_at", 0) or 0), reverse=True)


def _status_label(record: dict) -> str:
    return {
        "draft": "📝 草稿",
        "scheduled": "🕒 待发布",
        "open": "🟢 进行中",
        "drawn": "🏆 已开奖",
    }.get(str(record.get("status") or ""), "未知")


def _dashboard_text(records: list[dict], *, history: bool = False) -> str:
    title = "📚 历史抽奖" if history else "🎉 独立活动抽奖"
    lines = [title, "", "独立于积分抽奖和发言抽奖。"]
    shown = records if history else records[:6]
    if not shown:
        lines.append("\n暂无抽奖记录。")
    else:
        lines.append("")
        for record in shown:
            lines.append(f"{_status_label(record)} · {record.get('name') or '未命名抽奖'}")
    return "\n".join(lines)


def _dashboard_keyboard(chat_id: int, records: list[dict], *, history: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for record in (records if history else records[:6]):
        rows.append([InlineKeyboardButton(
            f"{_status_label(record)} {str(record.get('name') or '未命名')[:35]}",
            callback_data=f"{CALLBACK_PREFIX}:view:{record['id']}",
        )])
    if not history:
        rows.append([InlineKeyboardButton("➕ 创建独立抽奖", callback_data=f"{CALLBACK_PREFIX}:create:{chat_id}")])
        rows.append([InlineKeyboardButton("📚 查看历史抽奖", callback_data=f"{CALLBACK_PREFIX}:history:{chat_id}")])
    rows.append([InlineKeyboardButton("⬅️ 返回群设置", callback_data=f"gcfg:open:{chat_id}")])
    return InlineKeyboardMarkup(rows)


async def open_lottery_creator(query, context: ContextTypes.DEFAULT_TYPE, chat_id_str: str, user_id: int):
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        return await query.answer("群ID无效。", show_alert=True)
    if not await _can_manage(context, user_id, chat_id):
        return await query.answer("你不是该群管理员，无法管理抽奖。", show_alert=True)
    data = _load(context)
    records = _records_for_chat(data, chat_id)
    await query.answer()
    return await query.edit_message_text(
        _dashboard_text(records),
        reply_markup=_dashboard_keyboard(chat_id, records),
    )


async def _create_lottery(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    data = _load(context)
    record = _new_lottery(chat_id, user_id)
    data["lotteries"][record["id"]] = record
    _save(context, data)
    await query.answer("已创建抽奖草稿；草稿不会自动发布。")
    return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))


def _time_input_prompt(field: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Build time-entry guidance using this bot's configured local clock."""
    now = bot_now(context)
    timezone_name = get_bot_timezone(context)
    now_text = now.strftime("%Y-%m-%d %H:%M")
    if field == "publish_at":
        example = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        return (
            "请输入发布时间，或点击下方“⚡ 立即发布”。\n"
            f"当前机器人时间（{timezone_name}）：{now_text}\n"
            f"定时示例（5 分钟后）：{example}"
        )
    example = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    return (
        "请输入未来开奖时间，格式 YYYY-MM-DD HH:MM。\n"
        f"当前机器人时间（{timezone_name}）：{now_text}\n"
        f"可填写示例（1 小时后）：{example}"
    )


def _parse_time(value: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    text = str(value or "").strip()
    if text in {"立即", "现在"}:
        return 0
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
        local = naive.replace(tzinfo=bot_now(context).tzinfo)
        timestamp = int(local.timestamp())
    except Exception:
        return None
    return timestamp if timestamp > int(time.time()) else None


async def _send_lottery_content(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
    record: dict,
    *,
    reply_markup=None,
):
    """Send the lottery as one text post or one cover-media post with a caption."""
    text = _format_lottery(record, context)
    cover = record.get("cover")
    if not isinstance(cover, dict):
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    # Telegram captions are limited to 1024 characters. Do not silently split
    # the cover and lottery copy, because this workflow requires one message.
    if len(text) > 1024:
        raise ValueError("设置封面时，抽奖文案不能超过 1024 个字符。请缩短抽奖说明后再预览或发布。")

    payload_type = str(cover.get("type") or "").lower()
    file_id = cover.get("file_id")
    if not file_id:
        raise ValueError("抽奖封面文件无效，请重新上传图片或视频。")
    common = {
        "chat_id": chat_id,
        "caption": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    if payload_type == "photo":
        return await context.bot.send_photo(photo=file_id, **common)
    if payload_type == "video":
        return await context.bot.send_video(video=file_id, **common)
    raise ValueError("抽奖封面仅支持图片或视频，请重新设置封面。")


async def _lottery_deep_link(context: ContextTypes.DEFAULT_TYPE, lottery_id: str) -> str:
    username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not username:
        try:
            username = str((await context.bot.get_me()).username or "").strip().lstrip("@")
        except Exception:
            return ""
    return f"https://t.me/{username}?start=eventlot_{lottery_id}" if username else ""


async def _lottery_join_markup(context: ContextTypes.DEFAULT_TYPE, record: dict) -> InlineKeyboardMarkup:
    link = await _lottery_deep_link(context, str(record["id"]))
    if link:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🎫 参与抽奖", url=link)]])
    # A bot without a username cannot use a deep link. Keep old posts usable.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎫 参与抽奖", callback_data=f"{CALLBACK_PREFIX}:join:{record['id']}")
    ]])


async def _send_lottery_post(context: ContextTypes.DEFAULT_TYPE, record: dict) -> int:
    markup = await _lottery_join_markup(context, record)
    targets = [str(record["chat_id"])] + [str(item) for item in record.get("sync_targets", [])]
    sent = 0
    for target in dict.fromkeys(targets):
        try:
            await _send_lottery_content(
                context,
                target,
                record,
                reply_markup=markup,
            )
            sent += 1
        except Exception as exc:
            print(f"抽奖发布失败 lottery={record['id']} target={target}: {exc}")
    return sent


async def _publish_if_due(context: ContextTypes.DEFAULT_TYPE, record: dict) -> bool:
    # Drafts are never eligible for the scheduler. A draft can exist forever
    # without posting anything until its owner explicitly presses 完成发布.
    now = int(time.time())
    if record.get("status") == "scheduled" and int(record.get("publish_at", 0) or 0) <= now:
        sent = await _send_lottery_post(context, record)
        if sent:
            record["status"] = "open"
            record["published_at"] = now
            record["message_counts"] = {}
            return True
    return False


async def _draw_if_due(context: ContextTypes.DEFAULT_TYPE, record: dict) -> bool:
    if record.get("status") != "open" or not record.get("draw_at") or int(record["draw_at"]) > int(time.time()):
        return False
    participants = [
        item
        for item in (record.get("participants") or {}).values()
        if isinstance(item, dict) and bool(item.get("eligible", True))
    ]
    pool = list(participants)
    random.shuffle(pool)
    winners = []
    for prize in record.get("prizes", []):
        if not isinstance(prize, dict):
            continue
        for _ in range(max(0, int(prize.get("count", 1) or 1))):
            if not pool:
                break
            winner = pool.pop()
            winners.append({"prize": prize.get("name", "奖品"), "user_id": winner.get("user_id"), "name": winner.get("name", "用户")})
    lines = [f"🎉 <b>{record.get('name') or '抽奖'} 开奖结果</b>", ""]
    if winners:
        for item in winners:
            lines.append(f"🎁 {item['prize']}：{item['name']}")
    else:
        lines.append("暂无符合条件的参与者。")
    for target in dict.fromkeys([str(record["chat_id"])] + [str(item) for item in record.get("sync_targets", [])]):
        try:
            await context.bot.send_message(chat_id=target, text="\n".join(lines), parse_mode="HTML")
        except Exception as exc:
            print(f"抽奖开奖通知失败 lottery={record['id']} target={target}: {exc}")
    record["status"] = "drawn"
    record["drawn_at"] = int(time.time())
    record["winners"] = winners
    return True


async def event_lottery_tick(context: ContextTypes.DEFAULT_TYPE):
    data = _load(context)
    changed = False
    for record in data.get("lotteries", {}).values():
        if not isinstance(record, dict):
            continue
        changed = await _publish_if_due(context, record) or changed
        changed = await _draw_if_due(context, record) or changed
    if changed:
        _save(context, data)


def _lottery_message_count(record: dict, chat_id: int, user_id: int) -> int:
    """Read only messages recorded after this lottery was published."""
    counts = record.get("message_counts") if isinstance(record.get("message_counts"), dict) else {}
    by_chat = counts.get(str(chat_id)) if isinstance(counts, dict) else {}
    value = by_chat.get(str(user_id), 0) if isinstance(by_chat, dict) else 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _lottery_counted_chat_ids(record: dict) -> set[int]:
    result = {int(record["chat_id"])}
    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue
        if _target_is_group(item):
            result.add(chat_id)
    return result


async def event_lottery_message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Count eligible group messages from the instant an open lottery is published."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or getattr(user, "is_bot", False):
        return
    if chat.type not in {"group", "supergroup"}:
        return

    message_date = getattr(msg, "date", None)
    message_timestamp = int(message_date.timestamp()) if message_date else int(time.time())
    data = _load(context)
    changed = False
    for record in data.get("lotteries", {}).values():
        if not isinstance(record, dict) or record.get("status") != "open":
            continue
        published_at = int(record.get("published_at", 0) or 0)
        if not published_at or message_timestamp < published_at:
            continue
        if int(chat.id) not in _lottery_counted_chat_ids(record):
            continue
        counts = record.setdefault("message_counts", {})
        by_chat = counts.setdefault(str(chat.id), {})
        user_key = str(user.id)
        by_chat[user_key] = int(by_chat.get(user_key, 0) or 0) + 1
        changed = True
    if changed:
        _save(context, data)


def _registration(record: dict, user) -> dict:
    participants = record.setdefault("participants", {})
    user_id = str(user.id)
    entry = participants.get(user_id)
    if not isinstance(entry, dict):
        entry = {
            "user_id": user.id,
            "name": user.full_name,
            "username": user.username,
            "registered_at": int(time.time()),
            "eligible": False,
            "passed_passwords": [],
        }
        participants[user_id] = entry
    else:
        entry["name"] = user.full_name
        entry["username"] = user.username
        entry.setdefault("registered_at", int(time.time()))
        entry.setdefault("eligible", False)
        if not isinstance(entry.get("passed_passwords"), list):
            entry["passed_passwords"] = []
    return entry


def _required_passwords(record: dict) -> list[str]:
    passwords = [str(record.get("requirements", {}).get("password") or "").strip()]
    passwords.extend(
        _target_requirements(item)["password"]
        for item in record.get("follow_targets", []) or []
        if isinstance(item, dict) and _target_is_group(item)
    )
    return list(dict.fromkeys(value for value in passwords if value))


async def _build_participation_status(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    record: dict,
    *,
    submitted_password: str = "",
) -> tuple[bool, str]:
    """Register a user and render every completed/pending lottery requirement."""
    # Older records did not persist target chat_type. Refresh it before
    # evaluating group-only conditions such as talk count and password.
    for item in record.get("follow_targets", []) or []:
        if isinstance(item, dict):
            await _refresh_target_chat_type(context, item)

    registration = _registration(record, user)
    required_passwords = _required_passwords(record)
    passed = {
        str(value).strip()
        for value in registration.get("passed_passwords", [])
        if str(value).strip()
    }
    candidate = str(submitted_password or "").strip()
    if candidate and candidate in required_passwords:
        passed.add(candidate)
    registration["passed_passwords"] = sorted(passed)

    eligible = True
    condition_lines = []
    main_req = record.get("requirements") if isinstance(record.get("requirements"), dict) else {}
    has_main_requirements = bool(
        main_req.get("username")
        or int(main_req.get("min_messages", 0) or 0) > 0
        or str(main_req.get("password") or "").strip()
    )
    if has_main_requirements:
        condition_lines.append("🎫 抽奖发布群 ✅")
        if main_req.get("username"):
            ok = bool(getattr(user, "username", None))
            condition_lines.append(f"└ 🪪 已设置用户名 {'✅' if ok else '❌'}")
            eligible = eligible and ok
        main_min = int(main_req.get("min_messages", 0) or 0)
        if main_min > 0:
            count = _lottery_message_count(record, int(record["chat_id"]), user.id)
            ok = count > main_min
            condition_lines.append(
                f"└ 🎟️ 发布后发言数大于 {main_min} 条 {'✅' if ok else '❌'}（{count}/{main_min}）"
            )
            eligible = eligible and ok
        main_password = str(main_req.get("password") or "").strip()
        if main_password:
            ok = main_password in passed
            condition_lines.append(f"└ 🔑 发送口令﹝{main_password}﹞ {'✅' if ok else '❌'}")
            eligible = eligible and ok

    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "未设置")
        target_name = target.lstrip("@") or target
        joined = False
        chat = None
        try:
            chat = await context.bot.get_chat(target)
            member = await context.bot.get_chat_member(chat.id, user.id)
            status = getattr(member, "status", "")
            status = getattr(status, "value", status)
            joined = str(status).lower() not in {"left", "kicked"}
        except Exception:
            joined = False
        condition_lines.append(f"🎫 已加入-{target_name} {'✅' if joined else '❌'}")
        eligible = eligible and joined

        req = _target_requirements(item)
        if req["username"]:
            ok = bool(getattr(user, "username", None))
            condition_lines.append(f"└ 🪪 已设置用户名 {'✅' if ok else '❌'}")
            eligible = eligible and ok
        if _target_is_group(item):
            if req["min_messages"] > 0:
                count = _lottery_message_count(record, int(chat.id), user.id) if joined and chat else 0
                ok = count > req["min_messages"]
                condition_lines.append(
                    f"└ 🎟️ 发布后发言数大于 {req['min_messages']} 条 {'✅' if ok else '❌'}（{count}/{req['min_messages']}）"
                )
                eligible = eligible and ok
            if req["password"]:
                ok = req["password"] in passed
                condition_lines.append(f"└ 🔑 发送口令﹝{req['password']}﹞ {'✅' if ok else '❌'}")
                eligible = eligible and ok

    registration["eligible"] = bool(eligible)
    registration["checked_at"] = int(time.time())
    participants = record.get("participants") or {}
    registered_count = sum(1 for item in participants.values() if isinstance(item, dict))
    eligible_count = sum(
        1 for item in participants.values()
        if isinstance(item, dict) and bool(item.get("eligible", False))
    )
    status_title = "达标" if eligible else "未达标"
    status_note = "✅ 您已完成全部激活任务，可以参与抽奖！" if eligible else "❌ 您还需要完成激活任务才能抽奖！"
    draw_time = _timestamp_text(int(record.get("draw_at", 0) or 0), context)
    lines = [
        f"<b>您已成功报名抽奖（{status_title}）</b>",
        "",
        status_note,
        "",
        f"🎰 <b>{record.get('name') or '抽奖'}</b>",
        "",
        *(condition_lines or ["无需额外参与条件。"]),
    ]

    # Once the user is eligible, include the actual lottery information rather
    # than only the activation checklist.
    if eligible:
        lines.extend(["", "📜 <b>抽奖详情</b>", str(record.get("description") or "未填写")])
        lines.extend(["", "🎁 <b>奖品名单</b>"])
        prizes = record.get("prizes", []) or []
        if prizes:
            for prize in prizes:
                if isinstance(prize, dict):
                    lines.append(
                        f"💰️ {prize.get('name', '未命名奖品')} × <b>{int(prize.get('count', 1) or 1)}</b>"
                    )
        else:
            lines.append("暂未设置奖品")

    lines.extend([
        "",
        f"💁 参与情况：{eligible_count}达标 / {registered_count}报名",
        "",
        f"📅 开奖时间：（{get_bot_timezone(context)}）",
        draw_time,
    ])
    return bool(eligible), "\n".join(lines)


def _participation_status_keyboard(record: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新参与条件", callback_data=f"{CALLBACK_PREFIX}:join_refresh:{record['id']}")
    ]])


async def event_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    lottery_id = parts[2] if len(parts) > 2 else ""
    data = _load(context)

    if action in {"dashboard", "history", "create", "back"}:
        try:
            chat_id = int(lottery_id)
        except (TypeError, ValueError):
            return await query.answer("群ID无效。", show_alert=True)
        if action == "back":
            await query.answer()
            return await query.edit_message_text("已返回群设置。")
        if not await _can_manage(context, query.from_user.id, chat_id):
            return await query.answer("无管理权限。", show_alert=True)
        records = _records_for_chat(data, chat_id)
        if action == "dashboard":
            await query.answer()
            return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(chat_id, records))
        if action == "history":
            await query.answer()
            return await query.edit_message_text(_dashboard_text(records, history=True), reply_markup=_dashboard_keyboard(chat_id, records, history=True))
        return await _create_lottery(query, context, chat_id, query.from_user.id)

    record = _record(data, lottery_id)
    if not record:
        return await query.answer("抽奖不存在或已删除。", show_alert=True)

    if action == "time_immediate":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        record["publish_at"] = 0
        record["updated_at"] = int(time.time())
        _save(context, data)
        stage = context.user_data.get(DRAFT_KEY)
        if isinstance(stage, dict) and stage.get("id") == lottery_id and stage.get("field") == "publish_at":
            context.user_data.pop(DRAFT_KEY, None)
        await query.answer("已设置为立即发布。")
        return await query.edit_message_text(
            _editor_text(record, context),
            reply_markup=_editor_keyboard(record),
        )

    if action == "clone":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        clone = _new_lottery(int(record["chat_id"]), query.from_user.id)
        clone["name"] = (str(record.get("name") or "未命名抽奖") + "（副本）")[:80]
        clone["description"] = str(record.get("description") or "")
        clone["draw_at"] = 0
        clone["prizes"] = [dict(item) for item in record.get("prizes", []) if isinstance(item, dict)]
        clone["requirements"] = dict(record.get("requirements") or {})
        clone["follow_targets"] = [
            {**item, "requirements": dict(_target_requirements(item))}
            for item in record.get("follow_targets", [])
            if isinstance(item, dict)
        ]
        clone["sync_targets"] = list(record.get("sync_targets", []) or [])
        clone["cover"] = dict(record["cover"]) if isinstance(record.get("cover"), dict) else None
        # Publication schedule, participants and winners are deliberately reset.
        clone["publish_at"] = 0
        clone["status"] = "draft"
        data["lotteries"][clone["id"]] = clone
        _save(context, data)
        await query.answer("已根据历史设置创建草稿，不会自动发布。")
        return await query.edit_message_text(_editor_text(clone, context), reply_markup=_editor_keyboard(clone))

    if action == "view":
        text = _format_lottery(record, context) + f"\n\n状态：{_status_label(record)}"
        rows = []
        if record.get("status") in {"draft", "scheduled", "open"}:
            rows.append([InlineKeyboardButton("✏️ 编辑抽奖", callback_data=f"{CALLBACK_PREFIX}:edit:{record['id']}")])
            rows.append([InlineKeyboardButton("🗑 删除抽奖", callback_data=f"{CALLBACK_PREFIX}:delete:{record['id']}")])
        rows.append([InlineKeyboardButton("📋 使用此设置新建", callback_data=f"{CALLBACK_PREFIX}:clone:{record['id']}")])
        rows.append([InlineKeyboardButton("⬅️ 返回抽奖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")])
        await query.answer()
        return await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    if action in {"discard", "delete"}:
        if action == "discard" and record.get("status") != "draft":
            return await query.answer("草稿以外的抽奖请使用删除操作。", show_alert=True)
        data["lotteries"].pop(record["id"], None)
        _save(context, data)
        records = _records_for_chat(data, int(record["chat_id"]))
        note = "草稿已放弃，不会发布。" if action == "discard" else "抽奖已删除；已发出的历史消息不会自动撤回。"
        await query.answer(note)
        return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(int(record["chat_id"]), records))

    if action == "edit":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "field":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        field = parts[3] if len(parts) > 3 else ""
        _start_editor_input_stage(query, context, lottery_id, field)
        prompts = {
            "name": "请输入抽奖名称。",
            "description": "请输入抽奖说明。",
            "publish_at": _time_input_prompt("publish_at", context),
            "draw_at": _time_input_prompt("draw_at", context),
            "min_messages": "请输入最低发言条数；输入 0 表示不限制。",
            "password": "请输入参与口令；发送“清空”清除口令。",
            "cover": "请发送抽奖封面（图片或视频），或发送“清空”不设置封面。",
        }
        rows = []
        if field == "publish_at":
            rows.append([InlineKeyboardButton(
                "⚡ 立即发布",
                callback_data=f"{CALLBACK_PREFIX}:time_immediate:{lottery_id}",
            )])
        rows.append([InlineKeyboardButton(
            "⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lottery_id}"
        )])
        await query.answer()
        return await query.edit_message_text(
            prompts.get(field, "请输入设置内容。"),
            reply_markup=InlineKeyboardMarkup(rows),
        )
    if action == "prizes":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text("🎁 奖品设置", reply_markup=_prizes_keyboard(record))
    if action == "prize_add":
        _start_editor_input_stage(query, context, lottery_id, "prize_add")
        await query.answer()
        return await query.edit_message_text("请输入奖品，格式：奖品名称 x 数量\n例如：手机 x 2", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回奖品设置", callback_data=f"{CALLBACK_PREFIX}:prizes:{lottery_id}")]]))
    if action == "prize_delete" and len(parts) >= 4:
        index = int(parts[3])
        if 0 <= index < len(record.get("prizes", [])):
            record["prizes"].pop(index)
            record["updated_at"] = int(time.time())
            _save(context, data)
        await query.answer("已删除奖品")
        return await query.edit_message_text("🎁 奖品设置", reply_markup=_prizes_keyboard(record))
    if action == "requirements":
        await query.answer()
        return await query.edit_message_text(
            "📮 请选择要设置参与条件的群组：",
            reply_markup=_requirements_selector_keyboard(record),
        )
    if action == "req_main":
        await query.answer()
        return await query.edit_message_text(
            "🎰 抽奖发布群的参与条件",
            reply_markup=_requirements_keyboard(record),
        )
    if action == "req_toggle" and len(parts) >= 4:
        key = parts[3]
        if key == "username":
            record.setdefault("requirements", {})["username"] = not bool(record["requirements"].get("username"))
            _save(context, data)
        await query.answer("已更新")
        return await query.edit_message_text("🎰 抽奖发布群的参与条件", reply_markup=_requirements_keyboard(record))
    if action == "target_requirements" and len(parts) >= 4:
        index = int(parts[3])
        values = record.get("follow_targets", []) or []
        if not 0 <= index < len(values) or not isinstance(values[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        if await _refresh_target_chat_type(context, values[index]):
            _save(context, data)
        await query.answer()
        return await query.edit_message_text(
            _target_requirements_text(record, index),
            reply_markup=_target_requirements_keyboard(record, index),
        )
    if action == "target_req_toggle" and len(parts) >= 5:
        index, key = int(parts[3]), parts[4]
        values = record.get("follow_targets", []) or []
        if not 0 <= index < len(values) or not isinstance(values[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        req = _target_requirements(values[index])
        if key == "username":
            req["username"] = not req["username"]
            _set_target_requirements(values[index], req)
            _save(context, data)
        await query.answer("已更新")
        return await query.edit_message_text(
            _target_requirements_text(record, index),
            reply_markup=_target_requirements_keyboard(record, index),
        )
    if action == "target_req_field" and len(parts) >= 5:
        index, field = int(parts[3]), parts[4]
        values = record.get("follow_targets", []) or []
        if field not in {"min_messages", "password"} or not 0 <= index < len(values) or not isinstance(values[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        if not _target_is_group(values[index]):
            return await query.answer("频道不能设置发言条数或参与口令，只有群组可以设置。", show_alert=True)
        stage_field = f"target_req_{field}"
        _start_editor_input_stage(query, context, lottery_id, stage_field)
        context.user_data[DRAFT_KEY]["target_index"] = index
        context.user_data[DRAFT_KEY]["return_to"] = "target_requirements"
        prompt = "请输入该群最低发言条数；输入 0 表示不限制。" if field == "min_messages" else "请输入该群参与口令；发送“清空”清除口令。"
        await query.answer()
        return await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回群组条件", callback_data=f"{CALLBACK_PREFIX}:target_requirements:{lottery_id}:{index}")
            ]]),
        )
    if action == "targets" and len(parts) >= 4:
        kind = parts[3]
        await query.answer()
        title = "📢 关注频道/群组（点目标可设置该群参与条件）" if kind == "follow" else "📍 同步位置（从关注列表中选择）"
        return await query.edit_message_text(title, reply_markup=_targets_keyboard(record, kind))
    if action == "target_add" and len(parts) >= 4:
        kind = parts[3]
        if kind == "sync":
            await query.answer()
            return await query.edit_message_text("📍 同步位置（从关注列表中选择）", reply_markup=_targets_keyboard(record, "sync"))
        _start_editor_input_stage(query, context, lottery_id, "target_follow")
        await query.answer()
        return await query.edit_message_text(
            "请输入要关注的公开 @频道或 @群组。\n机器人必须已是该频道或群组的管理员，才可添加。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回关注列表", callback_data=f"{CALLBACK_PREFIX}:targets:{lottery_id}:follow")
            ]]),
        )
    if action == "sync_pick" and len(parts) >= 4:
        index = int(parts[3])
        follows = record.get("follow_targets", []) or []
        if not 0 <= index < len(follows) or not isinstance(follows[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        target = str(follows[index].get("target") or "")
        values = record.setdefault("sync_targets", [])
        if target and target not in values:
            if len(values) >= MAX_TARGETS:
                return await query.answer(f"最多只能设置 {MAX_TARGETS} 个同步位置。", show_alert=True)
            values.append(target)
            _save(context, data)
        await query.answer("已添加同步位置")
        return await query.edit_message_text("📍 同步位置（从关注列表中选择）", reply_markup=_targets_keyboard(record, "sync"))
    if action == "target_delete" and len(parts) >= 5:
        kind, index = parts[3], int(parts[4])
        key = "follow_targets" if kind == "follow" else "sync_targets"
        values = record.get(key, [])
        if 0 <= index < len(values):
            removed = values.pop(index)
            if kind == "follow":
                target = str((removed or {}).get("target") if isinstance(removed, dict) else removed)
                record["sync_targets"] = [item for item in record.get("sync_targets", []) if item != target]
            _save(context, data)
        await query.answer("已移除")
        title = "📢 关注频道/群组（点目标可设置该群参与条件）" if kind == "follow" else "📍 同步位置（从关注列表中选择）"
        return await query.edit_message_text(title, reply_markup=_targets_keyboard(record, kind))
    if action == "preview":
        await query.answer()
        try:
            return await _send_lottery_content(
                context,
                query.message.chat_id,
                record,
            )
        except Exception as exc:
            return await query.message.reply_text(f"❌ 无法预览：{exc}")
    if action == "publish":
        if not record.get("name") or not record.get("prizes") or not record.get("draw_at"):
            return await query.answer("请至少设置名称、奖品和开奖时间。", show_alert=True)
        if record.get("status") in {"open", "drawn"}:
            return await query.answer("该抽奖已经发布，不能重复发布。", show_alert=True)
        record["updated_at"] = int(time.time())
        if record.get("publish_at"):
            record["status"] = "scheduled"
            _save(context, data)
            await query.answer("已安排定时发布")
        else:
            sent = await _send_lottery_post(context, record)
            if not sent:
                return await query.answer("发布失败，请确认机器人可在目标位置发言。", show_alert=True)
            record["status"] = "open"
            record["published_at"] = int(time.time())
            record["message_counts"] = {}
            _save(context, data)
            await query.answer("已立即发布")
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "join":
        if record.get("status") != "open":
            return await query.answer("抽奖尚未开放或已结束。", show_alert=True)
        link = await _lottery_deep_link(context, lottery_id)
        if link:
            # Telegram opens the deep link directly from the callback response;
            # do not leave an extra prompt/button in the lottery group.
            return await query.answer(url=link)
        return await query.answer("机器人未设置用户名，暂时无法跳转私聊。", show_alert=True)
    if action == "join_refresh":
        if record.get("status") != "open":
            return await query.answer("抽奖尚未开放或已结束。", show_alert=True)
        await query.answer("已刷新")
        _eligible, status_text = await _build_participation_status(context, query.from_user, record)
        _save(context, data)
        return await query.edit_message_text(
            status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )



async def _handle_editor_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    stage = context.user_data.get(DRAFT_KEY)
    if not isinstance(stage, dict) or not update.message or not update.effective_user:
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    data = _load(context)
    record = _record(data, stage.get("id"))
    if not record or not await _can_manage(context, update.effective_user.id, int(record["chat_id"])):
        context.user_data.pop(DRAFT_KEY, None)
        await update.message.reply_text("❌ 抽奖草稿不存在或你没有管理权限。")
        return True
    field = str(stage.get("field") or "")
    text = (update.message.text or "").strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(DRAFT_KEY, None)
        await _delete_editor_input_prompt(context, stage)
        if stage.get("return_to") == "target_requirements":
            index = int(stage.get("target_index", -1))
            values = record.get("follow_targets", []) or []
            if 0 <= index < len(values) and isinstance(values[index], dict):
                await update.message.reply_text(
                    _target_requirements_text(record, index),
                    reply_markup=_target_requirements_keyboard(record, index),
                )
                return True
        await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
        return True

    req = record.setdefault("requirements", {})
    try:
        if field == "name":
            if not text:
                raise ValueError("抽奖名称不能为空。")
            record["name"] = text[:80]
        elif field == "description":
            if not text:
                raise ValueError("抽奖说明不能为空。")
            record["description"] = text[:3500]
        elif field == "publish_at":
            parsed = _parse_time(text, context)
            if parsed is None:
                raise ValueError("请输入“立即”或未来时间 YYYY-MM-DD HH:MM。")
            record["publish_at"] = parsed
        elif field == "draw_at":
            parsed = _parse_time(text, context)
            if not parsed:
                raise ValueError("请输入未来开奖时间 YYYY-MM-DD HH:MM。")
            record["draw_at"] = parsed
        elif field == "min_messages":
            if not text.isdigit():
                raise ValueError("发言条数必须是非负整数。")
            req["min_messages"] = min(100000, int(text))
        elif field == "password":
            req["password"] = "" if text in {"清空", "关闭"} else text[:80]
        elif field == "cover":
            if text in {"清空", "关闭"}:
                record["cover"] = None
            else:
                cover = build_message_payload(update.message)
                if cover.get("type") not in {"photo", "video"}:
                    raise ValueError("抽奖封面仅支持图片或视频。")
                record["cover"] = cover
        elif field == "prize_add":
            match = re.fullmatch(r"(.+?)\s*[xX×]\s*(\d+)\s*", text)
            if not match:
                raise ValueError("格式：奖品名称 x 数量，例如：手机 x 2")
            name, count_raw = (match.group(1).strip(), match.group(2))
            if not name or int(count_raw) <= 0:
                raise ValueError("奖品格式或数量不正确。")
            if len(record.get("prizes", [])) >= MAX_PRIZES:
                raise ValueError(f"最多只能设置 {MAX_PRIZES} 项奖品。")
            record.setdefault("prizes", []).append({"name": name[:80], "count": int(count_raw)})
        elif field == "target_follow":
            if len(record.get("follow_targets", [])) >= MAX_TARGETS:
                raise ValueError(f"最多只能设置 {MAX_TARGETS} 个关注目标。")
            target = _normalize_target(text)
            if not target:
                raise ValueError("请输入公开 @频道或 @群组用户名。")
            target_chat = await _get_admin_target_chat(context, target)
            if target_chat is None:
                raise ValueError("机器人不是该频道或群组的管理员，无法添加为关注目标。")
            chat_type = getattr(target_chat, "type", "")
            chat_type = getattr(chat_type, "value", chat_type)
            chat_type = str(chat_type).lower()
            if chat_type not in {"channel", "group", "supergroup"}:
                raise ValueError("仅支持频道、群组或超级群组作为关注目标。")
            values = record.setdefault("follow_targets", [])
            if any(item.get("target") == target for item in values if isinstance(item, dict)):
                raise ValueError("该关注目标已经存在。")
            values.append({
                "target": target,
                "chat_id": int(target_chat.id),
                "chat_type": chat_type,
                "requirements": {"username": False, "min_messages": 0, "password": ""},
            })
        elif field in {"target_req_min_messages", "target_req_password"}:
            index = int(stage.get("target_index", -1))
            values = record.get("follow_targets", []) or []
            if not 0 <= index < len(values) or not isinstance(values[index], dict):
                raise ValueError("关注目标不存在。")
            if not _target_is_group(values[index]):
                raise ValueError("频道不能设置发言条数或参与口令，只有群组可以设置。")
            target_req = _target_requirements(values[index])
            if field == "target_req_min_messages":
                if not text.isdigit():
                    raise ValueError("发言条数必须是非负整数。")
                target_req["min_messages"] = min(100000, int(text))
            else:
                target_req["password"] = "" if text in {"清空", "关闭"} else text[:80]
            _set_target_requirements(values[index], target_req)
        else:
            raise ValueError("未知抽奖设置项。")
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
        return True

    record["updated_at"] = int(time.time())
    _save(context, data)
    context.user_data.pop(DRAFT_KEY, None)
    await _delete_editor_input_prompt(context, stage)
    if stage.get("return_to") == "target_requirements":
        index = int(stage.get("target_index", -1))
        values = record.get("follow_targets", []) or []
        if 0 <= index < len(values) and isinstance(values[index], dict):
            await update.message.reply_text(
                _target_requirements_text(record, index),
                reply_markup=_target_requirements_keyboard(record, index),
            )
            return True
    await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    return True


async def event_lottery_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open a lottery registration/status card from t.me/<bot>?start=eventlot_<id>."""
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private" or not context.args:
        return
    parameter = str(context.args[0] or "")
    if not parameter.startswith("eventlot_"):
        return
    lottery_id = parameter[len("eventlot_"):]
    data = _load(context)
    record = _record(data, lottery_id)
    if not record or record.get("status") != "open":
        await update.message.reply_text("❌ 该抽奖不存在、尚未开放或已经结束。")
        raise ApplicationHandlerStop

    context.user_data[JOIN_KEY] = {"id": lottery_id, "source_chat_id": int(record["chat_id"])}
    _eligible, status_text = await _build_participation_status(context, update.effective_user, record)
    _save(context, data)
    await update.message.reply_text(
        status_text,
        parse_mode="HTML",
        reply_markup=_participation_status_keyboard(record),
    )
    raise ApplicationHandlerStop


async def event_lottery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_editor_input(update, context):
        return
    if not update.effective_user or not update.effective_chat or not update.message:
        return

    chat = update.effective_chat
    code = (update.message.text or "").strip()
    data = _load(context)
    state = context.user_data.get(JOIN_KEY)
    record = _record(data, state.get("id")) if isinstance(state, dict) else None

    # A matching password in the original lottery group can also resume the
    # flow when the user has not opened the private card in this session yet.
    if not isinstance(record, dict) and chat.type in {"group", "supergroup"}:
        matches = [
            item
            for item in data.get("lotteries", {}).values()
            if isinstance(item, dict)
            and item.get("status") == "open"
            and int(item.get("chat_id", 0) or 0) == int(chat.id)
            and code in _required_passwords(item)
        ]
        if len(matches) == 1:
            record = matches[0]
            context.user_data[JOIN_KEY] = {
                "id": record["id"],
                "source_chat_id": int(record["chat_id"]),
            }

    if not isinstance(record, dict) or record.get("status") != "open":
        if isinstance(state, dict):
            context.user_data.pop(JOIN_KEY, None)
        return

    is_private = chat.type == "private"
    is_lottery_group = int(chat.id) == int(record["chat_id"])
    # Ordinary messages remain silent. A matching participation password may be
    # sent either to the bot privately or in the original lottery group.
    if not (is_private or is_lottery_group) or code not in _required_passwords(record):
        return

    _eligible, status_text = await _build_participation_status(
        context,
        update.effective_user,
        record,
        submitted_password=code,
    )
    _save(context, data)

    if is_private:
        return await update.message.reply_text(
            status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )

    # The matching code was sent in the lottery group. Keep the detailed card
    # in the bot private chat and leave only a minimal confirmation in-group.
    try:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )
        return await update.message.reply_text("✅ 已收到参与口令，报名进度已更新，请到机器人私聊查看。")
    except Exception:
        return await update.message.reply_text(
            "✅ 已收到参与口令。请先点击抽奖消息的“参与抽奖”进入机器人私聊查看报名进度。"
        )


def register_event_lottery_handlers(app):
    # Run before the generic verification /start handler so eventlot deep links
    # open the private participation card instead of the ordinary welcome text.
    app.add_handler(
        CommandHandler("start", event_lottery_start, filters=filters.ChatType.PRIVATE),
        group=-100,
    )
    app.add_handler(CallbackQueryHandler(event_lottery_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), event_lottery_text), group=18)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), event_lottery_message_counter),
        group=-19,
    )
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), event_lottery_text), group=-18)
    app.job_queue.run_repeating(event_lottery_tick, interval=60, first=60, name="event_lottery_tick")
