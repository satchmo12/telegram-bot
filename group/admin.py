from telegram import Update, ChatPermissions
from telegram.ext import CommandHandler, ContextTypes
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
import re
from command_router import register_command
from utils import (
    WARNINGS_FILE,
    is_admin,
    is_super_admin,
    load_json,
    safe_reply,
    save_json,
    get_group_whitelist,
    GROUP_LIST_FILE,
)
import datetime


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
            update.message.reply_to_message.from_user.id,
            ChatPermissions(can_send_messages=False),
            until_date=until,
        )
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
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            update.message.reply_to_message.from_user.id,
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
@register_command("用户id")
async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_super_admin(update.effective_user.id):
        if update.message.reply_to_message:
            user = update.message.reply_to_message.from_user
            await safe_reply(update, context,
                f"👤 用户：{user.full_name}\n🆔 ID：<code>{user.id}</code>",
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
