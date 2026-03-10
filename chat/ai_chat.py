from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from typing import Dict, List

from chat.ai_aiohttp import ask_ai
from command_router import register_command
from utils import load_json, save_json, safe_reply

ai_chat_users = set()
AI_HISTORY_FILE = "data/ai_history.json"
MAX_HISTORY_MESSAGES = 16
AI_SYSTEM_PROMPT = (
    "你是一个中文聊天助手。回答要自然、有帮助、简洁。"
    "当用户问技术问题时，优先给可执行步骤。"
    "不知道时直接说不确定，不要编造。"
)


def _session_key(update: Update) -> str:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    return f"{chat_id}:{user_id}"


def _get_history(session_key: str) -> List[Dict[str, str]]:
    data = load_json(AI_HISTORY_FILE)
    if not isinstance(data, dict):
        return []
    history = data.get(session_key, [])
    if not isinstance(history, list):
        return []
    result = []
    for item in history[-MAX_HISTORY_MESSAGES:]:
        if (
            isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ):
            result.append({"role": item["role"], "content": item["content"]})
    return result


def _save_history(session_key: str, history: List[Dict[str, str]]) -> None:
    data = load_json(AI_HISTORY_FILE)
    if not isinstance(data, dict):
        data = {}
    data[session_key] = history[-MAX_HISTORY_MESSAGES:]
    save_json(AI_HISTORY_FILE, data)


def _is_reply_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or not msg.reply_to_message or not msg.reply_to_message.from_user:
        return False
    return msg.reply_to_message.from_user.id == context.bot.id


def _is_mentioning_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or not msg.text:
        return False
    username = context.bot.username
    return bool(username and f"@{username.lower()}" in msg.text.lower())


async def _ask_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str
) -> None:
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return await safe_reply(update, context, "请在命令后面加上内容，例如：聊天 今天天气")

    await safe_reply(update, context, "🤖 思考中...")
    session_key = _session_key(update)
    history = _get_history(session_key)
    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": clean_prompt}
    ]
    response = await ask_ai(messages)
    if not response or not str(response).strip():
        response = "🤖 暂时没有生成有效回复，请稍后再试。"
    history = history + [
        {"role": "user", "content": clean_prompt},
        {"role": "assistant", "content": str(response)},
    ]
    _save_history(session_key, history)
    await safe_reply(update, context, response)


@register_command("聊天", "ai")
async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args or [])
    await _ask_and_reply(update, context, prompt)


@register_command("开启ai")
async def aion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ai_chat_users.add(user_id)
    await safe_reply(update, context, "✅ 已开启 AI 自动回复（仅@机器人或回复机器人时触发）")


@register_command("关闭ai")
async def aioff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ai_chat_users.discard(user_id)
    await safe_reply(update, context, "🛑 已关闭 AI 自动回复")


@register_command("重置ai")
async def aireset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = _session_key(update)
    data = load_json(AI_HISTORY_FILE)
    if not isinstance(data, dict):
        data = {}
    if key in data:
        del data[key]
        save_json(AI_HISTORY_FILE, data)
    await safe_reply(update, context, "🧹 AI 对话上下文已清空")


async def ai_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    user_id = update.effective_user.id
    if user_id not in ai_chat_users:
        return

    is_private = update.effective_chat and update.effective_chat.type == "private"
    triggered = is_private or _is_reply_to_bot(update, context) or _is_mentioning_bot(
        update, context
    )
    if not triggered:
        return

    prompt = msg.text
    username = context.bot.username
    if username:
        prompt = prompt.replace(f"@{username}", "").strip()

    await _ask_and_reply(update, context, prompt)


def register_ai_chat_handlers(app):
    app.add_handler(CommandHandler("chat", ai_chat))
    app.add_handler(CommandHandler("aion", aion))
    app.add_handler(CommandHandler("aioff", aioff))
    app.add_handler(CommandHandler("aireset", aireset))
