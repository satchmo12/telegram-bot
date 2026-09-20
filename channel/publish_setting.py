# -*- coding: utf-8 -*-
from datetime import datetime
from types import SimpleNamespace
import html
from typing import Optional
from urllib.parse import quote, urlparse
import os
import random
import re
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, TypeHandler, filters
from telegram.error import BadRequest, Forbidden

from channel.channel_config import USER_MESSAGE_FILE
from utils import (
    BOT_USER_FILE,
    _can_manage,
    is_shared_session_name,
    is_super_admin,
    load_json,
    refresh_json_cache_if_changed,
    safe_reply,
    save_json,
)
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
POST_MIGRATION_STATE_FILE = "data/publish_post_migrations.json"
BACKUP_POST_MAP_FILE = "data/publish_backup_post_map.json"
CHECKIN_POSTS_FILE = "data/publish_checkin_posts.json"
HISTORY_FORWARD_STATE_FILE = "data/history_forward_state.json"
KEYWORD_INPUT_KEY = "publish_keyword_search"
KEYWORD_RESULTS_KEY = "publish_keyword_results"
KEYWORD_POST_RESULTS_KEY = "publish_keyword_post_results"
KEYWORD_POST_SEARCH_INPUT_KEY = "publish_keyword_post_search"
KEYWORD_LABEL_INPUT_KEY = "publish_keyword_label_input"
GROUP_KEYWORD_REPLY_STAGE_KEY = "publish_group_keyword_reply_stage"
REPORT_PAGE_SIZE = 6
REPORT_USERNAME_MIGRATION_KEY = "publish_report_username_migration"
TEMPLATE_DRAFT_KEY = "publish_template_draft"
TEMPLATE_FLOW_KEY = "publish_template_flow"
TEMPLATE_KEY_PATTERN = re.compile(r"\{([\w\-一-鿿]+)\}")

# =========================
# 配置读写
# =========================

