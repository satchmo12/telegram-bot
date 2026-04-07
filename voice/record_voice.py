import os
import uuid
import subprocess
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

from command_router import register_command

VOICE_SAMPLE_DIR = "voice_samples"

@register_command("收集声音")
async def record_voice_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户开始录制语音"""
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🎙️ 请发送你的语音消息，至少 30 秒，多条语音效果更好。\n"
        "发送完成后，可以使用 /voice_status 查看已收集的语音条数。"
    )

async def collect_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 打印消息类型调试
    print(f"收到消息类型：{type(update.message)}")
    print(f"voice: {update.message.voice}")
    print(f"audio: {update.message.audio}")
    print(f"video_note: {update.message.video_note}")
    print(f"text: {update.message.text}")

    voice = update.message.voice

    if not voice:
        await update.message.reply_text("⚠️ 这条消息不是语音，请发送语音消息")
        return  # 忽略非语音消息

    # 创建用户文件夹
    user_dir = os.path.join(VOICE_SAMPLE_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    # 生成唯一文件名
    file_id = str(uuid.uuid4())
    ogg_path = os.path.join(user_dir, f"{file_id}.ogg")
    wav_path = os.path.join(user_dir, f"{file_id}.wav")

    # 下载语音
    voice_file = await context.bot.get_file(voice.file_id)
    await voice_file.download_to_drive(ogg_path)

    # 转换为 wav
    try:
        result = subprocess.run(
            ['ffmpeg', '-i', ogg_path, '-ar', '22050', wav_path, '-y'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        if result.returncode != 0 or not os.path.exists(wav_path):
            await update.message.reply_text("⚠️ 语音保存失败，请重试")
            print(f"ffmpeg 转换失败：{result.stderr.decode()}")
            return
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

    await update.message.reply_text(f"✅ 已保存你的语音消息 ({voice.duration} 秒)")
    print(f"已保存文件：{wav_path}")
    
@register_command("查看声音")
async def voice_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看已收集语音数量"""
    user_id = update.effective_user.id
    user_dir = os.path.join(VOICE_SAMPLE_DIR, str(user_id))

    if not os.path.exists(user_dir):
        await update.message.reply_text("📭 你还没有发送任何语音消息")
        return

    wav_files = [f for f in os.listdir(user_dir) if f.endswith(".wav")]
    await update.message.reply_text(f"📊 已收集 {len(wav_files)} 条语音消息")

def register_record_voice(app): 
    # 注册命令和消息处理
    app.add_handler(CommandHandler("record_voice", record_voice_start))
    app.add_handler(CommandHandler("voice_status", voice_status))
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, collect_voice))