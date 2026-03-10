# 彩票功能
import asyncio
import datetime
import random
from typing import Dict, List, Tuple

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from command_router import register_command
from info.economy import change_balance, get_balance, get_user_data, save_user_data
from utils import GROUP_LIST_FILE, delete_later, load_json, safe_reply, save_json

LOTTERY_FILE = "data/lottery_data.json"

BET_OPTIONS = {"大", "小", "单", "双", "龙", "虎", "和"}
MAX_HISTORY_ROUNDS = 30

# 赔率
ODDS = {
    "龙": 2,
    "虎": 2,
    "和": 9,
    "大": 2,
    "小": 2,
    "单": 2,
    "双": 2,
    "number": 100,  # 数字完全命中
    "last": 10,  # 数字末位命中
}


def get_current_round() -> str:
    now = datetime.datetime.now()
    return now.strftime("%Y%m%d-%H") + f"{now.minute:02d}"


def get_dragon_tiger_result(number: int) -> str:
    ten = number // 10
    unit = number % 10
    if ten > unit:
        return "龙"
    if unit > ten:
        return "虎"
    return "和"


def default_chat_state() -> Dict:
    return {"current_round": "", "tickets": [], "history": {}}


def ensure_chat_lottery(data: Dict, chat_id: str) -> Dict:
    state = data.setdefault(chat_id, default_chat_state())
    state.setdefault("history", {})
    state.setdefault("tickets", [])
    state.setdefault("current_round", get_current_round())
    return state


def reset_if_round_changed(state: Dict):
    round_id = get_current_round()
    if state["current_round"] != round_id:
        state["current_round"] = round_id
        state["tickets"] = []


def parse_bet(args: List[str]) -> Tuple[str, str, int]:
    if len(args) < 2:
        raise ValueError("用法：彩票购买/下注 <号码/大小/单双/龙虎和> <金额>")

    bet_choice = args[0]
    try:
        bet_amount = int(args[1])
    except ValueError as exc:
        raise ValueError("下注金额必须是整数。") from exc

    if bet_amount <= 0:
        raise ValueError("下注金额必须大于 0。")

    if bet_choice.isdigit():
        num = int(bet_choice)
        if not (0 <= num <= 99):
            raise ValueError("号码范围必须是 0~99。")
        return "number", bet_choice, bet_amount

    if bet_choice in BET_OPTIONS:
        return bet_choice, bet_choice, bet_amount

    raise ValueError("只能下注 数字 或 大/小/单/双/龙/虎/和。")


def calc_reward(ticket: Dict, winning_num: int, dragon_tiger: str) -> int:
    choice = ticket["choice"]
    bet_type = ticket["bet_type"]
    bet_amount = ticket["bet_amount"]

    if bet_type == "number":
        if int(choice) == winning_num:
            return bet_amount * ODDS["number"]
        if choice[-1] == str(winning_num)[-1]:
            return bet_amount * ODDS["last"]
        return 0

    if bet_type == "大" and winning_num >= 50:
        return bet_amount * ODDS["大"]
    if bet_type == "小" and winning_num < 50:
        return bet_amount * ODDS["小"]
    if bet_type == "单" and winning_num % 2 == 1:
        return bet_amount * ODDS["单"]
    if bet_type == "双" and winning_num % 2 == 0:
        return bet_amount * ODDS["双"]
    if bet_type in {"龙", "虎", "和"} and choice == dragon_tiger:
        return bet_amount * ODDS[choice]

    return 0


@register_command("彩票购买", "下注")
async def buy_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    try:
        bet_type, bet_choice, bet_amount = parse_bet(context.args)
    except ValueError as e:
        return await safe_reply(update, context, str(e))

    data = load_json(LOTTERY_FILE)
    lottery_data = ensure_chat_lottery(data, chat_id)
    reset_if_round_changed(lottery_data)
    round_id = lottery_data["current_round"]

    if get_balance(chat_id, user_id) < bet_amount:
        return await safe_reply(update, context, "❌ 你的金币不足以购买彩票。")

    lottery_data["tickets"].append(
        {
            "user_id": user_id,
            "bet_type": bet_type,
            "choice": bet_choice,
            "bet_amount": bet_amount,
        }
    )
    change_balance(chat_id, user_id, -bet_amount)
    save_json(LOTTERY_FILE, data)

    await safe_reply(
        update,
        context,
        f"🎟 {user.full_name} 下注 {bet_choice} {bet_amount} 金币，期号 {round_id}",
    )


