import asyncio
import re
from typing import Optional
from datetime import datetime
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
import telegram
from telegram.ext import MessageHandler, ContextTypes, filters
from utils import load_json, is_super_admin

MEDIA_GROUP_CACHE = {}
MEDIA_GROUP_TASKS = {}
MEDIA_GROUP_WAIT_SECONDS = 1.2
RATE_LIMIT_MIN_INTERVAL_SEC = 1.0
RATE_LIMIT_MAX_RETRY = 3
TARGET_LOCKS = {}
TARGET_LAST_TS = {}

SUBSCRIPTION_FILE = "data/subscriptions.json"


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

def replace_links_and_submit(text: str, rule: dict) -> str:
    if not text:
        return text

    show_contact = rule.get("show_contact", True)

    replace_link = str(rule.get("replace_channel_user", "")).strip()
    replace_channel_user = str(rule.get("replace_group_name", "")).strip()
    replace_user = str(rule.get("replace_submit_user", "")).strip()

    original_text = text
    # 若固定宣传文案不是独立行，前置一个换行以分隔正文
    # “关注”前留一空行，且确保“关注”后不被拆行
    text = re.sub(r"关注\s*\n+", "关注", text)
    text = re.sub(r"^(关注)", r"\n\1", text)
    text = re.sub(r"(?<!^)(?<!\n)(关注\s*东南亚新闻大事件频道|东南亚新闻大事件频道)", r"\n\1", text)
    if replace_link:
        if not replace_link.startswith("@"):
            # 统一为 t.me/xxx 或 t.me/xxx/123 形式，兼容带 http(s) 的原链接
            replace_link = re.sub(r"^https?://", "", replace_link, flags=re.I).rstrip("/")
            # 匹配任意 Telegram 链接（含 t.me/+邀请码、joinchat、深层路径）
            # 例如:
            # - https://t.me/abc
            # - t.me/abc/123
            # - https://t.me/+AbCdEf
            # - https://t.me/joinchat/xxxx
            text = re.sub(
                r"(?i)(?:https?://)?t\.me/[^\s)\]}>]+",
                f"https://{replace_link}",
                text,
            )

    if replace_user:
        if not replace_user.startswith("@"):
            replace_user = f"@{replace_user}"

        # 先兼容旧占位词，再替换常见“投稿人”匿名名
        text = text.replace("@原投稿人", replace_user)
        text = re.sub(r"@投稿人\b", replace_user, text)
        # 替换“投稿/爆料/澄清”等标签后面的真实用户名
        text = re.sub(
            r"((?:投稿|爆料|澄清|联系|商务)[^:\n：]{0,20}[：:]\s*)@[A-Za-z0-9_]{3,}",
            rf"\1{replace_user}",
            text,
            flags=re.I,
        )

    if replace_channel_user:
        if not replace_channel_user.startswith("@"):
            replace_channel_user = f"@{replace_channel_user}"
        # 替换“聊天/交友群”等标签后的用户名
        text = re.sub(
            r"((?:聊天交友群|聊天群|交友群|交流群|群)[^\n]{0,20}?)[@＠]\s*[A-Za-z0-9_]{3,}",
            rf"\1{replace_channel_user}",
            text,
            flags=re.I,
        )

    if replace_link:
        # 如果 replace_link 本身是 @用户名，用在“关注...频道”标签后
        if replace_link.startswith("@"):
            text = re.sub(
                r"((?:关注[^\n]{0,40}?频道|订阅[^\n]{0,40}?频道)[^\n]{0,20}?)[@＠]\s*[A-Za-z0-9_]{3,}",
                rf"\1{replace_link}",
                text,
                flags=re.I,
            )
            # 兜底：直接匹配“东南亚新闻大事件频道”后的 @用户名
            text = re.sub(
                r"(东南亚新闻大事件频道[^\n]{0,20}?)[@＠]\s*[A-Za-z0-9_]{3,}",
                rf"\1{replace_link}",
                text,
                flags=re.I,
            )

    # 规范空格与分隔符，避免“频道名没有更改且没有空格”的情况
    text = re.sub(
        r"(频道[^\n]{0,40}?)(?:：|:|➡️|➡)?\s*(@[A-Za-z0-9_]{3,})",
        r"\1➡️  \2",
        text,
    )
    text = re.sub(
        r"((?:东南亚讨论群|东南亚聊天交友群|聊天交友群|聊天群|交友群|交流群)[^\n]{0,20}?@[A-Za-z0-9_]{3,})\s*投稿曝光",
        r"\1 投稿曝光",
        text,
    )

    if not show_contact:
        # 去除联系方式相关内容：从出现联系方式提示的行开始，截断后续
        contact_line_re = re.compile(
            r"(关注|订阅).{0,10}频道|频道➡️|聊天交友群|讨论群|投稿|爆料|澄清|联系|商务|便民信息|互助群|二手群|TG中文包|签证查询",
            re.I,
        )
        lines = text.splitlines()
        cutoff = None
        for idx, line in enumerate(lines):
            if contact_line_re.search(line):
                cutoff = idx
                break
        if cutoff is not None:
            lines = lines[:cutoff]
        text = "\n".join([ln for ln in lines if ln.strip()]).strip()
    return text

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
            main_text = replace_links_and_submit(text, rule)
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

    config = load_json("data/forward_config.json")
    base_rules = config.get("forward_rules", []) if isinstance(config, dict) else []
    forward_rules = list(base_rules) if isinstance(base_rules, list) else []

    user_config = load_json("data/forward_config_users.json")
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
                if not (is_super_admin(user_id) or _is_active_subscription(user_id, guess_username)):
                    continue
                if isinstance(rules, list):
                    forward_rules.extend(list(rules))

    # 去重：同一来源/目标/类型的规则优先保留后加入的（订阅用户覆盖默认）
    def _rule_key(rule: dict) -> tuple:
        sources = tuple(rule.get("sources", []) or [])
        targets = tuple(rule.get("targets", []) or [])
        ftype = str(rule.get("filter", "all") or "all").lower()
        return (sources, targets, ftype)

    deduped = {}
    for rule in forward_rules:
        deduped[_rule_key(rule)] = rule
    forward_rules = list(deduped.values())

    for idx, rule in enumerate(forward_rules):
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
        text = getattr(msg, "text", None) or getattr(msg, "caption", None)
        src = _get_source_id_from_msg(msg)
        if msg.photo:
            caption = replace_links_and_submit(msg.caption or "", rule) if msg.caption else None
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
        elif msg.animation:
            caption = replace_links_and_submit(msg.caption or "", rule) if msg.caption else None
            for target_id in targets:
                await _send_with_retry(
                    target_id,
                    lambda: context.bot.send_animation(
                        chat_id=target_id,
                        animation=msg.animation.file_id,
                        caption=caption,
                    ),
                    kind="GIF 动图",
                    src=str(src),
                )
            continue
        elif msg.document and getattr(msg.document, "mime_type", "") == "image/gif":
            caption = replace_links_and_submit(msg.caption or "", rule) if msg.caption else None
            for target_id in targets:
                await _send_with_retry(
                    target_id,
                    lambda: context.bot.send_document(
                        chat_id=target_id,
                        document=msg.document.file_id,
                        caption=caption,
                    ),
                    kind="GIF 文档",
                    src=str(src),
                )
            continue
        elif msg.video:
            caption = replace_links_and_submit(msg.caption or "", rule) if msg.caption else None
            for target_id in targets:
                await _send_with_retry(
                    target_id,
                    lambda: context.bot.send_video(
                        chat_id=target_id,
                        video=msg.video.file_id,
                        caption=caption,
                    ),
                    kind="单条视频",
                    src=str(src),
                )
            continue
        elif text:
            text = replace_links_and_submit(text, rule)
            for target_id in targets:
                await _send_with_retry(
                    target_id,
                    lambda: context.bot.send_message(chat_id=target_id, text=text),
                    kind="单条文本",
                    src=str(src),
                )
            continue

# 注册
def register_handle_message_handlers(app):
    app.add_handler(MessageHandler(filters.ALL, handle_message))
