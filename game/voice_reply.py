# voice_reply.py
import os
import asyncio
import uuid
import edge_tts
from gtts import gTTS
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
import whisper
from telegram.ext import MessageHandler, filters

from command_router import register_command
from utils import load_json, save_json, safe_reply

# 加载模型
whisper_model = whisper.load_model("base")  # 可以换成 small, medium 更快/精度更高

CONFIG_PATH = "config_data/group_tts_config.json"

VOICE_MAP = {"女": "zh-CN-XiaoxiaoNeural", "男": "zh-CN-YunxiNeural"}

STYLE_MAP = {
    "温柔": "affectionate",
    "聊天": "chat",
    "播报": "newscast",
    "专业": "narration-professional",
    "严肃": "serious",
    "默认": "general",
}

RATE_MAP = {"慢": "-10%", "默认": "+120%", "快": "+50%", "很快": "+80%"}

PITCH_MAP = {"高": "+50Hz", "低": "-50Hz", "默认": "+0Hz"}

TEMP_AUDIO_DIR = "downloads/voice_tmp"
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)


def _bot_temp_path(
    context: ContextTypes.DEFAULT_TYPE, suffix: str, ext: str, file_id: str = ""
) -> str:
    bot_name = context.application.bot_data.get("name", "bot")
    safe_bot = bot_name.replace("/", "_")
    middle = f"{file_id}_" if file_id else ""
    name = f"{safe_bot}_{middle}{suffix}_{uuid.uuid4().hex}.{ext}"
    return os.path.join(TEMP_AUDIO_DIR, name)


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
            # "style": STYLE_MAP["默认"],
            "rate": RATE_MAP["默认"],
            "pitch": PITCH_MAP["默认"],
        },
    )


#  将文本转为语音，并发送语音消息 (基本)
async def tts_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip() or "你好，这是一个语音测试。"
    mp3_path = _bot_temp_path(context, "tts", "mp3")
    ogg_path = _bot_temp_path(context, "tts", "ogg")

    try:
        # 1. 使用 gTTS 生成语音
        tts = gTTS(text=text, lang="zh")
        tts.save(mp3_path)

        # 2. 转换为 .ogg（opus 编码） - Telegram 语音格式要求
        os.system(f'ffmpeg -i "{mp3_path}" -c:a libopus "{ogg_path}" -y')

        # 3. 发送语音
        with open(ogg_path, "rb") as voice_file:
            await update.message.reply_voice(voice=voice_file)
            # await update.message.reply_voice(voice=voice_file, caption="🎧 语音生成完毕")

    except Exception as e:
        await update.message.reply_text(f"❌ 语音生成失败：{e}")
    finally:
        # 4. 清理临时文件
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        if os.path.exists(ogg_path):
            os.remove(ogg_path)


async def set_group_tts_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ 请在群聊中使用此命令")
        return

    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "❗使用格式：/tts设置 [性别] [语速] [音调]\n例如：/tts设置 女 快 高"
        )
        return

    gender, speed, pitch = args
    if gender not in VOICE_MAP or speed not in RATE_MAP or pitch not in PITCH_MAP:
        await update.message.reply_text(
            "⚠️ 参数错误。\n性别：男/女\n风格：播报/聊天/温柔等\n语速：快/慢/默认\n音调：高/低/默认"
        )
        return

    chat_id = str(update.effective_chat.id)
    config = load_config()
    config[chat_id] = {
        "voice": VOICE_MAP[gender],
        # "style": STYLE_MAP[style],
        "rate": RATE_MAP[speed],
        "pitch": PITCH_MAP[pitch],
    }
    save_config(config)

    await update.message.reply_text(
        f"✅ 当前群语音设置完成：\n性别：{gender} 语速：{speed} 音调：{pitch}"
    )


