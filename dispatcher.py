import asyncio
import time
import traceback

from channel.channel_forwarder import handle_message
from command_router import dispatch_command
from forward.message_forward import handle_text_private_message
from game.calculator import calculator_handler
from game.checkin import daycheckin
from game.lottery_game import points_lottery_panel
from game.talk_lottery_core import handle_talk_lottery
from group.check_for_ads import check_for_ads
from group.check_sacm import check_and_restrict_scam_user
from group.group_care import handle_text_message, watch_special_users
from group.group_logger import log_group
from group.grouplist import record_user
from game.qa_game import handle_qa_message
from group.talk_stats import count_message
from game.chengyu_game import handle_chengyu
from chat.my_bot import on_text
from chat.gemini_chat import handle_gemini_ai
from info.economy import my_points, top_points, top_richest
from translate.my_deep_translator import auto_translate
from slave.action_handler import apply_action
from utils import safe_reply
from feature_flags import is_feature_enabled
from channel.channel_config import handle_channel_config_text
from channel.telethon_login import handle_telethon_login_text
from group.ai_group_reply import ai_group_reply_handler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes


_BACKGROUND_TASKS = set()
SLOW_HANDLER_SECONDS = 0.8


async def _run_background_task(label: str, coroutine):
    """Run non-response work without delaying the user-facing message pipeline."""
    started = time.perf_counter()
    try:
        await coroutine
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[后台任务出错] {label}: {exc}")
        traceback.print_exc()
    finally:
        elapsed = time.perf_counter() - started
        if elapsed >= SLOW_HANDLER_SECONDS:
            print(f"[性能] 后台任务耗时 {elapsed:.3f}s: {label}")


def _schedule_background(label: str, coroutine):
    task = asyncio.create_task(_run_background_task(label, coroutine), name=f"bg:{label}")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 这些任务不决定普通消息/命令的回复内容。此前 asyncio.gather 会等待其中
    # 任意一次管理员接口或频道转发网络请求完成，导致用户感觉“机器人反应慢”。
    chat = update.effective_chat
    is_group_chat = bool(chat and chat.type in {"group", "supergroup"})
    background_tasks = []
    if is_group_chat:
        background_tasks.extend(
            [
                ("scam_check", check_and_restrict_scam_user(update, context)),
                ("ad_check", check_for_ads(update, context)),
                ("group_log", log_group(update, context)),
                ("special_follow", watch_special_users(update, context)),
                ("ai_reply", ai_group_reply_handler(update, context)),
            ]
        )
        if is_feature_enabled(context.application, "channel"):
            background_tasks.append(("channel_forward", handle_message(update, context)))
    for label, coroutine in background_tasks:
        _schedule_background(label, coroutine)

    started = time.perf_counter()
    try:
        await handle_text_dispatcher(update, context)
    finally:
        elapsed = time.perf_counter() - started
        if elapsed >= SLOW_HANDLER_SECONDS:
            print(f"[性能] 前台消息处理耗时 {elapsed:.3f}s")


async def handle_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return
    # or
    await count_message(update, context)  # 消息统计（不统计表情）

    if not update.message.text:
        return

    text = update.message.text.strip()
    
    # 仅主机器人：用户级 AI 对话（开启后无需 @）
    if await handle_gemini_ai(update, context):
        return

    if await handle_text(update, context):
        return

    try:
        # 优雅分发命令
        if await dispatch_command(update, context):
            return
        if is_feature_enabled(context.application, "channel"):
            # 协议号登录流程
            if await handle_telethon_login_text(update, context):
                return
            # 频道配置引导输入
            if await handle_channel_config_text(update, context):
                return
        # 自定义命令
        if await apply_action(update, context):
            return

        # if AUTO_TRANSLATE:
        #     translated = await auto_translate(text)
        #     if translated:
        #         # 示例行为：直接翻译并回复
        #         await safe_reply(
        #             update, context, f"🈶 原文: {text}\n🌐 翻译: {translated}"
        #         )

        # ✅ 正常文本消息处理（非命令）
        await record_user(update, context)  # 记录用户（如入库）

        await handle_qa_message(update, context)  # 问答模块
        await handle_chengyu(update, context)  # 成语接龙模块
        await handle_talk_lottery(update, context)  # 聊天抽奖
        
        await calculator_handler(update, context)  # 计算器
        # await handle_text_message(update, context)  # 聊天记录 (只记录私聊) 
        await handle_text_private_message(update, context)  # 聊天记录 (只记录私聊)
       
        # 会中断后面的

        # await on_text(update, context)

    except Exception as e:
        print(f"[文本调度出错] {e}")
        traceback.print_exc()
        
async def handle_text(update, context):

    text = update.message.text.strip()

    if text == "🎲积分抽奖":
        await points_lottery_panel(update, context)
        return True

    elif text == "📅每日签到":
        await daycheckin(update, context)
        return True

    elif text == "💰我的积分":
        await my_points(update, context)
        return True
    
    elif text == "🏆排行榜":
        await top_points(update, context)
        return True

    elif text == "招商负责人":
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("👉 点击联系", url="https://t.me/mr566")]]
        )
        await safe_reply(update, context, "点击按钮跳转：", reply_markup=reply_markup)
        return True


    return False
