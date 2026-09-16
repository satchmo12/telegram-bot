# -*- coding: utf-8 -*-
from datetime import datetime
from types import SimpleNamespace
import html
from typing import Optional
from urllib.parse import urlparse
import os
import random
import re
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, TypeHandler, filters
from telegram.error import BadRequest, Forbidden

from channel.channel_config import USER_MESSAGE_FILE
from utils import BOT_USER_FILE, _can_manage, is_super_admin, load_json, save_json
from admin_permissions import get_delegated_admin_ids, has_admin_permission

PUBLISH_CONFIG_FILE = "config_data/publish_config.json"
ANON_CHAT_FILE = "data/anon_chat.json"
USER_MESSAGE_FILE = "data/user_message_file.json"
BOTTLE_HISTORY_FILE = "data/bottle_history.json"
PENDING_SUBMISSIONS_FILE = "data/pending_submissions.json"

CALLBACK_PREFIX = "publish"
BUTTON_TEXT_MAX_LENGTH = 64
MAX_PUBLISH_BUTTONS = 20
PENDING_PROOF_KEY = "publish_pending_proof_id"
REJECT_REASON_KEY = "publish_reject_reason"
COMMENT_TARGET_KEY = "publish_comment_target"
COMMENT_MAP_FILE = "data/publish_comment_map.json"
KEYWORD_MAP_FILE = "data/publish_keyword_map.json"
COMMENT_REPORTS_FILE = "data/comment_reports.json"
KEYWORD_INPUT_KEY = "publish_keyword_search"
KEYWORD_RESULTS_KEY = "publish_keyword_results"
KEYWORD_LABEL_INPUT_KEY = "publish_keyword_label_input"
REPORT_PAGE_SIZE = 6
TEMPLATE_DRAFT_KEY = "publish_template_draft"
TEMPLATE_FLOW_KEY = "publish_template_flow"
TEMPLATE_KEY_PATTERN = re.compile(r"\{([\w\-一-鿿]+)\}")

# =========================
# 配置读写
# =========================

def load_publish_config():
    default = {
        "channel_id": None,
        "review_enabled": False,
        "daily_limit": 0,
        "ads_enabled": False,
        "ads": [],
        "buttons": [],
        # Keep existing bots' public buttons visible until an owner changes
        # this setting in 投稿配置.
        "bottom_buttons_enabled": True,
        # Require an extra proof message before a non-owner submission is sent
        # to the reviewer. It only serves as review evidence and is never posted.
        "proof_required": False,
        # When enabled, reviewer must type a rejection reason for the submitter.
        "reject_reason_required": False,
        # Comments are submitted through review and mirrored to forward_channel_id.
        "comment_forward_enabled": False,
        "forward_channel_id": None,
        # Generate a public Telegram deep link for comments under admin posts.
        "report_link_enabled": False,
        # Structured template publishing for administrators.
        "template_publish_enabled": False,
        "publish_templates": [],
        # Labels such as 艺名 / 联系方式 used to extract routing keywords.
        "keyword_extract_labels": [],
        # Preserve the existing behavior: one 投稿 action can send multiple posts.
        "continuous_submission_enabled": True,
        # Visibility of normal start-panel buttons controlled by the owner.
        "custom_menu_buttons": {
            "channel_clone": True,
            "telethon_manage": True,
            "bot_channel_config": True,
            "group_config": True,
            "global_ad_config": True,
        },
        "submission_enabled": False,
        "random_view_enabled": False,
    }

    data = load_json(PUBLISH_CONFIG_FILE)
    if not isinstance(data, dict) or not data:
        data = default.copy()
        save_json(PUBLISH_CONFIG_FILE, data)
        return data

    changed = False
    for key, value in default.items():
        if key not in data:
            data[key] = value.copy() if isinstance(value, (list, dict)) else value
            changed = True
    if changed:
        save_json(PUBLISH_CONFIG_FILE, data)

    return data


def save_publish_config(data):
    save_json(PUBLISH_CONFIG_FILE, data)


def _comment_map_key(channel_id, message_id) -> str:
    return f"{channel_id}:{message_id}"


def _load_comment_map() -> dict:
    data = load_json(COMMENT_MAP_FILE)
    return data if isinstance(data, dict) else {}


def _save_comment_map(data: dict) -> None:
    # Keep the mapping bounded; old channel posts no longer need new comments.
    if len(data) > 3000:
        oldest = sorted(
            data.items(),
            key=lambda item: int((item[1] or {}).get("created_at", 0) or 0),
        )[: len(data) - 3000]
        for key, _ in oldest:
            data.pop(key, None)
    save_json(COMMENT_MAP_FILE, data)


def _load_comment_reports() -> dict:
    data = load_json(COMMENT_REPORTS_FILE)
    if not isinstance(data, dict):
        data = {}
    reports = data.get("reports")
    if not isinstance(reports, dict):
        reports = {}
    data["reports"] = reports
    return data


def _save_comment_reports(data: dict) -> None:
    reports = data.get("reports", {}) if isinstance(data, dict) else {}
    if isinstance(reports, dict) and len(reports) > 3000:
        expired = sorted(
            reports,
            key=lambda key: int((reports.get(key) or {}).get("created_at", 0) or 0),
        )[: len(reports) - 3000]
        for key in expired:
            reports.pop(key, None)
    save_json(COMMENT_REPORTS_FILE, data)


def _create_comment_report(
    channel_id: int, message_id: int, report_id: str, subject_entries: Optional[list[dict]] = None
) -> None:
    data = _load_comment_reports()
    subjects = []
    for entry in subject_entries or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        value = str(entry.get("raw") or entry.get("key") or "").strip()
        if value and not any(item.get("label") == label and item.get("value") == value for item in subjects):
            subjects.append({"label": label, "value": value})
    data["reports"][report_id] = {
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "created_at": int(time.time()),
        "subjects": subjects,
        "comments": [],
    }
    _save_comment_reports(data)


def _get_comment_report(report_id: str):
    report = _load_comment_reports().get("reports", {}).get(str(report_id))
    return report if isinstance(report, dict) else None


def _comment_content(msg, max_length: int = 3500) -> str:
    text = str(getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    if not text:
        if getattr(msg, "photo", None):
            text = "[图片评论]"
        elif getattr(msg, "video", None):
            text = "[视频评论]"
        elif getattr(msg, "document", None):
            text = "[文件评论]"
        elif getattr(msg, "voice", None):
            text = "[语音评论]"
        else:
            text = "[媒体评论]"
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def _comment_author(msg) -> str:
    user = getattr(msg, "from_user", None)
    if not user:
        return "未知用户"
    name = str(getattr(user, "full_name", "") or getattr(user, "first_name", "") or "用户").strip()
    username = str(getattr(user, "username", "") or "").strip()
    return f"{name} (@{username})" if username else name


async def _report_deep_link(context: ContextTypes.DEFAULT_TYPE, report_id: str):
    username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not username:
        try:
            username = str((await context.bot.get_me()).username or "").strip().lstrip("@")
        except Exception:
            return None
    return f"https://t.me/{username}?start=report_{report_id}" if username else None


async def _append_report_link_to_original_post(
    context: ContextTypes.DEFAULT_TYPE,
    source_message,
    channel_id: int,
    message_id: int,
    report_url: str,
) -> bool:
    """Append a plain deep link without replacing Telegram's discussion button.

    Only plain text/plain captions are edited. Rich entities are deliberately
    left untouched because editing them as plain text would destroy formatting.
    """
    link_line = f"📋 评论报告：{report_url}"
    text = getattr(source_message, "text", None)
    caption = getattr(source_message, "caption", None)
    try:
        if text and not getattr(source_message, "entities", None):
            new_text = f"{text.rstrip()}\n\n{link_line}"
            if len(new_text) <= 4096:
                await context.bot.edit_message_text(
                    chat_id=channel_id,
                    message_id=message_id,
                    text=new_text,
                    disable_web_page_preview=True,
                )
                return True
        if caption and not getattr(source_message, "caption_entities", None):
            new_caption = f"{caption.rstrip()}\n\n{link_line}"
            if len(new_caption) <= 1024:
                await context.bot.edit_message_caption(
                    chat_id=channel_id,
                    message_id=message_id,
                    caption=new_caption,
                )
                return True
    except Exception as exc:
        print(f"追加评论报告链接到原帖失败: {exc}")
    return False


async def _send_report_link_companion(
    context: ContextTypes.DEFAULT_TYPE, channel_id: int, report_url: str
) -> None:
    """Fallback: send only a plain link text immediately after the source post."""
    await context.bot.send_message(
        chat_id=channel_id,
        text=f"📋 评论报告：{report_url}",
        disable_web_page_preview=True,
    )


def _append_report_comment(submission: dict, forwarded_message) -> None:
    target = _comment_target_from_submission(submission)
    channel_id = _as_int(target.get("channel_id"))
    message_id = _as_int(target.get("message_id"))
    if channel_id is None or message_id is None:
        return
    data = _load_comment_reports()
    report = None
    for item in data["reports"].values():
        if (
            isinstance(item, dict)
            and _as_int(item.get("channel_id")) == channel_id
            and _as_int(item.get("message_id")) == message_id
        ):
            report = item
            break
    if report is None:
        return
    comments = report.setdefault("comments", [])
    comments.append({
        "author": str(submission.get("report_author") or "用户"),
        "content": str(submission.get("report_content") or "[评论内容]"),
        "forward_channel_id": _as_int(submission.get("forward_channel_id")),
        "forward_message_id": _as_int(getattr(forwarded_message, "message_id", None)),
        "created_at": int(time.time()),
    })
    if len(comments) > 1000:
        del comments[:-1000]
    _save_comment_reports(data)


def _count_migratable_report_comments(target_channel_id: int) -> int:
    total = 0
    for report in _load_comment_reports().get("reports", {}).values():
        if not isinstance(report, dict):
            continue
        for comment in report.get("comments", []) or []:
            if not isinstance(comment, dict):
                continue
            old_channel = _as_int(comment.get("forward_channel_id"))
            old_message = _as_int(comment.get("forward_message_id"))
            if old_channel is not None and old_message is not None and old_channel != target_channel_id:
                total += 1
    return total


async def _migrate_report_comments(
    context: ContextTypes.DEFAULT_TYPE, target_channel_id: int
) -> tuple[int, int, int, int]:
    """Copy recorded comments to a new forward channel and rewrite report links.

    Returns ``(migrated, restored_from_content, skipped, failed)``. Every
    successful copy or content restoration updates stored channel/message IDs,
    so report detail links immediately point at the new channel and a retry
    will not duplicate already-migrated comments.
    """
    data = _load_comment_reports()
    migrated = restored_from_content = skipped = failed = 0
    changed = False
    for report in data.get("reports", {}).values():
        if not isinstance(report, dict):
            continue
        comments = report.get("comments", [])
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict):
                skipped += 1
                continue
            source_channel_id = _as_int(comment.get("forward_channel_id"))
            source_message_id = _as_int(comment.get("forward_message_id"))
            if source_channel_id == target_channel_id:
                skipped += 1
                continue
            if source_channel_id is None or source_message_id is None:
                skipped += 1
                continue
            try:
                copied = await context.bot.copy_message(
                    chat_id=target_channel_id,
                    from_chat_id=source_channel_id,
                    message_id=source_message_id,
                )
                migration_mode = "copy"
            except Exception as copy_exc:
                # The old forward channel may have removed the historical
                # message. Reports retain the comment body specifically so we
                # can still restore a readable text version to the new channel.
                content = str(comment.get("content") or "").strip()
                author = str(comment.get("author") or "用户").strip()
                if not content:
                    failed += 1
                    print(
                        "[评论迁移] 原消息不存在且报告未保存内容 "
                        f"from={source_channel_id}/{source_message_id}: {copy_exc}"
                    )
                    continue
                # restore_text = f"💬 历史评论迁移\n作者：{author}\n\n{content}"
                restore_text  = f"{content}"
                try:
                    copied = await context.bot.send_message(
                        chat_id=target_channel_id,
                        text=restore_text[:4096],
                        disable_web_page_preview=True,
                    )
                    migration_mode = "content_restore"
                    restored_from_content += 1
                    print(
                        "[评论迁移] 原消息不可复制，已按报告内容恢复 "
                        f"from={source_channel_id}/{source_message_id} to={target_channel_id}"
                    )
                except Exception as restore_exc:
                    failed += 1
                    print(
                        "[评论迁移] 转发和内容恢复均失败 "
                        f"from={source_channel_id}/{source_message_id}: copy={copy_exc}; restore={restore_exc}"
                    )
                    continue
            comment["forward_channel_id"] = target_channel_id
            comment["forward_message_id"] = copied.message_id
            comment["migrated_at"] = int(time.time())
            comment["migration_mode"] = migration_mode
            migrated += 1
            changed = True
    if changed:
        _save_comment_reports(data)
    return migrated, restored_from_content, skipped, failed


async def _run_comment_migration_task(
    context: ContextTypes.DEFAULT_TYPE, target_channel_id: int, notify_chat_id: int
) -> None:
    try:
        migrated, restored_from_content, skipped, failed = await _migrate_report_comments(context, target_channel_id)
        await context.bot.send_message(
            chat_id=notify_chat_id,
            text=(
                "✅ 历史评论迁移完成\n"
                f"新转发频道：{target_channel_id}\n"
                f"成功迁移：{migrated} 条\n"
                f"其中按报告内容恢复：{restored_from_content} 条\n"
                f"无需迁移/缺少记录：{skipped} 条\n"
                f"失败：{failed} 条\n\n"
                "报告中的“打开转发频道评论”已自动指向新频道。"
            ),
        )
    finally:
        context.application.bot_data["comment_migration_in_progress"] = False


