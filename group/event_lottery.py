"""Configurable scheduled group-event lotteries.

The feature is intentionally separate from the existing points/talk lotteries:
these events are published posts with explicit prizes, entry requirements and a
scheduled draw time.
"""
import random
import time
import uuid
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from forward.message_forward import build_message_payload, send_message_payload
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
        target_password = str(item.get("password") or "").strip()
        if target_password:
            lines.append(f"└  🔑 发送口令﹝{target_password}﹞")
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
        [InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")],
    ])


def _targets_keyboard(record: dict, kind: str) -> InlineKeyboardMarkup:
    lid = record["id"]
    values = record.get("follow_targets" if kind == "follow" else "sync_targets", [])
    rows = []
    if kind == "follow":
        for index, item in enumerate(values):
            if isinstance(item, dict):
                rows.append([InlineKeyboardButton(
                    f"🗑 {item.get('target')}", callback_data=f"{CALLBACK_PREFIX}:target_delete:{lid}:{kind}:{index}"
                )])
    else:
        for index, value in enumerate(values):
            rows.append([InlineKeyboardButton(
                f"🗑 {value}", callback_data=f"{CALLBACK_PREFIX}:target_delete:{lid}:{kind}:{index}"
            )])
    label = "关注频道/群组" if kind == "follow" else "同步位置"
    rows.append([InlineKeyboardButton("➕ 添加", callback_data=f"{CALLBACK_PREFIX}:target_add:{lid}:{kind}")])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


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


async def _send_lottery_post(context: ContextTypes.DEFAULT_TYPE, record: dict) -> int:
    text = _format_lottery(record, context)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎫 参加抽奖", callback_data=f"{CALLBACK_PREFIX}:join:{record['id']}")]])
    targets = [str(record["chat_id"])] + [str(item) for item in record.get("sync_targets", [])]
    sent = 0
    for target in dict.fromkeys(targets):
        try:
            if isinstance(record.get("cover"), dict):
                await send_message_payload(context.bot, chat_id=target, payload=record["cover"])
            await context.bot.send_message(chat_id=target, text=text, parse_mode="HTML", reply_markup=markup)
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
            return True
    return False


async def _draw_if_due(context: ContextTypes.DEFAULT_TYPE, record: dict) -> bool:
    if record.get("status") != "open" or not record.get("draw_at") or int(record["draw_at"]) > int(time.time()):
        return False
    participants = list((record.get("participants") or {}).values())
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


async def _check_follow_targets(context: ContextTypes.DEFAULT_TYPE, user_id: int, record: dict) -> bool:
    for item in record.get("follow_targets", []):
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        try:
            member = await context.bot.get_chat_member(target, user_id)
        except Exception:
            return False
        if getattr(member, "status", "") in {"left", "kicked"}:
            return False
    return True


