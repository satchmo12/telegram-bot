import random
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import register_command
from utils import ZHENXINHUA_LIST, group_allowed

@group_allowed
@register_command("真心话")
@register_command("大冒险")
async def start_zhenxinhua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"真心话：{random.choice(ZHENXINHUA_LIST)}")

def register_truth_handlers(app):
    app.add_handler(CommandHandler("zxh", start_zhenxinhua))