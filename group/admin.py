from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
import re
import time
from pypinyin import lazy_pinyin
from command_router import register_command, get_matched_command
from utils import (
    WARNINGS_FILE,
    BOT_USER_FILE,
    FORWARD_MAP_FILE,
    SHARED_SESSION_NAME,
    is_admin,
    is_super_admin,
    get_session_path,
    load_json,
    safe_reply,
    save_json,
    get_group_whitelist,
    GROUP_LIST_FILE,
)
import datetime
from group.mute_registry import add_mute, remove_mute, list_mutes


_USERNAME_CHECK_COOLDOWN: dict[int, float] = {}


def get_warnings_data() -> dict:
    data = load_json(WARNINGS_FILE)
    return data if isinstance(data, dict) else {}


def group_enabled_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if get_group_whitelist(context).get(chat_id, {}).get("enabled", False):
            return await func(update, context)
        else:
            return await safe_reply(update, context,"⚠️ 当前群未启用此功能。")

    return wrapper


def _full_send_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    )


def _normalize_group_target(raw: str):
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    if s.startswith("t.me/") or s.startswith("telegram.me/"):
        s = s.split("/", 1)[1] if "/" in s else s
    s = s.strip()
    if s.startswith("@"):
        return s
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return f"@{s}"


def _normalize_username(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s.strip()


def _is_valid_tg_username(username: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username or ""))


def _contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _to_pinyin(text: str) -> str:
    parts = lazy_pinyin(text or "")
    return "".join(parts).lower()


def _expand_alpha_wildcards(base: str, limit: int = 120) -> list[str]:
    """
    将字母位置按 a-z 做受控扩展。
    只扩展前两位字母位置，避免组合爆炸。
    """
    if not base:
        return []

    base = base.lower()
    alpha_positions = [idx for idx, ch in enumerate(base) if ch.isalpha()]
    if not alpha_positions:
        return [base]

    target_positions = alpha_positions[:2]
    results = []
    seen = set()
    letters = "abcdefghijklmnopqrstuvwxyz"

    def build(candidate_letters: tuple[str, ...]) -> str:
        chars = list(base)
        for pos, letter in zip(target_positions, candidate_letters):
            chars[pos] = letter
        return "".join(chars)

    if len(target_positions) == 1:
        for a in letters:
            value = build((a,))
            if value not in seen:
                seen.add(value)
                results.append(value)
            if len(results) >= limit:
                break
        return results

    for a in letters:
        for b in letters:
            value = build((a, b))
            if value not in seen:
                seen.add(value)
                results.append(value)
            if len(results) >= limit:
                return results

    return results


def _build_username_candidates(keyword: str) -> list[str]:
    raw = _normalize_username(keyword)
    if not raw:
        return []

    bases: list[str] = []
    if _contains_chinese(raw):
        pinyin_base = _to_pinyin(raw)
        if pinyin_base:
            bases.append(pinyin_base)
    else:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", raw).lower()
        if cleaned:
            bases.append(cleaned)

    # 中文输入时，同时补一个“原样转小写后清洗”的备用基底，避免只命中拼音模式
    fallback_base = re.sub(r"[^A-Za-z0-9_]", "", raw).lower()
    if fallback_base and fallback_base not in bases:
        bases.append(fallback_base)

    candidates: list[str] = []
    seen: set[str] = set()

    def push(value: str):
        value = re.sub(r"_+", "_", value).strip("_")
        if not value:
            return
        if not re.match(r"^[a-z][a-z0-9_]{4,31}$", value):
            return
        if value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for base in bases:
        is_five_char_third_repeat = (
            len(base) == 5
            and base[-1] == base[-2] == base[-3]
            and base.isalnum()
        )

        push(base)
        for variant in _expand_alpha_wildcards(base):
            push(variant)

        if is_five_char_third_repeat:
            continue

        if len(base) < 5:
            pad_char = base[-1:] or "a"
            push(base + pad_char * (5 - len(base)))
        if len(base) >= 5 and base[-1] == base[-2] == base[-3]:
            push(base[:2] + base[-1] * 3)

        suffixes = ["", "000", "111", "123", "321", "520", "521", "1314", "518", "618"]
        for suffix in suffixes:
            push(base + suffix)
            push(base + "_" + suffix if suffix else base)

        if len(base) >= 2:
            push(base[:2] + "_" + base[2:])
            push(base[:3] + "_" + base[3:] if len(base) > 3 else base)

        if len(base) >= 3:
            tail = base[-1]
            push(base[:2] + tail * 3)
            push(base[:1] + tail * 4)

    return candidates[:30]


