import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from command_router import register_command
from feature_flags import FEATURE_LABELS, parse_feature_list, sanitize_features
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
    is_bot_running,
    start_bot,
    stop_bot,
    update_running_bot_features,
)
from utils import get_runtime_owner_id, is_bot_owner, is_super_admin, safe_reply

CALLBACK_PREFIX = "mbot"
MASTER_BOT_NAME = "小雅"
TEXT_STAGE_KEY = "multi_bot_stage"
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


def _is_master_panel(context: ContextTypes.DEFAULT_TYPE) -> bool:
    bot_name = str(context.application.bot_data.get("name", "")).strip()
    return bot_name == MASTER_BOT_NAME


def _can_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return bool(user and _is_master_panel(context) and _can_view_any_bot(user.id))


def _can_view_any_bot(user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    return any(int(item.get("owner_id") or 0) == int(user_id) for item in load_all_bot_configs())


def _can_view_bot(cfg: dict, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    return int(cfg.get("owner_id") or 0) == int(user_id)


def _can_edit_bot(user_id: int) -> bool:
    return is_super_admin(user_id)


def _visible_bot_configs(user_id: int) -> list[dict]:
    return [
        cfg
        for cfg in sorted(load_all_bot_configs(), key=lambda item: item.get("name", ""))
        if _can_view_bot(cfg, user_id)
    ]


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
        )
    return await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
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


def _build_detail_text(cfg: dict) -> str:
    name = cfg.get("name", "")
    source = "面板托管" if cfg.get("managed") else "环境变量"
    running = "运行中" if is_bot_running(name) else "未运行"
    clone_from = cfg.get("clone_from", "") or "无"
    lines = [
        "🤖 机器人详情",
        f"名称：{html.escape(name)}",
        f"状态：{running}",
        f"来源：{source}",
        f"Owner：<code>{int(cfg.get('owner_id') or 0)}</code>",
        f"克隆自：{html.escape(clone_from)}",
    ]
    if cfg.get("managed"):
        auto_start = "开启" if cfg.get("auto_start", True) else "关闭"
        lines.insert(3, f"重启恢复：{auto_start}")
    return "\n".join(lines)


def _build_detail_keyboard(cfg: dict, *, can_edit: bool) -> InlineKeyboardMarkup:
    name = cfg.get("name", "")
    rows = []
    if can_edit and name != MASTER_BOT_NAME:
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
            [InlineKeyboardButton("🧬 克隆这个机器人", callback_data=f"{CALLBACK_PREFIX}:clone:{name}")]
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
        if name != MASTER_BOT_NAME:
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
        _build_detail_text(cfg),
        reply_markup=_build_detail_keyboard(cfg, can_edit=_can_edit_bot(user_id)),
        parse_mode="HTML",
    )


@register_command("机器人功能", "查看机器人功能")
async def current_bot_features(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    owner_id = int(get_runtime_owner_id())
    if not (is_super_admin(user.id) or is_bot_owner(user.id)):
        return

    features = sorted(context.application.bot_data.get("enabled_features", []))
    feature_names = [FEATURE_LABELS.get(feature, feature) for feature in features]
    bot_name = str(context.application.bot_data.get("name", "机器人")).strip()
    text = (
        f"🤖 {bot_name}\n"
        f"Owner：<code>{owner_id}</code>\n"
        f"功能数量：{len(features)}\n"
        f"已开启功能：{ '、'.join(feature_names) if feature_names else '无' }"
    )
    await _panel_reply(update, context, text, parse_mode="HTML")


@register_command("机器人面板", "机器人管理")
async def multi_bot_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _can_manage(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊小雅打开机器人面板。")
    await _panel_reply(
        update,
        context,
        _build_list_text_for_user(int(update.effective_user.id)),
        reply_markup=_build_list_keyboard(int(update.effective_user.id)),
    )


async def handle_multi_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return
    if not _can_manage(update, context):
        return

    await query.answer()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    name = parts[2] if len(parts) > 2 else ""
    feature_key = parts[3] if len(parts) > 3 else ""

    if action == "list":
        return await _show_list(query)

    cfg = get_bot_config_by_name(name)
    if action == "open":
        if not cfg:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_view_bot(cfg, query.from_user.id):
            return await query.answer("你无权查看该机器人。", show_alert=True)
        return await _show_detail(query, cfg)

    if action == "start":
        if not cfg:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_edit_bot(query.from_user.id):
            return await query.answer("仅超级管理员可操作。", show_alert=True)
        ok, msg = await start_bot(cfg)
        if ok and cfg.get("managed"):
            update_managed_bot_auto_start(name, True)
        await query.answer(msg[:180], show_alert=not ok)
        fresh = get_bot_config_by_name(name) or cfg
        return await _show_detail(query, fresh)

    if action == "stop":
        if not cfg:
            return await query.answer("机器人不存在", show_alert=True)
        if not _can_edit_bot(query.from_user.id):
            return await query.answer("仅超级管理员可操作。", show_alert=True)
        ok, msg = await stop_bot(name)
        if ok and cfg.get("managed"):
            update_managed_bot_auto_start(name, False)
        await query.answer(msg[:180], show_alert=not ok)
        fresh = get_bot_config_by_name(name) or cfg
        return await _show_detail(query, fresh)

    if action == "clone":
        if not cfg:
            return await query.answer("源机器人不存在", show_alert=True)
        if not _can_edit_bot(query.from_user.id):
            return await query.answer("仅超级管理员可克隆机器人。", show_alert=True)
        context.user_data[TEXT_STAGE_KEY] = {
            "stage": "clone_meta",
            "source_name": name,
            "source_features": list(cfg.get("enabled_features", [])),
            "source_owner_id": int(cfg.get("owner_id") or 0),
        }
        return await query.edit_message_text(
            "请输入新机器人信息：\n"
            "格式：token, 名称, owner_id\n"
            "owner_id 可省略，默认继承源机器人。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 返回详情", callback_data=f"{CALLBACK_PREFIX}:open:{name}")]]
            ),
        )

    if action == "feature_toggle":
        managed = get_managed_bot_by_name(name)
        if not managed:
            return await query.answer("只能修改面板托管机器人。", show_alert=True)
        if not _can_edit_bot(query.from_user.id):
            return await query.answer("仅超级管理员可修改功能。", show_alert=True)
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
        if is_bot_running(name):
            update_running_bot_features(name, sorted(feature_set))
        await query.answer(f"{tip} {feature_key}")
        fresh = get_bot_config_by_name(name) or updated or managed
        return await _show_detail(query, fresh)

    if action == "delete":
        managed = get_managed_bot_by_name(name)
        if not managed:
            return await query.answer("只能删除面板托管机器人。", show_alert=True)
        if not _can_edit_bot(query.from_user.id):
            return await query.answer("仅超级管理员可删除机器人。", show_alert=True)
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
        if not _can_edit_bot(query.from_user.id):
            return await query.answer("仅超级管理员可删除机器人。", show_alert=True)
        if is_bot_running(name):
            await stop_bot(name)
        delete_managed_bot(name)
        return await _show_list(query)


