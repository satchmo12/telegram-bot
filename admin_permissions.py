# -*- coding: utf-8 -*-
"""Per-bot delegated administrator management and permission checks."""
from __future__ import annotations

import html
from typing import Iterable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from utils import get_bot_path, is_super_admin, load_json, save_json

ADMIN_PERMISSIONS_FILE = "config_data/admin_permissions.json"
CALLBACK_PREFIX = "adm"
ADD_STAGE_KEY = "delegated_admin_add_stage"
RENAME_STAGE_KEY = "delegated_admin_rename_stage"

# Keep these stable: values are persisted in admin_permissions.json.
PERMISSION_OPTIONS = (
    ("submission_review", "📝 稿件审核", "审核、通过或拒绝投稿"),
    ("submission_config", "⚙️ 投稿配置", "管理投稿开关、频道、广告和按钮"),
    ("private_forward", "💬 私聊转发", "处理用户私聊、回复、拉黑和导出"),
    ("group_config", "👥 群配置", "打开并调整群配置"),
    ("global_ad_config", "📢 全群广告推送", "设置全群广告推送任务"),
    ("channel_config", "📣 频道配置", "管理克隆频道与频道规则"),
    ("telethon_manage", "📱 协议号管理", "管理协议号、群发和小号群组"),
    ("bot_channel_config", "🤖 机器人频道配置", "管理机器人频道转发规则"),
)
PERMISSION_LABELS = {key: label for key, label, _ in PERMISSION_OPTIONS}
ALL_PERMISSIONS = frozenset(PERMISSION_LABELS)


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    try:
        return int(context.application.bot_data.get("owner_id"))
    except (TypeError, ValueError, AttributeError):
        return None


def is_owner_or_super_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid == _owner_id(context) or is_super_admin(uid)


def _config_path(context: ContextTypes.DEFAULT_TYPE) -> str:
    return get_bot_path(context, ADMIN_PERMISSIONS_FILE)


