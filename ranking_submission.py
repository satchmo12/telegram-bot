# -*- coding: utf-8 -*-
"""主机器人「上榜投稿」流程。

This module deliberately uses its own callback prefix and JSON files.  It does
not reuse the legacy ``publish:`` draft state, so an in-progress ranking
submission cannot be consumed by the ordinary 投稿/关键词/私聊流程.
"""
from __future__ import annotations

import html
import os
import re
import time
import uuid
from urllib.parse import quote
from types import SimpleNamespace
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from admin_permissions import get_delegated_admin_ids, has_admin_permission, is_owner_or_super_admin
from channel.publish_setting import (
    _comment_deep_link,
    _create_comment_report,
    _extract_routing_keywords,
    _mark_submission_write_report_link,
    _register_post_keywords,
    _report_deep_link,
    _report_id_from_subject_entries,
    load_publish_config,
)
from utils import get_runtime_bot_name, load_json, save_json


CONFIG_FILE = "config_data/ranking_submission.json"
SUBMISSIONS_FILE = "data/ranking_submissions.json"
CALLBACK_PREFIX = "rank"
# Only ``{#}`` / ``{#标签}`` denote multi-value hashtag fields. Other
# fields retain the existing ``{姓名}`` placeholder syntax.
PLACEHOLDER_PATTERN = re.compile(r"\{(#(?:[\w\-\u4e00-\u9fff]+)?|[\w\-\u4e00-\u9fff]+)\}")
MAX_MEDIA = 9
# Generated content is a media caption, not a standalone message. Telegram
# caps captions at 1,024 characters; leave room for preview/review labels and
# an optional report link.
MAX_TEMPLATE_TEXT = 700
MAX_RENDERED_TEXT = 800
MAX_FIELD_VALUE = 500
MAX_CAPTION_LENGTH = 1024
DRAFT_TIMEOUT_SECONDS = 24 * 60 * 60

# Statuses below are persisted rather than only stored in context.user_data.
# That makes restarts safe and also acts as the idempotency lock for approval.
DRAFT_STATUSES = frozenset({"select_template", "collecting_media", "collecting_fields", "awaiting_confirmation"})
TERMINAL_STATUSES = frozenset({"published", "rejected", "cancelled", "expired"})


def _is_master_bot(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Only expose this feature in the configured main bot.

    A development/single-bot process may not set MASTER_BOT_NAME.  In that
    case the one running bot is treated as the main bot, which keeps the
    feature usable without weakening production multi-bot isolation.
    """
    configured = str(os.getenv("MASTER_BOT_NAME", "")).strip()
    app_name = str(getattr(context.application, "bot_data", {}).get("name", "")).strip()
    runtime_name = get_runtime_bot_name()
    return not configured or app_name == configured or runtime_name == configured


def _is_admin(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return bool(
        is_owner_or_super_admin(context, int(user_id))
        or has_admin_permission(context, int(user_id), "submission_config")
        or has_admin_permission(context, int(user_id), "submission_review")
    )


def _load_config() -> dict[str, Any]:
    raw = load_json(CONFIG_FILE)
    config = raw if isinstance(raw, dict) else {}
    changed = False
    if not isinstance(config.get("templates"), list):
        config["templates"] = []
        changed = True
    if "channel_id" not in config:
        config["channel_id"] = None
        changed = True
    if "greeting_text" not in config:
        config["greeting_text"] = ""
        changed = True
    if changed:
        save_json(CONFIG_FILE, config)
    return config


def _save_config(config: dict[str, Any]) -> None:
    save_json(CONFIG_FILE, config)


def _load_submissions() -> dict[str, Any]:
    raw = load_json(SUBMISSIONS_FILE)
    if not isinstance(raw, dict):
        raw = {}
    submissions = raw.get("submissions")
    if not isinstance(submissions, dict):
        # Keep the file extensible while being compatible with an early flat
        # mapping should one have been created manually.
        submissions = {
            key: value
            for key, value in raw.items()
            if isinstance(value, dict) and "status" in value
        }
        raw = {"submissions": submissions}
    raw["submissions"] = submissions
    return raw


def _save_submissions(data: dict[str, Any]) -> None:
    # Retain the newest completed records only. Pending and draft records are
    # never pruned, so recovery after a restart remains possible.
    records = data.get("submissions", {})
    if isinstance(records, dict) and len(records) > 5000:
        finished = sorted(
            (
                (sid, item)
                for sid, item in records.items()
                if isinstance(item, dict) and item.get("status") in TERMINAL_STATUSES
            ),
            key=lambda pair: int(pair[1].get("updated_at", 0) or 0),
        )
        for sid, _ in finished[: max(0, len(records) - 5000)]:
            records.pop(sid, None)
    save_json(SUBMISSIONS_FILE, data)


def _templates(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in config.get("templates", []) if isinstance(item, dict) and item.get("id")]


def _find_template(config: dict[str, Any], template_id: str) -> Optional[dict[str, Any]]:
    return next((item for item in _templates(config) if str(item.get("id")) == str(template_id)), None)


def _template_fields(template: dict[str, Any]) -> list[str]:
    source = str(template.get("body_html") or template.get("body_plain") or "")
    fields: list[str] = []
    for name in PLACEHOLDER_PATTERN.findall(source):
        if name not in fields:
            fields.append(name)
    return fields


def _safe_template_html(message) -> str:
    """Get Telegram HTML so custom emoji entities remain custom emoji.

    PTB's ``text_html`` serializes a CUSTOM_EMOJI entity as Telegram's
    ``<tg-emoji emoji-id="…">…</tg-emoji>`` tag.  Persisting that form and
    later sending it with HTML parse mode preserves Premium/custom emojis as
    well as normal rich text.
    """
    rich = getattr(message, "text_html", None)
    if isinstance(rich, str) and rich:
        return rich
    return html.escape(str(getattr(message, "text", "") or ""))


def _render_hashtag_values(value: Any) -> str:
    """Convert whitespace-separated input into escaped Telegram hashtags."""
    tags = []
    for item in re.split(r"\s+", str(value or "").strip()):
        item = item.strip().lstrip("#")
        if item:
            tags.append("#" + item)
    return " ".join(html.escape(item) for item in tags)


def _render_field_value(field_name: str, value: Any) -> str:
    """Render regular values safely and expand explicit ``{#字段}`` fields."""
    if field_name.startswith("#"):
        return _render_hashtag_values(value)
    # A submitter cannot inject Telegram HTML into an administrator's template.
    return html.escape(str(value or "").strip())


def _render_template(template: dict[str, Any], values: dict[str, Any]) -> str:
    source = str(template.get("body_html") or template.get("body_plain") or "")
    return PLACEHOLDER_PATTERN.sub(
        lambda match: _render_field_value(match.group(1), values.get(match.group(1), "")),
        source,
    )


CONTACT_USERNAME_PATTERN = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})")


