import html
import os
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

MASTER_BOT_NAME = str(os.getenv("MASTER_BOT_NAME", "")).strip()
from command_router import register_command
from feature_flags import ALL_FEATURES, FEATURE_LABELS
from multi_bot_registry import (
    delete_managed_bot,
    get_bot_config_by_name,
    get_managed_bot_by_name,
    load_all_bot_configs,
    save_managed_bot,
    update_managed_bot_auto_start,
    update_managed_bot_features,
)
from runtime_bot_manager import (
    get_running_app,
    is_bot_running,
    start_bot,
    stop_bot,
    update_running_bot_features,
)
from utils import BOT_USER_FILE, get_runtime_owner_id, is_bot_owner, is_super_admin, load_json, safe_reply

CALLBACK_PREFIX = "mbot"
SELF_SERVICE_CALLBACK_PREFIX = "pfbot"

TEXT_STAGE_KEY = "multi_bot_stage"
SELF_SERVICE_STAGE_KEY = "private_forward_self_service_stage"
SELF_SERVICE_FEATURES = ["private_forward"]
MANAGED_FEATURES = [
    ("economy", "经济系统"),
    ("entertainment", "娱乐功能"),
    ("game_hub", "玩法中心"),
    ("group", "群功能"),
    ("private_forward", "私聊转发"),
    ("channel", "频道功能"),
    ("lottery_betting", "彩票投注"),
    ("market_price", "市场行情"),
    ("my_bot", "智能回复"),
]
RESTART_HINT_FEATURES = {
    "economy",
    "entertainment",
    "game_hub",
    "group",
    "private_forward",
    "channel",
    "my_bot",
    "lottery_betting",
    "market_price",
}


def _is_master_panel(context: ContextTypes.DEFAULT_TYPE) -> bool:
    bot_name = str(context.application.bot_data.get("name", "")).strip()
    return bot_name == MASTER_BOT_NAME


def _is_master_private_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return bool(
        update.effective_user
        and chat
        and chat.type == "private"
        and _is_master_panel(context)
    )


def _can_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return bool(user and _is_master_panel(context) and _can_view_any_bot(user.id))


def _can_view_any_bot(user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    return any(int(item.get("owner_id") or 0) == int(user_id) for item in load_all_bot_configs())


def _no_bot_hint() -> str:
    return "你名下还没有机器人，请先创建双向机器人或克隆机器人。"


def _can_view_bot(cfg: dict, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    return int(cfg.get("owner_id") or 0) == int(user_id)


def _can_edit_bot(cfg: dict, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    return int(cfg.get("owner_id") or 0) == int(user_id)


def _can_control_bot(cfg: dict, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    return int(cfg.get("owner_id") or 0) == int(user_id)


def _can_self_service_clone(update: Update, context: ContextTypes.DEFAULT_TYPE, source_name: str) -> bool:
    return bool(
        source_name == MASTER_BOT_NAME
        and update.effective_user
        and update.effective_chat
        and update.effective_chat.type == "private"
        and _is_master_panel(context)
    )


def _can_continue_self_service_clone_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: Optional[dict]
) -> bool:
    return bool(
        isinstance(state, dict)
        and str(state.get("source_name", "")).strip() == MASTER_BOT_NAME
        and update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
        and _is_master_panel(context)
    )


def _visible_bot_configs(user_id: int) -> list[dict]:
    return [
        cfg
        for cfg in sorted(load_all_bot_configs(), key=lambda item: item.get("name", ""))
        if _can_view_bot(cfg, user_id)
    ]


def _find_bot_by_token(token: str):
    normalized = str(token or "").strip()
    if not normalized:
        return None
    for cfg in load_all_bot_configs():
        if str(cfg.get("token", "")).strip() == normalized:
            return cfg
    return None


def _unique_managed_name(base_name: str) -> str:
    base = (base_name or "").strip() or "private_forward_bot"
    if not get_bot_config_by_name(base):
        return base
    index = 2
    while get_bot_config_by_name(f"{base}_{index}"):
        index += 1
    return f"{base}_{index}"


def _build_self_service_keyboard(start_only: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("开始输入 API", callback_data=f"{SELF_SERVICE_CALLBACK_PREFIX}:start")]]
    if not start_only:
        rows.append([InlineKeyboardButton("返回", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _build_self_service_token_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("取消", callback_data=f"{SELF_SERVICE_CALLBACK_PREFIX}:cancel")]]
    )


def _build_self_service_name_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("返回", callback_data=f"{SELF_SERVICE_CALLBACK_PREFIX}:back_to_token")]]
    )


