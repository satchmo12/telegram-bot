import os
import random
import re
import difflib

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from command_router import register_command
from config import MESSAGE_VOICE
from game.voice_reply import group_tts_voice, tts_voice_reply

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from utils import QA_FILE, RE_FILE, load_json, safe_reply, save_json

# 确保 data 目录存在
os.makedirs("data", exist_ok=True)
FUZZY_MATCH_THRESHOLD = 0.82


# 加载问答数据
def load_qa_data():
    data = load_json(QA_FILE)
    return data if isinstance(data, dict) else {}


# 保存问答数据
def save_qa_data(data):
    save_json(QA_FILE, data)


def _normalize_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[。！!？?,，、~～…]+$", "", t)
    return t


def _find_best_match(text: str, candidates: list[str], threshold: float = FUZZY_MATCH_THRESHOLD):
    if not text or not candidates:
        return None
    src = _normalize_text(text)
    if len(src) < 2:
        return None

    best_key = None
    best_ratio = 0.0
    for c in candidates:
        c_norm = _normalize_text(c)
        if not c_norm:
            continue
        if abs(len(c_norm) - len(src)) > 12:
            continue
        ratio = difflib.SequenceMatcher(a=src, b=c_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_key = c
    if best_ratio >= threshold:
        return best_key
    return None


# 添加问题=答案
@register_command("添加问答")
async def add_qa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        return await safe_reply(update, context,"格式：/addQA 问题=答案")

    qa = " ".join(context.args).strip()
    if "=" not in qa:
        return await safe_reply(update, context,"格式错误，应为 问题=答案")

    question, answer = qa.split("=", 1)
    question, answer = question.strip(), answer.strip()

    data = load_qa_data()
    group_qa = data.setdefault(chat_id, {})

    if question in group_qa:
        return await safe_reply(update, context,"该问题已存在。")

    group_qa[question] = answer
    save_qa_data(data)
    # await safe_reply(update, context,"✅ 添加成功")


# 被动问答响应
async def handle_qa_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    thread_id = (
        update.effective_message.message_thread_id if update.effective_message else None
    )

    data = load_qa_data()
    group_qa = data.get(chat_id, {})
    match_key = text if text in group_qa else _find_best_match(text, list(group_qa.keys()))
    if match_key:
        context.args = group_qa[match_key]
        if MESSAGE_VOICE:
            await tts_voice_reply(update, context)
        else:
            # await group_tts_voice(update, context)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=str(group_qa[match_key]),
                message_thread_id=thread_id,
            )
        return

    # 多答案回复
    data = load_json(RE_FILE)
    group_qa = data.setdefault(chat_id, {})

    # 查找匹配问题
    match_key = text if text in group_qa else _find_best_match(text, list(group_qa.keys()))
    if match_key:
        answers = group_qa[match_key]
        # 确保答案是列表
        if not isinstance(answers, list):
            answers = [answers]
        reply = random.choice(answers)  # 随机选一个答案
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=str(reply),
            message_thread_id=thread_id,
        )
        return

    group_qa = data.setdefault("1000", {})
    match_key = text if text in group_qa else _find_best_match(text, list(group_qa.keys()))
    if match_key:
        answers = group_qa[match_key]
        # 确保答案是列表
        if not isinstance(answers, list):
            answers = [answers]
        reply = random.choice(answers)  # 随机选一个答案
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=str(reply),
            message_thread_id=thread_id,
        )
        return


