import random
from datetime import datetime
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import FEATURE_FRIENDS, feature_required, register_command
from info.economy import INFO_FILE, get_user_data, save_user_data
from slave.cooldown import is_on_cooldown
from slave.luck_helper import calculate_success
from slave.status_warnings import (
    CHARM_WARNINGS,
    JOB_ACTIONS,
    MARRIED_WARNINGS,
    SPONSORED_ACTIONS,
    SPONSORED_WARNINGS,
    SPONSORED_WROK_WARNINGS,
    STAMINA_WARNINGS,
)
from utils import safe_reply

SPONSORED_STAMINA_SPEED = 5
WORK_LIMIT = 10
CHARM_LIMIT = 50
ESCAPE_LIMIT = 0.25

@register_command("打工")
@feature_required(FEATURE_FRIENDS)
async def work_for_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id

    date_str = datetime.now().strftime("%Y-%m-%d")
  

    name = "打工"
    on_cd, remain = is_on_cooldown(chat_id, user_id, name, cooldown_seconds=120)
    if on_cd:
        return await safe_reply(
            update, context, f"⌛ {name }冷却中，请 {remain} 秒后再试。"
        )

    # 获取体力信息
    user_info = get_user_data(chat_id, user_id)

    if user_info["stamina"] <= 0:
        return await safe_reply(update, context, "💤 你已经精疲力尽，无法再工作了！")
    if user_info["relationship_status"] == "包养中":
        return await safe_reply(update, context, random.choice(SPONSORED_WROK_WARNINGS))

    job = random.choice(JOB_ACTIONS)
    amount = random.randint(100, 200)

    # 消耗体力
    user_info["stamina"] = max(0, user_info.get("stamina", 100) - 1)
    user_info["balance"] = max(0, user_info.get("balance", 100) + amount)

    # 保存
    save_user_data(chat_id, user_id, user_info)

    await safe_reply(
        update,
        context,
        f"🧹 {user.first_name} 今天去 {job}，赚到了 {amount} 枚金币，消耗了 1 点体力！",
    )


# 打劫
@register_command("打劫")
@feature_required(FEATURE_FRIENDS)
async def rob_for_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(
            update, context, "你打劫空气啊，要找个幸运儿回复一下，才能打劫！"
        )

    attacker = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    attacker_id = str(attacker.id)
    target_id = str(target.id)

    name = "打劫"
    on_cd, remain = is_on_cooldown(chat_id, attacker_id, name, cooldown_seconds=120)
    if on_cd:
        return await safe_reply(
            update, context, f"⌛ {name }冷却中，请 {remain} 秒后再试。"
        )

    if attacker_id == target_id:
        return await safe_reply(
            update, context, "畜生啊，自己都不放过，你不能打劫自己！"
        )

    attacker_data = get_user_data(chat_id, attacker_id)
    target_data = get_user_data(chat_id, target_id)

    # 检查体力
    if attacker_data.get("stamina", 100) <= 0:
        return await safe_reply(update, context, "💤 你已经精疲力尽，无法打劫！")

    # 检查魅力
    if attacker_data.get("charm", 60) < 10:
        return await safe_reply(
            update, context, "💸 你的魅力太低了，都不够打劫扣的，行动失败！"
        )

    # 检查目标是否有钱
    if target_data.get("balance", 100) <= 0:
        return await safe_reply(
            update,
            context,
            f"🙃 {target.full_name} 已经一贫如洗，打劫他也没用。你要饥劫色吗？",
        )

    # 扣除体力和魅力
    attacker_data["stamina"] = max(0, attacker_data.get("stamina", 100) - 1)
    attacker_data["charm"] = max(0, attacker_data.get("charm", 60) - 10)

    # 打劫成功
    if calculate_success(attacker_data["luck"], 0.2):

        target_balance = target_data.get("balance", 0)
        percentage = 0.3  # 抢劫比例（30%）
        amount = int(target_balance * percentage)
        amount = max(amount, 20)

        attacker_data["balance"] = attacker_data.get("balance", 100) + amount
        target_data["balance"] = max(0, target_data.get("balance", 100) - amount)
        target_data["luck"] = max(0, target_data.get("luck", 100) - 5)

        save_user_data(chat_id, attacker_id, attacker_data)
        save_user_data(chat_id, target_id, target_data)

        return await safe_reply(
            update,
            context,
            f"🎉 你成功打劫了 {target.full_name}，获得 {amount} 枚金币，魅力 -10，体力 -1。",
        )
    else:
        save_user_data(chat_id, attacker_id, attacker_data)
        return await safe_reply(
            update, context, f"❌ 打劫失败！你吓得瑟瑟发抖，魅力 -10，体力 -1。"
        )


