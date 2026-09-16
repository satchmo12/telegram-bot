# -*- coding: utf-8 -*-
"""Owner-managed custom Telegram command templates.

Each bot keeps its own command definitions in ``config_data/custom_commands.json``.
A visible definition is placed in Telegram's slash-command menu; it can reply
with text or with configurable text plus a URL button.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from admin_permissions import is_owner_or_super_admin
from utils import load_json, save_json

CUSTOM_COMMANDS_FILE = "config_data/custom_commands.json"
CALLBACK_PREFIX = "ccmd"
DRAFT_KEY = "custom_command_draft"

MAX_CUSTOM_COMMANDS = 80
COMMAND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Do not let a user-configured command shadow the bot's own control commands.
RESERVED_COMMANDS = {
    "start",
    "help",
    "restart",
    "show",
    "hide",
    "command_templates",
    "command_manage",
}

DEFAULT_COMMANDS = [
    {
        "id": 1,
        "command": "moban",
        "description": "模版",
        "visible": True,
        "mode": "text",
        "content": "模版：sdfads",
        "button_text": "点击跳转",
        "url": "",
    }
]


def _default_config() -> dict:
    return {"commands": [dict(item) for item in DEFAULT_COMMANDS]}


def _normalize_command_name(value: object) -> str:
    command = str(value or "").strip().lower().lstrip("/")
    if "@" in command:
        command = command.split("@", 1)[0]
    return command


def _normalize_url(value: object) -> str:
    url = str(value or "").strip()
    if url.startswith("t.me/"):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https", "tg"} and (parsed.netloc or parsed.scheme == "tg"):
        return url
    return ""


def _normalize_item(raw: object, fallback_id: int) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    command = _normalize_command_name(raw.get("command"))
    if not COMMAND_PATTERN.fullmatch(command) or command in RESERVED_COMMANDS:
        return None
    try:
        item_id = int(raw.get("id", fallback_id) or fallback_id)
    except (TypeError, ValueError):
        item_id = fallback_id
    description = str(raw.get("description") or command).strip()[:256] or command
    mode = "link" if raw.get("mode") == "link" else "text"
    content = str(raw.get("content") or "").strip()[:3500]
    button_text = str(raw.get("button_text") or "点击跳转").strip()[:64] or "点击跳转"
    url = _normalize_url(raw.get("url"))
    if mode == "link" and not url:
        # A broken legacy link entry remains editable, but never produces an
        # invalid inline keyboard.
        mode = "text"
    return {
        "id": max(1, item_id),
        "command": command,
        "description": description,
        "visible": bool(raw.get("visible", True)),
        "mode": mode,
        "content": content,
        "button_text": button_text,
        "url": url,
    }


def load_custom_commands() -> list[dict]:
    """Load and normalize this bot's command definitions."""
    raw = load_json(CUSTOM_COMMANDS_FILE)
    config = raw if isinstance(raw, dict) else {}
    raw_commands = config.get("commands")
    if not isinstance(raw_commands, list):
        raw_commands = []

    commands = []
    seen_names = set()
    seen_ids = set()
    next_id = 1
    for item in raw_commands:
        normalized = _normalize_item(item, next_id)
        if not normalized:
            continue
        if normalized["command"] in seen_names or normalized["id"] in seen_ids:
            continue
        commands.append(normalized)
        seen_names.add(normalized["command"])
        seen_ids.add(normalized["id"])
        next_id = max(next_id, normalized["id"] + 1)

    # Preserve the previously hard-coded /moban command for existing bots on
    # first use, while new definitions are fully configurable afterwards.
    if not commands and not raw_commands:
        commands = [dict(item) for item in DEFAULT_COMMANDS]

    normalized_config = {"commands": commands[:MAX_CUSTOM_COMMANDS]}
    if config != normalized_config:
        save_json(CUSTOM_COMMANDS_FILE, normalized_config)
    return normalized_config["commands"]


