import os
import json
import re
import sqlite3
import asyncio
from datetime import datetime
from typing import Optional

from command_router import register_command
from utils import (
    SHARED_SESSION_NAME,
    get_sessions_dir,
    get_session_path,
    is_shared_session_name,
    load_json,
    save_json,
    is_super_admin,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from channel.channel_config import start_channel_config_with_source, start_channel_config_new

HISTORY_RANGE_FILE = os.path.join("data", "history_forward_range.json")
SUBSCRIPTION_FILE = "data/subscriptions.json"
SESSION_OWNERS_FILE = "data/telethon_session_owners.json"

LOGIN_STEP_PHONE = "await_phone"
LOGIN_STEP_CODE = "await_code"
LOGIN_STEP_PASSWORD = "await_password"

_LOGIN_STATE = {}
CALLBACK_PREFIX = "tlogin"
_JOIN_STATE = {}
_SESSION_LABEL_CACHE = {}
_CHANNEL_LIST_CACHE = {}
_BROADCAST_STATE = {}


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
        return True
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
    return _is_active_subscription(user) and _is_session_owner(user, session_name)


def _can_login(user) -> bool:
    return bool(user and (is_super_admin(user.id) or _is_active_subscription(user)))


def _require_active_subscription(user) -> bool:
    if not user:
        return False
    if is_super_admin(user.id):
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
    rows = [[InlineKeyboardButton(s, callback_data=f"{CALLBACK_PREFIX}:menu:{s}")] for s in sessions]
    rows.append(
        [
            InlineKeyboardButton("🔁 刷新列表", callback_data=f"{CALLBACK_PREFIX}:list"),
            InlineKeyboardButton("🔄 刷新用户名", callback_data=f"{CALLBACK_PREFIX}:refresh"),
        ]
    )
    return InlineKeyboardMarkup(rows)


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
    msg = await _plain_reply(update, context, "请输入手机号（含国家码），例如：+8613812345678")
    if msg:
        _LOGIN_STATE[uid]["prompt_chat_id"] = msg.chat_id
        _LOGIN_STATE[uid]["prompt_message_id"] = msg.message_id


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
        session_name = broadcast_state.get("session")
        _BROADCAST_STATE.pop(uid, None)
        if not session_name:
            await _plain_reply(update, context, "群发失败：未找到小号。")
            return True
        if not _can_access_session(update.effective_user, session_name):
            await _plain_reply(update, context, "🚫 无权使用该账号群发。")
            return True
        api_id, api_hash = _get_api_creds()
        if not api_id or not api_hash:
            await _plain_reply(update, context, "❗ 未配置 API_ID/API_HASH。")
            return True
        try:
            from telethon import TelegramClient
        except Exception:
            await _plain_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")
            return True
        group_ids = await _fetch_account_group_ids(context, session_name)
        if not group_ids:
            await _plain_reply(update, context, "未获取到群组列表（可能未加入群）。")
            return True
        session_path = get_session_path(context, session_name)
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                await _plain_reply(update, context, "该小号未登录，请重新登录。")
                return True
            sent, failed = 0, 0
            msg = update.message
            for gid in group_ids:
                try:
                    if msg.text:
                        await client.send_message(gid, msg.text)
                    elif msg.photo:
                        file = await context.bot.get_file(msg.photo[-1].file_id)
                        temp_path = f"/tmp/tg_bc_{msg.photo[-1].file_id}.jpg"
                        await file.download_to_drive(temp_path)
                        await client.send_file(gid, temp_path, caption=msg.caption or "")
                        os.remove(temp_path)
                    elif msg.video:
                        file = await context.bot.get_file(msg.video.file_id)
                        temp_path = f"/tmp/tg_bc_{msg.video.file_id}.mp4"
                        await file.download_to_drive(temp_path)
                        await client.send_file(gid, temp_path, caption=msg.caption or "")
                        os.remove(temp_path)
                    elif msg.document:
                        file = await context.bot.get_file(msg.document.file_id)
                        temp_path = f"/tmp/tg_bc_{msg.document.file_id}"
                        await file.download_to_drive(temp_path)
                        await client.send_file(gid, temp_path, caption=msg.caption or "")
                        os.remove(temp_path)
                    elif msg.animation:
                        file = await context.bot.get_file(msg.animation.file_id)
                        temp_path = f"/tmp/tg_bc_{msg.animation.file_id}.gif"
                        await file.download_to_drive(temp_path)
                        await client.send_file(gid, temp_path, caption=msg.caption or "")
                        os.remove(temp_path)
                    else:
                        failed += 1
                        continue
                    sent += 1
                    await asyncio.sleep(0.3)
                except Exception as e:
                    failed += 1
                    print(f"群发失败 {gid}: {e}")
            await _plain_reply(update, context, f"✅ 群发完成\n成功: {sent}\n失败: {failed}")
            return True
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

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
        from telethon.errors import SessionPasswordNeededError
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

        session_path = get_session_path(context, session_name)
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                _JOIN_STATE.pop(uid, None)
                await _plain_reply(update, context, "该小号未登录，请重新登录。")
                return True

            if "t.me/+" in raw or "joinchat" in raw:
                invite_hash = raw.split("/")[-1]
                if invite_hash.startswith("+"):
                    invite_hash = invite_hash[1:]
                await client(ImportChatInviteRequest(invite_hash))
            else:
                target = raw
                if target.startswith("https://t.me/"):
                    target = target.replace("https://t.me/", "")
                if target.startswith("t.me/"):
                    target = target.replace("t.me/", "")
                if target.startswith("@"):
                    target = target[1:]
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
        await _plain_reply(update, context, "已发送验证码，请输入验证码：")
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
            await _plain_reply(update, context, "该账号开启了二步验证，请输入密码：")
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
        await client.sign_in(password=text)
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
        return await _plain_reply(update, context, "暂无可查看的小号。")
    labels = []
    for s in sessions:
        label = _get_cached_session_label(s)
        labels.append((s, label))
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:menu:{s}")] for s, label in labels]
        + [
            [
                InlineKeyboardButton("🔁 刷新列表", callback_data=f"{CALLBACK_PREFIX}:list"),
                InlineKeyboardButton("🔄 刷新用户名", callback_data=f"{CALLBACK_PREFIX}:refresh"),
            ]
        ]
    )
    await _plain_reply(
        update,
        context,
        "已登录小号列表（点击查看关注频道）：",
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
) -> list[str]:
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
        lines = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not getattr(entity, "megagroup", False) and getattr(entity, "broadcast", False):
                continue
            if not getattr(entity, "megagroup", False) and not getattr(entity, "broadcast", False):
                continue
            if getattr(entity, "broadcast", False):
                continue
            title = getattr(entity, "title", "") or "未命名群组"
            username = getattr(entity, "username", "")
            cid = getattr(entity, "id", "")
            if username:
                lines.append(f"{title} (@{username}) [{cid}]")
            else:
                lines.append(f"{title} [{cid}]")
            if len(lines) >= 100:
                break
        return lines
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _fetch_account_group_ids(
    context: ContextTypes.DEFAULT_TYPE, session_name: str
) -> list[int]:
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
        ids = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not getattr(entity, "megagroup", False):
                continue
            cid = getattr(entity, "id", None)
            if cid is None:
                continue
            ids.append(int(cid))
            if len(ids) >= 200:
                break
        return ids
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _fetch_account_group_ids(
    context: ContextTypes.DEFAULT_TYPE, session_name: str
) -> list[int]:
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
        ids = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not getattr(entity, "megagroup", False):
                continue
            cid = getattr(entity, "id", None)
            if cid is None:
                continue
            ids.append(int(cid))
            if len(ids) >= 200:
                break
        return ids
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


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

    if action == "list":
        await _clear_login_state(str(query.from_user.id), context)
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        sessions = _list_session_names(context, query.from_user)
        if not sessions:
            return await query.edit_message_text("暂无可查看的小号。")
        labels = []
        for s in sessions:
            label = _get_cached_session_label(s)
            labels.append((s, label))
        rows = [[InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:menu:{s}")] for s, label in labels]
        rows.append(
            [
                InlineKeyboardButton("🔁 刷新列表", callback_data=f"{CALLBACK_PREFIX}:list"),
                InlineKeyboardButton("🔄 刷新用户名", callback_data=f"{CALLBACK_PREFIX}:refresh"),
            ]
        )
        if context.user_data.get("start_panel"):
            rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
        keyboard = InlineKeyboardMarkup(rows)
        return await query.edit_message_text(
            "已登录小号列表（点击查看关注频道）：",
            reply_markup=keyboard,
        )

    if action == "refresh":
        await _clear_login_state(str(query.from_user.id), context)
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        sessions = _list_session_names(context, query.from_user)
        if not sessions:
            return await query.edit_message_text("暂无可查看的小号。")
        await query.edit_message_text("正在刷新小号用户名，请稍候...")
        for s in sessions:
            try:
                await _get_session_label(context, s)
            except Exception:
                continue
        labels = []
        for s in sessions:
            label = _get_cached_session_label(s)
            labels.append((s, label))
        rows = [[InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:menu:{s}")] for s, label in labels]
        rows.append(
            [
                InlineKeyboardButton("🔁 刷新列表", callback_data=f"{CALLBACK_PREFIX}:list"),
                InlineKeyboardButton("🔄 刷新用户名", callback_data=f"{CALLBACK_PREFIX}:refresh"),
            ]
        )
        if context.user_data.get("start_panel"):
            rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
        keyboard = InlineKeyboardMarkup(rows)
        return await query.edit_message_text(
            "已登录小号列表（点击查看关注频道）：",
            reply_markup=keyboard,
        )

    if action == "login":
        uid = str(query.from_user.id)
        await _clear_login_state(uid, context)
        return await _start_login_flow(update, context)

    if action == "menu":
        await _clear_login_state(str(query.from_user.id), context)
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
        await _clear_login_state(str(query.from_user.id), context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法群发。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权使用该账号群发。")
        uid = str(query.from_user.id)
        _BROADCAST_STATE[uid] = {"session": session_name}
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❌ 取消群发", callback_data=f"{CALLBACK_PREFIX}:bcancel:{session_name}")],
                [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:menu:{session_name}")],
            ]
        )
        return await query.edit_message_text("请发送要群发的消息：", reply_markup=keyboard)

    if action == "bcancel":
        await _clear_login_state(str(query.from_user.id), context)
        session_name = payload
        uid = str(query.from_user.id)
        _BROADCAST_STATE.pop(uid, None)
        if not session_name:
            return await query.edit_message_text("已取消。")
        label = _get_cached_session_label(session_name)
        return await query.edit_message_text(
            f"已选择账号：{label}\n请选择操作：",
            reply_markup=_build_account_menu_keyboard(session_name),
        )

    if action == "channels":
        await _clear_login_state(str(query.from_user.id), context)
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
        uid = str(query.from_user.id)
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
        await _clear_login_state(str(query.from_user.id), context)
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
                f"未获取到群组列表（可能未加入群）。",
                reply_markup=_build_account_menu_keyboard(session_name),
            )
        text = "加入的群组（最多 100 个）：\n" + "\n".join(groups)
        return await query.edit_message_text(text, reply_markup=_build_account_menu_keyboard(session_name))

    if action == "join":
        await _clear_login_state(str(query.from_user.id), context)
        session_name = payload
        if not session_name:
            return await query.edit_message_text("账号无效。")
        if not _require_active_subscription(query.from_user):
            return await query.edit_message_text("🚫 订阅已到期，无法查看小号。")
        if not _can_access_session(query.from_user, session_name):
            return await query.edit_message_text("🚫 无权查看该账号。")
        uid = str(query.from_user.id)
        _JOIN_STATE[uid] = {"session": session_name}
        return await query.edit_message_text(
            "请输入群号/用户名/邀请链接（如 @group 或 https://t.me/+xxxxx）：",
            reply_markup=_build_account_menu_keyboard(session_name),
        )

    if action == "cfg_new":
        await _clear_login_state(str(query.from_user.id), context)
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
