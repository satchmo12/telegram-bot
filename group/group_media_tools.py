import asyncio
import os
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes


from command_router import register_command

MAX_FILE_SIZE = 15 * 1024 * 1024  # 15MB
SCALE_WIDTH = 256  # 想更小改 200 或 180
UNSPLASH_KEY = "rSbSZL9xoxY2_naIxQp6wRdCm_Dwd0xkUaypLWnXurU"

async def _run_ffmpeg(*args: str) -> tuple[bool, str]:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode == 0:
        return True, ""
    return False, stderr.decode("utf-8", errors="ignore")


@register_command("转贴纸")
async def gif_to_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    replied = msg.reply_to_message
    if not replied:
        await msg.reply_text("⚠️ 请回复一条 GIF 或图片")
        return

    if replied.sticker:
        await msg.reply_text("⚠️ 这条已经是贴纸了")
        return

    if replied.animation:
        file_id = replied.animation.file_id
        input_path = f"{file_id}.gif"
        output_path = f"{file_id}.webm"
        file = await context.bot.get_file(file_id)
        try:
            await file.download_to_drive(input_path)
            ok, _ = await _run_ffmpeg(
                "-i",
                input_path,
                "-vf",
                "scale=512:512:force_original_aspect_ratio=decrease",
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                "-an",
                "-t",
                "3",
                "-y",
                output_path,
            )
            if not ok:
                await msg.reply_text("⚠️ GIF 转贴纸失败，请稍后重试")
                return
            with open(output_path, "rb") as f:
                await context.bot.send_sticker(
                    chat_id=update.effective_chat.id, sticker=f
                )
        finally:
            if os.path.exists(input_path):
                os.remove(input_path)
            if os.path.exists(output_path):
                os.remove(output_path)
        return

    image = None
    if replied.photo:
        image = replied.photo[-1]
    elif replied.document and (replied.document.mime_type or "").startswith("image/"):
        image = replied.document

    if not image:
        await msg.reply_text("⚠️ 请回复一条 GIF 或图片")
        return

    file = await context.bot.get_file(image.file_id)
    input_path = f"{image.file_id}.img"
    output_path = f"{image.file_id}.webp"
    try:
        await file.download_to_drive(input_path)
        ok, _ = await _run_ffmpeg(
            "-i",
            input_path,
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease",
            "-vcodec",
            "libwebp",
            "-lossless",
            "0",
            "-q:v",
            "80",
            "-compression_level",
            "6",
            "-preset",
            "picture",
            "-an",
            "-y",
            output_path,
        )
        if not ok:
            await msg.reply_text("⚠️ 图片转贴纸失败，请稍后重试")
            return
        with open(output_path, "rb") as f:
            await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=f)
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)


@register_command("转表情")
async def sticker_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    replied = msg.reply_to_message
    if not replied:
        await msg.reply_text("⚠️ 请回复一条贴纸或图片")
        return

    # 分支1：贴纸 -> GIF
    if replied.sticker:
        sticker = replied.sticker

        file = await context.bot.get_file(sticker.file_id)

        input_path = f"{sticker.file_id}"
        output_path = f"{sticker.file_id}.gif"

        try:
            await file.download_to_drive(input_path)

            # 根据贴纸类型处理
            if sticker.is_animated:
                # webm 动画贴纸 -> gif
                ok, _ = await _run_ffmpeg(
                    "-i",
                    input_path,
                    "-vf",
                    "scale=512:512:force_original_aspect_ratio=decrease",
                    "-y",
                    output_path,
                )
            else:
                # 静态 webp -> gif
                ok, _ = await _run_ffmpeg(
                    "-i",
                    input_path,
                    "-vf",
                    "scale=512:512:force_original_aspect_ratio=decrease",
                    "-y",
                    output_path,
                )

            if not ok:
                await msg.reply_text("⚠️ 转换失败，请稍后重试")
                return

            with open(output_path, "rb") as f:
                await context.bot.send_animation(
                    chat_id=update.effective_chat.id,
                    animation=f,
                )

        finally:
            if os.path.exists(input_path):
                os.remove(input_path)
            if os.path.exists(output_path):
                os.remove(output_path)
        return

    # 分支2：图片 -> GIF（支持照片和图片文档）
    image = None
    if replied.photo:
        image = replied.photo[-1]
    elif replied.document and (replied.document.mime_type or "").startswith("image/"):
        image = replied.document

    if not image:
        await msg.reply_text("⚠️ 请回复一条贴纸或图片")
        return

    file = await context.bot.get_file(image.file_id)
    input_path = f"{image.file_id}.img"
    output_path = f"{image.file_id}.gif"

    try:
        await file.download_to_drive(input_path)
        ok, _ = await _run_ffmpeg(
            "-i",
            input_path,
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease",
            "-y",
            output_path,
        )
        if not ok:
            await msg.reply_text("⚠️ 图片转表情失败，请稍后重试")
            return

        with open(output_path, "rb") as f:
            await context.bot.send_animation(
                chat_id=update.effective_chat.id,
                animation=f,
            )
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
            

async def fetch_image(query: str) -> Optional[str]:
    url = "https://api.unsplash.com/photos/random"
    params = {
        "query": query,
        "client_id": UNSPLASH_KEY,
        "orientation": "portrait",
        "content_filter": "high",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            urls = data.get("urls", {})
            return urls.get("regular")


@register_command("看看腿")
async def send_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    try:
        img_url = await fetch_image("beautiful legs woman portrait")
        if not img_url:
            await update.message.reply_text("获取图片失败")
            return
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=img_url)
    except:
        await update.message.reply_text("获取图片失败")


    
def register_group_media_tools_handlers(app):
    app.add_handler(CommandHandler("sticker", gif_to_sticker))
    app.add_handler(CommandHandler("legs", send_image))
