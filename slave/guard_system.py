# game/guard_system.py
import time
from datetime import datetime
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import mention_html

from command_router import FEATURE_FRIENDS, feature_required, register_command
from utils import INFO_FILE, load_json, save_json, group_allowed, safe_reply
from info.economy import change_balance, get_balance


GUARD_COST_PER_DAY = 1000

# 雇佣保镖
@group_allowed
@register_command("雇佣保镖")
@feature_required(FEATURE_FRIENDS)
async def hire_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(INFO_FILE)
    user_data = data.setdefault(chat_id, {}).setdefault("users", {}).setdefault(user_id, {})

    if user_data.get("guard", {}).get("hired"):
        return await safe_reply(update, context,  "🛡 你已经雇佣了保镖！")

    if user_data.get("balance", 0) < GUARD_COST_PER_DAY:
        return await safe_reply(update, context,  f"💰 你没有足够金币支付首日保镖费用{GUARD_COST_PER_DAY} 金币）。")

    user_data["balance"] -= GUARD_COST_PER_DAY
    user_data["guard"] = {
        "level": 1,
        "hired": True,
        "last_paid": datetime.now().strftime("%Y-%m-%d")
    }

    save_json(INFO_FILE, data)
    await safe_reply(update, context,  f"✅ 你已成功雇佣保镖，当前等级：1，已支付首日工资{GUARD_COST_PER_DAY}金币 。")


@register_command("升级保镖")
@feature_required(FEATURE_FRIENDS)
async def upgrade_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(INFO_FILE)
    user_data = data.setdefault(chat_id, {}).setdefault("users", {}).setdefault(user_id, {})
    guard = user_data.get("guard", {})

    if not guard.get("hired"):
        return await safe_reply(update, context,  "🛡 你还没有雇佣保镖。")

    level = guard.get("level", 1)
    
    if(level >= 10):
        return await safe_reply(update, context,  "🛡 保镖已满级。")
    
    cost = level * 200

    if user_data.get("balance", 0) < cost:
        return await safe_reply(update, context,  f"💰 升级到等级 {level + 1} 需要 {cost} 金币，你的金币不足。")

    user_data["balance"] -= cost
    guard["level"] += 1
    save_json(INFO_FILE, data)

    await safe_reply(update, context,  f"🛡 保镖升级成功，当前等级：{guard['level']}，已扣除 {cost} 金币。")


@register_command("我的保镖")
@feature_required(FEATURE_FRIENDS)
async def my_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(INFO_FILE)
    user_data = data.get(chat_id, {}).get("users", {}).get(user_id, {})
    guard = user_data.get("guard", {})

    if not guard.get("hired"):
        return await safe_reply(update, context,  "🛡 你还没有雇佣保镖。")

    level = guard.get("level", 1)
    last_paid = guard.get("last_paid", "未知")
    await safe_reply(update, context,  f"🛡 保镖等级：{level}\n💸 上次付款日期：{last_paid}\n🪙 每日费用：{GUARD_COST_PER_DAY} 金币")


@register_command("解雇保镖")
@feature_required(FEATURE_FRIENDS)
async def fire_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(INFO_FILE)
    user_data = data.get(chat_id, {}).get("users", {}).get(user_id, {})
    guard = user_data.get("guard")

    if not guard or not guard.get("hired"):
        return await safe_reply(update, context, "🛡 你当前没有雇佣任何保镖。")

    guard["hired"] = False
    save_json(INFO_FILE, data)

    await safe_reply(
        update,
        context,
        f"🛡 你已解雇保镖。\n"
        f"📉 当前保镖等级已保留（Lv.{guard.get('level', 1)}），重新雇佣可继续使用。"
    )
    
# 每日定时扣工资（可接入 JobQueue 调用）
def charge_guard_fees():
    data = load_json(INFO_FILE)
    now = datetime.now().strftime("%Y-%m-%d")

    for chat_id, group_data in data.items():
        for user_id, user_data in group_data.get("users", {}).items():
            guard = user_data.get("guard")
            if guard and guard.get("hired"):
                last_paid = guard.get("last_paid")
                if last_paid != now:
                    if user_data.get("balance", 0) >= GUARD_COST_PER_DAY:
                        user_data["balance"] -= GUARD_COST_PER_DAY
                        guard["last_paid"] = now
                    else:
                        guard["hired"] = False  # 解雇保镖
    save_json(INFO_FILE, data)
    
def register_guard_handlers(app):
    app.add_handler(CommandHandler("hire_guard", hire_guard))
    app.add_handler(CommandHandler("upgrade_guard", upgrade_guard))
    app.add_handler(CommandHandler("my_guard", my_guard))
    app.add_handler(CommandHandler("fire_guard", fire_guard))  # ⭐ 新增