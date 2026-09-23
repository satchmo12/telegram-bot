# === bot.py 主文件 ===
import asyncio
import html
import os
import sys
import re
import logging
from datetime import datetime
from datetime import time
from typing import Optional
from dotenv import load_dotenv

from customer.customer_qa import handle_customer_qa, handle_business_connection
from customer.editUserInfo import handle_media
from group.invite_stats import load_invite_link_map
from tool.utils.update_helper import get_message
load_dotenv(override=True)

from telegram import InlineQueryResultCachedPhoto, InlineQueryResultPhoto, MessageEntity, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    ApplicationBuilder,
    BusinessConnectionHandler,
    MessageHandler,
    CommandHandler,
    ApplicationHandlerStop,
    TypeHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
)

from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut, InvalidToken

from group.group_logger import GROUPS_FILE
from forward.message_forward import (
    forward_to_owner,
    handle_private_dialog_callback,
    owner_auto_forward_in_dialog,
    reply_from_owner,
)
from menu import build_feature_intro
from modules import register_all_handlers  # 注册各功能模块
from dispatcher import message_router  # 最终文本处理路由器
from channel.telethon_forwarder import start_telethon_forwarder_job, stop_telethon_forwarder
from channel.telethon_login import _clear_login_state
from info.storage import ensure_info_migrated
from channel.channel_config import is_active_subscription
from channel.publish_setting import (
    handle_comment_start_parameter,
    handle_report_start_parameter,
    load_publish_config,
)
from custom_command_templates import (
    register_custom_command_handlers,
    visible_bot_commands,
    visible_reply_labels,
)
from command_router import get_matched_command
from admin_permissions import has_admin_permission, is_owner_or_super_admin

from chat.my_bot import cleaned_word
from chat.gemini_chat import handle_gemini_ai
from run_daily import (
    daily_master_job,
    five_minute_master_job,
    hour_master_job,
    ten_minute_master_job,
)
from feature_flags import ALL_FEATURES, is_feature_enabled, parse_feature_list, sanitize_features
from multi_bot_registry import load_all_bot_configs
from runtime_bot_manager import (
    configure_runtime_hooks,
    register_running_app,
    unregister_running_app,
)
from utils import (
    get_group_whitelist,
    is_super_admin,
    load_json,
    safe_reply,
    save_json,
    is_bot_owner,
    set_bot_owner,
    set_bot_timezone,
    set_runtime_bot_name,
)

# ===== 注册 Telegram / 命令 =====
from telegram import BotCommand
import uuid

async def show_menu(update, context):
    # Visible custom command templates are also exposed as reply-keyboard
    # shortcuts. Adding/removing a visible template updates this menu too.
    keyboard = [["📅每日签到"]]
    for label in visible_reply_labels():
        if len(keyboard[-1]) >= 2:
            keyboard.append([])
        keyboard[-1].append(label)
    keyboard.append(["🏆排行榜", "💰我的积分"])

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,   # 自动缩小
        one_time_keyboard=False # 不自动关闭
    )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="请选择功能：",
        reply_markup=reply_markup
    )

    # 注意：ReplyKeyboard 的按钮无法“一键打开链接”，只能发送文本。
    # 需要用 InlineKeyboard 的 url 按钮才能做到点一次直接跳转。
    # await context.bot.send_message(
    #     chat_id=update.effective_chat.id,
    #     text="快捷入口：",
    #     reply_markup=InlineKeyboardMarkup(
    #         [[InlineKeyboardButton("招商负责人（点此跳转）", url="https://t.me/mr566")]]
    #     ),
    # )

async def hide_menu(update, context):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="菜单已关闭",
        reply_markup=ReplyKeyboardRemove()
    )

def _is_stale_callback_query_error(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "query is too old" in msg
        or "response timeout expired" in msg
        or "query id is invalid" in msg
    )


async def error_handler(update, context):
    err = getattr(context, "error", None)

    # 1️⃣ 基础信息
    bot_name = context.bot.username if context.bot else "unknown_bot"

    chat_id = None
    chat_type = None
    user_id = None
    update_type = type(update).__name__

    if update:
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)

        if chat:
            chat_id = chat.id
            chat_type = chat.type

        if user:
            user_id = user.id

    base_info = (
        f"[BOT: @{bot_name}] "
        f"[CHAT: {chat_id} ({chat_type})] "
        f"[USER: {user_id}] "
        f"[UPDATE: {update_type}]"
    )

    # 2️⃣ 过滤已知错误
    if err and _is_stale_callback_query_error(err):
        logging.info("忽略过期按钮回调 | %s | %s", base_info, err)
        return

    if isinstance(err, NetworkError):
        msg = str(err).lower()

        if "message to delete not found" in msg:
            return

        if "not enough rights to send text messages" in msg:
            logging.warning(
                "无发言权限错误 | %s | ERROR: %s",
                base_info,
                err
            )
            return

        logging.warning(
            "网络错误 | %s | ERROR: %s",
            base_info,
            err
        )
        return

    if isinstance(err, TimedOut):
        logging.warning(
            "请求超时 | %s | ERROR: %s",
            base_info,
            err
        )
        return

    logging.exception(
        "未处理异常 | %s",
        base_info,
        exc_info=err
    )

# ===== 日志设置 =====
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.WARNING,
)
logging.getLogger("apscheduler").setLevel(logging.ERROR)


DEFAULT_OWNER_ID = 6085551760
MASTER_BOT_NAME = str(os.getenv("MASTER_BOT_NAME", "")).strip()
MASTER_BOT_USERNAME = str(os.getenv("MASTER_BOT_USERNAME", "")).strip().lstrip("@")
PRIVATE_FORWARD_SELF_SERVICE_STAGE_KEY = "private_forward_self_service_stage"
MULTI_BOT_STAGE_KEY = "multi_bot_stage"
STARTUP_DEBUG_FILE = os.path.join("data", "startup_debug.log")

