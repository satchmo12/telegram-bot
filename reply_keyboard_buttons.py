# -*- coding: utf-8 -*-
"""Owner-managed reply-keyboard buttons.

Each bot stores its own keyboard definitions in
``config_data/reply_keyboard_buttons.json``.  Buttons appear in the optional
bottom ReplyKeyboard shown after /start and can reply with text or an inline
URL button, using the same editing flow as custom command templates.
"""
from __future__ import annotations

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

REPLY_KEYBOARD_BUTTONS_FILE = "config_data/reply_keyboard_buttons.json"
CALLBACK_PREFIX = "rkb"
DRAFT_KEY = "reply_keyboard_button_draft"

MAX_REPLY_KEYBOARD_BUTTONS = 40
MAX_LABEL_LENGTH = 64


def _normalize_url(value: object) -> str:
    url = str(value or "").strip()
    if url.startswith("t.me/"):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https", "tg"} and (parsed.netloc or parsed.scheme == "tg"):
        return url
    return ""


def _normalize_label(value: object) -> str:
    """Normalize a ReplyKeyboard label and reject slash-command labels."""
    label = " ".join(str(value or "").strip().split())[:MAX_LABEL_LENGTH]
    return "" if not label or label.startswith("/") else label


def _normalize_item(raw: object, fallback_id: int) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    label = _normalize_label(raw.get("label"))
    if not label:
        return None
    try:
        item_id = int(raw.get("id", fallback_id) or fallback_id)
    except (TypeError, ValueError):
        item_id = fallback_id
    mode = "link" if raw.get("mode") == "link" else "text"
    content = str(raw.get("content") or "").strip()[:3500]
    button_text = str(raw.get("button_text") or "点击跳转").strip()[:64] or "点击跳转"
    url = _normalize_url(raw.get("url"))
    # Keep invalid legacy definitions editable but never emit an invalid URL button.
    if mode == "link" and not url:
        mode = "text"
    return {
        "id": max(1, item_id),
        "label": label,
        "visible": bool(raw.get("visible", True)),
        "mode": mode,
        "content": content,
        "button_text": button_text,
        "url": url,
    }


def load_reply_keyboard_buttons() -> list[dict]:
    raw = load_json(REPLY_KEYBOARD_BUTTONS_FILE)
    config = raw if isinstance(raw, dict) else {}
    raw_buttons = config.get("buttons")
    if not isinstance(raw_buttons, list):
        raw_buttons = []

    buttons = []
    seen_ids = set()
    seen_labels = set()
    next_id = 1
    for item in raw_buttons:
        normalized = _normalize_item(item, next_id)
        if not normalized:
            continue
        label_key = normalized["label"].casefold()
        if normalized["id"] in seen_ids or label_key in seen_labels:
            continue
        buttons.append(normalized)
        seen_ids.add(normalized["id"])
        seen_labels.add(label_key)
        next_id = max(next_id, normalized["id"] + 1)

    normalized_config = {"buttons": buttons[:MAX_REPLY_KEYBOARD_BUTTONS]}
    if config != normalized_config:
        save_json(REPLY_KEYBOARD_BUTTONS_FILE, normalized_config)
    return normalized_config["buttons"]


def save_reply_keyboard_buttons(buttons: list[dict]) -> None:
    normalized = []
    seen_ids = set()
    seen_labels = set()
    for index, item in enumerate(buttons, start=1):
        normalized_item = _normalize_item(item, index)
        if not normalized_item:
            continue
        label_key = normalized_item["label"].casefold()
        if normalized_item["id"] in seen_ids or label_key in seen_labels:
            continue
        normalized.append(normalized_item)
        seen_ids.add(normalized_item["id"])
        seen_labels.add(label_key)
    save_json(REPLY_KEYBOARD_BUTTONS_FILE, {"buttons": normalized[:MAX_REPLY_KEYBOARD_BUTTONS]})


def visible_reply_keyboard_labels() -> list[str]:
    """Labels to append to the /start ReplyKeyboard, in configured order."""
    return [item["label"] for item in load_reply_keyboard_buttons() if item.get("visible")]


def _find_by_id(buttons: list[dict], item_id: int) -> Optional[dict]:
    return next((item for item in buttons if int(item.get("id", 0)) == item_id), None)


def _find_by_label(text: str) -> Optional[dict]:
    value = str(text or "").strip()
    if not value:
        return None
    return next(
        (
            item
            for item in load_reply_keyboard_buttons()
            if item.get("visible") and item.get("label") == value
        ),
        None,
    )


