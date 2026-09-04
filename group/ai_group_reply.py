import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from typing import Optional

import httpx

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from utils import group_allowed


# ============================================================
# 配置
# ============================================================

# Ollama
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"

# ============================================================
# AI 接话配置
# ============================================================

# 是否开启
AI_REPLY_ENABLED = True

# AI 判断为适合回复后，实际回复的概率
# 例如 0.7 = 70%
REPLY_PROBABILITY = 0.70

# 同一个群，两次 AI 回复之间至少间隔多少秒
MIN_REPLY_INTERVAL = 30

# 每小时最多回复多少次
MAX_REPLIES_PER_HOUR = 100

# AI 查看最近多少条消息
CONTEXT_LIMIT = 3

# AI 回复最长多少字
MAX_REPLY_LENGTH = 40

# 回复前随机等待
MIN_DELAY = 2
MAX_DELAY = 5


# ============================================================
# 群配置
# ============================================================

# 如果设置为空 set()：
# 表示所有群都允许使用 AI 接话。
#
# 如果只想指定某几个群：
#
# AI_REPLY_GROUPS = {
#     -1001234567890,
#     -1009876543210,
# }
#
# 那么只有这些群会运行。
AI_REPLY_GROUPS = set()


# ============================================================
# 运行状态
# ============================================================

# 每个群最后一次 AI 回复时间
last_reply_time = defaultdict(float)

# 每个群最近一小时 AI 回复时间
reply_history = defaultdict(deque)

# 正在处理的群
processing_chats = set()


# ============================================================
# 基础判断
# ============================================================

def group_enabled(chat_id: int) -> bool:
    """
    判断当前群是否开启 AI 接话。

    AI_REPLY_GROUPS 为空：
        所有群开启

    AI_REPLY_GROUPS 有内容：
        只有列表中的群开启
    """

    if not AI_REPLY_GROUPS:
        return True

    return chat_id in AI_REPLY_GROUPS


def cleanup_reply_history(chat_id: int):
    """
    删除一小时前的回复记录。
    """

    now = time.time()

    history = reply_history[chat_id]

    while history:
        if now - history[0] > 3600:
            history.popleft()
        else:
            break


def can_reply(chat_id: int) -> bool:
    """
    判断当前群是否允许再次 AI 回复。
    同时输出详细调试信息。
    """

    now = time.time()

    # --------------------------------------------------------
    # 最短间隔
    # --------------------------------------------------------
    last_time = last_reply_time[chat_id]

    if last_time <= 0:
        print(f"[AI][CHECK] 群 {chat_id} 从未回复过，可以继续")
    else:
        elapsed = now - last_time
        remaining = MIN_REPLY_INTERVAL - elapsed

        print(
            f"[AI][CHECK] 群 {chat_id} "
            f"距离上次回复 {elapsed:.1f}s，"
            f"冷却要求 {MIN_REPLY_INTERVAL}s"
        )

        if elapsed < MIN_REPLY_INTERVAL:
            print(
                f"[AI][SKIP] 群 {chat_id} 还在冷却，"
                f"剩余 {remaining:.1f}s"
            )
            return False

    # --------------------------------------------------------
    # 每小时次数
    # --------------------------------------------------------
    cleanup_reply_history(chat_id)

    history = reply_history[chat_id]

    print(
        f"[AI][CHECK] 群 {chat_id} "
        f"最近1小时回复次数 {len(history)}/{MAX_REPLIES_PER_HOUR}"
    )

    if len(history) >= MAX_REPLIES_PER_HOUR:
        print(
            f"[AI][SKIP] 群 {chat_id} "
            f"已达到每小时最大回复次数"
        )
        return False

    print(f"[AI][CHECK] 群 {chat_id} 通过 can_reply()")
    return True


# ============================================================
# 消息过滤
# ============================================================

def should_ignore_message(update: Update) -> bool:
    """
    判断一条消息是否不应该进入 AI。
    """

    message = update.effective_message

    if not message:
        return True

    # --------------------------------------------------------
    # 必须有文本
    # --------------------------------------------------------

    text = message.text

    if not text:
        return True

    text = text.strip()

    if not text:
        return True

    # --------------------------------------------------------
    # Telegram 命令
    # --------------------------------------------------------

    if text.startswith("/"):
        return True

    # --------------------------------------------------------
    # 太长
    # --------------------------------------------------------

    if len(text) > 500:
        return True

    return False


