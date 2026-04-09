import asyncio
import html
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters, ApplicationHandlerStop

from command_router import FEATURE_FRIENDS, register_command
from feature_flags import is_feature_enabled
from utils import (
    GROUP_LIST_FILE,
    get_group_whitelist,
    is_bot_owner,
    is_super_admin,
    load_json,
    safe_reply,
    save_json,
)
from channel.channel_force import unmute_force_subscribe_chat

CALLBACK_PREFIX = "gcfg"
FORCE_SUBSCRIBE_FILE = "config_data/force_subscribe.json"
TOGGLE_FIELDS = [
    ("reply_enabled", "开启回复"),
    ("verify", "身份验证"),
    ("welcome", "入群欢迎"),
    ("silent", "群静默"),
    ("ad_filter", "广告拦截"),
    ("ad_push_enabled", "广告定时推送"),
    ("chengyu_game", "成语接龙"),
    ("spam_limit", "防刷屏限频"),
    ("manor", "庄园系统"),
    (FEATURE_FRIENDS, "群好友功能"),
    ("active_speak_enabled", "主动说话"),
    ("force_subscribe", "强制关注频道"),
    ("name_change_notice", "用户名变更提示"),
    ("recommend", "群推荐"),
]
BOT_ADMIN_REQUIRED_FIELDS = {"verify", "ad_filter", "spam_limit", "force_subscribe"}
ACTIVE_SPEAK_MIN_INTERVAL = 1
ACTIVE_SPEAK_MAX_INTERVAL = 1440
AD_PUSH_MIN_INTERVAL = 5
AD_PUSH_MAX_INTERVAL = 1440
MANAGE_CHECK_CACHE_TTL_SEC = 60
GROUP_LIST_PAGE_SIZE = 10


def _is_group_chat(update: Update) -> bool:
    chat_type = (update.effective_chat.type or "").lower() if update.effective_chat else ""
    return chat_type in {"group", "supergroup"}


