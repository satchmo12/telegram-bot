import asyncio
import re
from typing import Optional
from datetime import datetime
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
import telegram
from telegram.ext import MessageHandler, ContextTypes, filters
from channel.access_control import is_channel_subscription_required
from utils import load_json, is_super_admin

MEDIA_GROUP_CACHE = {}
MEDIA_GROUP_TASKS = {}
MEDIA_GROUP_WAIT_SECONDS = 5
RATE_LIMIT_MIN_INTERVAL_SEC = 1.0
RATE_LIMIT_MAX_RETRY = 3
TARGET_LOCKS = {}
TARGET_LAST_TS = {}

SUBSCRIPTION_FILE = "config_data/subscriptions.json"

LINK_RE = re.compile(r"(?i)(https?://[^\s)\]}>]+|t\.me/[^\s)\]}>]+|www\.[^\s)\]}>]+)")


def _entity_urls(entities) -> list:
    urls = []
    for ent in entities or []:
        url = getattr(ent, "url", None)
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def _has_link(text: str, entities=None) -> bool:
    if entities:
        for url in _entity_urls(entities):
            if url:
                return True
    if not text:
        return False
    if LINK_RE.search(text):
        return True
    return False


def _should_skip_by_links(rule: dict, text: str, entities=None) -> bool:
    if rule.get("skip_links"):
        return _has_link(text or "", entities)
    return False


def _join_with_suffix(text: str, suffix: str) -> str:
    base = text or ""
    extra = suffix or ""
    if not extra:
        return base
    if not base:
        return extra.strip("\n")
    base = re.sub(r"\n+\Z", "", base)
    extra = re.sub(r"\A\n+", "", extra)
    return f"{base}\n\n{extra}"


def _should_skip_by_words(rule: dict, text: str) -> bool:
    t = text or ""
    include_words = rule.get("include_words") or []
    block_words = rule.get("block_words") or []
    if include_words:
        if not any(w and w in t for w in include_words):
            return True
    if block_words:
        if any(w and w in t for w in block_words):
            return True
    return False


def _is_active_subscription(user_id: str, username: Optional[str] = None) -> bool:
    if not user_id:
        return False
    data = load_json(SUBSCRIPTION_FILE)
    if not isinstance(data, dict):
        return False
    record = data.get("users", {}).get(str(user_id))
    if not isinstance(record, dict) and username:
        record = data.get("usernames", {}).get(str(username).lstrip("@").lower())
    if not isinstance(record, dict):
        return False
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d").date()
        return exp >= datetime.now().date()
    except Exception:
        return False

