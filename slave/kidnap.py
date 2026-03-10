import math
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from command_router import FEATURE_FRIENDS, feature_required, register_command
from info.economy_bank import BANK_FILE
from utils import load_json, save_json, safe_reply, group_allowed
from info.economy import INFO_FILE
from slave.cooldown import is_on_cooldown
import random

KIDNAP_PERCENTAGE = 0.3  # 清空银行余额比例
KIDNAP_COOLDOWN = 300  # 秒
# 绑架成功时，设置冷却时间
cooldown_seconds = 86400  # 1天
# 发起绑架
@group_allowed
@register_command("绑架")
@feature_required(FEATURE_FRIENDS)
async def kidnap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context,  "你要绑谁？请回复一条消息来选择目标。")

    attacker = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    attacker_id = str(attacker.id)
    target_id = str(target.id)
    
    
    name = "被绑架"
    on_cd, remain = is_on_cooldown(chat_id, target_id, f"{name}", cooldown_seconds=cooldown_seconds)
    if on_cd:
        return await safe_reply(update, context,  f"⌛ {target.full_name}刚被绑架，请 {remain} 秒后再试。")

    name = "绑架"
    on_cd, remain = is_on_cooldown(chat_id, attacker_id, name, cooldown_seconds=KIDNAP_COOLDOWN)
    if on_cd:
        return await safe_reply(update, context,  f"⌛ {name}冷却中，请 {remain} 秒后再试。")

    if attacker_id == target_id:
        return await safe_reply(update, context,  "你不能绑架自己！")

    data = load_json(INFO_FILE)
    attacker_data = data[chat_id]["users"][attacker_id]
    target_data = data[chat_id]["users"][target_id]

    # 判断体力和魅力
    if attacker_data.get("stamina", 100) < 2:
        return await safe_reply(update, context,  "😮‍💨 你体力不足，绑不动人了。")
    if attacker_data.get("charm", 60) < 20:
        return await safe_reply(update, context,  "🧟‍♂️ 你太猥琐了，没人愿意被你绑。")
    
    guard = target_data.get("guard", {})
    guard_level = guard.get("level", 0)
    protection_rate = min(guard_level * 0.1, 0.9)  # 最多抵消90%
    base_success_rate = 0.8  # 假设原始成功率
    final_success_rate = base_success_rate * (1 - protection_rate)

    if random.random() > final_success_rate:
        return await safe_reply(update, context,  f"🛡 对方保镖太厉害了，你绑架失败了！")

    # 扣除资源
    attacker_data["stamina"] -= 2
    attacker_data["charm"] -= 20

    # 加载银行数据
    bankdata = load_json(BANK_FILE)
    bank_user_data = bankdata.setdefault(chat_id, {}).setdefault(target_id, {})
    bank_balance = bank_user_data.get("bank_balance", 0)

    # 计算赎金
    balance = target_data.get("balance", 0)
    total = balance + bank_balance
    ransom = math.ceil(total * 0.2)
    
    if ransom < 0:
        return await safe_reply(update, context,  f"💰 {target.full_name} 太穷了，绑架失败。")

    if total < ransom:
        return await safe_reply(update, context,  f"💰 {target.full_name} 的金币不足以支付赎金（需要 {ransom}，目前共 {total}），绑架失败。")

    # 先扣余额
    if balance >= ransom:
        target_data["balance"] -= ransom
    else:
        # 不足部分从银行扣
        need_from_bank = ransom - balance
        target_data["balance"] = 0
        bank_balance -= need_from_bank
        bank_user_data["bank_balance"] = max(bank_balance, 0)

    # 赎金转给绑匪
    attacker_data["balance"] = attacker_data.get("balance", 0) + ransom

    save_json(INFO_FILE, data)
    save_json(BANK_FILE, bankdata)

    return await safe_reply(update, context,  f"🔫 你成功绑架了 {target.full_name} 并勒索了 {ransom} 金币！\n🎉 金币已转入你账户。")



def register_kinnap_handlers(app):
    app.add_handler(CommandHandler("kidnap", kidnap))

