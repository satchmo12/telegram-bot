import logging
import time
from info.economy import give_daily_stamina_to_all
from telegram.ext import CommandHandler, ContextTypes
from farm.farm_manager import auto_farm_tasks

from chat.my_bot import ad_push_to
from group.online_baby_scheduler import schedule_online_babies, schedule_teachers
from slave.guard_system import charge_guard_fees
from game.red_packet import send_system_packet


SLOW_JOB_SECONDS = 0.8


async def _run_timed_job(label: str, coroutine):
    """Log individual scheduled jobs that can delay the shared event loop."""
    started = time.perf_counter()
    try:
        return await coroutine
    finally:
        elapsed = time.perf_counter() - started
        if elapsed >= SLOW_JOB_SECONDS:
            print(f"[性能] 定时任务耗时 {elapsed:.3f}s: {label}")


async def daily_master_job(context):
    charge_guard_fees()
    logging.info("💪 执行每日保镖费用")
    give_daily_stamina_to_all()
    logging.info("💪 执行每日体力恢复任务角色/宠物")

async def hour_master_job(context: ContextTypes.DEFAULT_TYPE):
    pass

async def ten_minute_master_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_timed_job("定时广告", ad_push_to(context))
    await _run_timed_job("在线宝宝", schedule_online_babies(context))
    await _run_timed_job("老师列表", schedule_teachers(context))
    
async def five_minute_master_job(bot):
    # 管家
    await _run_timed_job("农场管家", auto_farm_tasks(bot))

#  系统发送红包   #
async def system_packet_job(context: ContextTypes.DEFAULT_TYPE):
    await send_system_packet(context)
    logging.info("💪 执行发送系统红包")