def _report_subjects(report: dict) -> list[dict]:
    subjects = report.get("subjects", []) if isinstance(report.get("subjects"), list) else []
    result = [item for item in subjects if isinstance(item, dict) and item.get("value")]
    if result:
        return result

    # Reports created before this field was added can recover their subject from
    # the keyword index using the channel post they belong to.
    channel_id = report.get("channel_id")
    message_id = report.get("message_id")
    for records in _load_keyword_map().values():
        for record in records or []:
            if (
                isinstance(record, dict)
                and str(record.get("channel_id")) == str(channel_id)
                and int(record.get("channel_message_id", 0) or 0) == int(message_id or 0)
            ):
                label = str(record.get("label") or "").strip()
                value = str(record.get("raw") or "").strip()
                if value and not any(item.get("label") == label and item.get("value") == value for item in result):
                    result.append({"label": label, "value": value})
    return result


def _report_subject_text(report: dict) -> str:
    subjects = _report_subjects(report)
    if not subjects:
        return "收录对象：未识别"
    values = []
    for item in subjects:
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        values.append(f"{label}：{value}" if label else value)
    return "收录对象：" + "；".join(values)


def _comment_date(comment: dict) -> str:
    try:
        return datetime.fromtimestamp(int(comment.get("created_at", 0) or 0)).strftime("%Y-%m-%d")
    except Exception:
        return "未知日期"


def _report_list_view(report_id: str, report: dict, page: int):
    comments = report.get("comments", []) if isinstance(report.get("comments"), list) else []
    total = len(comments)
    pages = max(1, (total + REPORT_PAGE_SIZE - 1) // REPORT_PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * REPORT_PAGE_SIZE
    rows = []
    lines = [
        "📋 帖子评论报告",
        "",
        f"报告总数：{total} 条评论",
        f"当前页：{page}/{pages}",
        _report_subject_text(report),
        "",
        "请选择下方评论查看详情：",
    ]
    for index, comment in enumerate(comments[start : start + REPORT_PAGE_SIZE], start=start):
        author = str(comment.get("author") or "用户")[:28]
        date_text = _comment_date(comment)
        # Do not put comment content into the report overview. It is available
        # only after the viewer selects this entry.
        rows.append([InlineKeyboardButton(
            f"📅 {date_text} · {index + 1}. {author}",
            callback_data=f"publish:report_detail:{report_id}:{page}:{index}",
        )])
    if not comments:
        lines.append("暂无已审核并转发的评论。")
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"publish:report_page:{report_id}:{page - 1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"publish:report_page:{report_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_report_start_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE, parameter: str) -> bool:
    if not isinstance(parameter, str) or not parameter.startswith("report_"):
        return False
    report_id = parameter.removeprefix("report_").strip()
    report = _get_comment_report(report_id)
    if not report:
        if update.message:
            await update.message.reply_text("❗ 该报告不存在或已过期。")
        return True
    text, markup = _report_list_view(report_id, report, 1)
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
    return True


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _publish_failure_detail(exc: Exception) -> str:
    """Return an actionable review error without hiding the real failure."""
    if isinstance(exc, Forbidden):
        return "机器人在目标频道或讨论组没有发言/发布权限。"
    if isinstance(exc, BadRequest):
        raw = str(exc)
        lower = raw.lower()
        if "chat not found" in lower:
            return "频道或讨论组 ID 不正确，或机器人尚未加入目标频道。"
        if "message to reply not found" in lower:
            return "未找到主频道帖子的讨论组映射。请使用新发布的主频道帖子评论。"
        if "not enough rights" in lower or "forbidden" in lower:
            return "机器人缺少目标频道或讨论组的发言权限。"
        if "message can't be edited" in lower or "message is not modified" in lower:
            return "无法移除主频道帖子的底部按钮以启用原生评论。请确认该帖子由机器人发布。"
        return f"Telegram 返回：{raw[:240]}"
    return str(exc)[:300] or "未知错误"


def _comment_target_from_submission(submission: dict):
    target = submission.get("comment_target")
    return target if isinstance(target, dict) else {}


def _keyword_labels(config: dict) -> list[str]:
    labels = config.get("keyword_extract_labels", [])
    if not isinstance(labels, list):
        return []
    result = []
    for label in labels:
        value = str(label or "").strip()
        if value and value not in result:
            result.append(value[:30])
    return result


def _normalize_routing_keyword(value: str) -> str:
    value = str(value or "").strip()
    # Tags are commonly used for stage names, but searches should use the name.
    if value.startswith("#"):
        value = value[1:].strip()
    return value.lower()


def _extract_routing_keywords(msg, config: dict) -> list[dict]:
    text = str(
        getattr(msg, "text", None)
        or getattr(msg, "caption", None)
        or ""
    )

    if not text:
        return []

    result = []

    for label in _keyword_labels(config):
        if label == "标签":
            # 找到“标签”字段
            pattern = re.compile(
                rf"{re.escape(label)}[：:]?(.*)",
                re.IGNORECASE | re.DOTALL,
            )

            match = pattern.search(text)
            if not match:
                continue

            # 标签字段后面的所有 #xxx
            content = match.group(1)

            for raw in re.findall(r"#[^\s#]+", content):
                key = _normalize_routing_keyword(raw)

                if key and not any(
                    item["key"] == key and item["label"] == label
                    for item in result
                ):
                    result.append({
                        "label": label,
                        "key": key,
                        "raw": raw,
                    })

        else:
            # 普通字段：取 label 后第一个非空白内容
            pattern = re.compile(
                rf"{re.escape(label)}[：:]?\s*([^\s]+)",
                re.IGNORECASE,
            )

            for match in pattern.finditer(text):
                raw = match.group(1).strip()
                key = _normalize_routing_keyword(raw)

                if key and not any(
                    item["key"] == key and item["label"] == label
                    for item in result
                ):
                    result.append({
                        "label": label,
                        "key": key,
                        "raw": raw,
                    })

    return result


def _load_keyword_map() -> dict:
    data = load_json(KEYWORD_MAP_FILE)
    return data if isinstance(data, dict) else {}


def _save_keyword_map(data: dict) -> None:
    # Keep enough history for routing while avoiding unlimited growth.
    for key, records in list(data.items()):
        if not isinstance(records, list):
            data.pop(key, None)
            continue
        data[key] = records[-30:]
    if len(data) > 3000:
        for key in list(data)[: len(data) - 3000]:
            data.pop(key, None)
    save_json(KEYWORD_MAP_FILE, data)


def _register_post_keywords(entries: list[dict], channel_id, message_id) -> None:
    if not entries:
        return
    data = _load_keyword_map()
    now = int(time.time())
    for entry in entries:
        key = entry.get("key")
        if not key:
            continue
        records = data.setdefault(key, [])
        records[:] = [
            item for item in records
            if not (
                str(item.get("channel_id")) == str(channel_id)
                and int(item.get("channel_message_id", 0) or 0) == int(message_id)
                and item.get("label") == entry.get("label")
            )
        ]
        records.append({
            "label": entry.get("label", ""),
            "raw": entry.get("raw", key),
            "channel_id": channel_id,
            "channel_message_id": message_id,
            "created_at": now,
        })
    _save_keyword_map(data)


def _replace_post_keywords_from_channel_edit(
    entries: list[dict], channel_id, message_id
) -> int:
    """Replace all keyword records for one edited main-channel post.

    Unlike normal publication, an edit can remove or rename an existing keyword,
    so stale entries for the same post must be removed across every keyword key.
    Existing discussion mapping is retained for the replacement records.
    """
    data = _load_keyword_map()
    retained_mapping = {}
    for key, records in list(data.items()):
        if not isinstance(records, list):
            continue
        kept = []
        for record in records:
            is_same_post = (
                isinstance(record, dict)
                and str(record.get("channel_id")) == str(channel_id)
                and int(record.get("channel_message_id", 0) or 0) == int(message_id)
            )
            if is_same_post:
                for field in ("discussion_chat_id", "discussion_message_id"):
                    if record.get(field) is not None:
                        retained_mapping[field] = record[field]
            else:
                kept.append(record)
        if kept:
            data[key] = kept
        else:
            data.pop(key, None)

    # The discussion map is the source of truth if the old keyword records did
    # not include it (for example, when an earlier edit occurred before mapping).
    mapping = _load_comment_map().get(_comment_map_key(channel_id, message_id), {})
    if isinstance(mapping, dict):
        retained_mapping.setdefault("discussion_chat_id", mapping.get("discussion_chat_id"))
        retained_mapping.setdefault("discussion_message_id", mapping.get("discussion_message_id"))

    now = int(time.time())
    for entry in entries:
        key = entry.get("key")
        if not key:
            continue
        record = {
            "label": entry.get("label", ""),
            "raw": entry.get("raw", key),
            "channel_id": channel_id,
            "channel_message_id": message_id,
            "created_at": now,
        }
        if retained_mapping.get("discussion_chat_id") is not None:
            record["discussion_chat_id"] = retained_mapping["discussion_chat_id"]
        if retained_mapping.get("discussion_message_id") is not None:
            record["discussion_message_id"] = retained_mapping["discussion_message_id"]
        data.setdefault(key, []).append(record)

    _save_keyword_map(data)
    return len(entries)


def _update_keyword_comment_mapping(channel_id, message_id, discussion_chat_id, discussion_message_id) -> None:
    data = _load_keyword_map()
    changed = False
    for records in data.values():
        if not isinstance(records, list):
            continue
        for record in records:
            if (
                str(record.get("channel_id")) == str(channel_id)
                and int(record.get("channel_message_id", 0) or 0) == int(message_id)
            ):
                record["discussion_chat_id"] = discussion_chat_id
                record["discussion_message_id"] = discussion_message_id
                changed = True
    if changed:
        _save_keyword_map(data)


def _find_keyword_routes(query: str) -> list[dict]:
    key = _normalize_routing_keyword(query)
    if not key:
        return []
    data = _load_keyword_map()
    matches = []
    for stored_key, records in data.items():
        if key not in stored_key and stored_key not in key:
            continue
        for record in records or []:
            if isinstance(record, dict) and record.get("discussion_message_id"):
                matches.append({**record, "key": stored_key})
    matches.sort(key=lambda item: int(item.get("created_at", 0) or 0), reverse=True)
    return matches[:10]


def _fallback_channel_message_link(channel_id, message_id):
    channel = str(channel_id or "")
    if channel.startswith("-100") and str(message_id).isdigit():
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return None


async def _route_message_link(context: ContextTypes.DEFAULT_TYPE, route: dict):
    """Build a public link when possible, otherwise use Telegram's private link."""
    channel_id = route.get("channel_id")
    message_id = route.get("channel_message_id")
    try:
        chat = await context.bot.get_chat(channel_id)
        username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
        if username and message_id:
            return f"https://t.me/{username}/{message_id}"
    except Exception:
        pass
    return _fallback_channel_message_link(channel_id, message_id)


def _keyword_route_keyboard(route: dict, link: str, enabled: bool):
    rows = []
    if link:
        rows.append([InlineKeyboardButton("🔗 查看对应消息", url=link)])
    rows.extend(create_post_keyboard(enabled).inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _keyword_settings_text(config: dict) -> str:
    labels = _keyword_labels(config)
    sample = "【艺名】：#丹丹\n【联系方式】：@dandan"
    return (
        "🔑 评论关键词设置\n\n"
        f"当前提取标签：{'、'.join(labels) if labels else '未设置'}\n\n"
        "管理员发布到主频道时，机器人会从内容中提取这些标签对应的值，"
        "并映射到主频道消息 ID 与讨论组评论 ID。\n\n"
        f"示例：\n{sample}\n"
        "设置“艺名”可提取丹丹；设置“联系方式”可提取 @dandan。"
    )


def _keyword_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 设置提取标签", callback_data="publish:keywords_set")],
        [InlineKeyboardButton("🗑 清空标签", callback_data="publish:keywords_clear")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")],
    ])


def _load_pending_submissions() -> dict:
    """Load review queue. JSON storage keeps pending reviews after a restart."""
    data = load_json(PENDING_SUBMISSIONS_FILE)
    return data if isinstance(data, dict) else {}


def _save_pending_submissions(data: dict) -> None:
    save_json(PENDING_SUBMISSIONS_FILE, data)


def _owner_id(context: ContextTypes.DEFAULT_TYPE):
    """Read the owner from this app instead of a process-global multi-bot value."""
    try:
        return int(context.application.bot_data.get("owner_id"))
    except (TypeError, ValueError):
        return None


def _review_keyboard(submission_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 通过并发布",
                callback_data=f"publish:review_approve:{submission_id}",
            ),
            InlineKeyboardButton(
                "❌ 拒绝",
                callback_data=f"publish:review_reject:{submission_id}",
            ),
        ]
    ])


def _submission_author_text(msg) -> str:
    user = getattr(msg, "from_user", None)
    if not user:
        return "未知用户"
    name = " ".join(
        part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if part
    ).strip()
    username = getattr(user, "username", None)
    if username:
        return f"{name or '用户'} (@{username}, ID: {user.id})"
    return f"{name or '用户'} (ID: {user.id})"


