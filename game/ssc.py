# ssc.py

import requests
from datetime import datetime
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue, Job
from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus

from command_router import register_command


# 示例使用 Steam 上 CSGO 的 AppID
APP_ID = 730

def get_online_players(app_id=APP_ID):
    """获取 Steam 某游戏当前在线人数"""
    url = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        return data["response"]["player_count"]
    except Exception as e:
        print(f"获取在线人数失败: {e}")
        return None


def generate_ssc_result(player_count: int) -> str:
    """从在线人数生成时时彩开奖号码（取最后5位，每位对10）"""
    last_five = str(player_count)[-5:].zfill(5)
    return "".join(str(int(d) % 10) for d in last_five)


def get_ssc_draw_result():
    """获取完整开奖信息"""
    count = get_online_players()
    if count is None:
        return "❌ 获取在线人数失败，暂时无法开奖。"

    result = generate_ssc_result(count)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"📅 时间：{now}\n👥 当前在线人数：{count}\n🎯 开奖号码：<b>{result}</b>"


async def auto_push_ssc(context: ContextTypes.DEFAULT_TYPE):
    """定时自动推送SSC开奖结果"""
    result = get_ssc_draw_result()

    await context.bot.send_message(
        chat_id=context.job.chat_id, text=result, parse_mode="HTML"
    )

@register_command("开启开奖")
async def start_auto_ssc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开启自动开奖"""
    chat_id = str(update.effective_chat.id)
    job = context.job_queue.run_repeating(
        auto_push_ssc, chat_id=chat_id, interval=300, first=0
    )  # 每 5 分钟
    context.chat_data["ssc_job"] = job
    await update.message.reply_text("✅ SSC自动开奖已启动，每5分钟推送一次。")

@register_command("停止开奖")
async def stop_auto_ssc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """停止自动开奖"""
    job = context.chat_data.get("ssc_job")
    if job:
        job.schedule_removal()
        await update.message.reply_text("🛑 SSC自动开奖已停止。")
    else:
        await update.message.reply_text("⚠️ 当前没有运行中的 SSC 自动开奖任务。")
     
def get_ssc_handler(app):
    app.add_handler(CommandHandler("startssc", start_auto_ssc))
    app.add_handler(CommandHandler("stopssc", stop_auto_ssc))
