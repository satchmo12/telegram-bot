import asyncio
import json
import os
import re
import tempfile
from telegram import Update
import telegram
from telegram.ext import ContextTypes
from utils import (
    AD_KEYWORDS_FILE,
    delete_later,
    get_bot_path,
    get_group_whitelist,
    group_allowed,
    is_admin,
    is_bot_admin,
    load_json,
    safe_reply,
    save_json,
    WHITELIST_FILE,
)

# 正则匹配 URL
URL_PATTERN = re.compile(r"(https?://\S+|t\.me/\S+|bit\.ly/\S+)", re.IGNORECASE)

TELEGRAM_LINK_PATTERN = re.compile(
    r"(https?://)?(t\.me|telegram\.me)/\S+", re.IGNORECASE
)

ZODIAC = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
# 生肖判断,防六合彩
# def contains_zodiac_ad(text: str) -> bool:
#     if not text:
#         return False

#     pattern = rf"[{re.escape(ZODIAC)}](?:\s*[{re.escape(ZODIAC)}]){{2,}}"
#     return bool(re.search(pattern, text))
# 生肖判断，防六合彩
def contains_zodiac_ad(text: str) -> bool:
    if not text:
        return False

    count = sum(1 for char in text if char in ZODIAC)
    return count >= 3

# 配置文件由 utils.load_json 缓存，但旧实现仍会在每条消息上重新清洗、排序
# 所有群的关键词和白名单。这里按底层缓存对象复用标准化结果。
_AD_KEYWORDS_CACHE = {}
_WHITELIST_CACHE = {}
_LOWER_GROUP_KEYWORDS_CACHE = {}


def _drop_path_caches(path: str):
    _AD_KEYWORDS_CACHE.pop(path, None)
    _WHITELIST_CACHE.pop(path, None)
    for key in list(_LOWER_GROUP_KEYWORDS_CACHE):
        if key[0] == path:
            _LOWER_GROUP_KEYWORDS_CACHE.pop(key, None)


async def _ensure_ad_command_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat or (chat.type or "").lower() not in {"group", "supergroup"}:
        await safe_reply(update, context, "❌ 该命令只能在群组中使用。")
        return False

    chat_id = str(chat.id)
    group_config = get_group_whitelist(context).get(chat_id)
    if not isinstance(group_config, dict) or not group_config.get("enabled", True):
        await safe_reply(update, context, "⚠️ 本群主功能未开启，无法使用广告词命令。")
        return False

    if not await is_admin(update, context):
        await safe_reply(update, context, "❌ 仅群管理员或超级管理员可操作。")
        return False

    return True


def get_ad_keywords(context: ContextTypes.DEFAULT_TYPE):
    """兼容旧结构并按群读取广告关键词。"""
    path = get_bot_path(context, AD_KEYWORDS_FILE)
    raw = load_json(path)
    cached = _AD_KEYWORDS_CACHE.get(path)
    if cached and cached[0] == id(raw):
        return cached[1]

    # 旧结构：直接是 list（全局共享）
    if isinstance(raw, list):
        cleaned = sorted(
            {
                kw.strip()
                for kw in raw
                if isinstance(kw, str) and kw.strip()
            }
        )
        data = {"__legacy__": cleaned}
        _AD_KEYWORDS_CACHE[path] = (id(raw), data)
        return data

    # 新结构：{chat_id: [kw1, kw2]}
    if isinstance(raw, dict):
        data = {}
        for k, v in raw.items():
            if isinstance(v, list):
                data[str(k)] = sorted(
                    {
                        kw.strip()
                        for kw in v
                        if isinstance(kw, str) and kw.strip()
                    }
                )
        _AD_KEYWORDS_CACHE[path] = (id(raw), data)
        return data
    data = {}
    _AD_KEYWORDS_CACHE[path] = (id(raw), data)
    return data


