# message_forward.py
# 用户私聊机器人，机器转发用户私聊信息，管理员通过回复把消息回复给用户
from functools import wraps
from typing import Optional
import time
from datetime import datetime
from html import escape
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes
import os
import asyncio


from command_router import get_matched_command, register_command
from utils import (
    BOT_OWNER_ID,
    BOT_USER_FILE,
    FORWARD_MAP_FILE,
    GROUP_LIST_FILE,
    load_json,
    safe_reply,
    save_json,
)

BATCH_SIZE = 50  # 每批发送数量，可调整
BATCH_DELAY = 1  # 每批发送延迟秒数
PRIVATE_DIALOG_STATE_KEY = "private_forward_dialog_state"
PRIVATE_DIALOG_CALLBACK_PREFIX = "pfmode"
PRIVATE_DIALOG_PAGE_SIZE = 6
SEND_USER_STAGE_KEY = "send_user_stage"
PRIVATE_FORWARD_DEBUG_FILE = os.path.join("data", "private_forward_debug.log")
PRIVATE_CONFIG_COMMANDS = {
    "群配置",
    "群设置",
    "群开关",
    "群状态",
    "群静默",
    "群验证",
    "群欢迎",
    "群广告",
    "群限频",
    "群限频条数",
    "限频条数",
    "群庄园",
    "群好友",
    "群主动说话",
    "主动说话",
    "频道配置",
    "会员订阅",
    "讲",
    "说",
    "叫",
}


def _debug_private_forward(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(PRIVATE_FORWARD_DEBUG_FILE), exist_ok=True)
        with open(PRIVATE_FORWARD_DEBUG_FILE, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def _get_owner_runtime_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    owner_id = int(get_owner_id(context))
    owner_store = context.application.user_data[owner_id]
    state = owner_store.get(PRIVATE_DIALOG_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        owner_store[PRIVATE_DIALOG_STATE_KEY] = state
    return state


def _sorted_private_users(user_data: dict) -> list[tuple[str, dict]]:
    items = list(user_data.items())
    items.sort(
        key=lambda item: (
            int(item[1].get("last_active", 0) or 0),
            int(item[1].get("join_time", 0) or 0),
        ),
        reverse=True,
    )
    return items


def _display_user_name(uid: str, info: dict) -> str:
    name = (info or {}).get("name", "") or "未命名用户"
    username = (info or {}).get("username", "") or ""
    if username:
        return f"{name} (@{username})"
    return f"{name} ({uid})"


def _build_private_dialog_text(
    context: ContextTypes.DEFAULT_TYPE, *, page: int = 1, notice: str = ""
) -> str:
    user_data = _load_user_data()
    users = _sorted_private_users(user_data)
    state = _get_owner_runtime_state(context)
    current_uid = str(state.get("current_uid") or "")
    total = len(users)
    total_pages = max(
        1, (total + PRIVATE_DIALOG_PAGE_SIZE - 1) // PRIVATE_DIALOG_PAGE_SIZE
    )
    page = max(1, min(page, total_pages))

    lines = ["🤖 双向机器人模式"]
    if notice:
        lines.extend(["", notice])

    if current_uid and current_uid in user_data:
        lines.extend(
            ["", f"当前会话：{_display_user_name(current_uid, user_data[current_uid])}"]
        )
    elif current_uid:
        lines.extend(["", f"当前会话：{current_uid}"])
    else:
        lines.extend(["", "当前会话：未选择"])

    if not users:
        lines.extend(["", "暂无私聊用户记录"])
        return "\n".join(lines)

    start = (page - 1) * PRIVATE_DIALOG_PAGE_SIZE
    end = start + PRIVATE_DIALOG_PAGE_SIZE
    lines.extend(["", f"私聊用户列表：第 {page}/{total_pages} 页"])
    for idx, (uid, info) in enumerate(users[start:end], start=start + 1):
        marker = "👉 " if uid == current_uid else ""
        lines.append(f"{idx}. {marker}{_display_user_name(uid, info)}")
    lines.append("")
    lines.append("主人直接发送消息，将自动转发给当前会话用户。")
    return "\n".join(lines)


def _build_private_dialog_keyboard(
    context: ContextTypes.DEFAULT_TYPE, *, page: int = 1
) -> InlineKeyboardMarkup:
    user_data = _load_user_data()
    users = _sorted_private_users(user_data)
    state = _get_owner_runtime_state(context)
    current_uid = str(state.get("current_uid") or "")
    total_pages = max(
        1, (len(users) + PRIVATE_DIALOG_PAGE_SIZE - 1) // PRIVATE_DIALOG_PAGE_SIZE
    )
    page = max(1, min(page, total_pages))
    start = (page - 1) * PRIVATE_DIALOG_PAGE_SIZE
    end = start + PRIVATE_DIALOG_PAGE_SIZE

    rows = []
    for uid, info in users[start:end]:
        label = _display_user_name(uid, info)
        if uid == current_uid:
            label = f"✅ {label}"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:60],
                    callback_data=f"{PRIVATE_DIALOG_CALLBACK_PREFIX}:switch:{uid}:{page}",
                )
            ]
        )

    nav_row = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton(
                "⬅️ 上一页",
                callback_data=f"{PRIVATE_DIALOG_CALLBACK_PREFIX}:page:{page-1}",
            )
        )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                "➡️ 下一页",
                callback_data=f"{PRIVATE_DIALOG_CALLBACK_PREFIX}:page:{page+1}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 刷新",
                callback_data=f"{PRIVATE_DIALOG_CALLBACK_PREFIX}:refresh:{page}",
            ),
            InlineKeyboardButton(
                "❌ 退出模式", callback_data=f"{PRIVATE_DIALOG_CALLBACK_PREFIX}:exit"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show_private_dialog_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 1,
    notice: str = "",
):
    text = _build_private_dialog_text(context, page=page, notice=notice)
    markup = _build_private_dialog_keyboard(context, page=page)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
        return

    if update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=markup,
        )


