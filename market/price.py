from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from command_router import register_command
from market.crypto_price_service import (
    fetch_quote,
    format_quote_text,
    resolve_coin_id,
)
from utils import safe_reply

# === /price 单次查询命令 ===
@register_command("查询")
async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await safe_reply(update, context, "📌 用法：查询 币种，例如 查询 btc 或 查询 以太坊")

    coin_code = context.args[0]
    coin_id = resolve_coin_id(coin_code)
    quote = await fetch_quote(coin_id)
    if not quote:
        return await safe_reply(update, context, "❌ 无法获取该币种价格，检查输入是否正确。")
    await safe_reply(update, context, format_quote_text(f"📈 {coin_code.upper()} 当前价格：", quote))

# === rice 定时任务逻辑 ===

async def rice_command_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    coin_id = job_data.get("coin_id")
    chat_id = context.job.chat_id

    quote = await fetch_quote(coin_id)
    if not quote:
        return await context.bot.send_message(chat_id=chat_id, text=f"❌ 获取 {coin_id.upper()} 价格失败")
    await context.bot.send_message(
        chat_id=chat_id,
        text=format_quote_text(f"💰 {coin_id.upper()} 当前价格：", quote),
    )

# === /rice 启动推送（多个币支持） ===
@register_command("推送")
async def rice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        return await safe_reply(update, context, "📌 用法：推送 币种 [间隔分钟]，例如 推送 btc 5")

    coin_code = context.args[0]
    coin_id = resolve_coin_id(coin_code)

    try:
        interval_min = int(context.args[1]) if len(context.args) > 1 else 5
        interval_sec = max(60, interval_min * 60)
    except ValueError:
        return await safe_reply(update, context, "⚠️ 推送间隔必须为整数，单位为分钟")

    job_name = f"rice_{chat_id}_{coin_id}"

    # 删除旧任务（如有）
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    # 立即执行一次
    quote = await fetch_quote(coin_id)
    if not quote:
        return await safe_reply(update, context, f"❌ 无法获取 {coin_code.upper()} 价格")
    await safe_reply(update, context, format_quote_text(f"📈 {coin_code.upper()} 当前价格：", quote))

    # 添加定时任务
    context.job_queue.run_repeating(
        rice_command_job,
        interval=interval_sec,
        first=interval_sec,
        chat_id=chat_id,
        name=job_name,
        data={"coin_id": coin_id}
    )

    await safe_reply(update, context, f"✅ 已开始每 {interval_min} 分钟推送 {coin_id.upper()} 的价格。")

# === /stop_rice 停止推送 ===
@register_command("停止推送")
async def stop_rice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        return await safe_reply(
            update,
            context,
            "📌 用法：停止推送 币种名 或 all，例如 停止推送 btc 或 停止推送 all",
        )

    target = context.args[0].lower()

    if target == "all":
        count = 0
        for job in context.job_queue.jobs():
            if job.name.startswith(f"rice_{chat_id}_"):
                job.schedule_removal()
                count += 1
        if count == 0:
            return await safe_reply(update, context, "⚠️ 当前没有任何推送任务。")
        return await safe_reply(update, context, f"🛑 已停止你订阅的全部币种推送（共 {count} 个）")

    coin_id = resolve_coin_id(target)
    job_name = f"rice_{chat_id}_{coin_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)

    if not jobs:
        return await safe_reply(update, context, f"⚠️ 当前没有 {coin_id.upper()} 的推送任务。")

    for job in jobs:
        job.schedule_removal()

    await safe_reply(update, context, f"🛑 已停止 {coin_id.upper()} 的价格推送。")
    
    
@register_command("查币命令")
async def rice_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await safe_reply(
        update,
        context,
        "查币命令：查询 <币名>｜推送 <币名> [分钟]｜停止推送 <币名|all>\n例如：查询 btc、推送 比特币 5",
    )


# === 注册命令 ===
def register_price_handlers(app):
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("rice", rice_command))
    app.add_handler(CommandHandler("stop_rice", stop_rice_command))
