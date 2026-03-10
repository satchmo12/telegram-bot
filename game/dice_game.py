from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import register_command
from utils import group_allowed


@group_allowed
@register_command("骰子")
async def roll_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_dice(emoji="🎲")


def register_dice_handlers(app):
    app.add_handler(CommandHandler("roll", roll_dice))