WAITING_POST = "waiting_post"
REPLY_BOTTLE = "reply_bottle"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photo = update.message.photo[-1]
    print("FILE_ID:")
    print(photo.file_id)
    await update.message.reply_text(photo.file_id)


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()
    bot_username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    bot_name = str(context.application.bot_data.get("name", "") or "").strip() or "机器人"
    display_bot = f"@{bot_username}" if bot_username else bot_name

    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"当前机器人：{display_bot}",
            description=f"收到的 inline 请求来自 {display_bot}",
            input_message_content=InputTextMessageContent(
                f"🤖 当前监听到该 inline 请求的机器人：{display_bot}\n"
                f"查询内容：{query or '（空）'}"
            ),
        )
    ]

    # chat_id = int(update.effective_chat.id) if update and update.effective_chat else None
    # await context.bot.send_message(
    #             chat_id=chat_id,
    #             text=results,
    #         )

    await update.inline_query.answer(results=results, cache_time=0, is_personal=True)


def write_startup_debug(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(STARTUP_DEBUG_FILE), exist_ok=True)
        with open(STARTUP_DEBUG_FILE, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def load_bot_configs():
    configs = load_all_bot_configs()
    if not configs:
        raise RuntimeError(
            "未找到机器人配置。请在环境变量或面板托管配置中添加机器人。"
        )
    return configs


def load_startup_bot_configs():
    configs = load_bot_configs()
    return [
        cfg
        for cfg in configs
        if not cfg.get("managed") or cfg.get("auto_start", True)
    ]


def bind_runtime_bot_context(context: ContextTypes.DEFAULT_TYPE):
    bot_name = context.application.bot_data.get("name", "")
    set_runtime_bot_name(bot_name)
    set_bot_timezone(bot_name, context.application.bot_data.get("timezone", "Asia/Shanghai"))


async def runtime_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)