def load_publish_config():
    # Config files are often edited directly in the management workspace.
    # Invalidate the five-minute JSON cache so keyword_extract_labels takes
    # effect on the very next publish or edit event.
    refresh_json_cache_if_changed(PUBLISH_CONFIG_FILE)
    default = {
        "channel_id": None,
        # Optional mirror for every main-channel post. It is never used for
        # keyword indexing/search, which always stays on channel_id.
        "backup_channel_id": None,
        # Default mirror transport is the Bot API; optionally use a selected
        # protocol account when the backup channel requires that identity.
        "backup_channel_use_telethon": False,
        # Compatibility alias for the forwarding protocol account.
        "backup_channel_session": "",
        "backup_channel_listen_session": "",
        "backup_channel_forward_session": "",
        # When the owner switches to a cloned replacement channel, remember
        # the old/new pair so historical report migration needs no old ID input.
        "pending_history_post_migration": None,
        "channel_change_history": [],
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
        # Group lookup: input label (e.g. 艺名) -> response label (e.g. 联系方式).
        "keyword_group_reply_enabled": False,
        "keyword_group_reply_rules": [],
        # Private “查找收录” result source: main / backup / all.
        "keyword_post_search_display_mode": "main",
        # Channel-post check-in / online roster settings.
        "checkin_enabled": False,
        "checkin_duration_hours": 8,
        "checkin_user_label": "联系",
        "checkin_display_label": "艺名",
        "checkin_group_label": "",
        "checkin_command_text": "打卡",
        "checkin_cancel_command_text": "取消打卡",
        "checkin_online_command_text": "在线宝宝",
        "checkin_online_text": "在线宝宝",
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
    normalized_labels = _keyword_labels(data)
    if data.get("keyword_extract_labels") != normalized_labels:
        data["keyword_extract_labels"] = normalized_labels
        changed = True
    if changed:
        save_json(PUBLISH_CONFIG_FILE, data)

    return data


def save_publish_config(data):
    save_json(PUBLISH_CONFIG_FILE, data)


def _backup_channel_id(config: dict, main_channel_id=None):
    backup = _as_int((config or {}).get("backup_channel_id"))
    main = _as_int(main_channel_id if main_channel_id is not None else (config or {}).get("channel_id"))
    if backup is None or backup == main:
        return None
    return backup


def _load_backup_post_map() -> dict:
    data = load_json(BACKUP_POST_MAP_FILE)
    if not isinstance(data, dict):
        data = {}
    mappings = data.get("mappings")
    if not isinstance(mappings, dict):
        mappings = {}
    data["mappings"] = mappings
    return data


def _save_backup_post_map(data: dict) -> None:
    mappings = data.get("mappings", {}) if isinstance(data, dict) else {}
    if isinstance(mappings, dict) and len(mappings) > 10000:
        oldest = sorted(
            mappings.items(),
            key=lambda item: int((item[1] or {}).get("created_at", 0) or 0),
        )[: len(mappings) - 10000]
        for key, _ in oldest:
            mappings.pop(key, None)
    save_json(BACKUP_POST_MAP_FILE, data)


def _record_backup_post_mapping(
    main_channel_id: int,
    main_message_id: int,
    backup_channel_id: int,
    backup_message_id: int,
) -> None:
    data = _load_backup_post_map()
    data["mappings"][_comment_map_key(main_channel_id, main_message_id)] = {
        "backup_channel_id": int(backup_channel_id),
        "backup_message_id": int(backup_message_id),
        "created_at": int(time.time()),
    }
    _save_backup_post_map(data)


def _backup_post_mapping(main_channel_id: int, main_message_id: int) -> dict:
    mapping = _load_backup_post_map().get("mappings", {}).get(
        _comment_map_key(main_channel_id, main_message_id)
    )
    return mapping if isinstance(mapping, dict) else {}


def _backup_channel_use_telethon(config: dict) -> bool:
    return bool((config or {}).get("backup_channel_use_telethon", False))


def _backup_channel_forward_session(config: dict) -> str:
    """Protocol account that posts the copy into the backup channel."""
    return str(
        (config or {}).get("backup_channel_forward_session")
        or (config or {}).get("backup_channel_session")
        or ""
    ).strip()


def _backup_channel_listen_session(config: dict) -> str:
    """Protocol account used to read the primary channel post."""
    return str(
        (config or {}).get("backup_channel_listen_session")
        or _backup_channel_forward_session(config)
        or ""
    ).strip()


def _backup_transport_label(config: dict) -> str:
    if not _backup_channel_use_telethon(config):
        return "🤖 机器人"
    listen_session = _backup_channel_listen_session(config)
    forward_session = _backup_channel_forward_session(config)
    if not forward_session:
        return "📱 协议号：未选择"
    if listen_session == forward_session:
        return f"📱 协议号：{forward_session}"
    return f"📱 监听 {listen_session} → 转发 {forward_session}"


async def _get_backup_telethon_client(
    context: ContextTypes.DEFAULT_TYPE,
    config: dict,
    *,
    role: str,
):
    if role not in {"listen", "forward"}:
        raise RuntimeError("备用频道协议号角色无效")
    session_name = (
        _backup_channel_listen_session(config)
        if role == "listen"
        else _backup_channel_forward_session(config)
    )
    if not session_name:
        raise RuntimeError(f"备用频道{('监听' if role == 'listen' else '转发')}协议号未选择")
    try:
        from channel.telethon_forwarder import SESSION_CLIENTS_BY_BOT, request_telethon_refresh
    except Exception as exc:
        raise RuntimeError(f"无法加载协议号组件：{exc}") from exc
    bot_name = str(context.application.bot_data.get("name", "") or "")
    client = SESSION_CLIENTS_BY_BOT.get(bot_name, {}).get(session_name)
    if client is None:
        # Do not open a second Telethon client against the same SQLite session.
        # The central forwarder owns those clients and will start this session
        # after a refresh request.
        request_telethon_refresh(bot_name)
        raise RuntimeError(
            f"协议号 {session_name} 尚未就绪，已请求协议号转发器刷新，请稍后重试"
        )
    return client


async def _mirror_main_post_via_telethon(
    context: ContextTypes.DEFAULT_TYPE,
    config: dict,
    main_channel_id: int,
    main_message_id: int,
    backup_channel_id: int,
):
    """Copy content between protocol accounts without Telegram forward source."""
    listen_client = await _get_backup_telethon_client(context, config, role="listen")
    forward_client = await _get_backup_telethon_client(context, config, role="forward")
    try:
        from channel.telethon_forwarder import _send_message_safe
    except Exception as exc:
        raise RuntimeError(f"无法加载协议号发送工具：{exc}") from exc

    source_message = await listen_client.get_messages(main_channel_id, ids=main_message_id)
    if source_message is None:
        raise RuntimeError("监听协议号未找到主频道原消息")
    text = getattr(source_message, "message", None) or ""
    entities = getattr(source_message, "entities", None)
    media = getattr(source_message, "media", None)
    # send_message/send_file is a true copy: unlike forward_messages it does
    # not display “转发自 …” in the backup channel.
    return await _send_message_safe(
        forward_client,
        backup_channel_id,
        text,
        entities=entities,
        file=media,
    )


async def _edit_backup_post_via_telethon(
    context: ContextTypes.DEFAULT_TYPE,
    config: dict,
    backup_channel_id: int,
    backup_message_id: int,
    source_message,
) -> None:
    client = await _get_backup_telethon_client(context, config, role="forward")
    if getattr(source_message, "text", None) is not None:
        html_text = getattr(source_message, "text_html", None)
        await client.edit_message(
            backup_channel_id,
            backup_message_id,
            html_text if html_text is not None else (source_message.text or ""),
            parse_mode="html" if html_text is not None else None,
        )
    elif getattr(source_message, "caption", None) is not None:
        html_caption = getattr(source_message, "caption_html", None)
        await client.edit_message(
            backup_channel_id,
            backup_message_id,
            html_caption if html_caption is not None else (source_message.caption or ""),
            parse_mode="html" if html_caption is not None else None,
        )
    else:
        raise RuntimeError("主频道编辑内容为空")


async def _mirror_main_post_to_backup(
    context: ContextTypes.DEFAULT_TYPE,
    config: dict,
    main_channel_id: int,
    main_message_id: int,
):
    """Copy a finalized main post to its optional backup channel.

    A backup failure must not roll back the successfully published primary post.
    Keyword/report routing remains bound to the primary channel only.
    """
    backup_channel_id = _backup_channel_id(config, main_channel_id)
    if backup_channel_id is None:
        return None
    try:
        if _backup_channel_use_telethon(config):
            copied = await _mirror_main_post_via_telethon(
                context,
                config,
                main_channel_id,
                main_message_id,
                backup_channel_id,
            )
        else:
            copied = await context.bot.copy_message(
                chat_id=backup_channel_id,
                from_chat_id=main_channel_id,
                message_id=main_message_id,
            )
        backup_message_id = _as_int(
            getattr(copied, "message_id", None) or getattr(copied, "id", None)
        )
        if backup_message_id is None:
            raise RuntimeError("备用频道同步没有返回消息 ID")
        _record_backup_post_mapping(
            main_channel_id,
            main_message_id,
            backup_channel_id,
            backup_message_id,
        )
        return copied
    except Exception as exc:
        print(
            "备用频道同步失败 "
            f"main={main_channel_id}/{main_message_id} backup={backup_channel_id}: {exc}"
        )
        return None


async def _sync_main_post_edit_to_backup(
    context: ContextTypes.DEFAULT_TYPE,
    source_message,
    config: dict,
    *,
    main_channel_id=None,
    main_message_id=None,
) -> bool:
    """Synchronize an edited primary post's text/caption to its backup copy."""
    main_channel_id = _as_int(
        main_channel_id if main_channel_id is not None else config.get("channel_id")
    )
    main_message_id = _as_int(
        main_message_id if main_message_id is not None else getattr(source_message, "message_id", None)
    )
    backup_channel_id = _backup_channel_id(config, main_channel_id)
    if main_channel_id is None or main_message_id is None or backup_channel_id is None:
        return False
    mapping = _backup_post_mapping(main_channel_id, main_message_id)
    if (
        _as_int(mapping.get("backup_channel_id")) != backup_channel_id
        or _as_int(mapping.get("backup_message_id")) is None
    ):
        # Posts cloned by the protocol forwarder before backup mirroring was
        # added already have their exact old→new IDs in history_forward_state.
        # Recover that mapping on demand so their future edits can sync too.
        historic = _collect_cloned_history_mappings(backup_channel_id, main_channel_id)
        historic_item = historic.get((main_channel_id, main_message_id), {})
        historic_backup_message_id = _as_int(historic_item.get("target_message_id"))
        if historic_backup_message_id is None:
            return False
        _record_backup_post_mapping(
            main_channel_id,
            main_message_id,
            backup_channel_id,
            historic_backup_message_id,
        )
        mapping = {
            "backup_channel_id": backup_channel_id,
            "backup_message_id": historic_backup_message_id,
        }
    backup_message_id = int(mapping["backup_message_id"])
    try:
        if _backup_channel_use_telethon(config):
            await _edit_backup_post_via_telethon(
                context,
                config,
                backup_channel_id,
                backup_message_id,
                source_message,
            )
        elif getattr(source_message, "text", None) is not None:
            await context.bot.edit_message_text(
                chat_id=backup_channel_id,
                message_id=backup_message_id,
                text=source_message.text or "",
                entities=getattr(source_message, "entities", None),
                disable_web_page_preview=True,
            )
        elif getattr(source_message, "caption", None) is not None:
            await context.bot.edit_message_caption(
                chat_id=backup_channel_id,
                message_id=backup_message_id,
                caption=source_message.caption or "",
                caption_entities=getattr(source_message, "caption_entities", None),
            )
        else:
            return False
        return True
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print(
                "备用频道主帖编辑同步失败 "
                f"main={main_channel_id}/{main_message_id} backup={backup_channel_id}/{backup_message_id}: {exc}"
            )
        return False


async def _publish_comment_to_backup_discussion(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict,
    config: dict,
    main_channel_id: int,
    main_message_id: int,
) -> bool:
    """Reply below the backup channel's matching post, never as a standalone post."""
    backup_channel_id = _backup_channel_id(config, main_channel_id)
    if backup_channel_id is None:
        return False
    backup_post = _backup_post_mapping(main_channel_id, main_message_id)
    backup_message_id = _as_int(backup_post.get("backup_message_id"))
    if _as_int(backup_post.get("backup_channel_id")) != backup_channel_id or backup_message_id is None:
        print(
            "备用频道评论跳过：未找到主帖镜像映射 "
            f"main={main_channel_id}/{main_message_id} backup={backup_channel_id}"
        )
        return False

    comment_map = _load_comment_map()
    mapping = comment_map.get(_comment_map_key(backup_channel_id, backup_message_id))
    if not isinstance(mapping, dict):
        keyword_data = _load_keyword_map()
        _resolved, _failed, changed = await _restore_cloned_discussion_mappings(
            context,
            config=config,
            target_channel_id=backup_channel_id,
            target_message_ids=[backup_message_id],
            comment_map=comment_map,
            keyword_data=keyword_data,
        )
        if changed:
            _save_comment_map(comment_map)
            _save_keyword_map(keyword_data)
        mapping = comment_map.get(_comment_map_key(backup_channel_id, backup_message_id))
    if not isinstance(mapping, dict):
        print(
            "备用频道评论跳过：未找到备用帖讨论组映射 "
            f"backup={backup_channel_id}/{backup_message_id}"
        )
        return False

    discussion_chat_id = _as_int(mapping.get("discussion_chat_id"))
    discussion_message_id = _as_int(mapping.get("discussion_message_id"))
    if discussion_chat_id is None or discussion_message_id is None:
        return False
    try:
        copied = await context.bot.copy_message(
            chat_id=discussion_chat_id,
            from_chat_id=submission["user_chat_id"],
            message_id=submission["user_message_id"],
            reply_to_message_id=discussion_message_id,
        )
        submission["backup_discussion_chat_id"] = discussion_chat_id
        submission["backup_discussion_message_id"] = copied.message_id
        return True
    except Exception as exc:
        print(
            "备用频道评论发布失败 "
            f"discussion={discussion_chat_id}/{discussion_message_id}: {exc}"
        )
        return False


def _checkin_command_text(config: dict, key: str, default: str) -> str:
    value = str((config or {}).get(key) or default).strip()
    return value[:30] or default


def _checkin_duration_seconds(config: dict) -> int:
    try:
        hours = int((config or {}).get("checkin_duration_hours", 8))
    except (TypeError, ValueError):
        hours = 8
    return max(1, min(hours, 168)) * 3600


def _load_checkin_posts() -> dict:
    data = load_json(CHECKIN_POSTS_FILE)
    if not isinstance(data, dict):
        data = {}
    posts = data.get("posts")
    if not isinstance(posts, dict):
        posts = {}
    data["posts"] = posts
    return data


def _save_checkin_posts(data: dict) -> None:
    posts = data.get("posts", {}) if isinstance(data, dict) else {}
    if isinstance(posts, dict) and len(posts) > 5000:
        oldest = sorted(
            posts.items(),
            key=lambda item: int((item[1] or {}).get("created_at", 0) or 0),
        )[: len(posts) - 5000]
        for key, _ in oldest:
            posts.pop(key, None)
    save_json(CHECKIN_POSTS_FILE, data)


def _clean_keyword_value(value: str) -> str:
    return str(value or "").strip().lstrip("#").strip()


def _checkin_post_key(channel_id: int, message_id: int) -> str:
    return f"{channel_id}:{message_id}"


def _cleanup_expired_checkin_posts(data: dict, now_ts: int = None) -> bool:
    """Expire only check-in states; published posts remain eligible forever."""
    now_ts = int(now_ts or time.time())
    changed = False
    for post in data.get("posts", {}).values():
        if not isinstance(post, dict):
            continue
        checkins = post.get("checkins")
        if not isinstance(checkins, dict):
            post["checkins"] = {}
            changed = True
            continue
        for user_id, checkin in list(checkins.items()):
            if not isinstance(checkin, dict):
                checkins.pop(user_id, None)
                changed = True
                continue
            # Compatibility with older records that only had checked_at.
            expires_at = int(checkin.get("expires_at", 0) or 0)
            if expires_at and expires_at <= now_ts:
                checkins.pop(user_id, None)
                changed = True
    return changed


def _register_checkin_post(
    config: dict,
    channel_id: int,
    message_id: int,
    entries: list[dict],
) -> None:
    """Register eligible usernames and display fields for a newly published main post."""
    if not bool(config.get("checkin_enabled", False)):
        return
    if _as_int(config.get("channel_id")) != _as_int(channel_id):
        return
    user_label = str(config.get("checkin_user_label") or "联系").strip()
    users = []
    fields: dict[str, list[str]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        raw = str(entry.get("raw") or entry.get("key") or "").strip()
        if label and raw:
            fields.setdefault(label, [])
            if raw not in fields[label]:
                fields[label].append(raw)
        if label == user_label:
            for username in re.findall(r"@([A-Za-z0-9_]{5,32})", raw):
                if username.lower() not in users:
                    users.append(username.lower())
    if not users:
        return
    data = _load_checkin_posts()
    _cleanup_expired_checkin_posts(data)
    key = _checkin_post_key(int(channel_id), int(message_id))
    old = data["posts"].get(key, {})
    existing_checkins = old.get("checkins", {}) if isinstance(old, dict) else {}
    # Keep only still-eligible users after an admin edits a post.
    checkins = {
        uid: item for uid, item in existing_checkins.items()
        if isinstance(item, dict) and str(item.get("username", "")).lower() in users
    }
    now_ts = int(time.time())
    data["posts"][key] = {
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "created_at": int(old.get("created_at", now_ts) or now_ts) if isinstance(old, dict) else now_ts,
        "eligible_usernames": users,
        "fields": fields,
        "checkins": checkins,
    }
    _save_checkin_posts(data)


def _checkin_field_values(post: dict, label: str) -> list[str]:
    values = (post.get("fields", {}) or {}).get(label, [])
    if isinstance(values, list) and values:
        return [str(item) for item in values if item]
    # Recover display fields for posts recorded by an older version before
    # fields were persisted in the check-in file.
    channel_id = _as_int(post.get("channel_id"))
    message_id = _as_int(post.get("message_id"))
    recovered = []
    for records in _load_keyword_map().values():
        for record in records if isinstance(records, list) else []:
            if (
                isinstance(record, dict)
                and _as_int(record.get("channel_id")) == channel_id
                and _as_int(record.get("channel_message_id")) == message_id
                and str(record.get("label") or "").strip() == label
            ):
                value = str(record.get("raw") or record.get("key") or "").strip()
                if value and value not in recovered:
                    recovered.append(value)
    return recovered


def _rebuild_checkin_posts_from_keyword_index(config: dict) -> int:
    """Recreate persistent check-in eligibility for posts indexed before this feature."""
    main_channel_id = _as_int(config.get("channel_id"))
    if main_channel_id is None:
        return 0
    grouped: dict[int, list[dict]] = {}
    for records in _load_keyword_map().values():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or _as_int(record.get("channel_id")) != main_channel_id:
                continue
            message_id = _as_int(record.get("channel_message_id"))
            if message_id is None:
                continue
            grouped.setdefault(message_id, []).append({
                "label": record.get("label", ""),
                "raw": record.get("raw") or record.get("key") or "",
                "key": record.get("key", ""),
            })
    before = len(_load_checkin_posts().get("posts", {}))
    for message_id, entries in grouped.items():
        _register_checkin_post(config, main_channel_id, message_id, entries)
    after = len(_load_checkin_posts().get("posts", {}))
    return max(0, after - before)


async def _hydrate_checkin_post_fields(
    context: ContextTypes.DEFAULT_TYPE,
    post: dict,
    config: dict,
) -> bool:
    """Fetch a legacy primary post through the active protocol client and re-extract fields."""
    display_label = str(config.get("checkin_display_label") or "艺名").strip()
    if _checkin_field_values(post, display_label):
        return False
    channel_id = _as_int(post.get("channel_id"))
    message_id = _as_int(post.get("message_id"))
    if channel_id is None or message_id is None:
        return False
    session_name = str(
        config.get("discussion_mapping_session")
        or _backup_channel_forward_session(config)
        or "main"
    ).strip()
    try:
        from channel.telethon_forwarder import SESSION_CLIENTS_BY_BOT, request_telethon_refresh
    except Exception:
        return False
    bot_name = str(context.application.bot_data.get("name", "") or "")
    client = SESSION_CLIENTS_BY_BOT.get(bot_name, {}).get(session_name)
    if client is None:
        request_telethon_refresh(bot_name)
        return False
    try:
        source = await client.get_messages(channel_id, ids=message_id)
        if source is None:
            return False
        entries = _extract_routing_keywords(
            SimpleNamespace(text=getattr(source, "message", "") or "", caption=None),
            config,
        )
        if not entries:
            return False
        _register_checkin_post(config, channel_id, message_id, entries)
        return True
    except Exception as exc:
        print(f"在线打卡补全帖子字段失败 channel={channel_id}/{message_id}: {exc}")
        return False


def _checkin_display_value(post: dict, config: dict) -> str:
    label = str(config.get("checkin_display_label") or "艺名").strip()
    values = _checkin_field_values(post, label)
    if values:
        return "、".join(_clean_keyword_value(item) for item in values)[:100]
    fallback_values = _checkin_field_values(
        post,
        str(config.get("checkin_user_label") or "联系").strip(),
    )
    if fallback_values:
        return "、".join(fallback_values)[:100]
    return "在线用户"


def _checkin_group_value(post: dict, config: dict) -> str:
    label = str(config.get("checkin_group_label") or "").strip()
    if not label:
        return ""
    values = _checkin_field_values(post, label)
    if values:
        return "、".join(_clean_keyword_value(item) for item in values)[:100]
    return "未分类"


async def _handle_checkin_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or not msg.text or not update.effective_user:
        return False
    chat = update.effective_chat
    if not chat or chat.type not in {"group", "supergroup"}:
        return False
    command = msg.text.strip()
    config = load_publish_config()
    checkin_command = _checkin_command_text(config, "checkin_command_text", "打卡")
    cancel_command = _checkin_command_text(config, "checkin_cancel_command_text", "取消打卡")
    online_command = _checkin_command_text(config, "checkin_online_command_text", "在线宝宝")
    online_text = _checkin_command_text(config, "checkin_online_text", "在线宝宝")
     
    if command not in {checkin_command, cancel_command, online_command}:
        return False
    if not bool(config.get("checkin_enabled", False)):
        return False

    data = _load_checkin_posts()
    if _cleanup_expired_checkin_posts(data):
        _save_checkin_posts(data)
    posts = data.get("posts", {})
    if not posts:
        _rebuild_checkin_posts_from_keyword_index(config)
        data = _load_checkin_posts()
        posts = data.get("posts", {})
    username = str(update.effective_user.username or "").strip().lstrip("@").lower()

    if command in {checkin_command, cancel_command}:
        if not username:
            # await msg.reply_text("❗ 打卡需要先设置 Telegram 用户名（@username）。")
            return True
        matched = [
            post for post in posts.values()
            if isinstance(post, dict) and username in (post.get("eligible_usernames") or [])
        ]
        if not matched:
            # await msg.reply_text("❗ 当前没有包含你用户名的有效帖子，无法打卡。")
            return True
        changed = 0
        for post in matched:
            checkins = post.setdefault("checkins", {})
            if command == checkin_command:
                checkins[str(update.effective_user.id)] = {
                    "username": username,
                    "name": update.effective_user.full_name,
                    "checked_at": int(time.time()),
                    "expires_at": int(time.time()) + _checkin_duration_seconds(config),
                }
                changed += 1
            elif str(update.effective_user.id) in checkins:
                checkins.pop(str(update.effective_user.id), None)
                changed += 1
        _save_checkin_posts(data)
        await msg.reply_text(
            f"✅ 已{'打卡' if command == checkin_command else '取消打卡'}，"
            f"{'打卡成功，牛马上线' if command == checkin_command else '下线成功，祝早日财富自由'}"
        )
        return True

    online_posts = [
        post for post in posts.values()
        if isinstance(post, dict) and isinstance(post.get("checkins"), dict) and post.get("checkins")
    ]
    hydrated = False
    for post in online_posts:
        hydrated = await _hydrate_checkin_post_fields(context, post, config) or hydrated
    if hydrated:
        data = _load_checkin_posts()
        posts = data.get("posts", {})
        online_posts = [
            post for post in posts.values()
            if isinstance(post, dict) and isinstance(post.get("checkins"), dict) and post.get("checkins")
        ]
    if not online_posts:
        await msg.reply_text("当前暂无在线宝宝。")
        return True

    grouped: dict[str, list[dict]] = {}
    for post in online_posts:
        group = _checkin_group_value(post, config)
        grouped.setdefault(group, []).append(post)

    lines = [f"🟢 {online_text}"]
    link_items = []

    for group, group_posts in grouped.items():
        if group:
            lines.extend(["", f"【{group}】"])

        names = []

        for post in group_posts:
            link = await _route_message_link(context, post)
            name_text = _checkin_display_value(post, config)

            names.append(name_text)

            if link:
                link_items.append((name_text, link))

        if names:
            lines.append("  ".join(names))

    result_text = "\n".join(lines)

    # 根据最终文本重新寻找每个名字的位置
    text_link_entities = []

    search_start = 0
    for name_text, link in link_items:
        offset = result_text.find(name_text, search_start)

        if offset >= 0:
            text_link_entities.append(
                MessageEntity(
                    type=MessageEntity.TEXT_LINK,
                    offset=offset,
                    length=len(name_text),
                    url=link,
                )
            )
            search_start = offset + len(name_text)

    text_link_entities = MessageEntity.adjust_message_entities_to_utf_16(
        result_text,
        text_link_entities,
    )

    await msg.reply_text(
        result_text,
        entities=text_link_entities or None,
        disable_web_page_preview=True,
    )
    return True


async def _checkin_group_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_checkin_group_message(update, context):
        raise ApplicationHandlerStop


def _checkin_settings_text(config: dict) -> str:
    group_label = str(config.get("checkin_group_label") or "").strip() or "不分组"
    return "\n".join([
        "🟢 在线打卡设置",
        "",
        f"状态：{'✅ 开启' if config.get('checkin_enabled', False) else '🚫 关闭'}",
        f"有效时间：{_checkin_duration_seconds(config) // 3600} 小时",
        f"打卡用户名字段：{config.get('checkin_user_label') or '联系'}",
        f"展示文字字段：{config.get('checkin_display_label') or '艺名'}",
        f"展示分组字段：{group_label}",
        f"打卡文案：{_checkin_command_text(config, 'checkin_command_text', '打卡')}",
        f"取消文案：{_checkin_command_text(config, 'checkin_cancel_command_text', '取消打卡')}",
        f"在线展示文案：{_checkin_command_text(config, 'checkin_online_command_text', '在线宝宝')}",
        f"展示文案：{_checkin_command_text(config, 'checkin_online_text', '在线宝宝')}",
        "",
        "管理员发布带 @用户名 的主频道帖子后，对应用户可按上述文案打卡或取消。",
    ])


def _checkin_settings_keyboard(config: dict) -> InlineKeyboardMarkup:
    enabled = bool(config.get("checkin_enabled", False))
    group_label = str(config.get("checkin_group_label") or "").strip() or "不分组"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅' if enabled else '🚫'} 在线打卡",
            callback_data="publish:toggle_checkin",
        )],
        [InlineKeyboardButton(
            f"⏱ 有效时间：{_checkin_duration_seconds(config) // 3600} 小时",
            callback_data="publish:checkin_duration",
        )],
        [
            InlineKeyboardButton(
                f"👤 用户名字段：{config.get('checkin_user_label') or '联系'}",
                callback_data="publish:checkin_user_label",
            ),
            InlineKeyboardButton(
                f"📝 展示字段：{config.get('checkin_display_label') or '艺名'}",
                callback_data="publish:checkin_display_label",
            ),
        ],
        [InlineKeyboardButton(
            f"🗂 分组字段：{group_label}",
            callback_data="publish:checkin_group_label",
        )],
        [
            InlineKeyboardButton(
                f"✅ 打卡文案：{_checkin_command_text(config, 'checkin_command_text', '打卡')}",
                callback_data="publish:checkin_command_text",
            ),
            InlineKeyboardButton(
                f"🚫 取消文案：{_checkin_command_text(config, 'checkin_cancel_command_text', '取消打卡')}",
                callback_data="publish:checkin_cancel_command_text",
            ),
        ],
        [   InlineKeyboardButton(
                f"🟢 在线文案：{_checkin_command_text(config, 'checkin_online_command_text', '在线宝宝')}",
                callback_data="publish:checkin_online_command_text",
            ),
            InlineKeyboardButton(
            f"输出正文文案：{_checkin_command_text(config, 'checkin_online_text', '在线宝宝')}",
            callback_data="publish:checkin_online_text",
            )
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")],
    ])


def _record_publish_channel_change(config: dict, old_channel_id, new_channel_id) -> None:
    """Remember a main-channel switch for later automatic history rebinding."""
    old_channel_id = _as_int(old_channel_id)
    new_channel_id = _as_int(new_channel_id)
    if old_channel_id is None or new_channel_id is None or old_channel_id == new_channel_id:
        return
    history = config.get("channel_change_history")
    if not isinstance(history, list):
        history = []
    record = {
        "old_channel_id": old_channel_id,
        "new_channel_id": new_channel_id,
        "changed_at": int(time.time()),
    }
    history = [
        item for item in history
        if not (
            isinstance(item, dict)
            and _as_int(item.get("old_channel_id")) == old_channel_id
            and _as_int(item.get("new_channel_id")) == new_channel_id
        )
    ]
    history.append(record)
    config["channel_change_history"] = history[-20:]
    config["pending_history_post_migration"] = record


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
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        aliases = {}
    data["reports"] = reports
    data["aliases"] = aliases
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


def _report_id_from_subject_entries(subject_entries: Optional[list[dict]]) -> str:
    """Use the first Telegram @username in a post as its stable report ID."""
    for entry in subject_entries or []:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("raw") or entry.get("key") or "")
        match = re.search(r"@([A-Za-z0-9_]{5,32})", raw)
        if match:
            return match.group(1).lower()
    return uuid.uuid4().hex[:16]


def _report_subject_post_refs(report: Optional[dict]) -> list[tuple[int, int]]:
    """Return source posts whose indexed fields belong to this report."""
    if not isinstance(report, dict):
        return []
    refs = []

    def add_ref(channel_id, message_id) -> None:
        channel_id = _as_int(channel_id)
        message_id = _as_int(message_id)
        if channel_id is not None and message_id is not None and (channel_id, message_id) not in refs:
            refs.append((channel_id, message_id))

    # Current and legacy single-previous fields support reports created before
    # source-reference history was introduced.
    add_ref(report.get("channel_id"), report.get("message_id"))
    add_ref(report.get("previous_channel_id"), report.get("previous_message_id"))
    for ref in report.get("subject_post_refs", []) or []:
        if isinstance(ref, dict):
            add_ref(ref.get("channel_id"), ref.get("message_id"))
    return refs