def _user_message_count(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> int:
    data = load_json(get_bot_path(context, f"data/talk_count/{chat_id}.json"))
    info = data.get(str(user_id), {}) if isinstance(data, dict) else {}
    daily = info.get("daily", {}) if isinstance(info, dict) else {}
    return sum(int(value or 0) for value in daily.values()) if isinstance(daily, dict) else 0


async def _finish_join(context: ContextTypes.DEFAULT_TYPE, user, record: dict) -> tuple[bool, str]:
    req = record.get("requirements", {})
    if req.get("username") and not getattr(user, "username", None):
        return False, "需要先设置 Telegram 用户名。"
    if _user_message_count(context, int(record["chat_id"]), user.id) <= int(req.get("min_messages", 0) or 0):
        return False, "发言条数未达到要求。"
    if not await _check_follow_targets(context, user.id, record):
        return False, "请先关注全部要求的频道或群组。"
    record.setdefault("participants", {})[str(user.id)] = {"user_id": user.id, "name": user.full_name, "username": user.username}
    return True, "✅ 已成功参加抽奖。"


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

    if action == "clone":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        clone = _new_lottery(int(record["chat_id"]), query.from_user.id)
        clone["name"] = (str(record.get("name") or "未命名抽奖") + "（副本）")[:80]
        clone["description"] = str(record.get("description") or "")
        clone["draw_at"] = 0
        clone["prizes"] = [dict(item) for item in record.get("prizes", []) if isinstance(item, dict)]
        clone["requirements"] = dict(record.get("requirements") or {})
        clone["follow_targets"] = [dict(item) for item in record.get("follow_targets", []) if isinstance(item, dict)]
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
        context.user_data[DRAFT_KEY] = {"id": lottery_id, "field": field}
        prompts = {
            "name": "请输入抽奖名称。",
            "description": "请输入抽奖说明。",
            "publish_at": "请输入发布时间：发送“立即”立即发布，或输入未来时间 YYYY-MM-DD HH:MM。",
            "draw_at": "请输入未来开奖时间，格式 YYYY-MM-DD HH:MM。",
            "min_messages": "请输入最低发言条数；输入 0 表示不限制。",
            "password": "请输入参与口令；发送“清空”可不要求口令。",
            "cover": "请发送抽奖封面（图片/视频），或发送“清空”不设置封面。",
        }
        await query.answer()
        return await query.edit_message_text(
            prompts.get(field, "请输入设置内容。"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lottery_id}")
            ]]),
        )
    if action == "prizes":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text("🎁 奖品设置", reply_markup=_prizes_keyboard(record))
    if action == "prize_add":
        context.user_data[DRAFT_KEY] = {"id": lottery_id, "field": "prize_add"}
        await query.answer()
        return await query.edit_message_text("请输入奖品，格式：奖品名称 | 数量\n例如：手机 | 2", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回奖品设置", callback_data=f"{CALLBACK_PREFIX}:prizes:{lottery_id}")]]))
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
        return await query.edit_message_text("📮 参与条件", reply_markup=_requirements_keyboard(record))
    if action == "req_toggle" and len(parts) >= 4:
        key = parts[3]
        if key == "username":
            record.setdefault("requirements", {})["username"] = not bool(record["requirements"].get("username"))
            _save(context, data)
        await query.answer("已更新")
        return await query.edit_message_text("📮 参与条件", reply_markup=_requirements_keyboard(record))
    if action == "targets" and len(parts) >= 4:
        kind = parts[3]
        await query.answer()
        return await query.edit_message_text(
            "📢 关注频道/群组" if kind == "follow" else "📍 同步位置",
            reply_markup=_targets_keyboard(record, kind),
        )
    if action == "target_add" and len(parts) >= 4:
        kind = parts[3]
        context.user_data[DRAFT_KEY] = {"id": lottery_id, "field": f"target_{kind}"}
        prompt = "请输入目标，格式：@频道或群组 | 可选参与口令" if kind == "follow" else "请输入同步频道或群组，例如：@navigation_channel"
        await query.answer()
        return await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lottery_id}")]]))
    if action == "target_delete" and len(parts) >= 5:
        kind, index = parts[3], int(parts[4])
        key = "follow_targets" if kind == "follow" else "sync_targets"
        values = record.get(key, [])
        if 0 <= index < len(values):
            values.pop(index)
            _save(context, data)
        await query.answer("已移除")
        return await query.edit_message_text("目标设置", reply_markup=_targets_keyboard(record, kind))
    if action == "preview":
        await query.answer()
        return await query.message.reply_text(_format_lottery(record, context), parse_mode="HTML")
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
            _save(context, data)
            await query.answer("已立即发布")
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "join":
        if record.get("status") != "open":
            return await query.answer("抽奖尚未开放或已结束。", show_alert=True)
        passwords = [str(record.get("requirements", {}).get("password") or "").strip()]
        passwords += [str(item.get("password") or "").strip() for item in record.get("follow_targets", []) if isinstance(item, dict)]
        passwords = [value for value in passwords if value]
        if passwords:
            context.user_data[JOIN_KEY] = {"id": lottery_id, "chat_id": int(record["chat_id"]), "required": passwords, "passed": []}
            return await query.answer("请在抽奖群发送参与口令。", show_alert=True)
        ok, note = await _finish_join(context, query.from_user, record)
        if ok:
            _save(context, data)
        return await query.answer(note, show_alert=True)


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
                record["cover"] = build_message_payload(update.message)
        elif field == "prize_add":
            if "|" not in text:
                raise ValueError("格式：奖品名称 | 数量，例如：手机 | 2")
            name, count_raw = [item.strip() for item in text.split("|", 1)]
            if not name or not count_raw.isdigit() or int(count_raw) <= 0:
                raise ValueError("奖品格式或数量不正确。")
            if len(record.get("prizes", [])) >= MAX_PRIZES:
                raise ValueError(f"最多只能设置 {MAX_PRIZES} 项奖品。")
            record.setdefault("prizes", []).append({"name": name[:80], "count": int(count_raw)})
        elif field == "target_follow":
            if len(record.get("follow_targets", [])) >= MAX_TARGETS:
                raise ValueError(f"最多只能设置 {MAX_TARGETS} 个关注目标。")
            target_raw, sep, password = text.partition("|")
            target = _normalize_target(target_raw)
            if not target:
                raise ValueError("请输入公开 @频道或 @群组用户名。")
            values = record.setdefault("follow_targets", [])
            if any(item.get("target") == target for item in values if isinstance(item, dict)):
                raise ValueError("该关注目标已经存在。")
            values.append({"target": target, "password": password.strip()[:80] if sep else ""})
        elif field == "target_sync":
            if len(record.get("sync_targets", [])) >= MAX_TARGETS:
                raise ValueError(f"最多只能设置 {MAX_TARGETS} 个同步位置。")
            target = _normalize_target(text)
            if not target:
                raise ValueError("请输入公开 @频道或 @群组用户名。")
            values = record.setdefault("sync_targets", [])
            if target in values:
                raise ValueError("该同步位置已经存在。")
            values.append(target)
        else:
            raise ValueError("未知抽奖设置项。")
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
        return True

    record["updated_at"] = int(time.time())
    _save(context, data)
    context.user_data.pop(DRAFT_KEY, None)
    await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    return True


async def event_lottery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_editor_input(update, context):
        return
    state = context.user_data.get(JOIN_KEY)
    if not isinstance(state, dict) or not update.effective_user or not update.effective_chat or not update.message:
        return
    if int(state.get("chat_id", 0) or 0) != int(update.effective_chat.id):
        return
    data = _load(context)
    record = _record(data, state.get("id"))
    if not record or record.get("status") != "open":
        context.user_data.pop(JOIN_KEY, None)
        return
    code = (update.message.text or "").strip()
    required = state.get("required", [])
    passed = state.setdefault("passed", [])
    if code in required and code not in passed:
        passed.append(code)
    if set(passed) != set(required):
        return await update.message.reply_text("口令正确，还需完成其他参与口令。")
    ok, note = await _finish_join(context, update.effective_user, record)
    if ok:
        _save(context, data)
        context.user_data.pop(JOIN_KEY, None)
    await update.message.reply_text(note)


def register_event_lottery_handlers(app):
    app.add_handler(CallbackQueryHandler(event_lottery_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), event_lottery_text), group=18)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), event_lottery_text), group=-18)
    app.job_queue.run_repeating(event_lottery_tick, interval=60, first=60, name="event_lottery_tick")
