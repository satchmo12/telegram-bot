import time
from command_router import register_command
from info.economy import INFO_FILE, change_balance, get_user_data
from datetime import datetime
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import mention_html

from utils import (
    BANK_FILE,
    INTEREST_LOG_FILE,
    get_group_whitelist,
    group_allowed,
    load_json,
    safe_reply,
    save_json,
)

INTEREST_RATE = 0.01  # 每次计算利息为 1%
INTEREST_INTERVAL = 7200  # 每两小时发一次利息（单位：秒）

LOAN_INTEREST_RATE = 0.02  # 每小时贷款利率2%
MAX_LOAN_MULTIPLE = 5  # 最大贷款为余额的2倍
MAX_LOAN_NUM = 100 * 10000


@register_command("存款")
async def deposit_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    user_info = get_user_data(chat_id, user_id)

    if not context.args or not context.args[0].isdigit():
        return await safe_reply(update, context,"💰 用法：存款 金额")

    amount = int(context.args[0])

    if amount <= 0:
        return await safe_reply(update, context,"❌ 存入金额必须大于0。")

    if user_info["balance"] < amount:
        return await safe_reply(update, context,"❌ 你没有足够的金币。")

    change_balance(chat_id, user_id, -amount)

    # 存入银行
    data = load_json(BANK_FILE)
    user_cd = data.setdefault(str(chat_id), {}).setdefault(str(user_id), {})
    user_cd["bank_balance"] = user_cd.get("bank_balance", 0) + amount
    user_cd["last_interest_time"] = int(time.time())
    save_json(BANK_FILE, data)

    await safe_reply(update, context,f"🏦 已存入 {amount} 金币到银行。")


@register_command("我的存款")
async def check_bank_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)

    data = load_json(BANK_FILE)
    user_cd = data.setdefault(str(chat_id), {}).setdefault(str(user_id), {})
    bank = user_cd.get("bank_balance", 0)

    await safe_reply(update, context,f"🏦 我的存款 {bank} 金币")


# 利息计算
def apply_interest():
    bank_data = load_json(BANK_FILE)
    log_data = load_json(INTEREST_LOG_FILE)
    now = int(time.time())
    updated = 0

    for chat_id, users in bank_data.items():
        for user_id, user in users.items():
            bank = user.get("bank_balance", 0)
            last_time = user.get("last_interest_time", 0)

            if bank > 0 and now - last_time >= INTEREST_INTERVAL:
                periods = (now - last_time) // INTEREST_INTERVAL
                interest = int(bank * INTEREST_RATE * periods)

                if interest > 0:
                    user["bank_balance"] += interest
                    user["last_interest_time"] = last_time + periods * INTEREST_INTERVAL

                    log_data.setdefault(chat_id, {}).setdefault(user_id, []).append(
                        {"time": now, "interest": interest}
                    )
                    # 只保留最近 20 条记录
                    log_data[chat_id][user_id] = log_data[chat_id][user_id][-20:]
                    
                    updated += 1

    save_json(BANK_FILE, bank_data)
    save_json(INTEREST_LOG_FILE, log_data)

    print(f"[银行利息] ✅ 已为 {updated} 用户发放利息")


@register_command("查看利息")
async def show_interest_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    all_logs = load_json(INTEREST_LOG_FILE)
    log_data = all_logs.get(chat_id, {}).get(user_id, [])

    if not log_data:
        return await safe_reply(update, context,"暂无利息记录。")

    lines = [f"📈 最近利息记录："]
    for entry in log_data[-5:]:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["time"]))
        lines.append(f"🕒 {t}：+{entry['interest']} 金币利息")

    await safe_reply(update, context,"\n".join(lines))


@register_command("取款")
async def withdraw_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)

    if not context.args or not context.args[0].isdigit():
        return await safe_reply(update, context,"💰 用法：取款 金额")

    amount = int(context.args[0])
    if amount <= 0:
        return await safe_reply(update, context,"❌ 取出金额必须大于0。")

    data = load_json(BANK_FILE)
    user_cd = data.setdefault(str(chat_id), {}).setdefault(str(user_id), {})
    bank_balance = user_cd.get("bank_balance", 0)

    if bank_balance < amount:
        return await safe_reply(update, context,"❌ 你在银行的存款不足。")

    # 从银行扣款
    user_cd["bank_balance"] -= amount
    save_json(BANK_FILE, data)

    # 加回金币余额
    change_balance(chat_id, user_id, amount)

    await safe_reply(update, context,f"🏦 你已成功取出 {amount} 金币。")