def _create_comment_report(
    channel_id: int, message_id: int, report_id: str, subject_entries: Optional[list[dict]] = None
) -> None:
    data = _load_comment_reports()
    existing = data["reports"].get(report_id)

    # A username-based report is reused when its post is republished. Preserve
    # every previously indexed field (including 标签 / 艺名) and merge the fields
    # extracted from the new post instead of replacing the old subject list.
    subjects = []
    for item in _report_subjects(existing) if isinstance(existing, dict) else []:
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if label and value and not any(
            saved.get("label") == label and saved.get("value") == value
            for saved in subjects
        ):
            subjects.append({"label": label, "value": value})
    for entry in subject_entries or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        value = str(entry.get("raw") or entry.get("key") or "").strip()
        if label and value and not any(
            saved.get("label") == label and saved.get("value") == value
            for saved in subjects
        ):
            subjects.append({"label": label, "value": value})

    # Retain all historical post references so reports created before this fix
    # can still recover indexed fields after one or more republishes.
    post_refs = _report_subject_post_refs(existing)
    current_ref = (_as_int(channel_id), _as_int(message_id))
    if current_ref[0] is not None and current_ref[1] is not None and current_ref not in post_refs:
        post_refs.append(current_ref)

    # A report ID based on the post username is intentionally stable. When the
    # same username is published again, keep its report/comments but point the
    # report link at the newest primary message.
    comments = existing.get("comments", []) if isinstance(existing, dict) else []
    if not isinstance(comments, list):
        comments = []
    report = dict(existing) if isinstance(existing, dict) else {}
    report.update({
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "created_at": int(report.get("created_at", int(time.time())) or int(time.time())),
        "updated_at": int(time.time()),
        "subjects": subjects,
        "subject_post_refs": [
            {"channel_id": source_channel_id, "message_id": source_message_id}
            for source_channel_id, source_message_id in post_refs[-20:]
        ],
        "comments": comments,
    })
    if isinstance(existing, dict):
        report["previous_channel_id"] = _as_int(existing.get("channel_id"))
        report["previous_message_id"] = _as_int(existing.get("message_id"))
    data["reports"][report_id] = report
    _save_comment_reports(data)


def _normalize_report_username(value: str) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    return username if re.fullmatch(r"[a-z0-9_]{5,32}", username) else ""


def _resolve_report_id(data: dict, report_id: str) -> str:
    current = str(report_id or "").strip()
    aliases = data.get("aliases", {}) if isinstance(data, dict) else {}
    seen = set()
    while current and current not in seen and isinstance(aliases, dict) and current in aliases:
        seen.add(current)
        current = str(aliases[current] or "").strip()
    return current


def _get_comment_report(report_id: str):
    data = _load_comment_reports()
    resolved_id = _resolve_report_id(data, report_id)
    report = data.get("reports", {}).get(resolved_id)
    return report if isinstance(report, dict) else None


def _find_report_id_by_username(username: str) -> str:
    """Find the current report for @username, including older subject-based records."""
    username = _normalize_report_username(username)
    if not username:
        return ""
    data = _load_comment_reports()
    report_id = _resolve_report_id(data, username)
    if isinstance(data.get("reports", {}).get(report_id), dict):
        return report_id
    needle = f"@{username}".lower()
    matches = []
    for candidate_id, report in data.get("reports", {}).items():
        if not isinstance(report, dict):
            continue
        for subject in report.get("subjects", []) or []:
            if isinstance(subject, dict) and str(subject.get("value") or "").lower() == needle:
                matches.append((candidate_id, report))
                break
    if not matches:
        return ""
    matches.sort(key=lambda item: int((item[1] or {}).get("updated_at", 0) or (item[1] or {}).get("created_at", 0) or 0), reverse=True)
    return str(matches[0][0])


def _merge_report_username(old_username: str, new_username: str) -> tuple[bool, str]:
    """Move old username report history under the new username report ID."""
    old_username = _normalize_report_username(old_username)
    new_username = _normalize_report_username(new_username)
    if not old_username or not new_username:
        return False, "用户名格式无效。"
    if old_username == new_username:
        return False, "新旧用户名相同，无需迁移。"

    data = _load_comment_reports()
    old_id = _find_report_id_by_username(old_username)
    if not old_id:
        return False, f"未找到 @{old_username} 的历史报告。"
    old_id = _resolve_report_id(data, old_id)
    old_report = data.get("reports", {}).get(old_id)
    if not isinstance(old_report, dict):
        return False, f"未找到 @{old_username} 的历史报告。"

    new_id = _resolve_report_id(data, new_username)
    new_report = data.get("reports", {}).get(new_id)
    if not isinstance(new_report, dict):
        new_report = dict(old_report)
        new_report["comments"] = []
        new_report["migrated_from_report_ids"] = []
        data["reports"][new_username] = new_report
        new_id = new_username

    merged_comments = list(new_report.get("comments", []) or [])
    seen_comments = {
        (
            str(item.get("forward_channel_id")),
            str(item.get("forward_message_id")),
            str(item.get("created_at")),
            str(item.get("content")),
        )
        for item in merged_comments if isinstance(item, dict)
    }
    for comment in old_report.get("comments", []) or []:
        if not isinstance(comment, dict):
            continue
        key = (
            str(comment.get("forward_channel_id")),
            str(comment.get("forward_message_id")),
            str(comment.get("created_at")),
            str(comment.get("content")),
        )
        if key not in seen_comments:
            merged_comments.append(comment)
            seen_comments.add(key)
    new_report["comments"] = merged_comments[-1000:]
    history = new_report.setdefault("migrated_from_report_ids", [])
    if old_id not in history:
        history.append(old_id)
    new_report["updated_at"] = int(time.time())
    new_report["username_migrated_from"] = old_username
    new_report["username"] = new_username
    data["reports"][new_id] = new_report
    if old_id != new_id:
        data["reports"].pop(old_id, None)
    data["aliases"][old_username] = new_id
    data["aliases"][old_id] = new_id
    _save_comment_reports(data)
    return True, f"✅ 已将 @{old_username} 的报告迁移到 @{new_username}。"


async def _handle_username_report_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Open the latest report when a user sends an exact @username in private."""
    msg = update.message
    chat = update.effective_chat
    if not msg or not msg.text or not chat or chat.type != "private":
        return False
    username = _normalize_report_username(msg.text)
    if not username or not msg.text.strip().startswith("@"):
        return False
    report_id = _find_report_id_by_username(username)
    if not report_id:
        return False
    report = _get_comment_report(report_id)
    if not report:
        return False
    text, markup = _report_list_view(report_id, report, 1)
    await msg.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
    return True


async def _handle_report_username_migration_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    stage = (context.user_data or {}).get(REPORT_USERNAME_MIGRATION_KEY)
    if not isinstance(stage, dict):
        return False
    msg = update.message
    if not msg or not msg.text:
        return True
    if not update.effective_user or not has_admin_permission(
        context, update.effective_user.id, "submission_config"
    ):
        context.user_data.pop(REPORT_USERNAME_MIGRATION_KEY, None)
        return False
    text = msg.text.strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(REPORT_USERNAME_MIGRATION_KEY, None)
        await msg.reply_text("已取消用户名报告迁移。")
        return True
    username = _normalize_report_username(text)
    if not username:
        await msg.reply_text("❗ 请输入有效 Telegram 用户名，例如：@newname。")
        return True
    if stage.get("step") == "new":
        stage["new_username"] = username
        stage["step"] = "old"
        await msg.reply_text(
            f"新用户名：@{username}\n\n"
            "请继续输入旧用户名，例如：@oldname。\n"
            "旧用户名的报告评论会合并到新用户名报告下。"
        )
        return True
    if stage.get("step") == "old":
        new_username = str(stage.get("new_username") or "")
        ok, result = _merge_report_username(username, new_username)
        context.user_data.pop(REPORT_USERNAME_MIGRATION_KEY, None)
        await msg.reply_text(result)
        return True
    context.user_data.pop(REPORT_USERNAME_MIGRATION_KEY, None)
    return False


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


def _comment_nickname(msg) -> str:
    """Return the sender's visible Telegram name without username or ID."""
    user = getattr(msg, "from_user", None)
    if not user:
        return "匿名用户"
    return str(
        getattr(user, "full_name", "") or getattr(user, "first_name", "") or "匿名用户"
    ).strip()


def _comment_author(msg) -> str:
    name = _comment_nickname(msg)
    user = getattr(msg, "from_user", None)
    username = str(getattr(user, "username", "") or "").strip() if user else ""
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


def _load_post_migration_state() -> dict:
    data = load_json(POST_MIGRATION_STATE_FILE)
    if not isinstance(data, dict):
        data = {}
    migrations = data.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
    data["migrations"] = migrations
    return data


def _save_post_migration_state(data: dict) -> None:
    save_json(POST_MIGRATION_STATE_FILE, data if isinstance(data, dict) else {"migrations": {}})


def _post_migration_key(source_channel_id: int, target_channel_id: int) -> str:
    return f"{source_channel_id}:{target_channel_id}"


def _collect_cloned_history_mappings(
    target_channel_id: int,
    source_channel_id: Optional[int] = None,
) -> dict[tuple[int, int], dict]:
    """Read exact old→new IDs written by the channel-clone forwarder.

    The migration requires both the user-supplied old channel and current new
    channel to match the stored mapping. This avoids rebinding a report to a
    cloned post from an unrelated source channel.
    """
    state = load_json(HISTORY_FORWARD_STATE_FILE)
    message_maps = state.get("message_maps", {}) if isinstance(state, dict) else {}
    if not isinstance(message_maps, dict):
        return {}

    mappings: dict[tuple[int, int], dict] = {}
    for raw_key, record in message_maps.items():
        try:
            session_name, source_text, target_text, _rule_hash = str(raw_key).rsplit(":", 3)
        except ValueError:
            continue
        mapped_source_channel_id = _as_int(source_text)
        mapped_target_channel_id = _as_int(target_text)
        if (
            mapped_source_channel_id is None
            or mapped_target_channel_id != target_channel_id
            or (
                source_channel_id is not None
                and mapped_source_channel_id != source_channel_id
            )
        ):
            continue
        messages = record.get("messages", {}) if isinstance(record, dict) else {}
        if not isinstance(messages, dict):
            continue
        for source_message_text, target_message_ids in messages.items():
            source_message_id = _as_int(source_message_text)
            if source_message_id is None or source_message_id <= 0:
                continue
            values = target_message_ids if isinstance(target_message_ids, list) else []
            target_message_id = next(
                (value for value in (_as_int(item) for item in values) if value and value > 0),
                None,
            )
            if target_message_id is None:
                continue
            mappings.setdefault(
                (mapped_source_channel_id, source_message_id),
                {
                    "target_channel_id": target_channel_id,
                    "target_message_id": target_message_id,
                    "session_name": session_name,
                },
            )
    return mappings


def _count_migratable_history_posts(
    target_channel_id: int,
    source_channel_id: Optional[int] = None,
) -> int:
    mappings = _collect_cloned_history_mappings(target_channel_id, source_channel_id)
    state = _load_post_migration_state()
    total = 0
    for (mapped_source_channel_id, source_message_id), mapping in mappings.items():
        migration = state["migrations"].get(
            _post_migration_key(mapped_source_channel_id, target_channel_id),
            {},
        )
        posts = migration.get("posts", {}) if isinstance(migration, dict) else {}
        if _as_int(posts.get(str(source_message_id))) != mapping["target_message_id"]:
            total += 1
    return total


def _relocate_post_indexes(
    reports_data: dict,
    keyword_data: dict,
    user_posts: list,
    *,
    source_channel_id: int,
    source_message_id: int,
    target_channel_id: int,
    target_message_id: int,
) -> list[str]:
    """Point report/keyword/random-view records at an existing cloned post."""
    report_ids = []
    for report_id, report in reports_data.get("reports", {}).items():
        if not isinstance(report, dict):
            continue
        if (
            _as_int(report.get("channel_id")) == source_channel_id
            and _as_int(report.get("message_id")) == source_message_id
        ):
            report["migrated_from_channel_id"] = source_channel_id
            report["migrated_from_message_id"] = source_message_id
            report["channel_id"] = target_channel_id
            report["message_id"] = target_message_id
            report["migrated_at"] = int(time.time())
            report_ids.append(str(report_id))

    for records in keyword_data.values():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            if (
                _as_int(record.get("channel_id")) == source_channel_id
                and _as_int(record.get("channel_message_id")) == source_message_id
            ):
                record["channel_id"] = target_channel_id
                record["channel_message_id"] = target_message_id
                record["migrated_at"] = int(time.time())

    for item in user_posts:
        if not isinstance(item, dict):
            continue
        if (
            _as_int(item.get("channel_id")) == source_channel_id
            and _as_int(item.get("channel_message_id")) == source_message_id
        ):
            item["channel_id"] = target_channel_id
            item["channel_message_id"] = target_message_id
            item["migrated_at"] = int(time.time())

    return report_ids


def _apply_discussion_mapping_to_keyword_data(
    keyword_data: dict,
    channel_id: int,
    message_id: int,
    discussion_chat_id: int,
    discussion_message_id: int,
) -> bool:
    changed = False
    for records in keyword_data.values():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            if (
                _as_int(record.get("channel_id")) == channel_id
                and _as_int(record.get("channel_message_id")) == message_id
            ):
                record["discussion_chat_id"] = discussion_chat_id
                record["discussion_message_id"] = discussion_message_id
                changed = True
    return changed


async def _restore_cloned_discussion_mappings(
    context: ContextTypes.DEFAULT_TYPE,
    config: dict,
    target_channel_id: int,
    target_message_ids: list[int],
    comment_map: dict,
    keyword_data: dict,
) -> tuple[int, int, bool]:
    """Resolve linked-discussion messages for already-cloned channel posts.

    Cloned messages may have reached the discussion group while the old channel
    was still configured, so the normal update handler intentionally ignored
    them. Resolve them through the configured protocol account now, then write
    the normal comment-map and keyword-map records expected by comment review.
    """
    unresolved = [
        message_id for message_id in target_message_ids
        if not isinstance(comment_map.get(_comment_map_key(target_channel_id, message_id)), dict)
    ]
    if not unresolved:
        return 0, 0, False

    try:
        from telethon import utils as telethon_utils
        from channel.telethon_forwarder import SESSION_CLIENTS_BY_BOT, request_telethon_refresh
    except Exception as exc:
        print(f"[历史主帖关联] Telethon 讨论组映射组件不可用: {exc}")
        return 0, len(unresolved), False

    # Prefer an explicitly configured discussion resolver. For a bot that
    # mirrors to a backup through a dedicated protocol account, that account is
    # the safest fallback; only then use the legacy shared main session.
    session_name = str(
        config.get("discussion_mapping_session")
        or config.get("backup_channel_session")
        or "main"
    ).strip()
    bot_name = str(context.application.bot_data.get("name", "") or "")
    client = SESSION_CLIENTS_BY_BOT.get(bot_name, {}).get(session_name)
    if client is None:
        # Never open a second TelegramClient against the same SQLite .session
        # file here: Telethon will try to write session state and can produce
        # "database is locked" plus orphaned send/receive tasks. Ask the shared
        # forwarder loop to start the configured session instead.
        request_telethon_refresh(bot_name)
        print(
            "[历史主帖关联] 讨论组映射协议号尚未就绪，已请求协议号转发器刷新 "
            f"bot={bot_name} session={session_name}"
        )
        return 0, len(unresolved), False

    resolved = failed = 0
    changed = False
    for message_id in unresolved:
        try:
            # Telethon's helper invokes GetDiscussionMessageRequest and returns
            # the discussion peer plus the automatic-forward message ID.
            discussion_peer, discussion_message_id = await client._get_comment_data(
                target_channel_id,
                message_id,
            )
            discussion_chat_id = int(telethon_utils.get_peer_id(discussion_peer))
            comment_map[_comment_map_key(target_channel_id, message_id)] = {
                "discussion_chat_id": discussion_chat_id,
                "discussion_message_id": int(discussion_message_id),
                "created_at": int(time.time()),
                "migrated_at": int(time.time()),
            }
            _apply_discussion_mapping_to_keyword_data(
                keyword_data,
                target_channel_id,
                message_id,
                discussion_chat_id,
                int(discussion_message_id),
            )
            resolved += 1
            changed = True
        except Exception as exc:
            failed += 1
            print(
                "[历史主帖关联] 获取新频道讨论组映射失败 "
                f"channel={target_channel_id} message={message_id}: {exc}"
            )
    return resolved, failed, changed


