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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ApplicationHandlerStop,
    TypeHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut, InvalidToken

from group.group_logger import GROUPS_FILE
from forward.message_forward import (
    _debug_private_forward,
    forward_to_owner,
    handle_private_dialog_callback,
    owner_auto_forward_in_dialog,
    register_send_user_conv,
    reply_from_owner,
)
from menu import build_feature_intro
from modules import register_all_handlers  # 注册各功能模块
from dispatcher import message_router  # 最终文本处理路由器
from channel.telethon_forwarder import start_telethon_forwarder_job
from channel.telethon_login import _clear_login_state
from command_router import get_matched_command

from chat.my_bot import cleaned_word
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
    is_super_admin,
    load_json,
    save_json,
    is_bot_owner,
    set_bot_owner,
    set_runtime_bot_name,
)

# ===== 注册 Telegram / 命令 =====
from telegram import BotCommand

load_dotenv(override=True)


async def error_handler(update, context):
    err = getattr(context, "error", None)
    if isinstance(err, NetworkError):
        msg = str(err).lower()
        if "message to delete not found" in msg:
            return
        if "not enough rights to send text messages" in msg:
            logging.warning("机器人在目标群没有发言权限: %s", err)
            return
        logging.warning("网络错误，可能是Telegram服务器临时不可用: %s", err)
        return
    if isinstance(err, TimedOut):
        logging.warning("请求超时，重试中: %s", err)
        return
    logging.exception("未处理异常", exc_info=err)


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


async def runtime_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)


async def owner_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    owner_id = int(context.application.bot_data.get("owner_id", DEFAULT_OWNER_ID))
    if not update.effective_user or update.effective_user.id != owner_id:
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
    
    _debug_private_forward(
        f"[private_forward_router] bot={context.application.bot_data.get('name')} "
        f"chat_type={getattr(chat, 'type', None)} "
        f"user_id={getattr(user, 'id', None)} "
        f"text={getattr(update.message, 'text', None)!r}"
    )
    print(
        f"[private_forward_router] chat_type={getattr(chat, 'type', None)} "
        f"user_id={getattr(user, 'id', None)} "
        f"text={getattr(update.message, 'text', None)!r}"
    )
    if (
        str(context.application.bot_data.get("name", "")).strip() == MASTER_BOT_NAME
        and (
            isinstance(context.user_data.get(PRIVATE_FORWARD_SELF_SERVICE_STAGE_KEY), dict)
            or isinstance(context.user_data.get(MULTI_BOT_STAGE_KEY), dict)
        )
    ):
        _debug_private_forward("[private_forward_router] skip self-service stage")
        print("[private_forward_router] 忽略：主机器人当前处于自助/多机器人输入阶段")
        return
    msg = update.message
    if msg:
        sender_chat = getattr(msg, "sender_chat", None)
        forward_origin = getattr(msg, "forward_origin", None)
        origin_chat = getattr(forward_origin, "chat", None) if forward_origin else None

        channel = None
        if (
            sender_chat
            and getattr(sender_chat, "type", None)
            and sender_chat.type.name == "CHANNEL"
        ):
            channel = sender_chat
        elif (
            origin_chat
            and getattr(origin_chat, "type", None)
            and origin_chat.type.name == "CHANNEL"
        ):
            channel = origin_chat

        if channel:
            username = getattr(channel, "username", "") or ""
            title = getattr(channel, "title", "") or ""
            channel_id = f"<code>{channel.id}</code>"
            origin_msg_id = (
                getattr(forward_origin, "message_id", None) if forward_origin else None
            )
            msg_id_val = origin_msg_id if origin_msg_id is not None else msg.message_id
            msg_id = f"<code>{msg_id_val}</code>"
            if username:
                text = (
                    f"✅ 频道ID：{channel_id} 点击红色数字拷贝\n"
                    f"消息ID：{msg_id}\n"
                    f"频道用户名：@{username}\n"
                    f"频道名：{title}"
                )
            else:
                text = f"✅ 频道ID：{channel_id}\n消息ID：{msg_id}\n频道名：{title}"
            await msg.reply_text(text, parse_mode="HTML")
            return
    await forward_to_owner(update, context)