async def show_private_dialog_panel_to_owner(
    context: ContextTypes.DEFAULT_TYPE, *, page: int = 1, notice: str = ""
):
    owner_id = int(get_owner_id(context))
    await context.bot.send_message(
        chat_id=owner_id,
        text=_build_private_dialog_text(context, page=page, notice=notice),
        reply_markup=_build_private_dialog_keyboard(context, page=page),
    )


def get_owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.application.bot_data.get("owner_id", BOT_OWNER_ID)


def load_forward_map():
    data = load_json(FORWARD_MAP_FILE)
    return data if isinstance(data, dict) else {}


def save_forward_map(data):
    save_json(FORWARD_MAP_FILE, data)


def get_group_list():
    data = load_json(GROUP_LIST_FILE)
    return data if isinstance(data, dict) else {}


def _load_user_data():
    return load_json(BOT_USER_FILE) or {}


def _save_user_data(data: dict):
    save_json(BOT_USER_FILE, data)


def _resolve_target_uid(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[str]:
    if update.message and update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id
        forward_map = load_forward_map()
        uid = forward_map.get(str(reply_msg_id))
        if uid:
            return str(uid)
    if context.args:
        if str(context.args[0]).lstrip("-").isdigit():
            return str(context.args[0])
    return None


#  机器人代发
async def safe_forward_media(bot, chat_id, msg):
    caption = (msg.caption or "")[:1024]

    if msg.text:
        return await bot.send_message(chat_id, msg.text)

    if msg.photo:
        return await bot.send_photo(chat_id, msg.photo[-1].file_id, caption=caption)

    if msg.animation:
        return await bot.send_animation(chat_id, msg.animation.file_id, caption=caption)

    if msg.video:
        return await bot.send_video(chat_id, msg.video.file_id, caption=caption)

    if msg.document:
        return await bot.send_document(chat_id, msg.document.file_id, caption=caption)

    if msg.sticker:
        return await bot.send_sticker(chat_id, msg.sticker.file_id)

    if msg.voice:
        return await bot.send_voice(chat_id, msg.voice.file_id, caption=caption)

    if msg.audio:
        return await bot.send_audio(chat_id, msg.audio.file_id, caption=caption)

    if msg.location:
        return await bot.send_location(
            chat_id, msg.location.latitude, msg.location.longitude
        )

    if msg.contact:
        return await bot.send_contact(
            chat_id, msg.contact.phone_number, msg.contact.first_name
        )

    raise ValueError("不支持的消息类型")


def build_message_payload(msg) -> dict:
    if not msg:
        raise ValueError("消息为空")

    caption = (getattr(msg, "caption", None) or "")[:1024]
    text = getattr(msg, "text", None)
    if text:
        return {"type": "text", "text": text}
    if getattr(msg, "photo", None):
        return {
            "type": "photo",
            "file_id": msg.photo[-1].file_id,
            "caption": caption,
        }
    if getattr(msg, "animation", None):
        return {
            "type": "animation",
            "file_id": msg.animation.file_id,
            "caption": caption,
        }
    if getattr(msg, "video", None):
        return {
            "type": "video",
            "file_id": msg.video.file_id,
            "caption": caption,
        }
    if getattr(msg, "document", None):
        return {
            "type": "document",
            "file_id": msg.document.file_id,
            "caption": caption,
        }
    if getattr(msg, "sticker", None):
        return {"type": "sticker", "file_id": msg.sticker.file_id}
    if getattr(msg, "voice", None):
        return {
            "type": "voice",
            "file_id": msg.voice.file_id,
            "caption": caption,
        }
    if getattr(msg, "audio", None):
        return {
            "type": "audio",
            "file_id": msg.audio.file_id,
            "caption": caption,
        }
    if getattr(msg, "video_note", None):
        return {"type": "video_note", "file_id": msg.video_note.file_id}
    if getattr(msg, "location", None):
        return {
            "type": "location",
            "latitude": msg.location.latitude,
            "longitude": msg.location.longitude,
        }
    if getattr(msg, "contact", None):
        return {
            "type": "contact",
            "phone_number": msg.contact.phone_number,
            "first_name": msg.contact.first_name,
            "last_name": getattr(msg.contact, "last_name", None),
        }
    raise ValueError("不支持的消息类型")


async def send_message_payload(bot, chat_id, payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("消息载荷无效")

    payload_type = str(payload.get("type", "")).strip().lower()
    if payload_type == "text":
        return await bot.send_message(chat_id, payload.get("text", ""))
    if payload_type == "photo":
        return await bot.send_photo(
            chat_id, payload.get("file_id"), caption=payload.get("caption", "")
        )
    if payload_type == "animation":
        return await bot.send_animation(
            chat_id, payload.get("file_id"), caption=payload.get("caption", "")
        )
    if payload_type == "video":
        return await bot.send_video(
            chat_id, payload.get("file_id"), caption=payload.get("caption", "")
        )
    if payload_type == "document":
        return await bot.send_document(
            chat_id, payload.get("file_id"), caption=payload.get("caption", "")
        )
    if payload_type == "sticker":
        return await bot.send_sticker(chat_id, payload.get("file_id"))
    if payload_type == "voice":
        return await bot.send_voice(
            chat_id, payload.get("file_id"), caption=payload.get("caption", "")
        )
    if payload_type == "audio":
        return await bot.send_audio(
            chat_id, payload.get("file_id"), caption=payload.get("caption", "")
        )
    if payload_type == "video_note":
        return await bot.send_video_note(chat_id, payload.get("file_id"))
    if payload_type == "location":
        return await bot.send_location(
            chat_id, payload.get("latitude"), payload.get("longitude")
        )
    if payload_type == "contact":
        return await bot.send_contact(
            chat_id,
            payload.get("phone_number"),
            payload.get("first_name", ""),
            last_name=payload.get("last_name"),
        )
    raise ValueError("不支持的消息类型")


async def send_to_targets(bot, target_ids: list[int], src):
    for chat_id in target_ids:
        try:
            await safe_forward_media(bot, chat_id, src)

            print(f"✅ 已发送到 {chat_id}")

        except Exception as e:
            print(f"❌ 发送失败 {chat_id}: {e}")


# 用户私聊 → 转发给管理员
async def forward_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = get_owner_id(context)

    user = update.effective_user
    if not update.message or not user:
        return
    _debug_private_forward(
        f"[forward_to_owner] user_id={getattr(user, 'id', None)} "
        f"chat_id={getattr(getattr(update, 'effective_chat', None), 'id', None)} "
        f"text={getattr(update.message, 'text', None)!r}"
    )
    print(
        f"[private_forward] 收到私聊 user_id={getattr(user, 'id', None)} "
        f"chat_id={getattr(getattr(update, 'effective_chat', None), 'id', None)} "
        f"type={'text' if getattr(update.message, 'text', None) else 'media'}"
    )
    if user.id == owner_id:
        _debug_private_forward("[forward_to_owner] skip owner self message")
        print("[private_forward] 忽略：消息来自 owner 自己")
        return  # 管理员自己发的消息不转发

    user_data = _load_user_data()
    uid = str(user.id)
    if user_data.get(uid, {}).get("blocked", False):
        _debug_private_forward(f"[forward_to_owner] skip blocked uid={uid}")
        print(f"[private_forward] 忽略：用户已拉黑 uid={uid}")
        return

    # 忽略命令消息
    if update.message.text and update.message.text.startswith("/"):
        _debug_private_forward(
            f"[forward_to_owner] skip slash command text={update.message.text!r}"
        )
        print(f"[private_forward] 忽略：slash 命令 text={update.message.text!r}")
        return
    if update.message.text:
        matched = get_matched_command(update.message.text)
        if matched in PRIVATE_CONFIG_COMMANDS:
            _debug_private_forward(
                f"[forward_to_owner] skip private config cmd={matched}"
            )
            print(f"[private_forward] 忽略：命中私聊配置命令 cmd={matched}")
            return

    try:
        safe_name = escape(user.full_name or str(user.id))
        await context.bot.send_message(
            chat_id=owner_id,
            text=f'来自 <a href="tg://user?id={user.id}">{safe_name}</a> 的消息：',
            parse_mode="HTML",
        )
        _debug_private_forward(
            f"[forward_to_owner] owner notice sent owner_id={owner_id}"
        )
        print(f"[private_forward] 已发送提示消息给主人 owner_id={owner_id}")
    except Exception as e:
        _debug_private_forward(f"[forward_to_owner] owner notice failed error={e}")
        print(f"[private_forward] 提示消息发送失败，但继续转发正文: {e}")

    try:
        # 转发消息到管理员
        sent = await safe_forward_media(context.bot, owner_id, update.message)
        _debug_private_forward(
            f"[forward_to_owner] forward success owner_id={owner_id} "
            f"owner_msg_id={getattr(sent, 'message_id', None)} uid={uid}"
        )
        print(
            f"[private_forward] 已转发给主人 owner_id={owner_id} "
            f"owner_msg_id={getattr(sent, 'message_id', None)} uid={uid}"
        )
    except Exception as e:
        _debug_private_forward(f"[forward_to_owner] forward failed error={e}")
        print(f"❌ 转发消息失败: {e}")
        await safe_reply(update, context, "⚠️ 转发消息失败，请稍后重试。")
        return

    # 记录原用户 ID 以供管理员回复时查找
    forward_map = load_forward_map()
    forward_map[str(sent.message_id)] = user.id
    save_forward_map(forward_map)

    users = user_data
    users[uid] = {
        "name": user.first_name or "",
        "username": user.username or "",
        "join_time": users.get(uid, {}).get("join_time", int(time.time())),
        "last_active": int(time.time()),
        "blocked": users.get(uid, {}).get("blocked", False),
    }

    _save_user_data(users)

    state = _get_owner_runtime_state(context)
    state["enabled"] = True
    state["current_uid"] = uid
    state["page"] = 1
    try:
        await show_private_dialog_panel_to_owner(
            context,
            page=1,
            notice=f"已切换到 {users[uid].get('name') or uid} 的私聊会话",
        )
    except Exception as e:
        _debug_private_forward(f"[forward_to_owner] owner panel failed error={e}")
        print(f"[private_forward] 面板发送失败，但正文已转发: {e}")

    # await safe_reply(update, context,"✅ 已将你的消息转发给管理员，请等待回复。")


# 管理员回复转发消息 → 自动回原用户
async def reply_from_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return

    if not update.message.reply_to_message:
        await safe_reply(update, context, "请使用“回复”功能回复用户消息。")
        return

    reply_msg_id = update.message.reply_to_message.message_id
    forward_map = load_forward_map()
    target_user_id = forward_map.get(str(reply_msg_id))

    if not target_user_id:
        # await safe_reply(update, context,"找不到对应用户，可能消息过期或未记录。")
        return

    try:
        await safe_forward_media(context.bot, target_user_id, update.message)
        _debug_private_forward(
            f"[reply_from_owner] success target_user_id={target_user_id} reply_msg_id={reply_msg_id}"
        )
        print(
            f"[private_forward] owner 回复成功 target_user_id={target_user_id} "
            f"reply_msg_id={reply_msg_id}"
        )
    except Exception as e:
        _debug_private_forward(f"[reply_from_owner] failed error={e}")
        await safe_reply(update, context, f"发送失败: {e}")


async def owner_auto_forward_in_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user or update.effective_user.id != get_owner_id(context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    if not update.message or update.message.reply_to_message:
        return
    if update.message.text:
        matched = get_matched_command(update.message.text)
        if matched:
            return
    if context.user_data.get(SEND_USER_STAGE_KEY) == "typing":
        return

    state = _get_owner_runtime_state(context)
    if not state.get("enabled"):
        return

    target_uid = str(state.get("current_uid") or "")
    if not target_uid:
        await safe_reply(
            update, context, "当前没有选中的私聊用户，先用面板选择一个用户。"
        )
        raise ApplicationHandlerStop

    try:
        await safe_forward_media(context.bot, int(target_uid), update.message)
        _debug_private_forward(
            f"[dialog_mode] owner_id={update.effective_user.id} target_uid={target_uid} success"
        )
        print(
            f"[private_forward] 双向模式发送成功 owner_id={update.effective_user.id} "
            f"target_uid={target_uid}"
        )
    except Exception as e:
        _debug_private_forward(
            f"[dialog_mode] failed target_uid={target_uid} error={e}"
        )
        await safe_reply(update, context, f"发送失败: {e}")
        raise ApplicationHandlerStop

    raise ApplicationHandlerStop


@register_command("双向模式", "私聊模式", "私聊面板")
async def cmd_private_dialog_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    user_data = _load_user_data()
    if not user_data:
        return await safe_reply(update, context, "📭 当前没有私聊用户记录")

    state = _get_owner_runtime_state(context)
    current_uid = str(state.get("current_uid") or "")
    if not current_uid or current_uid not in user_data:
        state["current_uid"] = _sorted_private_users(user_data)[0][0]
    state["enabled"] = True
    page = int(state.get("page") or 1)
    await show_private_dialog_panel(update, context, page=page)


async def handle_private_dialog_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query or not query.data:
        return
    if not query.data.startswith(f"{PRIVATE_DIALOG_CALLBACK_PREFIX}:"):
        return

    await query.answer()
    if not update.effective_user or update.effective_user.id != get_owner_id(context):
        return await query.edit_message_text("⚠️ 仅机器人所有者可操作该面板")

    if not update.effective_chat or update.effective_chat.type != "private":
        return await query.edit_message_text("⚠️ 请在私聊里使用该面板")

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    state = _get_owner_runtime_state(context)
    user_data = _load_user_data()

    if action == "switch":
        uid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        if uid not in user_data:
            return await show_private_dialog_panel(
                update, context, page=page, notice="该用户记录不存在或已失效"
            )
        state["enabled"] = True
        state["current_uid"] = uid
        state["page"] = page
        return await show_private_dialog_panel(
            update,
            context,
            page=page,
            notice=f"已切换到 {_display_user_name(uid, user_data[uid])}",
        )

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        state["page"] = page
        return await show_private_dialog_panel(update, context, page=page)

    if action == "refresh":
        page = (
            int(parts[2])
            if len(parts) > 2 and parts[2].isdigit()
            else int(state.get("page") or 1)
        )
        state["page"] = page
        return await show_private_dialog_panel(
            update, context, page=page, notice="列表已刷新"
        )

    if action == "exit":
        state.clear()
        return await query.edit_message_text("✅ 已退出双向机器人模式")


@register_command("广播")
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    if not update.message.reply_to_message:
        return await safe_reply(update, context, "📌 请【回复】你要广播的内容")

    group_list = get_group_list()
    group_ids = [
        int(chat_id)
        for chat_id, cfg in group_list.items()
        if isinstance(cfg, dict) and bool(cfg.get("bot_in_group", True))
    ]
    src = update.message.reply_to_message

    await send_to_targets(context.bot, group_ids, src)
    await safe_reply(update, context, "📣 广播完成")


@register_command("用户广播")
async def cmd_user_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    if not update.message.reply_to_message:
        return await safe_reply(update, context, "📌 请回复要发送的消息")

    src = update.message.reply_to_message
    user_ids = list(load_json(BOT_USER_FILE).keys())  # 获取用户ID列表

    total_users = len(user_ids)
    success, failed = 0, 0

    for i in range(0, total_users, BATCH_SIZE):
        batch = user_ids[i : i + BATCH_SIZE]
        for uid in batch:
            try:
                await safe_forward_media(context.bot, uid, src)
                success += 1
            except Exception as e:
                print(f"发送失败 {uid}: {e}")
                failed += 1
        await asyncio.sleep(BATCH_DELAY)  # 等待，避免触发频率限制
        print(f"✅ 已发送 {min(i+BATCH_SIZE, total_users)}/{total_users} 个用户")

    await safe_reply(
        update, context, f"📣 用户广播完成\n✅ 成功: {success}\n❌ 失败: {failed}"
    )


@register_command("用户列表")
async def cmd_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    user_data = _load_user_data()
    if not user_data:
        return await safe_reply(update, context, "📭 当前没有用户记录")

    args = update.message.text.split()
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    per_page = 20

    total = len(user_data)
    pages = (total + per_page - 1) // per_page
    if page > pages:
        page = pages

    start = (page - 1) * per_page
    end = start + per_page
    items = list(user_data.items())[start:end]

    text = f"👥 用户总数：{total} | 第 {page}/{pages} 页\n\n"
    for i, (uid, info) in enumerate(items, start=start + 1):
        name = info.get("name", "")
        username = info.get("username", "")
        blocked = "（已拉黑）" if info.get("blocked", False) else ""
        text += f"{i}. {name} (@{username}){blocked}\nID: {uid}\n\n"

    if page < pages:
        text += f"➡️ 发送 /用户列表 {page+1} 查看下一页"

    await safe_reply(update, context, text)


@register_command("拉黑用户", "拉黑", "黑名单添加")
async def cmd_blacklist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    uid = _resolve_target_uid(update, context)
    if not uid:
        return await safe_reply(
            update, context, "用法：拉黑用户 用户ID\n或回复用户消息后发送 拉黑用户"
        )

    user_data = _load_user_data()
    info = user_data.get(uid, {})
    info["blocked"] = True
    info["blocked_at"] = int(time.time())
    info.setdefault("name", "")
    info.setdefault("username", "")
    user_data[uid] = info
    _save_user_data(user_data)

    await safe_reply(update, context, f"✅ 已拉黑用户：{uid}")


@register_command("移除拉黑", "解除拉黑", "黑名单移除")
async def cmd_unblacklist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    uid = _resolve_target_uid(update, context)
    if not uid:
        return await safe_reply(
            update, context, "用法：移除拉黑 用户ID\n或回复用户消息后发送 移除拉黑"
        )

    user_data = _load_user_data()
    if uid not in user_data:
        return await safe_reply(update, context, "未找到该用户记录。")
    user_data[uid]["blocked"] = False
    _save_user_data(user_data)
    await safe_reply(update, context, f"✅ 已移除拉黑：{uid}")


@register_command("黑名单", "查看黑名单")
async def cmd_blacklist_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    user_data = _load_user_data()
    black_users = [
        (uid, info) for uid, info in user_data.items() if info.get("blocked")
    ]
    if not black_users:
        return await safe_reply(update, context, "当前黑名单为空。")

    args = update.message.text.split()
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    per_page = 20
    total = len(black_users)
    pages = (total + per_page - 1) // per_page
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    end = start + per_page
    items = black_users[start:end]

    text = f"黑名单总数：{total} | 第 {page}/{pages} 页\n\n"
    for i, (uid, info) in enumerate(items, start=start + 1):
        name = info.get("name", "")
        username = info.get("username", "")
        text += f"{i}. {name} (@{username})\nID: {uid}\n\n"

    if page < pages:
        text += f"➡️ 发送 /黑名单 {page+1} 查看下一页"

    await safe_reply(update, context, text)


@register_command("导出用户")
async def cmd_export_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return

    user_data = load_json(BOT_USER_FILE) or {}

    if not user_data:
        return await safe_reply(update, context, "没有用户数据")

    file_path = "users.txt"

    with open(file_path, "w", encoding="utf-8") as f:
        for uid, info in user_data.items():
            f.write(f"{uid} | {info.get('name','')} | @{info.get('username','')}\n")

    with open(file_path, "rb") as f:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=f)


from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    filters,
)

# 状态机阶段
SELECT_PAGE, TYPING_MESSAGE = range(2)
page_size = 10  # 每页显示用户数量
selected_users = {}  # 管理员ID -> set of选中用户ID


# 1️⃣ 命令入口：显示用户列表第一页
@register_command("发送消息用户")
async def cmd_send_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    user_data = _load_user_data()
    if not user_data:
        return await safe_reply(update, context, "📭 当前没有用户记录")

    selected_users[update.effective_user.id] = set()
    context.user_data[SEND_USER_STAGE_KEY] = "selecting"
    return await show_user_page(update, context, page=1)


# 2️⃣ 分页显示用户列表
async def show_user_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    user_data = _load_user_data()
    user_list = list(user_data.items())
    total_pages = (len(user_list) + page_size - 1) // page_size
    if total_pages <= 0:
        total_pages = 1
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    start = (page - 1) * page_size
    end = start + page_size
    items = user_list[start:end]

    admin_id = update.effective_user.id
    selected_set = selected_users.get(admin_id, set())

    keyboard = []
    for uid, info in items:
        name = info.get("name") or "无名"
        username = info.get("username", "")
        display = f"{name} (@{username})" if username else name
        prefix = "✅" if str(uid) in selected_set else ""
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{prefix}{display}", callback_data=f"user_{uid}_page{page}"
                )
            ]
        )

    # 翻页按钮
    nav_buttons = []
    if page > 1:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ 上一页", callback_data=f"page_{page-1}")
        )
    if page < total_pages:
        nav_buttons.append(
            InlineKeyboardButton("下一页 ➡️", callback_data=f"page_{page+1}")
        )
    if selected_set:
        nav_buttons.append(InlineKeyboardButton("✅ 完成选择", callback_data="done"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "请选择要发送消息的用户：", reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "请选择要发送消息的用户：", reply_markup=reply_markup
        )

    context.user_data[SEND_USER_STAGE_KEY] = "selecting"
    return SELECT_PAGE


# 3️⃣ 按钮点击处理
async def user_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if context.user_data.get(SEND_USER_STAGE_KEY) not in {"selecting", "typing"}:
        return
    await query.answer()
    admin_id = query.from_user.id
    data = query.data

    if data.startswith("user_"):
        uid = data.split("_")[1]
        # 切换选择状态
        if uid in selected_users.get(admin_id, set()):
            selected_users[admin_id].remove(uid)
        else:
            selected_users[admin_id].add(uid)
        page = int(data.split("page")[1])
        return await show_user_page(update, context, page)
    elif data.startswith("page_"):
        page = int(data.split("_")[1])
        return await show_user_page(update, context, page)
    elif data == "done":
        if not selected_users.get(admin_id):
            await query.edit_message_text("❌ 未选择任何用户，请选择后再完成")
            return SELECT_PAGE
        context.user_data[SEND_USER_STAGE_KEY] = "typing"
        await query.edit_message_text("✅ 已选择用户，请发送消息内容：")
        return TYPING_MESSAGE


# 4️⃣ 管理员输入消息 → 发送给选定用户
async def send_message_to_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(SEND_USER_STAGE_KEY) != "typing":
        return
    admin_id = update.effective_user.id
    uids = selected_users.get(admin_id, set())
    if not uids:
        return await safe_reply(update, context, "❌ 未选择用户，请重新操作。")

    success, failed = 0, 0
    for uid in uids:
        try:
            await safe_forward_media(context.bot, int(uid), update.message)
            success += 1
        except Exception as e:
            print(f"发送失败 {uid}: {e}")
            failed += 1

    await safe_reply(
        update, context, f"📩 消息已发送完成\n✅ 成功: {success}\n❌ 失败: {failed}"
    )
    selected_users.pop(admin_id, None)
    context.user_data.pop(SEND_USER_STAGE_KEY, None)
    return ConversationHandler.END


# 5️⃣ 取消操作
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selected_users.pop(update.effective_user.id, None)
    context.user_data.pop(SEND_USER_STAGE_KEY, None)
    await safe_reply(update, context, "❌ 已取消操作")
    return ConversationHandler.END


def register_send_user_conv(app):
    app.add_handler(
        CallbackQueryHandler(user_page_callback, pattern=r"^(user_|page_|done$)")
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            send_message_to_users,
        ),
        group=2,
    )