async def _check_username_available(context: ContextTypes.DEFAULT_TYPE, username: str) -> bool:
    try:
        from telethon import TelegramClient
        from telethon.tl.functions.account import CheckUsernameRequest
    except Exception:
        return False

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return False

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return False
        return bool(await client(CheckUsernameRequest(username)))
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _resolve_reply_user_for_id(update: Update):
    reply = update.message.reply_to_message if update.message else None
    if not reply:
        return None

    forward_map = load_json(FORWARD_MAP_FILE) or {}
    mapped_uid = forward_map.get(str(reply.message_id))
    if mapped_uid is not None:
        uid = str(mapped_uid)
        user_data = load_json(BOT_USER_FILE) or {}
        info = user_data.get(uid, {}) if isinstance(user_data, dict) else {}
        name = str(info.get("name", "")).strip() or f"用户 {uid}"
        username = str(info.get("username", "")).strip()
        if username:
            name = f"{name} (@{username})"
        return int(uid), name

    user = getattr(reply, "from_user", None)
    if not user:
        return None
    name = user.full_name
    if getattr(user, "username", None):
        name = f"{name} (@{user.username})"
    return int(user.id), name


@register_command("群管")
async def start_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context,
        f"📖 群管功能！\n  '踢' 踢人，选择要踢的人回复 \n '移除黑名单' 从黑名单移除\n '禁言' 禁言+时间\n '解除禁言'解除禁言 \n /警告，警号三次踢出 \n"
    )


@group_enabled_only
@register_command("踢")
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")

    # bot_member = await context.bot.get_chat_member(
    #     update.effective_chat.id, context.bot.id
    # )
    # if not bot_member.can_restrict_members:
    #     return f"⚠ 我没有“限制用户权限”的权限，无法操作成员。"

    if not update.message.reply_to_message:
        return
        return await safe_reply(update, context,"请回复需要踢出的人。")

    target_user = update.message.reply_to_message.from_user
    if target_user and is_super_admin(target_user.id):
        return
        # return await safe_reply(update, context, "⚠️ 目标是超级管理员，不能踢出。")

    try:
        await context.bot.ban_chat_member(
            update.effective_chat.id, target_user.id
        )
        await safe_reply(update, context,"✅ 用户已被踢出。")
    except Exception as e:
        await safe_reply(update, context,f"❌ 失败：{e}")


@group_enabled_only
@register_command("移除黑名单")
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")
    if not await check_can_restrict(update, context):
        return
        return await safe_reply(update, context, "⚠️ 我没有限制成员权限，无法移除黑名单。")
    if not update.message.reply_to_message:
        return await safe_reply(update, context,"请回复需要解封的人。")
    try:
        await context.bot.unban_chat_member(
            update.effective_chat.id, update.message.reply_to_message.from_user.id
        )
        await safe_reply(update, context,"✅ 用户已解封。")
    except Exception as e:
        await safe_reply(update, context,f"❌ 失败：{e}")


