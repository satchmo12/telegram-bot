import asyncio
import time

from telegram import Update
from telegram.ext import (
    ContextTypes,
    MessageHandler,
    filters,
)

from command_router import register_command
from utils import is_admin
from game.virus_game import virus_manager


# ============================================================
# /病毒开始
# ============================================================

@register_command("病毒开始")
async def virus_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_chat:
        return

    if not update.message:
        return

    if not await is_admin(
        update,
        context
    ):
        return

    chat_id = update.effective_chat.id

    duration = None

    if context.args:

        try:
            duration = int(
                context.args[0]
            )
        except ValueError:
            duration = None

    result, data = virus_manager.start_game(
        chat_id,
        duration
    )

    if not result:

        await update.message.reply_text(
            data
        )

        return

    minimum = virus_manager.config.get(
        "min_active_players",
        5
    )

    duration = duration or virus_manager.config.get(
        "duration",
        30
    )

    await update.message.reply_text(
        "🦠 **病毒扩散游戏开始！**\n\n"
        f"⏱️ 游戏时间：{duration}分钟\n"
        f"👥 需要至少 {minimum} 名活跃玩家\n\n"
        "⚠️ 病毒目前还没有爆发。\n"
        "不要惊慌，请大家正常聊天。\n\n"
        "🧬 病毒会从游戏开始后发言的人中随机选择感染者。\n\n"
        "💊 病毒爆发后，机器人会随机发布神秘咒语。\n"
        "感染者直接发送正确咒语即可尝试解毒。",
        parse_mode="Markdown"
    )

    # ========================================================
    # 启动这个群自己的后台任务
    # ========================================================

    old_task = virus_manager._tasks.get(
        str(chat_id)
    )

    if old_task and not old_task.done():

        old_task.cancel()

    task = asyncio.create_task(
        virus_auto_finish(
            chat_id,
            context.application
        )
    )

    virus_manager._tasks[
        str(chat_id)
    ] = task


# ============================================================
# /病毒结束
# ============================================================

@register_command("病毒结束")
async def virus_stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_chat:
        return

    if not update.message:
        return

    if not await is_admin(
        update,
        context
    ):
        return

    chat_id = update.effective_chat.id

    game = virus_manager.stop_game(
        chat_id
    )

    if not game:

        await update.message.reply_text(
            "当前没有正在进行的病毒游戏。"
        )

        return

    task = virus_manager._tasks.get(
        str(chat_id)
    )

    if task and not task.done():

        task.cancel()

    virus_manager._tasks.pop(
        str(chat_id),
        None
    )

    await update.message.reply_text(
        "🛑 病毒游戏已手动结束。\n\n"
        "正在统计本局战绩……"
    )

    await send_result(
        chat_id,
        context
    )


# ============================================================
# /病毒状态
# ============================================================

@register_command("病毒状态")
async def virus_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    text = virus_manager.status_text(
        chat_id
    )

    if update.message:

        await update.message.reply_text(
            text
        )


# ============================================================
# 自动游戏任务
# ============================================================

async def virus_auto_finish(
    chat_id: int,
    application
):

    try:

        while True:

            await asyncio.sleep(5)

            game = virus_manager.get_game(
                chat_id
            )

            if not game:
                return

            if not game.get(
                "running",
                False
            ):
                return

            # =================================================
            # 游戏时间结束
            # =================================================

            # end_time = game.get(
            #     "end_time"
            # )

            # if end_time and time.time() >= end_time:

            #     virus_manager.stop_game(
            #         chat_id
            #     )

            #     await application.bot.send_message(
            #         chat_id=chat_id,
            #         text=(
            #             "⏰ **病毒游戏时间结束！**\n\n"
            #             "🦠 病毒扩散游戏结束。\n"
            #             "正在统计本局战绩……"
            #         ),
            #         parse_mode="Markdown"
            #     )

            #     await send_result(
            #         chat_id,
            #         application
            #     )

            #     return

            # =================================================
            # 病毒被消灭
            # =================================================

            if virus_manager.should_end_game(
                chat_id
            ):

                virus_manager.stop_game(
                    chat_id
                )

                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🎉 **病毒已经被消灭！**\n\n"
                        "🦠 场上已经没有任何感染者。\n"
                        "所有感染者都已经痊愈、服用解药或阵亡。\n\n"
                        "🏆 本局提前结束！"
                    ),
                    parse_mode="Markdown"
                )

                await send_result(
                    chat_id,
                    application
                )

                return

            # =================================================
            # 自动产生神秘咒语
            #
            # check_antidote_trigger 内部会再次检查：
            #
            # running == True
            # phase == running
            #
            # 所以 waiting 阶段绝对不会产生咒语
            # =================================================

            antidote = virus_manager.check_antidote_trigger(
                chat_id
            )

            if not antidote:
                continue

            text = antidote.get(
                "text"
            )

            if not text:
                continue

            duration = virus_manager.config.get(
                "antidote",
                {}
            ).get(
                "duration",
                60
            )

            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🧪 **神秘咒语出现！**\n\n"
                    "🔮 神秘咒语：\n"
                    f"「{text}」\n\n"
                    "💊 这可能是传说中的解药！\n"
                    "感染者请直接发送正确的咒语。\n\n"
                    f"⏳ 有效时间：{duration} 秒\n"
                    "⚠️ 解药只有一份，先到先得！"
                ),
                parse_mode="Markdown"
            )

    except asyncio.CancelledError:

        return

    except Exception as e:

        print(
            "[virus_auto_finish] "
            f"chat_id={chat_id}, "
            f"error={e}"
        )


