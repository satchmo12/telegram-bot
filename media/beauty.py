from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from command_router import register_command
from media.pexels_service import KEYWORD_MAP, fetch_random_photo_url
from translate.my_deep_translator import to_english
from utils import safe_reply

@register_command("来个")
async def send_beauty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if not has_pexels_key():
    #     return await safe_reply(update, context, "⚠️ 未配置 PEXELS_API_KEY，无法使用来个命令。")

    query = " ".join(context.args).strip() if context.args else "asian girl"
    # 中文关键词映射
    if query in KEYWORD_MAP:
        query = KEYWORD_MAP[query]
    else:
        query = await translate_to_english(query) or "asian girl"
    url = await fetch_random_photo_url(query)

    if not url:
        return await safe_reply(update,context, "❌ 无法获取图片，请稍后重试")

    await context.bot.send_photo(
        chat_id=update.effective_chat.id, photo=url, caption=""
    )


async def translate_to_english(text):
    # 这里调用翻译API，返回英文文本
    # 伪代码示例：
    translated = await to_english(text)
    return translated


def get_beauty_handler(app):
    app.add_handler(CommandHandler("beauty", send_beauty))