# ============================================================
# 获取最近聊天记录
# ============================================================

async def get_chat_context(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> str:
    """
    获取本地缓存的最近聊天记录。

    Bot API 不能像 Telethon 那样随时读取群历史，
    所以这里直接使用本地缓存。
    """
    return build_context(chat_id)


# ============================================================
# 本地群聊缓存
# ============================================================

chat_message_history = defaultdict(
    lambda: deque(maxlen=CONTEXT_LIMIT)
)


def add_message_to_history(
    chat_id: int,
    user_id: int,
    text: str,
):
    """
    把消息放入本地上下文缓存。
    """

    chat_message_history[chat_id].append(
        {
            "user_id": user_id,
            "text": text,
            "time": time.time(),
        }
    )


def build_context(chat_id: int) -> str:
    """
    把缓存转换成 AI 能理解的文本。
    """

    history = chat_message_history[chat_id]

    if not history:
        return ""

    lines = []

    for item in history:
        lines.append(
            f"用户{item['user_id']}: {item['text']}"
        )

    return "\n".join(lines)


# ============================================================
# Ollama
# ============================================================

async def ask_ollama(
    context_text: str,
    current_text: str,
) -> Optional[str]:
    """
    调用本地 Ollama。
    """

    prompt = f"""
你是一个 Telegram 群聊 AI 助手。

你的任务是判断当前这句话是否值得接一句话。

聊天上下文：

{context_text}

当前消息：

{current_text}

请根据上下文判断。

如果不适合回复：
只输出：
NO

如果适合回复：
只输出一句简短、自然的中文聊天回复。

要求：

1. 回复尽量简短。
2. 最多40个字。
3. 不要解释。
4. 不要重复对方原话。
5. 不要输出“作为AI”之类的话。
6. 不要声称自己是真人。
7. 不要冒充群里的某个人。
8. 不要连续使用很多表情。
9. 不要每条消息都回复。
10. 如果回复会显得突兀，就输出 NO。

例如：

当前消息：
这老板脾气太好了吧

合适回复：
说的就是好脾气

只输出最终结果，不要输出分析过程。
"""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个简短、自然的中文群聊助手。"
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "stream": False,
        "options": {
            "temperature": 0.8,
        },
    }

    try:
        async with httpx.AsyncClient(
            timeout=120
        ) as http:

            print(
                f"[Ollama][REQUEST] url={OLLAMA_URL} "
                f"model={OLLAMA_MODEL}"
            )

            response = await http.post(
                OLLAMA_URL,
                json=payload,
            )

            print(
                f"[Ollama][HTTP] status={response.status_code}"
            )

            response.raise_for_status()

            data = response.json()

            print(f"[Ollama][RAW] {data}")

        result = (
            data
            .get("message", {})
            .get("content", "")
            .strip()
        )

        print(f"[Ollama][CONTENT] {result!r}")

        if not result:
            print(
                "[Ollama] 没有返回内容"
            )
            return None

        # ----------------------------------------------------
        # Qwen 有时会输出 NO 前后的内容
        # ----------------------------------------------------

        if result.upper() == "NO":
            return None

        if result.startswith("NO"):
            return None

        # ----------------------------------------------------
        # 清理一些可能出现的格式
        # ----------------------------------------------------

        result = result.strip(
            "` \n\t\"'“”"
        )

        if not result:
            return None

        # ----------------------------------------------------
        # 限制长度
        # ----------------------------------------------------

        if len(result) > MAX_REPLY_LENGTH:
            result = result[:MAX_REPLY_LENGTH]

        return result

    except httpx.ConnectError:
        print(
            "[Ollama] 无法连接 Ollama，请确认："
            "ollama serve 是否正在运行"
        )
        return None

    except httpx.HTTPError as e:
        print(
            "[Ollama] HTTP 错误: %s",
            e,
        )
        return None

    except Exception as e:
        print(
            "[Ollama] 调用失败: %s",
            e,
        )
        return None


