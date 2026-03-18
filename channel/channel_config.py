# -*- coding: utf-8 -*-
import os
import re
import time
from typing import Optional
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from command_router import register_command
from utils import BOT_USER_FILE, get_bot_path, is_super_admin, load_json, save_json, safe_reply

FORWARD_USER_CONFIG_FILE = "data/forward_config_users.json"
HISTORY_REQUESTS_FILE = "data/history_forward_requests.json"
SUBSCRIPTION_FILE = "data/subscriptions.json"
SESSION_OWNERS_FILE = "data/telethon_session_owners.json"
CALLBACK_PREFIX = "chcfg"
SUBSCRIBER_MAX_RULES = 5

FILTER_LABELS = {
    "all": "全部",
    "text": "文本",
    "photo": "图片",
    "video": "视频",
}

MODE_LABELS = {
    "listen": "监听消息",
    "history": "转发历史",
}


def _normalize_username(value: str) -> str:
    if not value:
        return ""
    v = value.strip()
    if v.startswith("@"):  # keep without @ for key
        v = v[1:]
    return v.strip().lower()


def _format_username(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    return v if v.startswith("@") else f"@{v}"


def _normalize_newlines(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return re.sub(r"\n{2,}", "\n", text)


def _today_date():
    return datetime.now().date()


async def _resolve_chat_title(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    if not context or not chat_id:
        return ""
    try:
        chat = await context.bot.get_chat(chat_id)
    except Exception:
        return ""
    title = (getattr(chat, "title", "") or "").strip()
    if not title:
        first = (getattr(chat, "first_name", "") or "").strip()
        last = (getattr(chat, "last_name", "") or "").strip()
        title = (f"{first} {last}").strip()
    if not title:
        username = (getattr(chat, "username", "") or "").strip()
        if username:
            return f"@{username}"
    return title


def _parse_expiry(value: str):
    v = (value or "").strip()
    if not v:
        return None
    if v.isdigit():
        days = int(v)
        return _today_date() + timedelta(days=days)
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except Exception:
        return None


def _load_subscriptions() -> dict:
    data = load_json(SUBSCRIPTION_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("users", {})
    data.setdefault("usernames", {})
    return data


def _save_subscriptions(data: dict) -> None:
    save_json(SUBSCRIPTION_FILE, data)


def _is_active_subscription(user) -> bool:
    if not user:
        return False
    data = _load_subscriptions()
    today = _today_date()
    user_id = str(getattr(user, "id", "") or "")
    username = _normalize_username(getattr(user, "username", "") or "")

    record = None
    if user_id and user_id in data.get("users", {}):
        record = data["users"].get(user_id)
    if not record and username:
        record = data.get("usernames", {}).get(username)

    if not isinstance(record, dict):
        return False

    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d").date()
        return exp >= today
    except Exception:
        return False


def _load_user_forward_config() -> dict:
    data = load_json(FORWARD_USER_CONFIG_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("users", {})
    return data


def _save_user_forward_config(data: dict) -> None:
    save_json(FORWARD_USER_CONFIG_FILE, data)


def _load_bot_users() -> dict:
    data = load_json(BOT_USER_FILE)
    return data if isinstance(data, dict) else {}


def _record_bot_user(user) -> str:
    if not user:
        return ""
    uid = str(getattr(user, "id", "") or "")
    if not uid:
        return ""
    users = _load_bot_users()
    record = users.get(uid)
    record = record if isinstance(record, dict) else {}
    username = getattr(user, "username", "") or record.get("username", "") or ""
    name = getattr(user, "first_name", "") or record.get("name", "") or ""
    merged = dict(record)
    merged.update(
        {
            "name": name,
            "username": username,
            "join_time": record.get("join_time", int(time.time())),
            "last_active": int(time.time()),
            "blocked": bool(record.get("blocked", False)),
        }
    )
    users[uid] = merged
    save_json(BOT_USER_FILE, users)
    return username


def _get_bot_user_username(user_id: str) -> str:
    if not user_id:
        return ""
    record = _load_bot_users().get(str(user_id), {})
    if not isinstance(record, dict):
        return ""
    return record.get("username", "") or ""


def _load_session_owners() -> dict:
    data = load_json(SESSION_OWNERS_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    return data


def _is_session_owner(user, session_name: str) -> bool:
    if not user or not session_name:
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


def _get_sessions_dir(context: ContextTypes.DEFAULT_TYPE) -> str:
    base = get_bot_path(context, "sessions")
    os.makedirs(base, exist_ok=True)
    return base


def _list_accessible_sessions(context: ContextTypes.DEFAULT_TYPE, user) -> list[str]:
    base = _get_sessions_dir(context)
    if not os.path.isdir(base):
        return []
    names = []
    for name in os.listdir(base):
        if not name.endswith(".session"):
            continue
        raw = name[: -len(".session")]
        if raw:
            names.append(raw)
    names = sorted(names)
    if not user or is_super_admin(user.id):
        return names
    return [n for n in names if _is_session_owner(user, n)]


def _get_session_label(session_name: str) -> str:
    data = _load_session_owners()
    record = data.get("sessions", {}).get(session_name)
    if isinstance(record, dict):
        label = (record.get("label") or "").strip()
        if label:
            return label
    return session_name


def _get_user_rules(user_id: str) -> list:
    data = _load_user_forward_config()
    user_cfg = data.get("users", {}).get(user_id, {})
    rules = user_cfg.get("forward_rules")
    return rules if isinstance(rules, list) else []


def _set_user_rules(user_id: str, rules: list, username: Optional[str] = None) -> None:
    data = _load_user_forward_config()
    user_cfg = data.get("users", {}).get(user_id, {})
    user_cfg["forward_rules"] = rules
    if username:
        user_cfg["username"] = username
    data.setdefault("users", {})[user_id] = user_cfg
    _save_user_forward_config(data)


def _private_chat_url(context: ContextTypes.DEFAULT_TYPE) -> str:
    username = getattr(context.bot, "username", "") or ""
    return f"https://t.me/{username}" if username else ""


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


def _require_access(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (is_super_admin(user.id) or _is_active_subscription(user)))


async def _ensure_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = (update.effective_chat.type or "").lower() if update.effective_chat else ""
    if chat_type in {"private"}:
        return True

    url = _private_chat_url(context)
    if url:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("👉 去私聊配置", url=url)]]
        )
        return await _plain_reply(update, context, "请私聊机器人发送“频道配置”进行设置。", reply_markup=keyboard)
    return await _plain_reply(update, context, "请私聊机器人发送“频道配置”进行设置。")


@register_command("频道配置")
async def channel_config_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_access(update):
        return await _plain_reply(update, context, "🚫 仅高级管理员或订阅会员可使用该功能。")

    ok = await _ensure_private(update, context)
    if ok is not True:
        return

    _record_bot_user(update.effective_user)

    sessions = _list_accessible_sessions(context, update.effective_user)
    if sessions:
        context.user_data["channel_config_session_select"] = {"next": "main"}
        return await _plain_reply(
            update,
            context,
            "请选择要配置的小号：",
            reply_markup=_with_start_back(context, _build_session_select_keyboard(sessions)),
        )

    await _plain_reply(
        update,
        context,
        "请选择操作：\n\n“新建配置”会按步骤引导填写频道名称、ID、任务类型、任务模式等。",
        reply_markup=_with_start_back(context, _build_main_menu_keyboard()),
    )


@register_command("订阅会员", "我的订阅")
async def subscription_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    data = _load_subscriptions()
    user_id = str(user.id)
    username = _normalize_username(getattr(user, "username", "") or "")

    record = data.get("users", {}).get(user_id)
    if not record and username:
        record = data.get("usernames", {}).get(username)

    if not isinstance(record, dict):
        return await _plain_reply(update, context, "📭 你当前不是订阅会员。")

    expires_at = record.get("expires_at", "")
    if not expires_at:
        return await _plain_reply(update, context, "📭 你当前不是订阅会员。")

    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d").date()
        status = "✅ 有效" if exp >= _today_date() else "❌ 已过期"
        return await _plain_reply(update, context, f"订阅状态：{status}\n到期时间：{expires_at}")
    except Exception:
        return await _plain_reply(update, context, "⚠️ 订阅信息格式异常，请联系管理员。")


@register_command("订阅列表", "查看订阅")
async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return await _plain_reply(update, context, "🚫 仅高级管理员可查看订阅列表。")

    data = _load_subscriptions()
    users = data.get("users", {}) if isinstance(data, dict) else {}
    usernames = data.get("usernames", {}) if isinstance(data, dict) else {}

    lines = ["订阅列表："]

    if users:
        lines.append("按用户ID：")
        for uid, record in users.items():
            if not isinstance(record, dict):
                continue
            expires_at = record.get("expires_at", "")
            uname = record.get("username", "")
            label = f"{uid}"
            if uname:
                label = f"{label} (@{uname})"
            if expires_at:
                label = f"{label} - 到期 {expires_at}"
            lines.append(label)

    if usernames:
        lines.append("按用户名：")
        for uname, record in usernames.items():
            if not isinstance(record, dict):
                continue
            expires_at = record.get("expires_at", "")
            label = f"@{uname}"
            if expires_at:
                label = f"{label} - 到期 {expires_at}"
            lines.append(label)

    if len(lines) == 1:
        lines.append("暂无订阅用户。")

    await _plain_reply(update, context, "\n".join(lines))


@register_command("添加订阅")
async def add_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return await _plain_reply(update, context, "🚫 仅高级管理员可添加订阅。")

    target_user = None
    username = ""
    user_id = ""

    if update.message and update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user

    if target_user:
        user_id = str(target_user.id)
        username = _normalize_username(getattr(target_user, "username", "") or "")

    args = context.args or []
    expiry_arg = ""
    if not target_user:
        if len(args) < 2:
            return await _plain_reply(
                update,
                context,
                "用法：添加订阅 @用户名 30\n或：添加订阅 @用户名 2026-12-31",
            )
        raw_user = args[0]
        if raw_user.isdigit() or re.fullmatch(r"-?\d+", raw_user):
            user_id = str(int(raw_user))
        else:
            username = _normalize_username(raw_user)
        expiry_arg = args[1]
    else:
        if len(args) < 1:
            return await _plain_reply(
                update,
                context,
                "用法：添加订阅 30\n或：添加订阅 2026-12-31",
            )
        expiry_arg = args[0]

    exp_date = _parse_expiry(expiry_arg)
    if not exp_date:
        return await _plain_reply(update, context, "❗ 时间格式示例：30 或 2026-12-31")

    data = _load_subscriptions()
    norm_username = _normalize_username(username) if username else ""
    record = {
        "expires_at": exp_date.strftime("%Y-%m-%d"),
    }
    if norm_username:
        record["username"] = norm_username
    if user_id:
        record["user_id"] = user_id

    if user_id:
        data.setdefault("users", {})[user_id] = record
    if norm_username:
        data.setdefault("usernames", {})[norm_username] = record

    _save_subscriptions(data)
    label = f"@{username}" if username else user_id
    return await _plain_reply(update, context, f"✅ 已设置订阅：{label} 到期 {record['expires_at']}")


def _start_new_wizard(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    context.user_data["channel_config"] = {
        "stage": "name",
        "draft": {
            "owner_id": user_id,
            "owner_username": _get_bot_user_username(user_id),
            "enabled": True,
            "mode": "listen",
        },
    }


def _new_rule_default(user_id: str, *, source_id=None, source_name: str = "", session_name: str = "") -> dict:
    rule = {
        "name": source_name or "未命名",
        "sources": [source_id] if source_id is not None else [],
        "targets": [],
        "source_title": "",
        "target_title": "",
        "enabled": True,
        "mode": "listen",
        "filter": "all",
        "exclude_channels": [],
        "show_contact": True,
        "replace_channel_user": "",
        "replace_group_name": "",
        "replace_submit_user": "",
        "clear_links": False,
        "start_id": "",
        "end_id": "",
        "include_words": [],
        "replace_words": [],
        "block_words": [],
        "cut_words": "",
        "suffix": "",
        "media_replace": "",
        "speed": "",
        "session_name": session_name or "",
    }
    return rule


def _create_rule(user_id: str, *, source_id=None, source_name: str = "", session_name: str = "") -> int:
    rules = _get_user_rules(user_id)
    rule = _new_rule_default(user_id, source_id=source_id, source_name=source_name, session_name=session_name)
    rules.append(rule)
    _set_user_rules(user_id, rules)
    return len(rules) - 1


def start_channel_config_new(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    session_name: str = "",
):
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return None, None
    idx = _create_rule(user_id, session_name=session_name or "")
    rules = _get_user_rules(user_id)
    rule = rules[idx] if idx >= 0 and idx < len(rules) else {}
    text = _format_rule_panel_text(rule, idx)
    return text, _build_rule_panel_keyboard(idx, rule)


def _clear_history_requests(user_id: str, rule_index: int) -> None:
    data = load_json(HISTORY_REQUESTS_FILE)
    if not isinstance(data, list):
        return
    remaining = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("user_id", "")) == str(user_id) and int(item.get("rule_index", -1)) == int(rule_index):
            continue
        remaining.append(item)
    save_json(HISTORY_REQUESTS_FILE, remaining)


def _clear_wizard(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("channel_config", None)


def start_channel_config_with_source(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    source_id: int,
    source_name: str = "",
    session_name: str = "",
):
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return None, None
    idx = _create_rule(
        user_id,
        source_id=int(source_id),
        source_name=source_name or "",
        session_name=session_name or "",
    )
    rules = _get_user_rules(user_id)
    rule = rules[idx] if idx >= 0 and idx < len(rules) else {}
    text = _format_rule_panel_text(rule, idx)
    return text, _build_rule_panel_keyboard(idx, rule)


def _build_filter_keyboard():
    rows = [
        [
            InlineKeyboardButton("全部", callback_data=f"{CALLBACK_PREFIX}:filter:all"),
            InlineKeyboardButton("文本", callback_data=f"{CALLBACK_PREFIX}:filter:text"),
        ],
        [
            InlineKeyboardButton("图片", callback_data=f"{CALLBACK_PREFIX}:filter:photo"),
            InlineKeyboardButton("视频", callback_data=f"{CALLBACK_PREFIX}:filter:video"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _build_enabled_keyboard(is_enabled: bool = True, *, index: Optional[int] = None) -> InlineKeyboardMarkup:
    label = "✅ 当前：开启" if is_enabled else "🚫 当前：关闭"
    toggle_action = "toggle_enabled"
    if index is not None:
        callback = f"{CALLBACK_PREFIX}:{toggle_action}:{index}"
    else:
        callback = f"{CALLBACK_PREFIX}:{toggle_action}"
    rows = [[InlineKeyboardButton(label, callback_data=callback)]]
    if index is None:
        rows.append([InlineKeyboardButton("➡️ 下一步", callback_data=f"{CALLBACK_PREFIX}:enabled_next")])
    return InlineKeyboardMarkup(rows)


def _build_mode_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("监听消息", callback_data=f"{CALLBACK_PREFIX}:mode:listen"),
            InlineKeyboardButton("转发历史", callback_data=f"{CALLBACK_PREFIX}:mode:history"),
        ]
    ]
    return InlineKeyboardMarkup(rows)


def _build_contact_keyboard(show_contact: bool = True, *, index: Optional[int] = None) -> InlineKeyboardMarkup:
    label = "✅ 当前：显示" if show_contact else "🚫 当前：不显示"
    toggle_action = "toggle_contact"
    if index is not None:
        callback = f"{CALLBACK_PREFIX}:{toggle_action}:{index}"
    else:
        callback = f"{CALLBACK_PREFIX}:{toggle_action}"
    rows = [[InlineKeyboardButton(label, callback_data=callback)]]
    if index is None:
        rows.append([InlineKeyboardButton("➡️ 下一步", callback_data=f"{CALLBACK_PREFIX}:contact_next")])
    return InlineKeyboardMarkup(rows)


def _build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:back"),
                InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:cancel"),
            ]
        ]
    )


def _build_panel_input_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:panel_back:{index}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:panel_cancel:{index}"),
            ]
        ]
    )