@register_command("移除黑名单ID", "黑名单移除")
async def unban_user_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    超级管理员专用：
    在任意聊天（建议私聊机器人）通过 群ID+用户ID 解除黑名单。
    用法：移除黑名单ID -1001234567890 123456789
    """
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你没有权限执行此命令。")

    if len(context.args) < 2:
        return await safe_reply(
            update,
            context,
            "📌 用法：移除黑名单ID 群ID 用户ID\n例如：移除黑名单ID -1001234567890 123456789",
        )

    try:
        chat_id = int(context.args[0])
        user_id = int(context.args[1])
    except Exception:
        return await safe_reply(
            update, context, "❗参数格式错误，群ID 和 用户ID 都必须是数字。"
        )

    try:
        await context.bot.unban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            only_if_banned=False,
        )
        await safe_reply(
            update,
            context,
            f"✅ 已尝试移除黑名单。\n群ID：<code>{chat_id}</code>\n用户ID：<code>{user_id}</code>",
            html=True,
        )
    except Exception as e:
        await safe_reply(update, context, f"❌ 操作失败：{e}")


@group_enabled_only
@register_command("禁言")
async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")
    if not await check_can_restrict(update, context):
        return
        # return await safe_reply(update, context, "⚠️ 我没有限制成员权限，无法禁言。")
    if not update.message.reply_to_message:
        return await safe_reply(update, context,"请回复需要禁言的人。")

    chat = update.effective_chat
    if chat.type != "supergroup":
        await safe_reply(update, context,"❗此功能只能在超级群中使用。")
        return

    try:
        target_user = update.message.reply_to_message.from_user
        if context.args:
            # 解析时长
            duration = parse_duration(context.args[0])
            until = update.message.date + duration
            tip = f"🔇 已禁言 {context.args[0]}"
        else:
            # 永久禁言
            until = None  # Telegram 里表示永久
            tip = "🔇 已永久禁言"

        await context.bot.restrict_chat_member(
            chat.id,
            target_user.id,
            ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        add_mute(str(chat.id), target_user.id, target_user.full_name, source="admin")
        await safe_reply(update, context,tip)
    except Exception as e:
        await safe_reply(update, context,f"❌ 失败：{e}")


@group_enabled_only
@register_command("解禁", "解除禁言")
async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")
    if not await check_can_restrict(update, context):
        return
        # return await safe_reply(update, context, "⚠️ 我没有限制成员权限，无法解除禁言。")
    if not update.message.reply_to_message:
        return await safe_reply(update, context,"请回复需要解禁的人。")
    chat = update.effective_chat
    if chat.type != "supergroup":
        await safe_reply(update, context,"❗此功能只能在超级群中使用。")
        return
    try:
        target_user = update.message.reply_to_message.from_user
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target_user.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
                can_manage_topics=False,
            ),
        )
        remove_mute(str(update.effective_chat.id), target_user.id)
        await safe_reply(update, context,"🔊 已解除禁言。")
    except (BadRequest, Forbidden) as e:
        if "Not enough rights" in str(e):
            return await safe_reply(
                update,
                context,
                "⚠️ 我没有解除禁言权限。请把机器人设为管理员，并开启“限制成员”权限。",
            )
        await safe_reply(update, context,f"❌ 失败：{e}")
    except Exception as e:
        await safe_reply(update, context,f"❌ 失败：{e}")

@register_command("解禁ID", "禁言移除", "解除禁言ID")
async def unmute_user_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    超级管理员专用：
    在任意聊天通过 群ID+用户ID 解除禁言。
    用法：解禁ID -1001234567890 123456789
    """
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你没有权限执行此命令。")

    if len(context.args) < 2:
        return await safe_reply(
            update,
            context,
            "📌 用法：解禁ID 群ID 用户ID\n例如：解禁ID -1001234567890 123456789",
        )

    try:
        chat_id = int(context.args[0])
        user_id = int(context.args[1])
    except Exception:
        return await safe_reply(
            update, context, "❗参数格式错误，群ID 和 用户ID 都必须是数字。"
        )

    if not await check_can_restrict_in_chat(context, chat_id):
        return await safe_reply(
            update,
            context,
            "⚠️ 目标群权限不足：请先把机器人拉进该群并设为管理员，同时开启“限制成员”权限。",
        )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
                can_manage_topics=False,
            ),
        )
        remove_mute(str(chat_id), user_id)
        await safe_reply(
            update,
            context,
            f"✅ 已尝试解除禁言。\n群ID：<code>{chat_id}</code>\n用户ID：<code>{user_id}</code>",
            html=True,
        )
    except (BadRequest, Forbidden) as e:
        if "Not enough rights" in str(e):
            return await safe_reply(
                update,
                context,
                "⚠️ 操作失败：机器人在目标群没有足够权限解除禁言（需要管理员 + 限制成员权限）。",
            )
        await safe_reply(update, context, f"❌ 操作失败：{e}")
    except Exception as e:
        await safe_reply(update, context, f"❌ 操作失败：{e}")

