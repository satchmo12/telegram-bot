import logging
from info.economy import give_daily_stamina_to_all
from info.economy_bank import apply_interest, apply_loan_interest
from telegram.ext import CommandHandler, ContextTypes

from farm.farm_manager import auto_farm_tasks
from company.company_ipo import update_stock_prices_job
from chat.my_bot import ad_push_to, cleaned_word, speaking_to
from slave.guard_system import charge_guard_fees
from slave.pet_game import give_daily_stamina_to_all_pets
from game.red_packet import send_system_packet


async def daily_master_job(context):
    charge_guard_fees()
    logging.info("💪 执行每日保镖费用")
    give_daily_stamina_to_all()
    give_daily_stamina_to_all_pets()
    logging.info("💪 执行每日体力恢复任务角色/宠物")


async def hour_master_job(context: ContextTypes.DEFAULT_TYPE):
    apply_interest()
    apply_loan_interest()
    # await cleaned_word()
    logging.info("💪 执行每小时存款/贷款利息任务")


async def ten_minute_master_job(context: ContextTypes.DEFAULT_TYPE):
    await speaking_to(context)
    await ad_push_to(context)
    # 更新股价
    # await update_stock_prices_job(context)


async def five_minute_master_job(bot):
    # 管家
    await auto_farm_tasks(bot)


#  系统发送红包   #
async def system_packet_job(context: ContextTypes.DEFAULT_TYPE):
    await send_system_packet(context)
    logging.info("💪 执行发送系统红包")
