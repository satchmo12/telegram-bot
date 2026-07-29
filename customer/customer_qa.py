import os
import re
import difflib

from telegram import Update
from telegram.ext import (
    ContextTypes,
    CommandHandler,
)

from command_router import register_command
from utils import load_json, save_json, safe_reply


CUSTOMER_QA_FILE = "data/customer_qa.json"
BUSINESS_CONNECTION_FILE = "data/business_connections.json"
FUZZY_MATCH_THRESHOLD = 0.82


os.makedirs("data", exist_ok=True)



def load_business_connections():
    data = load_json(BUSINESS_CONNECTION_FILE)
    return data if isinstance(data, dict) else {}


def save_business_connections(data):
    save_json(BUSINESS_CONNECTION_FILE, data)
    

# ==========================
# 数据加载
# ==========================

def load_customer_qa():
    data = load_json(CUSTOMER_QA_FILE)
    return data if isinstance(data, dict) else {}


def save_customer_qa(data):
    save_json(CUSTOMER_QA_FILE, data)


def get_owner_by_business_connection(connection_id):

    if not connection_id:
        return None

    data = load_business_connections()

    info = data.get(connection_id, {})

    if not isinstance(info, dict):
        return None

    return info.get("owner_id")

# ==========================
# 文本匹配
# ==========================

def normalize_text(text):
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[。！!？?,，、~～…]+$", "", text)
    return text


def find_best_match(text, questions):

    if not text or not questions:
        return None

    src = normalize_text(text)

    best_key = None
    best_ratio = 0

    for q in questions:

        ratio = difflib.SequenceMatcher(
            a=src,
            b=normalize_text(q)
        ).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_key = q


    if best_ratio >= FUZZY_MATCH_THRESHOLD:
        return best_key

    return None



# ==========================
# 获取当前消息
# ==========================

def get_message(update: Update):

    return (
        update.message
        or getattr(update, "business_message", None)
    )



# ==========================
# 客服自动回复
# ==========================

async def handle_customer_qa(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    print("come here=====")
    msg = get_message(update)
    
    if not msg or not msg.text:
        return


    # 只处理私聊
    if msg.chat.type != "private":
        return

    connection_id = msg.business_connection_id

    user_id = get_owner_by_business_connection(connection_id)
    text = msg.text.strip()
    data = load_customer_qa()


    # 获取当前用户自己的知识库
    user_qa = data.get(user_id, {})

    if not user_qa:
        return

    match_key = (
        text
        if text in user_qa
        else find_best_match(
            text,
            list(user_qa.keys())
        )
    )

    if not match_key:
        return

    answer = user_qa[match_key]

    await msg.reply_text(
        str(answer)
    )



# ==========================
# 添加客服问答
# ==========================
@register_command("添加客服问答")
async def add_customer_qa(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return


    if not context.args:
        return await safe_reply(
            update,
            context,
            "格式：/添加客服问答 问题=答案"
        )


    content = " ".join(context.args)


    if "=" not in content:
        return await safe_reply(
            update,
            context,
            "格式错误，应为：问题=答案"
        )


    question, answer = content.split("=", 1)


    question = question.strip()
    answer = answer.strip()


    if not question or not answer:
        return await safe_reply(
            update,
            context,
            "问题和答案不能为空"
        )


    user_id = str(user.id)


    data = load_customer_qa()


    user_qa = data.setdefault(
        user_id,
        {}
    )


    user_qa[question] = answer


    data[user_id] = user_qa


    save_customer_qa(data)


    await safe_reply(
        update,
        context,
        "✅ 客服问答添加成功"
    )



# ==========================
# 查看客服问答
# ==========================
@register_command("查看客服问答")
async def customer_qa_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = str(update.effective_user.id)


    data = load_customer_qa()

    user_qa = data.get(
        user_id,
        {}
    )


    if not user_qa:
        return await safe_reply(
            update,
            context,
            "暂无客服问答"
        )


    lines = [
        "📚 我的客服问答："
    ]


    for q, a in user_qa.items():
        lines.append(
            f"{q} = {a}"
        )


    await safe_reply(
        update,
        context,
        "\n".join(lines)
    )

async def handle_business_connection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    connection = update.business_connection

    if not connection:
        return


    connection_id = connection.id

    user_id = connection.user.id


    data = load_business_connections()


    data[connection_id] = {
        "owner_id": str(user_id),
        "user_name": connection.user.username or "",
        "is_enabled": connection.is_enabled,
    }


    save_business_connections(data)


    print(
        "保存Business绑定:",
        connection_id,
        "=>",
        user_id
    )

# ==========================
# 注册
# ==========================

def register_customer_qa_handlers(app):

    app.add_handler(
        CommandHandler(
            "add_customer_qa",
            add_customer_qa
        )
    )


    app.add_handler(
        CommandHandler(
            "customer_qa_list",
            customer_qa_list
        )
    )