@group_enabled_only
@register_command("删除")
async def delete_replied_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        return
        return await safe_reply(update, context, "⚠️ 请回复要删除的那条消息。")

    # 机器人需要有删除消息权限
    try:
        bot_member = await context.bot.get_chat_member(
            update.effective_chat.id, context.bot.id
        )
        can_delete = bool(getattr(bot_member, "can_delete_messages", False))
        if not can_delete:
            return
            return await safe_reply(
                update, context, "⚠️ 我没有删除消息权限，请给机器人管理员的“删除消息”权限。"
            )
    except Exception:
        return await safe_reply(update, context, "⚠️ 无法获取机器人权限信息。")

    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.reply_to_message.message_id,
        )
        # await safe_reply(update, context, "✅ 已删除该消息。")
    except Exception as e:
        await safe_reply(update, context, f"❌ 删除失败：{e}")


@group_enabled_only
@register_command("警告")
async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")
    if not await check_can_restrict(update, context):
        return
        return await safe_reply(update, context, "⚠️ 我没有限制成员权限，无法执行警告踢人。")
    if not update.message.reply_to_message:
        return await safe_reply(update, context,"请回复需要警告的人。")

    chat_id = str(update.effective_chat.id)
    user_id = str(update.message.reply_to_message.from_user.id)
    warnings = get_warnings_data()

    if chat_id not in warnings:
        warnings[chat_id] = {}
    if user_id not in warnings[chat_id]:
        warnings[chat_id][user_id] = 0

    warnings[chat_id][user_id] += 1
    save_json(WARNINGS_FILE, warnings)

    count = warnings[chat_id][user_id]
    if count >= 3:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, int(user_id))
            await safe_reply(update, context,"🚫 警告 3 次，已踢出用户。")
        except Exception as e:
            await safe_reply(update, context,f"⚠️ 踢人失败：{e}")
    else:
        await safe_reply(update, context,f"⚠️ 当前警告次数：{count}/3")


@group_enabled_only
@register_command("管理员")
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    names = [admin.user.full_name for admin in admins]
    await safe_reply(update, context,"👮 当前群管理员：\n" + "\n".join(names))


# 获取id  仅高级管理
@register_command("用户id", "查询id", "查id", "用户ID", "查询ID", "查ID")
async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_super_admin(update.effective_user.id):
        if update.message.reply_to_message:
            resolved = _resolve_reply_user_for_id(update)
            if not resolved:
                return await safe_reply(update, context, "未找到被回复用户的信息。")
            user_id, user_label = resolved
            await safe_reply(
                update,
                context,
                f"👤 用户：{user_label}\n🆔 ID：<code>{user_id}</code>",
                html=True,
            )
        else:
            user = update.effective_user
            await safe_reply(update, context,
                f"👤 你自己：{user.full_name}\n🆔 ID：<code>{user.id}</code>",
                html=True,
            )
    else:
        return await safe_reply(update, context,"🚫 你不是管理员，无法执行此命令。")


@register_command("群id")
async def get_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_super_admin(update.effective_user.id):
        chat = update.effective_chat
        await safe_reply(update, context,
            f"📢 群名：{chat.title or '私聊/频道'}\n🆔 群ID：<code>{chat.id}</code>",
            html=True,
        )
    else:
        return await safe_reply(update, context,"🚫 你不是管理员，无法执行此命令。")