def _build_self_service_text() -> str:
    return (
        "🤖 创建双向机器人\n\n"
        "1. 打开 @BotFather\n"
        "2. 发送 /newbot\n"
        "3. 输入机器人名字，先随便填，后面还能改\n"
        "4. 输入用户名，必须以 bot 结尾\n"
        "5. 把 BotFather 返回的 API Token 发给我\n\n"
        "点击下方“开始输入 API”后，先把 token 发给我，再输入你要保存的机器人名字。\n"
        "我会自动创建一个仅开启“私聊转发”功能的双向机器人，owner_id 默认就是你自己的 Telegram ID。"
    )


def _build_clone_keyboard(source_name: str, *, start_only: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("开始输入 API", callback_data=f"{CALLBACK_PREFIX}:clone_start:{source_name}")]]
    if not start_only:
        back_callback = "start:back" if source_name == MASTER_BOT_NAME else f"{CALLBACK_PREFIX}:open:{source_name}"
        rows.append([InlineKeyboardButton("返回", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


def _build_clone_token_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("取消", callback_data=f"{CALLBACK_PREFIX}:clone_cancel")]]
    )


def _build_clone_name_keyboard(source_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("返回", callback_data=f"{CALLBACK_PREFIX}:clone_back_to_token:{source_name}")]]
    )


def _build_clone_owner_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("跳过", callback_data=f"{CALLBACK_PREFIX}:owner_skip"),
                InlineKeyboardButton("取消", callback_data=f"{CALLBACK_PREFIX}:clone_cancel"),
            ]
        ]
    )


def _build_clone_text(source_name: str) -> str:
    return (
        f"🤖 克隆机器人：{source_name}\n\n"
        "1. 打开 @BotFather\n"
        "2. 发送 /newbot\n"
        "3. 输入机器人名字，先随便填，后面还能改\n"
        "4. 输入用户名，必须以 bot 结尾\n"
        "5. 把 BotFather 返回的 API Token 发给我\n\n"
        "点击下方“开始输入 API”后，我会依次引导你输入 token、机器人名称、owner_id。\n"
        f"我会按 {source_name} 模板创建一个新机器人，并默认开启全部功能。"
    )


async def _fetch_bot_profile(token: str):
    probe_bot = Bot(token=token)
    try:
        await probe_bot.initialize()
        return await probe_bot.get_me()
    finally:
        try:
            await probe_bot.shutdown()
        except Exception:
            pass


async def _resolve_clone_owner_id(
    raw: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    text = str(raw or "").strip()
    if not update.effective_user:
        return None
    if text in {"跳过", "默认", "当前用户", "我", "skip"}:
        return int(update.effective_user.id)
    if text.isdigit():
        return int(text)
    if text.startswith("@") and len(text) > 1:
        username = text[1:].strip().lower()
        user_data = load_json(BOT_USER_FILE) or {}
        if isinstance(user_data, dict):
            for user_id, info in user_data.items():
                if not isinstance(info, dict):
                    continue
                saved_username = str(info.get("username", "") or "").strip().lstrip("@").lower()
                if saved_username and saved_username == username:
                    try:
                        return int(user_id)
                    except Exception:
                        pass
        try:
            chat = await context.bot.get_chat(text)
            return int(chat.id)
        except Exception:
            return None
    return None


async def _finalize_clone_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, owner_id: int
):
    features = sorted(ALL_FEATURES)

    record = save_managed_bot(
        {
            "name": state.get("new_name"),
            "token": state.get("new_token"),
            "owner_id": owner_id,
            "enabled": True,
            "auto_start": False,
            "enabled_features": features,
            "clone_from": state.get("source_name", ""),
            "username": state.get("bot_username", ""),
            "first_name": state.get("bot_first_name", ""),
        }
    )
    context.user_data.pop(TEXT_STAGE_KEY, None)
    ok, msg = await start_bot(record)
    if not ok:
        delete_managed_bot(record["name"])
        await _panel_reply(update, context, f"创建失败。\n{msg}")
        raise ApplicationHandlerStop

    record = update_managed_bot_auto_start(record["name"], True) or record
    feature_names = [FEATURE_LABELS.get(feature, feature) for feature in features]
    await _panel_reply(
        update,
        context,
        f"✅ 已创建机器人：{record['name']}\n"
        f"owner_id：{owner_id}\n"
        f"已启用功能：{'、'.join(feature_names)}\n"
        f"{msg}",
    )
    raise ApplicationHandlerStop


