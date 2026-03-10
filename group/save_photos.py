import os
from datetime import datetime
from telegram import Update
from telegram.ext import MessageHandler, filters, ContextTypes
 
# 保存图片的文件夹
PHOTO_SAVE_DIR = "photos"
os.makedirs(PHOTO_SAVE_DIR, exist_ok=True)

async def save_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    # 最大尺寸
    photo = update.message.photo[-1]

    # 用户信息
    user = update.message.from_user
    user_id = str(user.id) if user else "unknown"
    user_name = user.username or "unknown"
    user_fullname = user.full_name or "unknown"

    # 群信息
    chat = update.effective_chat
    bot_name = context.application.bot_data.get("name", "bot")
    if chat.type in ["group", "supergroup"]:
        chat_name = chat.title or "未知群"
    else:
        chat_name = "私聊"

    # 日期文件夹（按天分割）
    today = datetime.now().strftime("%Y-%m-%d")
    save_dir = os.path.join(PHOTO_SAVE_DIR, bot_name, chat_name, today, user_fullname)
    os.makedirs(save_dir, exist_ok=True)

    # 文件路径
    file_path = os.path.join(save_dir, f"{photo.file_id}.jpg")

    # 下载照片
    file = await context.bot.get_file(photo.file_id)
    await file.download_to_drive(custom_path=file_path)
    # print(f"📥 照片已保存: {file_path}")

    # 回复用户
    # await update.message.reply_text("✅ 照片已保存！")
    
# 注册
def register_save_photos_handlers(app):
    app.add_handler(MessageHandler(filters.PHOTO, save_photo))