@register_command("注册", "注册用户名", "创建用户名", "设置用户名")
async def register_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if update.effective_chat.type not in {"group", "supergroup", "channel"}:
        return await safe_reply(update, context, "请在目标群或频道中使用：注册用户名 <用户名>")
    if not context.args:
        return await safe_reply(update, context, "用法：注册用户名 <用户名>")

    username = _normalize_username(context.args[0])
    if not _is_valid_tg_username(username):
        return await safe_reply(
            update,
            context,
            "用户名格式不正确，需以字母开头，长度 5-32 位，只能包含字母、数字和下划线。",
        )

    now = time.time()
    last_call = _USERNAME_CHECK_COOLDOWN.get(int(update.effective_user.id), 0.0)
    if now - last_call < 10:
        wait_seconds = int(10 - (now - last_call))
        return await safe_reply(
            update, context, f"请稍后再试，剩余冷却 {wait_seconds} 秒。", auto_delete_seconds=0
        )
    _USERNAME_CHECK_COOLDOWN[int(update.effective_user.id)] = now

    try:
        from telethon import TelegramClient
        from telethon.tl.functions.channels import UpdateUsernameRequest
    except Exception:
        return await safe_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await safe_reply(update, context, "❗ 未配置 Telethon API 信息。")

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return await safe_reply(update, context, "❗ 协议号未登录，请先登录可用的小号。")

        entity = await client.get_entity(update.effective_chat.id)
        if update.effective_chat.type == "group":
            return await safe_reply(update, context, "❗ 普通群不能设置用户名，请先升级为超级群。")

        await client(UpdateUsernameRequest(entity, username))
        await safe_reply(
            update,
            context,
            f"✅ 用户名设置成功：@{username}",
            auto_delete_seconds=0,
        )
    except Exception as e:
        await safe_reply(update, context, f"❌ 用户名设置失败：{e}", auto_delete_seconds=0)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@register_command("创建频道", "创建群组", "创建超级群")
async def create_channel_or_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊机器人使用：创建频道 <标题> [用户名]")
    if not context.args:
        return await safe_reply(update, context, "用法：创建频道 <标题> [用户名]")

    title = str(context.args[0]).strip()
    username = _normalize_username(context.args[1]) if len(context.args) > 1 else ""
    if not title:
        return await safe_reply(update, context, "请输入有效的标题。")
    if username and not _is_valid_tg_username(username):
        return await safe_reply(
            update,
            context,
            "用户名格式不正确，需以字母开头，长度 5-32 位，只能包含字母、数字和下划线。",
        )

    now = time.time()
    last_call = _USERNAME_CHECK_COOLDOWN.get(int(update.effective_user.id), 0.0)
    if now - last_call < 15:
        wait_seconds = int(15 - (now - last_call))
        return await safe_reply(
            update, context, f"请稍后再试，剩余冷却 {wait_seconds} 秒。", auto_delete_seconds=0
        )
    _USERNAME_CHECK_COOLDOWN[int(update.effective_user.id)] = now

    command_name = get_matched_command(update.message.text or "") or ""
    is_channel = command_name == "创建频道"

    try:
        from telethon import TelegramClient
        from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest
    except Exception:
        return await safe_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await safe_reply(update, context, "❗ 未配置 Telethon API 信息。")

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return await safe_reply(update, context, "❗ 协议号未登录，请先登录可用的小号。")

        created = await client(
            CreateChannelRequest(
                title=title,
                about=f"Created by bot: {title}",
                megagroup=not is_channel,
            )
        )
        entity = getattr(created, "chats", [None])[0]
        if not entity:
            return await safe_reply(update, context, "❌ 创建失败：未返回频道/群组实体。", auto_delete_seconds=0)

        final_username = username
        if final_username:
            try:
                await client(UpdateUsernameRequest(entity, final_username))
            except Exception as e:
                await safe_reply(
                    update,
                    context,
                    f"⚠️ 已创建成功，但用户名设置失败：{e}",
                    auto_delete_seconds=0,
                )
                final_username = ""

        chat_id = getattr(entity, "id", "")
        kind = "频道" if not getattr(entity, "megagroup", False) else "超级群"
        msg = [
            f"✅ 创建成功：{kind}",
            f"标题：{title}",
            f"ID：<code>{chat_id}</code>",
        ]
        if final_username:
            msg.append(f"用户名：@{final_username}")
        await safe_reply(update, context, "\n".join(msg), html=True, auto_delete_seconds=0)
    except Exception as e:
        await safe_reply(update, context, f"❌ 创建失败：{e}", auto_delete_seconds=0)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@register_command("协议号查询", "查询协议号", "查协议号", "查询用户名", "用户名查询")