async def start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """兜底 /start：保证未启用 verification 的机器人也能响应。"""
    if not update.message:
        return

    bot_name = context.application.bot_data.get("name", "机器人")
    features = sorted(context.application.bot_data.get("enabled_features", []))
    feature_text = ", ".join(features[:20]) if features else "默认功能"
    context.user_data["start_panel"] = {
        "bot_name": bot_name,
        "feature_text": feature_text,
    }
    keyboard_rows = _build_start_panel_rows(context)
    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    await update.message.reply_text(
        _build_start_welcome_text(bot_name),
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
    keyboard_rows = _build_start_panel_rows(context)
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
        user_id and (is_super_admin(user_id) or is_bot_owner(user_id))
    )
    lines = [
        f"📖 {bot_name} 命令帮助",
        "",
        "基础命令：",
        "/start 查看欢迎面板",
        "/help 查看命令帮助",
        "/features 查看当前机器人启用功能",
    ]

    if "group" in enabled:
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

    if "channel" in enabled:
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

    if "private_forward" in enabled:
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
                "小雅专属：",
                "克隆机器人 按模板克隆新机器人",
                "机器人面板 查看名下机器人列表",
            ]
        )
        if user_id and is_super_admin(user_id):
            lines.append("/restart 重启小雅")

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


def _build_start_panel_rows(context: ContextTypes.DEFAULT_TYPE) -> list[list[InlineKeyboardButton]]:
    enabled = context.application.bot_data.get("enabled_features") or set(ALL_FEATURES)
    bot_name = str(context.application.bot_data.get("name", "")).strip()
    rows: list[list[InlineKeyboardButton]] = []
    if bot_name == MASTER_BOT_NAME:
        rows.append([InlineKeyboardButton("🧬克隆机器人", callback_data=f"mbot:clone:{MASTER_BOT_NAME}")])
        rows.append([InlineKeyboardButton("🤖机器人面板", callback_data="mbot:list")])
    if "group" in enabled:
        rows.append([InlineKeyboardButton("👥群配置", callback_data="gcfg:list")])
    if "channel" in enabled:
        rows.extend(
            [
                [InlineKeyboardButton("📣频道配置", callback_data="chcfg:back")],
                [InlineKeyboardButton("📣机器人频道配置", callback_data="chcfg:bot")],
                [InlineKeyboardButton("📱查看登录", callback_data="tlogin:list")],
                [InlineKeyboardButton("📱登录小号", callback_data="tlogin:login")],
            ]
        )
    return rows


def _build_start_welcome_text(bot_name: str) -> str:
    safe_name = html.escape(str(bot_name or "机器人"))
    if str(bot_name or "").strip() == MASTER_BOT_NAME:
        return f"👋 欢迎使用 {safe_name}\n"

    if MASTER_BOT_USERNAME:
        master_label = (
            f'<a href="https://t.me/{html.escape(MASTER_BOT_USERNAME, quote=True)}">{html.escape(MASTER_BOT_NAME)}</a>'
        )
    else:
        master_label = html.escape(MASTER_BOT_NAME)
    return f"👋 欢迎使用 {safe_name} 克隆自 {master_label}\n"


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
    app.bot_data["enabled_features"] = set(bot_cfg.get("enabled_features", []))
    set_bot_owner(bot_name, owner_id)
    app.add_handler(TypeHandler(Update, runtime_context_handler), group=-1000)

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

    # ===== 私聊转发逻辑 =====
    if is_feature_enabled(app, "private_forward"):
        write_startup_debug(f"[create_app] register private_forward handlers bot={bot_name}")
        app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.COMMAND,
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
        app.add_handler(
            CallbackQueryHandler(
                handle_private_dialog_callback, pattern=r"^pfmode:"
            )
        )
    app.add_handler(CallbackQueryHandler(clear_login_prompt_on_callback), group=-900)
    app.add_handler(CallbackQueryHandler(start_panel_callback, pattern=r"^start:"))

    # ===== 注册所有功能模块 =====
    register_all_handlers(app)
    register_send_user_conv(app)
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
    commands.append(BotCommand("help", "命令帮助"))
    if "group" in enabled:
        commands.append(BotCommand("group", "群设置"))
    if "channel" in enabled:
        commands.append(BotCommand("channel_config", "频道设置"))
    if "game_hub" in enabled:
        commands.append(BotCommand("start_menu", "游戏菜单"))
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
    write_startup_debug(f"[post_init_setup] bot={app.bot_data.get('name')} post-init start")
    await set_bot_commands(app)  # 直接 await，事件循环已运行
    write_startup_debug(f"[post_init_setup] bot={app.bot_data.get('name')} post-init done")


configure_runtime_hooks(create_app, post_init_setup)


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
                await app.updater.start_polling()
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
