import os
import asyncio
import uuid
import edge_tts
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

from command_router import register_command
from utils import load_json, save_json, safe_reply

try:
    import whisper
except ModuleNotFoundError:
    whisper = None

whisper_model = None


def _get_whisper_model():
    global whisper_model
    if whisper_model is None:
        whisper_model = whisper.load_model("base")  # 可改 tiny / small / medium
    return whisper_model


CONFIG_PATH = "config_data/group_tts_config.json"
VOICE_STATE = {}

VOICE_MAP = {
    "女": "zh-CN-XiaoxiaoNeural",
    "男": "zh-CN-YunxiNeural",
    "御姐": "zh-CN-XiaoyiNeural",
    "播报": "zh-CN-YunjianNeural",
    "客服": "zh-CN-XiaohanNeural",
}

STYLE_MAP = {
    "温柔": "affectionate",
    "聊天": "chat",
    "播报": "newscast",
    "专业": "narration-professional",
    "严肃": "serious",
    "默认": "general",
}

RATE_MAP = {
    "慢": "-25%",
    "默认": "+0%",
    "快": "+20%",
    "很快": "+35%",
}

PITCH_MAP = {"高": "+50Hz", "低": "-50Hz", "默认": "+0Hz"}

TEMP_AUDIO_DIR = "downloads/voice_tmp"
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)


# =========================
# CONFIG
# =========================
def load_config():
    data = load_json(CONFIG_PATH)
    return data if isinstance(data, dict) else {}


def save_config(config):
    save_json(CONFIG_PATH, config)


def get_group_config(chat_id: str):
    config = load_config()
    return config.get(
        chat_id,
        {
            "voice": VOICE_MAP["女"],
            "rate": RATE_MAP["默认"],
            "pitch": PITCH_MAP["默认"],
        },
    )


# =========================
# TEMP FILE
# =========================
def _bot_temp_path(context, suffix, ext, file_id=""):
    bot_name = context.application.bot_data.get("name", "bot")
    safe_bot = bot_name.replace("/", "_")
    middle = f"{file_id}_" if file_id else ""
    name = f"{safe_bot}_{middle}{suffix}_{uuid.uuid4().hex}.{ext}"
    return os.path.join(TEMP_AUDIO_DIR, name)


# =========================
# TTS (主功能：edge-tts)
# =========================
async def tts_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip() or "你好，这是一个语音测试。"

    chat_id = str(update.effective_chat.id)
    config = get_group_config(chat_id)

    mp3_path = _bot_temp_path(context, "tts", "mp3")
    ogg_path = _bot_temp_path(context, "tts", "ogg")

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=config["voice"],
            rate=config["rate"],
            pitch=config["pitch"],
        )
        await communicate.save(mp3_path)

    except Exception as e:
        # fallback
        print("edge-tts失败，使用gTTS:", e)
        tts = gTTS(text=text, lang="zh")
        tts.save(mp3_path)

    os.system(f'ffmpeg -i "{mp3_path}" -c:a libopus -ar 24000 -ac 1 "{ogg_path}" -y')

    try:
        with open(ogg_path, "rb") as f:
            await update.message.reply_voice(voice=f)
    finally:
        for f in [mp3_path, ogg_path]:
            if os.path.exists(f):
                os.remove(f)


# =========================
# GROUP TTS (edge-tts)
@register_command("讲")
async def group_tts_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("请输入内容")
        return

    chat_id = str(update.effective_chat.id)
    config = get_group_config(chat_id)

    mp3_path = _bot_temp_path(context, "group_tts", "mp3")
    ogg_path = _bot_temp_path(context, "group_tts", "ogg")

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=config["voice"],
            rate=config["rate"],
            pitch=config["pitch"],
        )
        await communicate.save(mp3_path)

        os.system(
            f'ffmpeg -i "{mp3_path}" -c:a libopus -ar 24000 -ac 1 "{ogg_path}" -y'
        )

        with open(ogg_path, "rb") as f:
            await update.message.reply_voice(voice=f)

    finally:
        for f in [mp3_path, ogg_path]:
            if os.path.exists(f):
                os.remove(f)


# =========================
# 🎛️ 按钮菜单（新增）
@register_command("声音设置")
async def voice_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("👧 女声", callback_data="voice_女"),
            InlineKeyboardButton("👨 男声", callback_data="voice_男"),
        ],
        [
            InlineKeyboardButton("👩 御姐", callback_data="voice_御姐"),
            InlineKeyboardButton("🎧 客服", callback_data="voice_客服"),
        ],
        [
            InlineKeyboardButton("📢 播报", callback_data="voice_播报"),
        ],
    ]

    await update.message.reply_text(
        "🎙 请选择语音：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def speed_menu(chat_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🐢 慢", callback_data=f"s_慢_{chat_id}"),
                InlineKeyboardButton("🙂 正常", callback_data=f"s_默认_{chat_id}"),
            ],
            [
                InlineKeyboardButton("⚡ 快", callback_data=f"s_快_{chat_id}"),
                InlineKeyboardButton("🚀 很快", callback_data=f"s_很快_{chat_id}"),
            ],
        ]
    )


