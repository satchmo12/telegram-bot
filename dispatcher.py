import traceback
import asyncio
from chat.ai_chat import ai_auto_reply
from channel.channel_forwarder import handle_message
from command_router import dispatch_command
from config import AUTO_TRANSLATE
from group.check_for_ads import check_for_ads
from group.check_sacm import check_and_restrict_scam_user
from group.group_care import handle_text_message, watch_special_users
from group.group_logger import log_group
from group.grouplist import record_user
from game.qa_game import handle_qa_message
from group.talk_stats import count_message
from game.chengyu_game import handle_chengyu
from chat.my_bot import on_text
from translate.my_deep_translator import auto_translate
from slave.action_handler import apply_action
from utils import safe_reply
from feature_flags import is_feature_enabled
from channel.channel_config import handle_channel_config_text


from telegram import Update
from telegram.ext import ContextTypes

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 可以并行执行
    tasks = [
        check_and_restrict_scam_user(update, context),
        check_for_ads(update, context),
        log_group(update, context),
        watch_special_users(update, context),
    ]
    if is_feature_enabled(context.application, "channel_forward"):
        tasks.append(handle_message(update, context))
    await asyncio.gather(*tasks)

    await handle_text_dispatcher(update, context)


async def handle_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return
    # or
    await count_message(update, context)  # 消息统计（不统计表情）

    if not update.message.text:
        return

    text = update.message.text.strip()

    try:
        # 优雅分发命令
        if await dispatch_command(update, context):
            return
        # 频道配置引导输入
        if await handle_channel_config_text(update, context):
            return
        # 自定义命令
        if await apply_action(update, context):
            return

        if AUTO_TRANSLATE:
            translated = await auto_translate(text)
            if translated:
                # 示例行为：直接翻译并回复
                await safe_reply(
                    update, context, f"🈶 原文: {text}\n🌐 翻译: {translated}"
                )

        # ✅ 正常文本消息处理（非命令）
        await record_user(update, context)  # 记录用户（如入库）

        await handle_qa_message(update, context)  # 问答模块
        await handle_chengyu(update, context)  # 成语接龙模块
        
        await ai_auto_reply(update, context)  # ai问答

        await handle_text_message(update, context)  # 群聊天记录
        # 会中断后面的

        # await on_text(update, context)

    except Exception as e:
        print(f"[文本调度出错] {e}")
        traceback.print_exc()