def _clear_links(text: str) -> str:
    if not text:
        return text
    t = text
    t = re.sub(r"(?i)https?://\S+", "", t)
    t = re.sub(r"(?i)t\.me/\S+", "", t)
    t = re.sub(r"@[\w_]{3,}", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _apply_replace(text: str, pairs) -> str:
    if not text or not pairs:
        return text
    out = text
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        src = str(pair.get("from", "") or "")
        dst = str(pair.get("to", "") or "")
        if not src:
            continue
        out = out.replace(src, dst)
    return out


def _apply_cut(text: str, cut_rule) -> str:
    if not text or not cut_rule:
        return text
    rules = [cut_rule] if isinstance(cut_rule, str) else [r for r in (cut_rule or []) if r]
    out = text
    for rule in rules:
        if "|" in rule:
            start, end = rule.split("|", 1)
        else:
            start, end = "", rule
        if start:
            idx = out.find(start)
            if idx >= 0:
                out = out[idx + len(start) :]
        if end:
            idx = out.find(end)
            if idx >= 0:
                out = out[:idx]
    return out


def _process_text(text: str, rule: dict):
    if text is None:
        return text
    t = text
    include_words = rule.get("include_words") or []
    block_words = rule.get("block_words") or []
    if include_words:
        if not any(w and w in t for w in include_words):
            return ""
    if block_words:
        if any(w and w in t for w in block_words):
            return ""
    t = _apply_replace(t, rule.get("replace_words") or [])
    t = _apply_cut(t, rule.get("cut_words", ""))
    if rule.get("clear_links"):
        t = _clear_links(t)
    suffix = str(rule.get("suffix", "") or "")
    if suffix:
        t = _join_with_suffix(t, suffix)
    if (
        t == ""
        and not include_words
        and not block_words
        and not rule.get("clear_links")
        and not rule.get("cut_words")
        and not rule.get("replace_words")
        and not suffix
    ):
        return None
    return t


def _processed_text_or_original(text: str, rule: dict):
    processed = _process_text(text, rule)
    if processed is None:
        return text
    return processed

def _get_media_group_key(msg, rule_idx: int) -> tuple[int, str, int]:
    # media_group_id 在不同 chat 可能重复，组合 chat_id 更稳妥
    # 追加 rule_idx，避免同一消息命中多条规则时任务互相覆盖
    return (msg.chat.id, str(msg.media_group_id), int(rule_idx))


def _get_source_id_from_msg(msg) -> str:
    try:
        return (
            getattr(getattr(msg, "sender_chat", None), "id", None)
            or getattr(
                getattr(getattr(msg, "forward_origin", None), "chat", None),
                "id",
                None,
            )
            or "unknown"
        )
    except Exception:
        return "unknown"


def _get_target_lock(target_id: int) -> asyncio.Lock:
    lock = TARGET_LOCKS.get(target_id)
    if lock is None:
        lock = asyncio.Lock()
        TARGET_LOCKS[target_id] = lock
    return lock


def _media_group_should_skip(group_msgs, rule: dict) -> bool:
    for msg in group_msgs or []:
        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        entities = getattr(msg, "entities", None) if getattr(msg, "text", None) else getattr(msg, "caption_entities", None)
        if _should_skip_by_words(rule, text):
            return True
        if _should_skip_by_links(rule, text, entities):
            return True
    return False


async def _send_with_retry(
    target_id: int, send_coro, *, kind: str, src: str
):
    lock = _get_target_lock(target_id)
    async with lock:
        for attempt in range(1, RATE_LIMIT_MAX_RETRY + 1):
            try:
                now = asyncio.get_event_loop().time()
                last = TARGET_LAST_TS.get(target_id, 0)
                wait = RATE_LIMIT_MIN_INTERVAL_SEC - (now - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                TARGET_LAST_TS[target_id] = asyncio.get_event_loop().time()
                return await send_coro()
            except telegram.error.RetryAfter as e:
                delay = max(1, int(getattr(e, "retry_after", 1)))
                await asyncio.sleep(delay)
            except telegram.error.TimedOut:
                await asyncio.sleep(1)
            except Exception as e:
                print(f"⚠️ {kind}搬运失败: {e} (来源: {src} → 目标: {target_id})")
                return None


async def process_media_group(
    group_msgs, targets, rule, context: ContextTypes.DEFAULT_TYPE
):
    """合并发送 MediaGroup"""
    group_msgs = sorted(group_msgs, key=lambda x: x.message_id or 0)

    # 提取文字，只取第一条消息中的 text 或 caption
    main_text = None
    for msg in group_msgs:
        text = getattr(msg, "text", None) or getattr(msg, "caption", None)
        if text:
            main_text = _processed_text_or_original(text, rule)
            break

    media_list = []
    for idx, msg in enumerate(group_msgs):
        caption = main_text if idx == 0 else None
        if msg.photo:
            media_list.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption))
        elif msg.video:
            media_list.append(InputMediaVideo(media=msg.video.file_id, caption=caption))
        elif msg.document and getattr(msg.document, "mime_type", "") == "image/gif":
            media_list.append(InputMediaDocument(media=msg.document.file_id, caption=caption))

    src = _get_source_id_from_msg(group_msgs[0]) if group_msgs else "unknown"
    for target_id in targets:
        if not media_list:
            continue
        await _send_with_retry(
            target_id,
            lambda: context.bot.send_media_group(chat_id=target_id, media=media_list),
            kind="MediaGroup ",
            src=str(src),
        )