@register_command("银行存款排行")
async def show_bank_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    data = load_json(BANK_FILE).get(chat_id, {})

    if not data:
        return await safe_reply(update, context,"暂无银行存款记录。")

    sorted_users = sorted(
        data.items(), key=lambda x: x[1].get("bank_balance", 0), reverse=True
    )
    lines = ["🏦 银行排行榜（前10名）："]

    for i, (user_id, info) in enumerate(sorted_users[:10], 1):
        name = get_user_data(chat_id, user_id).get("name", f"用户{user_id}")
        if is_silent:
            lines.append(f"{i}. {name or '用户'} - 💰 {info.get('bank_balance', 0)} 金币")
        else:
            mention = mention_html(user_id, name or "用户")
            lines.append(f"{i}. {mention} - 💰 {info.get('bank_balance', 0)} 金币")

    if is_silent:
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
    else:
        await update.message.reply_html("\n".join(lines), disable_web_page_preview=True)


@register_command("贷款")
async def take_loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    if not context.args or not context.args[0].isdigit():
        return await safe_reply(update, context,"💰 用法：贷款 [贷款金额]")

    loan_amount = int(context.args[0])
    if loan_amount <= 0:
        return await safe_reply(update, context,"❌ 贷款金额必须大于0。")

    data = load_json(BANK_FILE)
    user_data = data.setdefault(chat_id, {}).setdefault(user_id, {})

    bank_balance = user_data.get("bank_balance", 0)
    current_loan = user_data.get("loan_amount", 0)

    # 限制贷款总额不能超过余额的2倍
    max_loan = bank_balance * MAX_LOAN_MULTIPLE
    max_loan = min(MAX_LOAN_NUM, max_loan)
    if current_loan + loan_amount > max_loan:
        return await safe_reply(update, context,
            f"❌ 贷款总额不能超过你余额的{MAX_LOAN_MULTIPLE}倍（{max_loan}金币）最多贷款金额{MAX_LOAN_NUM}"
        )

    # 放款，增加用户金币余额和贷款记录
    change_balance(update.effective_chat.id, user_id, loan_amount)

    user_data["loan_amount"] = current_loan + loan_amount
    user_data["loan_start_time"] = int(time.time())
    save_json(BANK_FILE, data)

    await safe_reply(update, context,
        f"✅ 成功贷款 {loan_amount} 金币，请按时还款，避免利息增长。"
    )


@register_command("还款")
async def repay_loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    if not context.args or not context.args[0].isdigit():
        return await safe_reply(update, context,"💰 用法：还款 还款金额")

    repay_amount = int(context.args[0])
    if repay_amount <= 0:
        return await safe_reply(update, context,"❌ 还款金额必须大于0。")

    data = load_json(BANK_FILE)
    user_data = data.setdefault(chat_id, {}).setdefault(user_id, {})

    loan_amount = user_data.get("loan_amount", 0)
    bank_balance = user_data.get("bank_balance", 0)

    if loan_amount <= 0:
        return await safe_reply(update, context,"你当前没有贷款。")

    if bank_balance < repay_amount:
        return await safe_reply(update, context,"❌ 你在银行的余额不足以还款。")

    if repay_amount > loan_amount:
        repay_amount = loan_amount  # 不能还超过欠款

    # 扣除银行余额，减少贷款金额
    user_data["loan_amount"] = loan_amount - repay_amount
    user_data["bank_balance"] = bank_balance - repay_amount
    save_json(BANK_FILE, data)

    await safe_reply(update, context,
        f"✅ 成功还款 {repay_amount} 金币，剩余贷款 {user_data['loan_amount']} 金币。"
    )


def apply_loan_interest():
    bank_data = load_json(BANK_FILE)
    now = int(time.time())
    updated = 0

    for chat_id, users in bank_data.items():
        for user_id, user in users.items():
            loan_amount = user.get("loan_amount", 0)
            loan_start = user.get("loan_start_time", now)

            if loan_amount > 0:
                hours_passed = (now - loan_start) // 3600
                if hours_passed > 0:
                    # 计算利息
                    interest = int(loan_amount * LOAN_INTEREST_RATE * hours_passed)
                    user["loan_amount"] += interest
                    user["loan_start_time"] = loan_start + hours_passed * 3600
                    updated += 1

    save_json(BANK_FILE, bank_data)
    print(f"[贷款利息] ✅ 已为 {updated} 用户累计贷款利息。")


@register_command("我的欠款")
async def loan_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    data = load_json(BANK_FILE)
    user_data = data.get(chat_id, {}).get(user_id, {})

    loan_amount = user_data.get("loan_amount", 0)
    if loan_amount <= 0:
        return await safe_reply(update, context,"你当前没有贷款。")

    await safe_reply(update, context,
        f"💸 你的贷款余额为：{loan_amount} 金币，请及时还款。"
    )


def register_economy_bank_handlers(app):
    app.add_handler(CommandHandler("deposit", deposit_coins))
    app.add_handler(CommandHandler("withdraw", withdraw_coins))
    app.add_handler(CommandHandler("bankbalance", check_bank_balance))
    app.add_handler(CommandHandler("interestlog", show_interest_log))
    app.add_handler(CommandHandler("banktop", show_bank_top))

    # 新增贷款相关命令
    app.add_handler(CommandHandler("loan", take_loan))
    app.add_handler(CommandHandler("repay", repay_loan))
    app.add_handler(CommandHandler("loanstatus", loan_status))