def _load(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = load_json(_config_path(context))
    if not isinstance(data, dict):
        data = {}
    admins = data.get("admins")
    if not isinstance(admins, dict):
        admins = {}
    data["admins"] = admins

    # Compatibility with the first multi-admin release: the old “频道配置”
    # permission included protocol-account management and bot channel rules,
    # while “群配置” included the global-ad panel. Preserve that access once,
    # then let the owner disable each new item independently in the panel.
    changed = False
    for record in admins.values():
        if not isinstance(record, dict):
            continue
        permissions = list(dict.fromkeys(record.get("permissions") or []))
        if "channel_config" in permissions:
            for permission in ("telethon_manage", "bot_channel_config"):
                if permission not in permissions:
                    permissions.append(permission)
                    changed = True
        if "group_config" in permissions and "global_ad_config" not in permissions:
            permissions.append("global_ad_config")
            changed = True
        if permissions != record.get("permissions"):
            record["permissions"] = permissions
            changed = True
    if changed:
        save_json(_config_path(context), data)
    return data


def _save(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    save_json(_config_path(context), data)


def get_delegated_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Optional[dict]:
    try:
        record = _load(context)["admins"].get(str(int(user_id)))
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def get_delegated_admin_ids(
    context: ContextTypes.DEFAULT_TYPE, permission: Optional[str] = None
) -> list[int]:
    result: list[int] = []
    for raw_id, record in _load(context)["admins"].items():
        if not isinstance(record, dict):
            continue
        permissions = set(record.get("permissions") or [])
        if permission and permission not in permissions:
            continue
        try:
            result.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    return result


def has_admin_permission(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, permission: str
) -> bool:
    """Owner/super-admin retain all permissions; delegated admins are explicit."""
    if permission not in ALL_PERMISSIONS:
        return False
    if is_owner_or_super_admin(context, user_id):
        return True
    record = get_delegated_admin(context, user_id)
    return bool(record and permission in set(record.get("permissions") or []))


def _display_admin(raw_id: str, record: dict) -> str:
    name = str(record.get("name") or "").strip()
    username = str(record.get("username") or "").strip().lstrip("@")
    identity = name or (f"@{username}" if username else "未命名")
    return f"{identity} · {raw_id}"


def _admin_list_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    data = _load(context)
    admins = data["admins"]
    owner_id = _owner_id(context)
    lines = [
        "👥 <b>多管理员</b>",
        "",
        "机器人所有者和超级管理员始终拥有全部权限。",
        "以下管理员仅能使用你为其开启的功能。",
        "",
        f"机器人所有者：<code>{owner_id}</code>" if owner_id else "机器人所有者：未配置",
        "",
    ]
    if not admins:
        lines.append("当前还没有授权管理员。")
    else:
        lines.append(f"已授权管理员：{len(admins)} 名")
        for raw_id, record in admins.items():
            if not isinstance(record, dict):
                continue
            labels = [PERMISSION_LABELS[p] for p in record.get("permissions", []) if p in PERMISSION_LABELS]
            lines.append(
                f"• {html.escape(_display_admin(raw_id, record))}\n  权限：{'、'.join(labels) if labels else '未开启'}"
            )
    return "\n".join(lines)


def _admin_list_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ 增加管理员", callback_data=f"{CALLBACK_PREFIX}:add")]]
    for raw_id, record in _load(context)["admins"].items():
        if not isinstance(record, dict):
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    f"✏️ {_display_admin(raw_id, record)[:50]}",
                    callback_data=f"{CALLBACK_PREFIX}:select:{raw_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _admin_detail_text(raw_id: str, record: dict) -> str:
    labels = [PERMISSION_LABELS[p] for p in record.get("permissions", []) if p in PERMISSION_LABELS]
    return "\n".join(
        [
            "👤 <b>管理员权限</b>",
            "",
            f"管理员：{html.escape(_display_admin(raw_id, record))}",
            f"用户 ID：<code>{raw_id}</code>",
            "",
            "已启用：" + ("、".join(labels) if labels else "无"),
            "",
            "点击下方项目可开关权限。",
        ]
    )


def _admin_detail_keyboard(raw_id: str, record: dict) -> InlineKeyboardMarkup:
    enabled = set(record.get("permissions") or [])
    rows = [[InlineKeyboardButton("✏️ 修改显示名称", callback_data=f"{CALLBACK_PREFIX}:rename:{raw_id}")]]
    for key, label, _ in PERMISSION_OPTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if key in enabled else '⬜'} {label}",
                    callback_data=f"{CALLBACK_PREFIX}:toggle:{raw_id}:{key}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton("🗑 删除管理员", callback_data=f"{CALLBACK_PREFIX}:delete_confirm:{raw_id}")],
            [InlineKeyboardButton("⬅️ 返回列表", callback_data=f"{CALLBACK_PREFIX}:panel")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _show_panel(query, context: ContextTypes.DEFAULT_TYPE):
    return await query.edit_message_text(
        _admin_list_text(context),
        reply_markup=_admin_list_keyboard(context),
        parse_mode="HTML",
    )


async def delegated_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return
    user = update.effective_user
    if not user or not is_owner_or_super_admin(context, user.id):
        return await query.answer("仅机器人所有者或超级管理员可管理多管理员。", show_alert=True)
    if not update.effective_chat or update.effective_chat.type != "private":
        return await query.answer("请在私聊中管理多管理员。", show_alert=True)

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    await query.answer()

    if action == "panel":
        context.user_data.pop(ADD_STAGE_KEY, None)
        context.user_data.pop(RENAME_STAGE_KEY, None)
        return await _show_panel(query, context)
    if action == "add":
        context.user_data.pop(RENAME_STAGE_KEY, None)
        context.user_data[ADD_STAGE_KEY] = True
        return await query.edit_message_text(
            "➕ <b>增加管理员</b>\n\n请发送对方的 Telegram 数字用户 ID。\n"
            "也可以转发对方发送给机器人的一条消息。\n\n"
            "添加后，请在权限页面逐项开启可用功能。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 取消", callback_data=f"{CALLBACK_PREFIX}:panel")]]
            ),
            parse_mode="HTML",
        )
    if action == "rename" and len(parts) >= 3:
        raw_id = parts[2]
        record = _load(context)["admins"].get(raw_id)
        if not isinstance(record, dict):
            return await _show_panel(query, context)
        context.user_data.pop(ADD_STAGE_KEY, None)
        context.user_data[RENAME_STAGE_KEY] = raw_id
        return await query.edit_message_text(
            f"✏️ 请发送管理员 <code>{raw_id}</code> 的显示名称。\n"
            "例如：夜班审核员。发送“取消”返回权限页。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 取消", callback_data=f"{CALLBACK_PREFIX}:select:{raw_id}")]]
            ),
            parse_mode="HTML",
        )
    if action == "select" and len(parts) >= 3:
        context.user_data.pop(RENAME_STAGE_KEY, None)
        raw_id = parts[2]
        record = _load(context)["admins"].get(raw_id)
        if not isinstance(record, dict):
            return await _show_panel(query, context)
        return await query.edit_message_text(
            _admin_detail_text(raw_id, record),
            reply_markup=_admin_detail_keyboard(raw_id, record),
            parse_mode="HTML",
        )
    if action == "toggle" and len(parts) >= 4:
        raw_id, permission = parts[2], parts[3]
        if permission not in ALL_PERMISSIONS:
            return await query.answer("权限项无效。", show_alert=True)
        data = _load(context)
        record = data["admins"].get(raw_id)
        if not isinstance(record, dict):
            return await _show_panel(query, context)
        permissions = set(record.get("permissions") or [])
        if permission in permissions:
            permissions.remove(permission)
        else:
            permissions.add(permission)
        record["permissions"] = [key for key, _, _ in PERMISSION_OPTIONS if key in permissions]
        data["admins"][raw_id] = record
        _save(context, data)
        return await query.edit_message_text(
            _admin_detail_text(raw_id, record),
            reply_markup=_admin_detail_keyboard(raw_id, record),
            parse_mode="HTML",
        )
    if action == "delete_confirm" and len(parts) >= 3:
        raw_id = parts[2]
        record = _load(context)["admins"].get(raw_id)
        if not isinstance(record, dict):
            return await _show_panel(query, context)
        return await query.edit_message_text(
            f"确认删除管理员：<b>{html.escape(_display_admin(raw_id, record))}</b>？\n删除后将立即失去所有授权。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ 确认删除", callback_data=f"{CALLBACK_PREFIX}:delete:{raw_id}")],
                    [InlineKeyboardButton("⬅️ 取消", callback_data=f"{CALLBACK_PREFIX}:select:{raw_id}")],
                ]
            ),
            parse_mode="HTML",
        )
    if action == "delete" and len(parts) >= 3:
        data = _load(context)
        data["admins"].pop(parts[2], None)
        _save(context, data)
        return await _show_panel(query, context)