async def _migrate_historical_posts(
    context: ContextTypes.DEFAULT_TYPE,
    target_channel_id: int,
    source_channel_id: int,
) -> tuple[int, int, int, int, int]:
    """Rebind only mappings whose old and new channel IDs both match.

    Also restores the target channel's discussion-message mapping so comments
    on migrated posts continue to publish below the new cloned main post.
    """
    mappings = _collect_cloned_history_mappings(target_channel_id, source_channel_id)
    state = _load_post_migration_state()
    reports_data = _load_comment_reports()
    keyword_data = _load_keyword_map()
    user_posts = _load_cannel_message()
    if not isinstance(user_posts, list):
        user_posts = []

    rebound = already_done = reports_relinked = 0
    target_message_ids = []
    data_changed = False
    for (mapped_source_channel_id, source_message_id), mapping in mappings.items():
        target_message_id = mapping["target_message_id"]
        migration_key = _post_migration_key(mapped_source_channel_id, target_channel_id)
        migration = state["migrations"].setdefault(
            migration_key,
            {"posts": {}, "created_at": int(time.time())},
        )
        posts = migration.setdefault("posts", {})
        if not isinstance(posts, dict):
            posts = migration["posts"] = {}
        if _as_int(posts.get(str(source_message_id))) == target_message_id:
            already_done += 1
        else:
            posts[str(source_message_id)] = target_message_id
            migration["mode"] = "history_forward_state"
            migration["session_name"] = mapping.get("session_name", "")
            migration["updated_at"] = int(time.time())
            _save_post_migration_state(state)
            rebound += 1

        if target_message_id not in target_message_ids:
            target_message_ids.append(target_message_id)
        report_ids = _relocate_post_indexes(
            reports_data,
            keyword_data,
            user_posts,
            source_channel_id=mapped_source_channel_id,
            source_message_id=source_message_id,
            target_channel_id=target_channel_id,
            target_message_id=target_message_id,
        )
        reports_relinked += len(report_ids)
        data_changed = True

    comment_map = _load_comment_map()
    discussion_resolved, discussion_failed, discussion_changed = await _restore_cloned_discussion_mappings(
        context,
        config=load_publish_config(),
        target_channel_id=target_channel_id,
        target_message_ids=target_message_ids,
        comment_map=comment_map,
        keyword_data=keyword_data,
    )
    if discussion_changed:
        _save_comment_map(comment_map)
        data_changed = True
    if data_changed:
        _save_comment_reports(reports_data)
        _save_keyword_map(keyword_data)
        save_json(USER_MESSAGE_FILE, user_posts)
    return rebound, already_done, reports_relinked, discussion_resolved, discussion_failed


async def _run_historical_post_migration_task(
    context: ContextTypes.DEFAULT_TYPE,
    source_channel_id: int,
    target_channel_id: int,
    notify_chat_id: int,
) -> None:
    try:
        rebound, already_done, reports_relinked, discussion_resolved, discussion_failed = await _migrate_historical_posts(
            context,
            target_channel_id,
            source_channel_id,
        )
        await context.bot.send_message(
            chat_id=notify_chat_id,
            text=(
                "✅ 已关联已克隆历史主帖\n"
                f"旧频道：{source_channel_id}\n"
                f"新频道：{target_channel_id}\n"
                f"新关联帖子：{rebound} 条\n"
                f"已关联跳过：{already_done} 条\n"
                f"已重新绑定报告：{reports_relinked} 个\n"
                f"已恢复讨论组映射：{discussion_resolved} 条\n"
                f"讨论组映射失败：{discussion_failed} 条\n\n"
                "已同时校验 history_forward_state 中的旧频道和新频道 ID；"
                "没有访问或复制已经失效的旧频道。"
            ),
        )
    finally:
        context.application.bot_data["history_post_migration_in_progress"] = False


def _migration_source_counts(target_channel_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    for source_channel_id, _source_message_id in _collect_cloned_history_mappings(target_channel_id):
        counts[source_channel_id] = counts.get(source_channel_id, 0) + 1
    return counts


def _migration_source_keyboard(source_counts: dict[int, int]) -> InlineKeyboardMarkup:
    rows = []
    for source_channel_id, count in sorted(source_counts.items()):
        rows.append([
            InlineKeyboardButton(
                f"旧频道 {source_channel_id}（{count} 条映射）",
                callback_data=f"publish:migrate_history_source:{source_channel_id}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


async def _start_historical_post_migration(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    source_channel_id: int,
    target_channel_id: int,
) -> None:
    total = _count_migratable_history_posts(target_channel_id, source_channel_id)
    if total <= 0:
        await query.answer("这组旧/新频道没有待关联的消息映射。", show_alert=True)
        return
    context.application.bot_data["history_post_migration_in_progress"] = True
    notify_chat_id = query.message.chat_id if query.message else query.from_user.id
    context.application.create_task(
        _run_historical_post_migration_task(
            context,
            source_channel_id,
            target_channel_id,
            notify_chat_id,
        ),
        name=f"history-post-rebind:{source_channel_id}:{target_channel_id}",
    )
    await query.answer("已开始自动关联。", show_alert=False)
    await query.edit_message_text(
        f"🔄 已确认频道变更并开始关联 {total} 条历史主帖。\n"
        f"旧频道：{source_channel_id}\n新频道：{target_channel_id}\n"
        "消息映射来自 history_forward_state，无需访问旧频道。"
    )


def _report_subjects(report: dict) -> list[dict]:
    """Return every indexed field belonging to a report's source post.

    Stored report subjects are retained for reliability, while the keyword index
    supplements them with fields such as 标签、艺名 and 区域. This also improves
    reports that were created before all extracted fields were saved to them.
    """
    subjects = report.get("subjects", []) if isinstance(report.get("subjects"), list) else []
    result = []
    for item in subjects:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if label and value and not any(
            current.get("label") == label and current.get("value") == value
            for current in result
        ):
            result.append({"label": label, "value": value})

    # The keyword map is the complete index for a post. Always merge it rather
    # than using it only as a fallback, because earlier reports often stored
    # only the contact field in ``subjects``.
    source_posts = set(_report_subject_post_refs(report))
    for records in _load_keyword_map().values():
        for record in records or []:
            if (
                isinstance(record, dict)
                and (
                    _as_int(record.get("channel_id")),
                    _as_int(record.get("channel_message_id")),
                ) in source_posts
            ):
                label = str(record.get("label") or "").strip()
                value = str(record.get("raw") or record.get("key") or "").strip()
                if label and value and not any(
                    item.get("label") == label and item.get("value") == value
                    for item in result
                ):
                    result.append({"label": label, "value": value})
    return result


def _report_subject_text(report: dict) -> str:
    subjects = _report_subjects(report)

    if not subjects:
        return "收录对象：未识别"

    grouped = {}

    for item in subjects:
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()

        if not label or not value:
            continue

        grouped.setdefault(label, []).append(value)

    if not grouped:
        return "收录对象：未识别"

    lines = []

    for label, values in grouped.items():
        lines.append(f"{label}：" + "、".join(values))

    return "\n".join(lines)


def _comment_date(comment: dict) -> str:
    try:
        return datetime.fromtimestamp(int(comment.get("created_at", 0) or 0)).strftime("%Y-%m-%d %H:%M")
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
        # Accept common separator input such as “标签、艺名” or “标签.艺名”
        # in addition to commas/newlines, so every field is indexed separately.
        for value in re.split(r"[,，、\n.。]+", str(label or "")):
            value = value.strip()
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
        # Support both “标签：#示例” and “【标签】：#示例” field styles.
        field_prefix = (
            rf"(?:[【\[（(]\s*)?{re.escape(label)}\s*(?:[】\]）)])?\s*[：:]?"
        )
        if label == "标签":
            # 找到“标签”字段
            pattern = re.compile(
                rf"{field_prefix}(.*)",
                re.IGNORECASE | re.DOTALL,
            )

            match = pattern.search(text)
            if not match:
                continue

            # Only read the 标签 field's own line. Do not accidentally add
            # values such as “艺名：#小雅” on later lines as tags.
            content = match.group(1).splitlines()[0]

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
                rf"{field_prefix}\s*([^\s]+)",
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


def _find_keyword_routes(query: str, *, require_discussion: bool = True) -> list[dict]:
    """Find indexed channel posts for a keyword.

    Comment submission needs a linked discussion message, while ordinary post
    lookup only needs the original channel message.  Keep the stricter behavior
    as the default for the existing comment workflow.
    """
    key = _normalize_routing_keyword(query)
    if not key:
        return []
    data = _load_keyword_map()
    matches = []
    for stored_key, records in data.items():
        if key not in stored_key and stored_key not in key:
            continue
        for record in records or []:
            if not isinstance(record, dict):
                continue
            if require_discussion and not record.get("discussion_message_id"):
                continue
            if record.get("channel_id") and record.get("channel_message_id"):
                matches.append({**record, "key": stored_key})
    matches.sort(key=lambda item: int(item.get("created_at", 0) or 0), reverse=True)
    return matches[:30]

def _find_keyword_routes_with_artist(
    query: str,
    *,
    require_discussion: bool = True,
) -> list[dict]:
    """Find keyword routes and attach the artist name of the same post."""
    routes = _find_keyword_routes(
        query,
        require_discussion=require_discussion,
    )

    if not routes:
        return []

    data = _load_keyword_map()

    # (channel_id, channel_message_id) -> 艺名
    artist_map = {}

    for records in data.values():
        for record in records or []:
            if not isinstance(record, dict):
                continue

            if record.get("label") != "艺名":
                continue

            channel_id = record.get("channel_id")
            message_id = record.get("channel_message_id")

            if not channel_id or not message_id:
                continue

            artist_name = str(record.get("raw", "")).lstrip("#").strip()
            if artist_name:
                artist_map[(int(channel_id), int(message_id))] = artist_name

    result = []

    for route in routes:
        route = dict(route)

        if route.get("label") == "标签":
            channel_id = route.get("channel_id")
            message_id = route.get("channel_message_id")

            if channel_id and message_id:
                artist_name = artist_map.get(
                    (int(channel_id), int(message_id))
                )
                if artist_name:
                    route["艺名"] = artist_name

        result.append(route)

    return result

KEYWORD_SEARCH_DISPLAY_OPTIONS = {
    "main": "主频道帖子",
    "backup": "备用频道帖子",
    "all": "主频道 + 备用频道",
}


def _keyword_post_search_display_mode(config: dict) -> str:
    mode = str((config or {}).get("keyword_post_search_display_mode") or "main").lower()
    return mode if mode in KEYWORD_SEARCH_DISPLAY_OPTIONS else "main"


def _keyword_post_search_display_keyboard(config: dict) -> InlineKeyboardMarkup:
    current = _keyword_post_search_display_mode(config)
    rows = []
    for mode, label in KEYWORD_SEARCH_DISPLAY_OPTIONS.items():
        rows.append([InlineKeyboardButton(
            f"{'✅' if mode == current else '⚪'} {label}",
            callback_data=f"publish:keyword_search_display_set:{mode}",
        )])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


def _backup_keyword_route(route: dict, config: dict):
    source_channel_id = _as_int(route.get("channel_id"))
    source_message_id = _as_int(route.get("channel_message_id"))
    backup_channel_id = _backup_channel_id(config, source_channel_id)
    if source_channel_id is None or source_message_id is None or backup_channel_id is None:
        return None
    mapping = _backup_post_mapping(source_channel_id, source_message_id)
    backup_message_id = _as_int(mapping.get("backup_message_id"))
    if _as_int(mapping.get("backup_channel_id")) != backup_channel_id or backup_message_id is None:
        historic = _collect_cloned_history_mappings(backup_channel_id, source_channel_id)
        historic_item = historic.get((source_channel_id, source_message_id), {})
        backup_message_id = _as_int(historic_item.get("target_message_id"))
    if backup_message_id is None:
        return None
    return {
        **route,
        "channel_id": backup_channel_id,
        "channel_message_id": backup_message_id,
        "display_channel": "备用频道",
        "source_channel_id": source_channel_id,
        "source_message_id": source_message_id,
    }


def _keyword_post_display_routes(routes: list[dict], config: dict) -> list[dict]:
    """Turn primary keyword index records into main/backup/all view routes."""
    mode = _keyword_post_search_display_mode(config)
    main_channel_id = _as_int(config.get("channel_id"))
    primary = [
        {**route, "display_channel": "主频道"}
        for route in routes
        if main_channel_id is None or _as_int(route.get("channel_id")) == main_channel_id
    ]
    if mode == "main":
        return primary[:10]

    result = []
    for route in primary:
        backup_route = _backup_keyword_route(route, config)
        if mode == "backup":
            if backup_route:
                result.append(backup_route)
        else:
            result.append(route)
            if backup_route:
                result.append(backup_route)
    deduped = []
    seen = set()
    for route in result:
        key = (_as_int(route.get("channel_id")), _as_int(route.get("channel_message_id")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(route)
    return deduped[:20]


def _fallback_channel_message_link(channel_id, message_id):
    channel = str(channel_id or "")
    if channel.startswith("-100") and str(message_id).isdigit():
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return None


async def _route_message_link(context: ContextTypes.DEFAULT_TYPE, route: dict):
    """Build a public link when possible, otherwise use Telegram's private link."""
    channel_id = route.get("channel_id")
    # Keyword routes use channel_message_id; check-in records use message_id.
    message_id = route.get("channel_message_id") or route.get("message_id")
    try:
        chat = await context.bot.get_chat(channel_id)
        username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
        if username and message_id:
            return f"https://t.me/{username}/{message_id}"
    except Exception:
        pass
    return _fallback_channel_message_link(channel_id, message_id)


def _keyword_post_results_keyboard(routes: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for index, route in enumerate(routes):
        label = str(route.get("label", "关键词"))[:12]
        value = str(route.get("raw", route.get("key", "")))[:30]
        channel_label = str(route.get("display_channel") or "主频道")
        rows.append([InlineKeyboardButton(
            f"查看 {channel_label} · {label}：{value}",
            callback_data=f"publish:keyword_post_pick:{index}",
        )])
    rows.append([InlineKeyboardButton("❌ 取消查询", callback_data="publish:keyword_post_search_cancel")])
    return InlineKeyboardMarkup(rows)


async def _send_keyword_post_result(message, context: ContextTypes.DEFAULT_TYPE, route: dict) -> None:
    """Send the indexed channel post to a private lookup requester.

    Copying shows the full original post inside the bot chat. If Telegram does
    not allow copying (for example, protected content), a channel link is used
    as the fallback.
    """
    label = str(route.get("label", "关键词"))
    value = str(route.get("raw", route.get("key", "")))
    link = await _route_message_link(context, route)
    try:
        await context.bot.copy_message(
            chat_id=message.chat_id,
            from_chat_id=route["channel_id"],
            message_id=route["channel_message_id"],
        )
        if link:
            await message.reply_text(
                f"🔎 已找到 {label}：{value}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔗 在频道中打开", url=link),
                        InlineKeyboardButton("⬅️ 返回", callback_data="publish:keyword_post_return"),
                    ]
                ]),
                disable_web_page_preview=True,
            )
        return
    except Exception as exc:
        print(f"关键词帖子复制失败: {exc}")

    if link:
        await message.reply_text(
            f"🔎 已找到 {label}：{value}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔗 查看对应帖子", url=link),
                    InlineKeyboardButton("⬅️ 返回", callback_data="publish:keyword_post_return"),
                ]
            ]),
            disable_web_page_preview=True,
        )
    else:
        await message.reply_text("❗ 已找到对应帖子，但暂时无法打开或复制该消息。")


