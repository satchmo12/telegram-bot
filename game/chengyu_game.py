import random
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import register_command
from info.economy import change_balance
from utils import (
    get_last_pinyin,
    get_first_pinyin,
    group_allowed,
    get_group_whitelist,
    load_chengyu_words,
    save_chengyu_words,
)

current_chengyu = {}
used_chengyu = {}
chengyu_scores = {}  # 新增：记录答题分数
# CHENGYU_LIST = set(item["word"] for item in load_idioms())
# CHENGYU_LIST = {item["word"] for item in load_idioms() if "word" in item}
# save_words(CHENGYU_LIST)

CHENGYU_LIST = load_chengyu_words()


def _chengyu_enabled(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    cfg = get_group_whitelist(context).get(str(chat_id), {})
    return bool(cfg.get("chengyu_game", False))


@group_allowed
@register_command("成语接龙")
async def start_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _chengyu_enabled(context, chat_id):
        return await update.message.reply_text("⚠️ 本群未开启成语接龙，请先到【群配置】开启。")

    if not CHENGYU_LIST:
        await update.message.reply_text("成语库为空，无法开始接龙")
        return

    # 随机选一个成语作为起始
    chengyu = random.choice(tuple(CHENGYU_LIST))

    current_chengyu[chat_id] = chengyu
    used_chengyu[chat_id] = {chengyu}
    chengyu_scores[chat_id] = {}  # 清空分数

    yin = get_last_pinyin(chengyu)

    await update.message.reply_text(
        f"成语接龙开始！{chengyu} 末字拼音：{yin} "
        f"请用 / 或 接龙 开头接龙，或回复机器人"
    )


@group_allowed
@register_command("结束接龙")
async def end_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _chengyu_enabled(context, chat_id):
        return

    if chat_id in current_chengyu:
        # 取排行榜
        scores = chengyu_scores.get(chat_id, {})
        if scores:
            ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            result_lines = [f"🏆 成语接龙排名："]

            for i, (uid, score) in enumerate(ranking, start=1):
                user = await context.bot.get_chat_member(chat_id, uid)
                name = user.user.first_name
                coins_to_add = (score // 1) * 100  # 如果每答对一个加 100 分
                result_lines.append(f"{i}. {name} - {score} 分 - {coins_to_add}金币")
                change_balance(chat_id, uid, coins_to_add)

            result_text = "\n".join(result_lines)
        else:
            result_text = "📭 本局没有人答对成语。"

        # 清理数据
        del current_chengyu[chat_id]
        del used_chengyu[chat_id]
        del chengyu_scores[chat_id]

        await update.message.reply_text("🛑 成语接龙已结束。\n" + result_text)
    else:
        await update.message.reply_text("📭 当前没有游戏在进行。")


@register_command("/", "接龙")
async def handle_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _chengyu_enabled(context, chat_id):
        return

    if chat_id not in current_chengyu:
        return

    if update.message.reply_to_message:
        text = update.message.text.strip()
        # 只处理回复机器人消息的情况
        if not update.message.reply_to_message.from_user.is_bot:
            return
    else:
        args = context.args
        if not args:
            return

        text = args[0]

    if text in used_chengyu.get(chat_id, set()):
        await update.message.reply_text("⚠️ 这个成语已经用过了。")
        return

    if text not in CHENGYU_LIST:
        await update.message.reply_text("🤔 不认识这个成语。")
        return

    last_pinyin = get_last_pinyin(current_chengyu[chat_id])
    current_pinyin = get_first_pinyin(text)

    if current_pinyin != last_pinyin:
        await update.message.reply_text(
            f"❌ 拼音不对，应以『{current_chengyu[chat_id][-1]}』开头。"
        )
        return

    # ✅ 正确接龙
    current_chengyu[chat_id] = text
    used_chengyu[chat_id].add(text)

    # 记录分数
    user_id = update.effective_user.id
    chengyu_scores.setdefault(chat_id, {})
    chengyu_scores[chat_id][user_id] = chengyu_scores[chat_id].get(user_id, 0) + 1

    yin = get_last_pinyin(text)
    await update.message.reply_text(f"✅ 很棒！接下来是：『{text[-1]}』 {yin}")


@register_command("成语添加")
async def add_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await update.message.reply_text("用法：成语添加 四面楚歌")
        return

    new_word = parts[1].strip()

    if len(new_word) < 4:
        await update.message.reply_text("❌ 成语至少要 4 个字。")
        return

    # 已存在
    if new_word in CHENGYU_LIST:
        await update.message.reply_text("⚠️ 这个成语已经存在。")
        return

    # ✅ 新增到内存
    CHENGYU_LIST.add(new_word)

    # ✅ 排序后写回文件
    save_chengyu_words(CHENGYU_LIST)

    await update.message.reply_text(f"✅ 成语『{new_word}』已加入词库（已排序）！")


def format_used_chengyu(chat_id: int) -> str:
    chengyu_set = used_chengyu.get(chat_id)

    if not chengyu_set:
        return "📭 当前还没有使用过任何成语。"

    chengyu_list = sorted(chengyu_set)
    return "📚 当前已用成语：\n" + "、".join(chengyu_list)


@group_allowed
@register_command("已用成语")
async def show_used_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _chengyu_enabled(context, chat_id):
        return
    text = format_used_chengyu(chat_id)
    await update.message.reply_text(text)


@group_allowed
@register_command("提示")
async def pick_next_chengyu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    从成语库中找一个还能接的成语（未使用）
    找不到返回 None
    """
    chat_id = update.effective_chat.id
    if not _chengyu_enabled(context, chat_id):
        return
    if chat_id not in current_chengyu:
        return

    last_pinyin = get_last_pinyin(current_chengyu[chat_id])
    used = used_chengyu.get(chat_id, set())

    candidates = [
        word
        for word in CHENGYU_LIST
        if word not in used and get_first_pinyin(word) == last_pinyin
    ]

    if not candidates:
        return "无可以接上的成语了"

    text = random.choice(candidates)
    # ✅ 正确接龙
    current_chengyu[chat_id] = text
    used_chengyu[chat_id].add(text)

    await update.message.reply_text(text)


def register_chengyu_handlers(app):
    app.add_handler(CommandHandler("chengyu", start_chengyu))
    app.add_handler(CommandHandler("endchengyu", end_chengyu))
    app.add_handler(CommandHandler("addchengyu", add_chengyu))