@register_command("求包养")
@feature_required(FEATURE_FRIENDS)
async def sex_for_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id

    # 获取魅力信息
    user_info = get_user_data(chat_id, user_id)

    if user_info["relationship_status"] == "包养中":
        return await safe_reply(update, context, random.choice(SPONSORED_WARNINGS))
    elif user_info["relationship_status"] == "已婚":
        return await safe_reply(update, context, random.choice(MARRIED_WARNINGS))
    if user_info["charm"] <= CHARM_LIMIT:
        return await safe_reply(update, context, random.choice(CHARM_WARNINGS))
    if user_info["stamina"] < SPONSORED_STAMINA_SPEED:
        return await safe_reply(update, context, random.choice(STAMINA_WARNINGS))

    job = random.choice(SPONSORED_ACTIONS)
    amount = random.randint(1000, 2000)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 消耗体力
    user_info["charm"] = max(0, user_info.get("charm", 60) - 10)
    user_info["stamina"] = max(
        0, user_info.get("stamina", 100) - SPONSORED_STAMINA_SPEED
    )
    user_info["balance"] = max(0, user_info.get("balance", 100) + amount)
    user_info["relationship_status"] = "包养中"

    save_user_data(chat_id, user_id, user_info)

    await safe_reply(
        update,
        context,
        f"💋 {user.first_name} 今天去「{job}」，成功获得金主赏识，赚到了 {amount} 枚金币 💰，但也累得虚脱，消耗了 {SPONSORED_STAMINA_SPEED} 点体力！",
    )


# 领工资
@register_command("求打赏")
@feature_required(FEATURE_FRIENDS)
async def salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # 获取用户信息
    user_info = get_user_data(chat_id, user_id)

    if user_info["relationship_status"] != "包养中":
        return await safe_reply(update, context, "你还没有金主爸爸")

    date_str = datetime.now().strftime("%Y-%m-%d")
    count = 0

    if count != 0:
        return await safe_reply(update, context, "你的金主爸爸今天已经打赏你了")

    amount = random.randint(1500, 2000)

    # 消耗体力
    user_info["stamina"] = max(
        0, user_info.get("stamina", 100) - SPONSORED_STAMINA_SPEED
    )
    user_info["balance"] = max(0, user_info.get("balance", 100) + amount)
    save_user_data(chat_id, user_id, user_info)

    return await safe_reply(
        update,
        context,
        f"💋 {user.first_name} 金主打赏了 {amount} 枚金币 💰，喊爸爸消耗了 {SPONSORED_STAMINA_SPEED} 点体力！",
    )


# 停止包养
@register_command("自力更生", "停止包养")
@feature_required(FEATURE_FRIENDS)
async def free_for_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id

    # 获取用户信息
    user_info = get_user_data(chat_id, user_id)

    if user_info["relationship_status"] != "包养中":
        return await safe_reply(update, context, "你已经很独立了")

    # 消耗体力
    user_info["relationship_status"] = "单身"
    user_info["stamina"] = max(
        0, user_info.get("stamina", 100) - SPONSORED_STAMINA_SPEED
    )
    amount = 2000
    user_info["balance"] = user_info.get("balance", 100) - amount
    save_user_data(chat_id, user_id, user_info)

    return await safe_reply(
        update, context, f"💋 {user.first_name} 花了 {amount} 枚金币 💰。为自己赎身！"
    )

def register_work_handlers(app):
    app.add_handler(CommandHandler("work", work_for_money))
    app.add_handler(CommandHandler("rob", rob_for_money))
    app.add_handler(CommandHandler("sex", sex_for_money))
    app.add_handler(CommandHandler("getfree", free_for_money))
