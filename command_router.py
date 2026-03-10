# command_router.py

from telegram import Update
from typing import Optional
from functools import wraps
from feature_flags import ALL_FEATURES
from utils import (
    GROUP_LIST_FILE,
    get_group_whitelist,
    is_super_admin,
    safe_reply,
    save_json,
)
from telegram.ext import ContextTypes


ZH_COMMAND_MAP = {}
COMMANDS_INFO = {}


FEATURE_MANOR = "manor"
FEATURE_WELCOME = "welcome"
FEATURE_FRIENDS = "friends"  # 群好友功能 key


def _is_runtime_feature_enabled(
    context: ContextTypes.DEFAULT_TYPE, feature_name: str, default: bool = True
) -> bool:
    feature_key = (feature_name or "").strip().lower()
    if not feature_key:
        return default
    enabled_features = context.application.bot_data.get("enabled_features")
    if enabled_features is None:
        return default
    return feature_key in enabled_features


def _feature_for_handler(handler) -> Optional[str]:
    tagged_feature = getattr(handler, "__feature_name__", None)
    if isinstance(tagged_feature, str) and tagged_feature.strip():
        tagged = tagged_feature.strip().lower()
        # 仅将 .env 支持的功能键用于运行时开关；群内开关键（如 manor）继续走模块映射兜底
        if tagged in ALL_FEATURES:
            return tagged

    module = getattr(handler, "__module__", "") or ""

    module_feature_map = [
        ("game_niuniu", "niuniu"),
        ("farm", "game_hub"),
        ("info.economy", "economy_info"),
        ("info.economy_bank", "economy_bank"),
        ("lottery.betting", "lottery_betting"),
        ("market.price", "market_price"),
        ("company.business", "company_business"),
        ("company.company_ipo", "company_ipo"),
        ("company.company_recruit", "company_recruit"),
        ("chat.ai_chat", "ai_chat"),
        ("media.beauty", "beauty"),
        ("game.checkin", "checkin"),
        ("game.qa_game", "qa"),
        ("game.chengyu_game", "chengyu"),
        ("game.truth_game", "truth"),
        ("game.dice_game", "dice"),
        ("game.lottery_game", "lottery_game"),
        ("game.voice_reply", "voice_reply"),
        ("game.answer_book", "answer_book"),
        ("game.ssc", "ssc"),
        ("group.admin", "admin"),
        ("group.group_setting", "group_setting"),
        ("group.invite_stats", "invite_stats"),
        ("group.verify", "verification"),
        ("group.talk_stats", "talk_stats"),
        ("group.group_care", "group_care"),
        ("group.group_media_tools", "group_media_tools"),
        ("group.save_photos", "save_photos"),
        ("group.grouplist", "user_tracker"),
        ("chat.my_bot", "my_bot"),
        ("slave.slave_game", "simulation"),
        ("slave.work_game", "work"),
        ("slave.action_handler", "action"),
        ("slave.kidnap", "kidnap"),
        ("slave.guard_system", "guard"),
        ("menu", "menu"),
    ]
    for prefix, feature in module_feature_map:
        if module == prefix or module.startswith(prefix + "."):
            return feature
    return None


def _match_command(text: str, cmd: str) -> bool:
    """严格命令匹配：完整相等，或命令后紧跟空白。"""
    if text == cmd:
        return True
    return text.startswith(cmd + " ")


def _extract_args(text: str, cmd: str) -> list[str]:
    if text == cmd:
        return []
    return text[len(cmd) :].strip().split()