def _append_published_submission(
    msg,
    published_message_id: int,
    published_channel_id=None,
) -> None:
    """Store a published submission for the existing random-view feature."""
    user = getattr(msg, "from_user", None)
    data = _load_cannel_message()
    data.append({
        "user_id": getattr(user, "id", None),
        "user_chat_id": msg.chat_id,
        "username": getattr(user, "username", None),
        "user_message_id": msg.message_id,
        "channel_message_id": published_message_id,
        "channel_id": published_channel_id,
        "publish_time": int(time.time()),
    })
    save_json(USER_MESSAGE_FILE, data)


def _review_prompt_text(submission: dict) -> str:
    proof_status = "已上传" if submission.get("proof_message_id") else "未要求"
    return (
        "📝 收到新的投稿，请审核。\n"
        f"投稿人：{submission.get('author', '未知用户')}\n"
        f"审核凭证：{proof_status}\n"
        f"投稿编号：{submission.get('id', '未知')}"
    )


def _reject_reason_cancel_keyboard(submission_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ 取消拒绝",
                callback_data=f"publish:review_reject_cancel:{submission_id}",
            )
        ]
    ])


async def _send_submission_for_review(
    context: ContextTypes.DEFAULT_TYPE,
    submission_id: str,
    submission: dict,
):
    """Forward the submission and optional proof to owner, then send controls."""
    owner_id = _owner_id(context)
    if owner_id is None:
        raise RuntimeError("未配置机器人所有者")

    await context.bot.forward_message(
        chat_id=owner_id,
        from_chat_id=submission["user_chat_id"],
        message_id=submission["user_message_id"],
    )
    if submission.get("proof_message_id"):
        await context.bot.forward_message(
            chat_id=owner_id,
            from_chat_id=submission["proof_chat_id"],
            message_id=submission["proof_message_id"],
        )

    # Send review work to the owner and every delegated reviewer. Each recipient
    # gets independent controls; the persisted submission state prevents duplicate publishing.
    reviewer_ids = {int(owner_id), *get_delegated_admin_ids(context, "submission_review")}
    prompt = _review_prompt_text({**submission, "id": submission_id})
    failures = []
    for reviewer_id in reviewer_ids:
        try:
            await context.bot.send_message(
                chat_id=reviewer_id,
                text=prompt,
                reply_markup=_review_keyboard(submission_id),
            )
        except Exception as exc:
            failures.append(str(exc))
    if len(failures) == len(reviewer_ids):
        raise RuntimeError("所有审核管理员均无法接收审核通知，请先让管理员私聊机器人。")


async def _finalize_rejection(
    context: ContextTypes.DEFAULT_TYPE,
    submission_id: str,
    submission: dict,
    reason: str = "",
):
    submission["status"] = "rejected"
    submission["reviewed_at"] = int(time.time())
    if reason:
        submission["reject_reason"] = reason

    pending = _load_pending_submissions()
    pending[submission_id] = submission
    _save_pending_submissions(pending)

    notice = "❌ 很抱歉，您的投稿未通过审核。"
    if reason:
        notice += f"\n\n审核原因：{reason}"
    try:
        await context.bot.send_message(chat_id=submission["user_chat_id"], text=notice)
    except Exception as exc:
        print("投稿拒绝通知失败:", exc)


