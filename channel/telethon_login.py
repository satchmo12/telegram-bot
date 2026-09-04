import os
import json
import re
import sqlite3
import asyncio
import tempfile
from datetime import datetime
from typing import Optional

from command_router import register_command
from channel.access_control import is_channel_subscription_required
from utils import (
    SHARED_SESSION_NAME,
    get_bot_path,
    get_sessions_dir,
    get_session_path,
    is_shared_session_name,
    load_json,
    save_json,
    is_super_admin,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from channel.channel_config import start_channel_config_with_source, start_channel_config_new

HISTORY_RANGE_FILE = os.path.join("data", "history_forward_range.json")
SUBSCRIPTION_FILE = "config_data/subscriptions.json"
SESSION_OWNERS_FILE = "data/telethon_session_owners.json"

LOGIN_STEP_PHONE = "await_phone"
LOGIN_STEP_CODE = "await_code"
LOGIN_STEP_PASSWORD = "await_password"

_LOGIN_STATE = {}
CALLBACK_PREFIX = "tlogin"
_JOIN_STATE = {}
_SESSION_LABEL_CACHE = {}
_CHANNEL_LIST_CACHE = {}
_GROUP_LIST_CACHE = {}
_BROADCAST_STATE = {}

GROUP_LIST_PAGE_SIZE = 15


async def _plain_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    try:
        if update.message:
            return await update.message.reply_text(text, reply_markup=reply_markup)
        return await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception:
        return None


def _login_cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ 取消登录", callback_data=f"{CALLBACK_PREFIX}:cancel")]]
    )


async def _send_login_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, uid: str, text: str
):
    state = _LOGIN_STATE.setdefault(uid, {})
    chat_id = state.get("prompt_chat_id")
    message_id = state.get("prompt_message_id")
    if chat_id and message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    msg = await _plain_reply(
        update, context, text, reply_markup=_login_cancel_markup()
    )
    if msg:
        state["prompt_chat_id"] = msg.chat_id
        state["prompt_message_id"] = msg.message_id
    return msg


def _empty_sessions_reply_markup(context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("start_panel"):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ 返回", callback_data="start:back")]]
        )
    return None


async def _clear_login_state(uid: str, context: ContextTypes.DEFAULT_TYPE):
    state = _LOGIN_STATE.pop(uid, None)
    if not state:
        return
    chat_id = state.get("prompt_chat_id")
    message_id = state.get("prompt_message_id")
    if chat_id and message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


def _clear_broadcast_state(uid: str):
    _BROADCAST_STATE.pop(uid, None)


def _normalize_username(value: str) -> str:
    if not value:
        return ""
    v = value.strip()
    if v.startswith("@"):
        v = v[1:]
    return v.strip().lower()


def _is_active_subscription(user) -> bool:
    if not user:
        return False
    data = load_json(SUBSCRIPTION_FILE)
    if not isinstance(data, dict):
        return False
    user_id = str(getattr(user, "id", "") or "")
    username = _normalize_username(getattr(user, "username", "") or "")

    record = None
    if user_id:
        record = data.get("users", {}).get(user_id)
    if not isinstance(record, dict) and username:
        record = data.get("usernames", {}).get(username)
    if not isinstance(record, dict):
        return False

    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d").date()
        return exp >= datetime.now().date()
    except Exception:
        return False