def _render_greeting_text(template: str, values: dict[str, Any]) -> str:
    """Render optional greeting placeholders before adding them to a t.me URL."""
    return PLACEHOLDER_PATTERN.sub(
        lambda match: str(values.get(match.group(1), "") or "").strip(),
        str(template or ""),
    ).strip()[:500]


def _link_contacts_with_greeting(
    html_text: str,
    greeting_template: str,
    values: dict[str, Any],
) -> str:
    """Turn plain @contacts into private-chat links with a prefilled greeting."""
    greeting = _render_greeting_text(greeting_template, values)
    if not greeting:
        return html_text

    def replace(match: re.Match[str]) -> str:
        username = match.group(1)
        url = "https://t.me/" + username + "?text=" + quote(greeting, safe="")
        return f'<a href="{html.escape(url, quote=True)}">@{username}</a>'

    return CONTACT_USERNAME_PATTERN.sub(replace, html_text)


def _plain_preview(html_text: str) -> str:
    """A short human-readable fallback for log/review headers."""
    value = re.sub(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", r"\1", html_text, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def _caption_length(html_text: str) -> int:
    return len(_plain_preview(html_text))


def _rendered_text_too_long(template: dict[str, Any], values: dict[str, Any]) -> bool:
    """Keep the generated body below the safe media-caption budget."""
    return _caption_length(_render_template(template, values)) > MAX_RENDERED_TEXT


def _is_multi_value_field(template: dict[str, Any], field_name: str) -> bool:
    # Deliberately strict: only a # inside the braces enables multi-value
    # mode, i.e. ``{#}`` and ``{#标签}``.
    return field_name.startswith("#")


def _field_prompt(template: dict[str, Any], field_name: str) -> str:
    if _is_multi_value_field(template, field_name):
        display_name = field_name.lstrip("#") or "标签"
        return (
            f"请填写「{display_name}」：\n"
            "该字段支持多个值，请用空格隔开，例如：上海 杭州\n"
            "机器人会自动生成 #上海 #杭州。"
        )
    return f"请填写「{field_name}」："


def _channel_id(config: dict[str, Any]) -> Optional[int]:
    value = config.get("channel_id")
    if value in (None, ""):
        # Reuse the already configured 投稿发布频道 as a practical default.
        value = load_publish_config().get("channel_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _touch(submission: dict[str, Any]) -> None:
    submission["updated_at"] = int(time.time())


def _expired(submission: dict[str, Any]) -> bool:
    if submission.get("status") not in DRAFT_STATUSES:
        return False
    return int(time.time()) - int(submission.get("updated_at", 0) or 0) > DRAFT_TIMEOUT_SECONDS


def _expire_if_needed(records: dict[str, Any], submission: dict[str, Any]) -> bool:
    if not _expired(submission):
        return False
    submission["status"] = "expired"
    submission["expired_at"] = int(time.time())
    _touch(submission)
    return True


def _find_active_submission(records: dict[str, Any], user_id: int) -> Optional[dict[str, Any]]:
    candidates = [
        item
        for item in records.values()
        if isinstance(item, dict)
        and int(item.get("user_id", 0) or 0) == int(user_id)
        and item.get("status") in DRAFT_STATUSES
    ]
    return max(candidates, key=lambda item: int(item.get("updated_at", 0) or 0), default=None)


def _template_picker(config: dict[str, Any], *, prefix: str = "rank:choose") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(str(template.get("name", "未命名模板"))[:64], callback_data=f"{prefix}:{template['id']}")]
        for template in _templates(config)
    ]
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="rank:cancel")])
    return InlineKeyboardMarkup(rows)


def _cancel_markup(submission_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消本次投稿", callback_data=f"rank:cancel:{submission_id}")]])


def _media_upload_markup(submission_id: str, *, can_switch_template: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_switch_template:
        rows.append([InlineKeyboardButton("🔄 切换模板", callback_data=f"rank:switch:{submission_id}")])
    rows.extend(
        [
            [InlineKeyboardButton("✅ 完成媒体上传", callback_data=f"rank:media_done:{submission_id}")],
            [InlineKeyboardButton("❌ 取消", callback_data=f"rank:cancel:{submission_id}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def _confirm_markup(submission_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 确定提交", callback_data=f"rank:confirm:{submission_id}")],
            [InlineKeyboardButton("❌ 取消", callback_data=f"rank:cancel:{submission_id}")],
        ]
    )


def _review_markup(submission_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ 同意", callback_data=f"rank:approve:{submission_id}"),
            InlineKeyboardButton("❌ 拒绝", callback_data=f"rank:reject:{submission_id}"),
        ]]
    )


