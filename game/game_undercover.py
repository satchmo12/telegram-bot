from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
import random
from command_router import register_command
from utils import group_allowed, safe_reply

# 卧底词语对
WORDS = [
    ["苹果", "香蕉"],
    ["猫", "老虎"],
    ["飞机", "直升机"],
    ["牛奶", "豆浆"],
    ["西瓜", "哈密瓜"],
    ["螃蟹", "龙虾"],
    ["刷牙", "洗脸"],
    ["火车", "地铁"],
    ["眼镜", "墨镜"],
    ["手机", "座机"],
    ["口红", "唇膏"],
    ["冰箱", "空调"],
    ["超人", "蝙蝠侠"],
    ["西装", "礼服"],
    ["程序员", "黑客"],
    ["报纸", "杂志"],
    ["小狗", "泰迪"],
    ["火锅", "烧烤"],
    ["咖啡", "奶茶"],
    ["牙刷", "剃须刀"],
    ["老师", "教练"],
    ["歌手", "舞者"],
    ["地球", "月球"],
    ["摩托车", "电动车"],
    ["婚房", "墓地"],
    ["臭豆腐", "屎"],
]

# 游戏状态缓存
GAME_STATE = {}
user_words = {}  # user_id -> word，供 /getword 使用


def get_game(chat_id: str):
    return GAME_STATE.setdefault(
        chat_id, {"players": {}, "votes": {}, "status": "waiting"}
    )


@register_command("开始卧底")
async def start_undercover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    game = get_game(chat_id)
    game["players"].clear()
    game["votes"].clear()
    game["status"] = "waiting"
    await safe_reply(
        update, context, "🕵️ 已创建“谁是卧底”游戏，请使用  加入卧底 加入游戏。"
    )


@register_command("加入卧底")
async def join_undercover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    game = get_game(chat_id)

    if game["status"] != "waiting":
        return await safe_reply(update, context, "🚫 游戏已经开始，无法加入。")

    user_id = str(user.id)
    if user_id in game["players"]:
        return await safe_reply(update, context, "⚠️ 你已经加入了游戏。")

    # 限制最多10人（可选）
    if len(game["players"]) >= 10:
        return await safe_reply(update, context, "🚫 游戏人数已满，无法加入。")

    game["players"][user_id] = {
        "name": user.full_name,
        "username": user.username or user.full_name,
        "alive": True,
    }

    return await safe_reply(
        update,
        context,
        f"✅ {user.full_name} 加入了游戏。当前人数：{len(game['players'])}/10",
    )


@register_command("发词")
async def send_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    game = get_game(chat_id)

    if len(game["players"]) < 3:
        return await safe_reply(update, context, "❗ 至少需要 3 名玩家才能开始游戏。")

    # normal_word, spy_word = random.choice(WORDS)
    word_pair = random.choice(WORDS)
    random.shuffle(word_pair)
    normal_word, spy_word = word_pair

    players = list(game["players"].items())
    spy_id = random.choice(players)[0]

    game["status"] = "voting"
    game["votes"] = {}

    failed_users = []

    for uid, info in players:
        role = "spy" if uid == spy_id else "civilian"
        word = spy_word if role == "spy" else normal_word

        info["role"] = role
        info["alive"] = True

        user_words[uid] = word  # 记录词语供私聊使用
        info["word"] = word  # ✅【就在这里加】

        try:
            await context.bot.send_message(chat_id=uid, text=f"🕵️ 你的词语是：{word}")
        except Exception as e:
            failed_users.append(info["username"] or info["name"])

    msg = "📢 词语已发送，游戏开始！请依次描述你的词语，不要暴露自己。\n使用 /vote 或者发送 投票 @用户名 进行投票。"

    if failed_users:
        msg += "\n\n⚠️ 以下玩家尚未私聊我，无法收到词语，请点击机器人头像并发送任意消息或 /getword：\n" + "\n".join(
            f"👉 @{u}" for u in failed_users
        )

    await safe_reply(update, context, msg)