def _is_group_feature_enabled(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return is_feature_enabled(context.application, "group")


def _private_chat_url(context: ContextTypes.DEFAULT_TYPE) -> str:
    username = getattr(context.bot, "username", "") or ""
    return f"https://t.me/{username}" if username else ""


def _add_group_url(context: ContextTypes.DEFAULT_TYPE) -> str:
    username = getattr(context.bot, "username", "") or ""
    return f"https://t.me/{username}?startgroup=true" if username else ""


def _group_title(chat_id: str, cfg: dict) -> str:
    title = (cfg or {}).get("title", "") or (cfg or {}).get("username", "")
    return title or f"群 {chat_id}"


def _get_force_channel(chat_id: str) -> str:
    data = load_json(FORCE_SUBSCRIBE_FILE)
    if not isinstance(data, dict):
        return ""
    return str(data.get(chat_id, "")).strip()


def _set_force_channel(chat_id: str, channel_username: str):
    data = load_json(FORCE_SUBSCRIBE_FILE)
    if not isinstance(data, dict):
        data = {}
    if channel_username:
        data[chat_id] = channel_username
    else:
        data.pop(chat_id, None)
    save_json(FORCE_SUBSCRIBE_FILE, data)


def _toggle_text(enabled: bool) -> str:
    return "✅ 开启" if enabled else "🚫 关闭"


def _visible_toggle_fields(bot_is_admin: bool) -> list[tuple[str, str]]:
    if bot_is_admin:
        return TOGGLE_FIELDS
    return [
        item for item in TOGGLE_FIELDS if item[0] not in BOT_ADMIN_REQUIRED_FIELDS
    ]


def _normalize_business_coop_link(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        return f"https://t.me/{text[1:]}"
    if text.lower().startswith("t.me/"):
        return f"https://{text}"
    return text


def _build_group_list_keyboard(
    data: dict, page: int = 1, *, add_group_url: str = ""
) -> InlineKeyboardMarkup:
    items = [
        (chat_id, cfg)
        for chat_id, cfg in sorted(
            data.items(), key=lambda item: _group_title(item[0], item[1]).lower()
        )
        if isinstance(cfg, dict)
    ]
    total = len(items)
    total_pages = max(1, (total + GROUP_LIST_PAGE_SIZE - 1) // GROUP_LIST_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * GROUP_LIST_PAGE_SIZE
    end = start + GROUP_LIST_PAGE_SIZE

    rows = []
    for chat_id, cfg in items[start:end]:
        if not isinstance(cfg, dict):
            continue
        label = _group_title(chat_id, cfg)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⚙️ {label[:22]}",
                    callback_data=f"{CALLBACK_PREFIX}:open:{chat_id}",
                )
            ]
        )
    nav_row = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton("⬅️ 上一页", callback_data=f"{CALLBACK_PREFIX}:list:{page - 1}")
        )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton("➡️ 下一页", callback_data=f"{CALLBACK_PREFIX}:list:{page + 1}")
        )
    if nav_row:
        rows.append(nav_row)
    if add_group_url:
        rows.append([InlineKeyboardButton("+ 添加群组", url=add_group_url)])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def _group_list_text(data: dict, page: int = 1) -> str:
    total = sum(1 for _, cfg in data.items() if isinstance(cfg, dict))
    total_pages = max(1, (total + GROUP_LIST_PAGE_SIZE - 1) // GROUP_LIST_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    return f"请选择要配置的群：\n第 {page}/{total_pages} 页"


def _build_group_panel_text(chat_id: str, cfg: dict, *, bot_is_admin: bool = False) -> str:
    group_name = html.escape(_group_title(chat_id, cfg))
    username = str(cfg.get("username", "") or "").strip()
    username_text = f"@{username}" if username else "未设置"
    spam_limit_value = int(cfg.get("spam_limit_max_per_minute", 10))
    interval = int(cfg.get("active_speak_interval_min", 120))
    ad_mode = str(cfg.get("ad_push_mode", "interval"))
    ad_interval = int(cfg.get("ad_push_interval_min", 120))
    ad_times = str(cfg.get("ad_push_times", "")).strip() or "未设置"
    ad_has_text = "已设置" if str(cfg.get("ad_push_text", "")).strip() else "未设置"
    business_coop = (
        _normalize_business_coop_link(cfg.get("business_coop_link", "")) or "未设置"
    )
    lines = [
        "📊 群配置面板",
        f"🆔 群ID：<code>{chat_id}</code> | 群名：{group_name}",
        f"👤 用户名：{html.escape(username_text)}",
        f"🤖 机器人管理员：{'✅ 是' if bot_is_admin else '🚫 否'}",
        "",
    ]
    for key, label in _visible_toggle_fields(bot_is_admin):
        lines.append(f"{label}：{_toggle_text(bool(cfg.get(key, False)))}")
    if bot_is_admin:
        lines.append(f"限频条数：{spam_limit_value} 条/分钟")
    lines.append(f"曝光度：{int(cfg.get('exposure', 0))}")
    lines.append(f"主动说话频率：每 {interval} 分钟")
    if bot_is_admin:
        lines.append(
            f"广告推送：模式={'定时' if ad_mode == 'fixed' else '间隔'} "
            f"间隔={ad_interval} 分钟 定时={ad_times} 文案={ad_has_text}"
        )
        force_channel = _get_force_channel(chat_id)
        lines.append(f"强制关注频道：{force_channel if force_channel else '未设置'}")
    lines.append(f"商业合作：{html.escape(business_coop)}")
    lines.append("")
    if not bot_is_admin:
        lines.append("部分需要管理权限的功能已隐藏。")
    lines.append("仅群管理员或超级管理员可修改。")
    return "\n".join(lines)


def _can_leave_group(user_id: int) -> bool:
    return is_super_admin(user_id) or is_bot_owner(user_id)


def _build_group_panel_keyboard(
    chat_id: str, cfg: dict, list_page: int = 1, *, bot_is_admin: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    toggle_buttons = []
    for key, label in _visible_toggle_fields(bot_is_admin):
        is_on = bool(cfg.get(key, False))
        toggle_buttons.append(
            InlineKeyboardButton(
                text=f"{'✅' if is_on else '🚫'} {label}",
                callback_data=f"{CALLBACK_PREFIX}:toggle:{chat_id}:{key}",
            )
        )
    for i in range(0, len(toggle_buttons), 2):
        rows.append(toggle_buttons[i : i + 2])

    if bot_is_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    "📢 设置关注频道",
                    callback_data=f"{CALLBACK_PREFIX}:force_channel:{chat_id}",
                ),
                InlineKeyboardButton(
                    "🤝 商业合作",
                    callback_data=f"{CALLBACK_PREFIX}:business_coop:{chat_id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "📝 广告文案",
                    callback_data=f"{CALLBACK_PREFIX}:ad_text:{chat_id}",
                ),
                InlineKeyboardButton(
                    "⏱ 广告间隔",
                    callback_data=f"{CALLBACK_PREFIX}:ad_interval:{chat_id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🕒 广告定时",
                    callback_data=f"{CALLBACK_PREFIX}:ad_times:{chat_id}",
                ),
                InlineKeyboardButton(
                    "🔀 推送模式",
                    callback_data=f"{CALLBACK_PREFIX}:ad_mode:{chat_id}",
                ),
            ]
        )

        spam_limit = int(cfg.get("spam_limit_max_per_minute", 10))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➖ 限频 {max(1, spam_limit - 1)}",
                    callback_data=f"{CALLBACK_PREFIX}:spam:{chat_id}:-1",
                ),
                InlineKeyboardButton(
                    text=f"➕ 限频 {min(200, spam_limit + 1)}",
                    callback_data=f"{CALLBACK_PREFIX}:spam:{chat_id}:+1",
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "🤝 商业合作",
                    callback_data=f"{CALLBACK_PREFIX}:business_coop:{chat_id}",
                )
            ]
        )

    interval = int(cfg.get("active_speak_interval_min", 120))
    rows.append(
        [
            InlineKeyboardButton(
                text="-10",
                callback_data=f"{CALLBACK_PREFIX}:interval_delta:{chat_id}:-10",
            ),
            InlineKeyboardButton(
                text="-1",
                callback_data=f"{CALLBACK_PREFIX}:interval_delta:{chat_id}:-1",
            ),
            InlineKeyboardButton(
                text=f"⏱ {interval}m",
                callback_data=f"{CALLBACK_PREFIX}:noop",
            ),
            InlineKeyboardButton(
                text="+1",
                callback_data=f"{CALLBACK_PREFIX}:interval_delta:{chat_id}:+1",
            ),
            InlineKeyboardButton(
                text="+10",
                callback_data=f"{CALLBACK_PREFIX}:interval_delta:{chat_id}:+10",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton("⬅️ 选择其他群", callback_data=f"{CALLBACK_PREFIX}:list:{max(1, list_page)}"),
            InlineKeyboardButton("🔄 刷新", callback_data=f"{CALLBACK_PREFIX}:open:{chat_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _build_group_panel_keyboard_for_user(
    chat_id: str, cfg: dict, user_id: int, list_page: int = 1, *, bot_is_admin: bool = False
) -> InlineKeyboardMarkup:
    rows = list(
        _build_group_panel_keyboard(
            chat_id, cfg, list_page, bot_is_admin=bot_is_admin
        ).inline_keyboard
    )
    if _can_leave_group(user_id):
        rows.append(
            [
                InlineKeyboardButton(
                    "🚪 退出群聊",
                    callback_data=f"{CALLBACK_PREFIX}:leave:{chat_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _can_manage_group(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int
) -> bool:
    if is_super_admin(user_id):
        return True

    cache = context.application.bot_data.setdefault("group_manage_cache", {})
    cache_key = f"{user_id}:{chat_id}"
    now_ts = time.time()
    hit = cache.get(cache_key)
    if isinstance(hit, dict):
        if now_ts - float(hit.get("ts", 0)) <= MANAGE_CHECK_CACHE_TTL_SEC:
            return bool(hit.get("ok", False))

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        ok = member.status in {"administrator", "creator"}
        cache[cache_key] = {"ok": ok, "ts": now_ts}
        return ok
    except Exception:
        cache[cache_key] = {"ok": False, "ts": now_ts}
        return False


async def _is_bot_group_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> bool:
    cache = context.application.bot_data.setdefault("group_bot_admin_cache", {})
    bot_id = getattr(context.bot, "id", 0)
    cache_key = f"{bot_id}:{chat_id}"
    now_ts = time.time()
    hit = cache.get(cache_key)
    if isinstance(hit, dict):
        if now_ts - float(hit.get("ts", 0)) <= MANAGE_CHECK_CACHE_TTL_SEC:
            return bool(hit.get("ok", False))

    try:
        member = await context.bot.get_chat_member(chat_id, bot_id)
        ok = member.status in {"administrator", "creator"}
        cache[cache_key] = {"ok": ok, "ts": now_ts}
        return ok
    except Exception:
        cache[cache_key] = {"ok": False, "ts": now_ts}
        return False


async def _visible_group_data_for_user(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, data: dict
) -> dict:
    # 只展示机器人当前仍在群内的群，避免离群后仍出现在配置列表
    active_data = {
        chat_id: cfg
        for chat_id, cfg in data.items()
        if isinstance(cfg, dict) and bool(cfg.get("bot_in_group", False))
    }

    if is_super_admin(user_id):
        return active_data

    checks = []
    meta = []
    for chat_id_str, cfg in active_data.items():
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            continue
        meta.append((chat_id_str, cfg))
        checks.append(_can_manage_group(context, user_id, chat_id))

    results = await asyncio.gather(*checks, return_exceptions=True)
    visible = {}
    for idx, result in enumerate(results):
        if result is True:
            chat_id_str, cfg = meta[idx]
            visible[chat_id_str] = cfg
    return visible


async def _show_group_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_group_whitelist(context)
    user = update.effective_user
    user_id = user.id if user else 0
    data = await _visible_group_data_for_user(context, user_id, data)
    if not data:
        return await safe_reply(update, context, "暂无可配置的群记录。")
    page = 1
    context.user_data["group_setting_list_page"] = page
    keyboard = _build_group_list_keyboard(data, page)
    text = _group_list_text(data, page)
    if keyboard.inline_keyboard:
        return await safe_reply(update, context, text, reply_markup=keyboard)
    return await safe_reply(update, context, "暂无可配置的群记录。")


def _parse_chat_id(raw: str) -> Optional[int]:
    try:
        return int(raw)
    except Exception:
        return None


async def _open_group_panel(
    query, context: ContextTypes.DEFAULT_TYPE, chat_id_str: str, user_id: int
):
    chat_id = _parse_chat_id(chat_id_str)
    if chat_id is None:
        return await query.answer("群ID无效", show_alert=True)

    if not await _can_manage_group(context, user_id, chat_id):
        return await query.answer("你不是该群管理员，无法修改。", show_alert=True)

    data = get_group_whitelist(context)
    cfg = data.get(chat_id_str, {})
    if not isinstance(cfg, dict):
        cfg = {}
    bot_is_admin = await _is_bot_group_admin(context, chat_id)

    await query.answer()
    list_page = int(context.user_data.get("group_setting_list_page", 1) or 1)
    keyboard = _build_group_panel_keyboard_for_user(
        chat_id_str, cfg, user_id, list_page=list_page, bot_is_admin=bot_is_admin
    )
    if context.user_data.get("start_panel"):
        rows = list(keyboard.inline_keyboard)
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
        keyboard = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        text=_build_group_panel_text(chat_id_str, cfg, bot_is_admin=bot_is_admin),
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@register_command("群状态")
async def group_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_group_feature_enabled(context):
        return await safe_reply(update, context, "⚠️ 当前机器人未开启群功能。")
    if not update.message or not update.effective_chat:
        return
    chat_id = str(update.effective_chat.id)
    bot_is_admin = await _is_bot_group_admin(context, int(chat_id))
    group_config = get_group_whitelist(context).get(chat_id, {})
    if not group_config.get("enabled", False):
        return await safe_reply(update, context, "⚠️ 本群尚未启用主功能。")
    await update.message.reply_text(
        _build_group_panel_text(chat_id, group_config, bot_is_admin=bot_is_admin),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@register_command("群开关", "群配置", "群设置")
async def group_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_group_feature_enabled(context):
        return await safe_reply(update, context, "⚠️ 当前机器人未开启群功能。")
    if not update.message:
        return

    if _is_group_chat(update):
        private_url = _private_chat_url(context)
        if private_url:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("👉 去私聊配置", url=private_url)]]
            )
            await update.message.reply_text(
                "请在私聊里配置群功能，避免群内多机器人同时响应。",
                reply_markup=keyboard,
            )
            return
        return await safe_reply(update, context, "请私聊机器人发送“群配置”。")

    user = update.effective_user
    user_id = user.id if user else 0
    data = get_group_whitelist(context)
    data = await _visible_group_data_for_user(context, user_id, data)
    if not data:
        add_group_url = _add_group_url(context)
        reply_markup = (
            InlineKeyboardMarkup([[InlineKeyboardButton("+ 添加群组", url=add_group_url)]])
            if add_group_url
            else None
        )
        return await safe_reply(update, context, "暂无可配置的群记录。", reply_markup=reply_markup)
    page = 1
    context.user_data["group_setting_list_page"] = page
    keyboard = _build_group_list_keyboard(data, page, add_group_url=_add_group_url(context))
    if not keyboard.inline_keyboard:
        return await safe_reply(update, context, "暂无可配置的群记录。")
    await update.message.reply_text(_group_list_text(data, page), reply_markup=keyboard)


async def _redirect_to_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_group_feature_enabled(context):
        return await safe_reply(update, context, "⚠️ 当前机器人未开启群功能。")
    if _is_group_chat(update):
        return await group_help(update, context)
    await safe_reply(update, context, "请发送“群配置”并通过按钮操作。")


@register_command("群静默")
async def toggle_silent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群验证")
async def toggle_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群欢迎")
async def toggle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群广告")
async def toggle_ad_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群限频")
async def toggle_spam_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群限频条数", "限频条数")
async def set_spam_limit_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群庄园")
async def toggle_manor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群好友")
async def toggle_friends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


@register_command("群成语")
async def toggle_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _redirect_to_private(update, context)


def _parse_ad_times(raw: str) -> list[str]:
    slots = []
    for p in (raw or "").replace("，", ",").split(","):
        x = p.strip()
        if len(x) != 5 or x[2] != ":":
            continue
        hh, mm = x[:2], x[3:]
        if not (hh.isdigit() and mm.isdigit()):
            continue
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            slots.append(f"{h:02d}:{m:02d}")
    return sorted(set(slots))


@register_command("群广告推送")
async def ad_push_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    if not _is_group_chat(update):
        return await safe_reply(update, context, "请在群里使用此命令。")

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id if update.effective_user else 0
    if not await _can_manage_group(context, user_id, int(chat_id)):
        return await safe_reply(update, context, "❌ 仅群管理员可配置。")

    data = get_group_whitelist(context)
    cfg = data.get(chat_id, {})
    if not isinstance(cfg, dict):
        cfg = {}

    args = context.args or []
    if not args or args[0] in {"查看", "状态"}:
        mode = str(cfg.get("ad_push_mode", "interval"))
        interval = int(cfg.get("ad_push_interval_min", 120))
        times = str(cfg.get("ad_push_times", "")).strip() or "未设置"
        text = "已设置" if str(cfg.get("ad_push_text", "")).strip() else "未设置"
        enabled = "✅ 开启" if bool(cfg.get("ad_push_enabled", False)) else "🚫 关闭"
        return await safe_reply(
            update,
            context,
            (
                f"📢 群广告推送：{enabled}\n"
                f"模式：{'定时' if mode == 'fixed' else '间隔'}\n"
                f"间隔：每 {interval} 分钟\n"
                f"定时：{times}\n"
                f"文案：{text}\n\n"
                "用法：\n"
                "群广告推送 开启/关闭\n"
                "群广告推送 文案 你的广告内容\n"
                "群广告推送 间隔 60\n"
                "群广告推送 定时 09:00,12:30,21:00\n"
                "群广告推送 模式 间隔/定时\n"
                "群广告推送 清空文案"
            ),
        )

    cmd = args[0].strip().lower()
    if cmd in {"开", "开启", "on", "true", "1"}:
        cfg["ad_push_enabled"] = True
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, "✅ 群广告推送已开启")

    if cmd in {"关", "关闭", "off", "false", "0"}:
        cfg["ad_push_enabled"] = False
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, "❌ 群广告推送已关闭")

    if cmd in {"文案", "text"}:
        ad_text = " ".join(args[1:]).strip() if len(args) > 1 else ""
        if not ad_text:
            return await safe_reply(update, context, "❗ 请输入广告文案。")
        cfg["ad_push_text"] = ad_text
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, "✅ 广告文案已保存。")

    if cmd in {"清空文案", "clear"}:
        cfg["ad_push_text"] = ""
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, "✅ 广告文案已清空。")

    if cmd in {"间隔", "interval"}:
        if len(args) < 2 or not args[1].isdigit():
            return await safe_reply(update, context, f"❗ 请输入分钟数（{AD_PUSH_MIN_INTERVAL}-{AD_PUSH_MAX_INTERVAL}）")
        interval = int(args[1])
        if interval < AD_PUSH_MIN_INTERVAL or interval > AD_PUSH_MAX_INTERVAL:
            return await safe_reply(update, context, f"❗ 间隔范围：{AD_PUSH_MIN_INTERVAL}-{AD_PUSH_MAX_INTERVAL} 分钟")
        cfg["ad_push_mode"] = "interval"
        cfg["ad_push_interval_min"] = interval
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, f"✅ 已设置广告间隔推送：每 {interval} 分钟")

    if cmd in {"定时", "time"}:
        raw = " ".join(args[1:]).strip() if len(args) > 1 else ""
        slots = _parse_ad_times(raw)
        if not slots:
            return await safe_reply(update, context, "❗ 时间格式示例：09:00,12:30,21:00")
        cfg["ad_push_mode"] = "fixed"
        cfg["ad_push_times"] = ",".join(slots)
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, f"✅ 已设置广告定时推送：{','.join(slots)}")

    if cmd in {"模式", "mode"}:
        if len(args) < 2:
            return await safe_reply(update, context, "❗ 用法：群广告推送 模式 间隔/定时")
        mode_raw = args[1].strip().lower()
        if mode_raw in {"间隔", "interval"}:
            cfg["ad_push_mode"] = "interval"
        elif mode_raw in {"定时", "fixed", "time"}:
            cfg["ad_push_mode"] = "fixed"
        else:
            return await safe_reply(update, context, "❗ 模式仅支持：间隔 或 定时")
        data[chat_id] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await safe_reply(update, context, f"✅ 已切换广告推送模式为：{args[1]}")

    await safe_reply(update, context, "❗ 参数错误，发送「群广告推送 查看」查看用法。")


