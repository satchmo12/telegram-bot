# message_forward.py
# 用户私聊机器人，机器转发用户私聊信息，管理员通过回复把消息回复给用户
from functools import wraps
from typing import Optional
import time
from telegram import Update
from telegram.ext import ContextTypes
import os
import asyncio


from command_router import get_matched_command, register_command
from utils import (
    BOT_OWNER_ID,
    BOT_USER_FILE,
    FORWARD_MAP_FILE,
    GROUP_LIST_FILE,
    load_json,
    safe_reply,
    save_json,
)

BATCH_SIZE = 50  # 每批发送数量，可调整
BATCH_DELAY = 1  # 每批发送延迟秒数
PRIVATE_CONFIG_COMMANDS = {
    "群配置",
    "群设置",
    "群开关",
    "群状态",
    "群静默",
    "群验证",
    "群欢迎",
    "群广告",
    "群限频",
    "群限频条数",
    "限频条数",
    "群庄园",
    "群好友",
    "群主动说话",
    "主动说话",
    "频道配置",
    "会员订阅"
}


def get_owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.application.bot_data.get("owner_id", BOT_OWNER_ID)


def load_forward_map():
    data = load_json(FORWARD_MAP_FILE)
    return data if isinstance(data, dict) else {}


def save_forward_map(data):
    save_json(FORWARD_MAP_FILE, data)


def get_group_list():
    data = load_json(GROUP_LIST_FILE)
    return data if isinstance(data, dict) else {}


def _load_user_data():
    return load_json(BOT_USER_FILE) or {}


def _save_user_data(data: dict):
    save_json(BOT_USER_FILE, data)


def _resolve_target_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if update.message and update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id
        forward_map = load_forward_map()
        uid = forward_map.get(str(reply_msg_id))
        if uid:
            return str(uid)
    if context.args:
        if str(context.args[0]).lstrip("-").isdigit():
            return str(context.args[0])
    return None


#  机器人代发
async def safe_forward_media(bot, chat_id, msg):
    """机器人代发消息，不显示原用户信息"""
    # 文字 + Emoji
    if msg.text:
        return await bot.send_message(chat_id=chat_id, text=msg.text)

    # 照片
    if msg.photo:
        return await bot.send_photo(
            chat_id=chat_id, photo=msg.photo[-1].file_id, caption=msg.caption or ""
        )

    # GIF / 动画
    if msg.animation:
        return await bot.send_animation(
            chat_id=chat_id, animation=msg.animation.file_id, caption=msg.caption or ""
        )

    # 视频
    if msg.video:
        return await bot.send_video(
            chat_id=chat_id, video=msg.video.file_id, caption=msg.caption or ""
        )

    # 文件 / 文档
    if msg.document:
        return await bot.send_document(
            chat_id=chat_id, document=msg.document.file_id, caption=msg.caption or ""
        )

    # 静态贴纸
    if msg.sticker and not msg.sticker.is_animated:
        return await bot.send_sticker(chat_id=chat_id, sticker=msg.sticker.file_id)

    # 动画贴纸（TGS） → 会变成文件
    if msg.sticker and msg.sticker.is_animated:
        file = await bot.get_file(msg.sticker.file_id)
        temp_path = f"temp_{msg.sticker.file_id}.tgs"
        await file.download_to_drive(temp_path)
        with open(temp_path, "rb") as f:
            res = await bot.send_document(
                chat_id=chat_id, document=f, filename="sticker.tgs"
            )
        os.remove(temp_path)
        return res

    raise ValueError("不支持的消息类型")