@register_command("投票")
async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)
    game = get_game(chat_id)

    if game["status"] != "voting":
        return await update.message.reply_text("⚠️ 当前不是投票阶段。")

    if not context.args:
        return await update.message.reply_text("请使用 /vote @用户名 进行投票。")

    target_username = context.args[0].lstrip("@")
    target_id = None
    for pid, pdata in game["players"].items():
        if pdata["username"] == target_username and pdata["alive"]:
            target_id = pid
            break

    if not target_id:
        return await update.message.reply_text("❌ 未找到该玩家，或该玩家已出局。")

    if user_id not in game["players"] or not game["players"][user_id]["alive"]:
        return await update.message.reply_text("❌ 你已被淘汰，无法投票。")

    game["votes"][user_id] = target_id
    await update.message.reply_text(f"✅ 你已投票给 @{target_username}")

    alive_ids = [pid for pid, p in game["players"].items() if p["alive"]]
    if all(pid in game["votes"] for pid in alive_ids):
        await process_vote_result(update, context, chat_id)


async def process_vote_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str
):
    game = get_game(chat_id)
    votes = game["votes"]
    players = game["players"]

    count = {}
    for target in votes.values():
        count[target] = count.get(target, 0) + 1

    max_votes = max(count.values())
    candidates = [uid for uid, v in count.items() if v == max_votes]

    if len(candidates) > 1:
        msg = "⚠️ 平票！没有人被淘汰，进入下一轮。"
    else:
        out_id = candidates[0]
        players[out_id]["alive"] = False
        role = players[out_id]["role"]
        word = players[out_id]["word"]
        msg = f"🗳️ 玩家 @{players[out_id]['username']} 被淘汰！身份：{'🕵️ 卧底' if role == 'spy' else '👥 平民'}"

    game["votes"] = {}
    await context.bot.send_message(chat_id, msg)
    await check_game_result(context, chat_id)


async def check_game_result(context: ContextTypes.DEFAULT_TYPE, chat_id: str):
    game = get_game(chat_id)
    players = game["players"]
    alive = [p for p in players.values() if p["alive"]]
    spies = [p for p in alive if p["role"] == "spy"]
    civilians = [p for p in alive if p["role"] == "civilian"]

    if not spies:
        msg = "🎉 游戏结束！平民胜利！所有卧底已被淘汰！"
        GAME_STATE.pop(chat_id, None)
    elif len(spies) >= len(civilians):
        msg = "😈 游戏结束！卧底胜利！卧底人数与平民持平！"
        GAME_STATE.pop(chat_id, None)
    else:
        msg = "🕵️ 新一轮开始，请继续讨论并使用 /vote 投票。"
        game["status"] = "voting"

    await context.bot.send_message(chat_id, msg)


@register_command("查看状态")
async def undercover_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    game = GAME_STATE.get(chat_id)
    if not game:
        return await safe_reply(update, context, "⚠️ 当前没有进行中的卧底游戏。")

    lines = ["👥 当前玩家状态："]
    for p in game["players"].values():
        status = "✅ 存活" if p["alive"] else "❌ 淘汰"
        lines.append(f"- @{p['username']} - {status}")

    await safe_reply(update, context, "\n".join(lines))


@register_command("结束卧底")
async def end_undercover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if GAME_STATE.pop(chat_id, None):
        await safe_reply(update, context, "🛑 游戏已手动结束，欢迎下次再来！")
    else:
        await safe_reply(update, context, "⚠️ 当前没有正在进行的卧底游戏。")


@register_command("谁是卧底", "卧底命令")
async def help_undercover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🕵️‍♂️ 【谁是卧底】游戏指令帮助\n\n"
        "开始卧底 - 创建新游戏\n"
        "加入卧底 - 加入游戏\n"
        "发词 - 分发词语，开始游戏\n"
        "投票 @用户名 - 投票淘汰玩家\n"
        "查看状态 - 查看当前玩家状态\n"
        "结束卧底 - 结束游戏\n"
        "流程是开始卧底后，发送加入卧底进入游戏，当超过三人的时候可以发词开始游戏，用户进行发言后进行投票\n"
    )
    await safe_reply(update, context, help_text)


async def get_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    word = user_words.get(uid)
    if word:
        await update.message.reply_text(f"🕵️ 你的词语是：{word}")
    else:
        await update.message.reply_text(
            "❌ 暂无分配词语。请确认你已加入游戏且游戏已开始。"
        )


def register_undercover_handlers(app):
    app.add_handler(CommandHandler("start_undercover", start_undercover))
    app.add_handler(CommandHandler("join_undercover", join_undercover))
    app.add_handler(CommandHandler("send_words", send_words))
    app.add_handler(CommandHandler("vote", vote))
    app.add_handler(CommandHandler("undercover_status", undercover_status))
    app.add_handler(CommandHandler("end_undercover", end_undercover))
    app.add_handler(CommandHandler("getword", get_word))