def save_ad_keywords(context: ContextTypes.DEFAULT_TYPE, data: dict):
    path = get_bot_path(context, AD_KEYWORDS_FILE)
    save_json(path, data)
    _drop_path_caches(path)


def get_group_ad_keywords(
    context: ContextTypes.DEFAULT_TYPE, chat_id: str, *, auto_migrate: bool = True
) -> list[str]:
    """获取某个群的广告词，支持从旧全局 list 自动迁移到当前群。"""
    chat_key = str(chat_id)
    data = get_ad_keywords(context)

    keywords = data.get(chat_key)
    if isinstance(keywords, list):
        return keywords

    legacy = data.get("__legacy__", [])
    if isinstance(legacy, list) and legacy:
        if auto_migrate:
            data[chat_key] = list(legacy)
            save_ad_keywords(context, data)
        return list(legacy)

    return []


def get_lower_group_ad_keywords(
    context: ContextTypes.DEFAULT_TYPE, chat_id: str
) -> tuple[str, ...]:
    """返回可直接用于匹配的低成本小写关键词缓存。"""
    path = get_bot_path(context, AD_KEYWORDS_FILE)
    keywords = get_group_ad_keywords(context, chat_id)
    cache_key = (path, str(chat_id))
    cached = _LOWER_GROUP_KEYWORDS_CACHE.get(cache_key)
    if cached and cached[0] == id(keywords):
        return cached[1]
    normalized = tuple(kw.lower() for kw in keywords if isinstance(kw, str) and kw.strip())
    _LOWER_GROUP_KEYWORDS_CACHE[cache_key] = (id(keywords), normalized)
    return normalized


def _normalize_keywords(items: list[str]) -> list[str]:
    cleaned = []
    for kw in items:
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if not kw:
            continue
        cleaned.append(kw)
    return sorted(list(set(cleaned)))


