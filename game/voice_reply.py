import asyncio
import os
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


# =========================
CONFIG_PATH = "config_data/user_tts_config.json"

# VOICE_MAP = {
#     "女": "zh-CN-XiaoxiaoNeural",
#     "男": "zh-CN-YunxiNeural",
#     "御姐": "zh-CN-XiaoyiNeural",
#     "播报": "zh-CN-YunjianNeural",
#     "客服": "zh-CN-XiaohanNeural",
# }


VOICE_MAP = {
    # 👧 女声系
    "女": "zh-CN-XiaoxiaoNeural",  # 标准女声（默认）
    "御姐": "zh-CN-XiaoyiNeural",  # 温柔御姐
    "客服": "zh-CN-XiaohanNeural",  # 温柔御姐
    # 👨 男声系
    "年轻": "zh-CN-YunxiNeural",  # 年轻男声（默认）
    "播报": "zh-CN-YunjianNeural",  # 播音/系统播报
    "男": "zh-CN-YunyangNeural",  # 自然男声
}

RATE_MAP = {
    "慢": "-25%",
    "默认": "+0%",
    "快": "+20%",
    "很快": "+35%",
}

PITCH_MAP = {
    "高": "+50Hz",
    "低": "-50Hz",
    "默认": "+0Hz",
}


STYLE_MAP = {
    "温柔": "affectionate",
    "聊天": "chat",
    "播报": "newscast",
    "专业": "narration-professional",
    "严肃": "serious",
    "默认": "general",
}


TEMP_AUDIO_DIR = "downloads/voice_tmp"
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)


# CONFIG TOOLS
# =========================
def load_config():
    data = load_json(CONFIG_PATH)
    return data if isinstance(data, dict) else {}


def save_config(config):
    save_json(CONFIG_PATH, config)


def get_user_config(user_id: str):
    config = load_config()
    return config.get(
        user_id,
        {
            "voice": VOICE_MAP["女"],
            "rate": RATE_MAP["默认"],
            "pitch": PITCH_MAP["默认"],
        },
    )


async def _edge_tts(text, voice, rate, pitch, path):
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
    )

    await communicate.save(path)


async def _safe_edge_tts(text, voice, rate, pitch, path, retry=2):
    for i in range(retry):
        try:
            await _edge_tts(text, voice, rate, pitch, path)
            return True
        except Exception as e:
            print(f"[edge-tts失败] 第{i+1}次:", e)
            await asyncio.sleep(0.5)

    return False


def _gtts_fallback(text, path):
    try:
        tts = gTTS(text=text, lang="zh")
        tts.save(path)
        return True
    except Exception as e:
        print("[gTTS也失败]:", e)
        return False
    
# =========================
# MENU
# =========================
@register_command("声音设置")
async def voice_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        # 👧 女声一组
        [
            InlineKeyboardButton("👧 女", callback_data="voice_女"),
            InlineKeyboardButton("💃 御姐", callback_data="voice_御姐"),
            InlineKeyboardButton("🎧 客服", callback_data="voice_客服"),
        ],
        # 👨 男声一组
        [
            InlineKeyboardButton("👨 男", callback_data="voice_男"),
            InlineKeyboardButton("📢 播报", callback_data="voice_播报"),
            InlineKeyboardButton("🧑‍💼 年轻", callback_data="voice_年轻"),
        ],
      
    ]

    await update.message.reply_text(
        "🎙 请选择语音类型：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def speed_menu(voice: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🐢 慢", callback_data=f"s_{voice}_慢"),
                InlineKeyboardButton("🙂 默认", callback_data=f"s_{voice}_默认"),
            ],
            [
                InlineKeyboardButton("⚡ 快", callback_data=f"s_{voice}_快"),
                InlineKeyboardButton("🚀 很快", callback_data=f"s_{voice}_很快"),
            ],
        ]
    )


async def voice_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    voice = query.data.replace("voice_", "")

    await query.edit_message_text(
        "🎚 请选择语速：",
        reply_markup=speed_menu(voice),
    )