async def _flush_media_group(
    group_key, targets, rule, context: ContextTypes.DEFAULT_TYPE
):
    try:
        await asyncio.sleep(MEDIA_GROUP_WAIT_SECONDS)
        group_msgs = MEDIA_GROUP_CACHE.pop(group_key, [])
        if group_msgs:
            if _media_group_should_skip(group_msgs, rule):
                return
            await process_media_group(group_msgs, targets, rule, context)
    except asyncio.CancelledError:
        return
    finally:
        MEDIA_GROUP_TASKS.pop(group_key, None)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not getattr(msg, "is_automatic_forward", False):
        return

    source_channel_ids = set()
    sender_chat = getattr(msg, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "type", None) and sender_chat.type.name == "CHANNEL":
        source_channel_ids.add(sender_chat.id)

    forward_origin = getattr(msg, "forward_origin", None)
    origin_chat = getattr(forward_origin, "chat", None) if forward_origin else None
    if origin_chat and getattr(origin_chat, "type", None) and origin_chat.type.name == "CHANNEL":
        source_channel_ids.add(origin_chat.id)

    # 自动转发消息至少要识别出一个来源频道（中转频道或原始频道）
    if not source_channel_ids:
        return

    config = load_json("config_data/forward_config.json")
    base_rules = config.get("forward_rules", []) if isinstance(config, dict) else []
    forward_rules = list(base_rules) if isinstance(base_rules, list) else []

    user_config = load_json("config_data/forward_config_users_bot.json")
    if isinstance(user_config, dict):
        users = user_config.get("users", {})
        if isinstance(users, dict):
            for user_id, ucfg in users.items():
                if not isinstance(ucfg, dict):
                    continue
                rules = ucfg.get("forward_rules")
                guess_username = ucfg.get("username", "")
                if not guess_username and isinstance(rules, list) and rules:
                    raw = str(rules[0].get("replace_submit_user", "") or "")
                    guess_username = raw.lstrip("@")
                if is_channel_subscription_required() and not (
                    is_super_admin(user_id)
                    or _is_active_subscription(user_id, guess_username)
                ):
                    continue
                if isinstance(rules, list):
                    forward_rules.extend(list(rules))

    # 去重：同一来源/目标/类型的规则优先保留后加入的（订阅用户覆盖默认）
    def _rule_key(rule: dict) -> tuple:
        sources = tuple(rule.get("sources", []) or [])
        targets = tuple(rule.get("targets", []) or [])
        ftype = str(rule.get("filter", "all") or "all").lower()
        mode = str(rule.get("mode", "listen") or "listen").lower()
        return (sources, targets, ftype, mode)

    deduped = {}
    for rule in forward_rules:
        deduped[_rule_key(rule)] = rule
    forward_rules = list(deduped.values())

    for idx, rule in enumerate(forward_rules):
        if not bool(rule.get("enabled", True)):
            continue
        mode = str(rule.get("mode", "listen") or "listen").lower()
        if mode != "listen":
            continue
        sources = set(rule.get("sources", []))
        exclude_channels = set(rule.get("exclude_channels", []))
        filter_type = str(rule.get("filter", "all") or "all").lower()
        if filter_type not in {"all", "text", "photo", "video"}:
            filter_type = "all"

        if sources and source_channel_ids.isdisjoint(sources):
            continue
        if exclude_channels and not source_channel_ids.isdisjoint(exclude_channels):
            continue
        if msg.reply_to_message:
            continue

        # 搬运类型过滤
        has_gif = bool(getattr(msg, "animation", None)) or (
            getattr(msg, "document", None) and getattr(msg.document, "mime_type", "") == "image/gif"
        )
        if filter_type == "text" and (
            msg.photo or msg.video or has_gif or getattr(msg, "media_group_id", None)
        ):
            continue
        if filter_type == "photo" and not (msg.photo or has_gif):
            continue
        if filter_type == "video" and not (msg.video or has_gif):
            continue

        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        entities = getattr(msg, "entities", None) if getattr(msg, "text", None) else getattr(msg, "caption_entities", None)
        if _should_skip_by_words(rule, text):
            continue
        if _should_skip_by_links(rule, text, entities):
            continue

        print(f"📥 监听到频道消息: source={list(source_channel_ids)} rule={rule.get('name','')} targets={rule.get('targets', [])}")

        # === MediaGroup 处理 ===
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            group_key = _get_media_group_key(msg, idx)
            MEDIA_GROUP_CACHE.setdefault(group_key, []).append(msg)

            # 防抖：同组每来一条都重置计时，最终只发送一次
            task = MEDIA_GROUP_TASKS.get(group_key)
            if task and not task.done():
                task.cancel()
            MEDIA_GROUP_TASKS[group_key] = asyncio.create_task(
                _flush_media_group(group_key, rule.get("targets", []), rule, context)
                )
            continue

        # === 单条消息处理 ===
        targets = rule.get("targets", [])
        src = _get_source_id_from_msg(msg)
        if msg.photo:
            caption = _processed_text_or_original(msg.caption or "", rule) if msg.caption else None
            for target_id in targets:
                await _send_with_retry(
                    target_id,
                    lambda: context.bot.send_photo(
                        chat_id=target_id,
                        photo=msg.photo[-1].file_id,
                        caption=caption,
                    ),
                    kind="单张图片",
                    src=str(src),
                )
            continue


