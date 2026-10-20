"""Owner-only formatter for channel posts forwarded to the master bot."""

from __future__ import annotations

import html
import os
import re
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from utils import load_json, save_json


CALLBACK_PREFIX = "fmt"
CONFIG_FILE = "config_data/format_output_rules.json"
OUTPUT_STATE_KEY = "format_output_waiting_for_forward"
RULE_EDIT_STATE_KEY = "format_output_rule_editing"
REPORT_LINK_STATE_KEY = "format_output_report_link_waiting"
RULES_PER_PAGE = 8
DEFAULT_TEMPLATE = "{content}"
_CUSTOM_EMOJI_TYPE = "custom_emoji"
_TOKEN_RE = re.compile(r"\uE000fmt_(?:template|source)_\d+\uE001")
# Telegram usernames are identified from the forwarded source text.  Restrict
# the character before @ so email addresses (for example a@b.com) are skipped.
_USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")


def _is_master_owner(context: ContextTypes.DEFAULT_TYPE, user) -> bool:
    if not user:
        return False
    bot_name = str(context.application.bot_data.get("name", "")).strip()
    master_name = str(os.getenv("MASTER_BOT_NAME", "")).strip()
    try:
        owner_id = int(context.application.bot_data.get("owner_id"))
    except (TypeError, ValueError):
        return False
    return bool(master_name and bot_name == master_name and int(user.id) == owner_id)


