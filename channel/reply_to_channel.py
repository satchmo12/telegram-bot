from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from command_router import register_command
from utils import is_admin, load_json, safe_reply, save_json

CONFIG_FILE = "data/reply_forward_config.json"


def _load_config() -> dict:
    data = load_json(CONFIG_FILE)
    return data if isinstance(data, dict) else {}


def _save_config(data: dict):
    save_json(CONFIG_FILE, data)


def _parse_target_chat(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _build_forward_text(update: Update) -> str:
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    content = (msg.text or msg.caption or "").strip()
    return (
        f"📨 群回复转发\n"
        f"🏷 群：{chat.title or chat.id}\n"
        f"👤 用户：{user.full_name} ({user.id})\n\n"
        f"{content}"
    )

@register_command("转发频道")
async def _forward_message_to_channel(
    src_msg, update: Update, context: ContextTypes.DEFAULT_TYPE, target_chat
):
    text = _build_forward_text(update)

    if src_msg.photo:
        await context.bot.send_photo(
            chat_id=target_chat,
            photo=src_msg.photo[-1].file_id,
            caption=text,
        )
        return
    if src_msg.video:
        await context.bot.send_video(
            chat_id=target_chat,
            video=src_msg.video.file_id,
            caption=text,
        )
        return
    if src_msg.document:
        await context.bot.send_document(
            chat_id=target_chat,
            document=src_msg.document.file_id,
            caption=text,
        )
        return
    if src_msg.animation:
        await context.bot.send_animation(
            chat_id=target_chat,
            animation=src_msg.animation.file_id,
            caption=text,
        )
        return
    if src_msg.voice:
        await context.bot.send_voice(
            chat_id=target_chat,
            voice=src_msg.voice.file_id,
            caption=text,
        )
        return
    if src_msg.sticker:
        await context.bot.send_message(chat_id=target_chat, text=text)
        await context.bot.send_sticker(chat_id=target_chat, sticker=src_msg.sticker.file_id)
        return
    if src_msg.text:
        await context.bot.send_message(chat_id=target_chat, text=text)
        return


@register_command("设置回复转发频道")
async def set_reply_forward_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    if not context.args:
        return await safe_reply(
            update, context, "用法：设置回复转发频道 @频道用户名 或 频道ID"
        )

    target = _parse_target_chat(context.args[0])
    if target is None:
        return await safe_reply(update, context, "❗频道格式错误，请输入 @频道用户名 或 频道ID")

    chat_id = str(update.effective_chat.id)
    data = _load_config()
    data[chat_id] = {"enabled": True, "target_chat": target}
    _save_config(data)

    await safe_reply(update, context, f"✅ 已开启回复转发，目标频道：{target}")


@register_command("关闭回复转发")
async def disable_reply_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    chat_id = str(update.effective_chat.id)
    data = _load_config()
    cfg = data.get(chat_id, {})
    cfg["enabled"] = False
    data[chat_id] = cfg
    _save_config(data)
    await safe_reply(update, context, "✅ 已关闭回复转发")


@register_command("回复转发状态")
async def reply_forward_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = _load_config()
    cfg = data.get(chat_id, {})
    enabled = bool(cfg.get("enabled", False))
    target = cfg.get("target_chat", "未设置")
    await safe_reply(
        update,
        context,
        f"📡 回复转发状态：{'✅ 开启' if enabled else '🚫 关闭'}\n目标频道：{target}",
    )


@register_command("转发")
async def forward_reply_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return await safe_reply(update, context, "该功能仅支持群内使用")
    if not msg.reply_to_message:
        return await safe_reply(update, context, "请先回复你要转发的消息，再发送：转发")

    chat_id = str(update.effective_chat.id)
    cfg = _load_config().get(chat_id, {})
    if not cfg or not cfg.get("enabled", False):
        return await safe_reply(update, context, "⚠️ 本群未开启回复转发，请先设置目标频道")
    target_chat = cfg.get("target_chat")
    if not target_chat:
        return await safe_reply(update, context, "⚠️ 未设置目标频道，请先设置回复转发频道")

    try:
        await _forward_message_to_channel(msg.reply_to_message, update, context, target_chat)
        await safe_reply(update, context, "✅ 已转发到目标频道")
    except Exception as e:
        print(f"⚠️ 回复转发失败: {e}")
        await safe_reply(update, context, "❌ 转发失败，请检查机器人在目标频道是否有发言权限")


def register_reply_to_channel_handlers(app):
    app.add_handler(CommandHandler("set_reply_forward_channel", set_reply_forward_channel))
    app.add_handler(CommandHandler("disable_reply_forward", disable_reply_forward))
    app.add_handler(CommandHandler("reply_forward_status", reply_forward_status))