def _load_session_owners() -> dict:
    data = load_json(SESSION_OWNERS_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    return data


def _save_session_owners(data: dict) -> None:
    save_json(SESSION_OWNERS_FILE, data)


def _record_session_owner(session_name: str, user, label: str = "") -> None:
    if not session_name or not user:
        return
    data = _load_session_owners()
    data["sessions"][session_name] = {
        "owner_id": str(getattr(user, "id", "") or ""),
        "owner_username": _normalize_username(getattr(user, "username", "") or ""),
        "label": (label or "").strip(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_session_owners(data)


def _update_session_label(session_name: str, label: str) -> None:
    if not session_name:
        return
    clean_label = (label or "").strip()
    if not clean_label:
        return
    data = _load_session_owners()
    record = data.get("sessions", {}).get(session_name)
    record = record if isinstance(record, dict) else {}
    record["label"] = clean_label
    record["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data.setdefault("sessions", {})[session_name] = record
    _save_session_owners(data)
    _SESSION_LABEL_CACHE[session_name] = clean_label


def _get_cached_session_label(session_name: str) -> str:
    if session_name in _SESSION_LABEL_CACHE:
        return _SESSION_LABEL_CACHE[session_name]
    data = _load_session_owners()
    record = data.get("sessions", {}).get(session_name)
    label = ""
    if isinstance(record, dict):
        label = (record.get("label") or "").strip()
    if label:
        _SESSION_LABEL_CACHE[session_name] = label
        return label
    return session_name


def _is_session_owner(user, session_name: str) -> bool:
    if not user or not session_name:
        return False
    if is_shared_session_name(session_name):
        return False
    data = _load_session_owners()
    record = data.get("sessions", {}).get(session_name)
    if not isinstance(record, dict):
        return False
    user_id = str(getattr(user, "id", "") or "")
    username = _normalize_username(getattr(user, "username", "") or "")
    return bool(
        (user_id and record.get("owner_id") == user_id)
        or (username and record.get("owner_username") == username)
    )


def _can_access_session(user, session_name: str) -> bool:
    if not user:
        return False
    if is_super_admin(user.id):
        return True
    if not is_channel_subscription_required():
        return _is_session_owner(user, session_name)
    return _is_active_subscription(user) and _is_session_owner(user, session_name)


def _can_login(user) -> bool:
    if not user:
        return False
    if is_super_admin(user.id):
        return True
    if not is_channel_subscription_required():
        return True
    return _is_active_subscription(user)


def _require_active_subscription(user) -> bool:
    if not user:
        return False
    if is_super_admin(user.id):
        return True
    if not is_channel_subscription_required():
        return True
    return _is_active_subscription(user)


def _get_api_creds() -> tuple[Optional[int], Optional[str]]:
    api_id_raw = (
        os.getenv("API_ID")
        or os.getenv("api_id")
        or os.getenv("TELETHON_API_ID")
        or os.getenv("TG_API_ID")
    )
    api_hash = (
        os.getenv("API_HASH")
        or os.getenv("api_hash")
        or os.getenv("TELETHON_API_HASH")
        or os.getenv("TG_API_HASH")
    )
    if not api_id_raw or not api_hash:
        return None, None
    try:
        api_id = int(api_id_raw)
    except ValueError:
        return None, None
    return api_id, api_hash


def _sanitize_phone(phone: str) -> str:
    p = re.sub(r"[^\d+]", "", phone or "")
    if p.startswith("00"):
        p = "+" + p[2:]
    return p


def _get_sessions_dir(
    context: ContextTypes.DEFAULT_TYPE, session_name: Optional[str] = None
) -> str:
    base = get_sessions_dir(context, session_name)
    os.makedirs(base, exist_ok=True)
    return base


def _list_session_names(context: ContextTypes.DEFAULT_TYPE, user=None) -> list[str]:
    bot_base = _get_sessions_dir(context)
    shared_base = _get_sessions_dir(context, "main")
    names = set()
    if os.path.isdir(bot_base):
        for name in os.listdir(bot_base):
            if not name.endswith(".session"):
                continue
            raw = name[: -len(".session")]
            if raw:
                names.add(raw)
    if shared_base != bot_base and os.path.isdir(shared_base):
        main_path = os.path.join(shared_base, f"{SHARED_SESSION_NAME}.session")
        if os.path.exists(main_path):
            names.add(SHARED_SESSION_NAME)
    names = sorted(names)
    if not user or (user and is_super_admin(user.id)):
        return names
    # 仅返回该用户自己添加的账号
    return [n for n in names if _is_session_owner(user, n)]


def _build_sessions_keyboard(sessions: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_build_session_list_rows(sessions))


def _build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ 返回账号列表", callback_data=f"{CALLBACK_PREFIX}:list")]]
    )


def _build_account_menu_keyboard(session_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 查看频道", callback_data=f"{CALLBACK_PREFIX}:channels:{session_name}")],
            [InlineKeyboardButton("👥 查看群组", callback_data=f"{CALLBACK_PREFIX}:groups:{session_name}")],
            [InlineKeyboardButton("➕ 加群", callback_data=f"{CALLBACK_PREFIX}:join:{session_name}")],
            [InlineKeyboardButton("📣 群发消息", callback_data=f"{CALLBACK_PREFIX}:broadcast:{session_name}")],
            [InlineKeyboardButton("🗑 删除协议号", callback_data=f"{CALLBACK_PREFIX}:delete:{session_name}")],
            [InlineKeyboardButton("⬅️ 返回账号列表", callback_data=f"{CALLBACK_PREFIX}:list")],
        ]
    )


def _get_range_path(context: ContextTypes.DEFAULT_TYPE) -> str:
    return get_bot_path(context, HISTORY_RANGE_FILE)


def _load_range_config(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = load_json(_get_range_path(context))
    return data if isinstance(data, dict) else {}


def _save_range_config(context: ContextTypes.DEFAULT_TYPE, data: dict):
    save_json(_get_range_path(context), data)


async def _teardown_client(state: dict):
    client = state.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


def _is_group_entity(entity) -> bool:
    """Return whether a dialog entity is a Telegram group, excluding channels."""
    if getattr(entity, "broadcast", False):
        return False
    return bool(
        getattr(entity, "megagroup", False)
        or entity.__class__.__name__ == "Chat"  # 普通基础群
    )


def _group_display_name(group: dict) -> str:
    title = (group.get("title") or "未命名群组").strip()
    username = (group.get("username") or "").strip()
    group_id = group.get("id", "")
    if username:
        return f"{title} (@{username}) [{group_id}]"
    return f"{title} [{group_id}]"


def _truncate_button_text(text: str, max_bytes: int = 52) -> str:
    text = (text or "未命名群组").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max_bytes - len("…".encode("utf-8"))].decode(
        "utf-8", errors="ignore"
    ) + "…"


def _truncate_display_text(text: str, max_length: int = 180) -> str:
    text = (text or "未命名群组").strip()
    return text if len(text) <= max_length else f"{text[:max_length - 1]}…"


def _build_session_list_rows(sessions: list[str], include_start_back: bool = False) -> list[list[InlineKeyboardButton]]:
    rows = [
        [
            InlineKeyboardButton(
                _truncate_button_text(_get_cached_session_label(session_name), 60),
                callback_data=f"{CALLBACK_PREFIX}:menu:{session_name}",
            )
        ]
        for session_name in sessions
    ]
    rows.append(
        [
            InlineKeyboardButton("🔁 刷新列表", callback_data=f"{CALLBACK_PREFIX}:list"),
            InlineKeyboardButton("🔄 刷新用户名", callback_data=f"{CALLBACK_PREFIX}:refresh"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "📣 所有协议号群发", callback_data=f"{CALLBACK_PREFIX}:broadcast_all"
            )
        ]
    )
    if include_start_back:
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
    return rows


def _build_group_list_page(
    session_name: str, groups: list[dict], page: int
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(groups)
    total_pages = max(1, (total + GROUP_LIST_PAGE_SIZE - 1) // GROUP_LIST_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * GROUP_LIST_PAGE_SIZE
    visible_groups = groups[start : start + GROUP_LIST_PAGE_SIZE]

    lines = []
    keyboard_rows = []
    for idx, group in enumerate(visible_groups, start=start + 1):
        group_id = group.get("id")
        lines.append(f"{idx}. {_truncate_display_text(_group_display_name(group))}")
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    _truncate_button_text(f"{idx}. {group.get('title') or '未命名群组'}"),
                    callback_data=f"{CALLBACK_PREFIX}:gpage:{session_name}|{page}",
                ),
                InlineKeyboardButton(
                    "✉️ 发送消息",
                    callback_data=f"{CALLBACK_PREFIX}:sendgroup:{session_name}|{group_id}",
                ),
            ]
        )

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    "⬅️ 上一页", callback_data=f"{CALLBACK_PREFIX}:gpage:{session_name}|{page - 1}"
                )
            )
        if page < total_pages:
            nav.append(
                InlineKeyboardButton(
                    "下一页 ➡️", callback_data=f"{CALLBACK_PREFIX}:gpage:{session_name}|{page + 1}"
                )
            )
        if nav:
            keyboard_rows.append(nav)
    keyboard_rows.append(
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:menu:{session_name}")]
    )
    text = (
        f"加入的群组（共 {total} 个，第 {page}/{total_pages} 页；点击右侧按钮可单独发送）：\n"
        + "\n".join(lines)
    )
    return text, InlineKeyboardMarkup(keyboard_rows)