async def send_to_targets(bot, target_ids: list[int], src):
    for chat_id in target_ids:
        try:
            if src.sticker:
                await bot.send_sticker(chat_id, src.sticker.file_id)

            elif src.photo:
                await bot.send_photo(
                    chat_id, src.photo[-1].file_id, caption=src.caption
                )

            elif src.video:
                await bot.send_video(chat_id, src.video.file_id, caption=src.caption)

            elif src.animation:
                await bot.send_animation(
                    chat_id, src.animation.file_id, caption=src.caption
                )

            elif src.document:
                await bot.send_document(
                    chat_id, src.document.file_id, caption=src.caption
                )

            elif src.text:
                await bot.send_message(chat_id, src.text)

            else:
                print(f"⚠️ 不支持的消息类型: {chat_id}")
                continue

            print(f"✅ 已发送到 {chat_id}")

        except Exception as e:
            print(f"❌ 发送失败 {chat_id}: {e}")


# 用户私聊 → 转发给管理员
async def forward_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = get_owner_id(context)

    user = update.effective_user
    if user.id == owner_id:
        return  # 管理员自己发的消息不转发

    user_data = _load_user_data()
    uid = str(user.id)
    if user_data.get(uid, {}).get("blocked", False):
        return

    # 忽略命令消息
    if update.message.text and update.message.text.startswith("/"):
        return
    if update.message.text:
        matched = get_matched_command(update.message.text)
        if matched in PRIVATE_CONFIG_COMMANDS:
            return

    await context.bot.send_message(
        chat_id=owner_id,
        text=f'来自 <a href="tg://user?id={user.id}">{user.full_name}</a> 的消息：',
        parse_mode="HTML",
    )

    try:
        # 转发消息到管理员
        sent = await safe_forward_media(context.bot, owner_id, update.message)
    except Exception as e:
        print(f"❌ 转发消息失败: {e}")
        await safe_reply(update, context, "⚠️ 转发消息失败，请稍后重试。")
        return

    # 记录原用户 ID 以供管理员回复时查找
    forward_map = load_forward_map()
    forward_map[str(sent.message_id)] = user.id
    save_forward_map(forward_map)

    users = user_data
    users[uid] = {
        "name": user.first_name or "",
        "username": user.username or "",
        "join_time": users.get(uid, {}).get("join_time", int(time.time())),
        "last_active": int(time.time()),
        "blocked": users.get(uid, {}).get("blocked", False),
    }

    _save_user_data(users)

    # await safe_reply(update, context,"✅ 已将你的消息转发给管理员，请等待回复。")


# 管理员回复转发消息 → 自动回原用户
async def reply_from_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return

    if not update.message.reply_to_message:
        await safe_reply(update, context, "请使用“回复”功能回复用户消息。")
        return

    reply_msg_id = update.message.reply_to_message.message_id
    forward_map = load_forward_map()
    target_user_id = forward_map.get(str(reply_msg_id))

    if not target_user_id:
        # await safe_reply(update, context,"找不到对应用户，可能消息过期或未记录。")
        return

    try:
        await safe_forward_media(context.bot, target_user_id, update.message)
    except Exception as e:
        await safe_reply(update, context, f"发送失败: {e}")


@register_command("广播")
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    if not update.message.reply_to_message:
        return await safe_reply(update, context, "📌 请【回复】你要广播的内容")

    group_ids = list(get_group_list().keys())
    src = update.message.reply_to_message

    await send_to_targets(context.bot, group_ids, src)
    await safe_reply(update, context, "📣 广播完成")


@register_command("用户广播")
async def cmd_user_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    if not update.message.reply_to_message:
        return await safe_reply(update, context, "📌 请回复要发送的消息")

    src = update.message.reply_to_message
    user_ids = list(load_json(BOT_USER_FILE).keys())  # 获取用户ID列表

    total_users = len(user_ids)
    success, failed = 0, 0

    for i in range(0, total_users, BATCH_SIZE):
        batch = user_ids[i : i + BATCH_SIZE]
        for uid in batch:
            try:
                await safe_forward_media(context.bot, uid, src)
                success += 1
            except Exception as e:
                print(f"发送失败 {uid}: {e}")
                failed += 1
        await asyncio.sleep(BATCH_DELAY)  # 等待，避免触发频率限制
        print(f"✅ 已发送 {min(i+BATCH_SIZE, total_users)}/{total_users} 个用户")

    await safe_reply(
        update, context, f"📣 用户广播完成\n✅ 成功: {success}\n❌ 失败: {failed}"
    )