async def _send_button_response(
    update: Update, context: ContextTypes.DEFAULT_TYPE, button: dict
) -> None:
    message = update.effective_message
    if not message:
        return
    content = str(button.get("content") or "").strip()
    if button.get("mode") == "link":
        url = _normalize_url(button.get("url"))
        if not url:
            await message.reply_text("❗ 该键盘按钮的跳转链接尚未配置。")
            return
        await message.reply_text(
            content or "点击下方按钮打开链接：",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(str(button.get("button_text") or "点击跳转")[:64], url=url)]]
            ),
            disable_web_page_preview=True,
        )
        return
    await message.reply_text(content or "该键盘按钮暂未配置回复内容。")


async def handle_reply_keyboard_button_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Consume text sent by one of this bot's visible ReplyKeyboard buttons."""
    message = update.effective_message
    if not message or not message.text or message.text.lstrip().startswith("/"):
        return False
    button = _find_by_label(message.text)
    if not button:
        return False
    await _send_button_response(update, context, button)
    return True


def _can_manage(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int]) -> bool:
    return bool(user_id and is_owner_or_super_admin(context, int(user_id)))


def _panel_text(buttons: list[dict]) -> str:
    visible = sum(bool(item.get("visible")) for item in buttons)
    return (
        "⌨️ 键盘按钮设置\n\n"
        f"已添加：{len(buttons)} 个；/start 底部键盘显示：{visible} 个。\n"
        "每个按钮可配置显示/隐藏、文本回复或跳转链接。\n"
        "需先在主面板开启“/start 弹出键盘”，用户才能看到这些按钮。"
    )