async def speed_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, voice, speed = query.data.split("_")

    user_id = str(query.from_user.id)

    config = load_config()

    config[user_id] = {
        "voice": VOICE_MAP.get(voice),
        "rate": RATE_MAP.get(speed, RATE_MAP["默认"]),
        "pitch": PITCH_MAP["默认"],
    }

    save_config(config)

    await query.edit_message_text(f"✅ 设置成功：\n声音：{voice}\n语速：{speed}")


# =========================
@register_command("声音配置")
async def show_voice_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    config = get_user_config(user_id)

    voice = next((k for k, v in VOICE_MAP.items() if v == config["voice"]), "女")
    rate = next((k for k, v in RATE_MAP.items() if v == config["rate"]), "默认")
    pitch = next((k for k, v in PITCH_MAP.items() if v == config["pitch"]), "默认")

    await update.message.reply_text(
        f"🎙 你的语音配置：\n" f"声音：{voice}\n" f"语速：{rate}\n" f"音调：{pitch}"
    )


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

    user_config = get_user_config(str(update.effective_user.id))

    mp3_path = _bot_temp_path(context, "tts", "mp3")
    ogg_path = _bot_temp_path(context, "tts", "ogg")

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=user_config["voice"],
            rate=user_config["rate"],
            pitch=user_config["pitch"],
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


# GROUP TTS (edge-tts)
# async def group_tts_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     text = " ".join(context.args).strip()
#     if not text:
#         await update.message.reply_text("请输入内容")
#         return

#     user_config = get_user_config(str(update.effective_user.id))

#     mp3_path = _bot_temp_path(context, "group_tts", "mp3")
#     ogg_path = _bot_temp_path(context, "group_tts", "ogg")

#     try:
#         communicate = edge_tts.Communicate(
#             text=text,
#             voice=user_config["voice"],
#             rate=user_config["rate"],
#             pitch=user_config["pitch"],
#         )
#         await communicate.save(mp3_path)

#         os.system(
#             f'ffmpeg -i "{mp3_path}" -c:a libopus -ar 24000 -ac 1 "{ogg_path}" -y'
#         )

#         with open(ogg_path, "rb") as f:
#             await update.message.reply_voice(voice=f)

#     finally:
#         for f in [mp3_path, ogg_path]:
#             if os.path.exists(f):
#                 os.remove(f)

async def group_tts_voice(update, context):

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("请输入内容")
        return


    user_config = get_user_config(str(update.effective_user.id))

    mp3_path = _bot_temp_path(context, "tts", "mp3")
    ogg_path = _bot_temp_path(context, "tts", "ogg")

    voice = user_config["voice"]
    rate = user_config["rate"]
    pitch = user_config["pitch"]

    # 🧯 限制防炸
    text = text[:200]

    success = await _safe_edge_tts(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
        path=mp3_path
    )

    # ❗ edge失败 → gTTS兜底
    if not success:
        print("⚠️ edge失败，切换gTTS")
        success = _gtts_fallback(text, mp3_path)

    if not success:
        await update.message.reply_text("❌ TTS失败，请稍后再试")
        return

    # 🎧 转音频
    os.system(
        f'ffmpeg -i "{mp3_path}" -c:a libopus -ar 24000 -ac 1 "{ogg_path}" -y'
    )

    try:
        with open(ogg_path, "rb") as f:
            await update.message.reply_voice(voice=f)
    finally:
        for f in [mp3_path, ogg_path]:
            if os.path.exists(f):
                os.remove(f)

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


async def ignore_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return None


# =========================
# HANDLERS
# =========================
def register_voice_handlers(application):
    application.add_handler(CommandHandler("tts", tts_voice_reply))
    application.add_handler(CommandHandler("newtts", group_tts_voice))
    application.add_handler(CommandHandler("setvoice", voice_menu))
    application.add_handler(
        CallbackQueryHandler(voice_select_handler, pattern=r"^voice_")
    )
    application.add_handler(CallbackQueryHandler(speed_select_handler, pattern=r"^s_"))
    # 语音识别（保留）
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, ignore_voice_message)
    )