async def delegated_admin_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rename_target = context.user_data.get(RENAME_STAGE_KEY)
    if not context.user_data.get(ADD_STAGE_KEY) and not rename_target:
        return
    user = update.effective_user
    if not user or not is_owner_or_super_admin(context, user.id):
        context.user_data.pop(ADD_STAGE_KEY, None)
        return
    if not update.effective_chat or update.effective_chat.type != "private" or not update.message:
        return

    message = update.message
    if rename_target:
        new_name = (message.text or "").strip()
        if new_name in {"取消", "返回"}:
            context.user_data.pop(RENAME_STAGE_KEY, None)
            return await message.reply_text("已取消修改名称。")
        if not new_name:
            return await message.reply_text("显示名称不能为空，请重新输入。")
        if len(new_name) > 50:
            return await message.reply_text("显示名称不能超过 50 个字符，请重新输入。")
        data = _load(context)
        record = data["admins"].get(str(rename_target))
        if not isinstance(record, dict):
            context.user_data.pop(RENAME_STAGE_KEY, None)
            return await message.reply_text("该管理员不存在或已删除。")
        record["name"] = new_name
        data["admins"][str(rename_target)] = record
        _save(context, data)
        context.user_data.pop(RENAME_STAGE_KEY, None)
        return await message.reply_text(
            "✅ 显示名称已更新。",
            reply_markup=_admin_detail_keyboard(str(rename_target), record),
        )

    raw_id = (message.text or "").strip()
    target_user = getattr(message, "forward_origin", None)
    target_id = None
    if raw_id.isdigit():
        target_id = int(raw_id)
    else:
        # PTB exposes legacy forward_from for compatible Bot API messages.
        forwarded = getattr(message, "forward_from", None)
        if forwarded:
            target_id = getattr(forwarded, "id", None)
            target_user = forwarded
        elif target_user:
            sender_user = getattr(target_user, "sender_user", None)
            target_id = getattr(sender_user, "id", None)
            target_user = sender_user or target_user

    if not target_id:
        return await message.reply_text("请输入数字用户 ID，或转发一条未开启转发保护的用户消息。")
    if is_owner_or_super_admin(context, int(target_id)):
        context.user_data.pop(ADD_STAGE_KEY, None)
        return await message.reply_text("该用户已是机器人所有者或超级管理员，无需重复添加。")

    data = _load(context)
    existing = data["admins"].get(str(target_id), {})
    name = getattr(target_user, "full_name", "") if target_user else ""
    username = getattr(target_user, "username", "") if target_user else ""
    data["admins"][str(target_id)] = {
        "name": name or existing.get("name", ""),
        "username": username or existing.get("username", ""),
        "permissions": list(existing.get("permissions") or []),
    }
    _save(context, data)
    context.user_data.pop(ADD_STAGE_KEY, None)
    record = data["admins"][str(target_id)]
    await message.reply_text(
        f"✅ 已添加管理员 <code>{target_id}</code>。现在请配置其权限：",
        reply_markup=_admin_detail_keyboard(str(target_id), record),
        parse_mode="HTML",
    )


def register_admin_permission_handlers(app) -> None:
    app.add_handler(CallbackQueryHandler(delegated_admin_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, delegated_admin_add_input),
        group=9,
    )