# ============================================================
# 解药处理
#
# 玩家直接发送：
#
# 哈哈哈哈
#
# 不需要回复机器人消息
# ============================================================

async def virus_antidote_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if chat.type not in (
        "group",
        "supergroup"
    ):
        return

    if user.is_bot:
        return

    chat_id = chat.id

    if not virus_manager.is_running(
        chat_id
    ):
        return

    text = (
        message.text or ""
    ).strip()

    if not text:
        return

    # ========================================================
    # 尝试直接使用当前咒语
    # ========================================================

    result = virus_manager.use_antidote(
        chat_id,
        user.id,
        text
    )

    if not result.get(
        "success"
    ):
        return

    # ========================================================
    # 解毒成功
    # ========================================================

    await message.reply_text(
        "💊 **解毒成功！**\n\n"
        f"{user.full_name} "
        "成功服用神秘解药！\n\n"
        "🦠 病毒已清除。\n"
        "❤️ 当前状态：健康",
        parse_mode="Markdown"
    )

    # ========================================================
    # 解毒后已经没有感染者
    # ========================================================

    if virus_manager.should_end_game(
        chat_id
    ):

        virus_manager.stop_game(
            chat_id
        )

        await application_send_game_end(
            chat_id,
            context
        )


# ============================================================
# 普通群消息
# ============================================================

async def virus_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if chat.type not in (
        "group",
        "supergroup"
    ):
        return

    if user.is_bot:
        return

    chat_id = chat.id

    if not virus_manager.is_running(
        chat_id
    ):
        return

    # ========================================================
    # 解毒咒语优先
    #
    # 如果这条消息已经成功解毒，
    # 不再进入病毒传播逻辑。
    #
    # 这里再次检查是为了避免 Handler 顺序问题。
    # ========================================================

    text = (
        message.text or ""
    ).strip()

    if text:

        antidote_result = (
            virus_manager.use_antidote(
                chat_id,
                user.id,
                text
            )
        )

        if antidote_result.get(
            "success"
        ):

            await message.reply_text(
                "💊 **解毒成功！**\n\n"
                f"{user.full_name} "
                "成功服用神秘解药！\n\n"
                "🦠 病毒已清除。\n"
                "❤️ 当前状态：健康",
                parse_mode="Markdown"
            )

            if virus_manager.should_end_game(
                chat_id
            ):

                virus_manager.stop_game(
                    chat_id
                )

                await application_send_game_end(
                    chat_id,
                    context
                )

            return

    # ========================================================
    # 记录活跃玩家
    # ========================================================

    virus_manager.register_active_player(
        chat_id,
        user
    )

    # ========================================================
    # 尝试病毒爆发
    # ========================================================

    selected = virus_manager.try_start_infection(
        chat_id
    )

    if selected:

        await message.reply_text(
            "🚨 **病毒爆发！**\n\n"
            f"🦠 初始感染者："
            f"{selected['name']}\n\n"
            "📌 初始痊愈条件："
            "传播给 2 名不同成员。\n"
            "⚠️ 如果之后被新的感染源再次感染，"
            "痊愈所需传播人数会 +1。\n"
            "☠️ 被第 3 个不同感染源感染时，"
            "将直接死亡。",
            parse_mode="Markdown"
        )

    # ========================================================
    # 只有回复别人的消息才传播
    # ========================================================

    if not message.reply_to_message:
        return

    reply = message.reply_to_message

    target = reply.from_user

    if not target:
        return

    # ========================================================
    # 不允许回复机器人传播
    # ========================================================

    if target.is_bot:
        return

    # ========================================================
    # 自己回复自己
    # ========================================================

    if target.id == user.id:
        return

    # ========================================================
    # 传播
    # ========================================================

    result = virus_manager.spread(
        chat_id,
        user.id,
        target,
        message.message_id
    )

    if not result.get(
        "success"
    ):
        return

    # ========================================================
    # 目标死亡
    # ========================================================

    if result.get(
        "died"
    ):

        await message.reply_text(
            "☠️ **病毒致命反应！**\n\n"
            f"{target.full_name} "
            "已经被 3 名不同感染源感染！\n\n"
            "💀 **已阵亡！**\n"
            "本局无法继续传播。",
            parse_mode="Markdown"
        )

    else:

        target_sources = result.get(
            "target_infection_sources",
            0
        )

        target_required = result.get(
            "target_recovery_required",
            0
        )

        target_progress = result.get(
            "target_recovery_progress",
            0
        )

        source_progress = result.get(
            "recovery_progress",
            0
        )

        source_required = result.get(
            "recovery_required",
            0
        )

        await message.reply_text(
            "🦠 **感染成功！**\n\n"
            f"{target.full_name} 已感染病毒。\n"
            f"感染源：{user.full_name}\n\n"
            f"🎯 {target.full_name} 当前感染源："
            f"{target_sources} 个\n"
            f"💊 痊愈要求："
            f"{target_required} 次传播\n"
            f"📈 当前进度："
            f"{target_progress}/{target_required}\n\n"
            f"🧬 {user.full_name} 痊愈进度："
            f"{source_progress}/{source_required}",
            parse_mode="Markdown"
        )

    # ========================================================
    # 传播者痊愈
    # ========================================================

    if result.get(
        "recovered"
    ):

        await message.reply_text(
            f"💊 **{user.full_name} 痊愈！**\n\n"
            f"已经完成 "
            f"{result.get('recovery_required', 0)} "
            "次有效传播任务。\n"
            "病毒已从体内清除。",
            parse_mode="Markdown"
        )

    # ========================================================
    # 如果已经没有感染者
    # ========================================================

    if virus_manager.should_end_game(
        chat_id
    ):

        virus_manager.stop_game(
            chat_id
        )

        await application_send_game_end(
            chat_id,
            context
        )