async def handle_user_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # 用户自己转发：排除频道自动转发
    if getattr(msg, "is_automatic_forward", False):
        return

    source_channel_ids = set()
    sender_chat = getattr(msg, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "type", None) and sender_chat.type.name == "CHANNEL":
        source_channel_ids.add(sender_chat.id)

    forward_origin = getattr(msg, "forward_origin", None)
    origin_chat = getattr(forward_origin, "chat", None) if forward_origin else None
    if origin_chat and getattr(origin_chat, "type", None) and origin_chat.type.name == "CHANNEL":
        source_channel_ids.add(origin_chat.id)

    if not source_channel_ids:
        return

    config = load_json("config_data/forward_config.json")
    base_rules = config.get("forward_rules", []) if isinstance(config, dict) else []
    forward_rules = list(base_rules) if isinstance(base_rules, list) else []

    user_config = load_json("config_data/forward_config_users_bot.json")
    if isinstance(user_config, dict):
        users = user_config.get("users", {})
        if isinstance(users, dict):
            for user_id, ucfg in users.items():
                if not isinstance(ucfg, dict):
                    continue
                rules = ucfg.get("forward_rules")
                guess_username = ucfg.get("username", "")
                if not guess_username and isinstance(rules, list) and rules:
                    raw = str(rules[0].get("replace_submit_user", "") or "")
                    guess_username = raw.lstrip("@")
                if is_channel_subscription_required() and not (
                    is_super_admin(user_id)
                    or _is_active_subscription(user_id, guess_username)
                ):
                    continue
                if isinstance(rules, list):
                    forward_rules.extend(list(rules))

    def _rule_key(rule: dict) -> tuple:
        sources = tuple(rule.get("sources", []) or [])
        targets = tuple(rule.get("targets", []) or [])
        ftype = str(rule.get("filter", "all") or "all").lower()
        mode = str(rule.get("mode", "listen") or "listen").lower()
        return (sources, targets, ftype, mode)

    deduped = {}
    for rule in forward_rules:
        deduped[_rule_key(rule)] = rule
    forward_rules = list(deduped.values())

    for rule in forward_rules:
        if not bool(rule.get("enabled", True)):
            continue
        mode = str(rule.get("mode", "listen") or "listen").lower()
        if mode != "listen":
            continue
        sources = set(rule.get("sources", []))
        exclude_channels = set(rule.get("exclude_channels", []))
        filter_type = str(rule.get("filter", "all") or "all").lower()
        if filter_type not in {"all", "text", "photo", "video"}:
            filter_type = "all"

        if sources and source_channel_ids.isdisjoint(sources):
            continue
        if exclude_channels and not source_channel_ids.isdisjoint(exclude_channels):
            continue
        if msg.reply_to_message:
            continue

        has_gif = bool(getattr(msg, "animation", None)) or (
            getattr(msg, "document", None) and getattr(msg.document, "mime_type", "") == "image/gif"
        )
        if filter_type == "text" and (
            msg.photo or msg.video or has_gif or getattr(msg, "media_group_id", None)
        ):
            continue
        if filter_type == "photo" and not (msg.photo or has_gif):
            continue
        if filter_type == "video" and not (msg.video or has_gif):
            continue

        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        entities = getattr(msg, "entities", None) if getattr(msg, "text", None) else getattr(msg, "caption_entities", None)
        if _should_skip_by_words(rule, text):
            continue
        if _should_skip_by_links(rule, text, entities):
            continue

        targets = rule.get("targets", [])
        src = _get_source_id_from_msg(msg)
        for target_id in targets:
            await _send_with_retry(
                target_id,
                lambda: context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                ),
                kind="用户转发",
                src=str(src),
            )

# 注册
def register_handle_message_handlers(app):
    app.add_handler(MessageHandler(filters.ALL, handle_message))
