# -*- coding: utf-8 -*-
import re
from typing import Optional
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from command_router import register_command
from utils import is_super_admin, load_json, save_json, safe_reply

FORWARD_USER_CONFIG_FILE = "data/forward_config_users.json"
SUBSCRIPTION_FILE = "data/subscriptions.json"
CALLBACK_PREFIX = "chcfg"
SUBSCRIBER_MAX_RULES = 5

FILTER_LABELS = {
    "all": "全部",
    "text": "文本",
    "photo": "图片",
    "video": "视频",
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


def _today_date():
    return datetime.now().date()


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
        return await safe_reply(update, context, "请私聊机器人发送“频道配置”进行设置。", reply_markup=keyboard)
    return await safe_reply(update, context, "请私聊机器人发送“频道配置”进行设置。")


@register_command("频道配置")
async def channel_config_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_access(update):
        return await safe_reply(update, context, "🚫 仅高级管理员或订阅会员可使用该功能。")

    ok = await _ensure_private(update, context)
    if ok is not True:
        return

    await safe_reply(
        update,
        context,
        "请选择操作：\n\n“新建配置”会按步骤引导填写频道名称、ID、搬运类型、用户名等。",
        reply_markup=_build_main_menu_keyboard(),
    )


@register_command("订阅会员")
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
        return await safe_reply(update, context, "📭 你当前不是订阅会员。")

    expires_at = record.get("expires_at", "")
    if not expires_at:
        return await safe_reply(update, context, "📭 你当前不是订阅会员。")

    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d").date()
        status = "✅ 有效" if exp >= _today_date() else "❌ 已过期"
        return await safe_reply(update, context, f"订阅状态：{status}\n到期时间：{expires_at}")
    except Exception:
        return await safe_reply(update, context, "⚠️ 订阅信息格式异常，请联系管理员。")


@register_command("添加订阅")
async def add_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 仅高级管理员可添加订阅。")

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
            return await safe_reply(
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
            return await safe_reply(
                update,
                context,
                "用法：添加订阅 30\n或：添加订阅 2026-12-31",
            )
        expiry_arg = args[0]

    exp_date = _parse_expiry(expiry_arg)
    if not exp_date:
        return await safe_reply(update, context, "❗ 时间格式示例：30 或 2026-12-31")

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
    return await safe_reply(update, context, f"✅ 已设置订阅：{label} 到期 {record['expires_at']}")


def _start_new_wizard(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    context.user_data["channel_config"] = {
        "stage": "name",
        "draft": {"owner_id": user_id},
    }


def _clear_wizard(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("channel_config", None)


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


def _build_contact_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✅ 显示", callback_data=f"{CALLBACK_PREFIX}:contact:yes"),
            InlineKeyboardButton("🚫 不显示", callback_data=f"{CALLBACK_PREFIX}:contact:no"),
        ]
    ]
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


def _build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ 新建配置", callback_data=f"{CALLBACK_PREFIX}:new")],
            [InlineKeyboardButton("📄 查看现有", callback_data=f"{CALLBACK_PREFIX}:list")],
            [InlineKeyboardButton("❓ 帮助", callback_data=f"{CALLBACK_PREFIX}:help")],
            [InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:cancel")],
        ]
    )


def _build_edit_menu_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ 频道名称", callback_data=f"{CALLBACK_PREFIX}:field:{index}:name")],
            [InlineKeyboardButton("✏️ 搬运频道ID", callback_data=f"{CALLBACK_PREFIX}:field:{index}:source_id")],
            [InlineKeyboardButton("✏️ 目标频道ID", callback_data=f"{CALLBACK_PREFIX}:field:{index}:target_id")],
            [InlineKeyboardButton("✏️ 搬运类型", callback_data=f"{CALLBACK_PREFIX}:field:{index}:filter")],
            [InlineKeyboardButton("✏️ 显示联系方式", callback_data=f"{CALLBACK_PREFIX}:field:{index}:show_contact")],
            [InlineKeyboardButton("✏️ 频道用户名", callback_data=f"{CALLBACK_PREFIX}:field:{index}:channel_user")],
            [InlineKeyboardButton("✏️ 群名", callback_data=f"{CALLBACK_PREFIX}:field:{index}:group_name")],
            [InlineKeyboardButton("✏️ 投稿用户名", callback_data=f"{CALLBACK_PREFIX}:field:{index}:submit_user")],
            [InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:back")],
        ]
    )


