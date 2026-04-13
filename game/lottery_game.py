from datetime import datetime
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from command_router import register_command
from game.points_lottery_core import (
    draw_points_lottery,
    get_group_points_lottery,
    get_points_lottery_config,
    get_user_wins,
    list_prizes,
)
from info.economy import get_points
from utils import get_group_whitelist, safe_reply

CALLBACK_PREFIX = "plot"


def _lottery_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎁 开始抽奖", callback_data=f"{CALLBACK_PREFIX}:draw:{chat_id}:1"),
                InlineKeyboardButton("🎁 3连抽", callback_data=f"{CALLBACK_PREFIX}:draw:{chat_id}:3"),
            ],
            [
                InlineKeyboardButton("🎁 5连抽", callback_data=f"{CALLBACK_PREFIX}:draw:{chat_id}:5"),
                InlineKeyboardButton("🎁 10连抽", callback_data=f"{CALLBACK_PREFIX}:draw:{chat_id}:10"),
            ],
            [InlineKeyboardButton("我的中奖", callback_data=f"{CALLBACK_PREFIX}:my:{chat_id}")],
        ]
    )


def _format_panel(chat_id: str, cfg: dict) -> str:
    lottery_cfg = get_points_lottery_config(cfg)
    state = get_group_points_lottery(chat_id)
    prizes = list_prizes(chat_id)
    recent = state.get("recent_winners", [])
    lines = [
        "🎰 积分抽奖",
        f"状态：{'✅ 开启' if lottery_cfg['enabled'] else '🚫 关闭'}",
        f"单次消耗：{lottery_cfg['cost']} 积分",
        "",
        "🎁 奖池：",
    ]
    lines.append(f"- {escape(str(lottery_cfg.get('display_text', '') or '奖池丰厚，祝您好运。'))}")
    
    lines.append("")
    lines.append("🏆 最近中奖：")
    if recent:
        for item in recent[-10:]:
            ts = int(item.get("ts", 0) or 0)
            when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "--"
            lines.append(
                f"- {escape(str(item.get('user_name', '未知用户')))} 抽中 {escape(str(item.get('prize_name', '未知奖品')))} | {when}"
            )
    else:
        lines.append("- 暂无记录")
    return "\n".join(lines)


def _format_draw_result(user_name: str, draw_count: int, cost: int, current_points: int, results: list[dict]) -> str:
    lines = [
        f"🎰 {escape(user_name)} 进行了 {draw_count} 次积分抽奖",
        f"消耗积分：{cost}",
        f"剩余积分：{current_points}",
        "",
        "抽奖结果：",
    ]
    won = False
    for idx, item in enumerate(results, start=1):
        if item.get("win"):
            won = True
            lines.append(f"{idx}. 🎉 {escape(str(item.get('name', '未知奖品')))}")
        else:
            lines.append(f"{idx}. 谢谢参与")
    if won:
        lines.append("")
        lines.append("已记录到“我的中奖”。")
    return "\n".join(lines)


def _format_user_wins(chat_id: str, user_id: int) -> str:
    wins = get_user_wins(chat_id, user_id)
    if not wins:
        return ""
    lines = ["🎁 我的中奖："]
    for idx, item in enumerate(reversed(wins[-20:]), start=1):
        ts = int(item.get("ts", 0) or 0)
        when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "--"
        lines.append(f"{idx}. {escape(str(item.get('name', '未知奖品')))} | {when}")
    return "\n".join(lines)


@register_command("抽奖", "积分抽奖")
async def points_lottery_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    if update.effective_chat.type not in {"group", "supergroup"}:
        return await safe_reply(update, context, "请在群里发送“积分抽奖”。")
    chat_id = str(update.effective_chat.id)
    cfg = get_group_whitelist(context).get(chat_id, {})
    await update.message.reply_text(
        _format_panel(chat_id, cfg),
        reply_markup=_lottery_keyboard(chat_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def points_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return

    action = parts[1]
    chat_id = str(parts[2])
    cfg = get_group_whitelist(context).get(chat_id, {})

    if action == "my":
        user_wins_text = _format_user_wins(chat_id, query.from_user.id)
        if not user_wins_text:
            return await query.answer("🎁 你还没有中奖记录。", show_alert=False)
        await query.answer()
        return await query.message.reply_text(
            user_wins_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if action != "draw" or len(parts) < 4:
        return

    try:
        draw_count = int(parts[3])
    except Exception:
        return await query.answer("参数错误", show_alert=True)

    lottery_cfg = get_points_lottery_config(cfg)
    ok, err, results = draw_points_lottery(
        chat_id,
        query.from_user.id,
        query.from_user.full_name,
        draw_count,
        cfg,
    )
    if not ok:
        return await query.answer(err, show_alert=True)

    current_points = get_points(chat_id, query.from_user.id)
    await query.answer("抽奖完成", show_alert=False)
    return await query.message.reply_text(
        _format_draw_result(
            query.from_user.full_name,
            draw_count,
            lottery_cfg["cost"] * draw_count,
            current_points,
            results,
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def register_lottery_handlers(app):
    app.add_handler(CallbackQueryHandler(points_lottery_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
