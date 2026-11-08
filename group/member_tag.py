# -*- coding: utf-8 -*-
"""Telegram-native tags for regular group members (Bot API setChatMemberTag)."""
from __future__ import annotations

import re

from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters


COMMAND_PATTERN = re.compile(r"标签(?:\s+|[：:]\s*)(.*)", re.S)
MAX_TAG_LENGTH = 16  # Official Bot API limit; emoji are rejected by Telegram.


def _is_group_admin(member) -> bool:
    return str(getattr(member, "status", "")).lower() in {
        "administrator",
        "owner",
        "creator",
    }


_ADMIN_STATUSES = {"administrator", "owner", "creator"}
_REGULAR_STATUSES = {"member", "restricted"}


async def _set_member_tag_from_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Reply to a regular member with ``标签 标签内容`` to set a native tag."""
    message = update.message
    chat = update.effective_chat
    actor = update.effective_user
    if not message or not chat or chat.type not in {"group", "supergroup"}:
        return False

    match = COMMAND_PATTERN.fullmatch(str(message.text or "").strip())
    if not match or not message.reply_to_message:
        return False
    if not actor:
        return True

    try:
        actor_member = await context.bot.get_chat_member(chat.id, actor.id)
    except TelegramError as exc:
        await message.reply_text(f"❌ 无法读取你的群管理员状态：{str(exc)[:160]}")
        return True
    if not _is_group_admin(actor_member):
        await message.reply_text("❌ 只有本群管理员可以设置群内系统标签。")
        return True

    target = getattr(message.reply_to_message, "from_user", None)
    if not target or getattr(target, "is_bot", False):
        await message.reply_text("❌ 请回复一位普通成员的消息后设置标签。")
        return True

    tag = match.group(1).strip()
    if tag in {"清空", "删除", "-"}:
        tag = ""
    elif not tag:
        await message.reply_text("❌ 请使用“标签 标签内容”，例如：标签 广州老师。")
        return True
    elif len(tag) > MAX_TAG_LENGTH:
        await message.reply_text(f"❌ 群内标签不能超过 {MAX_TAG_LENGTH} 个字符。")
        return True

    try:
        target_member = await context.bot.get_chat_member(chat.id, target.id)
    except TelegramError as exc:
        await message.reply_text(f"❌ 无法读取目标成员状态：{str(exc)[:160]}")
        return True

    status = str(getattr(target_member, "status", "")).lower()
    try:
        if status in _REGULAR_STATUSES:
            # python-telegram-bot 22.5 has not exposed this newer Bot API
            # endpoint yet. _post still uses PTB's official request stack.
            await context.bot._post(
                "setChatMemberTag",
                data={"chat_id": chat.id, "user_id": target.id, "tag": tag},
            )
            target_kind = "群内系统标签"
        elif status in _ADMIN_STATUSES:
            # Telegram uses a different API for administrator titles. This is
            # why replying to an admin previously failed while regular members
            # could be tagged successfully.
            await context.bot.set_chat_administrator_custom_title(
                chat_id=chat.id,
                user_id=target.id,
                custom_title=tag,
            )
            target_kind = "管理员头衔"
        else:
            await message.reply_text("❌ 该成员当前不在群内，无法设置标签。")
            return True
    except Forbidden:
        await message.reply_text("❌ 机器人没有管理群内标签/管理员头衔的权限。")
        return True
    except BadRequest as exc:
        detail = str(exc)[:180]
        if status in _ADMIN_STATUSES:
            await message.reply_text(
                "❌ 管理员头衔设置失败。Telegram 只允许机器人为它自己提升/可管理的管理员设置头衔，"
                "群主通常不能修改。\n"
                f"原因：{detail}"
            )
        else:
            await message.reply_text(
                "❌ 普通成员标签设置失败。机器人必须是管理员且拥有“管理标签”权限；"
                "标签不能包含 emoji。\n"
                f"原因：{detail}"
            )
        return True
    except TelegramError as exc:
        await message.reply_text(f"❌ 设置标签失败：{str(exc)[:180]}")
        return True

    action = "已清空" if not tag else "已设置"
    await message.reply_text(
        f"✅ {action} {target.full_name} 的{target_kind}"
        + (f"：{tag}" if tag else "。")
    )
    return True


async def _member_tag_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _set_member_tag_from_reply(update, context):
        raise ApplicationHandlerStop


def register_member_tag_handlers(app) -> None:
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & (~filters.COMMAND)
            & ~filters.UpdateType.BUSINESS_MESSAGE,
            _member_tag_interceptor,
        ),
        group=-19,
    )