async def _handle_keyword_post_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle post lookup only after the user explicitly opened search."""
    if not (context.user_data or {}).get(KEYWORD_POST_SEARCH_INPUT_KEY):
        return False
    msg = update.message
    if not msg or not msg.text or not update.effective_chat:
        return True
    if update.effective_chat.type != "private":
        return False

    query = msg.text.strip()
    if query in {"取消", "返回"}:
        context.user_data.pop(KEYWORD_POST_SEARCH_INPUT_KEY, None)
        context.user_data.pop(KEYWORD_POST_RESULTS_KEY, None)
        await msg.reply_text(
            "已取消帖子关键词查询。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回首页", callback_data="start:back")]
            ]),
        )
        return True
    if not query or len(query) > 64 or "\n" in query or any(char.isspace() for char in query):
        await msg.reply_text("❗ 请发送一个不含空格的关键词，例如：悠悠 或 @youyouabc。")
        return True

    config = load_publish_config()
    routes = _keyword_post_display_routes(
        _find_keyword_routes_with_artist(query, require_discussion=False),
        config,
    )
    if not routes:
        await msg.reply_text("❗ 未找到对应收录帖子。你可以继续输入其他关键词，或发送“取消”。")
        return True

    if len(routes) == 1:
        context.user_data.pop(KEYWORD_POST_SEARCH_INPUT_KEY, None)
        await _send_keyword_post_result(msg, context, routes[0])
        return True

    context.user_data[KEYWORD_POST_RESULTS_KEY] = routes
    rows = []
    for index, route in enumerate(routes):
        label = str(route.get("label", "关键词"))[:12]
        value = str(route.get("raw", route.get("key", "")))[:30]
        extra = ""
        if label == "标签":
            artist_name = str(route.get("艺名", "")).strip()
            if artist_name:
                extra = f" · {artist_name[:20]}"
            
        channel_label = str(route.get("display_channel") or "主频道")
        rows.append([
            InlineKeyboardButton(
                f"查看 {channel_label} · {label}：{value} {extra}",
                callback_data=f"publish:keyword_post_pick:{index}",
            )
        ])
    await msg.reply_text("找到多个对应帖子，请选择要查看的帖子：", reply_markup=InlineKeyboardMarkup(rows))
    return True


def _keyword_route_keyboard(route: dict, link: str, enabled: bool):
    rows = []
    if link:
        rows.append([InlineKeyboardButton("🔗 查看对应消息", url=link)])
    rows.extend(create_post_keyboard(enabled).inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _keyword_group_reply_rules(config: dict) -> list[dict]:
    rules = config.get("keyword_group_reply_rules", []) if isinstance(config, dict) else []
    if not isinstance(rules, list):
        return []
    result = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        input_label = str(rule.get("input_label") or "").strip()[:30]
        reply_label = str(rule.get("reply_label") or "").strip()[:30]
        if not input_label or not reply_label:
            continue
        try:
            rule_id = int(rule.get("id", 0) or 0)
        except (TypeError, ValueError):
            rule_id = 0
        if rule_id <= 0:
            continue
        result.append({
            "id": rule_id,
            "input_label": input_label,
            "reply_label": reply_label,
            "display_text": str(
                rule.get("display_text") or f"{reply_label}：{{value}}"
            )[:500],
            "default_text": str(rule.get("default_text") or "")[:500],
            "button_text": str(rule.get("button_text") or "联系 {value}")[:64],
            "enabled": bool(rule.get("enabled", True)),
        })
    return result


def _group_keyword_reply_settings_text(config: dict) -> str:
    rules = _keyword_group_reply_rules(config)
    lines = [
        "💬 群关键词回复",
        "",
        f"状态：{'✅ 开启' if config.get('keyword_group_reply_enabled', False) else '🚫 关闭'}",
        f"规则数量：{len(rules)}",
        "",
        "群成员发送艺名等关键词时，机器人会从同一收录帖子中查找对应字段，",
        "把 @用户名显示成 Telegram 联系链接；多个联系方式会显示多个按钮。",
    ]
    if rules:
        lines.extend(["", "当前规则："])
        for rule in rules:
            default_text = rule.get("default_text") or "未设置"
            lines.append(
                f"#{rule['id']} 输入「{rule['input_label']}」 → 回复「{rule['reply_label']}」\n"
                f"默认咨询文字：{default_text[:80]}"
            )
    return "\n".join(lines)


def _group_keyword_reply_settings_keyboard(config: dict) -> InlineKeyboardMarkup:
    enabled = bool(config.get("keyword_group_reply_enabled", False))
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if enabled else '🚫'} 群关键词回复",
            callback_data="publish:toggle_group_keyword_reply",
        )],
        [InlineKeyboardButton("➕ 添加规则", callback_data="publish:group_keyword_reply_add")],
    ]
    for rule in _keyword_group_reply_rules(config):
        rows.append([InlineKeyboardButton(
            f"📝 {rule['input_label']} → {rule['reply_label']}",
            callback_data=f"publish:group_keyword_reply_view:{rule['id']}",
        )])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


def _group_keyword_reply_rule_text(rule: dict) -> str:
    return "\n".join([
        "💬 群关键词回复规则",
        f"输入关键词字段：{rule.get('input_label', '')}",
        f"回复关键词字段：{rule.get('reply_label', '')}",
        f"显示文案：{rule.get('display_text') or '{value}'}",
        f"私聊预填文字：{rule.get('default_text') or '未设置'}",
        f"联系方式按钮文案：{rule.get('button_text') or '联系 {{value}}'}",
        f"状态：{'✅ 开启' if rule.get('enabled', True) else '🚫 关闭'}",
    ])


def _group_keyword_reply_rule_keyboard(rule: dict) -> InlineKeyboardMarkup:
    rule_id = int(rule["id"])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ 输入字段", callback_data=f"publish:group_keyword_reply_field:input_label:{rule_id}"),
            InlineKeyboardButton("✏️ 回复字段", callback_data=f"publish:group_keyword_reply_field:reply_label:{rule_id}"),
        ],
        [
            InlineKeyboardButton("📝 显示文案", callback_data=f"publish:group_keyword_reply_field:display_text:{rule_id}"),
            InlineKeyboardButton("💬 私聊预填", callback_data=f"publish:group_keyword_reply_field:default_text:{rule_id}"),
        ],
        [InlineKeyboardButton("🔘 按钮文案", callback_data=f"publish:group_keyword_reply_field:button_text:{rule_id}")],
        [InlineKeyboardButton(
            f"{'🚫 关闭' if rule.get('enabled', True) else '✅ 开启'} 此规则",
            callback_data=f"publish:group_keyword_reply_toggle:{rule_id}",
        )],
        [InlineKeyboardButton("🗑 删除规则", callback_data=f"publish:group_keyword_reply_delete:{rule_id}")],
        [InlineKeyboardButton("⬅️ 返回规则列表", callback_data="publish:group_keyword_reply")],
    ])


def _find_group_keyword_reply_values(query: str, input_label: str, reply_label: str) -> list[dict]:
    """Find reply-label values from posts matching one configured input label."""
    query_key = _normalize_routing_keyword(query)
    if not query_key:
        return []
    data = _load_keyword_map()
    source_posts = set()
    for stored_key, records in data.items():
        if query_key != _normalize_routing_keyword(stored_key):
            continue
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            if str(record.get("label") or "").strip() != input_label:
                continue
            channel_id = _as_int(record.get("channel_id"))
            message_id = _as_int(record.get("channel_message_id"))
            if channel_id is not None and message_id is not None:
                source_posts.add((channel_id, message_id))

    values = []
    seen = set()
    for records in data.values():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            if str(record.get("label") or "").strip() != reply_label:
                continue
            channel_id = _as_int(record.get("channel_id"))
            message_id = _as_int(record.get("channel_message_id"))
            if (channel_id, message_id) not in source_posts:
                continue
            value = str(record.get("raw") or record.get("key") or "").strip()
            if value and value not in seen:
                seen.add(value)
                values.append({
                    "value": value,
                    "channel_id": channel_id,
                    "message_id": message_id,
                })
    return values[:20]


def _telegram_contact_buttons(
    value: str,
    button_template: str,
    query: str,
    default_text_template: str,
    all_contacts: list[str],
) -> list[InlineKeyboardButton]:
    """Build Telegram private-chat links, optionally with a prefilled text."""
    usernames = re.findall(r"@([A-Za-z0-9_]{5,32})", str(value or ""))
    if not usernames:
        return []
    buttons = []
    for username in dict.fromkeys(usernames):
        contact = f"@{username}"
        try:
            label = str(button_template or "联系 {value}").format(
                value=contact,
                keyword=query,
            )
        except Exception:
            label = f"联系 {contact}"
        default_text = _render_group_keyword_default_text(
            default_text_template,
            query,
            [contact, *[item for item in all_contacts if item != contact]],
        )
        url = f"https://t.me/{username}"
        if default_text:
            # Telegram opens the username's private chat and pre-fills this
            # message in its composer (the same behavior as t.me/name?text=...).
            url += "?text=" + quote(default_text, safe="")
        buttons.append(InlineKeyboardButton(label[:64] or contact, url=url))
    return buttons


def _render_group_keyword_display_text(rule: dict, query: str, contacts: list[str]) -> str:
    template = str(rule.get("display_text") or "{value}")
    values = "、".join(contacts)
    try:
        return template.format(
            keyword=query,
            value=contacts[0] if contacts else "",
            values=values,
            contacts=values,
            input_label=rule.get("input_label", ""),
            reply_label=rule.get("reply_label", ""),
        )[:500]
    except Exception:
        return template[:500]


def _render_group_keyword_default_text(template: str, query: str, contacts: list[str]) -> str:
    if not template:
        return ""
    try:
        return str(template).format(
            keyword=query,
            value=contacts[0] if contacts else "",
            contacts="、".join(contacts),
        )[:500]
    except Exception:
        return str(template)[:500]


async def _handle_group_keyword_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or chat.type not in {"group", "supergroup"}:
        return False
    if not msg.text or (user and user.is_bot):
        return False
    query = msg.text.strip()
    if not query or len(query) > 64 or "\n" in query or any(char.isspace() for char in query):
        return False

    config = load_publish_config()
    if not bool(config.get("keyword_group_reply_enabled", False)):
        return False
    rows = []
    display_messages = []
    for rule in _keyword_group_reply_rules(config):
        if not rule.get("enabled", True):
            continue
        values = _find_group_keyword_reply_values(
            query,
            rule["input_label"],
            rule["reply_label"],
        )
        if not values:
            continue
        contacts = []
        rule_buttons = []
        for item in values:
            contacts.append(item["value"])
        for item in values:
            rule_buttons.extend(
                _telegram_contact_buttons(
                    item["value"],
                    rule["button_text"],
                    query,
                    rule.get("default_text", ""),
                    contacts,
                )
            )
        if not rule_buttons:
            continue
        rows.extend([[button] for button in rule_buttons])
        display_text = _render_group_keyword_display_text(rule, query, contacts)
        if display_text and display_text not in display_messages:
            display_messages.append(display_text)

    if not rows:
        return False
    response_text = "\n\n".join(display_messages) or "已找到联系方式："
    # await msg.reply_text(
    #     response_text,
    #     reply_markup=InlineKeyboardMarkup(rows),
    #     disable_web_page_preview=True,
    # )
    
    #     update: Update,
    # context: ContextTypes.DEFAULT_TYPE,
    # text: str,
    # html: bool = False,
    # reply_markup=None,
    # auto_delete_seconds: int = 60,
    # bot_reply: bool = False,
    
    await safe_reply(update, context, response_text, reply_markup=InlineKeyboardMarkup(rows), auto_delete_seconds = 30, bot_reply = True)
    return True


def _keyword_submission_mode_keyboard(route: dict, link: str) -> InlineKeyboardMarkup:
    rows = []
    if link:
        rows.append([InlineKeyboardButton("🔗 查看对应帖子", url=link)])
    rows.append([
        InlineKeyboardButton("🙈 匿名投稿", callback_data="publish:keyword_submit_mode:anonymous"),
        InlineKeyboardButton("👤 实名投稿", callback_data="publish:keyword_submit_mode:real"),
    ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
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

    if submission.get("anonymous"):
        await context.bot.copy_message(
            chat_id=owner_id,
            from_chat_id=submission["user_chat_id"],
            message_id=submission["user_message_id"],
        )
    else:
        await context.bot.forward_message(
            chat_id=owner_id,
            from_chat_id=submission["user_chat_id"],
            message_id=submission["user_message_id"],
        )
    if submission.get("proof_message_id"):
        if submission.get("anonymous"):
            await context.bot.copy_message(
                chat_id=owner_id,
                from_chat_id=submission["proof_chat_id"],
                message_id=submission["proof_message_id"],
            )
        else:
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
        _register_checkin_post(config, main_channel_id, msg.message_id, entries)
        await _sync_main_post_edit_to_backup(context, msg, config)
        return

    channel_post = getattr(update, "channel_post", None)
    if (
        channel_post is not None
        and main_channel_id is not None
        and int(getattr(getattr(msg, "chat", None), "id", 0) or 0) == main_channel_id
    ):
        # New or republished channel posts can bypass the normal owner-publish
        # flow, so rebuild the complete keyword index from their current text.
        entries = _extract_routing_keywords(msg, config)
        _register_post_keywords(entries, main_channel_id, msg.message_id)
        _register_checkin_post(config, main_channel_id, msg.message_id, entries)
        print(
            "✅ 已收录新发布频道帖关键词 "
            f"channel={main_channel_id} message={msg.message_id} keywords={len(entries)}"
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
        await _sync_main_post_edit_to_backup(
            context,
            msg,
            config,
            main_channel_id=channel_id,
            main_message_id=channel_message_id,
        )

    if getattr(update, "edited_message", None) is None:
        # Fallback path for bots that receive the new post only as the linked
        # discussion group's automatic forward. Index every configured field
        # here as well, not just the check-in fields.
        entries = _extract_routing_keywords(msg, config)
        _register_post_keywords(entries, int(channel_id), int(channel_message_id))
        _register_checkin_post(config, int(channel_id), int(channel_message_id), entries)
        print(
            "✅ 已通过讨论组转发收录频道帖关键词 "
            f"channel={channel_id} message={channel_message_id} keywords={len(entries)}"
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
    register_keywords: bool = True,
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
    if register_keywords and submission.get("keyword_entries"):
        _register_post_keywords(
            submission["keyword_entries"],
            target_channel_id,
            published.message_id,
        )
        _register_checkin_post(
            config,
            int(target_channel_id),
            published.message_id,
            submission["keyword_entries"],
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

    comment_map = _load_comment_map()
    mapping = comment_map.get(_comment_map_key(main_channel_id, main_message_id))
    if not isinstance(mapping, dict):
        # A post copied into a replacement channel may have entered its linked
        # discussion group before that channel became the configured main
        # channel, so the normal update listener had no reason to store it.
        # Resolve it on demand through the configured protocol account.
        keyword_data = _load_keyword_map()
        resolved, _failed, changed = await _restore_cloned_discussion_mappings(
            context,
            config=config,
            target_channel_id=main_channel_id,
            target_message_ids=[main_message_id],
            comment_map=comment_map,
            keyword_data=keyword_data,
        )
        if changed:
            _save_comment_map(comment_map)
            _save_keyword_map(keyword_data)
        mapping = comment_map.get(_comment_map_key(main_channel_id, main_message_id))
        if not isinstance(mapping, dict):
            raise RuntimeError(
                "未找到主频道帖子的讨论组映射。请确认新频道已绑定讨论组、"
                "协议号可访问该频道及讨论组，且机器人是讨论组管理员。"
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
    await _publish_comment_to_backup_discussion(
        context,
        submission,
        config,
        main_channel_id,
        main_message_id,
    )
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
                await _mirror_main_post_to_backup(
                    context,
                    config,
                    int(target_channel_id),
                    published.message_id,
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


def _template_button_columns(template: dict) -> int:
    """Return the number of text links displayed on one line.

    Template links are deliberately rendered in the message body instead of as
    Telegram inline-keyboard buttons. Keep old templates compatible by using
    one link per line when the setting is absent or invalid.
    """
    try:
        return max(1, min(int(template.get("button_columns", 1) or 1), 8))
    except (TypeError, ValueError):
        return 1


def _template_keyboard(template: dict, values: dict):
    """Render configured template buttons as HTML text links.

    ``button_columns`` controls wrapping: links in the same row are separated
    by spaces, and a newline is inserted only after the configured count.
    """
    links = []

    for button in template.get("buttons", []) or []:
        if not isinstance(button, dict):
            continue

        text = _render_template_value(
            str(button.get("text") or ""),
            values,
            html_mode=False,
        ).strip()
        url = _render_template_value(
            str(button.get("url") or ""),
            values,
            html_mode=False,
        ).strip()
        url = _normalize_button_url(url)

        if text and url:
            # Escape rendered text/URL so a value cannot break the HTML body.
            links.append(
                f'<a href="{html.escape(url, quote=True)}">'
                f'{html.escape(text[:64])}</a>'
            )

    if not links:
        return None
    columns = _template_button_columns(template)
    return "\n".join(
        "  ".join(links[index:index + columns])
        for index in range(0, len(links), columns)
    )


def _render_template(template: dict, values: dict):
    html_mode = str(template.get("format") or "plain") == "html"
    text = _render_template_value(str(template.get("text") or ""), values, html_mode=html_mode)
    link_text = _template_keyboard(template, values)

    if link_text:
        # Text hyperlinks require HTML parsing.  For a plain-text body, escape
        # it first so it remains literal while the appended <a> tags work.
        if not html_mode:
            text = html.escape(text)
        text = f"{text.rstrip()}\n\n{link_text}"
        return text, None, "HTML"

    return text, None, "HTML" if html_mode else None


def _template_flow_preview(template: dict, values: dict) -> str:
    text, _, mode = _render_template(template, values)
    name = str(template.get("name", "未命名"))
    if mode == "HTML":
        name = html.escape(name)
    return "\n".join([
        f"🧩 模板预览：{name}",
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
        "超链接填写格式：按钮文字 | https://链接",
        "可设置每行显示几个超链接；同一行的链接以空格分隔。",
        "发布时机器人会逐项询问占位符的值。",
    ]
    return "\n".join(lines)


def _template_publish_keyboard(config: dict) -> InlineKeyboardMarkup:
    """Build the template picker used by the administrator publish flow."""
    rows = []
    for template in _publish_templates(config):
        template_id = int(template.get("id", 0) or 0)
        name = str(template.get("name") or f"模板 {template_id}")[:48]
        rows.append([
            InlineKeyboardButton(
                f"🧩 {name}",
                callback_data=f"publish:template_use:{template_id}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="start:back")])
    return InlineKeyboardMarkup(rows)


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
        f"文本超链接：{button_count} 个（每行 {_template_button_columns(template)} 个）",
        "",
        str(template.get("text") or ""),
    ])


def _template_draft_preview(draft: dict) -> str:
    template = {
        "name": draft.get("name", "未命名"),
        "text": draft.get("text", ""),
        "format": draft.get("format", "plain"),
        "buttons": draft.get("buttons", []),
        "button_columns": draft.get("button_columns", 1),
    }
    return _template_detail_text(template) + "\n\n确认保存该模板？"


def _template_edit_keyboard(draft: dict) -> InlineKeyboardMarkup:
    """Keyboard for editing a draft without discarding its other fields."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 模板名称", callback_data="publish:template_edit_field:name")],
        [InlineKeyboardButton("📝 模板正文", callback_data="publish:template_edit_field:text")],
        [InlineKeyboardButton("🔤 文本格式", callback_data="publish:template_edit_field:format")],
        [InlineKeyboardButton("🔗 文本超链接", callback_data="publish:template_edit_field:buttons")],
        [InlineKeyboardButton(
            f"↔️ 每行链接数（当前 {_template_button_columns(draft)}）",
            callback_data="publish:template_edit_field:button_columns",
        )],
        [InlineKeyboardButton("✅ 保存修改", callback_data="publish:template_save")],
        [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
    ])