def _format_draft_summary(draft: dict) -> str:
    show_contact = bool(draft.get("show_contact", True))
    return (
        "请确认配置：\n"
        f"频道名称：{draft.get('name', '')}\n"
        f"搬运频道ID：{draft.get('source_id', '')}\n"
        f"目标频道ID：{draft.get('target_id', '')}\n"
        f"搬运类型：{FILTER_LABELS.get(draft.get('filter', 'all'), draft.get('filter', 'all'))}\n"
        f"显示联系方式：{'是' if show_contact else '否'}\n"
        f"频道用户名：{draft.get('channel_user', '')}\n"
        f"群名：{draft.get('group_name', '')}\n"
        f"投稿用户名：{draft.get('submit_user', '')}"
    )


def _save_forward_rule(draft: dict, user_id: str, username: Optional[str] = None) -> None:
    rule = {
        "name": draft.get("name", ""),
        "sources": [draft.get("source_id")],
        "targets": [draft.get("target_id")],
        "filter": draft.get("filter", "all"),
        "exclude_channels": [],
        "show_contact": bool(draft.get("show_contact", True)),
        "replace_channel_user": draft.get("channel_user", ""),
        "replace_group_name": draft.get("group_name", ""),
        "replace_submit_user": draft.get("submit_user", ""),
    }
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
    elif field == "show_contact":
        rule["show_contact"] = bool(value)
    elif field == "source_id":
        rule["sources"] = [value]
    elif field == "target_id":
        rule["targets"] = [value]
    elif field == "channel_user":
        rule["replace_channel_user"] = value
    elif field == "group_name":
        rule["replace_group_name"] = value
    elif field == "submit_user":
        rule["replace_submit_user"] = value
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

    chat_type = (query.message.chat.type or "").lower() if query.message else ""
    if chat_type != "private":
        return await query.edit_message_text("请私聊机器人发送“频道配置”。")

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "new":
        user_id = str(update.effective_user.id)
        if not is_super_admin(update.effective_user.id):
            rules = _get_user_rules(user_id)
            if len(rules) >= SUBSCRIBER_MAX_RULES:
                return await query.edit_message_text(
                    f"⚠️ 订阅会员最多可配置 {SUBSCRIBER_MAX_RULES} 条规则。"
                )
        _start_new_wizard(context, user_id)
        context.user_data["channel_config"]["draft"]["owner_username"] = (
            update.effective_user.username or ""
        )
        return await query.edit_message_text("请输入频道名称：", reply_markup=_build_cancel_keyboard())

    if action == "list":
        user_id = str(update.effective_user.id)
        rules = _get_user_rules(user_id)
        if not rules:
            return await query.edit_message_text("暂无配置记录。", reply_markup=_build_main_menu_keyboard())
        lines = ["当前配置："]
        for idx, r in enumerate(rules, start=1):
            name = r.get("name", "")
            src = r.get("sources", [""])[0] if r.get("sources") else ""
            tgt = r.get("targets", [""])[0] if r.get("targets") else ""
            lines.append(f"{idx}. {name} | {src} → {tgt}")
        keyboard_rows = []
        for idx in range(1, len(rules) + 1):
            keyboard_rows.append(
                [
                    InlineKeyboardButton(f"✏️ 编辑 {idx}", callback_data=f"{CALLBACK_PREFIX}:edit:{idx-1}"),
                    InlineKeyboardButton(f"🗑 删除 {idx}", callback_data=f"{CALLBACK_PREFIX}:del:{idx-1}"),
                ]
            )
        keyboard_rows.append(
            [
                InlineKeyboardButton("❓ 帮助", callback_data=f"{CALLBACK_PREFIX}:help"),
                InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:back"),
            ]
        )
        return await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard_rows))

    if action == "cancel":
        _clear_wizard(context)
        return await query.edit_message_text("已取消。")

    if action == "back":
        return await query.edit_message_text(
            "请选择操作：\n\n“新建配置”会按步骤引导填写频道名称、ID、搬运类型、用户名等。",
            reply_markup=_build_main_menu_keyboard(),
        )

    if action == "help":
        help_text = (
            "📌 频道配置帮助\n"
            "1. 获取频道ID：私聊机器人转发一条频道消息，机器人会回复频道ID。\n"
            "2. 搬运频道ID：填写来源频道ID（如 -1001234567890）。\n"
            "3. 目标频道ID：填写要发布的目标频道ID。\n"
            "4. 搬运类型：全部/文本/图片/视频。\n"
            "5. 联系方式：选择显示或不显示；不显示会跳过用户名配置。\n"
            "6. 频道用户名/群名/投稿用户名：用于替换文本中的联系方式。\n"
        )
        return await query.edit_message_text(help_text, reply_markup=_build_main_menu_keyboard())

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
                _build_contact_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )

    if action == "contact" and len(parts) >= 3:
        state = context.user_data.get("channel_config")
        if not state:
            return await query.edit_message_text("请先点击“新建配置”。")
        show_contact = parts[2] != "no"
        if state.get("stage") == "edit_show_contact":
            user_id = str(update.effective_user.id)
            idx = int(state.get("draft", {}).get("edit_index", -1))
            ok = _update_rule_field(user_id, idx, "show_contact", show_contact)
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
                    "filter": rule.get("filter", "all"),
                    "show_contact": bool(rule.get("show_contact", True)),
                    "channel_user": rule.get("replace_channel_user", ""),
                    "group_name": rule.get("replace_group_name", ""),
                    "submit_user": rule.get("replace_submit_user", ""),
                    "edit_index": idx,
                }
            )
            state["stage"] = "edit_menu"
            return await query.edit_message_text(
                "✅ 已更新显示设置。\n\n" + _format_draft_summary(draft),
                reply_markup=_build_edit_menu_keyboard(idx),
            )

        state["draft"]["show_contact"] = show_contact
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
        required = ["name", "source_id", "target_id", "filter", "show_contact"]
        if draft.get("show_contact", True):
            required += ["channel_user", "group_name", "submit_user"]
        if any(not draft.get(k) for k in required):
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
        return await query.edit_message_text(
            f"✅ 已删除配置：{name}",
            reply_markup=_build_main_menu_keyboard(),
        )

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
            "filter": rule.get("filter", "all"),
            "show_contact": bool(rule.get("show_contact", True)),
            "channel_user": rule.get("replace_channel_user", ""),
            "group_name": rule.get("replace_group_name", ""),
            "submit_user": rule.get("replace_submit_user", ""),
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
            return await query.edit_message_text("请选择新的搬运类型：", reply_markup=_build_edit_filter_keyboard(idx))
        if field == "show_contact":
            return await query.edit_message_text("是否显示底部联系方式？", reply_markup=_build_contact_keyboard())
        label_map = {
            "name": "请输入新的频道名称：",
            "source_id": "请输入新的搬运频道ID（数字ID）：",
            "target_id": "请输入新的目标频道ID（数字ID）：",
            "channel_user": "请输入新的频道用户名（如 @gaoxiaoma）：",
            "group_name": "请输入新的群名（如 @U10000）：",
            "submit_user": "请输入新的投稿用户名（如 @mr566）：",
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
            "filter": rule.get("filter", "all"),
            "show_contact": bool(rule.get("show_contact", True)),
            "channel_user": rule.get("replace_channel_user", ""),
            "group_name": rule.get("replace_group_name", ""),
            "submit_user": rule.get("replace_submit_user", ""),
            "owner_id": user_id,
            "edit_index": idx,
        }
        context.user_data["channel_config"] = {"stage": "edit_menu", "draft": draft}
        return await query.edit_message_text(
            "✅ 已更新搬运类型。\n\n" + _format_draft_summary(draft),
            reply_markup=_build_edit_menu_keyboard(idx),
        )


