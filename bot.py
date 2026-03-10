# === bot.py 主文件 ===
import asyncio
import os
import sys
import logging
from datetime import time
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    TypeHandler,
    filters,
    ContextTypes,
)

from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut, InvalidToken

from group.group_logger import GROUPS_FILE
from forward.message_forward import forward_to_owner, reply_from_owner
from modules import register_all_handlers  # 注册各功能模块
from dispatcher import message_router  # 最终文本处理路由器

from chat.my_bot import cleaned_word
from run_daily import (
    daily_master_job,
    five_minute_master_job,
    hour_master_job,
    ten_minute_master_job,
)
from feature_flags import ALL_FEATURES, parse_feature_list, sanitize_features
from utils import (
    is_super_admin,
    load_json,
    save_json,
    set_bot_owner,
    set_runtime_bot_name,
)

# ===== 注册 Telegram / 命令 =====
from telegram import BotCommand

load_dotenv(override=True)


async def error_handler(update, context):
    err = getattr(context, "error", None)
    if isinstance(err, NetworkError):
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

# ===== 多机器人配置 =====
# 自动读取环境变量：
# - BOT_TOKEN_<KEY>  (必填)
# - BOT_NAME_<KEY>   (可选，不填默认 bot_<key>)
# - BOT_OWNER_<KEY>  (可选，不填默认 DEFAULT_OWNER_ID)
# - BOT_ENABLE_<KEY> (可选，默认开启)
# - BOT_FEATURES_<KEY> (可选，显式启用功能列表，逗号分隔)
# - BOT_DISABLE_FEATURES_<KEY> (可选，在当前启用列表上关闭功能，逗号分隔)
#
# 例如：
# BOT_TOKEN_A=xxxx
# BOT_NAME_A=bot_haha
# BOT_OWNER_A=6085551760
# BOT_ENABLE_A=1
# BOT_DISABLE_FEATURES_A=save_photos,group_media_tools
DEFAULT_OWNER_ID = 6085551760


def env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logging.warning("环境变量 %s 不是有效整数，使用默认值 %s", name, default)
        return default


def env_bool(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _discover_bot_keys_from_env() -> list[str]:
    keys = []
    prefix = "BOT_TOKEN_"
    for k in os.environ.keys():
        if k.startswith(prefix) and len(k) > len(prefix):
            keys.append(k[len(prefix) :])
    return sorted(set(keys))


def load_bot_configs():
    configs = []
    seen_tokens = set()
    bot_keys = _discover_bot_keys_from_env()
    if not bot_keys:
        raise RuntimeError(
            "未找到机器人配置。请在环境变量中设置 BOT_TOKEN_<KEY>（例如 BOT_TOKEN_A）。"
        )

    for key in bot_keys:
        token = str(os.getenv(f"BOT_TOKEN_{key}", "")).strip()
        name = str(os.getenv(f"BOT_NAME_{key}", f"bot_{key.lower()}")).strip()
        owner_id = env_int(f"BOT_OWNER_{key}", DEFAULT_OWNER_ID)
        enabled = env_bool(f"BOT_ENABLE_{key}", True)
        raw_features = str(os.getenv(f"BOT_FEATURES_{key}", "")).strip()
        raw_disable_features = str(os.getenv(f"BOT_DISABLE_FEATURES_{key}", "")).strip()

        if not enabled:
            continue

        if ":" not in token or not name:
            logging.warning("跳过无效 token 配置: BOT_TOKEN_%s", key)
            continue
        if token in seen_tokens:
            logging.warning(
                "跳过重复 token 配置: BOT_TOKEN_%s（该 token 已在其他机器人配置中使用）",
                key,
            )
            continue
        seen_tokens.add(token)

        if raw_features:
            enabled_features = sanitize_features(
                parse_feature_list(raw_features),
                source_name=f"BOT_FEATURES_{key}",
            )
        else:
            enabled_features = set(ALL_FEATURES)

        if raw_disable_features:
            disabled_features = sanitize_features(
                parse_feature_list(raw_disable_features),
                source_name=f"BOT_DISABLE_FEATURES_{key}",
            )
            enabled_features -= disabled_features

        configs.append(
            {
                "token": token,
                "owner_id": owner_id,
                "name": name,
                "enabled_features": sorted(enabled_features),
            }
        )
    return configs


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
    await reply_from_owner(update, context)


async def private_forward_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bind_runtime_bot_context(context)
    await forward_to_owner(update, context)


async def start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """兜底 /start：保证未启用 verification 的机器人也能响应。"""
    if not update.message:
        return

    bot_name = context.application.bot_data.get("name", "机器人")
    features = sorted(context.application.bot_data.get("enabled_features", []))
    feature_text = ", ".join(features[:20]) if features else "默认功能"
    await update.message.reply_text(
        f"👋 欢迎使用 {bot_name}\n"
        f"当前启用功能：{feature_text}\n\n"
        "提示：群配置请私聊发送「群配置」。"
    )


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

    request = HTTPXRequest(connect_timeout=10.0, read_timeout=30.0)

    app = ApplicationBuilder().token(token).request(request).build()

    app.bot_data["owner_id"] = owner_id  # ✅ 绑定到当前机器人
    app.bot_data["token"] = token
    app.bot_data["name"] = bot_name
    app.bot_data["enabled_features"] = set(bot_cfg.get("enabled_features", []))
    set_bot_owner(bot_name, owner_id)
    app.add_handler(TypeHandler(Update, runtime_context_handler), group=-1000)

    # ===== 基础命令 =====
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(CommandHandler("leave", leave_group_command))

    # ===== 私聊转发逻辑 =====
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.REPLY & ~filters.COMMAND,
            owner_reply_router,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            private_forward_router,
        )
    )

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
        ten_minute_master_job_wrapper, interval=60, first=60
    )

    app.job_queue.run_repeating(
        five_minute_master_job_wrapper,
        interval=300,
    )

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

    # 超级管理员命令
    for cmd, desc in SUPER_ADMIN_COMMANDS.items():
        commands.append(BotCommand(cmd, desc))

    # 你可以在这里加普通用户命令
    commands.append(BotCommand("start_menu", "游戏菜单"))
    commands.append(BotCommand("group", "群开关"))

    await app.bot.set_my_commands(commands)