def save_custom_commands(commands: list[dict]) -> None:
    normalized = []
    seen_names = set()
    seen_ids = set()
    for index, item in enumerate(commands, start=1):
        normalized_item = _normalize_item(item, index)
        if not normalized_item:
            continue
        if normalized_item["command"] in seen_names or normalized_item["id"] in seen_ids:
            continue
        normalized.append(normalized_item)
        seen_names.add(normalized_item["command"])
        seen_ids.add(normalized_item["id"])
    save_json(CUSTOM_COMMANDS_FILE, {"commands": normalized[:MAX_CUSTOM_COMMANDS]})


def visible_bot_commands() -> list[tuple[str, str]]:
    """Return ``(command, description)`` items for Telegram's command menu."""
    return [
        (item["command"], item["description"])
        for item in load_custom_commands()
        if item.get("visible")
    ]


def visible_reply_labels() -> list[str]:
    """Return short visible descriptions for the optional reply-keyboard menu."""
    labels = []
    for _, description in visible_bot_commands():
        label = description.strip()
        if label and len(label) <= 30 and label not in labels:
            labels.append(label)
    return labels


def _find_by_id(commands: list[dict], item_id: int) -> Optional[dict]:
    return next((item for item in commands if int(item.get("id", 0)) == item_id), None)


def _find_by_command(command: str) -> Optional[dict]:
    normalized = _normalize_command_name(command)
    return next((item for item in load_custom_commands() if item["command"] == normalized), None)


def _find_by_reply_label(text: str) -> Optional[dict]:
    value = str(text or "").strip()
    if not value:
        return None
    return next(
        (
            item
            for item in load_custom_commands()
            if item.get("visible") and item.get("description") == value
        ),
        None,
    )


async def _send_template_response(
    update: Update, context: ContextTypes.DEFAULT_TYPE, template: dict
) -> None:
    message = update.effective_message
    if not message:
        return
    mode = template.get("mode", "text")
    content = str(template.get("content") or "").strip()
    if mode == "link":
        url = _normalize_url(template.get("url"))
        if not url:
            await message.reply_text("❗ 该命令的跳转链接尚未配置。")
            return
        await message.reply_text(
            content or "点击下方按钮打开链接：",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(str(template.get("button_text") or "点击跳转")[:64], url=url)]
            ]),
            disable_web_page_preview=True,
        )
        return
    await message.reply_text(content or "该命令暂未配置输出内容。")


async def handle_custom_template_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle a configured slash command and report whether it was consumed."""
    message = update.effective_message
    if not message or not message.text:
        return False
    first = message.text.strip().split(maxsplit=1)[0]
    if not first.startswith("/"):
        return False
    template = _find_by_command(first)
    if not template:
        return False
    await _send_template_response(update, context, template)
    return True


async def handle_custom_template_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle a visible command's reply-keyboard text label."""
    message = update.effective_message
    if not message or not message.text or message.text.lstrip().startswith("/"):
        return False
    template = _find_by_reply_label(message.text)
    if not template:
        return False
    await _send_template_response(update, context, template)
    return True


async def _refresh_commands(context: ContextTypes.DEFAULT_TYPE) -> None:
    refresher = context.application.bot_data.get("refresh_bot_commands")
    if callable(refresher):
        await refresher(context.application)


def _can_manage(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int]) -> bool:
    return bool(user_id and is_owner_or_super_admin(context, int(user_id)))


def _panel_text(commands: list[dict]) -> str:
    visible = sum(bool(item.get("visible")) for item in commands)
    return (
        "⌨️ 自定义命令模板\n\n"
        f"已添加：{len(commands)} 个；Telegram 命令菜单显示：{visible} 个。\n"
        "每个命令可配置菜单文案、显示/隐藏、文本回复或跳转链接。\n"
        "隐藏后不显示在 Telegram 的 / 命令菜单中，但仍可直接输入 /命令 使用。"
    )