def _parse_keywords_text(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    parts = re.split(r"[,\s]+", text.strip())
    return _normalize_keywords(parts)


def _parse_import_payload(raw_text: str, chat_id: str) -> list[str]:
    """
    支持:
    - JSON list: ["a", "b"]
    - JSON dict: {"-100xxx": ["a", "b"]}
    - 纯文本: a b c / 换行 / 逗号分隔
    """
    text = (raw_text or "").strip()
    if not text:
        return []

    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            return _parse_keywords_text(text)
        if isinstance(data, list):
            return _normalize_keywords(data)
        if isinstance(data, dict):
            group_list = data.get(str(chat_id))
            if isinstance(group_list, list):
                return _normalize_keywords(group_list)
            return []
    return _parse_keywords_text(text)


def _format_keywords_page(items: list[str], page: int, per_page: int) -> str:
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    end = start + per_page
    lines = [f"广告词总数：{total} | 第 {page}/{pages} 页", ""]
    for i, kw in enumerate(items[start:end], start=start + 1):
        lines.append(f"{i}. {kw}")
    if page < pages:
        lines.append(f"\n➡️ 发送「群广告词 查看 {page + 1}」查看下一页")
    return "\n".join(lines)


def _merge_all_keywords(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    data = get_ad_keywords(context)
    merged = []
    if isinstance(data, dict):
        legacy = data.get("__legacy__")
        if isinstance(legacy, list):
            merged.extend(legacy)
        for k, v in data.items():
            if k == "__legacy__":
                continue
            if isinstance(v, list):
                merged.extend(v)
    return _normalize_keywords(merged)


def get_whitelist(context: ContextTypes.DEFAULT_TYPE):
    """只读取按群白名单新结构：{chat_id: {user_id: {...}}}。"""
    path = get_bot_path(context, WHITELIST_FILE)
    raw = load_json(path)
    cached = _WHITELIST_CACHE.get(path)
    if cached and cached[0] == id(raw):
        return cached[1]
    if not isinstance(raw, dict):
        # 忽略旧结构并重置
        save_whitelist(context, {})
        return {}

    data = {}
    valid = True
    for chat_id, users in raw.items():
        if not isinstance(users, dict):
            valid = False
            break
        normalized_users = {}
        for uid, info in users.items():
            if not isinstance(info, dict):
                valid = False
                break
            normalized_users[str(uid)] = info
        if not valid:
            break
        data[str(chat_id)] = normalized_users

    if not valid:
        # 旧结构或脏数据：直接忽略并重置新结构
        save_whitelist(context, {})
        return {}

    _WHITELIST_CACHE[path] = (id(raw), data)
    return data


def save_whitelist(context: ContextTypes.DEFAULT_TYPE, data: dict):
    path = get_bot_path(context, WHITELIST_FILE)
    save_json(path, data)
    _drop_path_caches(path)


def get_group_whitelist_users(
    context: ContextTypes.DEFAULT_TYPE, chat_id: str
) -> dict:
    """获取某群白名单（仅新结构）。"""
    chat_key = str(chat_id)
    data = get_whitelist(context)
    users = data.get(chat_key, {})
    return users if isinstance(users, dict) else {}


def is_whitelisted(user_id: int, chat_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    whitelist = get_group_whitelist_users(context, chat_id)
    return str(user_id) in whitelist


def has_telegram_link(msg) -> bool:
    entities = msg.entities or []
    text = msg.text or ""

    for e in entities:
        if e.type in ("url", "text_link"):
            url = e.url or text[e.offset : e.offset + e.length]
            if "t.me/" in url or "telegram.me/" in url:
                return True
    return False


def has_any_link(msg) -> bool:
    entities = msg.entities or []
    text = msg.text or ""

    for e in entities:
        if e.type == "url":
            return True
        if e.type == "text_link" and e.url:
            return True
    return False


async def _is_linked_channel_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, msg
) -> bool:
    sender_chat = getattr(msg, "sender_chat", None)
    if not sender_chat:
        return False

    sender_chat_id = getattr(sender_chat, "id", None)
    sender_chat_type = str(getattr(sender_chat, "type", "") or "").lower()
    if hasattr(getattr(sender_chat, "type", None), "name"):
        sender_chat_type = str(sender_chat.type.name or "").lower()

    if sender_chat_type != "channel":
        return False

    linked_chat_id = getattr(update.effective_chat, "linked_chat_id", None)
    if linked_chat_id is None:
        try:
            full_chat = await context.bot.get_chat(update.effective_chat.id)
            linked_chat_id = getattr(full_chat, "linked_chat_id", None)
        except Exception:
            linked_chat_id = None

    if linked_chat_id and sender_chat_id and int(linked_chat_id) == int(sender_chat_id):
        return True

    # 兼容旧行为：只要明确是频道身份发言，也默认放行
    return True

@group_allowed
async def check_for_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """只在命中广告规则后才做管理员 API 查询和删除操作。"""
    if update.channel_post:
        return  # 频道消息不删除

    msg = update.message or update.edited_message
    if not msg:
        return

    chat_id = str(update.effective_chat.id)
    group_config = get_group_whitelist(context).get(chat_id, {})
    # 大多数群没有开启广告过滤；此前这里会先请求管理员列表，造成首条消息变慢。
    if not group_config.get("ad_filter", False):
        return

    api_kwargs = getattr(msg, "api_kwargs", {}) or {}
    guest_bot_caller_user = api_kwargs.get("guest_bot_caller_user")
    user = msg.from_user

    # 访客机器人消息不需要关键词命中，仍保留原有的删除行为。
    if guest_bot_caller_user:
        if await is_bot_admin(update, context):
            try:
                await msg.delete()
            except Exception as exc:
                print(f"❌ 删除 Guest Bot 消息失败: {exc}", flush=True)
        return

    if not user:
        return
    if is_whitelisted(user.id, chat_id, context):
        return

    text = ((msg.text or "") + " " + (msg.caption or "")).lower()
    keyword_hit = any(keyword in text for keyword in get_lower_group_ad_keywords(context, chat_id))
    should_delete = bool(
        keyword_hit or URL_PATTERN.search(text) or TELEGRAM_LINK_PATTERN.search(text) or contains_zodiac_ad(text)
    )
    if not should_delete:
        return

    # 只有可疑消息才查询管理员；管理员列表有缓存，但缓存冷启动时是一次网络请求。
    if not await is_bot_admin(update, context):
        return
    if await is_admin(update, context):
        return
    if await _is_linked_channel_message(update, context, msg):
        return

    try:
        await msg.delete()
    except telegram.error.BadRequest as exc:
        error_text = str(exc).lower()
        if "message can't be deleted" in error_text or "message to delete not found" in error_text:
            return
        print(f"[删除失败] {exc}")



# ---------- 添加广告词命令 ----------
from command_router import register_command


@register_command("添加广告词")
async def add_ad_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_ad_command_access(update, context):
        return
    chat_id = str(update.effective_chat.id)

    # 判断是否是回复消息
    if update.message.reply_to_message and update.message.reply_to_message.text:
        raw_kw = update.message.reply_to_message.text.strip()
    else:
        # 否则从命令参数获取
        if not context.args:
            await safe_reply(update, context,
                "❗用法：添加广告词 <关键词1 关键词2 ...> 或回复一条消息"
            )
            return
        raw_kw = " ".join(context.args).strip()

    if not raw_kw:
        return

    new_keywords = [kw for kw in raw_kw.split() if kw.strip()]

    all_data = get_ad_keywords(context)
    AD_KEYWORDS = get_group_ad_keywords(context, chat_id)

    added = []
    existed = []
    for kw in new_keywords:
        if kw in AD_KEYWORDS:
            existed.append(kw)
            continue
        AD_KEYWORDS.append(kw)
        added.append(kw)

    # 去重 + 排序保存
    AD_KEYWORDS = sorted(list(set(AD_KEYWORDS)))
    all_data[chat_id] = AD_KEYWORDS
    save_ad_keywords(context, all_data)
    msg_lines = []
    if added:
        msg_lines.append("✅ 已添加广告词：" + " ".join([f"『{x}』" for x in added]))
    if existed:
        msg_lines.append("⚠️ 已存在广告词：" + " ".join([f"『{x}』" for x in existed]))
    await safe_reply(update, context, "\n".join(msg_lines) if msg_lines else "⚠️ 未添加任何广告词")


@register_command("删除广告词")
async def remove_ad_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_ad_command_access(update, context):
        return
    chat_id = str(update.effective_chat.id)

    # 判断是否是回复消息
    if update.message.reply_to_message and update.message.reply_to_message.text:
        del_kw = update.message.reply_to_message.text.strip()
    else:
        # 否则从命令参数获取
        if not context.args:
            await safe_reply(update, context,
                "❗用法：删除广告词 <关键词> 或回复一条消息"
            )
            return
        del_kw = " ".join(context.args).strip()

    if not del_kw:
        return

    all_data = get_ad_keywords(context)
    AD_KEYWORDS = get_group_ad_keywords(context, chat_id)

    if del_kw not in AD_KEYWORDS:
        await safe_reply(update, context,f"⚠️ 广告词『{del_kw}』不存在")
        return

    # 删除广告词
    AD_KEYWORDS.remove(del_kw)
    all_data[chat_id] = AD_KEYWORDS
    save_ad_keywords(context, all_data)

    await safe_reply(update, context,f"✅ 已删除广告词：『{del_kw}』")


@register_command("群广告词", "查看广告词")
async def group_ad_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_ad_command_access(update, context):
        return
    chat_id = str(update.effective_chat.id)

    args = context.args or []
    action = args[0] if args else "查看"

    if action in ("查看", "看", "列表"):
        page = 1
        if len(args) > 1 and str(args[1]).isdigit():
            page = int(args[1])
        keywords = get_group_ad_keywords(context, chat_id)
        if not keywords:
            return await safe_reply(update, context, "当前群广告词为空。")
        text = _format_keywords_page(keywords, page, per_page=50)
        return await safe_reply(update, context, text)

    if action == "导出":
        keywords = get_group_ad_keywords(context, chat_id)
        if not keywords:
            return await safe_reply(update, context, "当前群广告词为空，无法导出。")
        filename = f"ad_keywords_{chat_id}.json"
        temp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(keywords, f, ensure_ascii=False, indent=2)
        with open(temp_path, "rb") as f:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
        return
    if action in ("合并导出", "合并"):
        keywords = _merge_all_keywords(context)
        if not keywords:
            return await safe_reply(update, context, "当前广告词为空，无法导出。")
        filename = "ad_keywords_merged.json"
        temp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(keywords, f, ensure_ascii=False, indent=2)
        with open(temp_path, "rb") as f:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
        return

    if action == "导入":
        inline_text = " ".join(args[1:]).strip() if len(args) > 1 else ""
        source_text = ""
        cleanup_path = None

        if inline_text:
            source_text = inline_text
        elif update.message and update.message.reply_to_message:
            replied = update.message.reply_to_message
            if replied.text:
                source_text = replied.text
            elif replied.document:
                doc = replied.document
                file = await context.bot.get_file(doc.file_id)
                suffix = ""
                if doc.file_name and "." in doc.file_name:
                    suffix = "." + doc.file_name.rsplit(".", 1)[-1]
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                cleanup_path = tmp.name
                tmp.close()
                await file.download_to_drive(custom_path=cleanup_path)
                with open(cleanup_path, "r", encoding="utf-8") as f:
                    source_text = f.read()

        if cleanup_path:
            try:
                os.remove(cleanup_path)
            except Exception:
                pass

        if not source_text:
            return await safe_reply(
                update,
                context,
                "❗用法：群广告词 导入 <关键词...> 或回复一条消息/文件后发送「群广告词 导入」",
            )

        incoming = _parse_import_payload(source_text, chat_id)
        if not incoming:
            return await safe_reply(update, context, "⚠️ 未识别到可导入的广告词。")

        all_data = get_ad_keywords(context)
        current = get_group_ad_keywords(context, chat_id)
        merged = _normalize_keywords(current + incoming)
        all_data[chat_id] = merged
        save_ad_keywords(context, all_data)

        added_count = len(merged) - len(current)
        return await safe_reply(
            update, context, f"✅ 导入完成：新增 {added_count} 个，总数 {len(merged)}。"
        )

    return await safe_reply(
        update,
        context,
        "❗用法：群广告词 查看 [页码]\n"
        "群广告词 导出\n"
        "群广告词 合并导出\n"
        "群广告词 导入 <关键词...>（或回复文本/文件）",
    )


@register_command("添加白名单")
async def add_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_ad_command_access(update, context):
        return
    chat_id = str(update.effective_chat.id)

    # 获取目标用户
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        if not context.args:
            await safe_reply(update, context,
                "❗用法：回复某人发送「添加白名单」\n或：添加白名单 <user_id>"
            )
            return
        try:
            user_id = int(context.args[0])
            target_user = type("User", (), {"id": user_id, "username": str(user_id)})
        except ValueError:
            await safe_reply(update, context,"❗请输入正确的 user_id")
            return

    all_whitelist = get_whitelist(context)
    whitelist = get_group_whitelist_users(context, chat_id)
    uid = str(target_user.id)

    if uid in whitelist:
        await safe_reply(update, context,"⚠️ 该用户已在广告白名单中")
        return

    whitelist[uid] = {
        "username": target_user.username,
    }

    all_whitelist[chat_id] = whitelist
    save_whitelist(context, all_whitelist)

    await safe_reply(update, context,
        f"✅ 已将用户 {target_user.username or uid} 加入广告白名单"
    )