def _panel_keyboard(buttons: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ 添加键盘按钮", callback_data=f"{CALLBACK_PREFIX}:add")]]
    for item in buttons:
        state = "✅" if item.get("visible") else "🚫"
        rows.append([
            InlineKeyboardButton(
                f"{state} {item['label'][:42]}",
                callback_data=f"{CALLBACK_PREFIX}:view:{item['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回首页", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _detail_text(item: dict) -> str:
    output_kind = "跳转链接" if item.get("mode") == "link" else "显示文本"
    lines = [
        "⌨️ 键盘按钮详情",
        f"按钮文案：{item['label']}",
        f"底部键盘：{'✅ 显示' if item.get('visible') else '🚫 隐藏'}",
        f"输出方式：{output_kind}",
        f"回复内容：{item.get('content') or '（未设置）'}",
    ]
    if item.get("mode") == "link":
        lines.extend([
            f"跳转按钮：{item.get('button_text') or '点击跳转'}",
            f"链接：{item.get('url') or '（未设置）'}",
        ])
    return "\n".join(lines)


def _detail_keyboard(item: dict) -> InlineKeyboardMarkup:
    item_id = int(item["id"])
    visible_text = "🚫 从底部键盘隐藏" if item.get("visible") else "✅ 在底部键盘显示"
    rows = [
        [InlineKeyboardButton("✏️ 修改按钮文案", callback_data=f"{CALLBACK_PREFIX}:edit:label:{item_id}")],
        [InlineKeyboardButton(visible_text, callback_data=f"{CALLBACK_PREFIX}:toggle:{item_id}")],
        [InlineKeyboardButton("🔁 修改输出方式", callback_data=f"{CALLBACK_PREFIX}:mode:{item_id}")],
        [InlineKeyboardButton("📝 修改回复内容", callback_data=f"{CALLBACK_PREFIX}:edit:content:{item_id}")],
    ]
    if item.get("mode") == "link":
        rows.extend([
            [InlineKeyboardButton("🔘 修改跳转按钮文案", callback_data=f"{CALLBACK_PREFIX}:edit:button_text:{item_id}")],
            [InlineKeyboardButton("🔗 修改跳转链接", callback_data=f"{CALLBACK_PREFIX}:edit:url:{item_id}")],
        ])
    rows.extend([
        [InlineKeyboardButton("🗑 删除按钮", callback_data=f"{CALLBACK_PREFIX}:delete:{item_id}")],
        [InlineKeyboardButton("⬅️ 返回按钮列表", callback_data=f"{CALLBACK_PREFIX}:menu")],
    ])
    return InlineKeyboardMarkup(rows)


def _mode_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 显示文本", callback_data=f"{CALLBACK_PREFIX}:{prefix}:text")],
        [InlineKeyboardButton("🔗 跳转链接", callback_data=f"{CALLBACK_PREFIX}:{prefix}:link")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"{CALLBACK_PREFIX}:menu")],
    ])


async def open_reply_keyboard_button_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _can_manage(context, user_id):
        if update.message:
            await update.message.reply_text("❌ 只有机器人所有者或超级管理员可以管理键盘按钮。")
        return
    if update.effective_chat and update.effective_chat.type != "private":
        if update.message:
            await update.message.reply_text("请在私聊中管理键盘按钮。")
        return
    buttons = load_reply_keyboard_buttons()
    if update.message:
        await update.message.reply_text(_panel_text(buttons), reply_markup=_panel_keyboard(buttons))


async def _open_panel_from_query(query) -> None:
    buttons = load_reply_keyboard_buttons()
    await query.edit_message_text(_panel_text(buttons), reply_markup=_panel_keyboard(buttons))


async def _open_detail_from_query(query, item_id: int) -> None:
    item = _find_by_id(load_reply_keyboard_buttons(), item_id)
    if not item:
        await query.answer("键盘按钮不存在或已删除。", show_alert=True)
        return
    await query.edit_message_text(_detail_text(item), reply_markup=_detail_keyboard(item))


def _next_id(buttons: list[dict]) -> int:
    return max((int(item.get("id", 0) or 0) for item in buttons), default=0) + 1


async def _finish_add(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    draft = context.user_data.get(DRAFT_KEY)
    if not isinstance(draft, dict):
        return
    buttons = load_reply_keyboard_buttons()
    buttons.append({
        "id": int(draft["id"]),
        "label": draft["label"],
        "visible": True,
        "mode": draft["mode"],
        "content": draft["content"],
        "button_text": draft.get("button_text", "点击跳转"),
        "url": draft.get("url", ""),
    })
    save_reply_keyboard_buttons(buttons)
    context.user_data.pop(DRAFT_KEY, None)
    await message.reply_text(
        "✅ 键盘按钮已添加。用户下次 /start 打开底部键盘时即可看到。\n\n"
        + _panel_text(load_reply_keyboard_buttons()),
        reply_markup=_panel_keyboard(load_reply_keyboard_buttons()),
    )


async def reply_keyboard_button_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await message.reply_text("已取消键盘按钮编辑。", reply_markup=_panel_keyboard(load_reply_keyboard_buttons()))
        raise ApplicationHandlerStop

    step = str(draft.get("step") or "")
    buttons = load_reply_keyboard_buttons()
    if step in {"add_label", "edit_label"}:
        label = _normalize_label(text)
        item_id = int(draft.get("id", 0) or 0)
        if not label:
            await message.reply_text("❗ 按钮文案不能为空、不能以 / 开头，且不能超过 64 个字符。")
            raise ApplicationHandlerStop
        if any(item["label"].casefold() == label.casefold() and int(item["id"]) != item_id for item in buttons):
            await message.reply_text("❗ 该按钮文案已存在，请换一个。")
            raise ApplicationHandlerStop
        if step == "add_label":
            draft["label"] = label
            draft["step"] = "add_mode"
            await message.reply_text("请选择点击该按钮后的输出方式：", reply_markup=_mode_keyboard("add_mode"))
        else:
            item = _find_by_id(buttons, item_id)
            if item:
                item["label"] = label
                save_reply_keyboard_buttons(buttons)
                await message.reply_text("✅ 按钮文案已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop

    if step in {"add_content", "edit_content", "edit_link_content"}:
        content = "" if text in {"-", "无"} else text[:3500]
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
            item = _find_by_id(buttons, int(draft["id"]))
            if item:
                item["content"] = content
                save_reply_keyboard_buttons(buttons)
                await message.reply_text("✅ 回复内容已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop

    if step in {"add_button_text", "edit_button_text", "edit_link_button_text"}:
        if not text or len(text) > 64:
            await message.reply_text("❗ 跳转按钮文案不能为空，且不能超过 64 个字符。")
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
            item = _find_by_id(buttons, int(draft["id"]))
            if item:
                item["button_text"] = text
                save_reply_keyboard_buttons(buttons)
                await message.reply_text("✅ 跳转按钮文案已更新。", reply_markup=_detail_keyboard(item))
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
            item = _find_by_id(buttons, int(draft["id"]))
            if item:
                item["mode"] = "link"
                item["content"] = draft.get("content", "")
                item["button_text"] = draft.get("button_text", "点击跳转")
                item["url"] = url
                save_reply_keyboard_buttons(buttons)
                await message.reply_text("✅ 已切换为跳转链接输出。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        else:
            item = _find_by_id(buttons, int(draft["id"]))
            if item:
                item["url"] = url
                item["mode"] = "link"
                save_reply_keyboard_buttons(buttons)
                await message.reply_text("✅ 跳转链接已更新。", reply_markup=_detail_keyboard(item))
            context.user_data.pop(DRAFT_KEY, None)
        raise ApplicationHandlerStop


async def reply_keyboard_button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer("请在私聊中操作。", show_alert=True)
        return
    if not _can_manage(context, query.from_user.id if query.from_user else None):
        await query.answer("只有机器人所有者或超级管理员可以管理键盘按钮。", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        context.user_data.pop(DRAFT_KEY, None)
        await query.answer()
        await _open_panel_from_query(query)
        return

    if action == "add":
        buttons = load_reply_keyboard_buttons()
        if len(buttons) >= MAX_REPLY_KEYBOARD_BUTTONS:
            await query.answer(f"最多只能添加 {MAX_REPLY_KEYBOARD_BUTTONS} 个键盘按钮。", show_alert=True)
            return
        context.user_data[DRAFT_KEY] = {"id": _next_id(buttons), "step": "add_label"}
        await query.answer()
        await query.edit_message_text("请输入底部键盘显示的按钮文案，例如：联系客服。")
        return

    if action == "view" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("键盘按钮数据无效。", show_alert=True)
            return
        await query.answer()
        await _open_detail_from_query(query, item_id)
        return

    if action == "toggle" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("键盘按钮数据无效。", show_alert=True)
            return
        buttons = load_reply_keyboard_buttons()
        item = _find_by_id(buttons, item_id)
        if not item:
            await query.answer("键盘按钮不存在。", show_alert=True)
            return
        item["visible"] = not bool(item.get("visible"))
        save_reply_keyboard_buttons(buttons)
        await query.answer("✅ 底部键盘设置已更新")
        await query.edit_message_text(_detail_text(item), reply_markup=_detail_keyboard(item))
        return

    if action == "delete" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("键盘按钮数据无效。", show_alert=True)
            return
        buttons = load_reply_keyboard_buttons()
        if not _find_by_id(buttons, item_id):
            await query.answer("键盘按钮不存在。", show_alert=True)
            return
        save_reply_keyboard_buttons([item for item in buttons if int(item["id"]) != item_id])
        await query.answer("✅ 键盘按钮已删除")
        await _open_panel_from_query(query)
        return

    if action == "edit" and len(parts) >= 4:
        field, raw_id = parts[2], parts[3]
        try:
            item_id = int(raw_id)
        except ValueError:
            await query.answer("键盘按钮数据无效。", show_alert=True)
            return
        item = _find_by_id(load_reply_keyboard_buttons(), item_id)
        prompts = {
            "label": "请输入新的底部键盘按钮文案。",
            "content": "请输入新的回复内容。发送“-”或“无”可清空内容。",
            "button_text": "请输入新的跳转按钮文案。",
            "url": "请输入新的跳转链接。",
        }
        if not item or field not in prompts:
            await query.answer("键盘按钮或编辑项目无效。", show_alert=True)
            return
        context.user_data[DRAFT_KEY] = {"id": item_id, "step": f"edit_{field}"}
        await query.answer()
        await query.edit_message_text(prompts[field])
        return

    if action == "mode" and len(parts) >= 3:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("键盘按钮数据无效。", show_alert=True)
            return
        if not _find_by_id(load_reply_keyboard_buttons(), item_id):
            await query.answer("键盘按钮不存在。", show_alert=True)
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
        await query.edit_message_text(f"请输入点击按钮后显示的内容。{hint}")
        return

    if action == "set_mode" and len(parts) >= 4:
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("键盘按钮数据无效。", show_alert=True)
            return
        mode = parts[3]
        buttons = load_reply_keyboard_buttons()
        item = _find_by_id(buttons, item_id)
        if not item or mode not in {"text", "link"}:
            await query.answer("键盘按钮或输出方式无效。", show_alert=True)
            return
        if mode == "text":
            item["mode"] = "text"
            save_reply_keyboard_buttons(buttons)
            context.user_data[DRAFT_KEY] = {"id": item_id, "step": "edit_content"}
            await query.answer("请设置文本回复内容")
            await query.edit_message_text("请输入文本回复内容。")
            return
        # Do not persist link mode until a valid URL is supplied.
        context.user_data[DRAFT_KEY] = {"id": item_id, "step": "edit_link_content"}
        await query.answer()
        await query.edit_message_text("请输入链接模式下显示的提示内容。发送“-”可只显示按钮。")
        return

    await query.answer("未知操作。", show_alert=True)


def register_reply_keyboard_button_handlers(app) -> None:
    app.add_handler(CallbackQueryHandler(reply_keyboard_button_callback_handler, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(CommandHandler("keyboard_buttons", open_reply_keyboard_button_panel))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (~filters.COMMAND),
            reply_keyboard_button_input_handler,
        ),
        group=-24,
    )