def _normalize_replacements(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized = []
    seen_ids = set()
    for fallback_id, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        old = str(item.get("from", ""))
        if not old:
            continue
        try:
            replacement_id = int(item.get("id", fallback_id))
        except (TypeError, ValueError):
            replacement_id = fallback_id
        while replacement_id in seen_ids:
            replacement_id += 1
        seen_ids.add(replacement_id)
        normalized.append(
            {
                "id": replacement_id,
                "from": old,
                "to": str(item.get("to", "")),
            }
        )
    return normalized


def _normalize_rule(value: Any, fallback_id: int) -> dict:
    value = value if isinstance(value, dict) else {}
    try:
        rule_id = int(value.get("id", fallback_id))
    except (TypeError, ValueError):
        rule_id = fallback_id
    channel_id = str(value.get("channel_id", "")).strip()
    if channel_id:
        try:
            channel_id = str(int(channel_id))
        except ValueError:
            channel_id = ""
    template = str(value.get("output_template", DEFAULT_TEMPLATE))
    if not template:
        template = DEFAULT_TEMPLATE
    entities = value.get("output_entities", [])
    if not isinstance(entities, list):
        entities = []
    cut_words = value.get("cut_words", [])
    if not isinstance(cut_words, list):
        cut_words = []
    return {
        "id": max(rule_id, 1),
        "enabled": bool(value.get("enabled", True)),
        "channel_id": channel_id,
        "output_template": template,
        "output_entities": [item for item in entities if isinstance(item, dict)],
        "cut_words": [str(item).strip() for item in cut_words if str(item).strip()],
        "cut_includes_word": bool(value.get("cut_includes_word", True)),
        "personal_report_link_enabled": bool(value.get("personal_report_link_enabled", False)),
        "replacements": _normalize_replacements(value.get("replacements", [])),
    }


def load_format_output_config() -> dict:
    raw = load_json(CONFIG_FILE)
    raw = raw if isinstance(raw, dict) else {}
    rules = []
    seen_ids = set()
    for index, item in enumerate(raw.get("rules", []), start=1):
        rule = _normalize_rule(item, index)
        while rule["id"] in seen_ids:
            rule["id"] += 1
        seen_ids.add(rule["id"])
        rules.append(rule)
    try:
        next_id = int(raw.get("next_id", 1))
    except (TypeError, ValueError):
        next_id = 1
    return {"next_id": max(next_id, max(seen_ids, default=0) + 1), "rules": rules}


def save_format_output_config(config: dict) -> None:
    save_json(CONFIG_FILE, config)


def _find_rule(config: dict, rule_id: int) -> Optional[dict]:
    return next((rule for rule in config["rules"] if rule["id"] == rule_id), None)


def _entity_value(entity: Any, key: str, default=None):
    return entity.get(key, default) if isinstance(entity, dict) else getattr(entity, key, default)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _python_index_from_utf16(value: str, target: int) -> Optional[int]:
    offset = 0
    for index, char in enumerate(value):
        if offset == target:
            return index
        offset += _utf16_length(char)
    return len(value) if offset == target else None


def _protect_custom_emojis(text: str, entities: Any, scope: str) -> tuple[str, dict[str, tuple[str, str]]]:
    """Replace custom-emoji spans with inert markers while text is transformed."""
    spans = []
    for entity in entities or []:
        if str(_entity_value(entity, "type", "")) != _CUSTOM_EMOJI_TYPE:
            continue
        emoji_id = str(_entity_value(entity, "custom_emoji_id", "") or "")
        try:
            offset = int(_entity_value(entity, "offset"))
            length = int(_entity_value(entity, "length"))
        except (TypeError, ValueError):
            continue
        if not emoji_id or length <= 0:
            continue
        start = _python_index_from_utf16(text, offset)
        end = _python_index_from_utf16(text, offset + length)
        if start is None or end is None or start >= end:
            continue
        spans.append((start, end, emoji_id))

    protected = text
    tokens: dict[str, tuple[str, str]] = {}
    for index, (start, end, emoji_id) in enumerate(sorted(spans, reverse=True), start=1):
        token = f"\uE000fmt_{scope}_{index}\uE001"
        tokens[token] = (text[start:end], emoji_id)
        protected = f"{protected[:start]}{token}{protected[end:]}"
    return protected, tokens


def _restore_custom_emojis(text: str, tokens: dict[str, tuple[str, str]]) -> tuple[str, list[MessageEntity]]:
    entities = []
    output_parts = []
    cursor = 0
    for match in _TOKEN_RE.finditer(text):
        output_parts.append(text[cursor : match.start()])
        token = match.group(0)
        original, emoji_id = tokens.get(token, (token, ""))
        offset = _utf16_length("".join(output_parts))
        output_parts.append(original)
        if emoji_id:
            entities.append(
                MessageEntity(
                    type=_CUSTOM_EMOJI_TYPE,
                    offset=offset,
                    length=_utf16_length(original),
                    custom_emoji_id=emoji_id,
                )
            )
        cursor = match.end()
    output_parts.append(text[cursor:])
    return "".join(output_parts), entities


def _apply_content_rule(text: str, rule: dict) -> str:
    result = text
    for replacement in rule.get("replacements", []):
        old = str(replacement.get("from", ""))
        if old:
            result = result.replace(old, str(replacement.get("to", "")))

    positions = [
        result.find(word)
        for word in rule.get("cut_words", [])
        if word and result.find(word) >= 0
    ]
    if positions:
        word_position = min(positions)
        matching_word = next(
            word
            for word in rule.get("cut_words", [])
            if result.find(word) == word_position
        )
        cutoff_end = (
            word_position + len(matching_word)
            if bool(rule.get("cut_includes_word", True))
            else word_position
        )
        result = result[:cutoff_end]
    return result


def format_forwarded_message(message, rule: dict) -> tuple[str, list[MessageEntity]]:
    source_text = message.text if message.text is not None else (message.caption or "")
    source_entities = message.entities if message.text is not None else message.caption_entities
    protected_source, source_tokens = _protect_custom_emojis(
        source_text, source_entities, "source"
    )
    content = _apply_content_rule(protected_source, rule)

    template = str(rule.get("output_template") or DEFAULT_TEMPLATE)
    protected_template, template_tokens = _protect_custom_emojis(
        template, rule.get("output_entities", []), "template"
    )
    rendered = protected_template.replace("{content}", content)
    final_text, entities = _restore_custom_emojis(
        rendered, {**template_tokens, **source_tokens}
    )
    return final_text, entities


async def format_message_for_publish(
    context: ContextTypes.DEFAULT_TYPE,
    message,
) -> Optional[tuple[str, list[MessageEntity], dict]]:
    """Apply the enabled source-channel rule to a 投稿 message once.

    Returns ``None`` when the message is not a forwarded channel post or no
    enabled formatter rule matches its source channel.  Keeping the rule
    lookup here makes 投稿 and the standalone “格式输出” entry use exactly the
    same source-channel matching, template, replacement and report-link logic.
    """
    if (message.text is None) and not message.caption:
        return None

    channel_id = _source_channel_id(message)
    if not channel_id:
        return None

    config = load_format_output_config()
    rule = next(
        (
            item
            for item in config["rules"]
            if item.get("enabled", True) and item.get("channel_id") == channel_id
        ),
        None,
    )
    if not rule:
        return None

    output_text, entities = format_forwarded_message(message, rule)
    report_link = await _personal_report_link(context, rule, message)
    if report_link:
        output_text, entities = _append_personal_report_link(
            output_text, entities, report_link
        )
    return output_text, entities, rule


def _append_personal_report_link(
    output_text: str,
    entities: list[MessageEntity],
    report_link: str,
) -> tuple[str, list[MessageEntity]]:
    """Append a clickable personal-report deep link below formatted content."""
    prefix = "📋 个人报告："
    link_offset = _utf16_length(output_text + prefix)
    output_text = f"{output_text}{prefix}{report_link}"
    return output_text, [
        *entities,
        MessageEntity(
            type="url",
            offset=link_offset,
            length=_utf16_length(report_link),
        ),
    ]


def _report_id_from_message_text(message) -> Optional[str]:
    """Return the first Telegram @username found in a forwarded message."""
    text = message.text if message.text is not None else (message.caption or "")
    match = _USERNAME_RE.search(text)
    return match.group(1).lower() if match else None


async def _personal_report_link(
    context: ContextTypes.DEFAULT_TYPE,
    rule: dict,
    message,
) -> Optional[str]:
    """Build a report link from the first @username in the source message."""
    if not rule.get("personal_report_link_enabled", False):
        return None
    report_id = _report_id_from_message_text(message)
    if not report_id:
        return None
    try:
        # Keep the deep-link format consistent with the existing report-link
        # command and its bot-username fallback behavior.
        from channel.publish_setting import _report_deep_link

        return await _report_deep_link(context, report_id)
    except Exception as exc:
        print(f"生成个人报告链接失败 report_id={report_id}: {exc}")
        return None


def _source_channel_id(message) -> Optional[str]:
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin else None
    if chat is None:
        chat = getattr(message, "forward_from_chat", None)
    channel_id = getattr(chat, "id", None)
    if channel_id is None:
        return None
    try:
        return str(int(channel_id))
    except (TypeError, ValueError):
        return None


def _serialize_custom_emoji_entities(message) -> list[dict]:
    return [
        entity.to_dict()
        for entity in (getattr(message, "entities", None) or [])
        if str(getattr(entity, "type", "")) == _CUSTOM_EMOJI_TYPE
        and getattr(entity, "custom_emoji_id", None)
    ]


def _rule_label(rule: dict) -> str:
    state = "✅" if rule.get("enabled", True) else "🚫"
    channel = rule.get("channel_id") or "未设置频道"
    return f"{state} #{rule['id']} · {channel}"[:64]


def _rules_keyboard(config: dict, page: int) -> InlineKeyboardMarkup:
    rules = config["rules"]
    total_pages = max(1, (len(rules) + RULES_PER_PAGE - 1) // RULES_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * RULES_PER_PAGE
    rows = [
        [InlineKeyboardButton(_rule_label(rule), callback_data=f"{CALLBACK_PREFIX}:rule:{rule['id']}:{page}")]
        for rule in rules[start : start + RULES_PER_PAGE]
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"{CALLBACK_PREFIX}:rules:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"{CALLBACK_PREFIX}:rules:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ 新增规则", callback_data=f"{CALLBACK_PREFIX}:add:{page}")])
    rows.append([InlineKeyboardButton("⬅️ 返回主面板", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _rules_text(config: dict, page: int) -> str:
    count = len(config["rules"])
    return (
        "⚙️ <b>格式输出 · 规则设置</b>\n\n"
        f"已配置规则：{count} 条。\n"
        "每条规则对应一个来源频道 ID；转发该频道的消息到机器人后，会按规则处理。\n\n"
        "点击规则可修改频道号、输出内容、截取词和替换词。"
    )


async def _show_rules(query, page: int = 1) -> None:
    config = load_format_output_config()
    await query.edit_message_text(
        _rules_text(config, page),
        reply_markup=_rules_keyboard(config, page),
        parse_mode="HTML",
    )


def _preview(value: str, limit: int = 500) -> str:
    value = value or "（未设置）"
    return value if len(value) <= limit else f"{value[:limit]}…"


def _rule_detail_text(rule: dict) -> str:
    replacements = rule.get("replacements", [])
    replace_text = "\n".join(
        f"{item['from']} → {item['to'] or '（删除）'}" for item in replacements
    ) or "（未设置）"
    cut_text = "、".join(rule.get("cut_words", [])) or "（未设置）"
    cut_mode = "保留截取词" if rule.get("cut_includes_word", True) else "不保留截取词"
    status = "开启" if rule.get("enabled", True) else "关闭"
    report_link_status = (
        "开启（自动识别文本中的 @用户名）"
        if rule.get("personal_report_link_enabled", False)
        else "关闭"
    )
    return (
        f"⚙️ <b>规则 #{rule['id']}</b>\n\n"
        f"状态：{status}\n"
        f"来源频道号：<code>{html.escape(rule.get('channel_id') or '未设置')}</code>\n"
        f"截取词：<code>{html.escape(_preview(cut_text, 300))}</code>\n"
        f"截取方式：{cut_mode}\n"
        f"个人报告链接：{html.escape(report_link_status)}\n"
        f"替换词：<blockquote>{html.escape(_preview(replace_text, 700))}</blockquote>\n"
        f"输出内容：<blockquote>{html.escape(_preview(rule.get('output_template', ''), 700))}</blockquote>\n\n"
        "输出内容须包含 <code>{content}</code>，它会被替换为处理后的转发正文。"
    )


def _rule_detail_keyboard(rule: dict, page: int) -> InlineKeyboardMarkup:
    rule_id = rule["id"]
    toggle_text = "🚫 关闭规则" if rule.get("enabled", True) else "✅ 开启规则"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 设置来源频道号", callback_data=f"{CALLBACK_PREFIX}:field:{rule_id}:channel:{page}")],
            [InlineKeyboardButton("📝 设置输出内容", callback_data=f"{CALLBACK_PREFIX}:field:{rule_id}:template:{page}")],
            [
                InlineKeyboardButton("✂️ 设置截取词", callback_data=f"{CALLBACK_PREFIX}:field:{rule_id}:cut:{page}"),
                InlineKeyboardButton("🔁 管理替换词", callback_data=f"{CALLBACK_PREFIX}:replacements:{rule_id}:{page}"),
            ],
            [
                InlineKeyboardButton(
                    f"✂️ 保留截取词：{'✅' if rule.get('cut_includes_word', True) else '🚫'}",
                    callback_data=f"{CALLBACK_PREFIX}:cut_include:{rule_id}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 个人报告链接",
                    callback_data=f"{CALLBACK_PREFIX}:rule_report_link:{rule_id}:{page}",
                )
            ],
            [InlineKeyboardButton(toggle_text, callback_data=f"{CALLBACK_PREFIX}:toggle:{rule_id}:{page}")],
            [InlineKeyboardButton("🗑 删除规则", callback_data=f"{CALLBACK_PREFIX}:delete:{rule_id}:{page}")],
            [InlineKeyboardButton("⬅️ 返回规则列表", callback_data=f"{CALLBACK_PREFIX}:rules:{page}")],
        ]
    )


async def _show_rule(query, rule_id: int, page: int) -> None:
    rule = _find_rule(load_format_output_config(), rule_id)
    if not rule:
        await query.answer("规则不存在或已删除。", show_alert=True)
        await _show_rules(query, page)
        return
    await query.edit_message_text(
        _rule_detail_text(rule),
        reply_markup=_rule_detail_keyboard(rule, page),
        parse_mode="HTML",
    )


def _personal_report_link_keyboard(rule: dict, page: int) -> InlineKeyboardMarkup:
    rule_id = rule["id"]
    enabled = bool(rule.get("personal_report_link_enabled", False))
    toggle_label = "🚫 关闭自动附加" if enabled else "✅ 开启自动附加"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    toggle_label,
                    callback_data=f"{CALLBACK_PREFIX}:rule_report_link_toggle:{rule_id}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回规则",
                    callback_data=f"{CALLBACK_PREFIX}:rule:{rule_id}:{page}",
                )
            ],
        ]
    )