async def _delete_stage_prompt(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: Optional[dict]
) -> None:
    if not isinstance(state, dict):
        return
    message_id = state.pop("prompt_message_id", None)
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _send_stage_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state_key: str,
    state: dict,
    text: str,
    *,
    reply_markup=None,
    parse_mode=None,
):
    chat = update.effective_chat
    if not chat:
        return None
    await _delete_stage_prompt(context, chat.id, state)
    sent = await _panel_reply(
        update,
        context,
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    message_id = getattr(sent, "message_id", None)
    if message_id:
        state["prompt_message_id"] = message_id
        context.user_data[state_key] = state
    return sent


async def _reply_self_service_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update,
        context,
        _build_self_service_text(),
        reply_markup=_build_self_service_keyboard(),
    )


@register_command("双向机器人", "创建双向机器人")
async def private_forward_self_service_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_master_panel(context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊小雅创建双向机器人。")
    context.user_data.pop(SELF_SERVICE_STAGE_KEY, None)
    await _reply_self_service_guide(update, context)


async def _panel_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup=None,
    parse_mode=None,
):
    if update.message:
        return await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    return await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
    )


def _build_list_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for cfg in _visible_bot_configs(user_id):
        name = cfg.get("name", "")
        status = "🟢" if is_bot_running(name) else "⚫"
        source = "主" if name == MASTER_BOT_NAME else ("托管" if cfg.get("managed") else "环境")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{status} {name} [{source}]",
                    callback_data=f"{CALLBACK_PREFIX}:open:{name}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data=f"{CALLBACK_PREFIX}:list")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _build_list_text() -> str:
    return _build_list_text_for_user(0)


def _build_list_text_for_user(user_id: int) -> str:
    visible = _visible_bot_configs(user_id)
    total = len(visible)
    running = sum(1 for item in visible if is_bot_running(item.get("name", "")))
    scope = "全部机器人" if is_super_admin(user_id) else "你名下的机器人"
    return (
        "🤖 多机器人管理面板\n"
        f"主机器人：{MASTER_BOT_NAME}\n"
        f"范围：{scope}\n"
        f"总数：{total} | 运行中：{running}\n\n"
        "点击机器人进入详情查看功能。"
    )