async def _capture_comment_source_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Capture discussion forwards and refresh keywords after channel edits."""
    msg = update.effective_message
    if not msg:
        return

    config = load_publish_config()
    main_channel_id = _as_int(config.get("channel_id"))
    edited_channel_post = getattr(update, "edited_channel_post", None)
    if (
        edited_channel_post is not None
        and main_channel_id is not None
        and int(getattr(getattr(msg, "chat", None), "id", 0) or 0) == main_channel_id
    ):
        entries = _extract_routing_keywords(msg, config)
        count = _replace_post_keywords_from_channel_edit(
            entries, main_channel_id, msg.message_id
        )
        print(
            "✅ 已更新编辑后频道帖关键词 "
            f"channel={main_channel_id} message={msg.message_id} keywords={count}"
        )
        return

    sender_chat = getattr(msg, "sender_chat", None)
    origin = getattr(msg, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin else None
    legacy_forward_chat = getattr(msg, "forward_from_chat", None)
    channel_id = (
        getattr(sender_chat, "id", None)
        or getattr(origin_chat, "id", None)
        or getattr(legacy_forward_chat, "id", None)
    )
    channel_message_id = (
        getattr(origin, "message_id", None)
        or getattr(msg, "forward_from_message_id", None)
    )
    if channel_id is None or channel_message_id is None:
        return
    if not bool(config.get("comment_forward_enabled", False)) or str(channel_id) != str(config.get("channel_id")):
        return

    # Some bots receive an edit only through the linked discussion group's
    # automatic forward (edited_message), not as edited_channel_post. The
    # forwarded content mirrors the edited channel post, so refresh its keyword
    # records using the source channel/message IDs in that update.
    if getattr(update, "edited_message", None) is not None:
        entries = _extract_routing_keywords(msg, config)
        count = _replace_post_keywords_from_channel_edit(
            entries, channel_id, channel_message_id
        )
        print(
            "✅ 已通过讨论组编辑更新频道帖关键词 "
            f"channel={channel_id} message={channel_message_id} keywords={count}"
        )

    records = _load_comment_map()
    records[_comment_map_key(channel_id, channel_message_id)] = {
        "discussion_chat_id": msg.chat_id,
        "discussion_message_id": msg.message_id,
        "created_at": int(time.time()),
    }
    _save_comment_map(records)
    _update_keyword_comment_mapping(channel_id, channel_message_id, msg.chat_id, msg.message_id)
    print(f"✅ 已通过 Bot API 写入 discussion 映射 {channel_id}/{channel_message_id} -> {msg.chat_id}/{msg.message_id}")


async def _start_comment_submission(query, context: ContextTypes.DEFAULT_TYPE, config: dict):
    parts = query.data.split(":")
    if len(parts) != 4:
        return await query.answer("评论数据无效。", show_alert=True)
    if not bool(config.get("comment_forward_enabled", False)):
        return await query.answer("评论并转发功能未开启。", show_alert=True)

    channel_id = _as_int(parts[2])
    message_id = _as_int(parts[3])
    if channel_id is None or message_id is None:
        return await query.answer("评论数据无效。", show_alert=True)
    if str(channel_id) != str(config.get("channel_id")):
        return await query.answer("该帖子不是当前主频道的帖子。", show_alert=True)

    user = query.from_user
    context.user_data[COMMENT_TARGET_KEY] = {
        "channel_id": channel_id,
        "message_id": message_id,
    }
    context.user_data["waiting_post"] = True
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "💬 请发送要评论的内容。\n"
                "内容会按投稿流程审核；通过后会评论到该帖子下，并发布到转发频道。"
            ),
        )
    except Exception:
        context.user_data.pop(COMMENT_TARGET_KEY, None)
        context.user_data["waiting_post"] = False
        return await query.answer(
            "请先私聊机器人并发送 /start，再点击评论。",
            show_alert=True,
        )
    return await query.answer("请到与机器人的私聊发送评论内容。", show_alert=True)


async def _comment_deep_link(
    context: ContextTypes.DEFAULT_TYPE,
    channel_id,
    message_id,
):
    """Create a Telegram start link so the comment button opens the bot directly."""
    username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not username:
        try:
            me = await context.bot.get_me()
            username = str(getattr(me, "username", "") or "").strip().lstrip("@")
        except Exception as exc:
            print("获取机器人用户名失败，评论按钮将使用回调模式:", exc)
            return None
    if not username:
        return None
    parameter = f"comment_{channel_id}_{message_id}"
    return f"https://t.me/{username}?start={parameter}"


async def handle_comment_start_parameter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parameter: str,
) -> bool:
    """Start a comment submission from a Telegram deep link parameter."""
    if not isinstance(parameter, str) or not parameter.startswith("comment_"):
        return False
    parts = parameter.split("_", 2)
    if len(parts) != 3:
        return False
    channel_id = _as_int(parts[1])
    message_id = _as_int(parts[2])
    config = load_publish_config()
    if (
        channel_id is None
        or message_id is None
        or not bool(config.get("comment_forward_enabled", False))
        or str(channel_id) != str(config.get("channel_id"))
    ):
        if update.message:
            await update.message.reply_text("❗ 该评论入口已失效或评论并转发功能未开启。")
        return True

    context.user_data[COMMENT_TARGET_KEY] = {
        "channel_id": channel_id,
        "message_id": message_id,
    }
    context.user_data["waiting_post"] = True
    if update.message:
        await update.message.reply_text(
            "💬 请发送要评论的内容。\n"
            "内容会按投稿流程审核；通过后会评论到该帖子下，并发布到转发频道。"
        )
    return True


async def _copy_submission_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict,
    target_channel_id,
    config: dict,
    *,
    add_comment_button: bool = False,
):
    """Copy original user content to a channel and optionally append comment action."""
    # The main post cannot have inline markup: Telegram otherwise hides its
    # native comment thread.
    published = await context.bot.copy_message(
        chat_id=target_channel_id,
        from_chat_id=submission["user_chat_id"],
        message_id=submission["user_message_id"],
        # Never attach report markup to the main post: Telegram would replace
        # its native discussion button with this inline keyboard.
        reply_markup=None if add_comment_button else publish_buttons_keyboard(config),
    )
    if submission.get("keyword_entries"):
        _register_post_keywords(
            submission["keyword_entries"],
            target_channel_id,
            published.message_id,
        )
    if add_comment_button:
        # 暂时停用“参与讨论请点击下方按钮”辅助消息及其评论按钮。
        # 主频道正文保持无 InlineKeyboard，避免影响 Telegram 原生评论显示。
        pass
    return published


async def _publish_comment_and_forward(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict,
    config: dict,
):
    """Post approved content as a linked-discussion comment and mirror it onward."""
    target = _comment_target_from_submission(submission)
    main_channel_id = _as_int(target.get("channel_id"))
    main_message_id = _as_int(target.get("message_id"))
    forward_channel_id = _as_int(config.get("forward_channel_id"))
    if main_channel_id is None or main_message_id is None:
        raise RuntimeError("评论目标无效")
    if forward_channel_id is None:
        raise RuntimeError("未设置转发频道")

    mapping = _load_comment_map().get(_comment_map_key(main_channel_id, main_message_id))
    if not isinstance(mapping, dict):
        raise RuntimeError(
            "未找到主频道帖子的讨论组映射。请确认主频道已绑定讨论组，机器人是讨论组管理员。"
        )

    discussion_chat_id = mapping.get("discussion_chat_id")
    discussion_message_id = mapping.get("discussion_message_id")
    if discussion_chat_id is None or discussion_message_id is None:
        raise RuntimeError("评论讨论组映射无效")

    # Main posts created in comment mode have no inline markup, so this reply
    # is shown as a native comment below the original channel post.

    await context.bot.copy_message(
        chat_id=discussion_chat_id,
        from_chat_id=submission["user_chat_id"],
        message_id=submission["user_message_id"],
        reply_to_message_id=discussion_message_id,
    )
    submission["forward_channel_id"] = forward_channel_id
    forwarded = await _copy_submission_to_channel(
        context,
        submission,
        forward_channel_id,
        config,
    )
    _append_report_comment(submission, forwarded)
    return forwarded


async def _handle_review_callback(query, context: ContextTypes.DEFAULT_TYPE, config: dict):
    """Approve, reject, or collect a rejection reason for a submission."""
    parts = query.data.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        return await query.answer("审核数据无效。", show_alert=True)

    if not has_admin_permission(context, query.from_user.id, "submission_review"):
        return await query.answer("你没有稿件审核权限。", show_alert=True)

    action, submission_id = parts[1], parts[2]
    pending = _load_pending_submissions()
    submission = pending.get(submission_id)
    if not isinstance(submission, dict):
        return await query.answer("该投稿不存在或已清理。", show_alert=True)

    status = submission.get("status", "pending")
    if status != "pending":
        status_text = {
            "awaiting_proof": "仍在等待凭证",
            "approved": "已通过",
            "rejected": "已拒绝",
            "publishing": "正在发布中",
        }.get(status, "已处理")
        return await query.answer(f"该投稿{status_text}，请勿重复处理。", show_alert=True)

    if action == "review_reject_cancel":
        reject_stage = (context.user_data or {}).get(REJECT_REASON_KEY)
        if isinstance(reject_stage, dict) and reject_stage.get("submission_id") == submission_id:
            context.user_data.pop(REJECT_REASON_KEY, None)
        await query.answer("已取消拒绝")
        return await query.edit_message_text(
            _review_prompt_text({**submission, "id": submission_id}),
            reply_markup=_review_keyboard(submission_id),
        )

    if action == "review_approve":
        channel_id = config.get("channel_id")
        if not channel_id:
            return await query.answer("未配置发布频道，无法发布。", show_alert=True)

        # Persist a transient state first to prevent two owner clicks from publishing twice.
        submission["status"] = "publishing"
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        submission_kind = str(submission.get("submission_kind", "main"))
        target_channel_id = _as_int(submission.get("target_channel_id")) or channel_id
        try:
            # Only original submission content is published; proof is review-only.
            if submission_kind == "comment":
                published = await _publish_comment_and_forward(context, submission, config)
                target_channel_id = _as_int(config.get("forward_channel_id"))
            else:
                published = await _copy_submission_to_channel(
                    context,
                    submission,
                    target_channel_id,
                    config,
                )
        except Exception as exc:
            submission["status"] = "pending"
            pending[submission_id] = submission
            _save_pending_submissions(pending)
            detail = _publish_failure_detail(exc)
            print("审核投稿发布失败:", exc)
            try:
                await query.message.reply_text(f"❌ 发布失败详情：\n{detail}")
            except Exception as notify_exc:
                print("发送发布失败详情失败:", notify_exc)
            return await query.answer("发布失败，详情已发送。", show_alert=True)

        submission["status"] = "approved"
        submission["reviewed_at"] = int(time.time())
        submission["channel_message_id"] = published.message_id
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        data = _load_cannel_message()
        data.append({
            "user_id": submission.get("user_id"),
            "user_chat_id": submission["user_chat_id"],
            "username": submission.get("username"),
            "user_message_id": submission["user_message_id"],
            "channel_message_id": published.message_id,
            "channel_id": target_channel_id,
            "publish_time": int(time.time()),
        })
        save_json(USER_MESSAGE_FILE, data)

        try:
            await context.bot.send_message(
                chat_id=submission["user_chat_id"],
                text="✅ 您的投稿已审核通过，并已发布到频道。",
            )
        except Exception as exc:
            print("投稿通过通知失败:", exc)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            print("更新审核消息失败:", exc)
        await query.answer("投稿已通过并发布。")
        return await query.message.reply_text("✅ 已通过投稿并发布到频道。")

    if action == "review_reject":
        if bool(config.get("reject_reason_required", False)):
            review_chat = getattr(getattr(query.message, "chat", None), "id", None)
            context.user_data[REJECT_REASON_KEY] = {
                "submission_id": submission_id,
                "review_chat_id": review_chat,
                "review_message_id": query.message.message_id,
            }
            await query.answer()
            return await query.edit_message_text(
                "❌ 请发送拒绝原因。\n\n"
                "该原因会通知投稿人；发送“取消”可返回审核。",
                reply_markup=_reject_reason_cancel_keyboard(submission_id),
            )

        await _finalize_rejection(context, submission_id, submission)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            print("更新审核消息失败:", exc)
        await query.answer("投稿已拒绝。")
        return await query.message.reply_text("❌ 已拒绝投稿，已通知投稿人。")

    return await query.answer("未知审核操作。", show_alert=True)


def _publish_buttons(config: dict) -> list[dict]:
    """Return valid custom buttons from the current publish configuration."""
    buttons = config.get("buttons", []) if isinstance(config, dict) else []
    if not isinstance(buttons, list):
        return []
    return [button for button in buttons if isinstance(button, dict)]


def _button_settings_text(config: dict) -> str:
    buttons = _publish_buttons(config)
    lines = [
        "🔘 投稿按钮设置",
        "投稿发布到频道时，会在消息下方附加这些链接按钮。",
        "",
    ]
    if not buttons:
        lines.append("当前未设置按钮。")
    else:
        lines.append(f"当前已设置 {len(buttons)} 个按钮：")
        for button in buttons:
            lines.append(
                f"#{button.get('id')} {button.get('text', '未命名')}\n{button.get('url', '')}"
            )
    return "\n".join(lines)


def publish_buttons_keyboard(
    config: dict,
    *,
    comment_channel_id=None,
    comment_message_id=None,
    comment_url: str = None,
):
    """Build buttons for a published post, including an optional comment action."""
    rows = []
    if bool(config.get("bottom_buttons_enabled", True)):
        for button in _publish_buttons(config):
            button_text = str(button.get("text", "")).strip()
            button_url = str(button.get("url", "")).strip()
            if button_text and button_url:
                rows.append([InlineKeyboardButton(button_text, url=button_url)])

    # 不展示评论按钮
    # if (
    #     bool(config.get("comment_forward_enabled", False))
    #     and comment_channel_id is not None
    #     and comment_message_id is not None
    # ):
    #     rows.append([
    #         InlineKeyboardButton(
    #             "💬 评论",
    #             url=comment_url,
    #         )
    #         if comment_url
    #         else InlineKeyboardButton(
    #             "💬 评论",
    #             callback_data=(
    #                 f"publish:comment:{comment_channel_id}:{comment_message_id}"
    #             ),
    #         )
    #     ])
    return InlineKeyboardMarkup(rows) if rows else None


def publish_button_settings_keyboard(config: dict):
    buttons = _publish_buttons(config)
    rows = [[InlineKeyboardButton("➕ 添加按钮", callback_data="publish:button_add")]]
    if buttons:
        rows.extend(
            [
                [InlineKeyboardButton("📝 修改按钮", callback_data="publish:button_edit")],
                [InlineKeyboardButton("🗑 删除按钮", callback_data="publish:button_delete")],
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


def _button_selection_keyboard(config: dict, action: str):
    rows = []
    for button in _publish_buttons(config):
        button_id = button.get("id")
        button_text = str(button.get("text", "未命名"))[:40]
        prefix = "📝" if action == "edit" else "🗑"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{prefix} #{button_id} {button_text}",
                    callback_data=f"publish:button_{action}_{button_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:buttons")])
    return InlineKeyboardMarkup(rows)


def _normalize_button_url(value: str):
    url = (value or "").strip()
    if url.startswith("t.me/"):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "tg"} or not parsed.netloc and parsed.scheme != "tg":
        return None
    return url


def _clear_button_input(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("publish_button_input", None)


CUSTOM_USER_MENU_OPTIONS = (
    ("submission", "📝 我要投稿"),
    ("random_view", "🎲 随机查看"),
    ("channel_clone", "📣 克隆频道"),
    ("telethon_manage", "📱 管理协议号"),
    ("bot_channel_config", "📣 机器人配置"),
    ("group_config", "👥 群配置"),
    ("global_ad_config", "📢 全群广告配置"),
)


def _custom_button_visible(config: dict, key: str) -> bool:
    if key == "submission":
        return bool(config.get("submission_enabled", True))
    if key == "random_view":
        return bool(config.get("random_view_enabled", True))
    values = config.get("custom_menu_buttons", {})
    return bool(values.get(key, True)) if isinstance(values, dict) else True


def _set_custom_button_visible(config: dict, key: str, visible: bool) -> bool:
    if key == "submission":
        config["submission_enabled"] = visible
        return True
    if key == "random_view":
        config["random_view_enabled"] = visible
        return True
    if key not in {item[0] for item in CUSTOM_USER_MENU_OPTIONS}:
        return False
    values = config.get("custom_menu_buttons")
    if not isinstance(values, dict):
        values = {}
        config["custom_menu_buttons"] = values
    values[key] = visible
    return True


def _custom_user_buttons_text(config: dict) -> str:
    lines = [
        "🧩 用户按钮显示设置",
        "",
        "以下规则只限制普通用户；机器人所有者和高级管理员始终显示全部按钮：",
    ]
    for key, label in CUSTOM_USER_MENU_OPTIONS:
        lines.append(f"{'✅' if _custom_button_visible(config, key) else '🚫'} {label}")
    return "\n".join(lines)


def _custom_user_buttons_keyboard(config: dict):
    rows = []
    for key, label in CUSTOM_USER_MENU_OPTIONS:
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if _custom_button_visible(config, key) else '🚫'} {label}",
                callback_data=f"publish:custom_toggle:{key}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


def _publish_templates(config: dict) -> list[dict]:
    templates = config.get("publish_templates", []) if isinstance(config, dict) else []
    return [item for item in templates if isinstance(item, dict) and item.get("id")]


def _template_by_id(config: dict, template_id: int):
    for template in _publish_templates(config):
        if int(template.get("id", 0) or 0) == int(template_id):
            return template
    return None


def _template_keys(template: dict) -> list[str]:
    sources = [str(template.get("text") or "")]
    for button in template.get("buttons", []) or []:
        if isinstance(button, dict):
            sources.extend([str(button.get("text") or ""), str(button.get("url") or "")])
    keys = []
    for source in sources:
        for key in TEMPLATE_KEY_PATTERN.findall(source):
            if key not in keys:
                keys.append(key)
    return keys


def _render_template_value(value: str, values: dict, *, html_mode: bool) -> str:
    def replace(match):
        raw = str(values.get(match.group(1), ""))
        return html.escape(raw) if html_mode else raw
    return TEMPLATE_KEY_PATTERN.sub(replace, str(value or ""))


# def _template_keyboard(template: dict, values: dict):
#     rows = []
#     for button in template.get("buttons", []) or []:
#         if not isinstance(button, dict):
#             continue
#         text = _render_template_value(str(button.get("text") or ""), values, html_mode=False).strip()
#         url = _render_template_value(str(button.get("url") or ""), values, html_mode=False).strip()
#         if text and _normalize_button_url(url):
#             rows.append([InlineKeyboardButton(text[:64], url=_normalize_button_url(url))])
#     return InlineKeyboardMarkup(rows) if rows else None


def _template_keyboard(template: dict, values: dict):
    links = []

    for button in template.get("buttons", []) or []:
        if not isinstance(button, dict):
            continue

        text = _render_template_value(
            str(button.get("text") or ""),
            values,
            html_mode=False
        ).strip()

        url = _render_template_value(
            str(button.get("url") or ""),
            values,
            html_mode=False
        ).strip()

        url = _normalize_button_url(url)

        if text and url:
            links.append(f'<a href="{url}">{text[:64]}</a>')

    return "\n".join(links) if links else None


def _render_template(template: dict, values: dict):
    html_mode = str(template.get("format") or "plain") == "html"
    text = _render_template_value(str(template.get("text") or ""), values, html_mode=html_mode)
    # markup = _template_keyboard(template, values)
    
    link_text = _template_keyboard(template, values)

    if link_text:
        text = f"{text.rstrip()}\n\n{link_text}"
    
    return text, None, "HTML" if html_mode else None


def _template_flow_preview(template: dict, values: dict) -> str:
    text, _, mode = _render_template(template, values)
    return "\n".join([
        f"🧩 模板预览：{template.get('name', '未命名')}",
        f"格式：{'HTML' if mode == 'HTML' else '纯文本'}",
        "",
        text,
        "",
        "确认发布到频道？",
    ])


def _template_settings_text(config: dict) -> str:
    templates = _publish_templates(config)
    lines = [
        "🧩 模板发布设置",
        "",
        f"状态：{'✅ 开启' if config.get('template_publish_enabled', False) else '🚫 关闭'}",
        f"模板数量：{len(templates)}",
        "",
        "模板正文支持 {键名} 占位符，例如：{艺名}、{联系方式}。",
        "按钮填写格式：按钮文字 | https://链接",
        "发布时机器人会逐项询问占位符的值。",
    ]
    return "\n".join(lines)


def _template_settings_keyboard(config: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ 添加模板", callback_data="publish:template_add")],
    ]
    for template in _publish_templates(config):
        template_id = int(template.get("id", 0) or 0)
        name = str(template.get("name") or f"模板 {template_id}")[:46]
        rows.append([InlineKeyboardButton(f"📝 {name}", callback_data=f"publish:template_view:{template_id}")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


def _template_detail_text(template: dict) -> str:
    keys = _template_keys(template)
    button_count = len(template.get("buttons", []) or [])
    return "\n".join([
        f"🧩 模板：{template.get('name', '未命名')}",
        f"格式：{'HTML' if template.get('format') == 'html' else '纯文本'}",
        f"占位键：{'、'.join(keys) if keys else '无'}",
        f"超链接按钮：{button_count} 个",
        "",
        str(template.get("text") or ""),
    ])


def _template_draft_preview(draft: dict) -> str:
    template = {
        "name": draft.get("name", "未命名"),
        "text": draft.get("text", ""),
        "format": draft.get("format", "plain"),
        "buttons": draft.get("buttons", []),
    }
    return _template_detail_text(template) + "\n\n确认保存该模板？"


# =========================
# 键盘
# =========================
def publish_setting_keyboard(config: dict):
    bottom_buttons_enabled = bool(config.get("bottom_buttons_enabled", True))
    proof_required = bool(config.get("proof_required", False))
    reject_reason_required = bool(config.get("reject_reason_required", False))
    comment_forward_enabled = bool(config.get("comment_forward_enabled", False))
    continuous_submission_enabled = bool(config.get("continuous_submission_enabled", True))
    report_link_enabled = bool(config.get("report_link_enabled", False))
    template_publish_enabled = bool(config.get("template_publish_enabled", False))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 发布频道", callback_data="publish:channel")],
        [InlineKeyboardButton("📝 审核设置", callback_data="publish:review")],
        # [InlineKeyboardButton("📊 每日发布上限", callback_data="publish:limit")],
        # [InlineKeyboardButton("📣 广告管理", callback_data="publish:ads")],
        [InlineKeyboardButton("🔘 底部按钮设置", callback_data="publish:buttons")],
        [InlineKeyboardButton("🔑 关键词设置", callback_data="publish:keywords")],
        [
            InlineKeyboardButton(
                f"{'✅' if bottom_buttons_enabled else '🚫'} 显示底部按钮",
                callback_data="publish:toggle_bottom_buttons",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if proof_required else '🚫'} 审核凭证",
                callback_data="publish:toggle_proof_required",
            ),
            InlineKeyboardButton(
                f"{'✅' if reject_reason_required else '🚫'} 拒绝原因",
                callback_data="publish:toggle_reject_reason_required",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if comment_forward_enabled else '🚫'} 评论并转发",
                callback_data="publish:toggle_comment_forward",
            ),
            InlineKeyboardButton(
                "📨 转发频道",
                callback_data="publish:forward_channel",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if continuous_submission_enabled else '🚫'} 连续投稿",
                callback_data="publish:toggle_continuous_submission",
            )
        ],
        [InlineKeyboardButton(
            f"{'✅' if report_link_enabled else '🚫'} 生成报告链接",
            callback_data="publish:toggle_report_link",
        )],
        [InlineKeyboardButton(
            "🔄 一键迁移历史评论到当前转发频道",
            callback_data="publish:migrate_forward_comments",
        )],
        [
            InlineKeyboardButton(
                f"{'✅' if template_publish_enabled else '🚫'} 模板发布",
                callback_data="publish:toggle_template_publish",
            ),
            InlineKeyboardButton("🧩 模板配置", callback_data="publish:template_settings"),
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="start:back")]
    ])


def publish_channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 修改频道", callback_data="publish:set_channel")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_review_keyboard(enabled):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ 已开启' if enabled else '❌ 已关闭'}",
                callback_data="publish:toggle_review"
            )
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_limit_keyboard(limit):
    text = "♾ 不限制" if limit <= 0 else str(limit)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"当前：{text}", callback_data="noop")],
        [InlineKeyboardButton("✏️ 修改上限", callback_data="publish:set_limit")],
        [InlineKeyboardButton("🚫 不限制", callback_data="publish:unlimit")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_ads_keyboard(enabled):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ 已启用' if enabled else '❌ 已禁用'}",
                callback_data="publish:toggle_ads"
            )
        ],
        [InlineKeyboardButton("➕ 添加广告", callback_data="publish:add_ad")],
        [InlineKeyboardButton("📝 编辑广告", callback_data="publish:edit_ad")],
        [InlineKeyboardButton("🗑 删除广告", callback_data="publish:delete_ad")],
        [InlineKeyboardButton("📋 广告列表", callback_data="publish:list_ad")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


# =========================
# 回调处理
# =========================

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith("publish:"):
        return

    config = load_publish_config()
    channel_id = config.get("channel_id")
    action = query.data.split(":")[1]

    if action in {"review_approve", "review_reject", "review_reject_cancel"}:
        return await _handle_review_callback(query, context, config)

    if action == "comment":
        return await _start_comment_submission(query, context, config)

    if action == "report_page":
        parts = query.data.split(":")
        if len(parts) != 4:
            return await query.answer("报告分页参数无效。", show_alert=True)
        try:
            page = int(parts[3])
        except ValueError:
            page = 1
        report = _get_comment_report(parts[2])
        if not report:
            return await query.answer("该报告不存在或已过期。", show_alert=True)
        text, markup = _report_list_view(parts[2], report, page)
        await query.answer()
        return await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)

    if action == "report_detail":
        parts = query.data.split(":")
        if len(parts) != 5:
            return await query.answer("报告详情参数无效。", show_alert=True)
        report = _get_comment_report(parts[2])
        try:
            page, index = int(parts[3]), int(parts[4])
            comment = report.get("comments", [])[index] if report else None
        except (ValueError, IndexError, TypeError):
            comment = None
        if not isinstance(comment, dict):
            return await query.answer("该评论不存在或已清理。", show_alert=True)
        text = "\n".join([
            "💬 评论详情",
            "",
            _report_subject_text(report),
            f"评论日期：{_comment_date(comment)}",
            f"作者：{comment.get('author') or '用户'}",
            "",
            str(comment.get("content") or "[媒体评论]"),
        ])
        rows = []
        link = _fallback_channel_message_link(comment.get("forward_channel_id"), comment.get("forward_message_id"))
        if link:
            rows.append([InlineKeyboardButton("🔗 打开转发频道评论", url=link)])
        rows.append([InlineKeyboardButton("⬅️ 返回报告", callback_data=f"publish:report_page:{parts[2]}:{page}")])
        await query.answer()
        return await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), disable_web_page_preview=True)

    if action == "keyword_pick":
        results = (context.user_data or {}).get(KEYWORD_RESULTS_KEY, [])
        try:
            result_index = int(query.data.split(":", 2)[2])
            route = results[result_index]
        except (ValueError, IndexError, TypeError):
            return await query.answer("关键词结果已失效，请重新搜索。", show_alert=True)
        context.user_data.pop(KEYWORD_RESULTS_KEY, None)
        context.user_data.pop(KEYWORD_INPUT_KEY, None)
        context.user_data[COMMENT_TARGET_KEY] = {
            "channel_id": route["channel_id"],
            "message_id": route["channel_message_id"],
        }
        context.user_data["waiting_post"] = True
        link = await _route_message_link(context, route)
        await query.answer()
        return await query.edit_message_text(
            f"✅ 已选择 {route.get('label', '关键词')}：{route.get('raw', route.get('key'))}。\n"
            "请发送投稿内容，审核通过后会评论到对应帖子下，并发布到转发频道。",
            reply_markup=_keyword_route_keyboard(
                route,
                link,
                context.user_data.get("post_no_name", True),
            ),
        )

    # All remaining actions below are publishing configuration actions.  Do not
    # rely on the hidden start-menu button: callbacks can be forged manually.
    public_actions = {"publish", "channel_message", "bottle_prev", "bottle_next", "accept_friend", "reject_friend", "back"}
    if action not in public_actions and not has_admin_permission(
        context, query.from_user.id, "submission_config"
    ):
        return await query.answer("你没有投稿配置权限。", show_alert=True)

    await query.answer()

    if action == "toggle_template_publish":
        config["template_publish_enabled"] = not bool(config.get("template_publish_enabled", False))
        save_publish_config(config)
        return await query.edit_message_text(
            f"模板发布已{'开启' if config['template_publish_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "template_settings":
        return await query.edit_message_text(
            _template_settings_text(config),
            reply_markup=_template_settings_keyboard(config),
        )

    if action == "template_add":
        next_id = max((int(item.get("id", 0) or 0) for item in _publish_templates(config)), default=0) + 1
        context.user_data[TEMPLATE_DRAFT_KEY] = {
            "id": next_id, "step": "name", "format": "plain", "buttons": []
        }
        return await query.edit_message_text(
            "请输入模板名称，例如：招聘发布。\n发送“取消”可放弃添加。"
        )

    if action == "template_format":
        draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
        mode = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if not isinstance(draft, dict) or mode not in {"plain", "html"}:
            return await query.answer("模板草稿已失效，请重新添加。", show_alert=True)
        draft["format"] = mode
        draft["step"] = "buttons"
        return await query.edit_message_text(
            "请输入超链接按钮，每行一个：\n"
            "按钮文字 | https://example.com\n\n"
            "没有按钮请发送“无”。按钮文字和链接都可使用 {键名}。"
        )

    if action == "template_save":
        draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
        if not isinstance(draft, dict) or not draft.get("name") or not draft.get("text"):
            return await query.answer("模板草稿不完整，请重新添加。", show_alert=True)
        templates = _publish_templates(config)
        templates = [item for item in templates if int(item.get("id", 0) or 0) != int(draft["id"])]
        templates.append({
            "id": int(draft["id"]),
            "name": str(draft["name"])[:50],
            "text": str(draft["text"]),
            "format": str(draft.get("format") or "plain"),
            "buttons": list(draft.get("buttons") or []),
        })
        config["publish_templates"] = templates
        save_publish_config(config)
        context.user_data.pop(TEMPLATE_DRAFT_KEY, None)
        return await query.edit_message_text(
            "✅ 模板已保存。\n\n" + _template_settings_text(config),
            reply_markup=_template_settings_keyboard(config),
        )

    if action == "template_cancel":
        context.user_data.pop(TEMPLATE_DRAFT_KEY, None)
        context.user_data.pop(TEMPLATE_FLOW_KEY, None)
        return await query.edit_message_text(
            _template_settings_text(config),
            reply_markup=_template_settings_keyboard(config),
        )

    if action == "template_view":
        try:
            template_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("模板数据无效。", show_alert=True)
        template = _template_by_id(config, template_id)
        if not template:
            return await query.answer("模板不存在。", show_alert=True)
        return await query.edit_message_text(
            _template_detail_text(template),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 删除模板", callback_data=f"publish:template_delete:{template_id}")],
                [InlineKeyboardButton("⬅️ 返回模板列表", callback_data="publish:template_settings")],
            ]),
        )

    if action == "template_delete":
        try:
            template_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("模板数据无效。", show_alert=True)
        config["publish_templates"] = [
            item for item in _publish_templates(config)
            if int(item.get("id", 0) or 0) != template_id
        ]
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 模板已删除。\n\n" + _template_settings_text(config),
            reply_markup=_template_settings_keyboard(config),
        )

    if action == "template_publish":
        if not bool(config.get("template_publish_enabled", False)):
            return await query.answer("模板发布尚未开启，请先在投稿设置中开启。", show_alert=True)
        templates = _publish_templates(config)
        if not templates:
            return await query.answer("请先在模板配置中添加模板。", show_alert=True)
        rows = [[InlineKeyboardButton(
            f"🧩 {str(item.get('name') or '未命名')[:48]}",
            callback_data=f"publish:template_use:{int(item['id'])}",
        )] for item in templates]
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
        return await query.edit_message_text("请选择要发布的模板：", reply_markup=InlineKeyboardMarkup(rows))

    if action == "template_use":
        try:
            template_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("模板数据无效。", show_alert=True)
        template = _template_by_id(config, template_id)
        if not template:
            return await query.answer("模板不存在。", show_alert=True)
        keys = _template_keys(template)
        context.user_data[TEMPLATE_FLOW_KEY] = {"template_id": template_id, "keys": keys, "index": 0, "values": {}}
        if not keys:
            context.user_data[TEMPLATE_FLOW_KEY]["ready"] = True
            return await query.edit_message_text(
                _template_flow_preview(template, {}),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 确认发布", callback_data="publish:template_confirm")],
                    [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
                ]),
                parse_mode="HTML" if template.get("format") == "html" else None,
            )
        return await query.edit_message_text(
            f"请填写 {len(keys)} 项中的第 1 项：\n\n<b>{html.escape(keys[0])}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")]]),
            parse_mode="HTML",
        )

    if action == "template_confirm":
        flow = context.user_data.get(TEMPLATE_FLOW_KEY)
        if not isinstance(flow, dict) or not flow.get("ready"):
            return await query.answer("模板填写尚未完成。", show_alert=True)
        template = _template_by_id(config, int(flow.get("template_id", 0) or 0))
        channel_id_int = _as_int(config.get("channel_id"))
        if not template or channel_id_int is None:
            return await query.answer("模板或发布频道不存在。", show_alert=True)
        rendered_text, markup, parse_mode = _render_template(template, flow.get("values", {}))
        # Template publishing follows the same main-post rules as ordinary
        # admin publishing: keyword registration, comment routing and optional
        # report link creation all use the configured publish channel.
        entries = _extract_routing_keywords(SimpleNamespace(text=rendered_text, caption=None), config)
        report_id = ""
        report_url = ""
        text_to_publish = rendered_text
        if bool(config.get("comment_forward_enabled", False)) and bool(config.get("report_link_enabled", False)):
            report_id = uuid.uuid4().hex[:16]
            report_url = await _report_deep_link(context, report_id) or ""
            if report_url:
                link_for_text = html.escape(report_url) if parse_mode == "HTML" else report_url
                text_to_publish = f"{rendered_text.rstrip()}\n\n📋 评论报告：{link_for_text}"
        try:
            published = await context.bot.send_message(
                chat_id=channel_id_int,
                text=text_to_publish,
                parse_mode=parse_mode,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            return await query.answer(f"发布失败：{str(exc)[:100]}", show_alert=True)
        if bool(config.get("comment_forward_enabled", False)):
            _register_post_keywords(entries, channel_id_int, published.message_id)
        if report_id and report_url:
            _create_comment_report(
                channel_id_int,
                published.message_id,
                report_id,
                subject_entries=entries,
            )
        context.user_data.pop(TEMPLATE_FLOW_KEY, None)
        await query.answer("✅ 模板已发布。")
        return await query.edit_message_text(
            "✅ 模板已按投稿规则发布到频道。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回首页", callback_data="start:back")]]),
        )

    if action == "publishset":
        help_text = "📣 请设置发布的频道"
        await query.edit_message_text(
            help_text,
            reply_markup=publish_setting_keyboard(config)
        )

    if action == "toggle_submission":
        config["submission_enabled"] = not bool(config.get("submission_enabled", True))
        save_publish_config(config)
        return await query.edit_message_text(
            f"投稿开关已{'开启' if config['submission_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_continuous_submission":
        config["continuous_submission_enabled"] = not bool(
            config.get("continuous_submission_enabled", True)
        )
        save_publish_config(config)
        mode_text = "一次投稿完成后可继续发送" if config["continuous_submission_enabled"] else "一次投稿完成后自动结束"
        return await query.edit_message_text(
            f"连续投稿已{'开启' if config['continuous_submission_enabled'] else '关闭'}。\n{mode_text}",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "custom_buttons":
        if not has_admin_permission(context, query.from_user.id, "submission_config"):
            return await query.answer("你没有投稿配置权限。", show_alert=True)

        return await query.edit_message_text(
            _custom_user_buttons_text(config),
            reply_markup=_custom_user_buttons_keyboard(config),
        )

    if action == "custom_toggle":
        if not has_admin_permission(context, query.from_user.id, "submission_config"):
            return await query.answer("你没有投稿配置权限。", show_alert=True)

        key = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if not key:
            return await query.answer("按钮配置无效。", show_alert=True)
        current = _custom_button_visible(config, key)
        if not _set_custom_button_visible(config, key, not current):
            return await query.answer("按钮配置无效。", show_alert=True)
        save_publish_config(config)
        return await query.edit_message_text(
            _custom_user_buttons_text(config),
            reply_markup=_custom_user_buttons_keyboard(config),
        )

    if action == "toggle_random_view":
        config["random_view_enabled"] = not bool(config.get("random_view_enabled", True))
        save_publish_config(config)
        return await query.edit_message_text(
            f"随机查看开关已{'开启' if config['random_view_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_bottom_buttons":
        config["bottom_buttons_enabled"] = not bool(
            config.get("bottom_buttons_enabled", True)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"发布底部按钮已{'显示' if config['bottom_buttons_enabled'] else '隐藏'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_proof_required":
        config["proof_required"] = not bool(config.get("proof_required", False))
        save_publish_config(config)
        return await query.edit_message_text(
            f"审核凭证已{'开启' if config['proof_required'] else '关闭'}。"
            "开启后，普通投稿人需上传凭证才会提交审核。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_reject_reason_required":
        config["reject_reason_required"] = not bool(
            config.get("reject_reason_required", False)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"拒绝原因已{'开启' if config['reject_reason_required'] else '关闭'}。"
            "开启后，审核人拒绝投稿时需要填写原因。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_comment_forward":
        config["comment_forward_enabled"] = not bool(
            config.get("comment_forward_enabled", False)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"评论并转发已{'开启' if config['comment_forward_enabled'] else '关闭'}。"
            "开启后，管理员发布到主频道的帖子会显示评论按钮。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "migrate_forward_comments":
        target_channel_id = _as_int(config.get("forward_channel_id"))
        if target_channel_id is None:
            return await query.answer("请先设置新的转发频道。", show_alert=True)
        if context.application.bot_data.get("comment_migration_in_progress"):
            return await query.answer("历史评论正在迁移，请稍候。", show_alert=True)
        total = _count_migratable_report_comments(target_channel_id)
        if total <= 0:
            return await query.answer("没有需要迁移的已记录评论。", show_alert=True)
        context.application.bot_data["comment_migration_in_progress"] = True
        notify_chat_id = query.message.chat_id if query.message else query.from_user.id
        context.application.create_task(
            _run_comment_migration_task(context, target_channel_id, notify_chat_id),
            name=f"comment-migration:{target_channel_id}",
        )
        await query.answer("已开始迁移。", show_alert=False)
        return await query.edit_message_text(
            f"🔄 正在迁移 {total} 条历史评论到新转发频道：{target_channel_id}\n"
            "完成后会向你发送结果，报告链接会自动改为定位新频道。"
        )

    if action == "toggle_report_link":
        config["report_link_enabled"] = not bool(config.get("report_link_enabled", False))
        save_publish_config(config)
        note = "管理员发布主频道帖子时会附加“查看报告”链接；评论审核通过并转发后会收录到报告。"
        if not bool(config.get("comment_forward_enabled", False)):
            note += "\n⚠️ 请同时开启“评论并转发”。"
        return await query.edit_message_text(
            f"报告链接已{'开启' if config['report_link_enabled'] else '关闭'}。\n{note}",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "keywords":
        return await query.edit_message_text(
            _keyword_settings_text(config),
            reply_markup=_keyword_settings_keyboard(),
        )

    if action == "keywords_set":
        context.user_data[KEYWORD_LABEL_INPUT_KEY] = True
        return await query.edit_message_text(
            "请输入需要提取的字段标签，多个用逗号或换行分隔。\n"
            "例如：艺名, 联系方式",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回", callback_data="publish:keywords")]
            ]),
        )

    if action == "keywords_clear":
        config["keyword_extract_labels"] = []
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 已清空关键词提取标签。\n\n" + _keyword_settings_text(config),
            reply_markup=_keyword_settings_keyboard(),
        )

    if action == "forward_channel":
        context.user_data["waiting_forward_channel_id"] = True
        target = config.get("forward_channel_id")
        return await query.edit_message_text(
            f"📨 当前转发频道：{target if target else '未设置'}\n\n"
            "请输入转发频道 ID，例如：-1001234567890",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
            ]),
        )

    if action == "channel":

        text = (
            f"📢 当前频道：\n{channel_id}"
            if channel_id
            else "📢 当前未设置发布频道。请将频道任意一条消息转发给机器人，获取频道 ID 后输入即可设置。"
        )

        return await query.edit_message_text(
            text,
            reply_markup=publish_channel_keyboard()
        )

    if action == "set_channel":
        context.user_data["waiting_channel_id"] = True

        return await query.edit_message_text(
            "请输入频道ID\n\n例如：\n-1001234567890"
        )

    if action == "review":
        return await query.edit_message_text(
            f"📝 审核状态：{'开启' if config['review_enabled'] else '关闭'}",
            reply_markup=publish_review_keyboard(
                config["review_enabled"]
            )
        )

    if action == "toggle_review":
        config["review_enabled"] = not config["review_enabled"]
        save_publish_config(config)

        return await query.edit_message_text(
            f"📝 审核状态：{'开启' if config['review_enabled'] else '关闭'}",
            reply_markup=publish_review_keyboard(
                config["review_enabled"]
            )
        )

    if action == "limit":
        limit = config["daily_limit"]

        return await query.edit_message_text(
            f"📊 当前每日发布上限：{'♾ 不限制' if limit <= 0 else limit}",
            reply_markup=publish_limit_keyboard(limit)
        )

    if action == "set_limit":
        context.user_data["waiting_limit"] = True

        return await query.edit_message_text(
            "请输入每日发布上限数字"
        )

    if action == "unlimit":
        config["daily_limit"] = 0
        save_publish_config(config)

        return await query.edit_message_text(
            "✅ 已设置为不限制",
            reply_markup=publish_limit_keyboard(0)
        )

    if action == "ads":
        return await query.edit_message_text(
            f"📣 广告状态：{'开启' if config['ads_enabled'] else '关闭'}",
            reply_markup=publish_ads_keyboard(
                config["ads_enabled"]
            )
        )

    if action == "toggle_ads":
        config["ads_enabled"] = not config["ads_enabled"]
        save_publish_config(config)

        return await query.edit_message_text(
            f"📣 广告状态：{'开启' if config['ads_enabled'] else '关闭'}",
            reply_markup=publish_ads_keyboard(
                config["ads_enabled"]
            )
        )

    if action == "add_ad":

        context.user_data["waiting_add_ad"] = True

        return await query.edit_message_text(
            "请输入广告内容：\n\n例如：\n欢迎加入交流群 https://t.me/xxx"
        )
    if action == "list_ad":

        ads = config.get("ads", [])

        if not ads:
            return await query.edit_message_text(
                "暂无广告"
            )

        lines = ["📋 广告列表", ""]
        rows = []

        for ad in ads:
            lines.append(
                f"#{ad['id']} {'✅' if ad['enabled'] else '❌'}"
            )

            rows.append([
                InlineKeyboardButton(
                    f"{'✅' if ad['enabled'] else '❌'} #{ad['id']}",
                    callback_data=f"publish:toggle_ad_{ad['id']}"
                )
            ])

        return await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows)
        )

    if action == "delete_ad":

        ads = config.get("ads", [])

        if not ads:
            return await query.edit_message_text("暂无广告")

        rows = []

        for ad in ads:
            rows.append([
                InlineKeyboardButton(
                    f"🗑 删除 #{ad['id']}",
                    callback_data=f"publish:delete_ad_{ad['id']}"
                )
            ])

        rows.append([
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="publish:ads"
            )
        ])

        return await query.edit_message_text(
            "请选择要删除的广告",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    if action.startswith("delete_ad_"):

        ad_id = int(action.replace("delete_ad_", ""))

        ads = config.get("ads", [])

        ads = [x for x in ads if x["id"] != ad_id]

        config["ads"] = ads

        save_publish_config(config)

        return await query.edit_message_text(
            "✅ 删除成功"
        )
    if action == "edit_ad":

        ads = config.get("ads", [])

        rows = []

        for ad in ads:
            rows.append([
                InlineKeyboardButton(
                    f"📝 #{ad['id']}",
                    callback_data=f"publish:edit_ad_{ad['id']}"
                )
            ])

        return await query.edit_message_text(
            "选择广告",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    if action.startswith("edit_ad_"):

        ad_id = int(action.replace("edit_ad_", ""))

        context.user_data["edit_ad_id"] = ad_id
        context.user_data["waiting_edit_ad"] = True

        return await query.edit_message_text(
            f"请输入新的广告内容\n广告ID:{ad_id}"
        )

    if action.startswith("toggle_ad_"):

        ad_id = int(action.replace("toggle_ad_", ""))

        for ad in config["ads"]:
            if ad["id"] == ad_id:
                ad["enabled"] = not ad["enabled"]
                break

        save_publish_config(config)

        await query.edit_message_text(
        "📋 广告列表（点击切换启用状态）",
        reply_markup=build_ads_list_keyboard(config["ads"])
    )
        return

    if action == "buttons":
        _clear_button_input(context)
        return await query.edit_message_text(
            _button_settings_text(config),
            reply_markup=publish_button_settings_keyboard(config),
        )

    if action == "button_add":
        buttons = _publish_buttons(config)
        if len(buttons) >= MAX_PUBLISH_BUTTONS:
            return await query.edit_message_text(
                f"最多只能设置 {MAX_PUBLISH_BUTTONS} 个投稿按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        context.user_data["waiting_post"] = False
        context.user_data["publish_button_input"] = {"mode": "add", "step": "text"}
        return await query.edit_message_text(
            "请输入按钮文字（最多 64 个字符）。\n例如：加入交流群"
        )

    if action == "button_edit":
        buttons = _publish_buttons(config)
        if not buttons:
            return await query.edit_message_text(
                "当前未设置按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        return await query.edit_message_text(
            "请选择要修改的按钮：",
            reply_markup=_button_selection_keyboard(config, "edit"),
        )

    if action.startswith("button_edit_"):
        try:
            button_id = int(action.removeprefix("button_edit_"))
        except ValueError:
            return await query.answer("按钮数据无效", show_alert=True)
        if not any(button.get("id") == button_id for button in _publish_buttons(config)):
            return await query.answer("按钮不存在或已删除", show_alert=True)
        context.user_data["waiting_post"] = False
        context.user_data["publish_button_input"] = {
            "mode": "edit",
            "step": "text",
            "button_id": button_id,
        }
        return await query.edit_message_text(
            "请输入新的按钮文字（最多 64 个字符）。"
        )

    if action == "button_delete":
        buttons = _publish_buttons(config)
        if not buttons:
            return await query.edit_message_text(
                "当前未设置按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        return await query.edit_message_text(
            "请选择要删除的按钮：",
            reply_markup=_button_selection_keyboard(config, "delete"),
        )

    if action.startswith("button_delete_"):
        try:
            button_id = int(action.removeprefix("button_delete_"))
        except ValueError:
            return await query.answer("按钮数据无效", show_alert=True)
        buttons = _publish_buttons(config)
        updated_buttons = [button for button in buttons if button.get("id") != button_id]
        if len(updated_buttons) == len(buttons):
            return await query.answer("按钮不存在或已删除", show_alert=True)
        config["buttons"] = updated_buttons
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 按钮已删除。\n\n" + _button_settings_text(config),
            reply_markup=publish_button_settings_keyboard(config),
        )

    if action == "back":
        return await query.edit_message_text(
            "⚙️ 发布设置",
            reply_markup=publish_setting_keyboard(config)
        )

    if action == "publish":
        owner_id = _owner_id(context)
        if (
            bool(config.get("comment_forward_enabled", False))
            and query.from_user.id != owner_id
            and not isinstance(context.user_data.get(COMMENT_TARGET_KEY), dict)
        ):
            if not _keyword_labels(config):
                return await query.edit_message_text(
                    "❗ 当前未配置关键词提取标签，请联系管理员在投稿设置中配置。"
                )
            context.user_data[KEYWORD_INPUT_KEY] = True
            context.user_data["waiting_post"] = False
            return await query.edit_message_text(
                "🔎 请输入要查询的关键词（例如：@×××或名字）。\n"
                "找到对应帖子后，再发送投稿内容。"
            )
        await publish_message(update, context)

    if action == "channel_message":

        context.user_data["reply_bottle"] = True

        user_id = query.from_user.id

        posts = _load_cannel_message()

        history_data = _load_bottle_history()

        user_key = str(user_id)

        if user_key not in history_data:
            history_data[user_key] = {
                "history": [],
                "index": -1
            }

        user_info = history_data[user_key]

        history = user_info["history"]

        available_posts = [
            p for p in posts
            if p["user_id"] != user_id
            and p["channel_message_id"] not in history
        ]

        if not available_posts:
            await query.message.reply_text("没有更多资源了")
            return

        post = random.choice(available_posts)

        history.append(post["channel_message_id"])

        user_info["index"] = len(history) - 1

        _save_bottle_history(history_data)

        await send_bottle(
            context,
            user_id,
            post.get("channel_id") or channel_id,
            post,
        )

    if action == "global_ad_toggle":

        enabled = not context.user_data["post_no_name"]

        await query.answer("✅ 已更新", show_alert=False)


        help_text = "📣 请发送您要的内容。\n\n支持文字、图片、视频等消息。 点击返回停止发送"

        context.user_data["post_no_name"] = enabled

        await query.edit_message_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )

    if action == "bottle_next":
        user_id = query.from_user.id

        posts = _load_cannel_message()

        history_data = _load_bottle_history()

        user_info = history_data.get(str(user_id))

        if not user_info:
            await query.message.reply_text("请先请求一个资源")
            return

        history = user_info["history"]
        index = user_info["index"]

        # 已浏览历史里还有下一条
        if index < len(history) - 1:
            index += 1
            user_info["index"] = index
            _save_bottle_history(history_data)
            message_id = history[index]
            post = next(
                (item for item in posts if item.get("channel_message_id") == message_id),
                None,
            )
        else:
            available_posts = [
                p for p in posts
                if p["user_id"] != user_id
                and p["channel_message_id"] not in history
            ]
            if not available_posts:
                await query.message.reply_text("没有更多资源了")
                return

            post = random.choice(available_posts)
            history.append(post["channel_message_id"])
            user_info["index"] = len(history) - 1
            _save_bottle_history(history_data)
            message_id = post["channel_message_id"]

        await query.message.delete()
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=(post or {}).get("channel_id") or channel_id,
            message_id=message_id,
            reply_markup=query.message.reply_markup,
        )

    if action == "bottle_prev":

        user_id = query.from_user.id

        history_data = _load_bottle_history()

        user_info = history_data.get(str(user_id))

        if not user_info:
            return

        index = user_info["index"]

        if index <= 0:
            await query.message.reply_text(
                "已经是第一条了"
            )
            return

        index -= 1

        user_info["index"] = index

        _save_bottle_history(history_data)

        message_id = user_info["history"][index]
        post = next(
            (item for item in _load_cannel_message() if item.get("channel_message_id") == message_id),
            None,
        )

        await query.message.delete()
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=(post or {}).get("channel_id") or channel_id,
            message_id=message_id,
            reply_markup=query.message.reply_markup,
        )

    if action == "add_friend":

        target_user_id = int(query.data.split(":")[2])
        target_user_id = int(target_user_id)
        from_user_id = query.from_user.id

        history_data = _load_bottle_history()
        user_key = str(from_user_id)
        friend_applied = history_data[user_key].setdefault(
            "friend_applied",
            {}
        )

        if str(target_user_id) in friend_applied:
            await query.message.reply_text(
                "你已经发送过好友申请了"
            )
            return

        accepter = get_user(from_user_id)

        await context.bot.send_message(
            chat_id=target_user_id,
            text= (
                f"@{accepter['username']}  想认识你"
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ 同意",
                        callback_data=f"publish:accept_friend:{from_user_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ 拒绝",
                        callback_data=f"publish:reject_friend:{from_user_id}"
                    )
                ]
            ])
        )

        await query.message.reply_text(
            "好友申请发送成功"
        )

        friend_applied[str(target_user_id)] = True
        _save_bottle_history(history_data)
    if action == "accept_friend":
        requester_id = int(query.data.split(":")[2])

        accepter_id = query.from_user.id

        print("申请人:", requester_id)
        print("同意人:", accepter_id)

        # 从数据库读取双方信息
        requester = get_user(requester_id)
        accepter = get_user(accepter_id)

        # 通知申请人
        await context.bot.send_message(
            chat_id=requester_id,
            text=(
                "🎉 对方已同意交换联系方式\n\n"
                f"用户名：@{accepter['username']}"
            )
        )

        await query.message.delete()

        # 通知同意人
        await query.message.reply_text(
            f"已向对方发送你的联系方式，对方联系方式：@{requester['username']}"
        )



    if action == "reject_friend":
        requester_id = int(query.data.split(":")[2])

        await context.bot.send_message(
            chat_id=requester_id,
            text="❌ 对方拒绝了你的好友申请"
        )

        await query.message.delete()

        await query.message.reply_text("已拒绝")

async def publish_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["waiting_post"] = True

    enabled = context.user_data.get("post_no_name", True)

    help_text = "📣 请发送您要投稿的内容。\n\n支持文字、图片、视频等消息。"

    context.user_data["post_no_name"] = enabled

    if query:

        await query.edit_message_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )

    else:
        await update.message.reply_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )

def create_post_keyboard(enabled: bool):
    rows = [
        [
            # InlineKeyboardButton(
            #     f"{'✅' if enabled else '🚫'} 匿名模式",
            #     callback_data=f"{CALLBACK_PREFIX}:global_ad_toggle",
            # ),
            InlineKeyboardButton(
                "✅ 继续发",
                callback_data="publish:publish",
            ),
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="start:back",
            ),
        ]
    ]
    return InlineKeyboardMarkup(rows)


def _finish_submission_if_needed(context: ContextTypes.DEFAULT_TYPE, config: dict) -> None:
    """End the current submission session when continuous submission is off."""
    if bool(config.get("continuous_submission_enabled", True)):
        return
    context.user_data["waiting_post"] = False
    context.user_data.pop(PENDING_PROOF_KEY, None)
    context.user_data.pop(COMMENT_TARGET_KEY, None)


async def handle_wall_publish(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_post"):
        return

    msg = update.message
    if not msg or not msg.from_user:
        return

    config = load_publish_config()
    main_channel_id = _as_int(config.get("channel_id"))
    if main_channel_id is None:
        await msg.reply_text("✅ 暂未配置发布频道，感谢参与！")
        return

    # The next user message is the proof for an already saved submission.
    proof_submission_id = context.user_data.get(PENDING_PROOF_KEY)
    if proof_submission_id:
        pending = _load_pending_submissions()
        submission = pending.get(proof_submission_id)
        if not isinstance(submission, dict) or submission.get("status") != "awaiting_proof":
            context.user_data.pop(PENDING_PROOF_KEY, None)
            return await msg.reply_text("❗ 未找到待上传凭证的投稿，请重新发起投稿。")

        submission["proof_chat_id"] = msg.chat_id
        submission["proof_message_id"] = msg.message_id
        submission["proof_uploaded_at"] = int(time.time())
        submission["status"] = "pending"
        pending[proof_submission_id] = submission
        _save_pending_submissions(pending)
        try:
            await _send_submission_for_review(context, proof_submission_id, submission)
        except Exception as exc:
            submission.pop("proof_chat_id", None)
            submission.pop("proof_message_id", None)
            submission.pop("proof_uploaded_at", None)
            submission["status"] = "awaiting_proof"
            pending[proof_submission_id] = submission
            _save_pending_submissions(pending)
            print("转发投稿及凭证审核失败:", exc)
            await msg.reply_text(f"❌ 提交审核失败：{exc}")
            return

        context.user_data.pop(PENDING_PROOF_KEY, None)
        _finish_submission_if_needed(context, config)
        await msg.reply_text("🕒 凭证已收到，投稿已提交，等待管理员审核。")
        return

    owner_id = _owner_id(context)
    is_owner_submission = bool(
        msg.from_user.id == owner_id
        or has_admin_permission(context, msg.from_user.id, "submission_config")
    )
    comment_target = context.user_data.pop(COMMENT_TARGET_KEY, None)
    comment_target = comment_target if isinstance(comment_target, dict) else None
    comment_forward_enabled = bool(config.get("comment_forward_enabled", False))

    submission_kind = "main"
    target_channel_id = main_channel_id
    if comment_forward_enabled and comment_target:
        submission_kind = "comment"
    elif comment_forward_enabled and not is_owner_submission:
        submission_kind = "forward"
        target_channel_id = _as_int(config.get("forward_channel_id"))
        if target_channel_id is None:
            await msg.reply_text("❌ 未设置转发频道，暂时无法提交投稿。")
            return

    # Non-owner submissions use review when it is enabled.
    if bool(config.get("review_enabled", False)) and not is_owner_submission:
        if owner_id is None:
            await msg.reply_text("❌ 未配置机器人所有者，暂时无法提交审核。")
            return

        submission_id = uuid.uuid4().hex[:16]
        submission = {
            "status": "pending",
            "user_id": msg.from_user.id,
            "user_chat_id": msg.chat_id,
            "username": msg.from_user.username,
            "author": _submission_author_text(msg),
            "report_author": _comment_author(msg),
            "report_content": _comment_content(msg),
            "user_message_id": msg.message_id,
            "submitted_at": int(time.time()),
            "submission_kind": submission_kind,
            "target_channel_id": target_channel_id,
        }
        if comment_target:
            submission["comment_target"] = comment_target

        pending = _load_pending_submissions()
        if bool(config.get("proof_required", False)):
            submission["status"] = "awaiting_proof"
            pending[submission_id] = submission
            _save_pending_submissions(pending)
            context.user_data[PENDING_PROOF_KEY] = submission_id
            await msg.reply_text(
                "📎 请继续上传审核凭证。\n"
                "支持文字、图片、视频、文件等；凭证仅供审核，不会发布到频道。"
            )
            return

        pending[submission_id] = submission
        _save_pending_submissions(pending)
        try:
            await _send_submission_for_review(context, submission_id, submission)
        except Exception as exc:
            pending.pop(submission_id, None)
            _save_pending_submissions(pending)
            print("转发投稿审核失败:", exc)
            await msg.reply_text(f"❌ 提交审核失败：{exc}")
            return

        _finish_submission_if_needed(context, config)
        await msg.reply_text("🕒 投稿已提交，等待管理员审核。")
        return

    # Owner's normal post stays in main channel and receives a comment button.
    submission = {
        "user_chat_id": msg.chat_id,
        "user_message_id": msg.message_id,
        "report_author": _comment_author(msg),
        "report_content": _comment_content(msg),
        "keyword_entries": (
            _extract_routing_keywords(msg, config)
            if comment_forward_enabled and is_owner_submission and submission_kind == "main"
            else []
        ),
    }
    report_id = ""
    report_url = ""
    if (
        bool(config.get("report_link_enabled", False))
        and comment_forward_enabled
        and is_owner_submission
        and submission_kind == "main"
    ):
        report_id = uuid.uuid4().hex[:16]
        report_url = await _report_deep_link(context, report_id) or ""

    try:
        if submission_kind == "comment":
            published = await _publish_comment_and_forward(context, {
                **submission,
                "comment_target": comment_target,
            }, config)
            target_channel_id = _as_int(config.get("forward_channel_id"))
        else:
            published = await _copy_submission_to_channel(
                context,
                submission,
                target_channel_id,
                config,
                add_comment_button=(
                    comment_forward_enabled
                    and is_owner_submission
                    and submission_kind == "main"
                ),
            )
        if report_id and report_url:
            _create_comment_report(
                int(target_channel_id),
                published.message_id,
                report_id,
                subject_entries=submission.get("keyword_entries", []),
            )
            embedded = await _append_report_link_to_original_post(
                context,
                msg,
                int(target_channel_id),
                published.message_id,
                report_url,
            )
            if not embedded:
                try:
                    await _send_report_link_companion(
                        context, int(target_channel_id), report_url
                    )
                except Exception as exc:
                    # The report remains available by deep link; failure to
                    # send the fallback companion must not roll back the post.
                    print(f"发送评论报告入口失败: {exc}")
        _append_published_submission(msg, published.message_id, target_channel_id)
        _finish_submission_if_needed(context, config)
        await msg.reply_text("✅ 发送成功")
    except Exception as exc:
        print("投稿失败:", exc)
        await msg.reply_text(f"❌ 发送失败：{exc}")

# =========================
# 文本输入处理
# =========================

async def _handle_template_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or not msg.text:
        return False
    text = msg.text.strip()

    draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
    if isinstance(draft, dict):
        if text in {"取消", "返回"}:
            context.user_data.pop(TEMPLATE_DRAFT_KEY, None)
            await msg.reply_text("已取消模板编辑。")
            return True
        step = draft.get("step")
        if step == "name":
            if not text or len(text) > 50:
                await msg.reply_text("模板名称不能为空且不能超过 50 个字符。")
                return True
            draft["name"] = text
            draft["step"] = "text"
            await msg.reply_text(
                "请输入模板正文。可使用 {键名} 占位符，例如：\n"
                "【艺名】：{艺名}\n【联系】：{联系方式}"
            )
            return True
        if step == "text":
            if not text or len(text) > 3500:
                await msg.reply_text("模板正文不能为空且不能超过 3500 个字符。")
                return True
            draft["text"] = text
            draft["step"] = "format"
            await msg.reply_text(
                "请选择文本格式：",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("纯文本", callback_data="publish:template_format:plain")],
                    [InlineKeyboardButton("HTML 格式", callback_data="publish:template_format:html")],
                    [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
                ]),
            )
            return True
        if step == "buttons":
            if text in {"无", "跳过", "none"}:
                draft["buttons"] = []
            else:
                buttons = []
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    if "|" not in line:
                        await msg.reply_text("❗ 每行格式应为：按钮文字 | https://链接")
                        return True
                    label, url = [part.strip() for part in line.split("|", 1)]
                    test_url = TEMPLATE_KEY_PATTERN.sub("value", url)
                    if not label or not _normalize_button_url(test_url):
                        await msg.reply_text("❗ 按钮文字或链接无效，请重新输入。")
                        return True
                    buttons.append({"text": label[:64], "url": url})
                draft["buttons"] = buttons
            draft["step"] = "confirm"
            await msg.reply_text(
                _template_draft_preview(draft),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 保存模板", callback_data="publish:template_save")],
                    [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
                ]),
            )
            return True

    flow = context.user_data.get(TEMPLATE_FLOW_KEY)
    if isinstance(flow, dict) and not flow.get("ready"):
        if text in {"取消", "返回"}:
            context.user_data.pop(TEMPLATE_FLOW_KEY, None)
            await msg.reply_text("已取消模板发布。")
            return True
        keys = flow.get("keys", [])
        index = int(flow.get("index", 0) or 0)
        if index < 0 or index >= len(keys):
            context.user_data.pop(TEMPLATE_FLOW_KEY, None)
            await msg.reply_text("模板填写状态已失效，请重新选择模板。")
            return True
        flow.setdefault("values", {})[keys[index]] = text
        index += 1
        flow["index"] = index
        template = _template_by_id(load_publish_config(), int(flow.get("template_id", 0) or 0))
        if not template:
            context.user_data.pop(TEMPLATE_FLOW_KEY, None)
            await msg.reply_text("模板不存在，请重新选择。")
            return True
        if index < len(keys):
            await msg.reply_text(f"请填写第 {index + 1}/{len(keys)} 项：\n\n{keys[index]}")
            return True
        flow["ready"] = True
        await msg.reply_text(
            _template_flow_preview(template, flow.get("values", {})),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认发布", callback_data="publish:template_confirm")],
                [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
            ]),
            parse_mode="HTML" if template.get("format") == "html" else None,
        )
        return True

    return False


async def _handle_keyword_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not (context.user_data or {}).get(KEYWORD_INPUT_KEY):
        return False
    if (context.user_data or {}).get("waiting_post"):
        return False

    msg = update.message
    if not msg or not msg.text:
        if msg:
            await msg.reply_text("❗ 请发送文字关键词。")
        return True
    query = msg.text.strip()
    if query in {"取消", "返回"}:
        context.user_data.pop(KEYWORD_INPUT_KEY, None)
        await msg.reply_text("✅ 已取消关键词搜索。")
        return True
    routes = _find_keyword_routes(query)
    if not routes:
        await msg.reply_text("❗ 未找到可评论的对应帖子。请检查关键词，或等待管理员发布带关键词的新帖子后重试。")
        return True
    if len(routes) == 1:
        route = routes[0]
        context.user_data.pop(KEYWORD_INPUT_KEY, None)
        context.user_data[COMMENT_TARGET_KEY] = {"channel_id": route["channel_id"], "message_id": route["channel_message_id"]}
        context.user_data["waiting_post"] = True
        link = await _route_message_link(context, route)
        await msg.reply_text(
            f"✅ 已匹配 {route.get('label', '关键词')}：{route.get('raw', route.get('key'))}。\n"
            "请继续发送投稿内容，审核通过后会评论到对应帖子下，并发布到转发频道。",
            reply_markup=_keyword_route_keyboard(route, link, context.user_data.get("post_no_name", True)),
        )
        return True

    context.user_data[KEYWORD_RESULTS_KEY] = routes
    rows = []
    for index, route in enumerate(routes):
        label = str(route.get("label", "关键词"))[:12]
        value = str(route.get("raw", route.get("key", "")))[:30]
        rows.append([InlineKeyboardButton(f"选择 {label}：{value}", callback_data=f"publish:keyword_pick:{index}")])
        link = await _route_message_link(context, route)
        if link:
            rows.append([InlineKeyboardButton(f"🔗 查看 {value}", url=link)])
    await msg.reply_text("找到多个对应帖子，请选择：", reply_markup=InlineKeyboardMarkup(rows))
    return True


async def _handle_reject_reason_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    stage = (context.user_data or {}).get(REJECT_REASON_KEY)
    if not isinstance(stage, dict):
        return False

    msg = update.message
    if not msg:
        return True
    if not update.effective_user or not has_admin_permission(
        context, update.effective_user.id, "submission_review"
    ):
        context.user_data.pop(REJECT_REASON_KEY, None)
        return False
    if not msg.text:
        await msg.reply_text("❗ 请发送文字形式的拒绝原因，或发送“取消”。")
        return True

    submission_id = stage.get("submission_id")
    text = msg.text.strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(REJECT_REASON_KEY, None)
        pending = _load_pending_submissions()
        submission = pending.get(submission_id)
        try:
            if isinstance(submission, dict) and stage.get("review_chat_id"):
                await context.bot.edit_message_text(
                    chat_id=stage["review_chat_id"],
                    message_id=stage["review_message_id"],
                    text=_review_prompt_text({**submission, "id": submission_id}),
                    reply_markup=_review_keyboard(submission_id),
                )
        except Exception as exc:
            print("恢复审核消息失败:", exc)
        await msg.reply_text("✅ 已取消拒绝，投稿仍可继续审核。")
        return True

    if not text:
        await msg.reply_text("❗ 拒绝原因不能为空。")
        return True
    if len(text) > 3000:
        await msg.reply_text("❗ 拒绝原因不能超过 3000 个字符。")
        return True

    pending = _load_pending_submissions()
    submission = pending.get(submission_id)
    if not isinstance(submission, dict) or submission.get("status") != "pending":
        context.user_data.pop(REJECT_REASON_KEY, None)
        await msg.reply_text("❗ 该投稿已处理或不存在。")
        return True

    await _finalize_rejection(context, submission_id, submission, text)
    context.user_data.pop(REJECT_REASON_KEY, None)
    try:
        if stage.get("review_chat_id"):
            await context.bot.edit_message_text(
                chat_id=stage["review_chat_id"],
                message_id=stage["review_message_id"],
                text="❌ 投稿已拒绝，已通知投稿人。",
            )
    except Exception as exc:
        print("更新审核拒绝消息失败:", exc)
    await msg.reply_text("❌ 已拒绝投稿，已通知投稿人拒绝原因。")
    return True

async def _keyword_search_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Give active keyword search first chance to consume private text.

    If there is no active search/template flow, processing returns normally and
    the bidirectional private-forward handlers can continue.
    """
    if await _handle_template_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_keyword_search_input(update, context):
        raise ApplicationHandlerStop