def _template_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("纯文本", callback_data="publish:template_format:plain")],
        [InlineKeyboardButton("HTML 格式", callback_data="publish:template_format:html")],
        [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
    ])


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
        [
            InlineKeyboardButton("📢 发布频道", callback_data="publish:channel"),
            InlineKeyboardButton("📝 审核设置", callback_data="publish:review"),
        ],
        # [InlineKeyboardButton("📊 每日发布上限", callback_data="publish:limit")],
        # [InlineKeyboardButton("📣 广告管理", callback_data="publish:ads")],
        # [InlineKeyboardButton("🔘 底部按钮设置", callback_data="publish:buttons")],
        [
            InlineKeyboardButton("🔑 关键词设置", callback_data="publish:keywords"),
            InlineKeyboardButton("💬 群关键词回复", callback_data="publish:group_keyword_reply"),
        ],
        [InlineKeyboardButton(
            f"🔎 搜索展示：{KEYWORD_SEARCH_DISPLAY_OPTIONS[_keyword_post_search_display_mode(config)]}",
            callback_data="publish:keyword_search_display",
        )],
        [InlineKeyboardButton("🟢 在线打卡设置", callback_data="publish:checkin_settings")],
        [
            
            InlineKeyboardButton("🔘 底部按钮设置", callback_data="publish:buttons"),
            
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
        [   
            InlineKeyboardButton(
            f"{'✅' if report_link_enabled else '🚫'} 生成报告链接",
            callback_data="publish:toggle_report_link",
            ),
            InlineKeyboardButton(
                    "📛 用户名报告迁移",
                     callback_data="publish:report_username_migrate",
                 ),
         ],
        [InlineKeyboardButton(
            "🔄 一键迁移历史评论到当前转发频道(防止转发频道炸了)",
            callback_data="publish:migrate_forward_comments",
        )],
        [InlineKeyboardButton(
            "🔗 关联已克隆历史主帖到当前发布频道(防止主频道炸了,目前模式不需要)",
            callback_data="publish:migrate_history_posts",
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


def publish_channel_keyboard(config: dict):
    backup_channel_id = _backup_channel_id(config)
    rows = [
        [InlineKeyboardButton("✏️ 修改主频道", callback_data="publish:set_channel")],
        [InlineKeyboardButton(
            "✏️ 设置备用频道" if backup_channel_id is None else "✏️ 修改备用频道",
            callback_data="publish:set_backup_channel",
        )],
    ]
    if backup_channel_id is not None:
        rows.append([InlineKeyboardButton("🗑 清除备用频道", callback_data="publish:clear_backup_channel")])
        rows.append([InlineKeyboardButton(
            f"备用同步方式：{_backup_transport_label(config)}",
            callback_data="publish:backup_transport_toggle",
        )])
        if _backup_channel_use_telethon(config):
            rows.append([InlineKeyboardButton(
                f"📡 监听协议号：{_backup_channel_listen_session(config) or '未选择'}",
                callback_data="publish:backup_transport_listen_session",
            )])
            rows.append([InlineKeyboardButton(
                f"📤 转发协议号：{_backup_channel_forward_session(config) or '未选择'}",
                callback_data="publish:backup_transport_forward_session",
            )])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


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
        comments = report.get("comments", []) if isinstance(report, dict) else []
        nav_row = []
        previous_index = index - 1
        next_index = index + 1
        if previous_index >= 0:
            previous_page = previous_index // REPORT_PAGE_SIZE + 1
            nav_row.append(InlineKeyboardButton(
                "⬅️ 上一条",
                callback_data=f"publish:report_detail:{parts[2]}:{previous_page}:{previous_index}",
            ))
        nav_row.append(InlineKeyboardButton(
            "⬅️ 返回报告",
            callback_data=f"publish:report_page:{parts[2]}:{page}",
        ))
        if next_index < len(comments):
            next_page = next_index // REPORT_PAGE_SIZE + 1
            nav_row.append(InlineKeyboardButton(
                "➡️ 下一条",
                callback_data=f"publish:report_detail:{parts[2]}:{next_page}:{next_index}",
            ))
        rows.append(nav_row)
        await query.answer()
        return await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), disable_web_page_preview=True)

    if action == "checkin_view":
        parts = query.data.split(":")
        if len(parts) != 4:
            return await query.answer("帖子参数无效。", show_alert=True)
        channel_id = _as_int(parts[2])
        message_id = _as_int(parts[3])
        if channel_id is None or message_id is None:
            return await query.answer("帖子参数无效。", show_alert=True)
        try:
            await context.bot.copy_message(
                chat_id=query.message.chat_id,
                from_chat_id=channel_id,
                message_id=message_id,
            )
            return await query.answer("已发送帖子内容。")
        except Exception as exc:
            print(f"在线帖子查看失败 channel={channel_id}/{message_id}: {exc}")
            return await query.answer("暂时无法读取该帖子，请确认机器人可访问主频道。", show_alert=True)

    if action == "keyword_post_search":
        context.user_data[KEYWORD_POST_SEARCH_INPUT_KEY] = True
        context.user_data.pop(KEYWORD_POST_RESULTS_KEY, None)
        await query.answer()
        # Keep the original start/menu page untouched. The search prompt is a
        # separate message, so cancelling naturally returns the user to it.
        return await query.message.reply_text(
            "🔎 请输入要查询的收录关键词，例如：悠悠 或 @youyouabc。\n"
            "发送“取消”或点击下方按钮可退出查询。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消查询", callback_data="publish:keyword_post_search_cancel")]
            ]),
        )

    if action == "keyword_post_return":
        routes = (context.user_data or {}).get(KEYWORD_POST_RESULTS_KEY, [])
        await query.answer()
        if isinstance(routes, list) and routes:
            return await query.edit_message_text(
                "找到多个对应帖子，请选择要查看的帖子：",
                reply_markup=_keyword_post_results_keyboard(routes),
            )
        context.user_data[KEYWORD_POST_SEARCH_INPUT_KEY] = True
        return await query.edit_message_text(
            "🔎 请输入要查询的收录关键词，例如：悠悠 或 @youyouabc。\n"
            "发送“取消”可退出查询。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消查询", callback_data="publish:keyword_post_search_cancel")]
            ]),
        )

    if action == "keyword_post_search_cancel":
        context.user_data.pop(KEYWORD_POST_SEARCH_INPUT_KEY, None)
        context.user_data.pop(KEYWORD_POST_RESULTS_KEY, None)
        await query.answer("已取消查询。")
        # The prompt was sent as a separate message, so deleting it exposes
        # the untouched start/menu page directly underneath.
        try:
            return await query.message.delete()
        except Exception:
            return await query.edit_message_text("已取消帖子关键词查询。")

    if action == "keyword_post_pick":
        results = (context.user_data or {}).get(KEYWORD_POST_RESULTS_KEY, [])
        try:
            result_index = int(query.data.split(":", 2)[2])
            route = results[result_index]
        except (ValueError, IndexError, TypeError):
            return await query.answer("关键词结果已失效，请重新搜索。", show_alert=True)
        context.user_data[KEYWORD_POST_SEARCH_INPUT_KEY] = True
        await query.answer()
        await _send_keyword_post_result(query.message, context, route)
        try:
            return await query.edit_message_text("✅ 已发送对应帖子。")
        except Exception:
            return

    if action == "keyword_submit_mode":
        mode = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if mode not in {"anonymous", "real"}:
            return await query.answer("投稿方式无效。", show_alert=True)
        if not isinstance(context.user_data.get(COMMENT_TARGET_KEY), dict):
            return await query.answer("选中的帖子已失效，请重新查询。", show_alert=True)
        anonymous = mode == "anonymous"
        context.user_data["post_no_name"] = anonymous
        context.user_data["waiting_post"] = True
        await query.answer("已选择匿名投稿。" if anonymous else "已选择实名投稿。")
        return await query.edit_message_text(
            f"{'🙈 匿名投稿' if anonymous else '👤 实名投稿'}已开启。\n\n"
            "请发送投稿内容，审核通过后会评论到选中的帖子下，并发布到转发频道。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 取消", callback_data="start:back")]
            ]),
        )

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
        context.user_data["waiting_post"] = False
        link = await _route_message_link(context, route)
        await query.answer()
        return await query.edit_message_text(
            f"✅ 已选择 {route.get('label', '关键词')}：{route.get('raw', route.get('key'))}。\n"
            "请选择匿名投稿或实名投稿，选择后才会开始输入投稿内容。",
            reply_markup=_keyword_submission_mode_keyboard(route, link),
        )

    # All remaining actions below are publishing configuration actions.  Do not
    # rely on the hidden start-menu button: callbacks can be forged manually.
    public_actions = {"publish", "channel_message", "checkin_view", "keyword_post_search", "keyword_post_search_cancel", "keyword_post_return", "keyword_submit_mode", "bottle_prev", "bottle_next", "accept_friend", "reject_friend", "back"}
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
            "id": next_id,
            "step": "name",
            "format": "plain",
            "buttons": [],
            "button_columns": 1,
            "is_new": True,
        }
        return await query.edit_message_text(
            "请输入模板名称，例如：招聘发布。\n发送“取消”可放弃添加。"
        )

    if action == "template_format":
        draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
        mode = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if not isinstance(draft, dict) or mode not in {"plain", "html"}:
            return await query.answer("模板草稿已失效，请重新添加。", show_alert=True)

        # The body was received before this choice.  PTB exposes its original
        # Telegram entities as ``text_html``; use that representation only
        # when HTML was selected so bold/italic/link formatting is retained.
        if draft.get("body_plain") is not None:
            draft["text"] = (
                draft.get("body_html", draft["body_plain"])
                if mode == "html"
                else draft["body_plain"]
            )
            draft.pop("body_plain", None)
            draft.pop("body_html", None)
        draft["format"] = mode

        next_step = draft.pop("after_format_step", "buttons")
        if next_step == "edit_menu":
            draft["step"] = "edit_menu"
            return await query.edit_message_text(
                "请选择继续修改的项目，或保存修改：",
                reply_markup=_template_edit_keyboard(draft),
            )

        draft["step"] = "buttons"
        return await query.edit_message_text(
            "请输入文本超链接，每行一个：\n"
            "按钮文字 | https://example.com\n\n"
            "没有超链接请发送“无”。按钮文字和链接都可使用 {键名}。"
        )

    if action == "template_save":
        draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
        if not isinstance(draft, dict) or not draft.get("name") or not draft.get("text"):
            return await query.answer("模板草稿不完整，请重新添加。", show_alert=True)
        if draft.get("step") not in {"confirm", "edit_menu"}:
            return await query.answer("请先完成当前编辑项目。", show_alert=True)
        templates = _publish_templates(config)
        templates = [item for item in templates if int(item.get("id", 0) or 0) != int(draft["id"])]
        templates.append({
            "id": int(draft["id"]),
            "name": str(draft["name"])[:50],
            "text": str(draft["text"]),
            "format": str(draft.get("format") or "plain"),
            "buttons": list(draft.get("buttons") or []),
            "button_columns": _template_button_columns(draft),
        })
        config["publish_templates"] = templates
        save_publish_config(config)
        context.user_data.pop(TEMPLATE_DRAFT_KEY, None)
        return await query.edit_message_text(
            "✅ 模板已保存。\n\n" + _template_settings_text(config),
            reply_markup=_template_settings_keyboard(config),
        )

    if action == "template_cancel":
        # Adding/editing a template belongs to the template-settings screen,
        # while cancelling an actual template *publication* must return to the
        # template picker the user came from.
        had_publish_flow = isinstance(context.user_data.get(TEMPLATE_FLOW_KEY), dict)
        context.user_data.pop(TEMPLATE_DRAFT_KEY, None)
        context.user_data.pop(TEMPLATE_FLOW_KEY, None)
        if had_publish_flow:
            return await query.edit_message_text(
                "已取消本次模板发布，请重新选择模板：",
                reply_markup=_template_publish_keyboard(config),
            )
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
                [InlineKeyboardButton("✏️ 编辑模板", callback_data=f"publish:template_edit:{template_id}")],
                [InlineKeyboardButton("🗑 删除模板", callback_data=f"publish:template_delete:{template_id}")],
                [InlineKeyboardButton("⬅️ 返回模板列表", callback_data="publish:template_settings")],
            ]),
        )

    if action == "template_edit":
        try:
            template_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("模板数据无效。", show_alert=True)
        template = _template_by_id(config, template_id)
        if not template:
            return await query.answer("模板不存在。", show_alert=True)
        context.user_data[TEMPLATE_DRAFT_KEY] = {
            "id": template_id,
            "name": str(template.get("name") or ""),
            "text": str(template.get("text") or ""),
            "format": str(template.get("format") or "plain"),
            "buttons": [dict(button) for button in template.get("buttons", []) if isinstance(button, dict)],
            "button_columns": _template_button_columns(template),
            "step": "edit_menu",
            "is_new": False,
        }
        return await query.edit_message_text(
            "✏️ 编辑模板：请选择要修改的项目。",
            reply_markup=_template_edit_keyboard(context.user_data[TEMPLATE_DRAFT_KEY]),
        )

    if action == "template_edit_field":
        draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
        field = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if not isinstance(draft, dict) or draft.get("step") != "edit_menu":
            return await query.answer("编辑草稿已失效，请重新打开模板。", show_alert=True)
        prompts = {
            "name": "请输入新的模板名称（最多 50 个字符）：",
            "text": "请输入新的模板正文。发送时的粗体、斜体、链接等格式会在选择 HTML 后保留。",
            "buttons": (
                "请输入文本超链接，每行一个：\n"
                "按钮文字 | https://example.com\n\n"
                "没有超链接请发送“无”。"
            ),
            "button_columns": "请输入每行显示几个超链接（1-8）。同一行的链接会用空格隔开：",
        }
        if field == "format":
            draft["after_format_step"] = "edit_menu"
            return await query.edit_message_text("请选择文本格式：", reply_markup=_template_format_keyboard())
        if field not in prompts:
            return await query.answer("不支持的编辑项目。", show_alert=True)
        draft["step"] = f"edit_{field}"
        return await query.edit_message_text(prompts[field])

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
        return await query.edit_message_text(
            "请选择要发布的模板：",
            reply_markup=_template_publish_keyboard(config),
        )

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
                parse_mode=_render_template(template, {})[2],
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
            report_id = _report_id_from_subject_entries(entries)
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
        _register_checkin_post(config, channel_id_int, published.message_id, entries)
        if report_id and report_url:
            _create_comment_report(
                channel_id_int,
                published.message_id,
                report_id,
                subject_entries=entries,
            )
        await _mirror_main_post_to_backup(
            context,
            config,
            channel_id_int,
            published.message_id,
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

    if action == "migrate_history_posts":
        target_channel_id = _as_int(config.get("channel_id"))
        if target_channel_id is None:
            return await query.answer("请先将“发布频道”设置为新的目标频道。", show_alert=True)
        if context.application.bot_data.get("history_post_migration_in_progress"):
            return await query.answer("历史主帖正在关联，请稍候。", show_alert=True)

        source_counts = _migration_source_counts(target_channel_id)
        pending = config.get("pending_history_post_migration")
        recorded_source = (
            _as_int(pending.get("old_channel_id"))
            if isinstance(pending, dict)
            and _as_int(pending.get("new_channel_id")) == target_channel_id
            else None
        )
        if recorded_source is not None and source_counts.get(recorded_source):
            return await _start_historical_post_migration(
                query, context, recorded_source, target_channel_id
            )
        if len(source_counts) == 1:
            source_channel_id = next(iter(source_counts))
            return await _start_historical_post_migration(
                query, context, source_channel_id, target_channel_id
            )
        if not source_counts:
            return await query.answer(
                "未在 history_forward_state 中找到当前新频道的消息映射。",
                show_alert=True,
            )
        await query.answer()
        return await query.edit_message_text(
            "当前新频道对应多个旧频道来源，请选择要关联的旧频道：",
            reply_markup=_migration_source_keyboard(source_counts),
        )

    if action == "migrate_history_source":
        try:
            source_channel_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("旧频道数据无效。", show_alert=True)
        target_channel_id = _as_int(config.get("channel_id"))
        if target_channel_id is None:
            return await query.answer("当前发布频道无效。", show_alert=True)
        if context.application.bot_data.get("history_post_migration_in_progress"):
            return await query.answer("历史主帖正在关联，请稍候。", show_alert=True)
        return await _start_historical_post_migration(
            query, context, source_channel_id, target_channel_id
        )

    if action == "report_username_migrate":
        context.user_data[REPORT_USERNAME_MIGRATION_KEY] = {"step": "new"}
        return await query.edit_message_text(
            "📛 用户名报告迁移\n\n"
            "请先输入新的用户名，例如：@bbb。\n"
            "下一步再输入旧用户名，例如：@aaa。\n\n"
            "旧报告的评论会合并到新用户名报告下；旧链接也会自动重定向到新报告。"
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

    if action == "group_keyword_reply":
        return await query.edit_message_text(
            _group_keyword_reply_settings_text(config),
            reply_markup=_group_keyword_reply_settings_keyboard(config),
        )

    if action == "toggle_group_keyword_reply":
        config["keyword_group_reply_enabled"] = not bool(
            config.get("keyword_group_reply_enabled", False)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            _group_keyword_reply_settings_text(config),
            reply_markup=_group_keyword_reply_settings_keyboard(config),
        )

    if action == "group_keyword_reply_add":
        rules = _keyword_group_reply_rules(config)
        if len(rules) >= 20:
            return await query.answer("最多只能设置 20 条群关键词回复规则。", show_alert=True)
        next_id = max((int(rule["id"]) for rule in rules), default=0) + 1
        context.user_data[GROUP_KEYWORD_REPLY_STAGE_KEY] = {
            "id": next_id,
            "mode": "add",
            "step": "input_label",
        }
        return await query.edit_message_text(
            "请输入输入关键词字段名，例如：艺名。\n"
            "群成员发送“悠悠”时，会先在“艺名”字段中查找“悠悠”。"
        )

    if action == "group_keyword_reply_view":
        try:
            rule_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("规则数据无效。", show_alert=True)
        rule = next((item for item in _keyword_group_reply_rules(config) if item["id"] == rule_id), None)
        if not rule:
            return await query.answer("规则不存在。", show_alert=True)
        return await query.edit_message_text(
            _group_keyword_reply_rule_text(rule),
            reply_markup=_group_keyword_reply_rule_keyboard(rule),
        )

    if action == "group_keyword_reply_field":
        parts = query.data.split(":")
        if len(parts) != 4:
            return await query.answer("规则字段数据无效。", show_alert=True)
        field = parts[2]
        try:
            rule_id = int(parts[3])
        except ValueError:
            return await query.answer("规则数据无效。", show_alert=True)
        rule = next((item for item in _keyword_group_reply_rules(config) if item["id"] == rule_id), None)
        prompts = {
            "input_label": "请输入新的输入关键词字段名，例如：艺名。",
            "reply_label": "请输入新的回复关键词字段名，例如：联系。该字段必须已在关键词提取标签中配置。",
            "display_text": (
                "请输入新的机器人显示文案，例如：联系方式：{value}。\n"
                "支持 {keyword}、{value}、{values}；发送“默认”恢复为“回复字段：{value}”。"
            ),
            "default_text": (
                "请输入点击联系方式后，打开对方私聊时的预填文字。\n"
                "支持 {keyword}、{value}、{contacts}；发送“无”清空。"
            ),
            "button_text": "请输入新的联系方式按钮文案，例如：联系 {value}；发送“默认”恢复默认值。",
        }
        if not rule or field not in prompts:
            return await query.answer("规则或字段不存在。", show_alert=True)
        context.user_data[GROUP_KEYWORD_REPLY_STAGE_KEY] = {
            "mode": "field",
            "id": rule_id,
            "field": field,
            "step": "field",
        }
        return await query.edit_message_text(prompts[field])

    if action == "group_keyword_reply_toggle":
        try:
            rule_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("规则数据无效。", show_alert=True)
        rules = _keyword_group_reply_rules(config)
        rule = next((item for item in rules if item["id"] == rule_id), None)
        if not rule:
            return await query.answer("规则不存在。", show_alert=True)
        rule["enabled"] = not bool(rule.get("enabled", True))
        config["keyword_group_reply_rules"] = rules
        save_publish_config(config)
        return await query.edit_message_text(
            _group_keyword_reply_rule_text(rule),
            reply_markup=_group_keyword_reply_rule_keyboard(rule),
        )

    if action == "group_keyword_reply_delete":
        try:
            rule_id = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return await query.answer("规则数据无效。", show_alert=True)
        rules = [item for item in _keyword_group_reply_rules(config) if item["id"] != rule_id]
        config["keyword_group_reply_rules"] = rules
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 已删除规则。\n\n" + _group_keyword_reply_settings_text(config),
            reply_markup=_group_keyword_reply_settings_keyboard(config),
        )

    if action == "checkin_settings":
        return await query.edit_message_text(
            _checkin_settings_text(config),
            reply_markup=_checkin_settings_keyboard(config),
        )

    if action == "toggle_checkin":
        config["checkin_enabled"] = not bool(config.get("checkin_enabled", False))
        save_publish_config(config)
        return await query.edit_message_text(
            _checkin_settings_text(config),
            reply_markup=_checkin_settings_keyboard(config),
        )

    if action in {"checkin_duration", "checkin_user_label", "checkin_display_label", "checkin_group_label", "checkin_command_text", "checkin_cancel_command_text", "checkin_online_command_text", "checkin_online_text"}:
        field_map = {
            "checkin_duration": "checkin_duration_hours",
            "checkin_user_label": "checkin_user_label",
            "checkin_display_label": "checkin_display_label",
            "checkin_group_label": "checkin_group_label",
            "checkin_command_text": "checkin_command_text",
            "checkin_cancel_command_text": "checkin_cancel_command_text",
            "checkin_online_command_text": "checkin_online_command_text",
                "checkin_online_text": "checkin_online_text",
        }
        prompt_map = {
            "checkin_duration": "请输入打卡有效时间（小时），例如：8。范围 1-168。",
            "checkin_user_label": "请输入帖子中记录可打卡用户名的字段，例如：联系。",
            "checkin_display_label": "请输入“在线宝宝”展示文字使用的字段，例如：艺名。",
            "checkin_group_label": "请输入“在线宝宝”分组字段，例如：区域 或 标签。发送“无”表示不分组。",
            "checkin_command_text": "请输入用户打卡时发送的文案，例如：打卡。",
            "checkin_cancel_command_text": "请输入用户取消打卡时发送的文案，例如：取消打卡。",
            "checkin_online_command_text": "请输入展示在线帖子时发送的文案，例如：在线宝宝。",
            "checkin_online_text": "请输入展示在线帖子时发送的文案，例如：在线宝宝。",
        }
        context.user_data["publish_checkin_setting_field"] = field_map[action]
        return await query.edit_message_text(
            prompt_map[action],
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回打卡设置", callback_data="publish:checkin_settings")]]),
        )

    if action == "keyword_search_display":
        return await query.edit_message_text(
            "🔎 设置“查找收录”显示来源：\n\n"
            "主频道：只展示关键词索引绑定的主频道帖子。\n"
            "备用频道：仅展示有主备映射的备用频道镜像帖。\n"
            "全部展示：主频道和备用频道都展示。",
            reply_markup=_keyword_post_search_display_keyboard(config),
        )

    if action == "keyword_search_display_set":
        mode = query.data.split(":", 2)[2] if len(query.data.split(":", 2)) == 3 else ""
        if mode not in KEYWORD_SEARCH_DISPLAY_OPTIONS:
            return await query.answer("展示模式无效。", show_alert=True)
        config["keyword_post_search_display_mode"] = mode
        save_publish_config(config)
        return await query.edit_message_text(
            f"✅ 已设置搜索展示：{KEYWORD_SEARCH_DISPLAY_OPTIONS[mode]}",
            reply_markup=_keyword_post_search_display_keyboard(config),
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
        main_channel_id = _as_int(config.get("channel_id"))
        backup_channel_id = _backup_channel_id(config, main_channel_id)
        text = "\n".join([
            "📢 发布频道设置",
            "",
            f"主频道：{main_channel_id if main_channel_id is not None else '未设置'}",
            f"备用频道：{backup_channel_id if backup_channel_id is not None else '未设置'}",
            f"备用同步方式：{_backup_transport_label(config) if backup_channel_id is not None else '未设置'}",
            "",
            "发布主帖时会先发到主频道，再自动镜像到备用频道。",
            "关键词搜索、评论定位和报告仍只使用主频道。",
        ])
        return await query.edit_message_text(
            text,
            reply_markup=publish_channel_keyboard(config),
        )

    if action == "set_channel":
        context.user_data["waiting_channel_id"] = True
        return await query.edit_message_text(
            "✏️ 设置主发布频道\n\n请输入主频道 ID，例如：\n-1001234567890\n\n"
            "主频道用于关键词搜索、评论定位和报告。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回频道设置", callback_data="publish:channel")]]),
        )

    if action == "set_backup_channel":
        context.user_data["waiting_backup_channel_id"] = True
        return await query.edit_message_text(
            "✏️ 设置备用频道\n\n请输入备用频道 ID，例如：\n-1001234567890\n\n"
            "之后每条主帖会自动镜像到备用频道；备用频道不会参与关键词搜索或评论定位。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回频道设置", callback_data="publish:channel")]]),
        )

    if action == "clear_backup_channel":
        config["backup_channel_id"] = None
        config["backup_channel_use_telethon"] = False
        config["backup_channel_session"] = ""
        config["backup_channel_listen_session"] = ""
        config["backup_channel_forward_session"] = ""
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 已清除备用频道。",
            reply_markup=publish_channel_keyboard(config),
        )

    if action == "backup_transport_toggle":
        if _backup_channel_id(config) is None:
            return await query.answer("请先设置备用频道。", show_alert=True)
        enabled = not _backup_channel_use_telethon(config)
        config["backup_channel_use_telethon"] = enabled
        if not enabled:
            save_publish_config(config)
            return await query.edit_message_text(
                "✅ 备用频道同步已切换为机器人发送。",
                reply_markup=publish_channel_keyboard(config),
            )
        try:
            from channel.telethon_login import _list_session_names
            sessions = _list_session_names(context, query.from_user)
        except Exception as exc:
            sessions = []
            print(f"读取备用同步协议号失败: {exc}")
        if not sessions:
            config["backup_channel_use_telethon"] = False
            save_publish_config(config)
            return await query.answer("没有可用协议号，已保持机器人发送。", show_alert=True)
        current = _backup_channel_forward_session(config)
        if current in sessions:
            if not _backup_channel_listen_session(config):
                config["backup_channel_listen_session"] = current
            save_publish_config(config)
            return await query.edit_message_text(
                f"✅ 已启用协议号同步：监听 { _backup_channel_listen_session(config) } → 转发 {current}",
                reply_markup=publish_channel_keyboard(config),
            )
        rows = [[InlineKeyboardButton(
            f"📤 使用 {session_name} 作为转发协议号",
            callback_data=f"publish:backup_transport_session_pick:forward:{session_name}",
        )] for session_name in sessions]
        rows.append([InlineKeyboardButton("⬅️ 使用机器人", callback_data="publish:backup_transport_toggle")])
        return await query.edit_message_text(
            "请选择备用频道的转发协议号。\n"
            "随后可单独设置监听主频道的协议号。",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    if action in {"backup_transport_listen_session", "backup_transport_forward_session", "backup_transport_session"}:
        if not _backup_channel_use_telethon(config):
            return await query.answer("请先开启“协议号同步”。", show_alert=True)
        role = "listen" if action == "backup_transport_listen_session" else "forward"
        try:
            from channel.telethon_login import _list_session_names
            sessions = _list_session_names(context, query.from_user)
        except Exception as exc:
            sessions = []
            print(f"读取备用同步协议号失败: {exc}")
        if not sessions:
            return await query.answer("暂无可用协议号。", show_alert=True)
        role_label = "监听主频道" if role == "listen" else "转发到备用频道"
        rows = [[InlineKeyboardButton(
            f"📱 {session_name}",
            callback_data=f"publish:backup_transport_session_pick:{role}:{session_name}",
        )] for session_name in sessions]
        rows.append([InlineKeyboardButton("⬅️ 返回频道设置", callback_data="publish:channel")])
        return await query.edit_message_text(
            f"请选择用于“{role_label}”的协议号：",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    if action == "backup_transport_session_pick":
        parts = query.data.split(":")
        # Compatibility with the previous callback format: :pick:<session>
        if len(parts) == 3:
            role, session_name = "forward", parts[2]
        elif len(parts) == 4:
            role, session_name = parts[2], parts[3]
        else:
            return await query.answer("协议号参数无效。", show_alert=True)
        if role not in {"listen", "forward"}:
            return await query.answer("协议号角色无效。", show_alert=True)
        try:
            from channel.telethon_login import _list_session_names
            sessions = _list_session_names(context, query.from_user)
        except Exception:
            sessions = []
        if not session_name or session_name not in sessions:
            return await query.answer("协议号无效或无权限。", show_alert=True)
        config["backup_channel_use_telethon"] = True
        if role == "listen":
            config["backup_channel_listen_session"] = session_name
        else:
            config["backup_channel_forward_session"] = session_name
            config["backup_channel_session"] = session_name
            if not _backup_channel_listen_session(config):
                config["backup_channel_listen_session"] = session_name
        save_publish_config(config)
        return await query.edit_message_text(
            f"✅ 已设置备用频道{('监听' if role == 'listen' else '转发')}协议号：{session_name}",
            reply_markup=publish_channel_keyboard(config),
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
            and not is_super_admin(query.from_user.id)
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
            #     f"{'✅' if enabled else '🚫'} 匿名投稿",
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
        anonymous_submission = bool(context.user_data.get("post_no_name", False))
        submission = {
            "status": "pending",
            "user_id": msg.from_user.id,
            "user_chat_id": msg.chat_id,
            "username": msg.from_user.username,
            "anonymous": anonymous_submission,
            "author": "匿名投稿" if anonymous_submission else _submission_author_text(msg),
            # Anonymous comments reveal only the sender's nickname in the
            # public report; usernames and IDs remain hidden.
            "report_author": _comment_nickname(msg) if anonymous_submission else _comment_author(msg),
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
            if (
                (comment_forward_enabled or bool(config.get("checkin_enabled", False)))
                and is_owner_submission
                and submission_kind == "main"
            )
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
        report_id = _report_id_from_subject_entries(submission.get("keyword_entries", []))
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
        if submission_kind == "main":
            await _mirror_main_post_to_backup(
                context,
                config,
                int(target_channel_id),
                published.message_id,
            )
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

    # Keep ``raw_text`` for the body.  ``text`` is only for commands and
    # validation, so leading/trailing newlines in a template are not removed.
    raw_text = msg.text
    text = raw_text.strip()
    draft = context.user_data.get(TEMPLATE_DRAFT_KEY)
    if isinstance(draft, dict):
        if text in {"取消", "返回"}:
            context.user_data.pop(TEMPLATE_DRAFT_KEY, None)
            await msg.reply_text("已取消模板编辑。")
            return True

        step = draft.get("step")
        is_edit = str(step).startswith("edit_")

        if step in {"name", "edit_name"}:
            if not text or len(text) > 50:
                await msg.reply_text("模板名称不能为空且不能超过 50 个字符。")
                return True
            draft["name"] = text
            if is_edit:
                draft["step"] = "edit_menu"
                await msg.reply_text(
                    "模板名称已更新。请选择继续修改的项目，或保存修改：",
                    reply_markup=_template_edit_keyboard(draft),
                )
            else:
                draft["step"] = "text"
                await msg.reply_text(
                    "请输入模板正文。可使用 {键名} 占位符，例如：\n"
                    "【艺名】：{艺名}\n【联系】：{联系方式}\n\n"
                    "如果消息本身带有粗体、斜体、下划线或链接，请在下一步选择 HTML 格式以保留它们。"
                )
            return True

        if step in {"text", "edit_text"}:
            if not text or len(raw_text) > 3500:
                await msg.reply_text("模板正文不能为空且不能超过 3500 个字符。")
                return True

            # ``Message.text_html`` is generated from the entities Telegram
            # received with this message.  Storing both forms lets the user
            # decide *afterwards* whether to use plain text or HTML without
            # losing the original rich formatting.
            rich_text = getattr(msg, "text_html", None)
            draft["body_plain"] = raw_text
            draft["body_html"] = rich_text if isinstance(rich_text, str) else raw_text
            draft["after_format_step"] = "edit_menu" if is_edit else "buttons"
            draft["step"] = "format"
            await msg.reply_text("请选择文本格式：", reply_markup=_template_format_keyboard())
            return True

        if step in {"buttons", "edit_buttons"}:
            if text in {"无", "跳过", "none"}:
                draft["buttons"] = []
            else:
                buttons = []
                for line in raw_text.splitlines():
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

            if is_edit:
                draft["step"] = "edit_menu"
                await msg.reply_text(
                    "文本超链接已更新。请选择继续修改的项目，或保存修改：",
                    reply_markup=_template_edit_keyboard(draft),
                )
                return True

            if draft.get("buttons"):
                draft["step"] = "button_columns"
                await msg.reply_text(
                    "每行显示几个文本超链接？请输入 1-8。\n"
                    "同一行的链接会以空格隔开，达到数量后自动换行。"
                )
                return True
            draft["button_columns"] = 1
            draft["step"] = "confirm"
            await msg.reply_text(
                _template_draft_preview(draft),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 保存模板", callback_data="publish:template_save")],
                    [InlineKeyboardButton("❌ 取消", callback_data="publish:template_cancel")],
                ]),
            )
            return True

        if step in {"button_columns", "edit_button_columns"}:
            try:
                columns = int(text)
            except (TypeError, ValueError):
                columns = 0
            if not 1 <= columns <= 8:
                await msg.reply_text("❗ 请输入 1 到 8 之间的整数。")
                return True
            draft["button_columns"] = columns
            if is_edit:
                draft["step"] = "edit_menu"
                await msg.reply_text(
                    "每行链接数已更新。请选择继续修改的项目，或保存修改：",
                    reply_markup=_template_edit_keyboard(draft),
                )
            else:
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
            config = load_publish_config()
            await msg.reply_text(
                "已取消本次模板发布，请重新选择模板：",
                reply_markup=_template_publish_keyboard(config),
            )
            return True
        keys = flow.get("keys", [])
        index = int(flow.get("index", 0) or 0)
        if index < 0 or index >= len(keys):
            context.user_data.pop(TEMPLATE_FLOW_KEY, None)
            await msg.reply_text("模板填写状态已失效，请重新选择模板。")
            return True
        flow.setdefault("values", {})[keys[index]] = raw_text
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
            parse_mode=_render_template(template, flow.get("values", {}))[2],
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
        context.user_data[COMMENT_TARGET_KEY] = {
            "channel_id": route["channel_id"],
            "message_id": route["channel_message_id"],
        }
        # Keep this consistent with the multiple-result flow: the user must
        # explicitly choose anonymous or real-name submission before input.
        context.user_data["waiting_post"] = False
        link = await _route_message_link(context, route)
        await msg.reply_text(
            f"✅ 已匹配 {route.get('label', '关键词')}：{route.get('raw', route.get('key'))}。\n"
            "请选择匿名投稿或实名投稿，选择后才会开始输入投稿内容。",
            reply_markup=_keyword_submission_mode_keyboard(route, link),
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

async def _handle_group_keyword_reply_settings_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    draft = (context.user_data or {}).get(GROUP_KEYWORD_REPLY_STAGE_KEY)
    if not isinstance(draft, dict):
        return False
    msg = update.message
    if not msg or not msg.text:
        return True
    if not update.effective_user or not has_admin_permission(
        context, update.effective_user.id, "submission_config"
    ):
        context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
        return False

    text = msg.text.strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
        config = load_publish_config()
        await msg.reply_text(
            "已取消群关键词回复规则配置。",
            reply_markup=_group_keyword_reply_settings_keyboard(config),
        )
        return True

    step = str(draft.get("step") or "")
    config = load_publish_config()

    if draft.get("mode") == "field" and step == "field":
        field = str(draft.get("field") or "")
        rule_id = _as_int(draft.get("id"))
        rules = _keyword_group_reply_rules(config)
        rule = next((item for item in rules if item["id"] == rule_id), None)
        if not rule:
            context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
            await msg.reply_text("❗ 规则不存在或已删除。")
            return True
        if field in {"input_label", "reply_label"}:
            if not text or len(text) > 30:
                await msg.reply_text("❗ 字段不能为空，且不能超过 30 个字符。")
                return True
            rule[field] = text
        elif field == "display_text":
            if text == "默认":
                rule[field] = f"{rule.get('reply_label') or '联系方式'}：{{value}}"
            elif not text or len(text) > 500:
                await msg.reply_text("❗ 显示文案不能为空，且不能超过 500 个字符。")
                return True
            else:
                rule[field] = msg.text[:500]
        elif field == "default_text":
            rule[field] = "" if text in {"无", "-"} else msg.text[:500]
        elif field == "button_text":
            value = "联系 {value}" if text in {"默认", "-"} else text
            if not value or len(value) > 64:
                await msg.reply_text("❗ 按钮文案不能为空，且不能超过 64 个字符。")
                return True
            rule[field] = value
        else:
            context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
            return False

        config["keyword_group_reply_rules"] = rules
        save_publish_config(config)
        context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
        await msg.reply_text(
            "✅ 字段已更新。\n\n" + _group_keyword_reply_rule_text(rule),
            reply_markup=_group_keyword_reply_rule_keyboard(rule),
        )
        return True

    if step == "input_label":
        if not text or len(text) > 30:
            await msg.reply_text("❗ 输入关键词字段不能为空，且不能超过 30 个字符。")
            return True
        draft["input_label"] = text
        draft["step"] = "reply_label"
        await msg.reply_text(
            "请输入回复关键词字段名，例如：联系方式。\n"
            "注意：该字段必须同时在“关键词设置”的提取标签中配置。"
        )
        return True

    if step == "reply_label":
        if not text or len(text) > 30:
            await msg.reply_text("❗ 回复关键词字段不能为空，且不能超过 30 个字符。")
            return True
        draft["reply_label"] = text
        draft["step"] = "display_text"
        await msg.reply_text(
            "请输入机器人在群里回复用户的显示文案，例如：联系方式：{value}。\n"
            "可使用 {keyword}（用户输入）、{value}（第一个联系方式）、{values}（全部联系方式）。"
        )
        return True

    if step == "display_text":
        if not text or len(text) > 500:
            await msg.reply_text("❗ 显示文案不能为空，且不能超过 500 个字符。")
            return True
        draft["display_text"] = msg.text[:500]
        draft["step"] = "default_text"
        await msg.reply_text(
            "请输入用户点击联系方式后，打开对方私聊时自动带入的默认文字。\n"
            "可使用 {keyword}、{value}、{contacts} 占位符；发送“无”可跳过。"
        )
        return True

    if step == "default_text":
        draft["default_text"] = "" if text in {"无", "-"} else msg.text[:500]
        draft["step"] = "button_text"
        await msg.reply_text(
            "请输入联系方式按钮文案，例如：联系 {value}。\n"
            "可使用 {value}（@用户名）和 {keyword}（用户输入的艺名）；发送“默认”使用“联系 {value}”。"
        )
        return True

    if step == "button_text":
        button_text = "联系 {value}" if text in {"默认", "-"} else text
        if not button_text or len(button_text) > 64:
            await msg.reply_text("❗ 按钮文案不能为空，且不能超过 64 个字符。")
            return True
        draft["button_text"] = button_text
        rule = {
            "id": int(draft["id"]),
            "input_label": str(draft["input_label"]),
            "reply_label": str(draft["reply_label"]),
            "display_text": str(draft.get("display_text") or "{value}"),
            "default_text": str(draft.get("default_text") or ""),
            "button_text": button_text,
            "enabled": bool(draft.get("enabled", True)),
        }
        rules = [item for item in _keyword_group_reply_rules(config) if item["id"] != rule["id"]]
        rules.append(rule)
        rules.sort(key=lambda item: item["id"])
        config["keyword_group_reply_rules"] = rules
        save_publish_config(config)
        context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
        await msg.reply_text(
            "✅ 群关键词回复规则已保存。\n\n" + _group_keyword_reply_rule_text(rule),
            reply_markup=_group_keyword_reply_rule_keyboard(rule),
        )
        return True

    context.user_data.pop(GROUP_KEYWORD_REPLY_STAGE_KEY, None)
    return False

async def _group_keyword_reply_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_group_keyword_reply(update, context):
        raise ApplicationHandlerStop


async def _keyword_search_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Give active keyword search first chance to consume private text.

    If there is no active search/template flow, processing returns normally and
    the bidirectional private-forward handlers can continue.
    """
    if await _handle_checkin_settings_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_report_username_migration_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_group_keyword_reply_settings_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_template_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_keyword_search_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_keyword_post_search_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_username_report_lookup(update, context):
        raise ApplicationHandlerStop


async def _handle_checkin_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("publish_checkin_setting_field"):
        field = context.user_data.pop("publish_checkin_setting_field")
        if not update.message or not update.message.text:
            return
        value = update.message.text.strip()
        config = load_publish_config()
        if value in {"取消", "返回"}:
            return await update.message.reply_text(
                "已取消打卡设置修改。",
                reply_markup=_checkin_settings_keyboard(config),
            )
        if field == "checkin_duration_hours":
            try:
                hours = int(value)
            except ValueError:
                hours = 0
            if not 1 <= hours <= 168:
                return await update.message.reply_text("❗ 请输入 1 到 168 之间的小时数。")
            config[field] = hours
        elif field in {"checkin_user_label", "checkin_display_label"}:
            if not value or len(value) > 30:
                return await update.message.reply_text("❗ 字段不能为空且不能超过 30 个字符。")
            config[field] = value
        elif field == "checkin_group_label":
            config[field] = "" if value in {"无", "-"} else value[:30]
        elif field in {"checkin_command_text", "checkin_cancel_command_text", "checkin_online_command_text", "checkin_online_text"}:
            if not value or len(value) > 30:
                return await update.message.reply_text("❗ 文案不能为空且不能超过 30 个字符。")
            other_values = {
                _checkin_command_text(config, "checkin_command_text", "打卡"),
                _checkin_command_text(config, "checkin_cancel_command_text", "取消打卡"),
                _checkin_command_text(config, "checkin_online_command_text", "在线宝宝"),
                _checkin_command_text(config, "checkin_online_text", "在线宝宝"),
            }
            other_values.discard(_checkin_command_text(config, field, ""))
            if value in other_values:
                return await update.message.reply_text("❗ 三个打卡文案不能重复，请换一个。")
            config[field] = value
        else:
            return
        save_publish_config(config)
        return await update.message.reply_text(
            "✅ 已保存打卡设置。\n\n" + _checkin_settings_text(config),
            reply_markup=_checkin_settings_keyboard(config),
        )
    return False


async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_checkin_settings_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_report_username_migration_input(update, context):
        raise ApplicationHandlerStop

    if await _handle_group_keyword_reply_settings_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_template_input(update, context):
        raise ApplicationHandlerStop
    if await _handle_keyword_search_input(update, context):
        # Stop group=999 message_router and any later handler from sending the
        # search text into the bidirectional/private bot flow.
        raise ApplicationHandlerStop
    if await _handle_keyword_post_search_input(update, context):
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
            for value in re.split(r"[,，、\n.。]+", update.message.text or "")
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

    if context.user_data.get("waiting_backup_channel_id"):
        context.user_data["waiting_backup_channel_id"] = False
        try:
            backup_channel_id = int(update.message.text.strip())
        except (TypeError, ValueError):
            return await update.message.reply_text("❗ 频道 ID 格式错误，例如：-1001234567890")
        main_channel_id = _as_int(config.get("channel_id"))
        if main_channel_id is not None and backup_channel_id == main_channel_id:
            return await update.message.reply_text("❗ 备用频道不能与主频道相同，请重新输入。")
        config["backup_channel_id"] = backup_channel_id
        save_publish_config(config)
        return await update.message.reply_text(
            f"✅ 已保存备用频道：{backup_channel_id}\n主帖将同步镜像到该频道。",
            reply_markup=publish_channel_keyboard(config),
        )

    if context.user_data.get("waiting_channel_id"):
        context.user_data["waiting_channel_id"] = False
        try:
            new_channel_id = int(update.message.text.strip())
        except (TypeError, ValueError):
            return await update.message.reply_text(
                "❗ 频道 ID 格式错误，例如：-1001234567890"
            )

        old_channel_id = _as_int(config.get("channel_id"))
        _record_publish_channel_change(config, old_channel_id, new_channel_id)
        config["channel_id"] = new_channel_id
        save_publish_config(config)

        note = ""
        if old_channel_id is not None and old_channel_id != new_channel_id:
            note = (
                f"\n已记录频道变更：{old_channel_id} → {new_channel_id}。"
                "之后可自动关联已克隆历史主帖。"
            )
        return await update.message.reply_text(
            f"✅ 已保存频道：{new_channel_id}{note}"
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
    # Check-in commands run before generic group dispatch/AI handlers.
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & (~filters.COMMAND)
            & ~filters.UpdateType.BUSINESS_MESSAGE,
            _checkin_group_interceptor,
        ),
        group=-21,
    )
    # Group keyword replies run before generic group dispatch/AI handlers.
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & (~filters.COMMAND)
            & ~filters.UpdateType.BUSINESS_MESSAGE,
            _group_keyword_reply_interceptor,
        ),
        group=-20,
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
