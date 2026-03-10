import random
from datetime import datetime
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import FEATURE_FRIENDS, feature_required, register_command
from config import CHARM_LIMIT, SPONSORED_STAMINA_SPEED, WORK_LIMIT
from info.economy import INFO_FILE, get_user_data, save_user_data
from slave.cooldown import is_on_cooldown
from company.economy_activity import (
    get_salary,
    get_work_count,
    increment_salary_count,
    increment_work_count,
)
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

sponsored_stamina_speed = SPONSORED_STAMINA_SPEED


@register_command("打工")
@feature_required(FEATURE_FRIENDS)
async def work_for_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id

    date_str = datetime.now().strftime("%Y-%m-%d")
    count = get_work_count(chat_id, user_id, date_str)

    if count >= WORK_LIMIT:
        return await safe_reply(
            update, context, f"🚫 你今天已经打工 {count} 次啦，休息一下吧！"
        )

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

    increment_work_count(chat_id, user_id, date_str)

    # log_coin_change(user_id, amount, "打工", f"在 {job} 赚取金币")

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


@register_command("劫色")
@feature_required(FEATURE_FRIENDS)
async def rob_for_charm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(
            update, context, "你劫色空气啊，要回复一个人才能开始行动！"
        )

    attacker = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    attacker_id = str(attacker.id)
    target_id = str(target.id)

    name = "劫色"
    on_cd, remain = is_on_cooldown(chat_id, attacker_id, name, cooldown_seconds=120)
    if on_cd:
        return await safe_reply(
            update, context, f"⌛ {name }冷却中，请 {remain} 秒后再试。"
        )

    if attacker_id == target_id:
        return await safe_reply(update, context, "你连自己都不放过？禁止自劫色！")

    attacker_data = get_user_data(chat_id, attacker_id)
    target_data = get_user_data(chat_id, target_id)

    if attacker_data.get("stamina", 100) <= 0:
        return await safe_reply(update, context, "💤 你已经精疲力尽，无法劫色！")

    if attacker_data.get("charm", 60) < 5:
        return await safe_reply(update, context, "💔 你的魅力太低了，劫色还没开始就结束了。")

    if target_data.get("charm", 60) <= 0:
        return await safe_reply(update, context, f"🙃 {target.full_name} 魅力见底，劫了个寂寞。")

    # 先扣除行动消耗
    attacker_data["stamina"] = max(0, attacker_data.get("stamina", 100) - 1)
    attacker_data["charm"] = max(0, attacker_data.get("charm", 60) - 5)

    # 劫色成功后会转移目标魅力
    if calculate_success(attacker_data.get("luck", 100), 0.25):
        target_charm = target_data.get("charm", 60)
        stolen_charm = random.randint(5, 15)
        stolen_charm = min(stolen_charm, target_charm)

        attacker_data["charm"] = attacker_data.get("charm", 60) + stolen_charm
        target_data["charm"] = max(0, target_charm - stolen_charm)
        target_data["luck"] = max(0, target_data.get("luck", 100) - 3)

        save_user_data(chat_id, attacker_id, attacker_data)
        save_user_data(chat_id, target_id, target_data)

        return await safe_reply(
            update,
            context,
            f"😈 你对 {target.full_name} 劫色成功，掠走 {stolen_charm} 点魅力，体力 -1，魅力净变化 {stolen_charm - 5:+d}。",
        )

    save_user_data(chat_id, attacker_id, attacker_data)
    return await safe_reply(update, context, "❌ 劫色失败！你灰溜溜地跑了，体力 -1，魅力 -5。")


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
    if user_info["stamina"] < sponsored_stamina_speed:
        return await safe_reply(update, context, random.choice(STAMINA_WARNINGS))

    job = random.choice(SPONSORED_ACTIONS)
    amount = random.randint(1000, 2000)
    date_str = datetime.now().strftime("%Y-%m-%d")
    increment_salary_count(chat_id, user_id, date_str, amount)

    # 消耗体力
    user_info["charm"] = max(0, user_info.get("charm", 60) - 10)
    user_info["stamina"] = max(
        0, user_info.get("stamina", 100) - sponsored_stamina_speed
    )
    user_info["balance"] = max(0, user_info.get("balance", 100) + amount)
    user_info["relationship_status"] = "包养中"

    save_user_data(chat_id, user_id, user_info)

    await safe_reply(
        update,
        context,
        f"💋 {user.first_name} 今天去「{job}」，成功获得金主赏识，赚到了 {amount} 枚金币 💰，但也累得虚脱，消耗了 {sponsored_stamina_speed} 点体力！",
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
    count = get_salary(chat_id, user_id, date_str)

    if count != 0:
        return await safe_reply(update, context, "你的金主爸爸今天已经打赏你了")

    amount = random.randint(1500, 2000)

    # 消耗体力
    user_info["stamina"] = max(
        0, user_info.get("stamina", 100) - sponsored_stamina_speed
    )
    user_info["balance"] = max(0, user_info.get("balance", 100) + amount)
    save_user_data(chat_id, user_id, user_info)

    increment_salary_count(chat_id, user_id, date_str, amount)

    return await safe_reply(
        update,
        context,
        f"💋 {user.first_name} 金主打赏了 {amount} 枚金币 💰，喊爸爸消耗了 {sponsored_stamina_speed} 点体力！",
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
        0, user_info.get("stamina", 100) - sponsored_stamina_speed
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
    app.add_handler(CommandHandler("robsex", rob_for_charm))
    app.add_handler(CommandHandler("sex", sex_for_money))
    app.add_handler(CommandHandler("getfree", free_for_money))
