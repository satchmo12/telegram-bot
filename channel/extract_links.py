import re
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from command_router import register_command


@register_command("超链接")
async def extract_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.reply_to_message:
        await message.reply_text("请回复一条消息后使用 /超链接")
        return

    target = message.reply_to_message
    links = []

    # 1. 提取文字超链接，例如： [官网](https://example.com)
    entities = target.entities or []
    text = target.text or target.caption or ""

    for entity in entities:
        if entity.type == "text_link" and entity.url:
            links.append(entity.url)

        elif entity.type == "url":
            url = text[entity.offset:entity.offset + entity.length]
            if url:
                links.append(url)

    # 2. 有些媒体消息的 caption 使用 caption_entities
    caption_entities = target.caption_entities or []
    caption = target.caption or ""

    for entity in caption_entities:
        if entity.type == "text_link" and entity.url:
            links.append(entity.url)

        elif entity.type == "url":
            url = caption[entity.offset:entity.offset + entity.length]
            if url:
                links.append(url)

    # 3. 去重，保持原来的顺序
    links = list(dict.fromkeys(links))

    if not links:
        await message.reply_text("这条消息没有找到超链接。")
        return

    result = "\n".join(
        f"{index}. {url}"
        for index, url in enumerate(links, 1)
    )

    await message.reply_text(
        f"🔗 共找到 {len(links)} 个超链接：\n\n{result}",
        disable_web_page_preview=True,
    )

def register_extract_links_command(app):

    app.add_handler(CommandHandler("extract_links_command", extract_links_command))
   

    