async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_template_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_keyword_search_input(update, context):
        # Stop group=999 message_router and any later handler from sending the
        # search text into the bidirectional/private bot flow.
        raise ApplicationHandlerStop
    if await _handle_reject_reason_input(update, context):
        return

    await handle_wall_publish(update, context)

    if not update.message.text:
        return

    config = load_publish_config()

    if context.user_data.get(KEYWORD_LABEL_INPUT_KEY):
        labels = [
            value.strip()[:30]
            for value in re.split(r"[,，\n]+", update.message.text or "")
            if value.strip()
        ]
        if not labels:
            return await update.message.reply_text("❗ 请至少输入一个标签，例如：艺名")
        context.user_data.pop(KEYWORD_LABEL_INPUT_KEY, None)
        config["keyword_extract_labels"] = list(dict.fromkeys(labels))[:20]
        save_publish_config(config)
        return await update.message.reply_text(
            "✅ 关键词提取标签已保存。\n\n" + _keyword_settings_text(config),
            reply_markup=_keyword_settings_keyboard(),
        )

    button_input = context.user_data.get("publish_button_input")
    if isinstance(button_input, dict):
        value = update.message.text.strip()
        if button_input.get("step") == "text":
            if not value:
                return await update.message.reply_text("❗ 按钮文字不能为空，请重新输入。")
            if len(value) > BUTTON_TEXT_MAX_LENGTH:
                return await update.message.reply_text(
                    f"❗ 按钮文字不能超过 {BUTTON_TEXT_MAX_LENGTH} 个字符，请重新输入。"
                )
            button_input["text"] = value
            button_input["step"] = "url"
            return await update.message.reply_text(
                "请输入按钮链接。\n支持 https://、http://、tg:// 或 t.me/ 开头的链接。"
            )

        if button_input.get("step") == "url":
            url = _normalize_button_url(value)
            if not url:
                return await update.message.reply_text(
                    "❗ 链接格式不正确，请输入完整链接，例如：https://t.me/example"
                )

            buttons = _publish_buttons(config)
            if button_input.get("mode") == "edit":
                button_id = button_input.get("button_id")
                for button in buttons:
                    if button.get("id") == button_id:
                        button["text"] = button_input["text"]
                        button["url"] = url
                        break
                else:
                    _clear_button_input(context)
                    return await update.message.reply_text("❗ 按钮不存在或已删除。")
                result_text = "✅ 按钮修改成功。"
            else:
                if len(buttons) >= MAX_PUBLISH_BUTTONS:
                    _clear_button_input(context)
                    return await update.message.reply_text(
                        f"❗ 最多只能设置 {MAX_PUBLISH_BUTTONS} 个投稿按钮。"
                    )
                button_id = max(
                    (int(button.get("id", 0) or 0) for button in buttons), default=0
                ) + 1
                buttons.append({"id": button_id, "text": button_input["text"], "url": url})
                config["buttons"] = buttons
                result_text = "✅ 按钮添加成功。"

            save_publish_config(config)
            _clear_button_input(context)
            return await update.message.reply_text(
                result_text + "\n\n" + _button_settings_text(config),
                reply_markup=publish_button_settings_keyboard(config),
            )

    if context.user_data.get("waiting_forward_channel_id"):
        context.user_data["waiting_forward_channel_id"] = False
        try:
            forward_channel_id = int(update.message.text.strip())
        except (TypeError, ValueError):
            return await update.message.reply_text(
                "❗ 频道 ID 格式错误，例如：-1001234567890"
            )
        config["forward_channel_id"] = forward_channel_id
        save_publish_config(config)
        return await update.message.reply_text(
            f"✅ 已保存转发频道：{forward_channel_id}",
            reply_markup=publish_setting_keyboard(config),
        )

    if context.user_data.get("waiting_channel_id"):
        context.user_data["waiting_channel_id"] = False

        channel_id = update.message.text.strip()
        config["channel_id"] = int(channel_id)

        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 已保存频道：{channel_id}"
        )

    if context.user_data.get("waiting_limit"):
        context.user_data["waiting_limit"] = False

        limit = int(update.message.text.strip())


        config["daily_limit"] = limit

        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 已设置每日上限：{limit}"
        )
    if context.user_data.get("waiting_add_ad"):

        context.user_data["waiting_add_ad"] = False


        ads = config.get("ads", [])

        ad_id = max([x["id"] for x in ads], default=0) + 1

        ads.append({
            "id": ad_id,
            "title": f"广告{ad_id}",
            "content": update.message.text,
            "enabled": True
        })

        config["ads"] = ads
        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 广告添加成功\nID: {ad_id}"
        )
    if context.user_data.get("waiting_edit_ad"):

        context.user_data["waiting_edit_ad"] = False

        ad_id = context.user_data.pop("edit_ad_id")



        for ad in config["ads"]:
            if ad["id"] == ad_id:
                ad["content"] = update.message.text
                break

        save_publish_config(config)

        return await update.message.reply_text(
            "✅ 广告修改成功"
        )