# ============================================================
# 游戏提前结束
# ============================================================

async def application_send_game_end(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE
):

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎉 **病毒已经被消灭！**\n\n"
            "🦠 场上已经没有任何感染者。\n"
            "🏆 本局提前结束！\n\n"
            "正在统计本局病毒战绩……"
        ),
        parse_mode="Markdown"
    )

    await send_result(
        chat_id,
        context
    )


# ============================================================
# 结算
# ============================================================

async def send_result(
    chat_id,
    context
):

    result = virus_manager.build_result(
        chat_id
    )

    if not result:
        return

    stats = result.get(
        "stats",
        {}
    )

    players = result.get(
        "players",
        []
    )

    text = (
        "🦠━━━━━━━━━━━━━━🦠\n"
        "       病毒扩散结束\n"
        "🦠━━━━━━━━━━━━━━🦠\n\n"
        f"🧬 总感染次数："
        f"{stats.get('total_infections', 0)}\n"
        f"👥 总传播次数："
        f"{stats.get('total_spreads', 0)}\n"
        f"💀 阵亡人数："
        f"{stats.get('total_deaths', 0)}\n"
        f"🧪 解药出现："
        f"{stats.get('total_antidotes', 0)}次\n"
        f"💊 解药使用："
        f"{stats.get('total_antidote_uses', 0)}次\n\n"
        "👑 **本局毒王排行榜**\n\n"
    )

    for index, player in enumerate(
        players,
        1
    ):

        score = (
            player.get(
                "spread_count",
                0
            )
            + player.get(
                "infection_count",
                0
            ) * 2
            + player.get(
                "death_count",
                0
            ) * 10
        )

        text += (
            f"{index}. "
            f"{player.get('name', '未知玩家')}\n"
            f"   🦠 感染："
            f"{player.get('infection_count', 0)}\n"
            f"   🔄 传播："
            f"{player.get('spread_count', 0)}\n"
            f"   💀 造成阵亡："
            f"{player.get('death_count', 0)}\n"
            f"   💊 使用解药："
            f"{player.get('antidote_discover_count', 0)}\n"
            f"   ⭐ 毒王指数："
            f"{score}\n\n"
        )

    if players:

        winner = players[0]

        text += (
            "👑━━━━━━━━━━━━━━👑\n"
            f"      👑 毒王："
            f"{winner.get('name', '未知玩家')}\n"
            "👑━━━━━━━━━━━━━━👑"
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown"
    )

    virus_manager.remove_game(
        chat_id
    )


# ============================================================
# Handler 注册
# ============================================================

def register_virus_handlers(
    application
):

    # ========================================================
    # 解药 Handler
    #
    # 玩家直接发送咒语即可
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND,
            virus_antidote_handler
        ),
        group=0
    )

    # ========================================================
    # 普通病毒 Handler
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND,
            virus_message_handler
        ),
        group=1
    )