async def handle_multi_bot_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _can_manage(update, context):
        return
    if not update.message or not update.message.text:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    state = context.user_data.get(TEXT_STAGE_KEY)
    if not isinstance(state, dict):
        return

    text = (update.message.text or "").strip()
    stage = state.get("stage")
    if not text:
        return

    if text in {"取消", "返回"}:
        context.user_data.pop(TEXT_STAGE_KEY, None)
        await _panel_reply(update, context, "已取消。")
        raise ApplicationHandlerStop

    if stage == "clone_meta":
        parts = [item.strip() for item in text.replace("\n", ",").split(",") if item.strip()]
        if len(parts) < 2:
            await _panel_reply(update, context, "格式错误，请按：token, 名称, owner_id")
            raise ApplicationHandlerStop
        token = parts[0]
        name = parts[1]
        if get_bot_config_by_name(name):
            await _panel_reply(update, context, "该机器人名称已存在，请换一个名称。")
            raise ApplicationHandlerStop
        owner_id = state.get("source_owner_id") or 0
        if len(parts) >= 3:
            if not parts[2].isdigit():
                await _panel_reply(update, context, "owner_id 必须是数字。")
                raise ApplicationHandlerStop
            owner_id = int(parts[2])

        state["new_token"] = token
        state["new_name"] = name
        state["new_owner_id"] = owner_id
        state["stage"] = "clone_features"
        context.user_data[TEXT_STAGE_KEY] = state
        await _panel_reply(
            update,
            context,
            "请输入要克隆的功能列表。\n"
            "发送“全部”表示继承全部功能；或发送逗号分隔的功能 key。",
        )
        raise ApplicationHandlerStop

    if stage == "clone_features":
        if text == "全部":
            features = list(state.get("source_features", []))
        else:
            features = sorted(
                sanitize_features(parse_feature_list(text), source_name="clone_features")
            )
        if not features:
            await _panel_reply(update, context, "至少保留一个有效功能。")
            raise ApplicationHandlerStop

        record = save_managed_bot(
            {
                "name": state.get("new_name"),
                "token": state.get("new_token"),
                "owner_id": state.get("new_owner_id"),
                "enabled": True,
                "auto_start": False,
                "enabled_features": features,
                "clone_from": state.get("source_name", ""),
            }
        )
        context.user_data.pop(TEXT_STAGE_KEY, None)
        ok, msg = await start_bot(record)
        if ok:
            record = update_managed_bot_auto_start(record["name"], True) or record
        await _panel_reply(update, context, f"✅ 已创建机器人：{record['name']}\n{msg}")
        raise ApplicationHandlerStop


def register_multi_bot_manager_handlers(app):
    app.add_handler(
        CallbackQueryHandler(handle_multi_bot_callback, pattern=rf"^{CALLBACK_PREFIX}:")
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), handle_multi_bot_text),
        group=20,
    )