def build_ads_list_keyboard(ads):
    rows = []

    for ad in ads:
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if ad['enabled'] else '❌'} #{ad['id']} {ad['content'][:20]}",
                callback_data=f"publish:toggle_ad_{ad['id']}"
            )
        ])

    rows.append([
        InlineKeyboardButton("⬅️ 返回", callback_data="publish:ads")
    ])

    return InlineKeyboardMarkup(rows)

def get_user(user_id):
    users = load_json(BOT_USER_FILE)
    return users.get(str(user_id))

def _load_bottle_history():
    data = load_json(BOTTLE_HISTORY_FILE)
    return data if isinstance(data, dict) else {}

def _save_bottle_history(data):
    save_json(BOTTLE_HISTORY_FILE, data)

def get_user_bottle_data(user_id):
    data = _load_bottle_history()

    user_key = str(user_id)

    if user_key not in data:
        data[user_key] = {
            "history": [],
            "index": -1
        }

    return data

async def send_bottle(
    context,
    chat_id,
    channel_id,
    post
):

    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ 上一条",
                callback_data="publish:bottle_prev"
            ),
            # InlineKeyboardButton(
            #     "👤 添加好友",
            #     callback_data=f"publish:add_friend:{post['user_id']}"
            # ),
            InlineKeyboardButton(
                "➡️ 下一条",
                callback_data="publish:bottle_next"
            ),
        ]
    ]

    await context.bot.copy_message(
        chat_id=chat_id,
        from_chat_id=channel_id,
        message_id=post["channel_message_id"],
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def _load_cannel_message() -> list:
    data = load_json(USER_MESSAGE_FILE)
    return data if isinstance(data, list) else []


# =========================
# 注册
# =========================

def register_publish_setting_handlers(app):
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern=r"^publish:.+"))
    # Linked discussion groups receive main-channel posts as automatic forwards.
    # Keep the correspondence so approved user comments can reply below the post.
    # Use TypeHandler instead of MessageHandler: Telegram can deliver a linked
    # discussion copy as message, edited_message, channel_post or
    # edited_channel_post. MessageHandler filters may skip one of those shapes.
    # The callback returns immediately for all unrelated updates.
    app.add_handler(
        TypeHandler(Update, _capture_comment_source_message),
        # Must run before generic group handlers that may stop processing.
        group=-940,
    )
    # Keyword search must run before bot.py's group=0 private-forward handlers.
    # If it does not consume the message, processing falls through normally.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (~filters.COMMAND)
            & ~filters.UpdateType.BUSINESS_MESSAGE,
            _keyword_search_interceptor,
        ),
        group=-20,
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (~filters.COMMAND)
            & ~filters.UpdateType.BUSINESS_MESSAGE,
            _handle_text_input,
        ),
        group=10,
    )