# ===== 重启命令（超级管理员使用） =====
async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_super_admin(update.effective_user.id):
        await update.message.reply_text("♻️ 正在重启机器人...")

        # 重启前清理广告词
        await cleaned_word()
        # # 1️⃣ 先结束所有正在进行的成语接龙
        # for chat_id in group_list.keys():
        #     fake_update = Update(
        #         update_id=chat_id, message=update.message  # 使用当前消息上下文
        #     )
        #     # 调用 end_chengyu，传入 fake_update 和 context
        #     await end_chengyu(fake_update, context)

        python = sys.executable
        os.execv(python, [python] + sys.argv)


async def leave_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_super_admin(update.effective_user.id):
        chat = update.effective_chat
        # 确保是在群组中触发
        if update.effective_chat.type in ["group", "supergroup"]:

            groups = load_json(GROUPS_FILE)
            if not isinstance(groups, dict):
                groups = {}

            chat_id = str(chat.id)
            cfg = groups.get(chat_id, {})
            if not isinstance(cfg, dict):
                cfg = {}
            cfg["title"] = chat.title or cfg.get("title", "")
            cfg["username"] = chat.username or cfg.get("username", "")
            cfg["type"] = chat.type
            cfg["bot_in_group"] = False
            groups[chat_id] = cfg
            save_json(GROUPS_FILE, groups)
            print(f"👋 已标记离群: {chat.title} ({chat.id})")

            await update.message.reply_text("👋 再见，我要离开这个群了！")
            await context.bot.leave_chat(update.effective_chat.id)
        else:
            await update.message.reply_text("这个命令只能在群里使用。")


async def post_init_setup(app):
    set_runtime_bot_name(app.bot_data.get("name", ""))
    await set_bot_commands(app)  # 直接 await，事件循环已运行


async def main():
    bot_configs = load_bot_configs()
    if not bot_configs:
        raise RuntimeError("没有可用的机器人配置，请检查 BOT_TOKEN/BOT_ENABLE 环境变量")

    apps = [create_app(cfg) for cfg in bot_configs]

    print(f"🤖 已加载 {len(apps)} 个机器人配置")

    try:
        for app in apps:
            try:
                await app.initialize()
                # 主动验证 token，避免进入 polling 后才刷 InvalidToken 错误
                await app.bot.get_me()
                await post_init_setup(app)
                await app.start()
                await app.updater.start_polling()
                print(
                    f"✅ 已启动: {app.bot_data.get('name')} (owner={app.bot_data.get('owner_id')}, username=@{app.bot.username})"
                )
            except InvalidToken:
                logging.error(
                    "❌ 机器人 token 无效，已跳过: %s",
                    app.bot_data.get("name"),
                )
                try:
                    await app.shutdown()
                except Exception:
                    pass
                continue
            print(
                f"   ↳ 启用功能: {', '.join(sorted(app.bot_data.get('enabled_features', [])))}"
            )

        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("🛑 收到 Ctrl+C，正在安全关闭机器人...")
    finally:
        for app in reversed(apps):
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as e:
                logging.exception(
                    "停止机器人失败 [%s]: %s", app.bot_data.get("name"), e
                )


if __name__ == "__main__":
    asyncio.run(main())
