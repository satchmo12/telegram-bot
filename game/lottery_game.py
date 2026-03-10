from datetime import datetime
import random
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import register_command
from utils import LOTTERY_FILE, apply_reward, format_reward_text, group_allowed, load_json, save_json
from info.economy import INFO_FILE, get_user_data, save_user_data


    
@register_command("抽奖")
async def lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # 加载抽奖记录，避免重复抽奖
    lottery_data = load_json(LOTTERY_FILE)
    chat_str = str(chat_id)
    user_str = str(user.id)

    if chat_str not in lottery_data:
        lottery_data[chat_str] = {}
    if today not in lottery_data[chat_str]:
        lottery_data[chat_str][today] = {}

    if user_str in lottery_data[chat_str][today]:
        await update.message.reply_text(f"🎰 {user.first_name}，你今天已经抽过奖了，明天再来！")
        return

    # 奖励池
    rewards = [
        {"text": "🎁 你获得了 {balance} 金币！", "balance": 50},
        {"text": "🎉 你获得了 {points} 积分！", "points": 5},
        {"text": "🍀 幸运女神眷顾你，幸运值 +{luck}", "luck": 10},
        {"text": "😢 啥也没抽到，下次好运！心情 {mood}", "mood": -3},
        {"text": "💰 恭喜中大奖！金币 +{balance}", "balance": 100},
        {"text": "🍀 幸运值提升 +{luck}！", "luck": 5},
        {"text": "💪 体力恢复 {stamina} 点", "stamina": 10},
        {"text": "✨ 魅力值增加 {charm} 点", "charm": 3},
        {"text": "😊 心情提升 {mood} 点", "mood": 5}
    ]

    reward = random.choice(rewards)

    # 记录今日抽奖
    lottery_data[chat_str][today][user_str] = format_reward_text(reward)
    save_json(LOTTERY_FILE, lottery_data)

    # 加载并更新用户数据
    user_data = get_user_data(chat_id, user.id)

    user_data = apply_reward(user_data, reward)

    save_user_data(chat_id, user.id, user_data) 

    msg = format_reward_text(reward)
    await update.message.reply_text(msg)

def register_lottery_handlers(app):
    app.add_handler(CommandHandler("lottery", lottery))