async def _show_personal_report_link(query, rule_id: int, page: int) -> None:
    rule = _find_rule(load_format_output_config(), rule_id)
    if not rule:
        await query.answer("规则不存在或已删除。", show_alert=True)
        return await _show_rules(query, page)

    enabled = bool(rule.get("personal_report_link_enabled", False))
    detail = "已开启" if enabled else "已关闭"
    await query.edit_message_text(
        "📋 <b>个人报告链接</b>\n\n"
        f"自动附加：{detail}\n\n"
        "开启后，每次按本规则格式化时，机器人会从转发文本中自动识别第一个 "
        "<code>@用户名</code>，并在内容最下方添加对应的个人报告链接。\n\n"
        "无需预先输入或固定报告 ID。",
        reply_markup=_personal_report_link_keyboard(rule, page),
        parse_mode="HTML",
    )


def _find_replacement(rule: dict, replacement_id: int) -> Optional[dict]:
    return next(
        (
            item
            for item in rule.get("replacements", [])
            if int(item.get("id", 0)) == replacement_id
        ),
        None,
    )


def _replacement_label(replacement: dict) -> str:
    target = str(replacement.get("to", "")) or "（删除）"
    return f"{replacement.get('from', '')} → {target}"[:64]


def _replacements_keyboard(rule: dict, page: int) -> InlineKeyboardMarkup:
    rule_id = rule["id"]
    rows = [
        [
            InlineKeyboardButton(
                _replacement_label(replacement),
                callback_data=(
                    f"{CALLBACK_PREFIX}:replacement:{rule_id}:{replacement['id']}:{page}"
                ),
            )
        ]
        for replacement in rule.get("replacements", [])
    ]
    rows.append(
        [
            InlineKeyboardButton(
                "➕ 新增替换词",
                callback_data=f"{CALLBACK_PREFIX}:replacement_add:{rule_id}:{page}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回规则",
                callback_data=f"{CALLBACK_PREFIX}:rule:{rule_id}:{page}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _show_replacements(query, rule_id: int, page: int) -> None:
    rule = _find_rule(load_format_output_config(), rule_id)
    if not rule:
        await query.answer("规则不存在或已删除。", show_alert=True)
        return await _show_rules(query, page)
    replacements = rule.get("replacements", [])
    text = (
        f"🔁 <b>规则 #{rule_id} · 替换词管理</b>\n\n"
        f"当前共 {len(replacements)} 条替换词。\n"
        "点击一条替换词后，可单独修改原词或替换结果；替换结果设为“删除”即可去掉原词。"
    )
    await query.edit_message_text(
        text,
        reply_markup=_replacements_keyboard(rule, page),
        parse_mode="HTML",
    )


def _replacement_detail_keyboard(rule_id: int, replacement_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ 修改原词",
                    callback_data=(
                        f"{CALLBACK_PREFIX}:replacement_edit:{rule_id}:{replacement_id}:from:{page}"
                    ),
                ),
                InlineKeyboardButton(
                    "✏️ 修改替换结果",
                    callback_data=(
                        f"{CALLBACK_PREFIX}:replacement_edit:{rule_id}:{replacement_id}:to:{page}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑 删除这条替换词",
                    callback_data=(
                        f"{CALLBACK_PREFIX}:replacement_delete:{rule_id}:{replacement_id}:{page}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回替换词列表",
                    callback_data=f"{CALLBACK_PREFIX}:replacements:{rule_id}:{page}",
                )
            ],
        ]
    )


async def _show_replacement(query, rule_id: int, replacement_id: int, page: int) -> None:
    rule = _find_rule(load_format_output_config(), rule_id)
    replacement = _find_replacement(rule, replacement_id) if rule else None
    if not replacement:
        await query.answer("替换词不存在或已删除。", show_alert=True)
        return await _show_replacements(query, rule_id, page)
    target = str(replacement.get("to", "")) or "（删除原词）"
    await query.edit_message_text(
        f"🔁 <b>替换词 #{replacement_id}</b>\n\n"
        f"原词：<code>{html.escape(str(replacement.get('from', '')))}</code>\n"
        f"替换结果：<code>{html.escape(target)}</code>",
        reply_markup=_replacement_detail_keyboard(rule_id, replacement_id, page),
        parse_mode="HTML",
    )


def _field_prompt(field: str) -> str:
    return {
        "channel": "请输入来源频道号，例如 <code>-1001234567890</code>。\n之后转发该频道的消息，才会套用本规则。",
        "template": "请输入输出内容。必须带有 <code>{content}</code> 占位符，转发正文会填入这里。\n支持会员表情；发送 <code>{content}</code> 可恢复为仅输出正文。",
        "cut": "请输入截取词，可用逗号或换行设置多条。匹配到任一词后会截掉后续内容；是否保留截取词可在规则页面单独切换。\n发送“清空”可删除全部截取词。",
    }[field]


def _parse_cut_words(text: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in re.split(r"[,\n]", text) if item.strip()))


async def _open_output_mode(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(RULE_EDIT_STATE_KEY, None)
    context.user_data[OUTPUT_STATE_KEY] = True
    await query.edit_message_text(
        "📐 <b>格式输出</b>\n\n"
        "请把频道消息转发给我。我会依据来源频道号匹配已开启的规则，并把格式化结果发回给你。\n\n"
        "若没有匹配规则，请先进入“规则设置”添加来源频道号。",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⚙️ 规则设置", callback_data=f"{CALLBACK_PREFIX}:rules:1")],
                [InlineKeyboardButton("取消", callback_data=f"{CALLBACK_PREFIX}:cancel")],
            ]
        ),
        parse_mode="HTML",
    )