# =========================
# 🎛️ 按钮处理（核心）
# =========================
async def voice_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = str(query.message.chat.id)

    # ======================
    # 第一步：选声音
    # ======================
    if data.startswith("voice_"):
        voice = data.replace("voice_", "")

        VOICE_STATE[chat_id] = {"voice": voice}

        await query.edit_message_text(
            "🎚 请选择语速：",
            reply_markup=speed_menu(chat_id),
        )
        return

    # ======================
    # 第二步：选语速
    # ======================
    if data.startswith("s_"):
        parts = data.split("_", 2)
        speed = parts[1]
        origin_chat = parts[2]

        state = VOICE_STATE.get(origin_chat)
        if not state:
            await query.edit_message_text("❌ 选择失效，请重新 /setvoice")
            return

        config = load_config()

        config[origin_chat] = {
            "voice": VOICE_MAP[state["voice"]],
            "rate": RATE_MAP[speed],
            "pitch": PITCH_MAP["默认"],
        }

        save_config(config)

        VOICE_STATE.pop(origin_chat, None)

        await query.edit_message_text(f"✅ 已设置：{state['voice']} + {speed}")
        return


async def voice_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # voice_女 / voice_男 ...
    chat_id = str(query.message.chat.id)

    voice = data.replace("voice_", "")

    # 临时存状态
    VOICE_STATE[chat_id] = {"voice": voice}

    await query.edit_message_text(
        "🎚 请选择语速：",
        reply_markup=speed_menu(chat_id),
    )


async def speed_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # s_慢 / s_快 / s_默认 / s_很快
    chat_id = str(query.message.chat.id)

    parts = data.split("_", 1)
    speed = parts[1]

    state = VOICE_STATE.get(chat_id)

    if not state:
        await query.edit_message_text("❌ 状态失效，请重新 /setvoice")
        return

    config = load_config()

    config[chat_id] = {
        "voice": VOICE_MAP[state["voice"]],
        "rate": RATE_MAP[speed],
        "pitch": PITCH_MAP["默认"],
    }

    save_config(config)

    VOICE_STATE.pop(chat_id, None)

    await query.edit_message_text(
        f"✅ 设置成功：\n声音：{state['voice']}\n语速：{speed}"
    )


@register_command("声音配置")
async def show_voice_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    config = get_group_config(chat_id)

    voice = next((k for k, v in VOICE_MAP.items() if v == config.get("voice")), "女")
    rate = next((k for k, v in RATE_MAP.items() if v == config.get("rate")), "默认")
    pitch = next((k for k, v in PITCH_MAP.items() if v == config.get("pitch")), "默认")

    await update.message.reply_text(
        f"🎙 当前语音配置：\n" f"声音：{voice}\n" f"语速：{rate}\n" f"音调：{pitch}"
    )


@register_command("语音识别")
async def command_voice_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ 请回复一条语音再使用该命令")
        return

    replied_msg = update.message.reply_to_message
    voice = replied_msg.voice or replied_msg.audio

    if not voice:
        await update.message.reply_text("⚠️ 你回复的不是语音消息")
        return

    await update.message.reply_text("🎧 正在识别中...")

    file = await context.bot.get_file(voice.file_id)

    ogg_path = _bot_temp_path(context, "reply_voice_in", "ogg", voice.file_id)
    await file.download_to_drive(ogg_path)

    wav_path = _bot_temp_path(context, "reply_voice_in", "wav", voice.file_id)

    os.system(f'ffmpeg -y -i "{ogg_path}" -ar 16000 -ac 1 "{wav_path}"')

    try:
        model = _get_whisper_model()
        result = model.transcribe(wav_path, language="zh")
        text = result["text"]

        await safe_reply(update, context, f"📝 识别结果：\n{text}")

    except Exception as e:
        await safe_reply(update, context, f"❌ 识别失败：{e}")

    finally:
        for f in [ogg_path, wav_path]:
            if os.path.exists(f):
                os.remove(f)


# =========================
# HANDLERS
# =========================
def register_voice_handlers(application):
    application.add_handler(CommandHandler("tts", tts_voice_reply))
    application.add_handler(CommandHandler("newtts", group_tts_voice))

    application.add_handler(CommandHandler("setvoice", voice_menu))

    # ❗必须无 pattern
    application.add_handler(
        CallbackQueryHandler(voice_select_handler, pattern=r"^voice_")
    )

    application.add_handler(CallbackQueryHandler(speed_select_handler, pattern=r"^s_"))

    application.add_handler(CommandHandler("showvoice", show_voice_config))
    # 语音识别（保留）
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, lambda u, c: None)
    )