def _normalize_router_text(text: str) -> str:
    """
    支持将 Telegram 的 /命令 规范化为中文命令匹配文本：
    - /命令        -> 命令
    - /命令@Bot    -> 命令
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return t

    body = t[1:]
    if not body:
        return t

    first = body.split(maxsplit=1)[0]
    rest = body[len(first) :].strip()
    cmd = first.split("@", 1)[0].strip()
    if not cmd:
        return t
    return f"{cmd} {rest}".strip()


def get_matched_command(text: str) -> Optional[str]:
    """返回命中的注册命令名；未命中返回 None。"""
    t = _normalize_router_text(text or "")
    if not t:
        return None
    for cmd in sorted(ZH_COMMAND_MAP.keys(), key=len, reverse=True):
        if _match_command(t, cmd):
            return cmd
    return None


def register_command(*command_names):
    """注册一个或多个命令别名"""

    def decorator(func):
        for name in command_names:
            COMMANDS_INFO[name] = name
            ZH_COMMAND_MAP[name] = func
        return func

    return decorator


# 模块控制
def feature_required(feature_name: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context):
            chat_id = update.effective_chat.id
            if not is_feature_enabled(chat_id, feature_name, context):
                if feature_name == FEATURE_MANOR:
                    await safe_reply(
                        update,
                        context,
                        "⚠️ 本群庄园功能未开启，请管理员发送「群庄园」开启后再试。",
                    )
                return
            return await func(update, context)

        wrapper.__feature_name__ = (feature_name or "").strip().lower()
        return wrapper

    return decorator


# 是否开启
def is_feature_enabled(
    chat_id: int, feature: str, context: ContextTypes.DEFAULT_TYPE = None
) -> bool:
    data = get_group_whitelist(context)
    cfg = data.get(str(chat_id))
    if not cfg:
        return True  # 默认开启
    if feature == FEATURE_FRIENDS and feature not in cfg:
        return False
    return cfg.get(feature, True)


# 开关函数
async def toggle_feature(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    feature_key: str,
    feature_name: str,
):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)

    # if not is_super_admin(user_id):
    #     return

    group_whitelist = get_group_whitelist(context)
    group_config = group_whitelist.get(chat_id, {})

    if not group_config.get("enabled", False):
        return await safe_reply(
            update, context, "⚠️ 本群尚未启用主功能，请先使用 /addgroup 启用。"
        )

    current_status = group_config.get(feature_key, False)
    group_config[feature_key] = not current_status

    group_whitelist[chat_id] = group_config
    save_json(GROUP_LIST_FILE, group_whitelist)

    if not current_status:
        await safe_reply(update, context, f"✅ {feature_name}已开启。")
    else:
        await safe_reply(update, context, f"🚫 {feature_name}已关闭。")


async def dispatch_command(update, context):
    if not update.message or not update.message.text:
        return

    text = _normalize_router_text(update.message.text)
    chat_id = str(update.effective_chat.id)
    chat_type = (update.effective_chat.type or "").lower()
    is_group_chat = chat_type in {"group", "supergroup"}
    group_config = get_group_whitelist(context).get(chat_id, {})

    # 最长命令优先，避免「查看农场」被「查看」类命令误匹配
    ordered_cmds = sorted(ZH_COMMAND_MAP.keys(), key=len, reverse=True)

    for cmd in ordered_cmds:
        if _match_command(text, cmd):
            # 1) 机器人开关（.env BOT_FEATURES/BOT_DISABLE_FEATURES）
            handler = ZH_COMMAND_MAP[cmd]
            feature = _feature_for_handler(handler)
            if feature and not _is_runtime_feature_enabled(context, feature, True):
                if is_super_admin(update.effective_user.id):
                    bot_name = context.application.bot_data.get("name", "当前机器人")
                    print(f"⚠️ {bot_name} 未启用功能: {feature}")
                    # await safe_reply(
                    #     update,
                    #     context,
                    #     f"⚠️ {bot_name} 未启用功能: {feature}",
                    # )
                return True

            # 2) 群开关（仅群聊生效，私聊不拦截）
            if (
                is_group_chat
                and not is_super_admin(update.effective_user.id)
                and not group_config.get("enabled", False)
            ):
                await safe_reply(
                    update, context, "⚠️ 本群主功能未开启，请管理员发送「白名单」启用。"
                )
                return True

            # 3) 消息分发（执行命令处理器）
            args = _extract_args(text, cmd)
            context.args = args
            await handler(update, context)
            return True

    return False