async def draw_lottery(context: ContextTypes.DEFAULT_TYPE):
    data = load_json(LOTTERY_FILE)
    group_list = load_json(GROUP_LIST_FILE)

    winning_num = random.randint(0, 99)
    winning_str = str(winning_num)
    dragon_tiger_result = get_dragon_tiger_result(winning_num)

    for chat_id in group_list.keys():
        lottery_data = ensure_chat_lottery(data, chat_id)
        round_id = lottery_data["current_round"]
        tickets = lottery_data.get("tickets", [])

        history_round = []
        msg_lines = [
            f"🎰 第 {round_id} 期开奖！\n中奖号码：{winning_str} ({dragon_tiger_result})"
        ]

        for ticket in tickets:
            uid = ticket["user_id"]
            reward = calc_reward(ticket, winning_num, dragon_tiger_result)
            win = reward > 0
            group_cfg = group_list.get(str(chat_id), {}) if isinstance(group_list, dict) else {}
            is_silent = bool(group_cfg.get("silent", False))

            user_info = get_user_data(chat_id, uid)
            display_name = user_info.get("name") or uid
            choice = ticket["choice"]
            bet_amount = ticket["bet_amount"]

            if win:
                user_info["balance"] += reward
                save_user_data(chat_id, uid, user_info)
                if is_silent:
                    msg_lines.append(
                        f"🎉 {display_name} 下注 {choice} ({bet_amount}💰) 获得 {reward}💰"
                    )
                else:
                    msg_lines.append(
                        f"🎉 [{display_name}](tg://user?id={uid}) 下注 {choice} ({bet_amount}💰) 获得 {reward}💰"
                    )
            else:
                if is_silent:
                    msg_lines.append(
                        f" - {display_name} 下注 {choice} ({bet_amount}💰) 未中奖"
                    )
                else:
                    msg_lines.append(
                        f" - [{display_name}](tg://user?id={uid}) 下注 {choice} ({bet_amount}💰) 未中奖"
                    )

            history_round.append(
                {
                    "user_id": uid,
                    "bet_type": ticket["bet_type"],
                    "choice": choice,
                    "bet_amount": bet_amount,
                    "win": win,
                    "payout": reward,
                }
            )

        lottery_data["history"][round_id] = {
            "winning": winning_str,
            "tickets": history_round,
        }
        while len(lottery_data["history"]) > MAX_HISTORY_ROUNDS:
            oldest = sorted(lottery_data["history"].keys())[0]
            del lottery_data["history"][oldest]

        lottery_data["tickets"] = []
        lottery_data["current_round"] = get_current_round()
        save_json(LOTTERY_FILE, data)

        try:
            msg = await context.bot.send_message(
                chat_id=int(chat_id), text="\n".join(msg_lines), parse_mode="Markdown"
            )
            asyncio.create_task(delete_later(msg, delay=60))
        except Exception as e:
            print(f"发送群消息失败: {chat_id}, {e}")


@register_command("我的彩票", "我的下注")
async def my_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    lottery_data = load_json(LOTTERY_FILE).get(chat_id)
    if not lottery_data or not lottery_data.get("tickets"):
        return await safe_reply(update, context, "你本期没有购买彩票。")

    tickets = [
        f"{t['choice']} ({t['bet_amount']}💰)"
        for t in lottery_data["tickets"]
        if t["user_id"] == user_id
    ]
    if not tickets:
        return await safe_reply(update, context, "你本期没有购买彩票。")

    await safe_reply(
        update,
        context,
        f"🎟 你本期购买：{', '.join(tickets)} (期号 {lottery_data['current_round']})",
    )


@register_command("开奖记录", "历史开奖")
async def lottery_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    lottery_data = load_json(LOTTERY_FILE).get(chat_id)
    if not lottery_data or not lottery_data.get("history"):
        return await safe_reply(update, context, "暂无开奖记录。")

    msg_lines = ["📜 最近彩票开奖记录（最多30期）："]
    for rid in sorted(lottery_data["history"].keys(), reverse=True):
        record = lottery_data["history"][rid]
        msg_lines.append(f"期号 {rid}：中奖号码 {record['winning']}")
    await safe_reply(update, context, "\n".join(msg_lines))


@register_command("我的中奖", "我的开奖")
async def my_winnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    lottery_data = load_json(LOTTERY_FILE).get(chat_id)
    if not lottery_data or not lottery_data.get("history"):
        return await safe_reply(update, context, "暂无开奖记录。")

    msg_lines = ["🎉 你的开奖记录："]
    found = False
    for rid in sorted(lottery_data["history"].keys(), reverse=True):
        record = lottery_data["history"][rid]
        for t in record["tickets"]:
            if t["user_id"] != user_id:
                continue
            found = True
            choice = t["choice"]
            bet_amount = t["bet_amount"]
            payout = t["payout"]
            if payout > 0:
                msg_lines.append(
                    f"期号 {rid}：下注 {choice} ({bet_amount}💰) 获得 {payout}💰"
                )
            else:
                msg_lines.append(f"期号 {rid}：下注 {choice} ({bet_amount}💰) 未中奖")
    if not found:
        msg_lines.append("😭 暂无记录")

    await safe_reply(update, context, "\n".join(msg_lines))


@register_command("下注命令", "彩票命令")
async def lotter_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 下注命令：\n"
        "彩票购买/下注 <大/小/单/双/龙/虎/和/数字> <下注金额>｜我的彩票/我的下注 查询本期下注｜开奖记录/历史开奖｜我的中奖/我的开奖\n"
        "赔率：买数字 0-99 与开奖结果一致赔100倍，个位数一致赔10倍\n"
        "赔率：小 0-49，大 50-99，单/双 赔2倍\n"
        "赔率：龙 十位数比个位数大，虎 十位数比个位数小，和 十位数等于个位数 赔9倍\n"
    )
    await safe_reply(update, context, text)


def register_buy_lottery_handlers(app):
    app.add_handler(CommandHandler("buy_lottery", buy_lottery))
    app.add_handler(CommandHandler("my_lottery", my_lottery))
    app.add_handler(CommandHandler("lottery_history", lottery_history))
    app.add_handler(CommandHandler("my_winnings", my_winnings))
    app.add_handler(CommandHandler("lotter_help", lotter_help))