async def _open_report_link_mode(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(OUTPUT_STATE_KEY, None)
    context.user_data.pop(RULE_EDIT_STATE_KEY, None)
    context.user_data[REPORT_LINK_STATE_KEY] = True
    await query.edit_message_text(
        "📋 <b>生成报告链接</b>\n\n"
        "请发送报告 ID 或 @用户名，例如：<code>xilin718</code> 或 <code>@xilin718</code>。",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("取消", callback_data=f"{CALLBACK_PREFIX}:cancel")]]
        ),
        parse_mode="HTML",
    )


async def format_output_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{CALLBACK_PREFIX}:"):
        return
    if not _is_master_owner(context, query.from_user):
        return await query.answer("仅主机器人所有者可使用格式输出。", show_alert=True)

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action == "output":
            await query.answer()
            return await _open_output_mode(query, context)
        if action == "report_link":
            await query.answer()
            return await _open_report_link_mode(query, context)
        if action == "rules":
            context.user_data.pop(OUTPUT_STATE_KEY, None)
            context.user_data.pop(REPORT_LINK_STATE_KEY, None)
            await query.answer()
            page = int(parts[2]) if len(parts) > 2 else 1
            return await _show_rules(query, page)
        if action == "cancel":
            context.user_data.pop(OUTPUT_STATE_KEY, None)
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            context.user_data.pop(REPORT_LINK_STATE_KEY, None)
            await query.answer("已取消。")
            return await query.edit_message_text(
                "已取消当前操作。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ 返回主面板", callback_data="start:back")]]
                ),
            )
        if action == "add":
            page = int(parts[2]) if len(parts) > 2 else 1
            config = load_format_output_config()
            rule = _normalize_rule({"id": config["next_id"]}, config["next_id"])
            config["next_id"] = rule["id"] + 1
            config["rules"].append(rule)
            save_format_output_config(config)
            await query.answer("已新增规则，请先设置来源频道号。")
            return await _show_rule(query, rule["id"], page)
        if action == "rule":
            rule_id, page = int(parts[2]), int(parts[3])
            await query.answer()
            return await _show_rule(query, rule_id, page)
        if action == "toggle":
            rule_id, page = int(parts[2]), int(parts[3])
            config = load_format_output_config()
            rule = _find_rule(config, rule_id)
            if not rule:
                return await query.answer("规则不存在。", show_alert=True)
            rule["enabled"] = not bool(rule.get("enabled", True))
            save_format_output_config(config)
            await query.answer("规则状态已更新。")
            return await _show_rule(query, rule_id, page)
        if action == "cut_include":
            rule_id, page = int(parts[2]), int(parts[3])
            config = load_format_output_config()
            rule = _find_rule(config, rule_id)
            if not rule:
                return await query.answer("规则不存在。", show_alert=True)
            rule["cut_includes_word"] = not bool(rule.get("cut_includes_word", True))
            save_format_output_config(config)
            await query.answer(
                "截取内容将保留截取词。"
                if rule["cut_includes_word"]
                else "截取内容将同时删除截取词。"
            )
            return await _show_rule(query, rule_id, page)
        if action == "rule_report_link":
            rule_id, page = int(parts[2]), int(parts[3])
            await query.answer()
            return await _show_personal_report_link(query, rule_id, page)
        if action == "rule_report_link_toggle":
            rule_id, page = int(parts[2]), int(parts[3])
            config = load_format_output_config()
            rule = _find_rule(config, rule_id)
            if not rule:
                return await query.answer("规则不存在。", show_alert=True)
            rule["personal_report_link_enabled"] = not bool(
                rule.get("personal_report_link_enabled", False)
            )
            save_format_output_config(config)
            await query.answer(
                "格式化内容底部将附加个人报告链接。"
                if rule["personal_report_link_enabled"]
                else "已关闭个人报告链接自动附加。"
            )
            return await _show_personal_report_link(query, rule_id, page)
        if action == "delete":
            rule_id, page = int(parts[2]), int(parts[3])
            config = load_format_output_config()
            before = len(config["rules"])
            config["rules"] = [rule for rule in config["rules"] if rule["id"] != rule_id]
            if len(config["rules"]) == before:
                return await query.answer("规则不存在。", show_alert=True)
            save_format_output_config(config)
            await query.answer("规则已删除。")
            return await _show_rules(query, page)
        if action == "replacements":
            rule_id, page = int(parts[2]), int(parts[3])
            await query.answer()
            return await _show_replacements(query, rule_id, page)
        if action == "replacement":
            rule_id, replacement_id, page = int(parts[2]), int(parts[3]), int(parts[4])
            await query.answer()
            return await _show_replacement(query, rule_id, replacement_id, page)
        if action == "replacement_add":
            rule_id, page = int(parts[2]), int(parts[3])
            if not _find_rule(load_format_output_config(), rule_id):
                return await query.answer("规则不存在。", show_alert=True)
            context.user_data[RULE_EDIT_STATE_KEY] = {
                "kind": "replacement_add",
                "step": "from",
                "rule_id": rule_id,
                "page": page,
            }
            await query.answer()
            return await query.edit_message_text(
                "请输入要替换的原词。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:replacements:{rule_id}:{page}")]]
                ),
            )
        if action == "replacement_edit":
            rule_id, replacement_id, field, page = (
                int(parts[2]),
                int(parts[3]),
                parts[4],
                int(parts[5]),
            )
            rule = _find_rule(load_format_output_config(), rule_id)
            replacement = _find_replacement(rule, replacement_id) if rule else None
            if field not in {"from", "to"} or not replacement:
                return await query.answer("替换词不存在或参数无效。", show_alert=True)
            context.user_data[RULE_EDIT_STATE_KEY] = {
                "kind": "replacement_edit",
                "rule_id": rule_id,
                "replacement_id": replacement_id,
                "field": field,
                "page": page,
            }
            prompt = "请输入新的原词。" if field == "from" else "请输入新的替换结果；发送“删除”或“-”可删除原词。"
            await query.answer()
            return await query.edit_message_text(
                prompt,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ 返回", callback_data=f"{CALLBACK_PREFIX}:replacement:{rule_id}:{replacement_id}:{page}")]]
                ),
            )
        if action == "replacement_delete":
            rule_id, replacement_id, page = int(parts[2]), int(parts[3]), int(parts[4])
            config = load_format_output_config()
            rule = _find_rule(config, rule_id)
            replacement = _find_replacement(rule, replacement_id) if rule else None
            if not replacement:
                return await query.answer("替换词不存在。", show_alert=True)
            rule["replacements"] = [
                item for item in rule["replacements"] if item["id"] != replacement_id
            ]
            save_format_output_config(config)
            await query.answer("替换词已删除。")
            return await _show_replacements(query, rule_id, page)
        if action == "field":
            rule_id, field, page = int(parts[2]), parts[3], int(parts[4])
            if field not in {"channel", "template", "cut"}:
                return await query.answer("未知设置项。", show_alert=True)
            if not _find_rule(load_format_output_config(), rule_id):
                return await query.answer("规则不存在。", show_alert=True)
            context.user_data.pop(OUTPUT_STATE_KEY, None)
            context.user_data[RULE_EDIT_STATE_KEY] = {
                "kind": "field",
                "rule_id": rule_id,
                "field": field,
                "page": page,
            }
            await query.answer()
            return await query.edit_message_text(
                _field_prompt(field),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        "⬅️ 返回并取消输入",
                        callback_data=f"{CALLBACK_PREFIX}:rule:{rule_id}:{page}",
                    )]]
                ),
                parse_mode="HTML",
            )
    except (IndexError, ValueError):
        return await query.answer("操作参数无效，请重新打开面板。", show_alert=True)

    await query.answer("未知操作。", show_alert=True)