async def query_protocol_id_by_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if not context.args:
        return await safe_reply(update, context, "用法：协议号查询 @用户名")

    username = _normalize_username(context.args[0])
    if not username:
        return await safe_reply(update, context, "请输入有效的用户名。")

    try:
        from telethon import TelegramClient
    except Exception:
        return await safe_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await safe_reply(update, context, "❗ 未配置 Telethon API 信息。")

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return await safe_reply(update, context, "❗ 协议号未登录，请先登录可用的小号。")

        entity = await client.get_entity(username)
        user_id = getattr(entity, "id", None)
        if not user_id:
            return await safe_reply(update, context, "未查询到该用户名对应的协议号。")

        display_name = getattr(entity, "first_name", "") or getattr(entity, "title", "") or "未知用户"
        resolved_username = _normalize_username(getattr(entity, "username", "") or username)
        await safe_reply(
            update,
            context,
            f"👤 用户：{display_name}\n"
            f"@{resolved_username}\n"
            f"🆔 协议号：<code>{user_id}</code>",
            html=True,
        )
    except Exception as e:
        await safe_reply(update, context, f"查询失败：{e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@register_command("检测", "检测用户名", "查用户名")
async def detect_username_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if not context.args:
        return await safe_reply(update, context, "用法：检测 美女")

    now = time.time()
    last_call = _USERNAME_CHECK_COOLDOWN.get(int(update.effective_user.id), 0.0)
    if now - last_call < 20:
        wait_seconds = int(20 - (now - last_call))
        return await safe_reply(update, context, f"请稍后再试，剩余冷却 {wait_seconds} 秒。", auto_delete_seconds=0)
    _USERNAME_CHECK_COOLDOWN[int(update.effective_user.id)] = now

    keyword = " ".join(context.args).strip()
    candidates = _build_username_candidates(keyword)
    if not candidates:
        return await safe_reply(update, context, "请输入有效的中文或字母关键词。", auto_delete_seconds=0)

    lines = [f"🔎 关键词：{keyword}", "可尝试注册的用户名："]
    available: list[str] = []
    checked = 0

    for username in candidates:
        checked += 1
        is_available = await _check_username_available(context, username)
        if is_available:
            available.append(username)
            lines.append(f"✅ @{username}")
        else:
            lines.append(f"❌ @{username}")
        if len(available) >= 15:
            break

    if not available:
        lines.append("")
        lines.append("没有找到可用的用户名候选。")
    else:
        lines.append("")
        lines.append("结果仅列出可注册项，建议尽快尝试。")

    lines.append(f"已检测：{checked} 个候选")
    await safe_reply(update, context, "\n".join(lines), auto_delete_seconds=0)


@group_enabled_only
@register_command("查看禁言")
async def list_mute_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    chat_id = str(update.effective_chat.id)
    data = list_mutes(chat_id)
    if not data:
        return await safe_reply(update, context, "✅ 当前没有被禁言的用户。")

    lines = ["🔇 禁言列表："]
    keyboard_rows = []
    for idx, (uid, info) in enumerate(data.items(), start=1):
        name = (info or {}).get("name") or uid
        lines.append(f"{idx}. {name} (ID: {uid})")
        keyboard_rows.append(
            [InlineKeyboardButton(f"解禁 {name}", callback_data=f"mute_list_unmute|{chat_id}|{uid}")]
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


@register_command("我要进群", "进群")
async def join_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊机器人使用：我要进群 群号/群用户名")
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你没有权限执行此命令。")
    if not context.args:
        return await safe_reply(update, context, "用法：我要进群 群号/群用户名")

    target = _normalize_group_target(context.args[0])
    if target is None:
        return await safe_reply(update, context, "❗ 群号/群用户名格式不正确。")

    try:
        chat = await context.bot.get_chat(target)
    except Exception as e:
        return await safe_reply(update, context, f"❌ 找不到群：{e}")

    if chat.type not in ("group", "supergroup"):
        return await safe_reply(update, context, "❗ 目标不是群/超级群。")

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except Exception as e:
        return await safe_reply(update, context, f"❌ 无法获取机器人权限：{e}")

    if bot_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return await safe_reply(update, context, "❌ 机器人不在该群，无法邀请。")

    user_id = update.effective_user.id
    can_restrict = bool(getattr(bot_member, "can_restrict_members", False))
    can_invite = bool(
        getattr(bot_member, "can_invite_users", False)
        or getattr(bot_member, "can_create_invite_link", False)
    )

    unban_ok = False
    unmute_ok = False
    if can_restrict:
        try:
            await context.bot.unban_chat_member(
                chat_id=chat.id, user_id=user_id, only_if_banned=False
            )
            unban_ok = True
        except Exception:
            pass
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user_id,
                permissions=_full_send_permissions(),
            )
            unmute_ok = True
        except Exception:
            pass

    invite_link = None
    if can_invite:
        try:
            expire_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat.id,
                member_limit=1,
                expire_date=expire_at,
                name=f"self-invite:{user_id}",
            )
            invite_link = invite.invite_link
        except Exception:
            try:
                invite_link = await context.bot.export_chat_invite_link(chat.id)
            except Exception:
                invite_link = None

    lines = [
        f"目标群：{chat.title or chat.id}",
        f"解封：{'成功' if unban_ok else '未执行/失败'}",
        f"解除禁言：{'成功' if unmute_ok else '未执行/失败'}",
    ]
    if invite_link:
        lines.append(f"邀请链接：{invite_link}")
    else:
        lines.append("邀请链接：失败（机器人无邀请权限或群设置限制）")

    await safe_reply(update, context, "\n".join(lines))