async def group_setting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    if not _is_group_feature_enabled(context):
        return await query.answer("当前机器人未开启群功能。", show_alert=False)

    if not update.effective_chat or update.effective_chat.type != "private":
        return await query.answer("请在私聊里操作配置。", show_alert=True)

    parts = query.data.split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return

    action = parts[1]
    user_id = update.effective_user.id
    data = get_group_whitelist(context)

    if action == "list":
        visible_data = await _visible_group_data_for_user(context, user_id, data)
        page = 1
        if len(parts) >= 3:
            try:
                page = int(parts[2])
            except Exception:
                page = 1
        context.user_data["group_setting_list_page"] = page
        await query.answer()
        keyboard = _build_group_list_keyboard(
            visible_data,
            page,
            add_group_url=_add_group_url(context),
        )
        if not keyboard.inline_keyboard:
            return await query.edit_message_text("暂无可配置的群记录。")
        if context.user_data.get("start_panel"):
            rows = list(keyboard.inline_keyboard)
            rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
            keyboard = InlineKeyboardMarkup(rows)
        return await query.edit_message_text(
            _group_list_text(visible_data, page), reply_markup=keyboard
        )

    if action == "open" and len(parts) >= 3:
        return await _open_group_panel(query, context, parts[2], user_id)

    if action == "leave" and len(parts) >= 3:
        chat_id_str = parts[2]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return await query.answer("群ID无效", show_alert=True)
        if not _can_leave_group(user_id):
            return await query.answer("仅高级管理员或机器人所有者可操作。", show_alert=True)

        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["title"] = _group_title(chat_id_str, cfg)
        cfg["type"] = "group"
        cfg["bot_in_group"] = False
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)

        try:
            await context.bot.leave_chat(chat_id)
            await query.answer("已退出该群", show_alert=False)
        except Exception as e:
            cfg["bot_in_group"] = True
            data[chat_id_str] = cfg
            save_json(GROUP_LIST_FILE, data)
            return await query.answer(f"退出群失败: {e}", show_alert=True)

        visible_data = await _visible_group_data_for_user(context, user_id, data)
        page = int(context.user_data.get("group_setting_list_page", 1) or 1)
        keyboard = _build_group_list_keyboard(
            visible_data,
            page,
            add_group_url=_add_group_url(context),
        )
        if not keyboard.inline_keyboard:
            return await query.edit_message_text("已退出该群，暂无可配置的群记录。")
        if context.user_data.get("start_panel"):
            rows = list(keyboard.inline_keyboard)
            rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
            keyboard = InlineKeyboardMarkup(rows)
        return await query.edit_message_text(
            f"已退出群 {html.escape(_group_title(chat_id_str, cfg))}。\n\n"
            f"{_group_list_text(visible_data, page)}",
            reply_markup=keyboard,
        )

    if action == "toggle" and len(parts) >= 4:
        chat_id_str = parts[2]
        feature_key = parts[3]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        if (
            feature_key in BOT_ADMIN_REQUIRED_FIELDS
            and not await _is_bot_group_admin(context, chat_id)
        ):
            return await query.answer("机器人不是该群管理员，无法配置此项。", show_alert=True)
        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        if feature_key in {item[0] for item in TOGGLE_FIELDS}:
            new_value = not bool(cfg.get(feature_key, False))
            cfg[feature_key] = new_value
            data[chat_id_str] = cfg
            save_json(GROUP_LIST_FILE, data)
            if feature_key == "force_subscribe" and not new_value:
                await unmute_force_subscribe_chat(context, chat_id_str)
        return await _open_group_panel(query, context, chat_id_str, user_id)

    if action == "force_channel" and len(parts) >= 3:
        chat_id_str = parts[2]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        if not await _is_bot_group_admin(context, chat_id):
            return await query.answer("机器人不是该群管理员，无法配置此项。", show_alert=True)
        context.user_data["group_setting_stage"] = "force_channel"
        context.user_data["group_setting_chat_id"] = chat_id_str
        await query.answer()
        return await query.edit_message_text(
            "请输入要强制关注的频道用户名（如 @example）。\n"
            "发送「清空」可移除当前设置。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🧹 清除",
                            callback_data=f"{CALLBACK_PREFIX}:force_channel_clear:{chat_id_str}",
                        ),
                        InlineKeyboardButton(
                            "⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:force_channel_back"
                        ),
                    ]
                ]
            ),
        )
    if action == "business_coop" and len(parts) >= 3:
        chat_id_str = parts[2]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        context.user_data["group_setting_stage"] = "business_coop"
        context.user_data["group_setting_chat_id"] = chat_id_str
        await query.answer()
        return await query.edit_message_text(
            "请输入商业合作链接。\n支持 @username、t.me/xxx、https://xxx\n发送「清空」可移除当前设置。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:business_coop_back")]]
            ),
        )
    if action in {"ad_text", "ad_interval", "ad_times", "ad_mode"} and len(parts) >= 3:
        chat_id_str = parts[2]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        if not await _is_bot_group_admin(context, chat_id):
            return await query.answer("机器人不是该群管理员，无法配置此项。", show_alert=True)
        context.user_data["group_setting_chat_id"] = chat_id_str
        back_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:ad_back")]]
        )
        if action == "ad_text":
            context.user_data["group_setting_stage"] = "ad_text"
            await query.answer()
            return await query.edit_message_text(
                "请输入广告文案，发送「清空」可移除。",
                reply_markup=back_markup,
            )
        if action == "ad_interval":
            context.user_data["group_setting_stage"] = "ad_interval"
            await query.answer()
            return await query.edit_message_text(
                f"请输入广告推送间隔（分钟，{AD_PUSH_MIN_INTERVAL}-{AD_PUSH_MAX_INTERVAL}）。",
                reply_markup=back_markup,
            )
        if action == "ad_times":
            context.user_data["group_setting_stage"] = "ad_times"
            await query.answer()
            return await query.edit_message_text(
                "请输入定时时间，如：09:00,12:30,21:00",
                reply_markup=back_markup,
            )
        if action == "ad_mode":
            context.user_data["group_setting_stage"] = "ad_mode"
            await query.answer()
            return await query.edit_message_text(
                "请输入模式：间隔 或 定时",
                reply_markup=back_markup,
            )
    if action == "ad_back":
        context.user_data.pop("group_setting_stage", None)
        chat_id_str = context.user_data.pop("group_setting_chat_id", None)
        await query.answer()
        if chat_id_str:
            return await _open_group_panel(query, context, chat_id_str, user_id)
        return
    if action == "force_channel_clear" and len(parts) >= 3:
        chat_id_str = parts[2]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        if not await _is_bot_group_admin(context, chat_id):
            return await query.answer("机器人不是该群管理员，无法配置此项。", show_alert=True)
        _set_force_channel(chat_id_str, "")
        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_chat_id", None)
        await query.answer("✅ 已清空", show_alert=False)
        return await _open_group_panel(query, context, chat_id_str, user_id)
    if action == "force_channel_back":
        context.user_data.pop("group_setting_stage", None)
        chat_id_str = context.user_data.pop("group_setting_chat_id", None)
        await query.answer()
        if chat_id_str:
            return await _open_group_panel(query, context, chat_id_str, user_id)
        return
    if action == "business_coop_back":
        context.user_data.pop("group_setting_stage", None)
        chat_id_str = context.user_data.pop("group_setting_chat_id", None)
        await query.answer()
        if chat_id_str:
            return await _open_group_panel(query, context, chat_id_str, user_id)
        return

    if action == "spam" and len(parts) >= 4:
        chat_id_str = parts[2]
        delta_raw = parts[3]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        if not await _is_bot_group_admin(context, chat_id):
            return await query.answer("机器人不是该群管理员，无法配置此项。", show_alert=True)
        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        current = int(cfg.get("spam_limit_max_per_minute", 10))
        delta = 1 if delta_raw == "+1" else -1
        cfg["spam_limit_max_per_minute"] = max(1, min(200, current + delta))
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await _open_group_panel(query, context, chat_id_str, user_id)

    if action == "interval" and len(parts) >= 4:
        chat_id_str = parts[2]
        interval = int(parts[3])
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["active_speak_interval_min"] = max(
            ACTIVE_SPEAK_MIN_INTERVAL, min(ACTIVE_SPEAK_MAX_INTERVAL, interval)
        )
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await _open_group_panel(query, context, chat_id_str, user_id)

    if action == "interval_delta" and len(parts) >= 4:
        chat_id_str = parts[2]
        chat_id = _parse_chat_id(chat_id_str)
        if chat_id is None:
            return
        if not await _can_manage_group(context, user_id, chat_id):
            return await query.answer("你不是该群管理员，无法修改。", show_alert=True)

        try:
            delta = int(parts[3])
        except Exception:
            return

        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        current = int(cfg.get("active_speak_interval_min", 120))
        cfg["active_speak_interval_min"] = max(
            ACTIVE_SPEAK_MIN_INTERVAL,
            min(ACTIVE_SPEAK_MAX_INTERVAL, current + delta),
        )
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)
        return await _open_group_panel(query, context, chat_id_str, user_id)

    if action == "noop":
        return await query.answer("当前主动说话频率", show_alert=False)


