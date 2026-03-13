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


def get_ad_keywords(context: ContextTypes.DEFAULT_TYPE):
    """兼容旧结构并按群读取广告关键词。"""
    path = get_bot_path(context, AD_KEYWORDS_FILE)
    raw = load_json(path)

    # 旧结构：直接是 list（全局共享）
    if isinstance(raw, list):
        cleaned = sorted(
            {
                kw.strip()
                for kw in raw
                if isinstance(kw, str) and kw.strip()
            }
        )
        return {"__legacy__": cleaned}

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
        return data
    return {}


def save_ad_keywords(context: ContextTypes.DEFAULT_TYPE, data: dict):
    path = get_bot_path(context, AD_KEYWORDS_FILE)
    save_json(path, data)


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


def get_whitelist(context: ContextTypes.DEFAULT_TYPE):
    """只读取按群白名单新结构：{chat_id: {user_id: {...}}}。"""
    raw = load_json(get_bot_path(context, WHITELIST_FILE))
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

    return data


def save_whitelist(context: ContextTypes.DEFAULT_TYPE, data: dict):
    save_json(get_bot_path(context, WHITELIST_FILE), data)


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

@group_allowed
async def check_for_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_bot_admin(update, context):
        return  # 普通机器人只做日志/转发，不删
    chat_id = str(update.effective_chat.id)
    group_config = get_group_whitelist(context).get(chat_id, {})

    # 广告开关关闭 → 直接放行
    if not group_config.get("ad_filter", False):
        return
    if not await is_bot_admin(update, context):
        return

    if await is_admin(update, context):
        return

    msg = update.message or update.edited_message or update.channel_post
    if not msg:
        return

    user = msg.from_user
    if not user:
        return

    # ✅ 白名单放行
    if is_whitelisted(user.id, chat_id, context):
        return

    # 统一提取文本
    text = ((msg.text or "") + " " + (msg.caption or "")).lower()

    keyword_list = get_group_ad_keywords(context, chat_id)
    keyword_hit = any(
        isinstance(kw, str) and kw.strip() and kw.strip().lower() in text
        for kw in keyword_list
    )

    # 🔥 核心判断
    # is_link_preview = bool(msg.link_preview_options)
    # is_tg_link = has_any_link(msg)
    is_tg_link = False
    should_delete = (
        keyword_hit
        or URL_PATTERN.search(text)
        or TELEGRAM_LINK_PATTERN.search(text)
        # or is_link_preview  # ✅ 你截图那种预览名片
        or is_tg_link  # ✅ t.me 频道 / 群链接
    )

    if not should_delete:
        return

    # ================= 执行删除 =================

    try:
        await msg.delete()
    except telegram.error.BadRequest as e:
        print(f"[删除失败] {e}")
        return

    # 提示删除

    # try:

    #     tip_msg = await msg.chat.send_message(
    #         f"🚫 @{user.username or user.first_name}，广告内容{msg}已被删除。"
    #     )
    #     asyncio.create_task(delete_later(tip_msg, delay=5))
    # except telegram.error.BadRequest as e:
    #     print(f"[发送提示失败] {e}")


# ---------- 添加广告词命令 ----------
from command_router import register_command


@register_command("添加广告词")
async def add_ad_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await is_admin(update, context):
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

    if not await is_admin(update, context):
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

    if not await is_admin(update, context):
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
        "群广告词 导入 <关键词...>（或回复文本/文件）",
    )


@register_command("添加白名单")
async def add_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await is_admin(update, context):
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