def _build_edit_filter_keyboard(index: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("全部", callback_data=f"{CALLBACK_PREFIX}:editfilter:{index}:all"),
            InlineKeyboardButton("文本", callback_data=f"{CALLBACK_PREFIX}:editfilter:{index}:text"),
        ],
        [
            InlineKeyboardButton("图片", callback_data=f"{CALLBACK_PREFIX}:editfilter:{index}:photo"),
            InlineKeyboardButton("视频", callback_data=f"{CALLBACK_PREFIX}:editfilter:{index}:video"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _build_session_select_keyboard(sessions: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for s in sessions:
        label = _get_session_label(s)
        rows.append([InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}:pick_session:{s}")])
    return InlineKeyboardMarkup(rows)


def _with_start_back(context: ContextTypes.DEFAULT_TYPE, keyboard: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    if not keyboard or not isinstance(keyboard, InlineKeyboardMarkup):
        return keyboard
    if context and context.user_data.get("start_panel"):
        rows = list(keyboard.inline_keyboard)
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
        keyboard = InlineKeyboardMarkup(rows)
    return keyboard


def _build_rule_list_view(user_id: str) -> tuple[str, InlineKeyboardMarkup]:
    rules = _get_user_rules(user_id)
    if not rules:
        return "暂无配置记录。", _build_main_menu_keyboard()
    lines = ["当前配置："]
    for idx, r in enumerate(rules, start=1):
        name = r.get("name", "")
        src = r.get("sources", [""])[0] if r.get("sources") else ""
        tgt = r.get("targets", [""])[0] if r.get("targets") else ""
        src_title = (r.get("source_title") or "").strip()
        tgt_title = (r.get("target_title") or "").strip()
        src_label = f"{src_title}({src})" if src_title and src else (src or src_title)
        tgt_label = f"{tgt_title}({tgt})" if tgt_title and tgt else (tgt or tgt_title)
        enabled = bool(r.get("enabled", True))
        mode = str(r.get("mode", "listen") or "listen").lower()
        mode_label = MODE_LABELS.get(mode, mode)
        session_name = r.get("session_name", "")
        session_label = _get_session_label(session_name) if session_name else "未设置"
        lines.append(
            f"{idx}. {name} | {src_label} → {tgt_label} | {'开启' if enabled else '关闭'} | {mode_label} | {session_label}"
        )
    keyboard_rows = []
    for idx in range(1, len(rules) + 1):
        keyboard_rows.append(
            [
                InlineKeyboardButton(f"管理 {idx}", callback_data=f"{CALLBACK_PREFIX}:panel:open:{idx-1}"),
                InlineKeyboardButton(f"🗑 删除 {idx}", callback_data=f"{CALLBACK_PREFIX}:del:{idx-1}"),
            ]
        )
    keyboard_rows.append(
        [
            InlineKeyboardButton("❓ 帮助", callback_data=f"{CALLBACK_PREFIX}:help"),
            InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:back"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def _build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ 添加规则", callback_data=f"{CALLBACK_PREFIX}:new")],
            [InlineKeyboardButton("📄 规则列表", callback_data=f"{CALLBACK_PREFIX}:list")],
            [InlineKeyboardButton("❓ 帮助", callback_data=f"{CALLBACK_PREFIX}:help")],
            [InlineKeyboardButton("🔁 切换小号", callback_data=f"{CALLBACK_PREFIX}:choose_session")],
        ]
    )


def _build_edit_menu_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ 频道名称", callback_data=f"{CALLBACK_PREFIX}:field:{index}:name")],
            [InlineKeyboardButton("✏️ 搬运频道ID", callback_data=f"{CALLBACK_PREFIX}:field:{index}:source_id")],
            [InlineKeyboardButton("✏️ 目标频道ID", callback_data=f"{CALLBACK_PREFIX}:field:{index}:target_id")],
            [InlineKeyboardButton("✏️ 任务模式", callback_data=f"{CALLBACK_PREFIX}:field:{index}:mode")],
            [InlineKeyboardButton("✏️ 任务类型", callback_data=f"{CALLBACK_PREFIX}:field:{index}:filter")],
            [InlineKeyboardButton("✏️ 协议号/小号", callback_data=f"{CALLBACK_PREFIX}:field:{index}:session_name")],
            [InlineKeyboardButton("开关切换", callback_data=f"{CALLBACK_PREFIX}:toggle_enabled:{index}")],
            [InlineKeyboardButton("联系方式切换", callback_data=f"{CALLBACK_PREFIX}:toggle_contact:{index}")],
            [InlineKeyboardButton("✏️ 频道用户名", callback_data=f"{CALLBACK_PREFIX}:field:{index}:channel_user")],
            [InlineKeyboardButton("✏️ 群名", callback_data=f"{CALLBACK_PREFIX}:field:{index}:group_name")],
            [InlineKeyboardButton("✏️ 投稿用户名", callback_data=f"{CALLBACK_PREFIX}:field:{index}:submit_user")],
            [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:back")],
        ]
    )


def _build_rule_panel_keyboard(index: int, rule: dict) -> InlineKeyboardMarkup:
    enabled = bool(rule.get("enabled", True))
    clear_links = bool(rule.get("clear_links", False))
    mode = str(rule.get("mode", "listen") or "listen").lower()
    en_on = "✅ 开启" if enabled else "开启"
    en_off = "✅ 关闭" if not enabled else "关闭"
    mode_listen = "✅ 监听消息" if mode == "listen" else "监听消息"
    mode_history = "✅ 转发历史" if mode == "history" else "转发历史"
    clear_on = "✅ 开启" if clear_links else "开启"
    clear_off = "✅ 关闭" if not clear_links else "关闭"
    rows = [
        [InlineKeyboardButton("设置备注", callback_data=f"{CALLBACK_PREFIX}:panel:remark:{index}")],
        [
            InlineKeyboardButton(f"状态: {en_on}", callback_data=f"{CALLBACK_PREFIX}:panel:enabled_on:{index}"),
            InlineKeyboardButton(f"状态: {en_off}", callback_data=f"{CALLBACK_PREFIX}:panel:enabled_off:{index}"),
        ],
        [
            InlineKeyboardButton(f"类型: {mode_listen}", callback_data=f"{CALLBACK_PREFIX}:panel:mode_listen:{index}"),
            InlineKeyboardButton(f"类型: {mode_history}", callback_data=f"{CALLBACK_PREFIX}:panel:mode_history:{index}"),
        ],
        [
            InlineKeyboardButton(f"清除链接: {clear_on}", callback_data=f"{CALLBACK_PREFIX}:panel:clear_on:{index}"),
            InlineKeyboardButton(f"清除链接: {clear_off}", callback_data=f"{CALLBACK_PREFIX}:panel:clear_off:{index}"),
        ],
        [InlineKeyboardButton("设置开始和结束消息id", callback_data=f"{CALLBACK_PREFIX}:panel:range:{index}")],
        [InlineKeyboardButton("开始历史转发", callback_data=f"{CALLBACK_PREFIX}:panel:history_start:{index}")],
        [
            InlineKeyboardButton("设置监听频道id", callback_data=f"{CALLBACK_PREFIX}:panel:source:{index}"),
            InlineKeyboardButton("设置目标频道id", callback_data=f"{CALLBACK_PREFIX}:panel:target:{index}"),
        ],
        [InlineKeyboardButton("设置协议号/小号", callback_data=f"{CALLBACK_PREFIX}:panel:session:{index}")],
        [InlineKeyboardButton("设置包含词", callback_data=f"{CALLBACK_PREFIX}:panel:include:{index}")],
        [
            InlineKeyboardButton("设置替换词", callback_data=f"{CALLBACK_PREFIX}:panel:replace:{index}"),
            InlineKeyboardButton("设置屏蔽词", callback_data=f"{CALLBACK_PREFIX}:panel:block:{index}"),
        ],
        [
            InlineKeyboardButton("设置截取词", callback_data=f"{CALLBACK_PREFIX}:panel:cut:{index}"),
            InlineKeyboardButton("设置自定义后缀", callback_data=f"{CALLBACK_PREFIX}:panel:suffix:{index}"),
        ],
        [
            InlineKeyboardButton("媒体替换", callback_data=f"{CALLBACK_PREFIX}:panel:media:{index}"),
            InlineKeyboardButton("消息处理速度", callback_data=f"{CALLBACK_PREFIX}:panel:speed:{index}"),
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:list")],
    ]
    return InlineKeyboardMarkup(rows)


def _format_rule_panel_text(rule: dict, index: int) -> str:
    name = rule.get("name", "") or f"规则 {index + 1}"
    enabled = "开启" if bool(rule.get("enabled", True)) else "关闭"
    mode = MODE_LABELS.get(str(rule.get("mode", "listen")), "监听消息")
    clear_links = "开启" if bool(rule.get("clear_links", False)) else "关闭"
    sources = rule.get("sources", []) or []
    targets = rule.get("targets", []) or []
    source_title = (rule.get("source_title") or "").strip()
    target_title = (rule.get("target_title") or "").strip()
    src_id = sources[0] if sources else ""
    tgt_id = targets[0] if targets else ""
    src_label = f"{source_title} ({src_id})" if source_title and src_id else (src_id or source_title)
    tgt_label = f"{target_title} ({tgt_id})" if target_title and tgt_id else (tgt_id or target_title)
    start_id = rule.get("start_id", "")
    end_id = rule.get("end_id", "")
    include_words = rule.get("include_words", []) or []
    block_words = rule.get("block_words", []) or []
    replace_words = rule.get("replace_words", []) or []
    cut_words = rule.get("cut_words", "")
    suffix = rule.get("suffix", "")
    media_replace = rule.get("media_replace", "")
    speed = rule.get("speed", "")
    session_name = rule.get("session_name", "")
    return (
        f"规则：{name}\n"
        f"状态：{enabled}\n"
        f"任务类型：{mode}\n"
        f"清除链接：{clear_links}\n"
        f"监听频道：{src_label or sources}\n"
        f"目标频道：{tgt_label or targets}\n"
        f"协议号：{session_name}\n"
        f"开始/结束消息ID：{start_id} / {end_id}\n"
        f"包含词：{include_words}\n"
        f"屏蔽词：{block_words}\n"
        f"替换词：{replace_words}\n"
        f"截取词：{cut_words}\n"
        f"自定义后缀：{suffix}\n"
        f"媒体替换：{media_replace}\n"
        f"消息处理速度：{speed}"
    )


def _format_draft_summary(draft: dict) -> str:
    show_contact = bool(draft.get("show_contact", True))
    enabled = bool(draft.get("enabled", True))
    mode = str(draft.get("mode", "listen") or "listen").lower()
    session_name = draft.get("session_name", "")
    src_id = draft.get("source_id", "")
    tgt_id = draft.get("target_id", "")
    src_title = (draft.get("source_title") or "").strip()
    tgt_title = (draft.get("target_title") or "").strip()
    src_label = f"{src_title} ({src_id})" if src_title and src_id else (src_id or src_title)
    tgt_label = f"{tgt_title} ({tgt_id})" if tgt_title and tgt_id else (tgt_id or tgt_title)
    return (
        "请确认配置：\n"
        f"频道名称：{draft.get('name', '')}\n"
        f"搬运频道：{src_label}\n"
        f"目标频道：{tgt_label}\n"
        f"是否开启：{'开启' if enabled else '关闭'}\n"
        f"任务模式：{MODE_LABELS.get(mode, mode)}\n"
        f"任务类型：{FILTER_LABELS.get(draft.get('filter', 'all'), draft.get('filter', 'all'))}\n"
        f"显示联系方式：{'是' if show_contact else '否'}\n"
        f"频道用户名：{draft.get('channel_user', '')}\n"
        f"群名：{draft.get('group_name', '')}\n"
        f"投稿用户名：{draft.get('submit_user', '')}"
        + (f"\n协议号：{session_name}" if session_name else "")
    )


def _save_forward_rule(draft: dict, user_id: str, username: Optional[str] = None) -> None:
    rule = {
        "name": draft.get("name", ""),
        "sources": [draft.get("source_id")],
        "targets": [draft.get("target_id")],
        "source_title": draft.get("source_title", ""),
        "target_title": draft.get("target_title", ""),
        "enabled": bool(draft.get("enabled", True)),
        "mode": str(draft.get("mode", "listen") or "listen").lower(),
        "filter": draft.get("filter", "all"),
        "exclude_channels": [],
        "show_contact": bool(draft.get("show_contact", True)),
        "replace_channel_user": draft.get("channel_user", ""),
        "replace_group_name": draft.get("group_name", ""),
        "replace_submit_user": draft.get("submit_user", ""),
    }
    session_name = draft.get("session_name")
    if session_name:
        rule["session_name"] = session_name
    rules = _get_user_rules(user_id)
    rules.append(rule)
    _set_user_rules(user_id, rules, username=username)


def _update_rule_field(user_id: str, index: int, field: str, value) -> bool:
    rules = _get_user_rules(user_id)
    if index < 0 or index >= len(rules):
        return False
    rule = rules[index]
    if field in {"name", "filter", "replace_channel_user", "replace_group_name", "replace_submit_user"}:
        rule[field] = value
    elif field == "enabled":
        rule["enabled"] = bool(value)
    elif field == "mode":
        mode = str(value or "listen").lower()
        if mode not in MODE_LABELS:
            return False
        rule["mode"] = mode
    elif field == "clear_links":
        rule["clear_links"] = bool(value)
    elif field == "start_id":
        rule["start_id"] = value
    elif field == "end_id":
        rule["end_id"] = value
    elif field == "include_words":
        rule["include_words"] = value
    elif field == "replace_words":
        rule["replace_words"] = value
    elif field == "block_words":
        rule["block_words"] = value
    elif field == "cut_words":
        rule["cut_words"] = value
    elif field == "suffix":
        rule["suffix"] = value
    elif field == "media_replace":
        rule["media_replace"] = value
    elif field == "speed":
        rule["speed"] = value
    elif field == "show_contact":
        rule["show_contact"] = bool(value)
    elif field == "source_id":
        rule["sources"] = [value]
    elif field == "target_id":
        rule["targets"] = [value]
    elif field == "source_title":
        rule["source_title"] = value or ""
    elif field == "target_title":
        rule["target_title"] = value or ""
    elif field == "channel_user":
        rule["replace_channel_user"] = value
    elif field == "group_name":
        rule["replace_group_name"] = value
    elif field == "submit_user":
        rule["replace_submit_user"] = value
    elif field == "session_name":
        rule["session_name"] = str(value or "").strip()
    else:
        return False
    rules[index] = rule
    _set_user_rules(user_id, rules)
    return True


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    if not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return

    await query.answer()

    if not _require_access(update):
        return await query.edit_message_text("🚫 仅高级管理员或订阅会员可使用该功能。")

    _record_bot_user(update.effective_user)

    chat_type = (query.message.chat.type or "").lower() if query.message else ""
    if chat_type != "private":
        return await query.edit_message_text("请私聊机器人发送“频道配置”。")

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "choose_session":
        sessions = _list_accessible_sessions(context, update.effective_user)
        if not sessions:
            return await query.edit_message_text(
                "暂无可用小号。", reply_markup=_with_start_back(context, _build_main_menu_keyboard())
            )
        context.user_data["channel_config_session_select"] = {"next": "main"}
        return await query.edit_message_text(
            "请选择要配置的小号：",
            reply_markup=_with_start_back(context, _build_session_select_keyboard(sessions)),
        )

    if action == "pick_session" and len(parts) >= 3:
        session_name = parts[2]
        sessions = _list_accessible_sessions(context, update.effective_user)
        if session_name not in sessions:
            return await query.edit_message_text(
                "❗ 小号无效或无权限。", reply_markup=_with_start_back(context, _build_main_menu_keyboard())
            )
        context.user_data["channel_config_default_session"] = session_name
        pending = context.user_data.pop("channel_config_session_select", {}) or {}
        if pending.get("next") == "panel":
            idx = int(pending.get("index", -1))
            user_id = str(update.effective_user.id)
            ok = _update_rule_field(user_id, idx, "session_name", session_name)
            if not ok:
                return await query.edit_message_text("❗ 更新失败，请重试。")
            rules = _get_user_rules(user_id)
            rule = rules[idx] if 0 <= idx < len(rules) else {}
            return await query.edit_message_text(
                "✅ 已更新协议号。\n\n" + _format_rule_panel_text(rule, idx),
                reply_markup=_build_rule_panel_keyboard(idx, rule),
            )
        if pending.get("next") == "new":
            user_id = str(update.effective_user.id)
            idx = _create_rule(user_id, session_name=session_name)
            rules = _get_user_rules(user_id)
            rule = rules[idx] if idx >= 0 and idx < len(rules) else {}
            return await query.edit_message_text(
                _format_rule_panel_text(rule, idx),
                reply_markup=_build_rule_panel_keyboard(idx, rule),
            )
        return await query.edit_message_text(
            f"已选择小号：{_get_session_label(session_name)}\n请选择操作：",
            reply_markup=_with_start_back(context, _build_main_menu_keyboard()),
        )

    if action == "new":
        user_id = str(update.effective_user.id)
        if not is_super_admin(update.effective_user.id):
            rules = _get_user_rules(user_id)
            if len(rules) >= SUBSCRIBER_MAX_RULES:
                return await query.edit_message_text(
                    f"⚠️ 订阅会员最多可配置 {SUBSCRIBER_MAX_RULES} 条规则。"
                )
        sessions = _list_accessible_sessions(context, update.effective_user)
        session_name = context.user_data.get("channel_config_default_session", "")
        if sessions and not session_name:
            context.user_data["channel_config_session_select"] = {"next": "new"}
            return await query.edit_message_text(
                "请选择要配置的小号：",
                reply_markup=_with_start_back(context, _build_session_select_keyboard(sessions)),
            )
        idx = _create_rule(user_id, session_name=session_name or "")
        rules = _get_user_rules(user_id)
        rule = rules[idx] if idx >= 0 and idx < len(rules) else {}
        return await query.edit_message_text(
            _format_rule_panel_text(rule, idx),
            reply_markup=_build_rule_panel_keyboard(idx, rule),
        )

    if action == "list":
        user_id = str(update.effective_user.id)
        text, keyboard = _build_rule_list_view(user_id)
        return await query.edit_message_text(text, reply_markup=_with_start_back(context, keyboard))

    if action == "cancel":
        _clear_wizard(context)
        return await query.edit_message_text("已取消。", reply_markup=_with_start_back(context, _build_main_menu_keyboard()))

    if action == "back":
        sessions = _list_accessible_sessions(context, update.effective_user)
        if sessions:
            context.user_data["channel_config_session_select"] = {"next": "main"}
            return await query.edit_message_text(
                "请选择要配置的小号：",
                reply_markup=_with_start_back(context, _build_session_select_keyboard(sessions)),
            )
        return await query.edit_message_text(
            "请选择操作：\n\n“新建配置”会按步骤引导填写频道名称、ID、任务类型、任务模式等。",
            reply_markup=_with_start_back(context, _build_main_menu_keyboard()),
        )

    if action == "help":
        help_text = (
            "📌 频道配置帮助\n"
            "1. 获取频道ID：私聊机器人转发一条频道消息，机器人会回复频道ID。\n"
            "2. 搬运频道ID：填写来源频道ID（如 -1001234567890）。\n"
            "3. 目标频道ID：填写要发布的目标频道ID。\n"
            "4. 是否开启：决定是否执行该规则。\n"
            "5. 任务模式：监听消息 / 转发历史（历史模式暂不执行）。\n"
            "6. 任务类型：全部/文本/图片/视频。\n"
            "7. 联系方式：选择显示或不显示；不显示会跳过用户名配置。\n"
            "8. 频道用户名/群名/投稿用户名：用于替换文本中的联系方式。\n"
            "9. 协议号/小号：用于指定使用哪个登录的小号执行转发。\n"
        )
        return await query.edit_message_text(
            help_text, reply_markup=_with_start_back(context, _build_main_menu_keyboard())
        )

    if action == "panel" and len(parts) >= 4:
        sub = parts[2]
        try:
            idx = int(parts[3])
        except Exception:
            return await query.edit_message_text("❗ 无效的规则序号。")
        user_id = str(update.effective_user.id)
        rules = _get_user_rules(user_id)
        if idx < 0 or idx >= len(rules):
            return await query.edit_message_text("❗ 无效的规则序号。")
        rule = rules[idx]

        if sub == "open":
            return await query.edit_message_text(
                _format_rule_panel_text(rule, idx),
                reply_markup=_build_rule_panel_keyboard(idx, rule),
            )
        if sub == "enabled_on":
            _update_rule_field(user_id, idx, "enabled", True)
        elif sub == "enabled_off":
            _update_rule_field(user_id, idx, "enabled", False)
            _clear_history_requests(user_id, idx)
        elif sub == "mode_listen":
            _update_rule_field(user_id, idx, "mode", "listen")
            _clear_history_requests(user_id, idx)
        elif sub == "mode_history":
            _update_rule_field(user_id, idx, "mode", "history")
        elif sub == "clear_on":
            _update_rule_field(user_id, idx, "clear_links", True)
        elif sub == "clear_off":
            _update_rule_field(user_id, idx, "clear_links", False)
        elif sub == "history_start":
            requests = load_json(HISTORY_REQUESTS_FILE)
            if not isinstance(requests, list):
                requests = []
            requests.append(
                {
                    "user_id": user_id,
                    "rule_index": idx,
                    "rule": rule,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            save_json(HISTORY_REQUESTS_FILE, requests)
        elif sub == "session":
            sessions = _list_accessible_sessions(context, update.effective_user)
            if not sessions:
                return await query.edit_message_text("暂无可用小号。")
            context.user_data["channel_config_session_select"] = {"next": "panel", "index": idx}
            return await query.edit_message_text(
                "请选择要配置的小号：",
                reply_markup=_build_session_select_keyboard(sessions),
            )
        elif sub in {"remark", "range", "source", "target", "include", "replace", "block", "cut", "suffix", "media", "speed"}:
            context.user_data["channel_config_panel"] = {
                "index": idx,
                "field": sub,
            }
            prompt_map = {
                "remark": "请输入备注（频道名称）：",
                "range": "请输入开始与结束消息ID（例如：100 200，或仅填一个数字）：",
                "source": "请输入监听频道ID（数字ID）：",
                "target": "请输入目标频道ID（数字ID）：",
                "include": "请输入包含词（多个用逗号分隔）：",
                "replace": "请输入替换词（每行：旧词=新词）：",
                "block": "请输入屏蔽词（多个用逗号分隔）：",
                "cut": "请输入截取词（可多条，逗号或换行分隔；格式：开始词|结束词；只填一个词=从该词开始删除到末尾；可用“+ / 追加 / append”追加，默认覆盖；发送“清空”删除设置）：",
                "suffix": "请输入自定义后缀（可用“+ / 追加 / append”追加，默认覆盖；发送“清空”删除设置）：",
                "media": "请输入媒体替换配置（原样保存）：",
                "speed": "请输入消息处理速度（毫秒或秒数）：",
            }
            return await query.edit_message_text(
                prompt_map.get(sub, "请输入内容："),
                reply_markup=_build_panel_input_keyboard(idx),
            )

        rules = _get_user_rules(user_id)
        rule = rules[idx]
        return await query.edit_message_text(
            _format_rule_panel_text(rule, idx),
            reply_markup=_build_rule_panel_keyboard(idx, rule),
        )

    if action == "filter" and len(parts) >= 3:
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        fval = parts[2]
        state["draft"]["filter"] = fval
        state["stage"] = "contact"
        return await query.edit_message_text(
            "是否显示底部联系方式？",
            reply_markup=InlineKeyboardMarkup(
                _build_contact_keyboard(bool(state["draft"].get("show_contact", True))).inline_keyboard
                + _build_cancel_keyboard().inline_keyboard
            ),
        )

    if action == "toggle_enabled":
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        idx = None
        if len(parts) >= 3 and parts[2].isdigit():
            idx = int(parts[2])
        if idx is not None:
            user_id = str(update.effective_user.id)
            rules = _get_user_rules(user_id)
            if idx < 0 or idx >= len(rules):
                return await query.edit_message_text("❗ 无效的编辑序号。")
            rule = rules[idx]
            new_enabled = not bool(rule.get("enabled", True))
            ok = _update_rule_field(user_id, idx, "enabled", new_enabled)
            if not ok:
                return await query.edit_message_text("❗ 更新失败，请重试。")
            rules = _get_user_rules(user_id)
            rule = rules[idx]
            draft = state.get("draft", {})
            draft.update(
                {
                    "name": rule.get("name", ""),
                    "source_id": (rule.get("sources") or [""])[0],
                    "target_id": (rule.get("targets") or [""])[0],
                    "enabled": bool(rule.get("enabled", True)),
                    "mode": rule.get("mode", "listen"),
                    "filter": rule.get("filter", "all"),
                    "show_contact": bool(rule.get("show_contact", True)),
                    "channel_user": rule.get("replace_channel_user", ""),
                    "group_name": rule.get("replace_group_name", ""),
                    "submit_user": rule.get("replace_submit_user", ""),
                    "session_name": rule.get("session_name", ""),
                    "source_title": rule.get("source_title", ""),
                    "target_title": rule.get("target_title", ""),
                    "edit_index": idx,
                }
            )
            state["stage"] = "edit_menu"
            return await query.edit_message_text(
                "✅ 已更新开启状态。\n\n" + _format_draft_summary(draft),
                reply_markup=_build_edit_menu_keyboard(idx),
            )

        state["draft"]["enabled"] = not bool(state["draft"].get("enabled", True))
        return await query.edit_message_text(
            "是否开启该规则？",
            reply_markup=InlineKeyboardMarkup(
                _build_enabled_keyboard(bool(state["draft"].get("enabled", True))).inline_keyboard
                + _build_cancel_keyboard().inline_keyboard
            ),
        )

    if action == "enabled_next":
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        state["stage"] = "mode"
        return await query.edit_message_text(
            "请选择任务模式：",
            reply_markup=InlineKeyboardMarkup(
                _build_mode_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )

    if action == "mode" and len(parts) >= 3:
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        mode = parts[2]
        if mode not in MODE_LABELS:
            mode = "listen"
        if state.get("stage") == "edit_mode":
            user_id = str(update.effective_user.id)
            idx = int(state.get("draft", {}).get("edit_index", -1))
            ok = _update_rule_field(user_id, idx, "mode", mode)
            if not ok:
                return await query.edit_message_text("❗ 更新失败，请重试。")
            rules = _get_user_rules(user_id)
            rule = rules[idx]
            draft = state.get("draft", {})
            draft.update(
                {
                    "name": rule.get("name", ""),
                    "source_id": (rule.get("sources") or [""])[0],
                    "target_id": (rule.get("targets") or [""])[0],
                    "enabled": bool(rule.get("enabled", True)),
                    "mode": rule.get("mode", "listen"),
                    "filter": rule.get("filter", "all"),
                    "show_contact": bool(rule.get("show_contact", True)),
                    "channel_user": rule.get("replace_channel_user", ""),
                    "group_name": rule.get("replace_group_name", ""),
                    "submit_user": rule.get("replace_submit_user", ""),
                    "session_name": rule.get("session_name", ""),
                    "source_title": rule.get("source_title", ""),
                    "target_title": rule.get("target_title", ""),
                    "edit_index": idx,
                }
            )
            state["stage"] = "edit_menu"
            return await query.edit_message_text(
                "✅ 已更新任务模式。\n\n" + _format_draft_summary(draft),
                reply_markup=_build_edit_menu_keyboard(idx),
            )

        state["draft"]["mode"] = mode
        state["stage"] = "filter"
        return await query.edit_message_text(
            "请选择任务类型：",
            reply_markup=InlineKeyboardMarkup(
                _build_filter_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )

    if action == "toggle_contact":
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        idx = None
        if len(parts) >= 3 and parts[2].isdigit():
            idx = int(parts[2])
        if idx is not None:
            user_id = str(update.effective_user.id)
            rules = _get_user_rules(user_id)
            if idx < 0 or idx >= len(rules):
                return await query.edit_message_text("❗ 无效的编辑序号。")
            rule = rules[idx]
            new_show = not bool(rule.get("show_contact", True))
            ok = _update_rule_field(user_id, idx, "show_contact", new_show)
            if not ok:
                return await query.edit_message_text("❗ 更新失败，请重试。")
            if not new_show:
                _update_rule_field(user_id, idx, "channel_user", "")
                _update_rule_field(user_id, idx, "group_name", "")
                _update_rule_field(user_id, idx, "submit_user", "")
            rules = _get_user_rules(user_id)
            rule = rules[idx]
            draft = state.get("draft", {})
            draft.update(
                {
                    "name": rule.get("name", ""),
                    "source_id": (rule.get("sources") or [""])[0],
                    "target_id": (rule.get("targets") or [""])[0],
                    "enabled": bool(rule.get("enabled", True)),
                    "mode": rule.get("mode", "listen"),
                    "filter": rule.get("filter", "all"),
                    "show_contact": bool(rule.get("show_contact", True)),
                    "channel_user": rule.get("replace_channel_user", ""),
                    "group_name": rule.get("replace_group_name", ""),
                    "submit_user": rule.get("replace_submit_user", ""),
                    "session_name": rule.get("session_name", ""),
                    "source_title": rule.get("source_title", ""),
                    "target_title": rule.get("target_title", ""),
                    "edit_index": idx,
                }
            )
            state["stage"] = "edit_menu"
            return await query.edit_message_text(
                "✅ 已更新显示设置。\n\n" + _format_draft_summary(draft),
                reply_markup=_build_edit_menu_keyboard(idx),
            )

        state["draft"]["show_contact"] = not bool(state["draft"].get("show_contact", True))
        return await query.edit_message_text(
            "是否显示底部联系方式？",
            reply_markup=InlineKeyboardMarkup(
                _build_contact_keyboard(bool(state["draft"].get("show_contact", True))).inline_keyboard
                + _build_cancel_keyboard().inline_keyboard
            ),
        )

    if action == "contact_next":
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        show_contact = bool(state["draft"].get("show_contact", True))
        if show_contact:
            state["stage"] = "channel_user"
            return await query.edit_message_text("请输入频道用户名（如 @sdxwjs）：", reply_markup=_build_cancel_keyboard())
        state["draft"]["channel_user"] = ""
        state["draft"]["group_name"] = ""
        state["draft"]["submit_user"] = ""
        state["stage"] = "confirm"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ 保存", callback_data=f"{CALLBACK_PREFIX}:save")],
                [InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:cancel")],
            ]
        )
        return await query.edit_message_text(_format_draft_summary(state["draft"]), reply_markup=keyboard)

    if action == "save":
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("没有待保存的配置。")
        draft = state.get("draft", {})
        required = ["name", "source_id", "target_id", "enabled", "mode", "filter", "show_contact"]
        if draft.get("show_contact", True):
            required += ["channel_user", "group_name", "submit_user"]
        for key in required:
            if key not in draft:
                return await query.edit_message_text("配置未完整，请重新开始。")
            if key in {"enabled"}:
                continue
            if not draft.get(key):
                return await query.edit_message_text("配置未完整，请重新开始。")
        user_id = str(update.effective_user.id)
        _save_forward_rule(draft, user_id, username=draft.get("owner_username"))
        _clear_wizard(context)
        return await query.edit_message_text(
            "✅ 已保存频道配置。",
            reply_markup=_build_main_menu_keyboard(),
        )

    if action == "del" and len(parts) >= 3:
        user_id = str(update.effective_user.id)
        try:
            idx = int(parts[2])
        except Exception:
            return await query.edit_message_text("❗ 无效的删除序号。")
        rules = _get_user_rules(user_id)
        if idx < 0 or idx >= len(rules):
            return await query.edit_message_text("❗ 无效的删除序号。")
        removed = rules.pop(idx)
        user_cfg = _load_user_forward_config().get("users", {}).get(user_id, {})
        _set_user_rules(user_id, rules, username=user_cfg.get("username") if isinstance(user_cfg, dict) else None)
        name = removed.get("name", "")
        text, keyboard = _build_rule_list_view(user_id)
        text = f"✅ 已删除配置：{name}\n\n{text}" if text else f"✅ 已删除配置：{name}"
        return await query.edit_message_text(text, reply_markup=_with_start_back(context, keyboard))

    if action == "edit" and len(parts) >= 3:
        user_id = str(update.effective_user.id)
        try:
            idx = int(parts[2])
        except Exception:
            return await query.edit_message_text("❗ 无效的编辑序号。")
        rules = _get_user_rules(user_id)
        if idx < 0 or idx >= len(rules):
            return await query.edit_message_text("❗ 无效的编辑序号。")
        rule = rules[idx]
        draft = {
            "name": rule.get("name", ""),
            "source_id": (rule.get("sources") or [""])[0],
            "target_id": (rule.get("targets") or [""])[0],
            "enabled": bool(rule.get("enabled", True)),
            "mode": rule.get("mode", "listen"),
            "filter": rule.get("filter", "all"),
            "show_contact": bool(rule.get("show_contact", True)),
            "channel_user": rule.get("replace_channel_user", ""),
            "group_name": rule.get("replace_group_name", ""),
            "submit_user": rule.get("replace_submit_user", ""),
            "session_name": rule.get("session_name", ""),
            "source_title": rule.get("source_title", ""),
            "target_title": rule.get("target_title", ""),
            "owner_id": user_id,
            "edit_index": idx,
        }
        context.user_data["channel_config"] = {"stage": "edit_menu", "draft": draft}
        return await query.edit_message_text(
            _format_draft_summary(draft),
            reply_markup=_build_edit_menu_keyboard(idx),
        )

    if action == "field" and len(parts) >= 4:
        user_id = str(update.effective_user.id)
        try:
            idx = int(parts[2])
        except Exception:
            return await query.edit_message_text("❗ 无效的编辑序号。")
        field = parts[3]
        state = context.user_data.get("channel_config")
        if not state or state.get("stage") not in {"edit_menu", "edit_field"}:
            return await query.edit_message_text("请先选择要编辑的配置。")
        state["stage"] = f"edit_{field}"
        state["draft"]["edit_index"] = idx
        if field == "filter":
            return await query.edit_message_text("请选择新的任务类型：", reply_markup=_build_edit_filter_keyboard(idx))
        if field == "mode":
            return await query.edit_message_text("请选择新的任务模式：", reply_markup=_build_mode_keyboard())
        if field == "show_contact":
            return await query.edit_message_text(
                "是否显示底部联系方式？",
                reply_markup=_build_contact_keyboard(bool(state["draft"].get("show_contact", True)), index=idx),
            )
        label_map = {
            "name": "请输入新的频道名称：",
            "source_id": "请输入新的搬运频道ID（数字ID）：",
            "target_id": "请输入新的目标频道ID（数字ID）：",
            "channel_user": "请输入新的频道用户名（如 @gaoxiaoma）：",
            "group_name": "请输入新的群名（如 @U10000）：",
            "submit_user": "请输入新的投稿用户名（如 @mr566）：",
            "session_name": "请输入新的协议号/小号（session_name）：",
        }
        return await query.edit_message_text(label_map.get(field, "请输入新的值："))

    if action == "editfilter" and len(parts) >= 4:
        user_id = str(update.effective_user.id)
        try:
            idx = int(parts[2])
        except Exception:
            return await query.edit_message_text("❗ 无效的编辑序号。")
        fval = parts[3]
        if fval not in {"all", "text", "photo", "video"}:
            fval = "all"
        ok = _update_rule_field(user_id, idx, "filter", fval)
        if not ok:
            return await query.edit_message_text("❗ 更新失败，请重试。")
        rules = _get_user_rules(user_id)
        rule = rules[idx]
        draft = {
            "name": rule.get("name", ""),
            "source_id": (rule.get("sources") or [""])[0],
            "target_id": (rule.get("targets") or [""])[0],
            "enabled": bool(rule.get("enabled", True)),
            "mode": rule.get("mode", "listen"),
            "filter": rule.get("filter", "all"),
            "show_contact": bool(rule.get("show_contact", True)),
            "channel_user": rule.get("replace_channel_user", ""),
            "group_name": rule.get("replace_group_name", ""),
            "submit_user": rule.get("replace_submit_user", ""),
            "session_name": rule.get("session_name", ""),
            "source_title": rule.get("source_title", ""),
            "target_title": rule.get("target_title", ""),
            "owner_id": user_id,
            "edit_index": idx,
        }
        context.user_data["channel_config"] = {"stage": "edit_menu", "draft": draft}
        return await query.edit_message_text(
            "✅ 已更新任务类型。\n\n" + _format_draft_summary(draft),
            reply_markup=_build_edit_menu_keyboard(idx),
        )

    if action == "panel_back" and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except Exception:
            return await query.edit_message_text("❗ 无效的规则序号。")
        user_id = str(update.effective_user.id)
        rules = _get_user_rules(user_id)
        if idx < 0 or idx >= len(rules):
            return await query.edit_message_text("❗ 无效的规则序号。")
        rule = rules[idx]
        return await query.edit_message_text(
            _format_rule_panel_text(rule, idx),
            reply_markup=_build_rule_panel_keyboard(idx, rule),
        )

    if action == "panel_cancel" and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except Exception:
            return await query.edit_message_text("❗ 无效的规则序号。")
        user_id = str(update.effective_user.id)
        rules = _get_user_rules(user_id)
        if idx < 0 or idx >= len(rules):
            return await query.edit_message_text("❗ 无效的规则序号。")
        rule = rules[idx]
        return await query.edit_message_text(
            _format_rule_panel_text(rule, idx),
            reply_markup=_build_rule_panel_keyboard(idx, rule),
        )


async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False

    panel_state = context.user_data.get("channel_config_panel")
    if panel_state:
        user_id = str(update.effective_user.id)
        idx = int(panel_state.get("index", -1))
        field = panel_state.get("field")
        text = update.message.text.strip()

        def _parse_merge_mode(raw: str) -> tuple[str, str]:
            if not raw:
                return "replace", raw
            lower = raw.lower()
            if raw.startswith("+") or lower.startswith("追加") or lower.startswith("append"):
                cleaned = raw.lstrip("+").strip()
                cleaned = cleaned[2:].strip() if cleaned.startswith("追加") else cleaned
                if cleaned.lower().startswith("append"):
                    cleaned = cleaned[6:].strip(" :：")
                return "append", cleaned
            if lower.startswith("覆盖") or lower.startswith("替换") or lower.startswith("replace"):
                cleaned = raw
                if cleaned.startswith("覆盖"):
                    cleaned = cleaned[2:].strip()
                elif cleaned.startswith("替换"):
                    cleaned = cleaned[2:].strip()
                elif cleaned.lower().startswith("replace"):
                    cleaned = cleaned[7:].strip(" :：")
                return "replace", cleaned
            return "replace", raw

        def _merge_words(existing: list, new_items: list) -> list:
            seen = set()
            merged = []
            for w in (existing or []) + (new_items or []):
                w = (w or "").strip()
                if not w or w in seen:
                    continue
                seen.add(w)
                merged.append(w)
            return merged

        def _merge_replace_pairs(existing: list, new_items: list) -> list:
            merged = []
            index = {}
            for item in existing or []:
                k = (item or {}).get("from")
                if not k:
                    continue
                index[k] = len(merged)
                merged.append(item)
            for item in new_items or []:
                k = (item or {}).get("from")
                if not k:
                    continue
                if k in index:
                    merged[index[k]] = item
                else:
                    index[k] = len(merged)
                    merged.append(item)
            return merged
        if idx < 0:
            context.user_data.pop("channel_config_panel", None)
            return False
        if field == "remark":
            _update_rule_field(user_id, idx, "name", text)
        elif field == "range":
            parts = text.split()
            start_id = parts[0] if parts else ""
            end_id = parts[1] if len(parts) > 1 else ""
            _update_rule_field(user_id, idx, "start_id", start_id)
            _update_rule_field(user_id, idx, "end_id", end_id)
        elif field == "source":
            try:
                source_id = int(text)
                _update_rule_field(user_id, idx, "source_id", source_id)
                title = await _resolve_chat_title(context, source_id)
                _update_rule_field(user_id, idx, "source_title", title or "")
            except Exception:
                await update.message.reply_text("❗ 请输入正确的频道ID（数字）。")
                return True
        elif field == "target":
            try:
                target_id = int(text)
                _update_rule_field(user_id, idx, "target_id", target_id)
                title = await _resolve_chat_title(context, target_id)
                _update_rule_field(user_id, idx, "target_title", title or "")
            except Exception:
                await update.message.reply_text("❗ 请输入正确的频道ID（数字）。")
                return True
        elif field == "include":
            mode, cleaned = _parse_merge_mode(text)
            if cleaned in {"清空", "清除", "删除", "无", "-"}:
                _update_rule_field(user_id, idx, "include_words", [])
            else:
                words = [w.strip() for w in cleaned.split(",") if w.strip()]
                if mode == "append":
                    rules = _get_user_rules(user_id)
                    rule = rules[idx] if 0 <= idx < len(rules) else {}
                    merged = _merge_words(rule.get("include_words") or [], words)
                    _update_rule_field(user_id, idx, "include_words", merged)
                else:
                    _update_rule_field(user_id, idx, "include_words", words)
        elif field == "replace":
            mode, cleaned = _parse_merge_mode(text)
            pairs = []
            for line in cleaned.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "=>" in line:
                    left, right = line.split("=>", 1)
                elif "->" in line:
                    left, right = line.split("->", 1)
                elif "=" in line:
                    left, right = line.split("=", 1)
                else:
                    parts = line.split()
                    if len(parts) >= 2:
                        left, right = parts[0], " ".join(parts[1:])
                    else:
                        continue
                pairs.append({"from": left.strip(), "to": right.strip()})
            if cleaned in {"清空", "清除", "删除", "无", "-"}:
                _update_rule_field(user_id, idx, "replace_words", [])
            elif mode == "append":
                rules = _get_user_rules(user_id)
                rule = rules[idx] if 0 <= idx < len(rules) else {}
                merged = _merge_replace_pairs(rule.get("replace_words") or [], pairs)
                _update_rule_field(user_id, idx, "replace_words", merged)
            else:
                _update_rule_field(user_id, idx, "replace_words", pairs)
        elif field == "block":
            mode, cleaned = _parse_merge_mode(text)
            if cleaned in {"清空", "清除", "删除", "无", "-"}:
                _update_rule_field(user_id, idx, "block_words", [])
            else:
                words = [w.strip() for w in cleaned.split(",") if w.strip()]
                if mode == "append":
                    rules = _get_user_rules(user_id)
                    rule = rules[idx] if 0 <= idx < len(rules) else {}
                    merged = _merge_words(rule.get("block_words") or [], words)
                    _update_rule_field(user_id, idx, "block_words", merged)
                else:
                    _update_rule_field(user_id, idx, "block_words", words)
        elif field == "cut":
            mode, cleaned = _parse_merge_mode(text)
            if cleaned in {"清空", "清除", "删除", "无", "-"}:
                _update_rule_field(user_id, idx, "cut_words", [])
            else:
                parts = []
                for line in cleaned.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts.extend([p.strip() for p in line.split(",") if p.strip()])
                if mode == "append":
                    rules = _get_user_rules(user_id)
                    rule = rules[idx] if 0 <= idx < len(rules) else {}
                    existing = rule.get("cut_words") or []
                    if isinstance(existing, str):
                        existing = [existing] if existing else []
                    merged = _merge_words(existing, parts)
                    _update_rule_field(user_id, idx, "cut_words", merged)
                else:
                    _update_rule_field(user_id, idx, "cut_words", parts)
        elif field == "suffix":
            mode, cleaned = _parse_merge_mode(text)
            if cleaned in {"清空", "清除", "删除", "无", "-"}:
                _update_rule_field(user_id, idx, "suffix", "")
            else:
                cleaned = _normalize_newlines(cleaned)
                if mode == "append":
                    rules = _get_user_rules(user_id)
                    rule = rules[idx] if 0 <= idx < len(rules) else {}
                    existing = _normalize_newlines(str(rule.get("suffix", "") or ""))
                    new_suffix = f"{existing}\n{cleaned}" if existing and cleaned else (existing or cleaned)
                    new_suffix = _normalize_newlines(new_suffix)
                    _update_rule_field(user_id, idx, "suffix", new_suffix)
                else:
                    _update_rule_field(user_id, idx, "suffix", cleaned)
        elif field == "media":
            _update_rule_field(user_id, idx, "media_replace", text)
        elif field == "speed":
            _update_rule_field(user_id, idx, "speed", text)

        context.user_data.pop("channel_config_panel", None)
        rules = _get_user_rules(user_id)
        rule = rules[idx] if idx >= 0 and idx < len(rules) else {}
        await update.message.reply_text(
            _format_rule_panel_text(rule, idx),
            reply_markup=_build_rule_panel_keyboard(idx, rule),
        )
        return True

    state = context.user_data.get("channel_config")
    if not state:
        return False

    if (update.effective_chat.type or "").lower() != "private":
        return False

    stage = state.get("stage")
    draft = state.get("draft", {})
    text = update.message.text.strip()

    if stage == "name":
        draft["name"] = text
        state["stage"] = "source_id"
        await update.message.reply_text("请输入搬运频道ID（数字ID，如 -1001234567890）：", reply_markup=_build_cancel_keyboard())
        return True

    if stage == "source_id":
        try:
            source_id = int(text)
            draft["source_id"] = source_id
            title = await _resolve_chat_title(context, source_id)
            draft["source_title"] = title or ""
        except Exception:
            await update.message.reply_text("❗ 请输入正确的频道ID（数字）。", reply_markup=_build_cancel_keyboard())
            return True
        state["stage"] = "target_id"
        await update.message.reply_text("请输入目标频道ID（数字ID，如 -1001234567890）：", reply_markup=_build_cancel_keyboard())
        return True

    if stage == "target_id":
        try:
            target_id = int(text)
            draft["target_id"] = target_id
            title = await _resolve_chat_title(context, target_id)
            draft["target_title"] = title or ""
        except Exception:
            await update.message.reply_text("❗ 请输入正确的频道ID（数字）。", reply_markup=_build_cancel_keyboard())
            return True
        state["stage"] = "enabled"
        await update.message.reply_text(
            "是否开启该规则？",
            reply_markup=InlineKeyboardMarkup(
                _build_enabled_keyboard(bool(draft.get("enabled", True))).inline_keyboard
                + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "enabled":
        await update.message.reply_text(
            "请点击按钮选择是否开启。",
            reply_markup=InlineKeyboardMarkup(
                _build_enabled_keyboard(bool(draft.get("enabled", True))).inline_keyboard
                + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "mode":
        await update.message.reply_text(
            "请点击按钮选择任务模式。",
            reply_markup=InlineKeyboardMarkup(
                _build_mode_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "filter":
        await update.message.reply_text(
            "请点击按钮选择任务类型。",
            reply_markup=InlineKeyboardMarkup(
                _build_filter_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "contact":
        await update.message.reply_text(
            "是否显示底部联系方式？",
            reply_markup=InlineKeyboardMarkup(
                _build_contact_keyboard(bool(draft.get("show_contact", True))).inline_keyboard
                + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "channel_user":
        draft["channel_user"] = _format_username(text)
        state["stage"] = "group_name"
        await update.message.reply_text("请输入群名（群用户名，如 @dubai_mm）：", reply_markup=_build_cancel_keyboard())
        return True

    if stage == "group_name":
        draft["group_name"] = _format_username(text)
        state["stage"] = "submit_user"
        await update.message.reply_text("请输入投稿用户名（如 @nuan12）：", reply_markup=_build_cancel_keyboard())
        return True

    if stage == "submit_user":
        draft["submit_user"] = _format_username(text)
        state["stage"] = "confirm"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ 保存", callback_data=f"{CALLBACK_PREFIX}:save")],
                [InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:cancel")],
            ]
        )
        await update.message.reply_text(_format_draft_summary(draft), reply_markup=keyboard)
        return True

    if isinstance(stage, str) and stage.startswith("edit_"):
        field = stage[len("edit_") :]
        user_id = str(draft.get("owner_id") or update.effective_user.id)
        idx = int(draft.get("edit_index", -1))
        if field == "enabled":
            await update.message.reply_text(
                "请点击按钮选择是否开启。",
                reply_markup=_build_enabled_keyboard(bool(draft.get("enabled", True)), index=idx),
            )
            return True
        if field == "mode":
            await update.message.reply_text("请点击按钮选择任务模式。", reply_markup=_build_mode_keyboard())
            return True
        if field == "show_contact":
            await update.message.reply_text(
                "请点击按钮选择是否显示联系方式。",
                reply_markup=_build_contact_keyboard(bool(draft.get("show_contact", True)), index=idx),
            )
            return True
        value = text
        if field in {"channel_user", "group_name", "submit_user"}:
            value = _format_username(text)
        if field in {"source_id", "target_id"}:
            try:
                value = int(text)
            except Exception:
                await update.message.reply_text("❗ 请输入正确的频道ID（数字）。")
                return True
        ok = _update_rule_field(user_id, idx, field, value)
        if not ok:
            await update.message.reply_text("❗ 更新失败，请重试。")
            return True
        if field == "source_id":
            title = await _resolve_chat_title(context, value)
            _update_rule_field(user_id, idx, "source_title", title or "")
        if field == "target_id":
            title = await _resolve_chat_title(context, value)
            _update_rule_field(user_id, idx, "target_title", title or "")
        rules = _get_user_rules(user_id)
        rule = rules[idx]
        draft.update(
            {
                "name": rule.get("name", ""),
                "source_id": (rule.get("sources") or [""])[0],
                "target_id": (rule.get("targets") or [""])[0],
                "enabled": bool(rule.get("enabled", True)),
                "mode": rule.get("mode", "listen"),
                "filter": rule.get("filter", "all"),
                "channel_user": rule.get("replace_channel_user", ""),
                "group_name": rule.get("replace_group_name", ""),
                "submit_user": rule.get("replace_submit_user", ""),
                "session_name": rule.get("session_name", ""),
                "source_title": rule.get("source_title", ""),
                "target_title": rule.get("target_title", ""),
                "edit_index": idx,
            }
        )
        state["stage"] = "edit_menu"
        await update.message.reply_text(
            "✅ 已更新。\n\n" + _format_draft_summary(draft),
            reply_markup=_build_edit_menu_keyboard(idx),
        )
        return True

    return False


async def handle_channel_config_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return await _handle_text_input(update, context)


# 注册 handlers

def register_channel_config_handlers(app):
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern=f"^{CALLBACK_PREFIX}:.+"))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), _handle_text_input)
    )
    app.add_handler(CommandHandler("channel_config", channel_config_entry))
    app.add_handler(CommandHandler("subscription", subscription_status))
    app.add_handler(CommandHandler("add_subscription", add_subscription))