async def handle_group_setting_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    if not _is_group_feature_enabled(context):
        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_chat_id", None)
        return
    if update.effective_chat.type != "private":
        return
    stage = context.user_data.get("group_setting_stage")
    if stage not in {"force_channel", "ad_text", "ad_interval", "ad_times", "ad_mode", "business_coop"}:
        return

    chat_id_str = context.user_data.get("group_setting_chat_id")
    if not chat_id_str:
        context.user_data.pop("group_setting_stage", None)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    if stage == "force_channel":
        if text in {"清空", "取消", "关闭"}:
            _set_force_channel(chat_id_str, "")
            context.user_data.pop("group_setting_stage", None)
            context.user_data.pop("group_setting_chat_id", None)
            await update.message.reply_text("✅ 已清空强制关注频道设置。")
        else:
            if not text.startswith("@"):
                text = f"@{text}"
            _set_force_channel(chat_id_str, text)
            context.user_data.pop("group_setting_stage", None)
            context.user_data.pop("group_setting_chat_id", None)
            await update.message.reply_text(f"✅ 已设置强制关注频道为：{text}")
    elif stage == "business_coop":
        data = get_group_whitelist(context)
        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        if text in {"清空", "取消", "关闭"}:
            cfg["business_coop_link"] = ""
            await update.message.reply_text("✅ 已清空商业合作链接。")
        else:
            cfg["business_coop_link"] = _normalize_business_coop_link(text)
            await update.message.reply_text(
                f"✅ 已设置商业合作链接：{cfg['business_coop_link']}"
            )
        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_chat_id", None)
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)
    else:
        data = get_group_whitelist(context)
        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}

        if text in {"取消", "返回"}:
            context.user_data.pop("group_setting_stage", None)
            context.user_data.pop("group_setting_chat_id", None)
            await update.message.reply_text("✅ 已取消。")
        elif stage == "ad_text":
            if text in {"清空", "关闭"}:
                cfg["ad_push_text"] = ""
                await update.message.reply_text("✅ 已清空广告文案。")
            else:
                cfg["ad_push_text"] = text
                await update.message.reply_text("✅ 广告文案已保存。")
        elif stage == "ad_interval":
            if not text.isdigit():
                return await update.message.reply_text("❗ 请输入数字分钟。")
            interval = int(text)
            if interval < AD_PUSH_MIN_INTERVAL or interval > AD_PUSH_MAX_INTERVAL:
                return await update.message.reply_text(
                    f"❗ 间隔范围：{AD_PUSH_MIN_INTERVAL}-{AD_PUSH_MAX_INTERVAL} 分钟"
                )
            cfg["ad_push_mode"] = "interval"
            cfg["ad_push_interval_min"] = interval
            await update.message.reply_text(f"✅ 已设置广告间隔：每 {interval} 分钟")
        elif stage == "ad_times":
            slots = _parse_ad_times(text)
            if not slots:
                return await update.message.reply_text("❗ 时间格式示例：09:00,12:30,21:00")
            cfg["ad_push_mode"] = "fixed"
            cfg["ad_push_times"] = ",".join(slots)
            await update.message.reply_text(f"✅ 已设置广告定时：{','.join(slots)}")
        elif stage == "ad_mode":
            if text not in {"间隔", "定时"}:
                return await update.message.reply_text("❗ 模式仅支持：间隔 或 定时")
            cfg["ad_push_mode"] = "interval" if text == "间隔" else "fixed"
            await update.message.reply_text(f"✅ 已切换广告推送模式为：{text}")

        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_chat_id", None)
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)

    list_page = int(context.user_data.get("group_setting_list_page", 1) or 1)
    user = update.effective_user
    panel_user_id = user.id if user else 0
    keyboard = _build_group_panel_keyboard_for_user(
        chat_id_str, cfg, panel_user_id, list_page=list_page
    )
    if context.user_data.get("start_panel"):
        rows = list(keyboard.inline_keyboard)
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
        keyboard = InlineKeyboardMarkup(rows)
    await update.message.reply_text(
        _build_group_panel_text(chat_id_str, cfg),
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop

def register_group_setting_handlers(app):
    app.add_handler(CommandHandler("group_status", group_status))
    app.add_handler(CommandHandler("welcome", toggle_welcome))
    app.add_handler(CommandHandler("toggleverify", toggle_verification))
    app.add_handler(CommandHandler("silent", toggle_silent))
    app.add_handler(CommandHandler("_ad_filter", toggle_ad_filter))
    app.add_handler(CommandHandler("toggle_manor", toggle_manor))
    app.add_handler(CommandHandler("group", group_help))
    app.add_handler(CallbackQueryHandler(group_setting_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND),
            handle_group_setting_text,
        )
        ,
        group=-5,
    )