def _config_markup(config: dict[str, Any]) -> InlineKeyboardMarkup:
    channel = _channel_id(config)
    greeting_set = bool(str(config.get("greeting_text") or "").strip())
    rows = [
        [InlineKeyboardButton("➕ 新增模板", callback_data="rank:template:add")],
        [InlineKeyboardButton(f"📣 发布频道：{channel if channel else '未设置'}", callback_data="rank:channel")],
        [InlineKeyboardButton(
            f"💬 联系打招呼词：{'已设置' if greeting_set else '未设置'}",
            callback_data="rank:greeting",
        )],
    ]
    for template in _templates(config):
        rows.append([InlineKeyboardButton(f"🧩 {template.get('name', '未命名模板')}"[:64], callback_data=f"rank:template:open:{template['id']}")])
    rows.append([InlineKeyboardButton("⬅️ 返回首页", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _template_detail_markup(template_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ 编辑名称和内容", callback_data=f"rank:template:edit:{template_id}")],
            [InlineKeyboardButton("🗑 删除模板", callback_data=f"rank:template:delete:{template_id}")],
            [InlineKeyboardButton("⬅️ 返回模板列表", callback_data="rank:templates")],
        ]
    )


def _template_summary(template: dict[str, Any]) -> str:
    fields = _template_fields(template)
    preview = _plain_preview(str(template.get("body_html") or ""))
    if len(preview) > 800:
        preview = preview[:800] + "…"
    return (
        f"🧩 <b>{html.escape(str(template.get('name') or '未命名模板'))}</b>\n\n"
        f"动态字段：{'、'.join(html.escape(field) for field in fields) if fields else '无'}\n\n"
        f"<b>模板预览</b>\n{str(template.get('body_html') or '')}\n\n"
        "模板正文可使用 <code>{字段名}</code>；Telegram 自定义表情和富文本会被保留。\n"
        "使用 <code>{#标签}</code> 或 <code>{#}</code>，可让用户用空格填写多个值，"
        "发布时会自动转为多个 #标签。"
    )


def _reviewer_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    ids = set(get_delegated_admin_ids(context, "submission_review"))
    ids.update(get_delegated_admin_ids(context, "submission_config"))
    try:
        owner = int(context.application.bot_data.get("owner_id"))
        ids.add(owner)
    except (TypeError, ValueError, AttributeError):
        pass
    return sorted(ids)


async def _reply_or_edit(query, text: str, **kwargs):
    if query and query.message:
        try:
            return await query.edit_message_text(text, **kwargs)
        except BadRequest as exc:
            # Telegram rejects an edit with identical text; that is not an
            # application failure and the callback still needs answering.
            if "Message is not modified" not in str(exc):
                raise
    return None


async def _show_submission_prompt(message, submission: dict[str, Any], config: dict[str, Any]) -> None:
    status = submission.get("status")
    sid = str(submission["id"])
    if status == "select_template":
        await message.reply_text("请选择上榜模板：", reply_markup=_template_picker(config))
    elif status == "collecting_media":
        count = len(submission.get("media", []))
        await message.reply_text(
            f"请上传图片或视频（已收到 {count}/{MAX_MEDIA} 个）。\n"
            "上传完成后点击“完成媒体上传”。",
            reply_markup=_media_upload_markup(
                sid,
                can_switch_template=len(_templates(config)) > 1,
            ),
        )
    elif status == "collecting_fields":
        template = _find_template(config, str(submission.get("template_id")))
        fields = _template_fields(template or {})
        index = int(submission.get("field_index", 0) or 0)
        if index < len(fields):
            await message.reply_text(
                _field_prompt(template or {}, fields[index]) + f"\n（最多 {MAX_FIELD_VALUE} 个字符，发送“取消”可退出）",
                reply_markup=_cancel_markup(sid),
            )


async def _copy_media_with_caption(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media: list[dict[str, Any]],
    caption: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> list[int]:
    """Copy the original media and put generated content in its first caption.

    Telegram stores the caption of an album on its first item.  Copying an
    album first preserves its grouping; editing that first caption then keeps
    the media and generated text in the *same* channel post.
    """
    if not media:
        raise ValueError("投稿至少需要一个图片或视频。")
    if _caption_length(caption) > MAX_CAPTION_LENGTH:
        raise ValueError(f"生成内容超过 Telegram 媒体说明上限（{MAX_CAPTION_LENGTH} 个字符）。")

    copied = None
    same_source = len({int(item["chat_id"]) for item in media}) == 1
    if same_source and hasattr(context.bot, "copy_messages"):
        try:
            copied = await context.bot.copy_messages(
                chat_id=chat_id,
                from_chat_id=int(media[0]["chat_id"]),
                message_ids=[int(item["message_id"]) for item in media],
            )
        except (BadRequest, Forbidden):
            copied = None

    if copied is None:
        copied = []
        for item in media:
            copied.append(
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=int(item["chat_id"]),
                    message_id=int(item["message_id"]),
                )
            )

    values = copied if isinstance(copied, (list, tuple)) else [copied]
    message_ids = [
        int(getattr(item, "message_id", getattr(item, "id", 0)) or 0)
        for item in values
    ]
    message_ids = [message_id for message_id in message_ids if message_id > 0]
    if not message_ids:
        raise RuntimeError("媒体复制成功但未返回消息 ID。")

    await context.bot.edit_message_caption(
        chat_id=chat_id,
        message_id=message_ids[0],
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    return message_ids


async def _send_preview(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict[str, Any],
    template: dict[str, Any],
) -> None:
    rendered = _render_template(template, submission.get("fields", {}))
    caption = rendered + "\n\n<i>📝 投稿预览</i>"
    # Some Telegram clients do not reliably refresh an inline keyboard added
    # through editMessageCaption after a copied media album. Keep the actual
    # visual preview on the media, and send the confirmation keyboard as a
    # dedicated message so both normal users and administrators always get a
    # visible submit button.
    await _copy_media_with_caption(
        chat_id,
        context,
        submission.get("media", []),
        caption,
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="📝 投稿预览已生成，请确认是否提交：",
        reply_markup=_confirm_markup(str(submission["id"])),
    )


async def _send_for_review(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict[str, Any],
    template: dict[str, Any],
) -> int:
    rendered = _render_template(template, submission.get("fields", {}))
    user = submission.get("user", {}) if isinstance(submission.get("user"), dict) else {}
    identity = html.escape(str(user.get("full_name") or "用户"))
    username = str(user.get("username") or "").strip().lstrip("@")
    sender = f"<b>📥 待审核投稿</b>\n投稿人：{identity}"
    if username:
        sender += f" · @{html.escape(username)}"
    caption = sender + "\n\n" + rendered
    if _caption_length(caption) > MAX_CAPTION_LENGTH:
        # This should be prevented by the earlier input guard, but retain a
        # clear failure for old drafts created before that guard existed.
        raise ValueError("投稿内容过长，无法作为媒体说明发送审核。")

    delivered = 0
    for admin_id in _reviewer_ids(context):
        try:
            # Albums do not consistently show an inline keyboard attached via
            # editMessageCaption across Telegram clients. Keep the media and
            # generated caption together, then send a dedicated review-action
            # message so every reviewer can always see 同意 / 拒绝.
            await _copy_media_with_caption(
                admin_id,
                context,
                submission.get("media", []),
                caption,
            )
            await context.bot.send_message(
                chat_id=admin_id,
                text="📥 请审核这条上榜投稿：",
                reply_markup=_review_markup(str(submission["id"])),
            )
            delivered += 1
        except (Forbidden, BadRequest) as exc:
            print(f"上榜投稿审核通知发送失败 admin={admin_id}: {exc}")
    return delivered


async def _build_published_caption(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict[str, Any],
    template: dict[str, Any],
    rank_config: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str, str]:
    """Build the caption and its durable report metadata before publication."""
    rendered_raw = _render_template(template, submission.get("fields", {}))
    publish_config = load_publish_config()
    # The legacy report/index pipeline extracts keyword fields from message
    # text/caption. A lightweight source object lets ranking templates use the
    # same rules, including the special ``#`` tag extractor.
    source = SimpleNamespace(text=None, caption=_plain_preview(rendered_raw))
    entries = _extract_routing_keywords(source, publish_config)
    # A ranking template may use a contact-field label that the legacy keyword
    # panel has not configured yet. Still use an @username in the generated
    # caption as the stable report ID instead of creating a random report per
    # publication.
    if not entries:
        match = re.search(r"@([A-Za-z0-9_]{5,32})", _plain_preview(rendered_raw))
        if match:
            raw = "@" + match.group(1)
            entries.append({
                "label": "联系方式",
                "key": match.group(1).lower(),
                "raw": raw,
            })
    report_id = _report_id_from_subject_entries(entries)
    report_url = ""
    try:
        report_url = await _report_deep_link(context, report_id) or ""
    except Exception as exc:
        print(f"上榜投稿生成报告链接失败: {exc}")

    # Contact @usernames become t.me links whose composer is prefilled with
    # the administrator-configured greeting. Keep report extraction above on
    # the original plain caption so the generated links do not affect it.
    caption = _link_contacts_with_greeting(
        rendered_raw,
        str(rank_config.get("greeting_text") or ""),
        submission.get("fields", {}),
    )
    if report_url:
        caption += f"\n\n📋 老师个人报告：{html.escape(report_url)}"
    if _caption_length(caption) > MAX_CAPTION_LENGTH:
        raise ValueError(
            f"生成内容加报告链接后超过媒体说明上限（{MAX_CAPTION_LENGTH} 个字符），请缩短模板或填写内容。"
        )
    return caption, entries, report_id, report_url


async def _publish_submission(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict[str, Any],
    template: dict[str, Any],
    config: dict[str, Any],
    submission_data: dict[str, Any],
) -> None:
    """Publish one captioned media post and checkpoint every side effect."""
    channel_id = _channel_id(config)
    if channel_id is None:
        raise ValueError("未设置上榜发布频道。请管理员先在“上榜模板”中设置频道。")
    publication = submission.setdefault("publication", {})
    if not isinstance(publication, dict):
        publication = {}
        submission["publication"] = publication

    caption, entries, report_id, report_url = await _build_published_caption(
        context, submission, template, config
    )
    if not publication.get("post_published", False):
        message_ids = await _copy_media_with_caption(
            channel_id,
            context,
            submission.get("media", []),
            caption,
        )
        publication.update(
            {
                "post_published": True,
                "media_published": True,
                "caption_published": True,
                "caption_html": caption,
                "message_ids": message_ids,
                "message_id": message_ids[0],
                "report_id": report_id,
                "report_url": report_url,
            }
        )
        _touch(submission)
        _save_submissions(submission_data)

    message_id = int(publication.get("message_id") or 0)
    if message_id <= 0:
        raise RuntimeError("已发布的上榜投稿缺少频道消息 ID。")

    # Keep the legacy “频道发帖写报告” switch effective for 上榜 posts too.
    # The comment entry must be embedded in the same media caption, because a
    # ranking post has no standalone text message.
    publish_config = load_publish_config()
    write_report_enabled = bool(
        publish_config.get("channel_post_write_report_enabled", False)
    ) and bool(publish_config.get("comment_forward_enabled", False))
    if write_report_enabled and not publication.get("write_report_link_added", False):
        # Prevent the channel_post listener from appending a second write-report
        # action when Telegram delivers this newly copied post.
        _mark_submission_write_report_link(context, channel_id, message_id)
        comment_url = await _comment_deep_link(context, channel_id, message_id)
        if comment_url:
            base_caption = str(publication.get("caption_html") or caption)
            write_report_caption = (
                base_caption.rstrip()
                + "\n\n"
                + f'<a href="{html.escape(comment_url, quote=True)}">✍️ 写报告</a>'
            )
            if _caption_length(write_report_caption) > MAX_CAPTION_LENGTH:
                raise ValueError(
                    f"生成内容加写报告入口后超过媒体说明上限（{MAX_CAPTION_LENGTH} 个字符），请缩短模板或填写内容。"
                )
            await context.bot.edit_message_caption(
                chat_id=channel_id,
                message_id=message_id,
                caption=write_report_caption,
                parse_mode=ParseMode.HTML,
            )
            publication["caption_html"] = write_report_caption
            publication["write_report_link_added"] = True
            publication["write_report_url"] = comment_url
            _touch(submission)
            _save_submissions(submission_data)

    if not publication.get("report_registered", False):
        _register_post_keywords(entries, channel_id, message_id)
        _create_comment_report(
            channel_id,
            message_id,
            str(publication.get("report_id") or report_id),
            subject_entries=entries,
        )
        publication["report_registered"] = True
        _touch(submission)
        _save_submissions(submission_data)

async def _begin_submission(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = query.from_user
    if not user:
        return
    config = _load_config()
    templates = _templates(config)
    if not templates:
        await query.answer("管理员尚未配置上榜模板。", show_alert=True)
        return
    data = _load_submissions()
    records = data["submissions"]
    # Do not restore unfinished drafts. Clicking “我要上榜” always starts a
    # clean new submission; old media/field drafts are discarded explicitly.
    stale_ids = [
        submission_id
        for submission_id, item in records.items()
        if isinstance(item, dict)
        and int(item.get("user_id", 0) or 0) == int(user.id)
        and item.get("status") in DRAFT_STATUSES
    ]
    for submission_id in stale_ids:
        records.pop(submission_id, None)
    if stale_ids:
        _save_submissions(data)

    sid = uuid.uuid4().hex[:12]
    now = int(time.time())
    only_template = templates[0] if len(templates) == 1 else None
    records[sid] = {
        "id": sid,
        "status": "collecting_media" if only_template else "select_template",
        "user_id": user.id,
        "user_chat_id": query.message.chat_id if query.message else user.id,
        "user": {"username": user.username or "", "full_name": user.full_name or ""},
        "template_id": str(only_template["id"]) if only_template else None,
        "media": [],
        "fields": {},
        "field_index": 0,
        "created_at": now,
        "updated_at": now,
    }
    _save_submissions(data)
    await query.answer()
    if only_template:
        return await _reply_or_edit(
            query,
            f"已使用模板：<b>{html.escape(str(only_template.get('name') or '未命名模板'))}</b>\n\n"
            f"请上传图片或视频，最多 {MAX_MEDIA} 个。上传完成后点击下方按钮。",
            parse_mode=ParseMode.HTML,
            reply_markup=_media_upload_markup(sid, can_switch_template=False),
        )
    await _reply_or_edit(query, "请选择本次投稿使用的上榜模板：", reply_markup=_template_picker(config))


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("rank:"):
        return
    if not _is_master_bot(context):
        await query.answer("该功能仅主机器人可用。", show_alert=True)
        return
    if query.message and str(getattr(query.message.chat, "type", "")) != "private":
        await query.answer("请先私聊主机器人，再使用上榜投稿功能。", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = query.from_user.id if query.from_user else None
    config = _load_config()

    if action == "start":
        return await _begin_submission(query, context)

    if action == "templates":
        if not _is_admin(context, user_id):
            return await query.answer("仅管理员可管理上榜模板。", show_alert=True)
        await query.answer()
        return await _reply_or_edit(
            query,
            "<b>🧩 上榜模板</b>\n\n"
            "在此管理多个上榜模板、模板占位符、最终发布频道和联系打招呼词。\n"
            "模板正文支持 Telegram 自定义表情、富文本及 <code>{字段名}</code> 占位符。",
            parse_mode=ParseMode.HTML,
            reply_markup=_config_markup(config),
        )

    if action == "template":
        if not _is_admin(context, user_id):
            return await query.answer("仅管理员可管理上榜模板。", show_alert=True)
        sub_action = parts[2] if len(parts) > 2 else ""
        if sub_action == "add":
            context.user_data["rank_admin_draft"] = {"step": "name"}
            await query.answer()
            return await _reply_or_edit(query, "请发送新模板名称（最多 50 个字符）：")
        if sub_action == "open" and len(parts) == 4:
            template = _find_template(config, parts[3])
            if not template:
                return await query.answer("模板不存在或已删除。", show_alert=True)
            await query.answer()
            return await _reply_or_edit(
                query,
                _template_summary(template),
                parse_mode=ParseMode.HTML,
                reply_markup=_template_detail_markup(str(template["id"])),
            )
        if sub_action == "edit" and len(parts) == 4:
            template = _find_template(config, parts[3])
            if not template:
                return await query.answer("模板不存在或已删除。", show_alert=True)
            context.user_data["rank_admin_draft"] = {"step": "edit_name", "template_id": str(template["id"])}
            await query.answer()
            return await _reply_or_edit(query, "请发送新的模板名称（最多 50 个字符）：")
        if sub_action == "delete" and len(parts) == 4:
            template = _find_template(config, parts[3])
            if not template:
                return await query.answer("模板不存在或已删除。", show_alert=True)
            config["templates"] = [item for item in _templates(config) if str(item.get("id")) != parts[3]]
            _save_config(config)
            await query.answer("模板已删除。")
            return await _reply_or_edit(query, "✅ 模板已删除。", reply_markup=_config_markup(config))

    if action == "channel":
        if not _is_admin(context, user_id):
            return await query.answer("仅管理员可设置发布频道。", show_alert=True)
        context.user_data["rank_admin_draft"] = {"step": "channel"}
        await query.answer()
        return await _reply_or_edit(
            query,
            "请发送发布频道 ID，例如 <code>-1001234567890</code>。\n"
            "机器人必须已加入频道并拥有发消息权限。发送“取消”放弃修改。",
            parse_mode=ParseMode.HTML,
        )

    if action == "greeting":
        if not _is_admin(context, user_id):
            return await query.answer("仅管理员可设置打招呼词。", show_alert=True)
        context.user_data["rank_admin_draft"] = {"step": "greeting"}
        current = str(config.get("greeting_text") or "").strip()
        await query.answer()
        return await _reply_or_edit(
            query,
            "请发送用户点击 @联系方式 时自动带入的打招呼词。\n"
            "可使用模板字段，例如：你好，我想咨询一下 {艺名}。\n"
            "发送“无”可清空。\n\n"
            + (f"当前：{html.escape(current)}" if current else "当前：未设置"),
            parse_mode=ParseMode.HTML,
        )

    if action == "choose" and len(parts) == 3:
        data = _load_submissions()
        submission = _find_active_submission(data["submissions"], int(user_id or 0))
        template = _find_template(config, parts[2])
        if not submission or submission.get("status") != "select_template":
            return await query.answer("投稿已失效，请重新点击“我要上榜”。", show_alert=True)
        if not template:
            return await query.answer("该模板已被删除，请重新选择。", show_alert=True)
        submission["template_id"] = str(template["id"])
        submission["status"] = "collecting_media"
        _touch(submission)
        _save_submissions(data)
        await query.answer()
        return await _reply_or_edit(
            query,
            f"已选择模板：<b>{html.escape(str(template.get('name') or '未命名模板'))}</b>\n\n"
            f"请上传图片或视频，最多 {MAX_MEDIA} 个。上传完成后点击下方按钮。",
            parse_mode=ParseMode.HTML,
            reply_markup=_media_upload_markup(
                str(submission["id"]),
                can_switch_template=len(_templates(config)) > 1,
            ),
        )

    if action == "switch" and len(parts) == 3:
        data = _load_submissions()
        submission = data["submissions"].get(parts[2])
        if not isinstance(submission, dict) or int(submission.get("user_id", 0) or 0) != int(user_id or 0):
            return await query.answer("只能操作自己的投稿。", show_alert=True)
        if submission.get("status") not in {"collecting_media", "collecting_fields", "awaiting_confirmation"}:
            return await query.answer("当前不能切换模板。", show_alert=True)
        # A template's fields are structurally tied to the submitted values.
        # Starting the selection over avoids mismatched fields/media previews.
        submission.update({"status": "select_template", "template_id": None, "media": [], "fields": {}, "field_index": 0})
        _touch(submission)
        _save_submissions(data)
        await query.answer("已清空本次草稿，请重新选择模板。")
        return await _reply_or_edit(query, "请选择新的上榜模板：", reply_markup=_template_picker(config))

    if action in {"media_done", "confirm", "cancel", "approve", "reject"}:
        sid = parts[2] if len(parts) == 3 else ""
        data = _load_submissions()
        # A generic cancel button is used by the initial picker, before an id
        # is included in callback data.
        submission = (
            _find_active_submission(data["submissions"], int(user_id or 0))
            if action == "cancel" and not sid
            else data["submissions"].get(sid)
        )
        if not isinstance(submission, dict):
            return await query.answer("投稿不存在或已清理。", show_alert=True)
        if _expire_if_needed(data["submissions"], submission):
            _save_submissions(data)
            return await query.answer("本次投稿已超时，请重新发起。", show_alert=True)

        if action == "cancel":
            if int(submission.get("user_id", 0) or 0) != int(user_id or 0):
                return await query.answer("只能取消自己的投稿。", show_alert=True)
            if submission.get("status") not in DRAFT_STATUSES:
                return await query.answer("该投稿已进入审核或已结束，不能取消。", show_alert=True)
            submission["status"] = "cancelled"
            _touch(submission)
            _save_submissions(data)
            await query.answer("已取消。")
            return await _reply_or_edit(query, "已取消本次上榜投稿。")

        if action == "media_done":
            if int(submission.get("user_id", 0) or 0) != int(user_id or 0):
                return await query.answer("只能操作自己的投稿。", show_alert=True)
            if submission.get("status") != "collecting_media":
                return await query.answer("当前不在媒体上传阶段。", show_alert=True)
            if not submission.get("media"):
                return await query.answer("请至少上传一个图片或视频。", show_alert=True)
            template = _find_template(config, str(submission.get("template_id")))
            if not template:
                return await query.answer("模板已删除，请重新发起投稿。", show_alert=True)
            fields = _template_fields(template)
            submission["field_index"] = 0
            submission["status"] = "collecting_fields" if fields else "awaiting_confirmation"
            _touch(submission)
            _save_submissions(data)
            await query.answer()
            if fields:
                return await _reply_or_edit(
                    query,
                    _field_prompt(template, fields[0]) + f"\n（最多 {MAX_FIELD_VALUE} 个字符）",
                    reply_markup=_cancel_markup(sid),
                )
            await _reply_or_edit(query, "模板没有动态字段，正在生成预览…")
            return await _send_preview(query.message.chat_id, context, submission, template)

        if action == "confirm":
            if int(submission.get("user_id", 0) or 0) != int(user_id or 0):
                return await query.answer("只能确认自己的投稿。", show_alert=True)
            if submission.get("status") != "awaiting_confirmation":
                return await query.answer("该投稿已提交，请不要重复操作。", show_alert=True)
            template = _find_template(config, str(submission.get("template_id")))
            if not template:
                return await query.answer("模板已删除，无法提交。", show_alert=True)
            # Persist the lock before network calls. Repeated clicks and bot
            # restarts cannot create duplicate review tasks/publications.
            submission["status"] = "publishing" if _is_admin(context, user_id) else "pending_review"
            _touch(submission)
            _save_submissions(data)
            await query.answer("正在处理，请稍候…")
            if _is_admin(context, user_id):
                try:
                    await _publish_submission(context, submission, template, config, data)
                except Exception as exc:
                    submission["status"] = "awaiting_confirmation"
                    submission["last_error"] = str(exc)[:500]
                    _touch(submission)
                    _save_submissions(data)
                    return await _reply_or_edit(query, f"❌ 发布失败：{html.escape(str(exc)[:300])}\n请修正后重新确认。", parse_mode=ParseMode.HTML)
                submission["status"] = "published"
                submission["published_at"] = int(time.time())
                _touch(submission)
                _save_submissions(data)
                return await _reply_or_edit(query, "✅ 已直接发布到频道。")
            try:
                delivered = await _send_for_review(context, submission, template)
                if not delivered:
                    raise RuntimeError("没有可接收审核消息的管理员")
            except Exception as exc:
                submission["status"] = "awaiting_confirmation"
                submission["last_error"] = str(exc)[:500]
                _touch(submission)
                _save_submissions(data)
                return await _reply_or_edit(query, f"❌ 提交审核失败：{html.escape(str(exc)[:300])}\n请稍后重新确认。", parse_mode=ParseMode.HTML)
            submission["review_sent_at"] = int(time.time())
            _touch(submission)
            _save_submissions(data)
            return await _reply_or_edit(query, "🕒 投稿已提交给管理员审核，请耐心等待。")

        # Review buttons can only be used by an administrator. The status
        # transition is persisted first, making approve/reject idempotent.
        if action in {"approve", "reject"}:
            if not _is_admin(context, user_id):
                return await query.answer("仅管理员可审核投稿。", show_alert=True)
            if submission.get("status") != "pending_review":
                return await query.answer("该投稿已处理，请不要重复审核。", show_alert=True)
            if action == "reject":
                submission["status"] = "rejected"
                submission["reviewed_by"] = int(user_id)
                submission["reviewed_at"] = int(time.time())
                _touch(submission)
                _save_submissions(data)
                await query.answer("已拒绝。")
                try:
                    await context.bot.send_message(submission["user_chat_id"], "❌ 你的上榜投稿未通过审核。")
                except (Forbidden, BadRequest):
                    pass
                return await _reply_or_edit(query, "❌ 已拒绝本次投稿。")

            template = _find_template(config, str(submission.get("template_id")))
            if not template:
                return await query.answer("对应模板已删除，无法发布。", show_alert=True)
            submission["status"] = "publishing"
            submission["reviewed_by"] = int(user_id)
            submission["reviewed_at"] = int(time.time())
            _touch(submission)
            _save_submissions(data)
            await query.answer("正在发布…")
            try:
                await _publish_submission(context, submission, template, config, data)
            except Exception as exc:
                # Return to pending review so an admin can retry after fixing
                # channel permissions/configuration.
                submission["status"] = "pending_review"
                submission["last_error"] = str(exc)[:500]
                _touch(submission)
                _save_submissions(data)
                return await _reply_or_edit(query, f"❌ 发布失败：{html.escape(str(exc)[:300])}\n可在修正后再次点击“同意”。", parse_mode=ParseMode.HTML, reply_markup=_review_markup(sid))
            submission["status"] = "published"
            submission["published_at"] = int(time.time())
            _touch(submission)
            _save_submissions(data)
            try:
                await context.bot.send_message(submission["user_chat_id"], "✅ 你的上榜投稿已审核通过并发布。")
            except (Forbidden, BadRequest):
                pass
            return await _reply_or_edit(query, "✅ 已审核通过并发布到频道。")


async def _handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    draft = context.user_data.get("rank_admin_draft")
    if not draft or not message or not message.text:
        return False
    if not _is_master_bot(context) or not _is_admin(context, message.from_user.id if message.from_user else None):
        context.user_data.pop("rank_admin_draft", None)
        return False
    text = message.text.strip()
    if text in {"取消", "返回", "/cancel"}:
        context.user_data.pop("rank_admin_draft", None)
        await message.reply_text("已取消上榜模板编辑。")
        return True

    step = draft.get("step")
    if step == "channel":
        try:
            channel_id = int(text)
        except (TypeError, ValueError):
            await message.reply_text("❗ 频道 ID 格式错误，例如：-1001234567890")
            return True
        try:
            chat = await context.bot.get_chat(channel_id)
            if str(getattr(chat, "type", "")) != "channel":
                await message.reply_text("❗ 该 ID 不是频道，请重新输入频道 ID。")
                return True
        except (BadRequest, Forbidden) as exc:
            await message.reply_text(f"❗ 无法访问该频道：{str(exc)[:150]}。请确认机器人已加入频道。")
            return True
        config = _load_config()
        config["channel_id"] = channel_id
        _save_config(config)
        context.user_data.pop("rank_admin_draft", None)
        await message.reply_text("✅ 上榜发布频道已保存。", reply_markup=_config_markup(config))
        return True

    if step == "greeting":
        if len(text) > 500:
            await message.reply_text("❗ 打招呼词不能超过 500 个字符。")
            return True
        config = _load_config()
        config["greeting_text"] = "" if text in {"无", "-", "清空"} else text
        _save_config(config)
        context.user_data.pop("rank_admin_draft", None)
        await message.reply_text(
            "✅ 已保存联系打招呼词。" if config["greeting_text"] else "✅ 已清空联系打招呼词。",
            reply_markup=_config_markup(config),
        )
        return True

    if step in {"name", "edit_name"}:
        if not text or len(text) > 50:
            await message.reply_text("❗ 模板名称不能为空且不能超过 50 个字符。")
            return True
        draft["name"] = text
        draft["step"] = "body" if step == "name" else "edit_body"
        await message.reply_text(
            "请发送模板正文。支持 <code>{姓名}</code> 等动态字段；"
            "可直接发送 Telegram 自定义表情和富文本。\n"
            "如需多个标签，请使用 <code>{#标签}</code> 或 <code>{#}</code>；"
            "用户可用空格输入多个值，发布时会自动加上 #。\n\n"
            f"模板正文最多 {MAX_TEMPLATE_TEXT} 个字符（上榜内容会作为媒体说明发布）。",
            parse_mode=ParseMode.HTML,
        )
        return True

    if step in {"body", "edit_body"}:
        raw = str(message.text or "")
        if not raw.strip() or len(raw) > MAX_TEMPLATE_TEXT:
            await message.reply_text(f"❗ 模板正文不能为空且不能超过 {MAX_TEMPLATE_TEXT} 个字符。")
            return True
        body_html = _safe_template_html(message)
        fields = _template_fields({"body_html": body_html})
        if len(fields) > 30:
            await message.reply_text("❗ 占位符最多 30 个，请精简模板。")
            return True
        config = _load_config()
        if step == "body":
            template = {
                "id": uuid.uuid4().hex[:10],
                "name": draft["name"],
                "body_html": body_html,
                "body_plain": raw,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
            config["templates"].append(template)
            result = "✅ 上榜模板已新增。"
        else:
            template = _find_template(config, str(draft.get("template_id")))
            if not template:
                context.user_data.pop("rank_admin_draft", None)
                await message.reply_text("❗ 模板已被删除，无法保存。")
                return True
            template.update({"name": draft["name"], "body_html": body_html, "body_plain": raw, "updated_at": int(time.time())})
            result = "✅ 上榜模板已更新。"
        _save_config(config)
        context.user_data.pop("rank_admin_draft", None)
        await message.reply_text(result + "\n\n" + _template_summary(template), parse_mode=ParseMode.HTML, reply_markup=_config_markup(config))
        return True
    return False


async def _handle_submission_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not (message.photo or message.video):
        return False
    if not _is_master_bot(context) or not message.from_user:
        return False
    data = _load_submissions()
    submission = _find_active_submission(data["submissions"], message.from_user.id)
    if not submission:
        return False
    if _expire_if_needed(data["submissions"], submission):
        _save_submissions(data)
        await message.reply_text("⌛ 本次上榜投稿已超时，请重新点击“我要上榜”。")
        return True
    if submission.get("status") != "collecting_media":
        return False
    media = submission.setdefault("media", [])
    if len(media) >= MAX_MEDIA:
        await message.reply_text(f"❗ 最多只能上传 {MAX_MEDIA} 个媒体文件，请点击“完成媒体上传”。")
        return True
    media.append({
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "kind": "video" if message.video else "photo",
        "media_group_id": message.media_group_id or "",
    })
    _touch(submission)
    _save_submissions(data)
    await message.reply_text(
        f"✅ 已收到 {len(media)}/{MAX_MEDIA} 个媒体文件。",
        reply_markup=_media_upload_markup(
            str(submission["id"]),
            can_switch_template=len(_templates(_load_config())) > 1,
        ),
    )
    return True


async def _handle_submission_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not message.text or not message.from_user or not _is_master_bot(context):
        return False
    data = _load_submissions()
    submission = _find_active_submission(data["submissions"], message.from_user.id)
    if not submission:
        return False
    if _expire_if_needed(data["submissions"], submission):
        _save_submissions(data)
        await message.reply_text("⌛ 本次上榜投稿已超时，请重新点击“我要上榜”。")
        return True
    if submission.get("status") != "collecting_fields":
        return False
    text = message.text.strip()
    if text in {"取消", "返回", "/cancel"}:
        submission["status"] = "cancelled"
        _touch(submission)
        _save_submissions(data)
        await message.reply_text("已取消本次上榜投稿。")
        return True
    if len(text) > MAX_FIELD_VALUE:
        await message.reply_text(f"❗ 内容不能超过 {MAX_FIELD_VALUE} 个字符，请重新发送。")
        return True
    config = _load_config()
    template = _find_template(config, str(submission.get("template_id")))
    if not template:
        submission["status"] = "cancelled"
        _touch(submission)
        _save_submissions(data)
        await message.reply_text("❗ 所选模板已被删除，本次投稿已取消。")
        return True
    fields = _template_fields(template)
    index = int(submission.get("field_index", 0) or 0)
    if index >= len(fields):
        return False
    field_values = submission.setdefault("fields", {})
    field_name = fields[index]
    field_values[field_name] = text
    if _rendered_text_too_long(template, field_values):
        field_values.pop(field_name, None)
        await message.reply_text(
            f"❗ 填入后生成内容会超过 Telegram 长度限制（{MAX_RENDERED_TEXT} 个字符），"
            "请缩短本字段后重新发送。"
        )
        return True
    index += 1
    submission["field_index"] = index
    submission["status"] = "awaiting_confirmation" if index >= len(fields) else "collecting_fields"
    _touch(submission)
    _save_submissions(data)
    if index < len(fields):
        await message.reply_text(_field_prompt(template or {}, fields[index]), reply_markup=_cancel_markup(str(submission["id"])))
    else:
        await _send_preview(message.chat_id, context, submission, template)
    return True


async def _private_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Order matters: admin template editing must get first claim over the
    # ordinary field collector. Both calls return False for unrelated messages.
    if await _handle_admin_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_submission_media(update, context):
        raise ApplicationHandlerStop
    if await _handle_submission_field(update, context):
        raise ApplicationHandlerStop


def register_ranking_submission_handlers(app) -> None:
    """Register independent state handlers before generic private forwarding."""
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern=r"^rank:"), group=-19)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (~filters.COMMAND) & ~filters.UpdateType.BUSINESS_MESSAGE,
            _private_interceptor,
        ),
        group=-19,
    )