async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False

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
            draft["source_id"] = int(text)
        except Exception:
            await update.message.reply_text("❗ 请输入正确的频道ID（数字）。", reply_markup=_build_cancel_keyboard())
            return True
        state["stage"] = "target_id"
        await update.message.reply_text("请输入目标频道ID（数字ID，如 -1001234567890）：", reply_markup=_build_cancel_keyboard())
        return True

    if stage == "target_id":
        try:
            draft["target_id"] = int(text)
        except Exception:
            await update.message.reply_text("❗ 请输入正确的频道ID（数字）。", reply_markup=_build_cancel_keyboard())
            return True
        state["stage"] = "filter"
        await update.message.reply_text(
            "请选择搬运类型：",
            reply_markup=InlineKeyboardMarkup(
                _build_filter_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "filter":
        await update.message.reply_text(
            "请点击按钮选择搬运类型。",
            reply_markup=InlineKeyboardMarkup(
                _build_filter_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
            ),
        )
        return True

    if stage == "contact":
        await update.message.reply_text(
            "是否显示底部联系方式？",
            reply_markup=InlineKeyboardMarkup(
                _build_contact_keyboard().inline_keyboard + _build_cancel_keyboard().inline_keyboard
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
        if field == "show_contact":
            await update.message.reply_text("请点击按钮选择是否显示联系方式。", reply_markup=_build_contact_keyboard())
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
        rules = _get_user_rules(user_id)
        rule = rules[idx]
        draft.update(
            {
                "name": rule.get("name", ""),
                "source_id": (rule.get("sources") or [""])[0],
                "target_id": (rule.get("targets") or [""])[0],
                "filter": rule.get("filter", "all"),
                "channel_user": rule.get("replace_channel_user", ""),
                "group_name": rule.get("replace_group_name", ""),
                "submit_user": rule.get("replace_submit_user", ""),
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
