"""Scheduled points auctions for a single group.

Bids are held by deducting (freezing) their points immediately.  A bidder may
raise their own bid; only the difference from their previous highest bid is
frozen.  When the auction closes, every non-winning frozen bid is refunded and
the winner's final bid remains deducted as the purchase price.
"""
import html
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from forward.message_forward import build_message_payload
from info.economy import change_points, get_points
from utils import bot_datetime_from_timestamp, bot_now, get_bot_path, get_bot_timezone, get_group_whitelist, load_json, save_json

CALLBACK_PREFIX = "auction"
FILE_NAME = "data/points_auctions.json"
EDITOR_KEY = "points_auction_editor"
BID_KEY = "points_auction_bid"
MAX_DESCRIPTION_LENGTH = 1800
MAX_BID = 9_999_999


def _file(context: ContextTypes.DEFAULT_TYPE) -> str:
    return get_bot_path(context, FILE_NAME)


def _load(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = load_json(_file(context))
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("auctions"), dict):
        data["auctions"] = {}
    return data


def _save(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    save_json(_file(context), data)


def _record(data: dict, auction_id: str) -> Optional[dict]:
    record = data.get("auctions", {}).get(str(auction_id))
    return record if isinstance(record, dict) else None


def _new_auction(chat_id: int, owner_id: int) -> dict:
    now = int(time.time())
    return {
        "id": uuid.uuid4().hex[:12],
        "chat_id": int(chat_id),
        "owner_id": int(owner_id),
        "status": "draft",  # draft / scheduled / open / ended / cancelled
        "name": "",
        "description": "",
        "publish_at": 0,
        "end_at": 0,
        "starting_bid": 1,
        # Charged once on the first successful bid. It is not part of the
        # refundable frozen bid, and applies to both winner and non-winners.
        "entry_cost": 0,
        "stop_bidding_before_min": 0,
        "cover": None,
        "bids": {},  # user_id -> bidder info and their currently frozen high bid
        "post_messages": [],
        "winner": None,
        "published_at": 0,
        "ended_at": 0,
        "created_at": now,
        "updated_at": now,
    }


def _status_label(record: dict) -> str:
    return {
        "draft": "📝 草稿",
        "scheduled": "🕒 待发布",
        "open": "🟢 竞拍中",
        "ended": "🏆 已结束",
        "cancelled": "🚫 已取消",
    }.get(str(record.get("status") or ""), "未知")


def _timestamp_text(ts: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    return bot_datetime_from_timestamp(int(ts), context).strftime("%Y-%m-%d %H:%M") if ts else "未设置"


def _records_for_chat(data: dict, chat_id: int) -> list[dict]:
    items = [
        item for item in data.get("auctions", {}).values()
        if isinstance(item, dict) and int(item.get("chat_id", 0) or 0) == int(chat_id)
    ]
    return sorted(items, key=lambda item: int(item.get("created_at", 0) or 0), reverse=True)


async def _can_manage(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return str(getattr(member, "status", "")).lower() in {"creator", "owner", "administrator"}


def _points_enabled(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    cfg = get_group_whitelist(context).get(str(chat_id), {})
    return isinstance(cfg, dict) and bool(cfg.get("points_enabled", False))


def _bids(record: dict) -> list[dict]:
    values = record.get("bids") if isinstance(record.get("bids"), dict) else {}
    return sorted(
        [item for item in values.values() if isinstance(item, dict)],
        key=lambda item: (-int(item.get("amount", 0) or 0), int(item.get("first_bid_at", 0) or 0)),
    )


def _leading_bid(record: dict) -> Optional[dict]:
    values = _bids(record)
    return values[0] if values else None


def _minimum_next_bid(record: dict) -> int:
    leader = _leading_bid(record)
    start = max(1, int(record.get("starting_bid", 1) or 1))
    return start if not leader else int(leader.get("amount", 0) or 0) + 1


def _bidder_display(entry: dict) -> str:
    name = html.escape(str(entry.get("name") or "匿名用户")[:32])
    user_id = entry.get("user_id")
    try:
        return f'<a href="tg://user?id={int(user_id)}">{name}</a>'
    except (TypeError, ValueError):
        return name


def _auction_text(record: dict, context: ContextTypes.DEFAULT_TYPE, *, include_bids: bool = True) -> str:
    cover_exists = isinstance(record.get("cover"), dict)
    description_limit = 420 if cover_exists else MAX_DESCRIPTION_LENGTH
    description = html.escape(str(record.get("description") or "未填写")[:description_limit])
    leader = _leading_bid(record)
    now = int(time.time())
    cutoff = int(record.get("end_at", 0) or 0) - int(record.get("stop_bidding_before_min", 0) or 0) * 60
    if record.get("status") == "open" and cutoff and now >= cutoff:
        bid_state = "🔒 已停止出价，等待结束"
    elif leader:
        bid_state = f"当前最高：{int(leader.get('amount', 0) or 0)} 积分（{_bidder_display(leader)}）"
    else:
        bid_state = "当前暂无出价"
    lines = [
        f"💎 <b>{html.escape(str(record.get('name') or '未命名拍卖'))}</b>",
        f"状态：{_status_label(record)}",
        "",
        f"📜 {description}",
        "",
        f"📅 发布时间：{_timestamp_text(int(record.get('publish_at', 0) or 0), context) if record.get('publish_at') else '立即发布'}",
        f"⏰ 拍卖结束：{_timestamp_text(int(record.get('end_at', 0) or 0), context)}（{get_bot_timezone(context)}）",
        f"💰 起拍价：{int(record.get('starting_bid', 1) or 1)} 积分",
        f"🎫 参与消耗：{int(record.get('entry_cost', 0) or 0)} 积分（首次有效出价时扣除）",
        f"🔒 停止出价：结束前 {int(record.get('stop_bidding_before_min', 0) or 0)} 分钟",
        "",
        f"📊 {bid_state}",
    ]
    if include_bids:
        bids = _bids(record)
        lines.extend(["", f"📋 <b>出价表</b>（{len(bids)} 人）"])
        if not bids:
            lines.append("暂无出价")
        else:
            for index, bid in enumerate(bids[:8], start=1):
                lines.append(f"{index}. {_bidder_display(bid)}：<b>{int(bid.get('amount', 0) or 0)}</b> 积分")
            if len(bids) > 8:
                lines.append(f"… 另有 {len(bids) - 8} 人出价")
    return "\n".join(lines)


def _dashboard_text(records: list[dict]) -> str:
    lines = ["💎 积分拍卖", "", "出价会冻结积分；结束后自动退还未中标用户的冻结积分。"]
    if not records:
        lines.append("\n暂无拍卖记录。")
    else:
        lines.append("")
        for record in records[:8]:
            lines.append(f"{_status_label(record)} · {record.get('name') or '未命名拍卖'}")
    return "\n".join(lines)


def _dashboard_keyboard(chat_id: int, records: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{_status_label(record)} {str(record.get('name') or '未命名拍卖')[:32]}",
            callback_data=f"{CALLBACK_PREFIX}:view:{record['id']}",
        )]
        for record in records[:8]
    ]
    rows.append([InlineKeyboardButton("➕ 创建积分拍卖", callback_data=f"{CALLBACK_PREFIX}:create:{chat_id}")])
    rows.append([InlineKeyboardButton("⬅️ 返回群设置", callback_data=f"gcfg:open:{chat_id}")])
    return InlineKeyboardMarkup(rows)


def _editor_text(record: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    return "\n".join([
        "💎 拍卖编辑",
        f"状态：{_status_label(record)}",
        f"名称：{record.get('name') or '未设置'}",
        f"描述：{'已设置' if record.get('description') else '未设置'}",
        f"发布时间：{'立即发布' if not record.get('publish_at') else _timestamp_text(int(record['publish_at']), context)}",
        f"结束时间：{_timestamp_text(int(record.get('end_at', 0) or 0), context)}",
        f"起拍价：{int(record.get('starting_bid', 1) or 1)} 积分",
        f"参与消耗：{int(record.get('entry_cost', 0) or 0)} 积分",
        f"提前停止出价：{int(record.get('stop_bidding_before_min', 0) or 0)} 分钟",
        f"封面：{'已设置' if isinstance(record.get('cover'), dict) else '未设置'}",
    ])


def _editor_keyboard(record: dict) -> InlineKeyboardMarkup:
    aid = record["id"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ 名称", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:name"),
            InlineKeyboardButton("📜 描述", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:description"),
        ],
        [
            InlineKeyboardButton("📅 发布时间", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:publish_at"),
            InlineKeyboardButton("⏰ 结束时间", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:end_at"),
        ],
        [
            InlineKeyboardButton("💰 起拍价", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:starting_bid"),
            InlineKeyboardButton("🎫 参与消耗", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:entry_cost"),
        ],
        [
            InlineKeyboardButton("🔒 停止出价时间", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:stop_before"),
            InlineKeyboardButton("🖼 拍卖封面", callback_data=f"{CALLBACK_PREFIX}:field:{aid}:cover"),
        ],
        [
            InlineKeyboardButton("👁 预览", callback_data=f"{CALLBACK_PREFIX}:preview:{aid}"),
            InlineKeyboardButton("✅ 完成发布", callback_data=f"{CALLBACK_PREFIX}:publish:{aid}"),
        ],
        [InlineKeyboardButton("❌ 放弃草稿", callback_data=f"{CALLBACK_PREFIX}:discard:{aid}")],
        [InlineKeyboardButton("⬅️ 返回拍卖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")],
    ])


def _time_prompt(field: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    now = bot_now(context)
    example = (now + (timedelta(minutes=5) if field == "publish_at" else timedelta(hours=1))).strftime("%Y-%m-%d %H:%M")
    if field == "publish_at":
        return f"请输入发布时间，或输入「立即」。\n当前时间（{get_bot_timezone(context)}）：{now:%Y-%m-%d %H:%M}\n示例：{example}"
    return f"请输入未来拍卖结束时间，格式 YYYY-MM-DD HH:MM。\n当前时间（{get_bot_timezone(context)}）：{now:%Y-%m-%d %H:%M}\n示例：{example}"


def _parse_time(text: str, context: ContextTypes.DEFAULT_TYPE, *, allow_immediate: bool = False) -> Optional[int]:
    text = str(text or "").strip()
    if allow_immediate and text in {"立即", "现在"}:
        return 0
    try:
        result = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=bot_now(context).tzinfo)
    except Exception:
        return None
    timestamp = int(result.timestamp())
    return timestamp if timestamp > int(time.time()) else None


async def _send_post(context: ContextTypes.DEFAULT_TYPE, record: dict):
    markup = await _auction_join_markup(context, record)
    text = _auction_text(record, context)
    cover = record.get("cover")
    if not isinstance(cover, dict):
        return await context.bot.send_message(record["chat_id"], text, parse_mode="HTML", reply_markup=markup)
    common = {"chat_id": record["chat_id"], "caption": text[:1024], "parse_mode": "HTML", "reply_markup": markup}
    if cover.get("type") == "photo":
        return await context.bot.send_photo(photo=cover.get("file_id"), **common)
    if cover.get("type") == "video":
        return await context.bot.send_video(video=cover.get("file_id"), **common)
    raise ValueError("拍卖封面仅支持图片或视频。")


async def _auction_deep_link(context: ContextTypes.DEFAULT_TYPE, auction_id: str) -> str:
    username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not username:
        try:
            username = str((await context.bot.get_me()).username or "").strip().lstrip("@")
        except Exception:
            return ""
    return f"https://t.me/{username}?start=auction_{auction_id}" if username else ""


async def _auction_join_markup(context: ContextTypes.DEFAULT_TYPE, record: dict) -> InlineKeyboardMarkup:
    link = await _auction_deep_link(context, record["id"])
    if link:
        return InlineKeyboardMarkup([[InlineKeyboardButton("💰 点击出价", url=link)]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("💰 点击出价", callback_data=f"{CALLBACK_PREFIX}:open:{record['id']}")]])


async def _refresh_posts(context: ContextTypes.DEFAULT_TYPE, record: dict) -> None:
    markup = await _auction_join_markup(context, record) if record.get("status") == "open" else None
    text = _auction_text(record, context)
    valid = []
    for post in record.get("post_messages", []) or []:
        if not isinstance(post, dict):
            continue
        try:
            if post.get("cover_type") in {"photo", "video"}:
                await context.bot.edit_message_caption(chat_id=post["chat_id"], message_id=post["message_id"], caption=text[:1024], parse_mode="HTML", reply_markup=markup)
            else:
                await context.bot.edit_message_text(chat_id=post["chat_id"], message_id=post["message_id"], text=text, parse_mode="HTML", reply_markup=markup)
            valid.append(post)
        except Exception as exc:
            # Telegram rejects no-op edits; retain the message because it can be
            # refreshed after the next successful bid/state change.
            if "message is not modified" in str(exc).lower():
                valid.append(post)
            else:
                print(f"刷新拍卖消息失败 auction={record.get('id')}: {exc}")
                valid.append(post)
    record["post_messages"] = valid


async def _publish(record: dict, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        message = await _send_post(context, record)
    except Exception as exc:
        print(f"发布积分拍卖失败 auction={record.get('id')}: {exc}")
        return False
    record["status"] = "open"
    record["published_at"] = int(time.time())
    record["post_messages"] = [{
        "chat_id": int(record["chat_id"]),
        "message_id": int(message.message_id),
        "cover_type": str((record.get("cover") or {}).get("type") or ""),
    }]
    return True


def _settlement_text(record: dict, winner: Optional[dict], refunded: int, *, cancelled: bool, caption: bool = False) -> str:
    """Build the result announcement with the original auction description."""
    name_limit = 32 if caption else 80
    description_limit = 80 if caption else 1200
    name = html.escape(str(record.get("name") or "拍卖")[:name_limit])
    description = html.escape(str(record.get("description") or "未填写")[:description_limit])
    lines = [
        f"{'🚫' if cancelled else '🏆'} <b>{name}</b> {'已取消' if cancelled else '拍卖结束'}",
        "",
        "📜 <b>拍卖描述</b>",
        description,
        "",
    ]
    if cancelled:
        lines.append(f"已退还全部冻结出价：{refunded} 积分。")
    elif winner:
        lines.extend([
            f"中标者：{_bidder_display(winner)}",
            f"中标价：<b>{int(winner.get('amount', 0) or 0)}</b> 积分",
            f"已退还其他参与者冻结积分：{refunded} 积分。",
        ])
    else:
        lines.append("暂无有效出价。")
    return "\n".join(lines)


async def _send_settlement_announcement(
    context: ContextTypes.DEFAULT_TYPE,
    record: dict,
    result: str,
    *,
    detail_text: str = "",
) -> None:
    """Publish the settlement using the configured cover when available.

    Captions are limited to 1024 characters. For a long auction description,
    the cover carries a compact result card and the complete description is
    sent as the immediately following detail message.
    """
    cover = record.get("cover")
    try:
        sent_cover = False
        if isinstance(cover, dict) and cover.get("type") == "photo" and cover.get("file_id"):
            await context.bot.send_photo(
                chat_id=record["chat_id"], photo=cover["file_id"], caption=result,
                parse_mode="HTML",
            )
            sent_cover = True
        elif isinstance(cover, dict) and cover.get("type") == "video" and cover.get("file_id"):
            await context.bot.send_video(
                chat_id=record["chat_id"], video=cover["file_id"], caption=result,
                parse_mode="HTML",
            )
            sent_cover = True
        else:
            await context.bot.send_message(record["chat_id"], result, parse_mode="HTML")
        if sent_cover and detail_text and detail_text != result:
            await context.bot.send_message(record["chat_id"], detail_text, parse_mode="HTML")
    except Exception as exc:
        print(f"发送拍卖结算公告失败 auction={record.get('id')}: {exc}")


async def _settle(record: dict, context: ContextTypes.DEFAULT_TYPE, *, cancelled: bool = False) -> None:
    bids = _bids(record)
    winner = None if cancelled else (_leading_bid(record) if bids else None)
    refunded = 0
    for bid in bids:
        amount = max(0, int(bid.get("amount", 0) or 0))
        is_winner = winner is not None and str(bid.get("user_id")) == str(winner.get("user_id"))
        if amount and not is_winner:
            change_points(record["chat_id"], bid.get("user_id"), amount, reason=f"拍卖退款：{record.get('name') or '拍卖'}")
            refunded += amount
    record["winner"] = None if cancelled or not winner else {
        "user_id": winner.get("user_id"), "name": winner.get("name"), "username": winner.get("username"), "amount": int(winner.get("amount", 0) or 0),
    }
    record["status"] = "cancelled" if cancelled else "ended"
    record["ended_at"] = int(time.time())
    await _refresh_posts(context, record)
    # Captions have Telegram's 1024-character limit, so use a compact
    # description only when the configured cover is attached.
    has_cover = isinstance(record.get("cover"), dict)
    result = _settlement_text(
        record, winner, refunded, cancelled=cancelled, caption=has_cover
    )
    detail_text = _settlement_text(record, winner, refunded, cancelled=cancelled)
    await _send_settlement_announcement(
        context, record, result, detail_text=detail_text if has_cover else ""
    )


async def auction_tick(context: ContextTypes.DEFAULT_TYPE):
    data = _load(context)
    changed = False
    now = int(time.time())
    for record in data.get("auctions", {}).values():
        if not isinstance(record, dict):
            continue
        if record.get("status") == "scheduled" and int(record.get("publish_at", 0) or 0) <= now:
            changed = await _publish(record, context) or changed
        if record.get("status") == "open" and int(record.get("end_at", 0) or 0) <= now:
            await _settle(record, context)
            changed = True
    if changed:
        _save(context, data)


async def open_auction_creator(query, context: ContextTypes.DEFAULT_TYPE, chat_id_str: str, user_id: int):
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        return await query.answer("群ID无效。", show_alert=True)
    if not await _can_manage(context, user_id, chat_id):
        return await query.answer("你不是该群管理员，无法管理拍卖。", show_alert=True)
    if not _points_enabled(context, chat_id):
        return await query.answer("请先在群设置中开启积分系统。", show_alert=True)
    records = _records_for_chat(_load(context), chat_id)
    await query.answer()
    return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(chat_id, records))


async def _open_bid_card(query, context: ContextTypes.DEFAULT_TYPE, record: dict, user) -> None:
    bids = record.get("bids") if isinstance(record.get("bids"), dict) else {}
    mine = bids.get(str(user.id), {}) if isinstance(bids, dict) else {}
    status = record.get("status")
    cutoff = int(record.get("end_at", 0) or 0) - int(record.get("stop_bidding_before_min", 0) or 0) * 60
    can_bid = status == "open" and int(time.time()) < cutoff
    lines = [
        "💎 <b>积分拍卖出价</b>", f"拍卖：{html.escape(str(record.get('name') or '未命名拍卖'))}",
        f"结束：{_timestamp_text(int(record.get('end_at', 0) or 0), context)}", f"当前最低可出价：{_minimum_next_bid(record)} 积分",
        f"你的当前最高出价：{int(mine.get('amount', 0) or 0)} 积分", f"你的当前积分：{get_points(record['chat_id'], user.id)}",
        "", "每次改价仅补扣与自己最高出价的差额；未中标者的冻结出价会在结束后自动退还。",
    ]
    rows = []
    if can_bid:
        rows.append([InlineKeyboardButton("✍️ 输入出价", callback_data=f"{CALLBACK_PREFIX}:bid_input:{record['id']}")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data=f"{CALLBACK_PREFIX}:open:{record['id']}")])
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def auction_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return
    parameter = str(context.args[0] or "") if context.args else ""
    if not parameter.startswith("auction_"):
        return
    record = _record(_load(context), parameter.removeprefix("auction_"))
    if not record:
        await update.message.reply_text("❌ 该拍卖不存在。")
        raise ApplicationHandlerStop
    # Send a fresh card so it works from a /start deep link, not an existing callback message.
    # Reuse the card builder with an instance attribute so ``reply_text``
    # remains bound to this incoming private message.
    proxy = type("AuctionStartReply", (), {})()
    proxy.edit_message_text = update.message.reply_text
    await _open_bid_card(proxy, context, record, update.effective_user)
    raise ApplicationHandlerStop


async def _handle_editor_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    stage = context.user_data.get(EDITOR_KEY)
    if not isinstance(stage, dict) or not update.message or not update.effective_user or update.effective_chat.type != "private":
        return False
    data = _load(context)
    record = _record(data, stage.get("id"))
    if not record or not await _can_manage(context, update.effective_user.id, int(record["chat_id"])):
        context.user_data.pop(EDITOR_KEY, None)
        await update.message.reply_text("❌ 拍卖不存在或你没有管理权限。")
        return True
    field = str(stage.get("field") or "")
    text = (update.message.text or "").strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(EDITOR_KEY, None)
        await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
        return True
    try:
        if field == "name":
            if not text:
                raise ValueError("拍卖名称不能为空。")
            record["name"] = text[:80]
        elif field == "description":
            if not text:
                raise ValueError("拍卖描述不能为空。")
            record["description"] = text[:MAX_DESCRIPTION_LENGTH]
        elif field == "publish_at":
            value = _parse_time(text, context, allow_immediate=True)
            if value is None:
                raise ValueError("请输入「立即」或未来时间 YYYY-MM-DD HH:MM。")
            record["publish_at"] = value
        elif field == "end_at":
            value = _parse_time(text, context)
            if value is None:
                raise ValueError("请输入未来结束时间 YYYY-MM-DD HH:MM。")
            record["end_at"] = value
        elif field in {"starting_bid", "entry_cost", "stop_before"}:
            if not text.isdigit():
                raise ValueError("请输入非负整数。")
            value = int(text)
            if value > MAX_BID:
                raise ValueError(f"数值不能超过 {MAX_BID}。")
            if field == "starting_bid" and value < 1:
                raise ValueError("起拍价至少为 1。")
            record[{"stop_before": "stop_bidding_before_min"}.get(field, field)] = value
        elif field == "cover":
            if text in {"清空", "关闭"}:
                record["cover"] = None
            else:
                cover = build_message_payload(update.message)
                if cover.get("type") not in {"photo", "video"}:
                    raise ValueError("拍卖封面仅支持图片或视频；发送「清空」可移除。")
                record["cover"] = cover
        else:
            raise ValueError("未知拍卖设置项。")
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
        return True
    record["updated_at"] = int(time.time())
    _save(context, data)
    context.user_data.pop(EDITOR_KEY, None)
    await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    return True


async def _handle_bid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    stage = context.user_data.get(BID_KEY)
    if not isinstance(stage, dict) or not update.message or not update.effective_user or update.effective_chat.type != "private":
        return False
    text = (update.message.text or "").strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(BID_KEY, None)
        await update.message.reply_text("✅ 已取消出价。")
        return True
    if not text.isdigit():
        await update.message.reply_text("❗ 请输入整数积分出价，例如：100")
        return True
    amount = int(text)
    data = _load(context)
    record = _record(data, stage.get("id"))
    user = update.effective_user
    if not record or record.get("status") != "open":
        context.user_data.pop(BID_KEY, None)
        await update.message.reply_text("❌ 该拍卖未开始或已经结束。")
        return True
    now = int(time.time())
    cutoff = int(record.get("end_at", 0) or 0) - int(record.get("stop_bidding_before_min", 0) or 0) * 60
    if now >= cutoff:
        context.user_data.pop(BID_KEY, None)
        await update.message.reply_text("❌ 已到停止出价时间。")
        return True
    minimum = _minimum_next_bid(record)
    bids = record.setdefault("bids", {})
    previous = bids.get(str(user.id), {}) if isinstance(bids, dict) else {}
    previous_amount = int(previous.get("amount", 0) or 0)
    if amount < minimum or amount <= previous_amount:
        await update.message.reply_text(f"❗ 当前最低可出价为 {minimum} 积分；你的新出价必须高于自己的 {previous_amount} 积分。")
        return True
    first_bid = not bool(previous)
    entry_cost = int(record.get("entry_cost", 0) or 0) if first_bid else 0
    extra_freeze = amount - previous_amount
    balance = get_points(record["chat_id"], user.id)
    required = extra_freeze + entry_cost
    if balance < required:
        await update.message.reply_text(f"❗ 积分不足：本次还需冻结 {extra_freeze} 积分" + (f"，另需参与消耗 {entry_cost} 积分" if entry_cost else "") + f"；当前仅有 {balance} 积分。")
        return True
    if entry_cost:
        change_points(record["chat_id"], user.id, -entry_cost, reason=f"拍卖参与消耗：{record.get('name') or '拍卖'}", name=user.full_name, telegram_username=user.username)
    change_points(record["chat_id"], user.id, -extra_freeze, reason=f"拍卖冻结出价：{record.get('name') or '拍卖'}", name=user.full_name, telegram_username=user.username)
    bids[str(user.id)] = {
        "user_id": user.id, "name": user.full_name, "username": user.username,
        "amount": amount, "first_bid_at": int(previous.get("first_bid_at", now) or now), "updated_at": now,
        "entry_cost": int(previous.get("entry_cost", 0) or 0) + entry_cost,
    }
    record["updated_at"] = now
    _save(context, data)
    context.user_data.pop(BID_KEY, None)
    await _refresh_posts(context, record)
    _save(context, data)
    await update.message.reply_text(f"✅ 出价成功：{amount} 积分。已冻结本次新增 {extra_freeze} 积分。")
    return True


async def auction_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_editor_input(update, context):
        return
    if await _handle_bid_input(update, context):
        return


async def auction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    value = parts[2] if len(parts) >= 3 else ""
    data = _load(context)
    if action in {"dashboard", "create"}:
        try:
            chat_id = int(value)
        except (TypeError, ValueError):
            return await query.answer("群ID无效。", show_alert=True)
        if not await _can_manage(context, query.from_user.id, chat_id):
            return await query.answer("无管理权限。", show_alert=True)
        if action == "dashboard":
            await query.answer()
            records = _records_for_chat(data, chat_id)
            return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(chat_id, records))
        if not _points_enabled(context, chat_id):
            return await query.answer("请先在群设置中开启积分系统。", show_alert=True)
        record = _new_auction(chat_id, query.from_user.id)
        data["auctions"][record["id"]] = record
        _save(context, data)
        await query.answer("已创建拍卖草稿。")
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))

    record = _record(data, value)
    if not record:
        return await query.answer("拍卖不存在或已删除。", show_alert=True)

    if action == "open":
        if not update.effective_chat or update.effective_chat.type != "private":
            return await query.answer("请点击拍卖消息按钮，进入机器人私聊后出价。", show_alert=True)
        await query.answer()
        return await _open_bid_card(query, context, record, query.from_user)

    if action == "view":
        rows = []
        if record.get("status") in {"draft", "scheduled"}:
            rows.append([InlineKeyboardButton("✏️ 编辑拍卖", callback_data=f"{CALLBACK_PREFIX}:edit:{record['id']}")])
            rows.append([InlineKeyboardButton("🗑 删除草稿", callback_data=f"{CALLBACK_PREFIX}:discard:{record['id']}")])
        if record.get("status") == "open":
            rows.append([InlineKeyboardButton("🚫 取消拍卖并退款", callback_data=f"{CALLBACK_PREFIX}:cancel:{record['id']}")])
        rows.append([InlineKeyboardButton("⬅️ 返回拍卖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")])
        await query.answer()
        return await query.edit_message_text(_auction_text(record, context), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    if action in {"edit", "discard", "cancel", "field", "publish", "preview"}:
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)

    if action == "edit":
        await query.answer()
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "discard":
        if record.get("status") not in {"draft", "scheduled"}:
            return await query.answer("进行中的拍卖请使用取消并退款。", show_alert=True)
        data["auctions"].pop(record["id"], None)
        _save(context, data)
        records = _records_for_chat(data, int(record["chat_id"]))
        await query.answer("已删除拍卖草稿。")
        return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(int(record["chat_id"]), records))
    if action == "cancel":
        if record.get("status") != "open":
            return await query.answer("仅可取消进行中的拍卖。", show_alert=True)
        await _settle(record, context, cancelled=True)
        _save(context, data)
        await query.answer("拍卖已取消，冻结出价已退还。")
        return await query.edit_message_text(_auction_text(record, context), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回拍卖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")]]))
    if action == "field":
        if record.get("status") not in {"draft", "scheduled"}:
            return await query.answer("拍卖已发布，不能再修改设置。", show_alert=True)
        field = parts[3] if len(parts) >= 4 else ""
        prompts = {
            "name": "请输入拍卖名称。", "description": "请输入拍卖描述。", "publish_at": _time_prompt("publish_at", context),
            "end_at": _time_prompt("end_at", context), "starting_bid": "请输入起拍价（至少 1 积分）。",
            "entry_cost": "请输入参与拍卖消耗的积分（可填 0）。首次有效出价时扣除。",
            "stop_before": "请输入结束前多少分钟停止出价（可填 0）。", "cover": "请发送拍卖封面图片或视频；发送「清空」可移除。",
        }
        context.user_data[EDITOR_KEY] = {"id": record["id"], "field": field}
        await query.answer()
        return await query.edit_message_text(prompts.get(field, "请输入设置内容。"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{record['id']}")]]))
    if action == "preview":
        await query.answer()
        try:
            return await _send_post(context, {**record, "chat_id": query.message.chat_id, "status": "open"})
        except Exception as exc:
            return await query.message.reply_text(f"❌ 无法预览：{exc}")
    if action == "publish":
        if not record.get("name") or not record.get("end_at"):
            return await query.answer("请至少设置名称和拍卖结束时间。", show_alert=True)
        now = int(time.time())
        if int(record["end_at"]) <= now:
            return await query.answer("拍卖结束时间必须在未来。", show_alert=True)
        publish_time = int(record.get("publish_at", 0) or now)
        if publish_time >= int(record["end_at"]):
            return await query.answer("发布时间必须早于拍卖结束时间。", show_alert=True)
        stop_before = int(record.get("stop_bidding_before_min", 0) or 0) * 60
        if publish_time + stop_before >= int(record["end_at"]):
            return await query.answer("停止出价时间不能早于或等于发布时间。", show_alert=True)
        if record.get("status") not in {"draft", "scheduled"}:
            return await query.answer("该拍卖已发布。", show_alert=True)
        if record.get("publish_at"):
            record["status"] = "scheduled"
            _save(context, data)
            await query.answer("已安排定时发布。")
        else:
            if not await _publish(record, context):
                return await query.answer("发布失败，请确认机器人有群内发言权限。", show_alert=True)
            _save(context, data)
            await query.answer("已立即发布。")
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "bid_input":
        if record.get("status") != "open":
            return await query.answer("拍卖尚未开始或已结束。", show_alert=True)
        context.user_data[BID_KEY] = {"id": record["id"]}
        await query.answer()
        return await query.edit_message_text("请输入你的总出价（整数积分）。\n重复出价只会冻结比你当前最高出价多出的积分。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回拍卖", callback_data=f"{CALLBACK_PREFIX}:open:{record['id']}")]]))


def register_points_auction_handlers(app):
    app.add_handler(CommandHandler("start", auction_start, filters=filters.ChatType.PRIVATE), group=-99)
    app.add_handler(CallbackQueryHandler(auction_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), auction_text), group=17)
    app.job_queue.run_repeating(auction_tick, interval=60, first=60, name="points_auction_tick")