@register_command("发送", "群发单条", "发送消息")
async def send_to_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊机器人使用：发送 群号/用户名 内容")
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你没有权限执行此命令。")
    if len(context.args) < 2:
        return await safe_reply(update, context, "用法：发送 群号/用户名 内容")

    target = _normalize_group_target(context.args[0])
    if target is None:
        return await safe_reply(update, context, "❗ 群号/用户名格式不正确。")

    text = " ".join(context.args[1:]).strip()
    if not text:
        return await safe_reply(update, context, "❗ 发送内容不能为空。")

    try:
        await context.bot.send_message(chat_id=target, text=text)
    except Exception as e:
        return await safe_reply(update, context, f"❌ 发送失败：{e}")

    await safe_reply(update, context, "✅ 已发送。")


# ===== 操作群白名单（切换启用状态） =====
@register_command("白名单")
async def toggle_group_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)

    if not is_super_admin(user_id):
        return await safe_reply(update, context,"🚫 你没有权限执行此命令。")

    group_whitelist = get_group_whitelist(context)
    current = group_whitelist.get(chat_id, {}).get("enabled", False)

    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:

        group_config = group_whitelist.get(chat_id, {})
        group_config["enabled"] = not group_config.get("enabled", False)
        group_whitelist[chat_id] = group_config
        save_json(GROUP_LIST_FILE, group_whitelist)

        if not current:
            await safe_reply(update, context,"✅ 本群功能已启用。")
        else:
            await safe_reply(update, context,"🚫 本群功能已被禁用。")