async def block_disabled_group_messages(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    bind_runtime_bot_context(context)
    chat = update.effective_chat
    if not update.message or not chat or chat.type not in {"group", "supergroup"}:
        return

    cfg = get_group_whitelist(context).get(str(chat.id), {})
    if isinstance(cfg, dict) and not bool(cfg.get("bot_enabled", True)):
        raise ApplicationHandlerStop


async def owner_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    # 投稿内容、审核拒绝原因都不能被私聊双向转发当作回复用户的消息处理。
    if (
        (context.user_data or {}).get(WAITING_POST)
        or (context.user_data or {}).get("publish_reject_reason")
        or (context.user_data or {}).get("publish_keyword_label_input")
        or (context.user_data or {}).get("delegated_admin_add_stage")
    ):
        return

    if not update.effective_user or not has_admin_permission(
        context, update.effective_user.id, "private_forward"
    ):
        return
    if update.message and update.message.text:
        matched = get_matched_command(update.message.text)
        if matched:
            return
    await reply_from_owner(update, context)


async def private_forward_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)

    user = update.effective_user
    chat = update.effective_chat

    user_data = context.user_data or {}

    # 投稿及审核拒绝原因由投稿模块处理；不要再走私聊双向转发。
    if (
        user_data.get(WAITING_POST)
        or user_data.get("publish_reject_reason")
        or user_data.get("publish_keyword_search")
        or user_data.get("publish_template_draft")
        or user_data.get("publish_template_flow")
        or user_data.get("publish_keyword_label_input")
    ):
        print(
            "[private_forward_router] 忽略：当前正在投稿、关键词搜索或填写拒绝原因"
        )
        return

    if (
        str(context.application.bot_data.get("name", "")).strip() == MASTER_BOT_NAME
        and (
            isinstance(
                user_data.get(PRIVATE_FORWARD_SELF_SERVICE_STAGE_KEY),
                dict,
            )
            or isinstance(
                user_data.get(MULTI_BOT_STAGE_KEY),
                dict,
            )
            or user_data.get(REPLY_BOTTLE)
        )
    ):
        print(
            "[private_forward_router] 忽略：主机器人当前处于自助/多机器人输入阶段"
        )
        return

    # 获取消息
    msg = get_message(update)

    if not msg:
        return

    # =========================================================
    # 1. 优先处理“转发消息”
    #
    # 注意：
    # 转发的频道消息里面可能包含 custom_emoji，
    # 所以这里必须先判断 forward_origin，
    # 不能先判断会员表情。
    # =========================================================

    forward_origin = getattr(msg, "forward_origin", None)

    if forward_origin:
        print(
            "[private_forward_router] 检测到转发消息:",
            repr(forward_origin),
        )

        # -----------------------------------------------------
        # 1.1 转发的是普通用户消息
        # -----------------------------------------------------
        sender_user = getattr(forward_origin, "sender_user", None)

        if sender_user:
            print(
                f"[private_forward_router] 转发用户消息，用户ID={sender_user.id}"
            )

            await msg.reply_text(
                f"✅ 用户ID：<code>{sender_user.id}</code>",
                parse_mode="HTML",
            )
            return

        # -----------------------------------------------------
        # 1.2 转发的是频道消息
        # -----------------------------------------------------
        origin_chat = getattr(forward_origin, "chat", None)

        if origin_chat:
            chat_type = getattr(origin_chat, "type", None)

            # python-telegram-bot 的 Chat.type 一般是字符串
            # 同时兼容枚举/对象形式
            chat_type_name = getattr(chat_type, "name", None)

            if (
                chat_type == "channel"
                or chat_type == "CHANNEL"
                or chat_type_name == "CHANNEL"
            ):
                channel = origin_chat

                username = getattr(channel, "username", "") or ""
                title = getattr(channel, "title", "") or ""
                channel_id = channel.id

                # 转发来源消息 ID
                origin_msg_id = getattr(
                    forward_origin,
                    "message_id",
                    None,
                )

                msg_id_val = (
                    origin_msg_id
                    if origin_msg_id is not None
                    else msg.message_id
                )

                print(
                    f"[private_forward_router] 转发频道消息："
                    f"channel_id={channel_id}, "
                    f"message_id={msg_id_val}, "
                    f"username={username}, "
                    f"title={title}"
                )

                channel_id_text = f"<code>{channel_id}</code>"
                msg_id_text = f"<code>{msg_id_val}</code>"

                if username:
                    text = (
                        f"✅ 频道ID：{channel_id_text}\n"
                        f"消息ID：{msg_id_text}\n"
                        f"频道用户名：@{username}\n"
                        f"频道名：{title}"
                    )
                else:
                    text = (
                        f"✅ 频道ID：{channel_id_text}\n"
                        f"消息ID：{msg_id_text}\n"
                        f"频道名：{title}"
                    )

                await msg.reply_text(
                    text,
                    parse_mode="HTML",
                )
                return

        # -----------------------------------------------------
        # 1.3 如果是其他类型的转发消息
        # -----------------------------------------------------
        print(
            "[private_forward_router] 转发消息，但没有识别到用户或频道"
        )

    # =========================================================
    # 2. 非转发消息，才检查会员/自定义表情 ID
    # =========================================================

    for entity in msg.entities or []:
        if entity.type == "custom_emoji":
            emoji_id = entity.custom_emoji_id

            print(
                f"[private_forward_router] 检测到会员表情，ID={emoji_id}"
            )

            await msg.reply_text(
                f"会员表情 ID：\n<code>{emoji_id}</code>",
                parse_mode="HTML",
            )
            return

    # =========================================================
    # 3. 检查命令
    # =========================================================

    if msg.text:
        matched_command = get_matched_command(msg.text)

        if matched_command:
            print(
                f"[private_forward_router] 忽略：命中命令 {matched_command}"
            )
            return

    # =========================================================
    # 4. 判断频道
    #
    # 这里保留你原来的逻辑。
    # 主要用于 sender_chat 等场景。
    # =========================================================

    sender_chat = getattr(msg, "sender_chat", None)

    forward_origin = getattr(msg, "forward_origin", None)

    origin_chat = (
        getattr(forward_origin, "chat", None)
        if forward_origin
        else None
    )

    channel = None

    if (
        sender_chat
        and getattr(sender_chat, "type", None)
        and (
            sender_chat.type == "channel"
            or sender_chat.type == "CHANNEL"
            or getattr(sender_chat.type, "name", None) == "CHANNEL"
        )
    ):
        channel = sender_chat

    elif (
        origin_chat
        and getattr(origin_chat, "type", None)
        and (
            origin_chat.type == "channel"
            or origin_chat.type == "CHANNEL"
            or getattr(origin_chat.type, "name", None) == "CHANNEL"
        )
    ):
        channel = origin_chat

    if channel:
        username = getattr(channel, "username", "") or ""
        title = getattr(channel, "title", "") or ""
        channel_id = f"<code>{channel.id}</code>"

        origin_msg_id = (
            getattr(forward_origin, "message_id", None)
            if forward_origin
            else None
        )

        msg_id_val = (
            origin_msg_id
            if origin_msg_id is not None
            else msg.message_id
        )

        msg_id = f"<code>{msg_id_val}</code>"

        if username:
            text = (
                f"✅ 频道ID：{channel_id} 点击红色数字拷贝\n"
                f"消息ID：{msg_id}\n"
                f"频道用户名：@{username}\n"
                f"频道名：{title}"
            )
        else:
            text = (
                f"✅ 频道ID：{channel_id}\n"
                f"消息ID：{msg_id}\n"
                f"频道名：{title}"
            )

        await msg.reply_text(
            text,
            parse_mode="HTML",
        )
        return

    # =========================================================
    # 5. 主机器人私聊 AI
    #
    # 开启后不再转发给主人
    # =========================================================

    if await handle_gemini_ai(update, context):
        raise ApplicationHandlerStop

    # =========================================================
    # 6. 机器人转发
    # =========================================================

    await forward_to_owner(update, context)

    # =========================================================
    # 7. 客服机器人自动回复
    # =========================================================

    await handle_customer_qa(update, context)

    # =========================================================
    # 8. 客服机器人修改功能
    # =========================================================

    await handle_media(update, context)
    # await forward_to_owner(update, context)


START_WELCOME_FILE = "config_data/start_welcome.json"
START_WELCOME_EDIT_KEY = "start_welcome_editing"


def _load_start_welcome_config() -> dict:
    data = load_json(START_WELCOME_FILE)
    return data if isinstance(data, dict) else {}


def _can_configure_start_welcome(context: ContextTypes.DEFAULT_TYPE, user) -> bool:
    if not user:
        return False
    try:
        is_owner = int(user.id) == int(context.application.bot_data.get("owner_id"))
    except (TypeError, ValueError):
        is_owner = False
    return bool(is_owner)

    # This setting is intentionally stricter than ordinary admin settings.
    return bool(is_owner and is_active_subscription(user))


def _welcome_template_text(bot_name: str) -> str:
    safe_name = html.escape(str(bot_name or "机器人"))
    configured = str(_load_start_welcome_config().get("text") or "").strip()
    if configured:
        # Custom content is plain text to avoid broken HTML from arbitrary input.
        return html.escape(configured).replace("{bot_name}", safe_name)

    if str(bot_name or "").strip() == MASTER_BOT_NAME:
        return f"👏 欢迎使用 {safe_name}\n 能帮你便捷安全地管理频道和群组，是TG上领先的管理的机器人之一\n➡️请赋予我频道/群组管理员权限！"

    if MASTER_BOT_USERNAME:
        master_label = (
            f'<a href="https://t.me/{html.escape(MASTER_BOT_USERNAME, quote=True)}">{html.escape(MASTER_BOT_NAME)}</a>'
        )
    else:
        master_label = html.escape(MASTER_BOT_NAME)
    return f"👏欢迎使用 {safe_name} 克隆自 {master_label}\n 能帮你便捷安全地管理频道和群组，是TG上领先的管理的机器人之一\n➡️请赋予我频道/群组管理员权限！"