def _panel_keyboard(commands: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ 添加命令", callback_data=f"{CALLBACK_PREFIX}:add")]]
    for item in commands:
        state = "✅" if item.get("visible") else "🚫"
        rows.append([
            InlineKeyboardButton(
                f"{state} /{item['command']} · {item['description'][:28]}",
                callback_data=f"{CALLBACK_PREFIX}:view:{item['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回首页", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _detail_text(item: dict) -> str:
    output_kind = "跳转链接" if item.get("mode") == "link" else "显示文本"
    lines = [
        "⌨️ 命令模板详情",
        f"命令：/{item['command']}",
        f"菜单文案：{item['description']}",
        f"命令菜单：{'✅ 显示' if item.get('visible') else '🚫 隐藏'}",
        f"输出方式：{output_kind}",
        "",
        f"输出内容：\n{item.get('content') or '（未设置）'}",
    ]
    if item.get("mode") == "link":
        lines.extend([
            "",
            f"按钮文案：{item.get('button_text') or '点击跳转'}",
            f"跳转链接：{item.get('url') or '（未设置）'}",
        ])
    return "\n".join(lines)


def _detail_keyboard(item: dict) -> InlineKeyboardMarkup:
    item_id = int(item["id"])
    visible_label = "🚫 隐藏命令" if item.get("visible") else "✅ 显示命令"
    rows = [
        [InlineKeyboardButton(visible_label, callback_data=f"{CALLBACK_PREFIX}:toggle:{item_id}")],
        [
            InlineKeyboardButton("✏️ 命令名称", callback_data=f"{CALLBACK_PREFIX}:edit:command:{item_id}"),
            InlineKeyboardButton("📝 菜单文案", callback_data=f"{CALLBACK_PREFIX}:edit:description:{item_id}"),
        ],
        [
            InlineKeyboardButton("🔀 输出方式", callback_data=f"{CALLBACK_PREFIX}:mode:{item_id}"),
            InlineKeyboardButton("📄 输出内容", callback_data=f"{CALLBACK_PREFIX}:edit:content:{item_id}"),
        ],
    ]
    if item.get("mode") == "link":
        rows.append([
            InlineKeyboardButton("🔘 按钮文案", callback_data=f"{CALLBACK_PREFIX}:edit:button_text:{item_id}"),
            InlineKeyboardButton("🔗 跳转链接", callback_data=f"{CALLBACK_PREFIX}:edit:url:{item_id}"),
        ])
    rows.extend([
        [InlineKeyboardButton("🗑 删除命令", callback_data=f"{CALLBACK_PREFIX}:delete:{item_id}")],
        [InlineKeyboardButton("⬅️ 返回命令列表", callback_data=f"{CALLBACK_PREFIX}:menu")],
    ])
    return InlineKeyboardMarkup(rows)


def _mode_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 显示文本", callback_data=f"{CALLBACK_PREFIX}:{prefix}:text")],
        [InlineKeyboardButton("🔗 跳转链接", callback_data=f"{CALLBACK_PREFIX}:{prefix}:link")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:menu")],
    ])


async def open_custom_command_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _can_manage(context, user_id):
        if update.message:
            await update.message.reply_text("❌ 只有机器人所有者或超级管理员可以管理命令模板。")
        return
    if update.effective_chat and update.effective_chat.type != "private":
        if update.message:
            await update.message.reply_text("请在私聊中管理命令模板。")
        return
    commands = load_custom_commands()
    if update.message:
        await update.message.reply_text(_panel_text(commands), reply_markup=_panel_keyboard(commands))


async def _open_panel_from_query(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    commands = load_custom_commands()
    await query.edit_message_text(_panel_text(commands), reply_markup=_panel_keyboard(commands))


async def _open_detail_from_query(query, context: ContextTypes.DEFAULT_TYPE, item_id: int) -> None:
    item = _find_by_id(load_custom_commands(), item_id)
    if not item:
        await query.answer("命令不存在或已删除。", show_alert=True)
        return
    await query.edit_message_text(_detail_text(item), reply_markup=_detail_keyboard(item))


def _next_id(commands: list[dict]) -> int:
    return max((int(item.get("id", 0) or 0) for item in commands), default=0) + 1


async def _finish_add(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    draft = context.user_data.get(DRAFT_KEY)
    if not isinstance(draft, dict):
        return
    commands = load_custom_commands()
    commands.append({
        "id": int(draft["id"]),
        "command": draft["command"],
        "description": draft["description"],
        "visible": True,
        "mode": draft["mode"],
        "content": draft["content"],
        "button_text": draft.get("button_text", "点击跳转"),
        "url": draft.get("url", ""),
    })
    save_custom_commands(commands)
    context.user_data.pop(DRAFT_KEY, None)
    await _refresh_commands(context)
    await message.reply_text(
        "✅ 命令已添加，Telegram 命令菜单已刷新。\n\n" + _panel_text(load_custom_commands()),
        reply_markup=_panel_keyboard(load_custom_commands()),
    )


async def custom_command_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get(DRAFT_KEY)
    if not isinstance(draft, dict):
        return
    if not update.effective_user or not _can_manage(context, update.effective_user.id):
        context.user_data.pop(DRAFT_KEY, None)
        return
    message = update.message
    if not message:
        raise ApplicationHandlerStop
    if not message.text:
        await message.reply_text("请发送文字内容，或发送“取消”。")
        raise ApplicationHandlerStop

    text = message.text.strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(DRAFT_KEY, None)
        await message.reply_text("已取消命令模板编辑。", reply_markup=_panel_keyboard(load_custom_commands()))
        raise ApplicationHandlerStop

    step = str(draft.get("step") or "")
    commands = load_custom_commands()
    if step in {"add_command", "edit_command"}:
        command = _normalize_command_name(text)
        item_id = int(draft.get("id", 0) or 0)
        if not COMMAND_PATTERN.fullmatch(command):
            await message.reply_text("❗ 命令只能使用 1-32 位小写英文、数字和下划线，且必须以英文开头，例如：moban。")
            raise ApplicationHandlerStop
        if command in RESERVED_COMMANDS:
            await message.reply_text("❗ 该命令为系统保留命令，请换一个名称。")
            raise ApplicationHandlerStop
        if any(item["command"] == command and int(item["id"]) != item_id for item in commands):
            await message.reply_text("❗ 该命令已存在，请换一个名称。")
            raise ApplicationHandlerStop
        if step == "add_command":
            draft["command"] = command
            draft["step"] = "add_description"
            await message.reply_text("请输入 Telegram 命令菜单显示的文案，例如：模版入口。")
        else:
            item = _find_by_id(commands, item_id)
            if item:
                item["command"] = command
                save_custom_commands(commands)
                await _refresh_commands(context)
                await message.reply_text("✅ 命令名称已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop

    if step in {"add_description", "edit_description"}:
        if not text or len(text) > 256:
            await message.reply_text("❗ 菜单文案不能为空，且不能超过 256 个字符。")
            raise ApplicationHandlerStop
        if step == "add_description":
            draft["description"] = text
            draft["step"] = "add_mode"
            await message.reply_text("请选择该命令的输出方式：", reply_markup=_mode_keyboard("add_mode"))
        else:
            item = _find_by_id(commands, int(draft["id"]))
            if item:
                item["description"] = text
                save_custom_commands(commands)
                await _refresh_commands(context)
                await message.reply_text("✅ 菜单文案已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop

    if step in {"add_content", "edit_content", "edit_link_content"}:
        if len(text) > 3500:
            await message.reply_text("❗ 输出内容不能超过 3500 个字符。")
            raise ApplicationHandlerStop
        content = "" if text in {"-", "无"} else message.text
        if step == "add_content":
            draft["content"] = content
            if draft.get("mode") == "link":
                draft["step"] = "add_button_text"
                await message.reply_text("请输入跳转按钮文案，例如：立即查看。")
            else:
                await _finish_add(context, message)
        elif step == "edit_link_content":
            draft["content"] = content
            draft["step"] = "edit_link_button_text"
            await message.reply_text("请输入跳转按钮文案，例如：立即查看。")
        else:
            item = _find_by_id(commands, int(draft["id"]))
            if item:
                item["content"] = content
                save_custom_commands(commands)
                await message.reply_text("✅ 输出内容已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop

    if step in {"add_button_text", "edit_button_text", "edit_link_button_text"}:
        if not text or len(text) > 64:
            await message.reply_text("❗ 按钮文案不能为空，且不能超过 64 个字符。")
            raise ApplicationHandlerStop
        if step == "add_button_text":
            draft["button_text"] = text
            draft["step"] = "add_url"
            await message.reply_text("请输入跳转链接，支持 https://、http://、tg:// 或 t.me/ 开头。")
        elif step == "edit_link_button_text":
            draft["button_text"] = text
            draft["step"] = "edit_link_url"
            await message.reply_text("请输入跳转链接，支持 https://、http://、tg:// 或 t.me/ 开头。")
        else:
            item = _find_by_id(commands, int(draft["id"]))
            if item:
                item["button_text"] = text
                save_custom_commands(commands)
                await message.reply_text("✅ 按钮文案已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop

    if step in {"add_url", "edit_url", "edit_link_url"}:
        url = _normalize_url(text)
        if not url:
            await message.reply_text("❗ 链接格式不正确，请输入完整链接，例如：https://t.me/example")
            raise ApplicationHandlerStop
        if step == "add_url":
            draft["url"] = url
            await _finish_add(context, message)
        elif step == "edit_link_url":
            item = _find_by_id(commands, int(draft["id"]))
            if item:
                item["mode"] = "link"
                item["content"] = draft.get("content", "")
                item["button_text"] = draft.get("button_text", "点击跳转")
                item["url"] = url
                save_custom_commands(commands)
                await message.reply_text("✅ 已切换为跳转链接输出。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        else:
            item = _find_by_id(commands, int(draft["id"]))
            if item:
                item["url"] = url
                item["mode"] = "link"
                save_custom_commands(commands)
                await message.reply_text("✅ 跳转链接已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop


async def custom_command_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer("请在私聊中操作。", show_alert=True)
        return
    if not _can_manage(context, query.from_user.id if query.from_user else None):
        await query.answer("只有机器人所有者或超级管理员可以管理命令模板。", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        context.user_data.pop(DRAFT_KEY, None)
        await query.answer()
        await _open_panel_from_query(query, context)
        return

    if action == "add":
        commands = load_custom_commands()
        if len(commands) >= MAX_CUSTOM_COMMANDS:
            await query.answer(f"最多只能添加 {MAX_CUSTOM_COMMANDS} 个命令。", show_alert=True)
            return
        context.user_data[DRAFT_KEY] = {"id": _next_id(commands), "step": "add_command"}
        await query.answer()
        await query.edit_message_text("请输入命令名称（不含 /），例如：moban。\n只能使用小写英文、数字和下划线。")
        return

    if action == "view" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("命令数据无效。", show_alert=True)
            return
        await query.answer()
        await _open_detail_from_query(query, context, item_id)
        return

    if action == "toggle" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("命令数据无效。", show_alert=True)
            return
        commands = load_custom_commands()
        item = _find_by_id(commands, item_id)
        if not item:
            await query.answer("命令不存在。", show_alert=True)
            return
        item["visible"] = not bool(item.get("visible"))
        save_custom_commands(commands)
        await _refresh_commands(context)
        await query.answer("✅ 命令菜单已刷新")
        await query.edit_message_text(_detail_text(item), reply_markup=_detail_keyboard(item))
        return

    if action == "delete" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("命令数据无效。", show_alert=True)
            return
        commands = load_custom_commands()
        if not _find_by_id(commands, item_id):
            await query.answer("命令不存在。", show_alert=True)
            return
        save_custom_commands([item for item in commands if int(item["id"]) != item_id])
        await _refresh_commands(context)
        await query.answer("✅ 命令已删除")
        await _open_panel_from_query(query, context)
        return

    if action == "edit" and len(parts) >= 4:
        field, raw_id = parts[2], parts[3]
        try:
            item_id = int(raw_id)
        except ValueError:
            await query.answer("命令数据无效。", show_alert=True)
            return
        item = _find_by_id(load_custom_commands(), item_id)
        prompts = {
            "command": "请输入新的命令名称（不含 /）。",
            "description": "请输入新的 Telegram 命令菜单文案。",
            "content": "请输入新的输出内容。发送“-”或“无”可清空内容。",
            "button_text": "请输入新的跳转按钮文案。",
            "url": "请输入新的跳转链接。",
        }
        if not item or field not in prompts:
            await query.answer("命令或编辑项目无效。", show_alert=True)
            return
        context.user_data[DRAFT_KEY] = {"id": item_id, "step": f"edit_{field}"}
        await query.answer()
        await query.edit_message_text(prompts[field])
        return

    if action == "mode" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("命令数据无效。", show_alert=True)
            return
        if not _find_by_id(load_custom_commands(), item_id):
            await query.answer("命令不存在。", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text("请选择输出方式：", reply_markup=_mode_keyboard(f"set_mode:{item_id}"))
        return

    if action == "add_mode" and len(parts) >= 3:
        mode = parts[2]
        draft = context.user_data.get(DRAFT_KEY)
        if not isinstance(draft, dict) or draft.get("step") != "add_mode" or mode not in {"text", "link"}:
            await query.answer("添加状态已失效，请重新添加。", show_alert=True)
            return
        draft["mode"] = mode
        draft["step"] = "add_content"
        await query.answer()
        hint = "可发送“-”跳过文字，仅显示跳转按钮。" if mode == "link" else ""
        await query.edit_message_text(f"请输入命令触发后显示的内容。{hint}")
        return

    if action == "set_mode" and len(parts) >= 4:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("命令数据无效。", show_alert=True)
            return
        mode = parts[3]
        commands = load_custom_commands()
        item = _find_by_id(commands, item_id)
        if not item or mode not in {"text", "link"}:
            await query.answer("命令或输出方式无效。", show_alert=True)
            return
        if mode == "text":
            item["mode"] = "text"
            save_custom_commands(commands)
            await query.answer("请选择要显示的文本内容")
            context.user_data[DRAFT_KEY] = {"id": item_id, "step": "edit_content"}
            await query.edit_message_text("请输入文本输出内容。")
            return
        # Do not persist link mode until the URL is received; normalization
        # intentionally rejects a link definition without a valid URL.
        context.user_data[DRAFT_KEY] = {"id": item_id, "step": "edit_link_content"}
        await query.answer()
        await query.edit_message_text("请输入链接模式下显示的提示内容。发送“-”可只显示按钮。")
        return

    await query.answer("未知操作。", show_alert=True)


async def _slash_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_custom_template_command(update, context):
        raise ApplicationHandlerStop


def register_custom_command_handlers(app) -> None:
    # Group -25 runs before normal CommandHandler routes and before private
    # forwarding. Unknown commands are intentionally allowed to continue.
    app.add_handler(MessageHandler(filters.COMMAND, _slash_command_handler), group=-25)
    app.add_handler(CallbackQueryHandler(custom_command_callback_handler, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(CommandHandler("command_templates", open_custom_command_panel))
    app.add_handler(CommandHandler("command_manage", open_custom_command_panel))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (~filters.COMMAND),
            custom_command_input_handler,
        ),
        group=-25,
    )