async def format_output_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or update.effective_chat.type != "private":
        return
    if not _is_master_owner(context, update.effective_user):
        return

    text = update.message.text or ""
    text_for_command = text.strip()

    if context.user_data.get(REPORT_LINK_STATE_KEY):
        if text_for_command in {"取消", "返回"}:
            context.user_data.pop(REPORT_LINK_STATE_KEY, None)
            await update.message.reply_text("已取消生成报告链接。")
            raise ApplicationHandlerStop
        report_id = text_for_command.lstrip("@").lower()
        if not report_id:
            await update.message.reply_text("报告 ID 不能为空，请重新发送。")
            raise ApplicationHandlerStop
        try:
            # Reuse the existing report-link implementation rather than
            # duplicating its deep-link construction or report lookup rules.
            from channel.publish_setting import _report_deep_link

            link = await _report_deep_link(context, report_id)
        except Exception as exc:
            print(f"生成报告链接失败 report_id={report_id}: {exc}")
            link = None
        if not link:
            await update.message.reply_text("❌ 无法生成报告链接，请检查报告 ID 和机器人用户名配置。")
            raise ApplicationHandlerStop
        context.user_data.pop(REPORT_LINK_STATE_KEY, None)
        await update.message.reply_text(
            f"📋 报告链接：\n\n{link}",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 返回主面板", callback_data="start:back")]]
            ),
        )
        raise ApplicationHandlerStop

    edit_state = context.user_data.get(RULE_EDIT_STATE_KEY)
    if isinstance(edit_state, dict):
        try:
            rule_id = int(edit_state.get("rule_id"))
            page = int(edit_state.get("page", 1))
        except (TypeError, ValueError):
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            return
        if text_for_command in {"取消", "返回"}:
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            await update.message.reply_text("已取消修改。")
            raise ApplicationHandlerStop
        config = load_format_output_config()
        rule = _find_rule(config, rule_id)
        if not rule:
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            await update.message.reply_text("规则不存在或已删除。")
            raise ApplicationHandlerStop

        kind = edit_state.get("kind", "field")
        if kind == "replacement_add":
            if edit_state.get("step") == "from":
                if not text_for_command:
                    await update.message.reply_text("原词不能为空，请重新发送。")
                    raise ApplicationHandlerStop
                edit_state["step"] = "to"
                edit_state["from"] = text
                context.user_data[RULE_EDIT_STATE_KEY] = edit_state
                await update.message.reply_text(
                    "请输入替换结果；发送“删除”或“-”表示从原内容中删除这个词。"
                )
                raise ApplicationHandlerStop
            replacement_id = max(
                (int(item.get("id", 0)) for item in rule.get("replacements", [])),
                default=0,
            ) + 1
            replacement_to = "" if text_for_command in {"删除", "-"} else text
            rule.setdefault("replacements", []).append(
                {
                    "id": replacement_id,
                    "from": str(edit_state.get("from", "")),
                    "to": replacement_to,
                }
            )
            save_format_output_config(config)
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            await update.message.reply_text(
                "✅ 替换词已新增。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔁 返回替换词", callback_data=f"{CALLBACK_PREFIX}:replacements:{rule_id}:{page}")]]
                ),
            )
            raise ApplicationHandlerStop

        if kind == "replacement_edit":
            try:
                replacement_id = int(edit_state.get("replacement_id"))
            except (TypeError, ValueError):
                context.user_data.pop(RULE_EDIT_STATE_KEY, None)
                return
            replacement = _find_replacement(rule, replacement_id)
            field = edit_state.get("field")
            if not replacement or field not in {"from", "to"}:
                context.user_data.pop(RULE_EDIT_STATE_KEY, None)
                await update.message.reply_text("替换词不存在或修改参数无效。")
                raise ApplicationHandlerStop
            if field == "from":
                if not text_for_command:
                    await update.message.reply_text("原词不能为空，请重新发送。")
                    raise ApplicationHandlerStop
                replacement["from"] = text
            else:
                replacement["to"] = "" if text_for_command in {"删除", "-"} else text
            save_format_output_config(config)
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            await update.message.reply_text(
                "✅ 替换词已修改。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔁 返回该替换词", callback_data=f"{CALLBACK_PREFIX}:replacement:{rule_id}:{replacement_id}:{page}")]]
                ),
            )
            raise ApplicationHandlerStop

        field = edit_state.get("field")
        if field == "channel":
            try:
                channel_id = str(int(text_for_command))
            except ValueError:
                await update.message.reply_text("频道号格式不正确，请发送数字，例如 -1001234567890。")
                raise ApplicationHandlerStop
            duplicate = next(
                (
                    item
                    for item in config["rules"]
                    if item["id"] != rule_id and item.get("channel_id") == channel_id
                ),
                None,
            )
            if duplicate:
                await update.message.reply_text(
                    f"频道号 {channel_id} 已配置在规则 #{duplicate['id']}，请修改该规则。"
                )
                raise ApplicationHandlerStop
            rule["channel_id"] = channel_id
        elif field == "template":
            if len(text) > 3500:
                await update.message.reply_text("输出内容不能超过 3500 个字符，请重新发送。")
                raise ApplicationHandlerStop
            if "{content}" not in text:
                await update.message.reply_text("输出内容必须包含 {content}，请重新发送。")
                raise ApplicationHandlerStop
            rule["output_template"] = text
            rule["output_entities"] = _serialize_custom_emoji_entities(update.message)
        elif field == "cut":
            rule["cut_words"] = [] if text_for_command in {"清空", "-"} else _parse_cut_words(text)
        else:
            context.user_data.pop(RULE_EDIT_STATE_KEY, None)
            return

        save_format_output_config(config)
        context.user_data.pop(RULE_EDIT_STATE_KEY, None)
        await update.message.reply_text(
            "✅ 规则已保存。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "⚙️ 返回规则设置",
                    callback_data=f"{CALLBACK_PREFIX}:rule:{rule_id}:{page}",
                )]]
            ),
        )
        raise ApplicationHandlerStop

    if not context.user_data.get(OUTPUT_STATE_KEY):
        return
    if text_for_command in {"取消", "返回"}:
        context.user_data.pop(OUTPUT_STATE_KEY, None)
        await update.message.reply_text("已取消格式输出。")
        raise ApplicationHandlerStop
    if not getattr(update.message, "forward_origin", None) and not getattr(update.message, "forward_from_chat", None):
        await update.message.reply_text("请转发一条频道消息给我，或发送“取消”退出格式输出。")
        raise ApplicationHandlerStop
    if (update.message.text is None) and not update.message.caption:
        pass
        raise ApplicationHandlerStop

    channel_id = _source_channel_id(update.message)
    if not channel_id:
        await update.message.reply_text("无法识别转发来源频道，请确认消息是直接从频道转发而来。")
        raise ApplicationHandlerStop

    formatted = await format_message_for_publish(context, update.message)
    if not formatted:
        await update.message.reply_text(
            f"未找到来源频道 <code>{channel_id}</code> 的已开启规则。",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⚙️ 规则设置", callback_data=f"{CALLBACK_PREFIX}:rules:1")]]
            ),
        )
        raise ApplicationHandlerStop

    # Formatting is intentionally a one-shot action.  If this flag remains
    # active, subsequent forwarded submissions are intercepted here instead of
    # continuing through the normal channel-submission flow.
    context.user_data.pop(OUTPUT_STATE_KEY, None)

    output_text, entities, _rule = formatted
    if not output_text:
        await update.message.reply_text("处理后的输出内容为空。")
        raise ApplicationHandlerStop
    if len(output_text) > 4096:
        await update.message.reply_text("处理后的输出超过 Telegram 4096 字符限制，请调整规则或截取词。")
        raise ApplicationHandlerStop
    await update.message.reply_text(
        output_text,
        entities=entities or None,
        disable_web_page_preview=True,
    )
    raise ApplicationHandlerStop

def register_format_output_handlers(app) -> None:
    app.add_handler(CallbackQueryHandler(format_output_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), format_output_input),
        group=-23,
    )