# ============================================================
# AI 群聊 Handler
# ============================================================
@group_allowed
async def ai_group_reply_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    AI 群聊自动接话 Handler。

    这个版本增加了大量调试日志，
    用来定位“为什么没有回复”。
    """

    print("\n" + "=" * 70)
    print("[AI][UPDATE] 收到一条 Update")

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    print(
        f"[AI][UPDATE] message={bool(message)} "
        f"chat={bool(chat)} user={bool(user)}"
    )

    if not message or not chat:
        print("[AI][SKIP] 没有 message 或 chat")
        return

    print(
        f"[AI][CHAT] id={chat.id} "
        f"type={chat.type} "
        f"title={getattr(chat, 'title', None)!r}"
    )

    print(
        f"[AI][USER] id={getattr(user, 'id', None)} "
        f"name={getattr(user, 'full_name', None)!r} "
        f"username={getattr(user, 'username', None)!r} "
        f"is_bot={getattr(user, 'is_bot', None)}"
    )

    # ========================================================
    # 只处理群
    # ========================================================

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        print(
            f"[AI][SKIP] 不是群聊，chat.type={chat.type}"
        )
        return

    chat_id = chat.id

    # ========================================================
    # 群是否开启
    # ========================================================

    enabled = group_enabled(chat_id)

    print(
        f"[AI][CONFIG] 群 {chat_id} "
        f"group_enabled={enabled} "
        f"AI_REPLY_GROUPS={AI_REPLY_GROUPS}"
    )

    if not enabled:
        print(
            f"[AI][SKIP] 群 {chat_id} 没有开启 AI 接话"
        )
        return

    # ========================================================
    # 总开关
    # ========================================================

    print(
        f"[AI][CONFIG] AI_REPLY_ENABLED={AI_REPLY_ENABLED}"
    )

    if not AI_REPLY_ENABLED:
        print("[AI][SKIP] AI 总开关关闭")
        return

    # ========================================================
    # 忽略机器人消息
    # ========================================================

    if user and user.is_bot:
        print(
            f"[AI][SKIP] 发送者是机器人："
            f"{user.id} @{user.username}"
        )
        return

    # ========================================================
    # 消息过滤
    # ========================================================

    raw_text = message.text

    print(
        f"[AI][MESSAGE] message_id={message.message_id} "
        f"text={raw_text!r}"
    )

    if not raw_text:
        print("[AI][SKIP] message.text 为空")
        return

    text_value = raw_text.strip()

    if not text_value:
        print("[AI][SKIP] 消息去掉空格后为空")
        return

    if text_value.startswith("/"):
        print(
            f"[AI][SKIP] Telegram 命令：{text_value}"
        )
        return

    if len(text_value) > 500:
        print(
            f"[AI][SKIP] 消息长度 {len(text_value)} > 500"
        )
        return

    text = text_value

    # ========================================================
    # 保存到本地上下文
    # ========================================================

    add_message_to_history(
        chat_id=chat_id,
        user_id=user.id if user else 0,
        text=text,
    )

    print(
        f"[AI][HISTORY] 已缓存消息：{text!r}"
    )

    # ========================================================
    # 冷却检查
    # ========================================================

    if not can_reply(chat_id):
        return

    # ========================================================
    # 防止同一个群同时跑多个 AI
    # ========================================================

    if chat_id in processing_chats:
        print(
            f"[AI][SKIP] 群 {chat_id} 正在处理另一个 AI 请求"
        )
        return

    # ========================================================
    # 概率控制
    # ========================================================

    random_value = random.random()

    print(
        f"[AI][PROBABILITY] random={random_value:.4f} "
        f"threshold={REPLY_PROBABILITY}"
    )

    if random_value > REPLY_PROBABILITY:
        print(
            f"[AI][SKIP] 概率未命中："
            f"{random_value:.4f} > {REPLY_PROBABILITY}"
        )
        return

    print(
        f"[AI][START] 开始处理群 {chat_id} 的 AI 回复"
    )

    processing_chats.add(chat_id)

    try:
        # ----------------------------------------------------
        # 获取上下文
        # ----------------------------------------------------

        context_text = build_context(chat_id)

        print(
            "[AI][CONTEXT] 当前上下文："
        )
        print(
            context_text if context_text else "(空)"
        )

        # ----------------------------------------------------
        # 调用 Ollama
        # ----------------------------------------------------

        print(
            f"[AI][OLLAMA] 开始请求，"
            f"current_text={text!r}"
        )

        ollama_start = time.time()

        reply_text = await ask_ollama(
            context_text=context_text,
            current_text=text,
        )

        ollama_cost = time.time() - ollama_start

        print(
            f"[AI][OLLAMA] 请求结束，"
            f"耗时={ollama_cost:.2f}s，"
            f"result={reply_text!r}"
        )

        # ----------------------------------------------------
        # AI 判断不回复
        # ----------------------------------------------------

        if not reply_text:
            print(
                "[AI][DECISION] Ollama 判断：NO / 不回复"
            )
            return

        print(
            f"[AI][DECISION] AI 决定回复：{reply_text!r}"
        )

        # ----------------------------------------------------
        # 随机等待
        # ----------------------------------------------------

        delay = random.uniform(
            MIN_DELAY,
            MAX_DELAY,
        )

        print(
            f"[AI][DELAY] 回复前等待 {delay:.2f}s"
        )

        await asyncio.sleep(delay)

        # ----------------------------------------------------
        # 发送前再次检查冷却
        # ----------------------------------------------------

        print(
            "[AI][CHECK] 等待结束，发送前再次检查冷却"
        )

        if not can_reply(chat_id):
            print(
                "[AI][SKIP] 发送前冷却检查未通过"
            )
            return

        # ----------------------------------------------------
        # 回复原消息
        # ----------------------------------------------------

        print(
            f"[AI][SEND] 准备回复 "
            f"chat_id={chat_id} "
            f"message_id={message.message_id} "
            f"text={reply_text!r}"
        )

        send_start = time.time()

        # sent_message = await message.reply_text(
        #     reply_text,
        #     disable_notification=True,
        # )
        
        sent_message = await context.bot.send_message(
            chat_id=message.chat_id,
            text=reply_text,
            disable_notification=True,
        )

        send_cost = time.time() - send_start

        print(
            f"[AI][SEND] Telegram 发送成功，"
            f"sent_message_id={getattr(sent_message, 'message_id', None)} "
            f"耗时={send_cost:.2f}s"
        )

        # ----------------------------------------------------
        # 记录回复
        # ----------------------------------------------------

        now = time.time()

        last_reply_time[chat_id] = now
        reply_history[chat_id].append(now)

        print(
            f"[AI][RECORD] 已记录回复，"
            f"当前1小时次数={len(reply_history[chat_id])}"
        )

        # ----------------------------------------------------
        # 把 AI 回复也加入上下文
        # ----------------------------------------------------

        add_message_to_history(
            chat_id=chat_id,
            user_id=0,
            text=reply_text,
        )

        print(
            f"[AI][SUCCESS] 群 {chat_id} AI 回复完成："
            f"{reply_text!r}"
        )

    except Exception as e:
        print(
            f"[AI][ERROR] 群 {chat_id} 处理失败"
        )
        print(
            f"[AI][ERROR] exception_type={type(e).__name__}"
        )
        print(
            f"[AI][ERROR] exception={e!r}"
        )

        logging.exception(
            "[AI] 群聊 AI 回复发生异常"
        )

    finally:
        processing_chats.discard(chat_id)

        print(
            f"[AI][END] 群 {chat_id} 本次 AI 处理结束"
        )
        print("=" * 70)


# ============================================================
# 启动
# ============================================================

def register_ai_group_reply_handlers(app):



    # ========================================================
    # 群消息
    # ========================================================
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND,
            ai_group_reply_handler,
        )  
    )

    print("=" * 70)
    print("[AI][STARTUP] AI 群聊助手 Handler 已注册")
    print(f"[AI][STARTUP] AI_REPLY_ENABLED={AI_REPLY_ENABLED}")
    print(f"[AI][STARTUP] OLLAMA_URL={OLLAMA_URL}")
    print(f"[AI][STARTUP] OLLAMA_MODEL={OLLAMA_MODEL}")
    print(f"[AI][STARTUP] REPLY_PROBABILITY={REPLY_PROBABILITY}")
    print(f"[AI][STARTUP] MIN_REPLY_INTERVAL={MIN_REPLY_INTERVAL}s")
    print(f"[AI][STARTUP] MAX_REPLIES_PER_HOUR={MAX_REPLIES_PER_HOUR}")
    print(f"[AI][STARTUP] CONTEXT_LIMIT={CONTEXT_LIMIT}")
    print(f"[AI][STARTUP] MAX_REPLY_LENGTH={MAX_REPLY_LENGTH}")
    print(f"[AI][STARTUP] DELAY={MIN_DELAY}-{MAX_DELAY}s")
    print(f"[AI][STARTUP] AI_REPLY_GROUPS={AI_REPLY_GROUPS}")
    print("=" * 70)