@register_command("问答列表")
async def qa_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_qa_data()
    group_qa = data.get(chat_id, {})

    if not group_qa:
        return await safe_reply(update, context,"该群尚未添加任何问答。")

    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    page_size = 20
    questions = list(group_qa.items())
    total_pages = (len(questions) + page_size - 1) // page_size

    if page < 1 or page > total_pages:
        return await safe_reply(update, context,f"页码无效，范围：1 - {total_pages}")

    start = (page - 1) * page_size
    end = start + page_size
    page_questions = questions[start:end]

    text_lines = [f"📖 问答列表（第 {page}/{total_pages} 页）:"]
    for i, (q, a) in enumerate(page_questions, start=start + 1):
        text_lines.append(f"{i}. {q} → {a}")

    # 内联按钮
    keyboard = []
    if page > 1:
        keyboard.append(
            InlineKeyboardButton("⬅️ 上一页", callback_data=f"qa_page_{page - 1}")
        )
    if page < total_pages:
        keyboard.append(
            InlineKeyboardButton("➡️ 下一页", callback_data=f"qa_page_{page + 1}")
        )
    markup = InlineKeyboardMarkup([keyboard]) if keyboard else None

    if update.message:
        await update.message.reply_text("\n".join(text_lines), reply_markup=markup)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(text_lines),
            reply_markup=markup,
        )


async def qa_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    match = re.match(r"^qa_page_(\d+)$", query.data)
    if not match:
        return

    page = int(match.group(1))
    chat_id = str(query.message.chat.id)
    data = load_qa_data()
    group_qa = data.get(chat_id, {})

    page_size = 20
    questions = list(group_qa.items())
    total_pages = (len(questions) + page_size - 1) // page_size

    if page < 1 or page > total_pages:
        return await query.message.edit_text("无效页码。")

    start = (page - 1) * page_size
    end = start + page_size
    page_questions = questions[start:end]

    text_lines = [f"📖 问答列表（第 {page}/{total_pages} 页）:"]
    for i, (q, a) in enumerate(page_questions, start=start + 1):
        text_lines.append(f"{i}. {q} → {a}")

    keyboard = []
    if page > 1:
        keyboard.append(
            InlineKeyboardButton("⬅️ 上一页", callback_data=f"qa_page_{page - 1}")
        )
    if page < total_pages:
        keyboard.append(
            InlineKeyboardButton("➡️ 下一页", callback_data=f"qa_page_{page + 1}")
        )
    markup = InlineKeyboardMarkup([keyboard]) if keyboard else None

    await query.message.edit_text("\n".join(text_lines), reply_markup=markup)


@register_command("添加回复", "all", "g")
async def add_re_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    # 优先判断是否回复了消息
    if update.message.reply_to_message and update.message.reply_to_message.text:
        question = update.message.reply_to_message.text.strip()
    else:
        # 否则用参数拼接成新词
        question = " ".join(context.args).strip()

    if not question:
        return await safe_reply(update, context, "❗请回复一条消息或输入问题内容")

    # 获取答案部分
    qa_text = " ".join(context.args).strip()  # 合并所有参数为答案字符串
    # 支持多个答案，用 "|" 分割
    answers = [a.strip() for a in qa_text.split("/") if a.strip()]

    if not answers:
        return await safe_reply(
            update, context, "❗请提供至少一个答案，用 '/' 分隔多个答案"
        )
    triggered = update.message.text.split()[0].lstrip('/')

    # 加载已有数据
    data = load_json(RE_FILE)
    group_qa = data.setdefault(chat_id, {})
    
    if(triggered == "all"):
        group_qa = data.setdefault("1000", {})

    # 检查问题是否存在
    if question in group_qa:
        # 如果问题已存在，则追加新答案（去重）
        existing = group_qa[question]
        if not isinstance(existing, list):
            existing = [existing]
        new_answers = list(set(existing + answers))
        group_qa[question] = new_answers
        # msg = f"✅ 已追加答案，问题【{question}】现在有 {len(new_answers)} 个答案。"
    else:
        # 新问题直接存
        group_qa[question] = answers
        # msg = f"✅ 成功添加问题【{question}】及 {len(answers)} 个答案。"

    save_json(RE_FILE, data)
    # await safe_reply(update, context, msg)

    # data[chat_id].append(new_word)
    # save_json(INSULT_FILE, data)
    # await safe_reply(update, context, f"已添加新骂词：{new_word}")


# 注册
def register_qa_handlers(app):
    app.add_handler(CommandHandler("addQA", add_qa_command))
    app.add_handler(CommandHandler("add_re_word", add_re_word))
    app.add_handler(CallbackQueryHandler(qa_page_callback, pattern=r"^qa_page_\d+$"))