async def _resolve_bot_username(cfg: dict) -> str:
    username = str(cfg.get("username", "") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    name = str(cfg.get("name", "") or "").strip()
    app = get_running_app(name)
    if app:
        username = str(getattr(app.bot, "username", "") or "").strip().lstrip("@")
        if username:
            return f"@{username}"
    return f"@{name}" if name else "未设置"


def _build_owner_link(owner_id: int) -> str:
    user_data = load_json(BOT_USER_FILE) or {}
    username = ""
    if isinstance(user_data, dict):
        info = user_data.get(str(owner_id))
        if isinstance(info, dict):
            username = str(info.get("username", "") or "").strip().lstrip("@")
    if username:
        return f'<a href="https://t.me/{html.escape(username)}">@{html.escape(username)}</a>'
    return f'<a href="tg://user?id={int(owner_id)}">{int(owner_id)}</a>'


def _build_bot_link(bot_username: str) -> str:
    username = str(bot_username or "").strip().lstrip("@")
    if not username or username == "未设置":
        return html.escape(str(bot_username or "未设置"))
    return f'<a href="https://t.me/{html.escape(username)}">@{html.escape(username)}</a>'


async def _build_detail_text(cfg: dict) -> str:
    name = cfg.get("name", "")
    running = "运行中" if is_bot_running(name) else "未运行"
    bot_username = await _resolve_bot_username(cfg)
    owner_id = int(cfg.get("owner_id") or 0)
    lines = [
        "🤖 机器人详情",
        f"名称：{html.escape(name)}",
        f"状态：{running}",
        f"归属：{_build_owner_link(owner_id)}",
        f"机器人：{_build_bot_link(bot_username)}",
    ]
    return "\n".join(lines)


def _build_detail_keyboard(cfg: dict, *, can_edit: bool, can_control: bool) -> InlineKeyboardMarkup:
    name = cfg.get("name", "")
    rows = []
    if can_control and name != MASTER_BOT_NAME:
        if is_bot_running(name):
            rows.append(
                [InlineKeyboardButton("🛑 停止机器人", callback_data=f"{CALLBACK_PREFIX}:stop:{name}")]
            )
        else:
            rows.append(
                [InlineKeyboardButton("▶️ 启动机器人", callback_data=f"{CALLBACK_PREFIX}:start:{name}")]
            )

    if can_edit:
        rows.append(
            [InlineKeyboardButton("🧬 克隆机器人", callback_data=f"{CALLBACK_PREFIX}:clone:{name}")]
        )

    if can_edit and cfg.get("managed"):
        enabled_features = set(cfg.get("enabled_features", []))
        feature_buttons = []
        for feature_key, feature_label in MANAGED_FEATURES:
            is_enabled = feature_key in enabled_features
            status = "✅ " if is_enabled else ""
            feature_buttons.append(
                InlineKeyboardButton(
                    f"{status}{feature_label}",
                    callback_data=f"{CALLBACK_PREFIX}:feature_toggle:{name}:{feature_key}",
                )
            )
        for idx in range(0, len(feature_buttons), 2):
            rows.append(feature_buttons[idx : idx + 2])

    if can_control and cfg.get("managed") and name != MASTER_BOT_NAME:
        rows.append(
            [InlineKeyboardButton("🗑 删除机器人", callback_data=f"{CALLBACK_PREFIX}:delete:{name}")]
        )

    rows.append([InlineKeyboardButton("⬅️ 返回列表", callback_data=f"{CALLBACK_PREFIX}:list")])
    return InlineKeyboardMarkup(rows)


async def _show_list(query):
    user_id = int(query.from_user.id)
    text = _build_list_text_for_user(user_id)
    markup = _build_list_keyboard(user_id)
    return await query.edit_message_text(text, reply_markup=markup)


async def _show_detail(query, cfg: dict):
    user_id = int(query.from_user.id)
    return await query.edit_message_text(
        await _build_detail_text(cfg),
        reply_markup=_build_detail_keyboard(
            cfg,
            can_edit=_can_edit_bot(cfg, user_id),
            can_control=_can_control_bot(cfg, user_id),
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@register_command("机器人功能", "查看机器人功能")
async def current_bot_features(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if not (is_super_admin(user.id) or is_bot_owner(user.id)):
        return

    features = sorted(context.application.bot_data.get("enabled_features", []))
    feature_names = [FEATURE_LABELS.get(feature, feature) for feature in features]
    bot_name = str(context.application.bot_data.get("name", "机器人")).strip()
    bot_username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    owner_id = int(get_runtime_owner_id())
    text = (
        f"🤖 {bot_name}\n"
        f"归属：{_build_owner_link(owner_id)}\n"
        f"机器人：{_build_bot_link(bot_username or bot_name)}\n"
        f"功能数量：{len(features)}\n"
        f"已开启功能：{ '、'.join(feature_names) if feature_names else '无' }"
    )
    await _panel_reply(update, context, text, parse_mode="HTML")


@register_command("机器人面板", "机器人管理")
async def multi_bot_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊小雅打开机器人面板。")
    if not _is_master_panel(context):
        return
    user = update.effective_user
    if not user:
        return
    if not _can_view_any_bot(user.id):
        return await safe_reply(update, context, _no_bot_hint())
    await _panel_reply(
        update,
        context,
        _build_list_text_for_user(int(user.id)),
        reply_markup=_build_list_keyboard(int(user.id)),
    )


async def handle_multi_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return

    await query.answer()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    name = parts[2] if len(parts) > 2 else ""
    feature_key = parts[3] if len(parts) > 3 else ""

    if action == "list":
        if not _is_master_panel(context):
            return
        if not _can_view_any_bot(query.from_user.id):
            return await query.answer(_no_bot_hint(), show_alert=True)
        return await _show_list(query)

    if action == "clone_cancel":
        state = context.user_data.get(TEXT_STAGE_KEY)
        await _delete_stage_prompt(context, query.message.chat.id, state)
        context.user_data.pop(TEXT_STAGE_KEY, None)
        return await query.edit_message_text("已取消克隆机器人。")

    if action == "clone_back_to_token":
        if not _can_manage(update, context):
            return
        if not name:
            return await query.answer("源机器人不存在", show_alert=True)
        state = context.user_data.get(TEXT_STAGE_KEY)
        if not isinstance(state, dict):
            return await query.answer("当前不在克隆流程中。", show_alert=True)
        state["stage"] = "clone_await_token"
        state.pop("new_token", None)
        state.pop("new_name", None)
        state["prompt_message_id"] = getattr(getattr(query, "message", None), "message_id", None)
        context.user_data[TEXT_STAGE_KEY] = state
        return await query.edit_message_text(
            "请输入 API。\n"
            "格式通常是：1234567890:AA...",
            reply_markup=_build_clone_token_keyboard(),
        )

    if action == "owner_skip":
        state = context.user_data.get(TEXT_STAGE_KEY)
        if not isinstance(state, dict) or state.get("stage") != "clone_await_owner_id":
            return await query.answer("当前不在设置归属人的步骤。", show_alert=True)
        await _delete_stage_prompt(context, query.message.chat.id, state)
        return await _finalize_clone_creation(update, context, state, int(query.from_user.id))

    cfg = get_bot_config_by_name(name)
    allow_self_service_clone = _can_self_service_clone(update, context, name)

    if action == "clone":
        if not cfg:
            return await query.answer("源机器人不存在", show_alert=True)
        if not (allow_self_service_clone or _can_edit_bot(cfg, query.from_user.id)):
            return await query.answer("仅机器人所有者或超级管理员可克隆该机器人。", show_alert=True)
        return await query.edit_message_text(
            _build_clone_text(name),
            reply_markup=_build_clone_keyboard(name),
        )

    if action == "clone_start":
        if not cfg:
            return await query.answer("源机器人不存在", show_alert=True)
        if not (allow_self_service_clone or _can_edit_bot(cfg, query.from_user.id)):
            return await query.answer("仅机器人所有者或超级管理员可克隆该机器人。", show_alert=True)
        context.user_data[TEXT_STAGE_KEY] = {
            "stage": "clone_await_token",
            "source_name": name,
            "source_owner_id": int(cfg.get("owner_id") or 0),
            "prompt_message_id": getattr(getattr(query, "message", None), "message_id", None),
        }
        return await query.edit_message_text(
            "请输入 API。\n"
            "格式通常是：1234567890:AA...",
            reply_markup=_build_clone_token_keyboard(),
        )

    if not _can_manage(update, context):
        return

    if action == "open":
        if not cfg:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_view_bot(cfg, query.from_user.id):
            return await query.answer("你无权查看该机器人。", show_alert=True)
        return await _show_detail(query, cfg)

    if action == "start":
        if not cfg:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_control_bot(cfg, query.from_user.id):
            return await query.answer("仅机器人所有者或超级管理员可操作。", show_alert=True)
        ok, msg = await start_bot(cfg)
        if ok and cfg.get("managed"):
            update_managed_bot_auto_start(name, True)
        await query.answer(msg[:180], show_alert=not ok)
        fresh = get_bot_config_by_name(name) or cfg
        return await _show_detail(query, fresh)

    if action == "stop":
        if not cfg:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_control_bot(cfg, query.from_user.id):
            return await query.answer("仅机器人所有者或超级管理员可操作。", show_alert=True)
        ok, msg = await stop_bot(name)
        if ok and cfg.get("managed"):
            update_managed_bot_auto_start(name, False)
        await query.answer(msg[:180], show_alert=not ok)
        fresh = get_bot_config_by_name(name) or cfg
        return await _show_detail(query, fresh)

    if action == "feature_toggle":
        managed = get_managed_bot_by_name(name)
        if not managed:
            return await query.answer("只能修改面板托管机器人。", show_alert=True)
        if not _can_edit_bot(managed, query.from_user.id):
            return await query.answer("仅机器人所有者或超级管理员可修改功能。", show_alert=True)
        if feature_key not in {item[0] for item in MANAGED_FEATURES}:
            return await query.answer("功能不存在。", show_alert=True)

        feature_set = set(managed.get("enabled_features", []))
        enabled = feature_key in feature_set
        if enabled and len(feature_set) == 1:
            return await query.answer("不能把功能全部关闭。", show_alert=True)

        if enabled:
            feature_set.remove(feature_key)
            tip = "已关闭"
        else:
            feature_set.add(feature_key)
            tip = "已开启"

        updated = update_managed_bot_features(name, sorted(feature_set))
        is_running = is_bot_running(name)
        if is_running:
            update_running_bot_features(name, sorted(feature_set))
        message = f"{tip} {FEATURE_LABELS.get(feature_key, feature_key)}"
        if is_running and feature_key in RESTART_HINT_FEATURES:
            message += "，重启后完全生效"
        await query.answer(message, show_alert=False)
        fresh = get_bot_config_by_name(name) or updated or managed
        return await _show_detail(query, fresh)

    if action == "delete":
        managed = get_managed_bot_by_name(name)
        if not managed:
            return await query.answer("只能删除面板托管机器人。", show_alert=True)
        if not _can_control_bot(managed, query.from_user.id):
            return await query.answer("仅机器人所有者或超级管理员可删除机器人。", show_alert=True)
        return await query.edit_message_text(
            f"确认删除机器人 {name}？",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("确认删除", callback_data=f"{CALLBACK_PREFIX}:delete_confirm:{name}")],
                    [InlineKeyboardButton("取消", callback_data=f"{CALLBACK_PREFIX}:open:{name}")],
                ]
            ),
        )

    if action == "delete_confirm":
        managed = get_managed_bot_by_name(name)
        if not managed:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_control_bot(managed, query.from_user.id):
            return await query.answer("仅机器人所有者或超级管理员可删除机器人。", show_alert=True)
        if is_bot_running(name):
            await stop_bot(name)
        delete_managed_bot(name)
        return await _show_list(query)


async def handle_private_forward_self_service_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{SELF_SERVICE_CALLBACK_PREFIX}:"):
        return
    if not _is_master_private_chat(update, context):
        return

    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "guide":
        context.user_data.pop(SELF_SERVICE_STAGE_KEY, None)
        return await query.edit_message_text(
            _build_self_service_text(),
            reply_markup=_build_self_service_keyboard(),
        )

    if action == "cancel":
        context.user_data.pop(SELF_SERVICE_STAGE_KEY, None)
        return await query.edit_message_text("已取消创建双向机器人。")

    if action == "back_to_token":
        state = context.user_data.get(SELF_SERVICE_STAGE_KEY)
        if not isinstance(state, dict):
            return await query.answer("当前不在创建流程中。", show_alert=True)
        state["stage"] = "await_token"
        state.pop("token", None)
        state.pop("bot_username", None)
        state.pop("bot_first_name", None)
        state["prompt_message_id"] = getattr(getattr(query, "message", None), "message_id", None)
        context.user_data[SELF_SERVICE_STAGE_KEY] = state
        return await query.edit_message_text(
            "请输入 API。\n"
            "格式通常是：`1234567890:AA...`",
            reply_markup=_build_self_service_token_keyboard(),
            parse_mode="Markdown",
        )

    if action == "start":
        context.user_data[SELF_SERVICE_STAGE_KEY] = {
            "stage": "await_token",
            "prompt_message_id": getattr(getattr(query, "message", None), "message_id", None),
        }
        return await query.edit_message_text(
            "请输入 API。\n"
            "格式通常是：`1234567890:AA...`",
            reply_markup=_build_self_service_token_keyboard(),
            parse_mode="Markdown",
        )


async def handle_private_forward_self_service_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not _is_master_private_chat(update, context):
        return
    if not update.message or not update.message.text or not update.effective_user:
        return

    state = context.user_data.get(SELF_SERVICE_STAGE_KEY)
    if not isinstance(state, dict):
        return

    text = (update.message.text or "").strip()
    stage = state.get("stage")
    if not text:
        return
    if text in {"取消", "返回"}:
        await _delete_stage_prompt(context, update.effective_chat.id, state)
        context.user_data.pop(SELF_SERVICE_STAGE_KEY, None)
        await _panel_reply(update, context, "已取消创建双向机器人。")
        raise ApplicationHandlerStop
    if stage == "await_token":
        if ":" not in text:
            await _panel_reply(update, context, "API Token 格式不对，请直接发送 BotFather 给你的完整 token。")
            raise ApplicationHandlerStop

        token = text
        existing = _find_bot_by_token(token)
        if existing:
            context.user_data.pop(SELF_SERVICE_STAGE_KEY, None)
            await _panel_reply(
                update,
                context,
                f"这个 API 已经在使用了，对应机器人：{existing.get('name', '未知机器人')}。",
            )
            raise ApplicationHandlerStop

        try:
            me = await _fetch_bot_profile(token)
        except Exception:
            await _panel_reply(update, context, "API Token 无效，或暂时无法连接 Telegram，请检查后重试。")
            raise ApplicationHandlerStop

        state["stage"] = "await_name"
        state["token"] = token
        state["bot_username"] = (getattr(me, "username", "") or "").strip()
        state["bot_first_name"] = (getattr(me, "first_name", "") or "").strip()
        suggest_name = (
            state["bot_username"]
            or state["bot_first_name"]
            or f"private_forward_{update.effective_user.id}"
        )
        await _send_stage_prompt(
            update,
            context,
            SELF_SERVICE_STAGE_KEY,
            state,
            "API 验证成功。\n"
            "请输入新机器人的名称。\n"
            f"建议名称：{suggest_name}\n\n"
            "如需重新输入 API，可点下方“返回”。",
            reply_markup=_build_self_service_name_keyboard(),
        )
        raise ApplicationHandlerStop

    if stage == "await_name":
        managed_name = text
        if get_bot_config_by_name(managed_name):
            await _panel_reply(update, context, "这个机器人名字已存在，请换一个名字。")
            raise ApplicationHandlerStop

        await _delete_stage_prompt(context, update.effective_chat.id, state)
        record = save_managed_bot(
            {
                "name": managed_name,
                "token": state.get("token"),
                "owner_id": int(update.effective_user.id),
                "enabled": True,
                "auto_start": False,
                "enabled_features": list(SELF_SERVICE_FEATURES),
                "clone_from": MASTER_BOT_NAME,
                "username": state.get("bot_username", ""),
                "first_name": state.get("bot_first_name", ""),
            }
        )
        ok, msg = await start_bot(record)
        context.user_data.pop(SELF_SERVICE_STAGE_KEY, None)
        if not ok:
            delete_managed_bot(record["name"])
            await _panel_reply(update, context, f"创建失败。\n{msg}")
            raise ApplicationHandlerStop

        record = update_managed_bot_auto_start(record["name"], True) or record
        username = state.get("bot_username") or record["name"]
        first_name = state.get("bot_first_name") or record["name"]
        await _panel_reply(
            update,
            context,
            "✅ 双向机器人创建成功\n"
            f"名称：{first_name}\n"
            f"用户名：@{username}\n"
            f"面板名称：{record['name']}\n"
            f"owner_id：{int(update.effective_user.id)}\n"
            "已启用功能：私聊转发\n"
            f"{msg}",
        )
        raise ApplicationHandlerStop


async def handle_multi_bot_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    state = context.user_data.get(TEXT_STAGE_KEY)
    if not isinstance(state, dict):
        return
    if not (_can_manage(update, context) or _can_continue_self_service_clone_text(update, context, state)):
        return

    text = (update.message.text or "").strip()
    stage = state.get("stage")
    if not text:
        return

    if text in {"取消", "返回"}:
        await _delete_stage_prompt(context, update.effective_chat.id, state)
        context.user_data.pop(TEXT_STAGE_KEY, None)
        await _panel_reply(update, context, "已取消。")
        raise ApplicationHandlerStop

    if stage == "clone_await_token":
        if ":" not in text:
            await _panel_reply(update, context, "API Token 格式不对，请直接发送完整 token。")
            raise ApplicationHandlerStop

        existing = _find_bot_by_token(text)
        if existing:
            await _panel_reply(
                update,
                context,
                f"这个 API 已经在使用了，对应机器人：{existing.get('name', '未知机器人')}。",
            )
            raise ApplicationHandlerStop

        try:
            me = await _fetch_bot_profile(text)
        except Exception:
            await _panel_reply(update, context, "API Token 无效，或暂时无法连接 Telegram，请检查后重试。")
            raise ApplicationHandlerStop

        state["new_token"] = text
        state["bot_username"] = (getattr(me, "username", "") or "").strip()
        state["bot_first_name"] = (getattr(me, "first_name", "") or "").strip()
        state["stage"] = "clone_await_name"
        await _send_stage_prompt(
            update,
            context,
            TEXT_STAGE_KEY,
            state,
            "API 验证成功。\n请输入新机器人的名称。\n\n如需重新输入 API，可点下方“返回”。",
            reply_markup=_build_clone_name_keyboard(state.get("source_name", "")),
        )
        raise ApplicationHandlerStop

    if stage == "clone_await_name":
        if get_bot_config_by_name(text):
            await _panel_reply(update, context, "该机器人名称已存在，请换一个名称。")
            raise ApplicationHandlerStop

        state["new_name"] = text
        state["stage"] = "clone_await_owner_id"
        default_owner_id = int(state.get("source_owner_id") or 0)
        await _send_stage_prompt(
            update,
            context,
            TEXT_STAGE_KEY,
            state,
            "请输入机器人归属人。\n"
            f"默认为当前操作用户（{update.effective_user.id}），可点击“跳过”，或直接输入 @用户名 / 数字 ID。\n"
            f"当前模板归属人：{default_owner_id}\n\n"
            "也可以直接点下方按钮。",
            reply_markup=_build_clone_owner_keyboard(),
        )
        raise ApplicationHandlerStop

    if stage == "clone_await_owner_id":
        owner_id = await _resolve_clone_owner_id(text, update, context)
        if owner_id is None:
            await _panel_reply(
                update,
                context,
                "owner_id 输入无效，请发送数字 ID、@用户名，或发送“跳过”。",
            )
            raise ApplicationHandlerStop

        await _delete_stage_prompt(context, update.effective_chat.id, state)
        await _finalize_clone_creation(update, context, state, owner_id)


def register_multi_bot_manager_handlers(app):
    app.add_handler(
        CallbackQueryHandler(
            handle_private_forward_self_service_callback,
            pattern=rf"^{SELF_SERVICE_CALLBACK_PREFIX}:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(handle_multi_bot_callback, pattern=rf"^{CALLBACK_PREFIX}:")
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND),
            handle_private_forward_self_service_text,
        ),
        group=19,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), handle_multi_bot_text),
        group=20,
    )