def _clear_submission_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel an unfinished user submission when returning to the start menu."""
    for key in (
        "waiting_post",
        "publish_keyword_search",
        "publish_keyword_results",
        "publish_comment_target",
        "publish_pending_proof_id",
    ):
        context.user_data.pop(key, None)


async def start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """兜底 /start：保证未启用 verification 的机器人也能响应。"""
    if not update.message:
        return

    if context.args:
        
        # 开始验证
        start_param = context.args[0]
        if start_param.startswith("invite_"):
            invite_code = start_param[len("invite_"):]
            print("进入邀请功能:", invite_code)
            if await handle_join_start(update, context):
                return

        if await handle_comment_start_parameter(update, context, context.args[0]):
            return
        if await handle_report_start_parameter(update, context, context.args[0]):
            return

    # A normal /start is also an explicit exit from an unfinished submission.
    _clear_submission_draft(context)

    bot_name = context.application.bot_data.get("name", "机器人")
    features = sorted(context.application.bot_data.get("enabled_features", []))
    feature_text = ", ".join(features[:20]) if features else "默认功能"
    context.user_data["start_panel"] = {
        "bot_name": bot_name,
        "feature_text": feature_text,
    }
    user_id = update.effective_user.id if update.effective_user else None
    keyboard_rows = _build_start_panel_rows(context, user_id, update.effective_user)
    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None


    text = _build_start_welcome_text(bot_name)
    # text = "👏"
    await update.message.reply_text(
        text,
        # f"当前启用功能：{feature_text}\n\n",
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    if not query.data.startswith("start:"):
        return
    await query.answer()

    panel = context.user_data.get("start_panel", {})
    bot_name = panel.get("bot_name", context.application.bot_data.get("name", "机器人"))
    action = query.data.split(":", 1)[1]
    if action != "back":
        return
    # 返回首页时必须同时清理已选择的关键词/评论目标；否则下一次点击
    # 「我要投稿」会误用上次的目标，跳过关键词输入。
    _clear_submission_draft(context)
    user_id = update.effective_user.id if update.effective_user else None
    keyboard_rows = _build_start_panel_rows(context, user_id, update.effective_user)
    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    return await query.edit_message_text(
        _build_start_welcome_text(bot_name),
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _build_help_text(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int] = None) -> str:
    enabled = context.application.bot_data.get("enabled_features") or set(ALL_FEATURES)
    bot_name = str(context.application.bot_data.get("name", "机器人")).strip() or "机器人"
    is_master = bot_name == MASTER_BOT_NAME
    can_manage_private_forward = bool(
        user_id and has_admin_permission(context, int(user_id), "private_forward")
    )
    can_manage_group_config = bool(
        user_id and has_admin_permission(context, int(user_id), "group_config")
    )
    can_manage_channel_config = bool(
        user_id and any(
            has_admin_permission(context, int(user_id), permission)
            for permission in ("channel_config", "telethon_manage", "bot_channel_config")
        )
    )
    can_manage_group_config = can_manage_group_config or bool(
        user_id and has_admin_permission(context, int(user_id), "global_ad_config")
    )
    lines = [
        f"📖 {bot_name} 命令帮助",
        "",
        "基础命令：",
        "/start 查看欢迎面板",
        "/help 查看命令帮助",
        "/features 查看当前机器人启用功能",
    ]

    if "group" in enabled and can_manage_group_config:
        lines.extend(
            [
                "",
                "群配置：",
                "/group 打开群配置列表",
                "群状态 查看当前群配置",
                "群配置 / 群设置 打开群设置面板",
                "群静默 / 群验证 / 群欢迎 / 群广告 调整对应开关",
                "群限频 / 群限频条数 调整限频参数",
                "群庄园 / 群好友 / 群成语 调整群玩法开关",
                "群广告推送 查看或设置广告推送",
            ]
        )

    if "channel" in enabled and can_manage_channel_config:
        lines.extend(
            [
                "",
                "频道功能：",
                "/channel_config 打开频道配置",
                "频道配置 查看频道设置",
                "机器人频道配置 设置机器人频道转发",
                "登录小号 / 查看登录 管理协议号",
                "订阅会员 / 订阅列表 / 添加订阅 管理订阅",
            ]
        )

    if "private_forward" in enabled and (not user_id or can_manage_private_forward):
        lines.extend(
            [
                "",
                "双向机器人：",
                "用户直接私聊机器人，消息会转给主人，主人回复消息即可回给用户",
            ]
        )
        if can_manage_private_forward:
            lines.extend(
                [
                    "双向模式 / 私聊面板 打开当前私聊会话面板",
                    "用户列表 查看已私聊过机器人的用户",
                    "拉黑用户 回复用户消息或者 拉黑用户 用户ID 将用户拉黑 ",
                    "移除拉黑 回复用户消息或者 移除拉黑 用户ID 将用户解除拉黑 ",
                    "黑名单 查看已拉黑用户",
                    "广播 回复一条消息后群发给机器人所在的全部群",
                    "用户广播 回复一条消息后群发给全部私聊过的用户",
                    "导出用户 导出私聊用户列表",
                ]
            )

    if "game_hub" in enabled:
        lines.extend(
            [
                "",
                "玩法帮助：",
                "/start_menu 打开玩法帮助菜单",
            ]
        )

    if is_master:
        lines.extend(
            [
                "",
                "主机器人专属：",
                "克隆机器人 按模板克隆新机器人",
                "机器人面板 查看名下机器人列表",
                "添加扫描库 回复用户名将用户名添加到文件里",
                "扫描 扫描文件里可用的用户名",
                "用户 拼音 aabbc sub 检测可用的用户名，网站上未占用检测",
                "检测  aabbc  也可以回复用户名批量检测 检测可用的用户名，协议号检测，可用即可用",
                "设置用户名 aabbc  协议号在目标群有修改权限，机器人在，超级管理员的命令",
                "创建群组/频道 群中文名 用户名  协议号创建群租上限10个",
                "广播模式 -xxx 关闭广播模式",
                "收藏 收藏夹 查看收藏xx",
                "说话触发虚拟奖励",
                "病毒开始/病毒状态/病毒结束",
            ]
        )
        if user_id and is_super_admin(user_id):
            lines.append("/restart 重启机器人")

    return "\n".join(lines)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    text = _build_help_text(context, user_id)
    if update.message:
        return await update.message.reply_text(text)
    return await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

async def features_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = build_feature_intro(context)
    if update.message:
        return await update.message.reply_text(text)
    return await context.bot.send_message(chat_id=update.effective_chat.id, text=text)


def _build_start_panel_rows(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: Optional[int] = None,
    user=None,
) -> list[list[InlineKeyboardButton]]:
    enabled = context.application.bot_data.get("enabled_features") or set(ALL_FEATURES)
    bot_name = str(context.application.bot_data.get("name", "")).strip()
    owner_id = int(context.application.bot_data.get("owner_id", DEFAULT_OWNER_ID))
    publish_config = load_publish_config()
    custom_menu_buttons = publish_config.get("custom_menu_buttons", {})
    if not isinstance(custom_menu_buttons, dict):
        custom_menu_buttons = {}
    is_bot_admin_viewer = bool(user_id and is_owner_or_super_admin(context, int(user_id)))
    can_config_welcome = _can_configure_start_welcome(context, user)

    def can_use(permission: str) -> bool:
        return bool(user_id and has_admin_permission(context, int(user_id), permission))

    def show_custom_button(key: str) -> bool:
        # Visibility settings apply only to ordinary users. The bot owner and
        # super administrators always retain every management entry.
        return is_bot_admin_viewer or bool(custom_menu_buttons.get(key, True))

    rows: list[list[InlineKeyboardButton]] = []
    if bot_name == MASTER_BOT_NAME:
        rows.append(
            [
                InlineKeyboardButton("🧬克隆机器人", callback_data=f"mbot:clone:{MASTER_BOT_NAME}"),
                InlineKeyboardButton("🤖机器人面板", callback_data="mbot:list")
            ]
        )

    if is_bot_admin_viewer or any(
        can_use(permission) for permission in ("submission_config", "private_forward")
    ):
        owner_row = []
        # 私聊面板依赖 private_forward 的消息和回调处理器；未开启时不显示，
        # 避免克隆机器人出现“按钮可点但没有反应”的假入口。
        if "private_forward" in enabled and can_use("private_forward"):
            owner_row.append(InlineKeyboardButton("💬私聊面板", callback_data="pfmode:open:1"))
        if can_use("submission_config"):
            owner_row.append(InlineKeyboardButton("⚙️投稿配置", callback_data="publish:publishset"))
        # 自定义用户入口属于投稿配置权限；多管理员本身仅所有者/超级管理员可管理。
        if can_use("submission_config"):
            owner_row.append(InlineKeyboardButton("🧩自定义按钮", callback_data="publish:custom_buttons"))
            if bool(publish_config.get("template_publish_enabled", False)):
                owner_row.append(InlineKeyboardButton("🧩模板发布", callback_data="publish:template_publish"))
        if is_bot_admin_viewer:
            owner_row.append(InlineKeyboardButton("⌨️命令模板", callback_data="ccmd:menu"))
            owner_row.append(InlineKeyboardButton("👥多管理员", callback_data="adm:panel"))
        if can_config_welcome:
            owner_row.append(InlineKeyboardButton("✏️欢迎词", callback_data="welcome:edit"))
        if owner_row:
            # rows.append(owner_row)
            # 每两个按钮一行
            for i in range(0, len(owner_row), 2):
                rows.append(owner_row[i:i + 2])

    if "channel" in enabled:
        channel_row = []
        if  show_custom_button("channel_clone"):
            channel_row.append(InlineKeyboardButton("📣克隆频道", callback_data="chcfg:back"))
        if  show_custom_button("bot_channel_config"):
            channel_row.append(InlineKeyboardButton("📣机器人频道配置", callback_data="chcfg:bot"))
        if  show_custom_button("telethon_manage"):
            channel_row.append(InlineKeyboardButton("📱管理协议号(可群发)", callback_data="tlogin:list"))
       
        if channel_row:
            for i in range(0, len(channel_row), 2):
                rows.append(channel_row[i:i + 2])
            # rows.append(channel_row)

    if "group" in enabled:
        group_row = []
        if show_custom_button("group_config"):
            group_row.append(InlineKeyboardButton("👥群配置", callback_data="gcfg:list"))
        if can_use("global_ad_config") and show_custom_button("global_ad_config"):
            group_row.append(InlineKeyboardButton("📢全群广告推送", callback_data="gcfg:global_ad_menu"))
        if group_row:
            rows.append(group_row)

    # 投稿配置控制公共入口的显示；旧配置会在 load_publish_config 中自动
    # 补齐开关字段，并默认保持此前两个按钮都显示的行为。
    resource_row = []
    if is_bot_admin_viewer or bool(publish_config.get("submission_enabled", True)):
        resource_row.append(
            InlineKeyboardButton("✍️我要投稿", callback_data="publish:publish")
        )
    if is_bot_admin_viewer or bool(publish_config.get("random_view_enabled", True)):
        resource_row.append(
            InlineKeyboardButton("随机查看", callback_data="publish:channel_message")
        )
    if resource_row:
        rows.append(resource_row)
    # Post lookup is deliberately explicit. It must never consume ordinary
    # private text such as a protocol-login phone number.
    rows.append([InlineKeyboardButton("🔎 查找收录标签", callback_data="publish:keyword_post_search")])

    return rows

def _build_start_welcome_text(bot_name: str) -> str:
    return _welcome_template_text(bot_name)


async def start_welcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("welcome:"):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return await query.answer("请在私聊中配置欢迎词。", show_alert=True)
    if not _can_configure_start_welcome(context, update.effective_user):
        return await query.answer("仅机器人所有者且订阅有效时可以配置欢迎词。", show_alert=True)
    action = query.data.split(":", 1)[1]
    if action == "cancel":
        context.user_data.pop(START_WELCOME_EDIT_KEY, None)
        await query.answer("已取消当前欢迎词输入。")
        bot_name = context.application.bot_data.get("name", "机器人")
        user_id = update.effective_user.id if update.effective_user else None
        keyboard_rows = _build_start_panel_rows(context, user_id, update.effective_user)
        keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
        return await query.edit_message_text(
            _build_start_welcome_text(bot_name),
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    if action == "reset":
        save_json(START_WELCOME_FILE, {})
        context.user_data.pop(START_WELCOME_EDIT_KEY, None)
        await query.answer("已恢复默认欢迎词。")
        bot_name = context.application.bot_data.get("name", "机器人")
        return await query.edit_message_text(
            _build_start_welcome_text(bot_name),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回首页", callback_data="start:back")]]),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    context.user_data[START_WELCOME_EDIT_KEY] = True
    current = str(_load_start_welcome_config().get("text") or "").strip() or "（当前使用默认欢迎词）"
    await query.answer()
    return await query.edit_message_text(
        "✏️ <b>配置欢迎词</b>\n\n"
        "请直接发送新的欢迎词。支持使用 <code>{bot_name}</code> 自动代入机器人名称。\n"
        "发送“取消”放弃本次修改。\n\n"
        f"当前自定义内容：\n<code>{html.escape(current)}</code>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ 恢复默认", callback_data="welcome:reset")],
            [InlineKeyboardButton("⬅️ 返回并取消输入", callback_data="welcome:cancel")],
        ]),
        parse_mode="HTML",
    )


async def start_welcome_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get(START_WELCOME_EDIT_KEY):
        return
    if not _can_configure_start_welcome(context, update.effective_user):
        context.user_data.pop(START_WELCOME_EDIT_KEY, None)
        return
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(START_WELCOME_EDIT_KEY, None)
        await update.message.reply_text("已取消欢迎词修改。")
        raise ApplicationHandlerStop
    if not text:
        await update.message.reply_text("欢迎词不能为空，请重新发送。")
        raise ApplicationHandlerStop
    if len(text) > 3500:
        await update.message.reply_text("欢迎词不能超过 3500 个字符，请重新发送。")
        raise ApplicationHandlerStop
    save_json(START_WELCOME_FILE, {"text": text})
    context.user_data.pop(START_WELCOME_EDIT_KEY, None)
    bot_name = context.application.bot_data.get("name", "机器人")
    await update.message.reply_text(
        "✅ 欢迎词已保存，预览如下：\n\n" + _build_start_welcome_text(bot_name),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    raise ApplicationHandlerStop


async def clear_login_prompt_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not update.effective_user:
        return
    data = query.data or ""
    if data.startswith("tlogin:login"):
        return
    await _clear_login_state(str(update.effective_user.id), context)


async def daily_master_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    await daily_master_job(context)


async def hour_master_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    await hour_master_job(context)


async def ten_minute_master_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    await ten_minute_master_job(context)


async def five_minute_master_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    await five_minute_master_job(context.bot)


def create_app(bot_cfg: dict):
    token = bot_cfg["token"]
    owner_id = bot_cfg["owner_id"]
    bot_name = bot_cfg["name"]
    write_startup_debug(
        f"[create_app] bot={bot_name} owner_id={owner_id} "
        f"features={','.join(bot_cfg.get('enabled_features', []))}"
    )

    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        connection_pool_size=100,
        pool_timeout=20.0,
    )

    app = ApplicationBuilder().token(token).request(request).build()

    app.bot_data["owner_id"] = owner_id  # ✅ 绑定到当前机器人
    app.bot_data["token"] = token
    app.bot_data["name"] = bot_name
    app.bot_data["timezone"] = bot_cfg.get("timezone", "Asia/Shanghai")
    app.bot_data["enabled_features"] = set(bot_cfg.get("enabled_features", []))
    # Custom command edits can refresh Telegram's slash-command menu immediately.
    app.bot_data["refresh_bot_commands"] = set_bot_commands
    set_bot_owner(bot_name, owner_id)
    set_bot_timezone(bot_name, app.bot_data["timezone"])

    app.add_handler(BusinessConnectionHandler(handle_business_connection))
    app.add_handler(TypeHandler(Update, runtime_context_handler), group=-1000)
    app.add_handler(CallbackQueryHandler(start_welcome_callback, pattern=r"^welcome:"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND),
            start_welcome_input,
        ),
        group=-30,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS, block_disabled_group_messages),
        group=-950,
    )

    # ===== 基础命令 =====
    if bot_name == MASTER_BOT_NAME:
        app.add_handler(CommandHandler("restart", restart_bot))
        app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(r"^/restart(?:@\w+)?(?:\s|$)"),
                restart_bot_fallback,
            ),
            group=1,
        )
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("features", features_command))
    app.add_handler(CommandHandler("intro", features_command))
    app.add_handler(CommandHandler("leave", leave_group_command))

    app.add_handler(CommandHandler("show", show_menu))
    app.add_handler(CommandHandler("hide", hide_menu))
    # Custom command templates use their own early handlers so arbitrary
    # owner-configured /commands can be added without editing this file.
    register_custom_command_handlers(app)

    # ===== 私聊转发逻辑 =====
    if is_feature_enabled(app, "private_forward"):
        write_startup_debug(f"[create_app] register private_forward handlers bot={bot_name}")
        app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.REPLY & ~filters.COMMAND,
                owner_reply_router,
            ),
        )

        app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.COMMAND,
                private_forward_router,
            ),
        )

        app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.REPLY & ~filters.COMMAND,
                owner_auto_forward_in_dialog,
            ),
            group=1,
        )
    # 无论是否启用私聊转发，都注册 callback：旧 /start 菜单中的按钮、或配置
    # 刚关闭时，用户至少会得到明确提示，而不是 Telegram 客户端一直转圈。
    app.add_handler(
        CallbackQueryHandler(handle_private_dialog_callback, pattern=r"^pfmode:")
    )
    app.add_handler(CallbackQueryHandler(clear_login_prompt_on_callback), group=-900)
    app.add_handler(CallbackQueryHandler(start_panel_callback, pattern=r"^start:"))

    # 内连
    app.add_handler(InlineQueryHandler(inline_query_handler))
    # 访客机器人必须新的才可以，别的地方启动过就不可以了 guest_bot_handler 这个消息就见听不到
    # app.add_handler(TypeHandler(Update, guest_bot_handler)  , group=-1200)
    # app.add_handler(MessageHandler(filters.PHOTO, get_file_id))

    # ===== 注册所有功能模块 =====
    register_all_handlers(app)
    # 兜底 /start（放在更后 group，避免覆盖 verification 的 /start 校验逻辑）
    app.add_handler(CommandHandler("start", start_fallback), group=50)

    # ===== 统一文本路由（必须最后）=====
    app.add_handler(
        MessageHandler(filters.ALL, message_router),
        group=999,
    )

    # ===== 定时任务 =====
    app.job_queue.run_daily(
        daily_master_job_wrapper,
        time=time(hour=0, minute=0),
        name="daily_stamina",
    )

    app.job_queue.run_repeating(hour_master_job_wrapper, interval=7200, first=0)
    app.job_queue.run_repeating(
        ten_minute_master_job_wrapper,
        interval=60,
        first=60,
    )

    app.job_queue.run_repeating(
        five_minute_master_job_wrapper,
        interval=300,
    )
    if is_feature_enabled(app, "channel"):
        # Delay a bit to avoid startup misfire on some PTB versions.
        app.job_queue.run_once(start_telethon_forwarder_job, when=1)
        # 兜底重试：防止任务错过导致协议号未启动
        app.job_queue.run_repeating(start_telethon_forwarder_job, interval=30, first=30)

    app.add_error_handler(error_handler)

    return app


# ===== 超级管理员命令 =====
SUPER_ADMIN_COMMANDS = {
    "restart": "重启机器人（仅超级管理员）",
    # "leave": "让机器人离开当前群（仅超级管理员）",
}

async def set_bot_commands(app):
    """
    设置机器人命令列表，Telegram 输入 / 时显示
    """
    commands = []
    enabled = app.bot_data.get("enabled_features") or set(ALL_FEATURES)
    bot_name = str(app.bot_data.get("name", "")).strip()

    # 超级管理员命令：仅主机器人显示重启
    if bot_name == MASTER_BOT_NAME:
        for cmd, desc in SUPER_ADMIN_COMMANDS.items():
            commands.append(BotCommand(cmd, desc))

    # 普通用户命令：只显示当前机器人确实启用的功能
    commands.append(BotCommand("start", "功能简介"))

    # commands.append(BotCommand("help", "命令帮助"))
    existing_commands = {command.command for command in commands}
    for command, description in visible_bot_commands():
        if command not in existing_commands:
            commands.append(BotCommand(command, description))
            existing_commands.add(command)

    # if "group" in enabled:
    #     commands.append(BotCommand("group", "群设置"))

    # if "channel" in enabled:
    #     commands.append(BotCommand("channel_config", "频道设置"))
    # if "game_hub" in enabled:
    #     commands.append(BotCommand("start_menu", "游戏菜单"))
    await app.bot.set_my_commands(commands)


# ===== 重启命令（超级管理员使用） =====
async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or int(update.effective_user.id) != 6085551760:
        return
    if not update.message:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="♻️ 正在重启机器人...",
        reply_to_message_id=update.message.message_id,
    )
    print("♻️ 正在重启机器人...", flush=True)
    logging.warning("♻️ 正在重启机器人...")

    # 重启前清理广告词
    await cleaned_word()
    await asyncio.sleep(0.8)
    # # 1️⃣ 先结束所有正在进行的成语接龙
    # for chat_id in group_list.keys():
    #     fake_update = Update(
    #         update_id=chat_id, message=update.message  # 使用当前消息上下文
    #     )
    #     # 调用 end_chengyu，传入 fake_update 和 context
    #     await end_chengyu(fake_update, context)

    python = sys.executable
    os.execv(python, [python] + sys.argv)


async def restart_bot_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if not re.match(r"^/restart(?:@\w+)?(?:\s|$)", text, re.I):
        return
    await restart_bot(update, context)
    raise ApplicationHandlerStop


async def leave_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return

    chat = update.effective_chat
    chat_id = None

    # 1️⃣ 如果是在群里执行
    if chat.type in ["group", "supergroup"]:
        chat_id = chat.id
        chat_title = chat.title or ""

    # 2️⃣ 如果是在私聊执行，需要传群ID
    elif chat.type == "private":
        if not context.args:
            await update.message.reply_text("请提供群ID，例如：/leave -1001234567890")
            return

        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("群ID格式错误。")
            return

        chat_title = str(chat_id)

    else:
        await update.message.reply_text("无法识别的聊天类型。")
        return

    # 读取群配置
    groups = load_json(GROUPS_FILE)
    if not isinstance(groups, dict):
        groups = {}

    cfg = groups.get(str(chat_id), {})
    if not isinstance(cfg, dict):
        cfg = {}

    cfg["title"] = chat_title or cfg.get("title", "")
    cfg["type"] = "group"
    cfg["bot_in_group"] = False
    groups[str(chat_id)] = cfg

    save_json(GROUPS_FILE, groups)

    print(f"👋 已标记离群: {chat_title} ({chat_id})")

    # 私聊时回复给管理员
    if chat.type == "private":
        await update.message.reply_text(f"👋 已尝试退出群: {chat_id}")
    else:
        await update.message.reply_text("👋 再见，我要离开这个群了！")

    try:
        await context.bot.leave_chat(chat_id)
    except Exception as e:
        await update.message.reply_text(f"退出群失败: {e}")


async def post_init_setup(app):
    set_runtime_bot_name(app.bot_data.get("name", ""))
    set_bot_timezone(
        app.bot_data.get("name", ""),
        app.bot_data.get("timezone", "Asia/Shanghai"),
    )
    write_startup_debug(f"[post_init_setup] bot={app.bot_data.get('name')} post-init start")
    ensure_info_migrated()
    await set_bot_commands(app)  # 直接 await，事件循环已运行
    write_startup_debug(f"[post_init_setup] bot={app.bot_data.get('name')} post-init done")


configure_runtime_hooks(create_app, post_init_setup)


async def handle_join_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return False

    text = update.message.text or ""
    start_param = context.args[0]
    if not text.startswith("/start"):
        return False

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return False

    # 短邀请码，例如：
    # /start aK72xP
    # invite_code = parts[1].strip()
    
    invite_code = start_param[len("invite_"):].strip()

    if not invite_code:
        return False

    # 查找邀请码对应的群邀请链接
    link_map_data = load_invite_link_map()

    invite_link = None
    inviter_id = None
    inviter_name = "好友"
    chat_id = None

    for chat_key, group_link_map in link_map_data.items():
        for link, info in group_link_map.items():
            if info.get("invite_code") != invite_code:
                continue

            invite_link = link
            inviter_id = int(info.get("inviter_id", 0))
            inviter_name = info.get("inviter_name") or "好友"
            
            inviter_username = info.get("inviter_username")

            if inviter_username:
                username = inviter_username.lstrip("@")

                inviter_display = (
                    f'<a href="https://t.me/{html.escape(username)}">'
                    f'{html.escape(inviter_name)}'
                    f'</a>'
                )
            elif inviter_id:
                inviter_display = (
                    f'<a href="tg://user?id={inviter_id}">'
                    f'{html.escape(inviter_name)}'
                    f'</a>'
                )
            else:
                inviter_display = html.escape(inviter_name)

            try:
                chat_id = int(chat_key)
            except (ValueError, TypeError):
                chat_id = None

            break

        if invite_link:
            break

    if not invite_link:
        await update.message.reply_text(
            "❌ 这个邀请链接已经失效。"
        )
        return True

    # 获取群名称
    chat_title = "群聊"

    if chat_id:
        try:
            chat = await context.bot.get_chat(chat_id)
            chat_title = chat.title or "群聊"
        except Exception:
            pass

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 加入群聊",
                url=invite_link,
            )
        ]
    ]

    await update.message.reply_text(
        f"👋 欢迎你！\n\n"
        f"📢 群聊：{html.escape(chat_title)}\n"
        f"👤 邀请人：{inviter_display}\n\n"
        f"点击下面按钮加入群聊 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    return True

async def main():
    write_startup_debug(f"[main] process start cwd={os.getcwd()} argv={' '.join(sys.argv)}")
    bot_configs = load_startup_bot_configs()
    if not bot_configs:
        raise RuntimeError("没有可用的机器人配置，请检查 BOT_TOKEN/BOT_ENABLE 环境变量")

    apps = [create_app(cfg) for cfg in bot_configs]

    print(f"🤖 已加载 {len(apps)} 个机器人配置")

    try:
        for app in apps:
            try:
                write_startup_debug(f"[main] initializing bot={app.bot_data.get('name')}")
                await app.initialize()
                # 主动验证 token，避免进入 polling 后才刷 InvalidToken 错误
                await app.bot.get_me()
                await post_init_setup(app)
                await app.start()
                # Telegram 默认不会推送 chat_member 更新；显式订阅全部类型，确保
                # my_chat_member（机器人被踢出/重新加入群）能更新 groups.json。
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                register_running_app(app)
                write_startup_debug(
                    f"[main] started bot={app.bot_data.get('name')} username=@{app.bot.username}"
                )
                print(
                    f"✅ 已启动: {app.bot_data.get('name')} (owner={app.bot_data.get('owner_id')}, username=@{app.bot.username})"
                )
            except InvalidToken:
                write_startup_debug(
                    f"[main] invalid token bot={app.bot_data.get('name')}"
                )
                logging.error(
                    "❌ 机器人 token 无效，已跳过: %s",
                    app.bot_data.get("name"),
                )
                try:
                    await app.shutdown()
                except Exception:
                    pass
                unregister_running_app(app.bot_data.get("name"))
                continue
            # print(
            #     f"   ↳ 启用功能: {', '.join(sorted(app.bot_data.get('enabled_features', [])))}"
            # )

        await asyncio.Event().wait()
    except KeyboardInterrupt:
        write_startup_debug("[main] keyboard interrupt")
        print("🛑 收到 Ctrl+C，正在安全关闭机器人...")
    finally:
        for app in reversed(apps):
            try:
                # Stop the long-lived Telethon loop and await client disconnects
                # before PTB shuts down the event loop.
                await stop_telethon_forwarder(app)
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
                unregister_running_app(app.bot_data.get("name"))
                write_startup_debug(f"[main] stopped bot={app.bot_data.get('name')}")
            except Exception as e:
                write_startup_debug(
                    f"[main] stop failed bot={app.bot_data.get('name')} error={e}"
                )
                logging.exception(
                    "停止机器人失败 [%s]: %s", app.bot_data.get("name"), e
                )


if __name__ == "__main__":
    asyncio.run(main())