@register_command("用户列表")
async def cmd_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    user_data = _load_user_data()
    if not user_data:
        return await safe_reply(update, context, "📭 当前没有用户记录")

    args = update.message.text.split()
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    per_page = 20

    total = len(user_data)
    pages = (total + per_page - 1) // per_page
    if page > pages:
        page = pages

    start = (page - 1) * per_page
    end = start + per_page
    items = list(user_data.items())[start:end]

    text = f"👥 用户总数：{total} | 第 {page}/{pages} 页\n\n"
    for i, (uid, info) in enumerate(items, start=start + 1):
        name = info.get("name", "")
        username = info.get("username", "")
        blocked = "（已拉黑）" if info.get("blocked", False) else ""
        text += f"{i}. {name} (@{username}){blocked}\nID: {uid}\n\n"

    if page < pages:
        text += f"➡️ 发送 /用户列表 {page+1} 查看下一页"

    await safe_reply(update, context, text)


@register_command("拉黑用户", "拉黑", "黑名单添加")
async def cmd_blacklist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    uid = _resolve_target_uid(update, context)
    if not uid:
        return await safe_reply(
            update, context, "用法：拉黑用户 用户ID\n或回复用户消息后发送 拉黑用户"
        )

    user_data = _load_user_data()
    info = user_data.get(uid, {})
    info["blocked"] = True
    info["blocked_at"] = int(time.time())
    info.setdefault("name", "")
    info.setdefault("username", "")
    user_data[uid] = info
    _save_user_data(user_data)

    await safe_reply(update, context, f"✅ 已拉黑用户：{uid}")


@register_command("移除拉黑", "解除拉黑", "黑名单移除")
async def cmd_unblacklist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    uid = _resolve_target_uid(update, context)
    if not uid:
        return await safe_reply(
            update, context, "用法：移除拉黑 用户ID\n或回复用户消息后发送 移除拉黑"
        )

    user_data = _load_user_data()
    if uid not in user_data:
        return await safe_reply(update, context, "未找到该用户记录。")
    user_data[uid]["blocked"] = False
    _save_user_data(user_data)
    await safe_reply(update, context, f"✅ 已移除拉黑：{uid}")


@register_command("黑名单", "查看黑名单")
async def cmd_blacklist_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return await safe_reply(update, context, "⚠️ 仅管理员可用")

    user_data = _load_user_data()
    black_users = [(uid, info) for uid, info in user_data.items() if info.get("blocked")]
    if not black_users:
        return await safe_reply(update, context, "当前黑名单为空。")

    args = update.message.text.split()
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    per_page = 20
    total = len(black_users)
    pages = (total + per_page - 1) // per_page
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    end = start + per_page
    items = black_users[start:end]

    text = f"黑名单总数：{total} | 第 {page}/{pages} 页\n\n"
    for i, (uid, info) in enumerate(items, start=start + 1):
        name = info.get("name", "")
        username = info.get("username", "")
        text += f"{i}. {name} (@{username})\nID: {uid}\n\n"

    if page < pages:
        text += f"➡️ 发送 /黑名单 {page+1} 查看下一页"

    await safe_reply(update, context, text)


@register_command("导出用户")
async def cmd_export_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != get_owner_id(context):
        return

    user_data = load_json(BOT_USER_FILE) or {}

    if not user_data:
        return await safe_reply(update, context, "没有用户数据")

    file_path = "users.txt"

    with open(file_path, "w", encoding="utf-8") as f:
        for uid, info in user_data.items():
            f.write(f"{uid} | {info.get('name','')} | @{info.get('username','')}\n")

    with open(file_path, "rb") as f:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