@register_command("撤回")
async def recall_bot_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
        return await safe_reply(update, context, "⚠️ 该命令只能在群里使用。")
    if not is_super_admin(update.effective_user.id):
        return
        return await safe_reply(update, context, "🚫 仅超级管理员可使用该命令。")
    if not update.message.reply_to_message:
        return
        return await safe_reply(update, context, "⚠️ 请回复一条机器人消息后发送“撤回”。")

    target = update.message.reply_to_message
    target_user = target.from_user
    if not target_user or int(target_user.id) != int(context.bot.id):
        return
        return await safe_reply(update, context, "⚠️ 只能撤回机器人发送的消息。")

    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=target.message_id,
        )
    except Exception as e:
        return await safe_reply(update, context, f"❌ 撤回失败：{e}")

def parse_duration(arg: str) -> datetime.timedelta:
    if arg.endswith("h"):
        return datetime.timedelta(hours=int(arg[:-1]))
    elif arg.endswith("d"):
        return datetime.timedelta(days=int(arg[:-1]))
    else:
        return datetime.timedelta(minutes=int(arg))


@group_enabled_only
@register_command("锁群")
async def lock_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")
    if not await check_can_restrict(update, context):
        return await safe_reply(update, context, "⚠️ 我没有修改群权限的管理员权限。")

    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id, ChatPermissions(can_send_messages=False)
        )
        await safe_reply(update, context,"🔒 群已锁定，仅管理员可发言。")
    except (BadRequest, Forbidden) as e:
        if "Not enough rights" in str(e):
            return await safe_reply(update, context, "⚠️ 我权限不足，无法锁群。请给我开启“限制成员/管理群”权限。")
        return await safe_reply(update, context, f"❌ 锁群失败：{e}")


@group_enabled_only
@register_command("解锁群")
async def unlock_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
        # return await safe_reply(update, context,"你没有权限。")
    if not await check_can_restrict(update, context):
        return await safe_reply(update, context, "⚠️ 我没有修改群权限的管理员权限。")

    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id, ChatPermissions(can_send_messages=True)
        )
        await safe_reply(update, context,"🔓 群已解锁，所有人可发言。")
    except (BadRequest, Forbidden) as e:
        if "Not enough rights" in str(e):
            return await safe_reply(update, context, "⚠️ 我权限不足，无法解锁群。请给我开启“限制成员/管理群”权限。")
        return await safe_reply(update, context, f"❌ 解锁群失败：{e}")


async def check_can_restrict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_member = await context.bot.get_chat_member(
            update.effective_chat.id, context.bot.id
        )
        return bool(getattr(bot_member, "can_restrict_members", False))
    except Exception:
        return False


async def check_can_restrict_in_chat(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> bool:
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        return bool(getattr(bot_member, "can_restrict_members", False))
    except Exception:
        return False


async def mute_list_unmute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        _, chat_id_str, user_id_str = query.data.split("|", 2)
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
    except Exception:
        return

    try:
        member = await context.bot.get_chat_member(chat_id, query.from_user.id)
        if member.status not in {"administrator", "creator"} and not is_super_admin(query.from_user.id):
            return await query.answer("仅管理员可操作。", show_alert=True)
    except Exception:
        return await query.answer("无法校验权限。", show_alert=True)

    if not await check_can_restrict_in_chat(context, chat_id):
        return await query.answer("⚠️ 机器人没有限制成员权限。", show_alert=True)

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=_full_send_permissions(),
        )
        remove_mute(str(chat_id), user_id)
        await query.answer("✅ 已解除禁言", show_alert=True)
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        await query.answer(f"❌ 解禁失败：{e}", show_alert=True)


def register_admin_handlers(app):
    app.add_handler(CommandHandler("help", start_help))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("unban_id", unban_user_by_id))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("delete", delete_replied_message))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_handler(CommandHandler("lock", lock_group))
    app.add_handler(CommandHandler("unlock", unlock_group))

    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("groupid", get_group_id))
    app.add_handler(CommandHandler("addgroup", toggle_group_whitelist))
    app.add_handler(CommandHandler("recall", recall_bot_message))
    app.add_handler(CallbackQueryHandler(mute_list_unmute_callback, pattern=r"^mute_list_unmute\|"))