def _build_single_group_send_markup(session_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ 结束发送", callback_data=f"{CALLBACK_PREFIX}:bcancel:{session_name}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回群组列表", callback_data=f"{CALLBACK_PREFIX}:groups:{session_name}"
                )
            ],
        ]
    )


async def _delete_session_files(context: ContextTypes.DEFAULT_TYPE, session_name: str) -> int:
    """Delete one locally managed Telethon session and its SQLite sidecar files."""
    if not session_name or is_shared_session_name(session_name):
        return 0
    # Do not construct a path from arbitrary callback input.
    if session_name not in _list_session_names(context):
        return 0

    # A forwarding rule may keep this session open.  Disconnect it first so
    # Telethon cannot recreate the SQLite session file after it is deleted.
    try:
        from channel.telethon_forwarder import SESSION_CLIENTS_BY_BOT

        bot_name = str(context.application.bot_data.get("name", "") or "").strip()
        client = SESSION_CLIENTS_BY_BOT.get(bot_name, {}).pop(session_name, None)
        if client:
            await client.disconnect()
    except Exception as exc:
        print(f"删除协议号前断开监听失败 session={session_name}: {exc}")

    session_path = get_session_path(context, session_name)
    removed = 0
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        path = f"{session_path}{suffix}"
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            continue

    owners = _load_session_owners()
    if owners.get("sessions", {}).pop(session_name, None) is not None:
        _save_session_owners(owners)
    _SESSION_LABEL_CACHE.pop(session_name, None)
    for cache_key in list(_CHANNEL_LIST_CACHE):
        if cache_key[1] == session_name:
            _CHANNEL_LIST_CACHE.pop(cache_key, None)
    return removed


def _get_message_attachment(message):
    """Return attachment metadata needed to preserve Telegram media types."""
    if message.sticker:
        sticker = message.sticker
        if getattr(sticker, "is_animated", False):
            suffix, mime_type = ".tgs", "application/x-tgsticker"
        elif getattr(sticker, "is_video", False):
            suffix, mime_type = ".webm", "video/webm"
        else:
            suffix, mime_type = ".webp", "image/webp"
        return sticker, suffix, "sticker", mime_type
    if message.photo:
        return message.photo[-1], ".jpg", "photo", "image/jpeg"
    if message.video:
        return message.video, ".mp4", "video", getattr(message.video, "mime_type", None)
    if message.animation:
        animation = message.animation
        mime_type = getattr(animation, "mime_type", None)
        suffix = ".mp4" if mime_type == "video/mp4" else ".gif"
        return animation, suffix, "animation", mime_type
    if message.document:
        document = message.document
        mime_type = getattr(document, "mime_type", None)
        # GIF sent as a file should still retain its animation presentation.
        if mime_type == "image/gif":
            return document, ".gif", "animation", mime_type
        return document, "", "document", mime_type
    return None, None, None, None