async def show_group_tts_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    config = get_group_config(chat_id)

    gender = [k for k, v in VOICE_MAP.items() if v == config["voice"]][0]
    # style = [k for k, v in STYLE_MAP.items() if v == config["style"]][0]
    rate = [k for k, v in RATE_MAP.items() if v == config["rate"]][0]
    pitch = [k for k, v in PITCH_MAP.items() if v == config["pitch"]][0]

    await update.message.reply_text(
        f"🎙️ 当前群语音配置：\n性别：{gender} 语速：{rate} 音调：{pitch}"
    )


async def group_tts_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("⚠️ 请输入内容，例如：/tts 今天天气真好")
        return

    chat_id = str(update.effective_chat.id)
    config = get_group_config(chat_id)

    mp3_path = _bot_temp_path(context, "group_tts", "mp3")
    ogg_path = _bot_temp_path(context, "group_tts", "ogg")

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=config["voice"],
            # style=config["style"],
            rate=config["rate"],
            pitch=config["pitch"],
        )
        await communicate.save(mp3_path)

        os.system(
            f'ffmpeg -i "{mp3_path}" -c:a libopus -ar 24000 -ac 1 "{ogg_path}" -y'
        )

        with open(ogg_path, "rb") as f:
            await update.message.reply_voice(voice=f)
    except Exception as e:
        await update.message.reply_text(f"❌ 语音生成失败：{e}")
    finally:
        for f in [mp3_path, ogg_path]:
            if os.path.exists(f):
                os.remove(f)


async def voice_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 关掉语音识别
    if not False:
        return
    # voice = update.message.voice
    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text("⚠️ 请输入语音消息")
        return

    # 下载语音
    file = await context.bot.get_file(voice.file_id)
    ogg_path = _bot_temp_path(context, "voice_in", "ogg", voice.file_id)
    await file.download_to_drive(ogg_path)

    # 转成 wav（Whisper 更稳定识别 wav）
    wav_path = _bot_temp_path(context, "voice_in", "wav", voice.file_id)
    os.system(f'ffmpeg -i "{ogg_path}" -ar 16000 -ac 1 "{wav_path}" -y')

    try:
        result = whisper_model.transcribe(wav_path, language="zh")
        text = result["text"]
        await update.message.reply_text(f"📝 语音识别结果：{text}")
    except Exception as e:
        await update.message.reply_text(f"❌ 语音识别失败：{e}")
    finally:
        for f in [ogg_path, wav_path]:
            if os.path.exists(f):
                os.remove(f)


@register_command("语音识别")
async def command_voice_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # 必须是“回复某条消息”
    if not update.message.reply_to_message:
        # await update.message.reply_text("⚠️ 请回复一条语音消息，并输入指令")
        return

    replied_msg = update.message.reply_to_message

    # 被回复的必须是语音或音频
    voice = replied_msg.voice or replied_msg.audio
    if not voice:
        # await update.message.reply_text("⚠️ 你回复的不是语音消息")
        return

    # 下载语音
    file = await context.bot.get_file(voice.file_id)
    ogg_path = _bot_temp_path(context, "reply_voice_in", "ogg", voice.file_id)
    await file.download_to_drive(ogg_path)

    # 转成 wav
    wav_path = _bot_temp_path(context, "reply_voice_in", "wav", voice.file_id)
    os.system(f'ffmpeg -i "{ogg_path}" -ar 16000 -ac 1 "{wav_path}" -y')

    try:
        result = whisper_model.transcribe(wav_path, language="zh")
        text = result["text"]
        # await update.message.reply_text(f"📝 语音识别结果：\n{text}")
        await safe_reply(update, context, f"📝 语音：\n{text}")
    except Exception as e:
        # await update.message.reply_text(f"❌ 语音识别失败：{e}")
        await safe_reply(update, context, "识别失败")
    finally:
        for f in [ogg_path, wav_path]:
            if os.path.exists(f):
                os.remove(f)


def register_voice_handlers(application):
    application.add_handler(CommandHandler("tts", tts_voice_reply))
    application.add_handler(CommandHandler("settts", set_group_tts_config))
    application.add_handler(CommandHandler("showtts", show_group_tts_config))
    application.add_handler(CommandHandler("newtts", group_tts_voice))
    # 新增语音识别
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, voice_to_text)
    )