async def _send_payload_to_group_targets(
    context: ContextTypes.DEFAULT_TYPE,
    targets: list[tuple[str, int]],
    message,
) -> tuple[int, int, int]:
    """Send one private-chat message through the selected sessions to group targets."""
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        raise RuntimeError("未配置 API_ID/API_HASH。")
    try:
        from telethon import TelegramClient, types
        from telethon.errors import UserDeactivatedBanError, UserDeactivatedError
    except Exception as exc:
        raise RuntimeError("Telethon 未安装，请先安装依赖。") from exc

    text = (message.text or "").strip()
    attachment, suffix, attachment_kind, mime_type = _get_message_attachment(message)
    if not text and not attachment:
        raise ValueError("仅支持发送文字、图片、视频、文件、GIF 动图或贴纸。")

    temp_path = None
    if attachment:
        fd, temp_path = tempfile.mkstemp(prefix="tg_broadcast_", suffix=suffix or ".file")
        os.close(fd)
        try:
            telegram_file = await context.bot.get_file(attachment.file_id)
            await telegram_file.download_to_drive(temp_path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

    grouped_targets: dict[str, list[int]] = {}
    for session_name, group_id in targets:
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            continue
        grouped_targets.setdefault(session_name, []).append(group_id)

    sent = failed = unavailable_sessions = deactivated_sessions = 0
    try:
        for session_name, group_ids in grouped_targets.items():
            client = TelegramClient(get_session_path(context, session_name), api_id, api_hash)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    unavailable_sessions += 1
                    failed += len(group_ids)
                    continue
                for group_id in group_ids:
                    try:
                        if text:
                            await client.send_message(group_id, text)
                        elif attachment_kind == "animation":
                            await client.send_file(
                                group_id,
                                temp_path,
                                caption=message.caption or "",
                                force_document=False,
                                mime_type=mime_type,
                                attributes=[types.DocumentAttributeAnimated()],
                                # 无声 MP4 动图也按 GIF 动图样式发送，而不是普通视频。
                                nosound_video=True,
                            )
                        elif attachment_kind == "sticker":
                            await client.send_file(
                                group_id,
                                temp_path,
                                force_document=False,
                                mime_type=mime_type,
                                attributes=[
                                    types.DocumentAttributeSticker(
                                        alt=getattr(message.sticker, "emoji", None) or "🙂",
                                        stickerset=types.InputStickerSetEmpty(),
                                    )
                                ],
                            )
                        else:
                            await client.send_file(
                                group_id,
                                temp_path,
                                caption=message.caption or "",
                                force_document=False,
                                mime_type=mime_type,
                            )
                        sent += 1
                        await asyncio.sleep(0.3)
                    except Exception as exc:
                        failed += 1
                        print(f"群发失败 session={session_name} group={group_id}: {exc}")
            except (UserDeactivatedError, UserDeactivatedBanError) as exc:
                # 这是协议号本身已注销/停用，不是私聊机器人的 Bot API 故障。
                deactivated_sessions += 1
                failed += len(group_ids)
                print(f"协议号已注销或停用 session={session_name}: {exc}")
            except Exception as exc:
                failed += len(group_ids)
                print(f"协议号连接失败 session={session_name}: {exc}")
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return sent, failed, unavailable_sessions, deactivated_sessions


async def _handle_broadcast_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> str:
    """Resolve broadcast state into concrete groups and send the incoming payload."""
    user = update.effective_user
    mode = state.get("mode")
    targets: list[tuple[str, int]] = []

    if mode == "all":
        sessions = _list_session_names(context, user)
    else:
        session_name = state.get("session")
        sessions = [session_name] if session_name else []

    for session_name in sessions:
        if not _can_access_session(user, session_name):
            continue
        if mode == "group":
            group_ids = [state.get("group_id")]
        else:
            group_ids = await _fetch_account_group_ids(context, session_name)
        for group_id in group_ids:
            try:
                targets.append((session_name, int(group_id)))
            except (TypeError, ValueError):
                continue

    if not targets:
        return "未获取到可发送的群组，请确认协议号已登录且已加入群组。"

    sent, failed, unavailable_sessions, deactivated_sessions = await _send_payload_to_group_targets(
        context, targets, update.message
    )
    details = []
    if unavailable_sessions:
        details.append(f"未登录协议号: {unavailable_sessions}")
    if deactivated_sessions:
        details.append(
            f"已注销/停用协议号: {deactivated_sessions}（请删除该 session 后重新登录有效账号）"
        )
    extra = f"\n" + "\n".join(details) if details else ""
    return f"✅ 发送完成\n成功: {sent}\n失败: {failed}{extra}"


async def _start_login_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _can_login(update.effective_user):
        return await _plain_reply(update, context, "🚫 仅高级管理员或订阅会员可登录小号。")

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await _plain_reply(
            update,
            context,
            "❗ 未配置 API_ID/API_HASH（或 TELETHON_API_ID/TELETHON_API_HASH）。",
        )

    uid = str(update.effective_user.id)
    _LOGIN_STATE[uid] = {"step": LOGIN_STEP_PHONE}
    await _send_login_prompt(
        update, context, uid, "请输入手机号（含国家码），例如：+8613812345678"
    )


@register_command("登录小号", "小号登录", "协议号登录")
async def telethon_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _start_login_flow(update, context)


@register_command("取消登录")
async def telethon_login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    state = _LOGIN_STATE.get(uid)
    if state:
        await _teardown_client(state)
        await _clear_login_state(uid, context)
        return await _plain_reply(update, context, "已取消登录流程。")
    await _plain_reply(update, context, "当前没有进行中的登录流程。")


async def handle_telethon_login_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message:
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    uid = str(update.effective_user.id)
    state = _LOGIN_STATE.get(uid)
    join_state = _JOIN_STATE.get(uid)
    broadcast_state = _BROADCAST_STATE.get(uid)
    if not state:
        if not join_state and not broadcast_state:
            return False

    if not _can_login(update.effective_user):
        await _clear_login_state(uid, context)
        _JOIN_STATE.pop(uid, None)
        _BROADCAST_STATE.pop(uid, None)
        return False

    if broadcast_state and not state and not join_state:
        # 单群发送保持在输入状态，后续私聊消息继续通过同一个协议号发往该群；
        # 账号群发和全协议号群发仍然保持一次发送即结束的行为。
        keep_single_group_send = broadcast_state.get("mode") == "group"
        if not keep_single_group_send:
            _clear_broadcast_state(uid)
        try:
            result = await _handle_broadcast_message(update, context, broadcast_state)
        except (RuntimeError, ValueError) as exc:
            result = f"❗ 发送失败：{exc}"
        except Exception as exc:
            print(f"协议号群发异常: {exc}")
            result = "❗ 发送失败，请稍后重试。"
        reply_markup = None
        if keep_single_group_send:
            session_name = broadcast_state.get("session")
            if session_name:
                result += "\n\n✉️ 当前仍处于该群发送状态，可继续发送下一条消息。"
                reply_markup = _build_single_group_send_markup(session_name)
        await _plain_reply(update, context, result, reply_markup=reply_markup)
        return True

    if not update.message.text:
        return False
    text = update.message.text.strip()
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        await _clear_login_state(uid, context)
        await _plain_reply(
            update,
            context,
            "❗ 未配置 API_ID/API_HASH（或 TELETHON_API_ID/TELETHON_API_HASH）。",
        )
        return True

    try:
        from telethon import TelegramClient
        from telethon.errors import (
            PasswordHashInvalidError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
    except Exception:
        await _clear_login_state(uid, context)
        await _plain_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")
        return True

    if join_state:
        session_name = join_state.get("session")
        if not session_name:
            _JOIN_STATE.pop(uid, None)
            await _plain_reply(update, context, "加群状态异常，请重新选择小号。")
            return True
        if not _require_active_subscription(update.effective_user):
            _JOIN_STATE.pop(uid, None)
            await _plain_reply(update, context, "🚫 订阅已到期，无法操作该账号。")
            return True
        if not _can_access_session(update.effective_user, session_name):
            _JOIN_STATE.pop(uid, None)
            await _plain_reply(update, context, "🚫 无权使用该账号。")
            return True
        try:
            from telethon import TelegramClient
            from telethon.tl.functions.channels import JoinChannelRequest
            from telethon.tl.functions.messages import ImportChatInviteRequest
        except Exception:
            _JOIN_STATE.pop(uid, None)
            await _plain_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")
            return True

        raw = text.strip()
        if not raw:
            await _plain_reply(update, context, "请输入群号/用户名/邀请链接。")
            return True

        normalized = raw
        if normalized.startswith("https://t.me/"):
            normalized = normalized.replace("https://t.me/", "")
        if normalized.startswith("t.me/"):
            normalized = normalized.replace("t.me/", "")
        if normalized.startswith("@"):
            normalized = normalized[1:]

        is_invite_link = "t.me/+" in raw or "joinchat" in raw
        is_numeric_target = normalized.lstrip("-").isdigit()
        is_username_target = bool(
            normalized
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", normalized)
        )
        if not (is_invite_link or is_numeric_target or is_username_target):
            await _plain_reply(update, context, "❗ 请输入正确的群号、@用户名，或邀请链接。")
            return True

        session_path = get_session_path(context, session_name)
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                _JOIN_STATE.pop(uid, None)
                await _plain_reply(update, context, "该小号未登录，请重新登录。")
                return True

            if is_invite_link:
                invite_hash = raw.split("/")[-1]
                if invite_hash.startswith("+"):
                    invite_hash = invite_hash[1:]
                await client(ImportChatInviteRequest(invite_hash))
            else:
                target = normalized
                if target.lstrip("-").isdigit():
                    entity = await client.get_entity(int(target))
                    await client(JoinChannelRequest(entity))
                else:
                    await client(JoinChannelRequest(target))

            _JOIN_STATE.pop(uid, None)
            await _plain_reply(update, context, "✅ 已尝试加入群/频道。")
            return True
        except Exception as e:
            _JOIN_STATE.pop(uid, None)
            await _plain_reply(update, context, f"❗ 加群失败：{e}")
            return True
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


    step = state.get("step")
    if step == LOGIN_STEP_PHONE:
        phone = _sanitize_phone(text)
        if not phone or len(phone) < 6:
            await _plain_reply(update, context, "手机号格式不正确，请重新输入。")
            return True
        _get_sessions_dir(context, phone)
        session_path = get_session_path(context, phone)
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        await client.send_code_request(phone)
        state.update({"step": LOGIN_STEP_CODE, "phone": phone, "client": client})
        await _send_login_prompt(update, context, uid, "已发送验证码，请输入验证码：")
        return True

    if step == LOGIN_STEP_CODE:
        code = re.sub(r"\s+", "", text)
        if not code:
            await _plain_reply(update, context, "验证码不能为空，请重新输入。")
            return True
        client = state.get("client")
        phone = state.get("phone")
        if not client or not phone:
            await _clear_login_state(uid, context)
            await _plain_reply(update, context, "登录状态异常，请重新发送「登录小号」。")
            return True
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            state["step"] = LOGIN_STEP_PASSWORD
            await _send_login_prompt(
                update, context, uid, "该账号开启了二步验证，请输入密码："
            )
            return True
        except PhoneCodeInvalidError:
            await _send_login_prompt(
                update, context, uid, "验证码错误，请重新输入验证码："
            )
            return True
        except PhoneCodeExpiredError:
            await _teardown_client(state)
            await _clear_login_state(uid, context)
            await _plain_reply(update, context, "验证码已过期，请重新发送「登录小号」。")
            return True
        label = ""
        try:
            me = await client.get_me()
            name = (getattr(me, "first_name", "") or "").strip()
            username = (getattr(me, "username", "") or "").strip()
            label = name or phone
            if username:
                label = f"{label} (@{username})"
        except Exception:
            label = ""
        await _teardown_client(state)
        await _clear_login_state(uid, context)
        _record_session_owner(phone, update.effective_user, label=label)
        if label:
            _SESSION_LABEL_CACHE[phone] = label
        await _plain_reply(update, context, "✅ 登录成功，session 已保存。")
        return True

    if step == LOGIN_STEP_PASSWORD:
        client = state.get("client")
        if not client:
            await _clear_login_state(uid, context)
            await _plain_reply(update, context, "登录状态异常，请重新发送「登录小号」。")
            return True
        try:
            await client.sign_in(password=text)
        except PasswordHashInvalidError:
            await _send_login_prompt(
                update, context, uid, "二步验证密码错误，请重新输入密码："
            )
            return True
        label = ""
        try:
            me = await client.get_me()
            name = (getattr(me, "first_name", "") or "").strip()
            username = (getattr(me, "username", "") or "").strip()
            label = name or (state.get("phone") or "")
            if username:
                label = f"{label} (@{username})"
        except Exception:
            label = ""
        await _teardown_client(state)
        await _clear_login_state(uid, context)
        phone = state.get("phone")
        if phone:
            _record_session_owner(phone, update.effective_user, label=label)
            if label:
                _SESSION_LABEL_CACHE[phone] = label
        await _plain_reply(update, context, "✅ 登录成功，session 已保存。")
        return True

    return False


async def telethon_login_input_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if await handle_telethon_login_text(update, context):
        raise ApplicationHandlerStop


@register_command("历史转发范围")
async def history_forward_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return

    args = context.args or []
    action = args[0] if args else "查看"
    cfg = _load_range_config(context)

    if action in ("查看", "看", "状态"):
        if not cfg:
            return await _plain_reply(update, context, "历史转发范围未设置。")
        return await _plain_reply(update, context, json.dumps(cfg, ensure_ascii=False, indent=2))

    if action in ("最近", "条数"):
        if len(args) < 2 or not args[1].isdigit():
            return await _plain_reply(update, context, "用法：历史转发范围 最近 <条数>")
        cfg = {"mode": "recent", "limit": int(args[1])}
        _save_range_config(context, cfg)
        return await _plain_reply(update, context, f"✅ 已设置范围：最近 {cfg['limit']} 条")

    if action in ("起始ID", "起始"):
        if len(args) < 2 or not str(args[1]).isdigit():
            return await _plain_reply(update, context, "用法：历史转发范围 起始ID <消息ID>")
        cfg = {"mode": "from_id", "from_id": int(args[1])}
        _save_range_config(context, cfg)
        return await _plain_reply(update, context, "✅ 已设置范围：从指定消息 ID 开始")

    if action in ("时间段", "日期"):
        if len(args) < 3:
            return await _plain_reply(update, context, "用法：历史转发范围 时间段 <开始日期> <结束日期>")
        try:
            start = datetime.strptime(args[1], "%Y-%m-%d").date().isoformat()
            end = datetime.strptime(args[2], "%Y-%m-%d").date().isoformat()
        except Exception:
            return await _plain_reply(update, context, "日期格式错误，请使用 YYYY-MM-DD")
        cfg = {"mode": "date_range", "start_date": start, "end_date": end}
        _save_range_config(context, cfg)
        return await _plain_reply(update, context, f"✅ 已设置范围：{start} ~ {end}")

    await _plain_reply(
        update,
        context,
        "用法：历史转发范围 查看\n"
        "历史转发范围 最近 <条数>\n"
        "历史转发范围 起始ID <消息ID>\n"
        "历史转发范围 时间段 <YYYY-MM-DD> <YYYY-MM-DD>",
    )


@register_command("查看登录", "查看小号")
async def list_logged_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _require_active_subscription(user):
        return await _plain_reply(update, context, "🚫 订阅已到期，无法查看小号。")
    sessions = _list_session_names(context, user)
    if not sessions:
        return await _plain_reply(
            update,
            context,
            "暂无可查看的小号。",
            reply_markup=_empty_sessions_reply_markup(context),
        )
    keyboard = InlineKeyboardMarkup(_build_session_list_rows(sessions))
    await _plain_reply(
        update,
        context,
        "已登录协议号列表（点击进入管理）：",
        reply_markup=keyboard,
    )


async def _fetch_account_channels(
    context: ContextTypes.DEFAULT_TYPE, session_name: str
) -> list[dict]:
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return []
    try:
        from telethon import TelegramClient
    except Exception:
        return []

    session_path = get_session_path(context, session_name)
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
    except sqlite3.OperationalError:
        return []
    try:
        if not await client.is_user_authorized():
            return []
        channels = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not getattr(entity, "broadcast", False):
                continue
            title = getattr(entity, "title", "") or "未命名频道"
            username = getattr(entity, "username", "")
            cid = getattr(entity, "id", "")
            channels.append({"title": title, "username": username, "id": cid})
            if len(channels) >= 100:
                break
        return channels
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _fetch_account_groups(
    context: ContextTypes.DEFAULT_TYPE, session_name: str
) -> list[dict]:
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return []
    try:
        from telethon import TelegramClient, utils as telethon_utils
    except Exception:
        return []

    client = TelegramClient(get_session_path(context, session_name), api_id, api_hash)
    try:
        await client.connect()
    except sqlite3.OperationalError:
        return []
    try:
        if not await client.is_user_authorized():
            return []
        groups = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not _is_group_entity(entity):
                continue
            try:
                # Marked peer IDs preserve whether the dialog is a basic group or
                # a megagroup/channel when Telethon resolves the destination later.
                group_id = int(telethon_utils.get_peer_id(entity))
            except Exception:
                continue
            groups.append(
                {
                    "title": getattr(entity, "title", "") or "未命名群组",
                    "username": getattr(entity, "username", "") or "",
                    "id": group_id,
                }
            )
            if len(groups) >= 100:
                break
        return groups
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _fetch_account_group_ids(
    context: ContextTypes.DEFAULT_TYPE, session_name: str
) -> list[int]:
    groups = await _fetch_account_groups(context, session_name)
    return [group["id"] for group in groups if group.get("id") is not None]


async def _get_session_label(context: ContextTypes.DEFAULT_TYPE, session_name: str) -> str:
    if session_name in _SESSION_LABEL_CACHE:
        return _SESSION_LABEL_CACHE[session_name]
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return session_name
    try:
        from telethon import TelegramClient
    except Exception:
        return session_name
    session_path = get_session_path(context, session_name)
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
    except sqlite3.OperationalError:
        return session_name
    try:
        if not await client.is_user_authorized():
            return session_name
        me = await client.get_me()
        name = (getattr(me, "first_name", "") or "").strip()
        username = (getattr(me, "username", "") or "").strip()
        label = name or session_name
        if username:
            label = f"{label} (@{username})"
        _SESSION_LABEL_CACHE[session_name] = label
        _update_session_label(session_name, label)
        return label
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def handle_telethon_login_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(f"{CALLBACK_PREFIX}:"):
        return
    await query.answer()

    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""
    uid = str(query.from_user.id)

    # 只要离开“等待输入群发内容”的界面，就清掉待群发状态，避免后续任意私聊文本被误当成群发内容。
    if action not in {"broadcast", "broadcast_all", "sendgroup"}:
        _clear_broadcast_state(uid)

    if action == "list":
        await _clear_login_state(uid, context)
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        sessions = _list_session_names(context, query.from_user)
        if not sessions:
            return await query.edit_message_text(
                "暂无可查看的小号。",
                reply_markup=_empty_sessions_reply_markup(context),
            )
        keyboard = InlineKeyboardMarkup(
            _build_session_list_rows(
                sessions, include_start_back=bool(context.user_data.get("start_panel"))
            )
        )
        return await query.edit_message_text(
            "已登录协议号列表（点击进入管理）：",
            reply_markup=keyboard,
        )

    if action == "refresh":
        await _clear_login_state(uid, context)
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        sessions = _list_session_names(context, query.from_user)
        if not sessions:
            return await query.edit_message_text(
                "暂无可查看的小号。",
                reply_markup=_empty_sessions_reply_markup(context),
            )
        await query.edit_message_text("正在刷新小号用户名，请稍候...")
        for s in sessions:
            try:
                await _get_session_label(context, s)
            except Exception:
                continue
        keyboard = InlineKeyboardMarkup(
            _build_session_list_rows(
                sessions, include_start_back=bool(context.user_data.get("start_panel"))
            )
        )
        return await query.edit_message_text(
            "已登录协议号列表（点击进入管理）：",
            reply_markup=keyboard,
        )

    if action == "login":
        await _clear_login_state(uid, context)
        return await _start_login_flow(update, context)

    if action == "cancel":
        state = _LOGIN_STATE.pop(uid, None)
        if state:
            await _teardown_client(state)
            return await query.edit_message_text("已取消登录流程。")
        return await query.edit_message_text("当前没有进行中的登录流程。")

    if action == "menu":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        label = _get_cached_session_label(session_name)
        keyboard = _build_account_menu_keyboard(session_name)
        if context.user_data.get("start_panel"):
            rows = list(keyboard.inline_keyboard)
            rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
            keyboard = InlineKeyboardMarkup(rows)
        return await query.edit_message_text(
            f"已选择账号：{label}\n请选择操作：",
            reply_markup=keyboard,
        )

    if action == "broadcast":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法群发。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权使用该账号群发。")
        _BROADCAST_STATE[uid] = {"mode": "session", "session": session_name}
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❌ 取消群发", callback_data=f"{CALLBACK_PREFIX}:bcancel:{session_name}")],
                [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:menu:{session_name}")],
            ]
        )
        return await query.edit_message_text("请发送要群发的消息：", reply_markup=keyboard)

    if action == "broadcast_all":
        await _clear_login_state(uid, context)
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法群发。")
        sessions = _list_session_names(context, query.from_user)
        if not sessions:
            return await query.edit_message_text("暂无可使用的协议号。")
        _BROADCAST_STATE[uid] = {"mode": "all"}
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❌ 取消群发", callback_data=f"{CALLBACK_PREFIX}:bcancel:all")],
                [InlineKeyboardButton("⬅️ 返回账号列表", callback_data=f"{CALLBACK_PREFIX}:list")],
            ]
        )
        return await query.edit_message_text(
            f"将使用全部 {len(sessions)} 个可管理协议号，向各自已加入的群组发送消息。\n\n"
            "请发送要群发的消息：",
            reply_markup=keyboard,
        )

    if action == "sendgroup":
        await _clear_login_state(uid, context)
        try:
            session_name, group_id_raw = payload.rsplit("|", 1)
            group_id = int(group_id_raw)
        except (TypeError, ValueError):
            return await query.edit_message_text("群组信息无效，请重新查看群组列表。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法发送消息。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权使用该协议号发送消息。")
        groups = await _fetch_account_groups(context, session_name)
        if group_id not in {group.get("id") for group in groups}:
            return await query.edit_message_text("群组已不存在或协议号不在该群内，请重新查看群组列表。")
        _BROADCAST_STATE[uid] = {
            "mode": "group",
            "session": session_name,
            "group_id": group_id,
        }
        keyboard = _build_single_group_send_markup(session_name)
        return await query.edit_message_text(
            "请发送要发到该群的消息。\n发送成功后可继续连续发送，点击「结束发送」才会退出。",
            reply_markup=keyboard,
        )

    if action == "bcancel":
        await _clear_login_state(uid, context)
        session_name = payload
        _clear_broadcast_state(uid)
        if session_name == "all":
            sessions = _list_session_names(context, query.from_user)
            keyboard = (
                InlineKeyboardMarkup(
                    _build_session_list_rows(
                        sessions,
                        include_start_back=bool(context.user_data.get("start_panel")),
                    )
                )
                if sessions
                else _empty_sessions_reply_markup(context)
            )
            return await query.edit_message_text("已取消群发。", reply_markup=keyboard)
        if not session_name:
            return await query.edit_message_text("已取消。")
        label = _get_cached_session_label(session_name)
        return await query.edit_message_text(
            f"已选择账号：{label}\n请选择操作：",
            reply_markup=_build_account_menu_keyboard(session_name),
        )

    if action == "delete":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if is_shared_session_name(session_name):
            return await query.edit_message_text("🚫 共享主协议号不能在这里删除。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权删除该协议号。")
        label = _get_cached_session_label(session_name)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚠️ 确认删除", callback_data=f"{CALLBACK_PREFIX}:delconfirm:{session_name}"
                    ),
                    InlineKeyboardButton(
                        "取消", callback_data=f"{CALLBACK_PREFIX}:menu:{session_name}"
                    ),
                ]
            ]
        )
        return await query.edit_message_text(
            f"确定删除协议号「{label}」吗？\n删除后需要重新登录才能使用。",
            reply_markup=keyboard,
        )

    if action == "delconfirm":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name or is_shared_session_name(session_name):
            return await query.edit_message_text("🚫 该协议号不能删除。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权删除该协议号。")
        if not await _delete_session_files(context, session_name):
            return await query.edit_message_text("删除失败：未找到本地协议号文件。")
        sessions = _list_session_names(context, query.from_user)
        keyboard = (
            InlineKeyboardMarkup(
                _build_session_list_rows(
                    sessions, include_start_back=bool(context.user_data.get("start_panel"))
                )
            )
            if sessions
            else _empty_sessions_reply_markup(context)
        )
        return await query.edit_message_text("✅ 协议号已删除。", reply_markup=keyboard)

    if action == "channels":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        channels = await _fetch_account_channels(context, session_name)
        if not channels:
            return await query.edit_message_text(
                f"未获取到频道列表（可能未登录或无关注频道）。",
                reply_markup=_build_account_menu_keyboard(session_name),
            )
        _CHANNEL_LIST_CACHE[(uid, session_name)] = channels
        lines = []
        keyboard_rows = []
        for idx, ch in enumerate(channels, start=1):
            title = ch.get("title", "") or "未命名频道"
            username = ch.get("username", "")
            cid = ch.get("id", "")
            if username:
                lines.append(f"{idx}. {title} (@{username}) [{cid}]")
            else:
                lines.append(f"{idx}. {title} [{cid}]")
        keyboard_rows.append(
            [InlineKeyboardButton("➕ 添加频道配置", callback_data=f"{CALLBACK_PREFIX}:cfg_new:{session_name}")]
        )
        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:menu:{session_name}")]
        )
        text = "关注的频道（最多 100 个）：\n" + "\n".join(lines)
        return await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))

    if action == "groups":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        groups = await _fetch_account_groups(context, session_name)
        if not groups:
            return await query.edit_message_text(
                "未获取到群组列表（可能未加入群）。",
                reply_markup=_build_account_menu_keyboard(session_name),
            )
        _GROUP_LIST_CACHE[(uid, session_name)] = groups
        text, keyboard = _build_group_list_page(session_name, groups, page=1)
        return await query.edit_message_text(text, reply_markup=keyboard)

    if action == "gpage":
        await _clear_login_state(uid, context)
        try:
            session_name, page_raw = payload.rsplit("|", 1)
            page = int(page_raw)
        except (TypeError, ValueError):
            return await query.edit_message_text("群组分页信息无效，请重新查看群组列表。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        groups = _GROUP_LIST_CACHE.get((uid, session_name))
        if not groups:
            groups = await _fetch_account_groups(context, session_name)
            _GROUP_LIST_CACHE[(uid, session_name)] = groups
        if not groups:
            return await query.edit_message_text(
                "未获取到群组列表（可能未加入群）。",
                reply_markup=_build_account_menu_keyboard(session_name),
            )
        text, keyboard = _build_group_list_page(session_name, groups, page)
        return await query.edit_message_text(text, reply_markup=keyboard)

    if action == "join":
        await _clear_login_state(uid, context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        _JOIN_STATE[uid] = {"session": session_name}
        return await query.edit_message_text(
            "请输入群号/用户名/邀请链接（如 @group 或 https://t.me/+xxxxx）：",
            reply_markup=_build_account_menu_keyboard(session_name),
        )

    if action == "cfg_new":
        await _clear_login_state(uid, context)
        session_name = payload
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法配置规则。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        text, keyboard = start_channel_config_new(context, query.from_user, session_name=session_name)
        if not text:
            return await query.edit_message_text("无法启动配置流程，请重试。")
        return await query.edit_message_text(text, reply_markup=keyboard)


def register_telethon_login_handlers(app):
    app.add_handler(
        CallbackQueryHandler(handle_telethon_login_callback, pattern=f"^{CALLBACK_PREFIX}:")
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (~filters.COMMAND),
            telethon_login_input_router,
        ),
        group=-1,
    )
