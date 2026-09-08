import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from typing import Optional

import httpx

from telegram import Update
from telegram.constants import ChatType
from utils import get_group_whitelist

from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# 配置
# ============================================================

# Ollama
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"
# 让 Ollama 尽量保持模型在内存中，减少重复加载
OLLAMA_KEEP_ALIVE = "10m"

# ============================================================
# AI 接话配置
# ============================================================

# 以下默认值只用于兼容缺少字段的旧群配置；实际开关和限额均从每个群的配置读取。
DEFAULT_AI_REPLY_ENABLED = True
DEFAULT_REPLY_PROBABILITY_PERCENT = 100
DEFAULT_MIN_REPLY_INTERVAL_SEC = 3
DEFAULT_MAX_REPLIES_PER_HOUR = 1000

# AI 查看最近多少条消息
CONTEXT_LIMIT = 1

# AI 回复最长多少字
MAX_REPLY_LENGTH = 40

# 回复前随机等待
MIN_DELAY = 0.5
MAX_DELAY = 1.5


# ============================================================
# 群配置
# ============================================================

def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def get_ai_reply_settings(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict:
    """Read the AI reply controls for one group from the group configuration."""
    group_config = get_group_whitelist(context).get(str(chat_id), {})
    if not isinstance(group_config, dict):
        group_config = {}
    return {
        "enabled": bool(group_config.get("ai_reply_enabled", DEFAULT_AI_REPLY_ENABLED)),
        "probability_percent": _clamp_int(
            group_config.get("ai_reply_probability", DEFAULT_REPLY_PROBABILITY_PERCENT),
            0,
            100,
            DEFAULT_REPLY_PROBABILITY_PERCENT,
        ),
        "min_interval_sec": _clamp_int(
            group_config.get("ai_reply_min_interval_sec", DEFAULT_MIN_REPLY_INTERVAL_SEC),
            0,
            3600,
            DEFAULT_MIN_REPLY_INTERVAL_SEC,
        ),
        "max_replies_per_hour": _clamp_int(
            group_config.get("ai_reply_max_per_hour", DEFAULT_MAX_REPLIES_PER_HOUR),
            1,
            10000,
            DEFAULT_MAX_REPLIES_PER_HOUR,
        ),
    }


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


def can_reply(chat_id: int, settings: dict) -> bool:
    """Check the per-group cooldown and hourly reply cap."""
    now = time.time()
    min_interval = settings["min_interval_sec"]
    max_replies_per_hour = settings["max_replies_per_hour"]

    # --------------------------------------------------------
    # 最短间隔
    # --------------------------------------------------------
    last_time = last_reply_time[chat_id]

    if last_time <= 0:
        print(f"[AI][CHECK] 群 {chat_id} 从未回复过，可以继续")
    else:
        elapsed = now - last_time
        remaining = min_interval - elapsed

        print(
            f"[AI][CHECK] 群 {chat_id} "
            f"距离上次回复 {elapsed:.1f}s，"
            f"冷却要求 {min_interval}s"
        )

        if elapsed < min_interval:
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

    
    if len(history) >= max_replies_per_hour:
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
    优化：
    1. 关闭 Qwen3 thinking，避免简单接话判断生成大量思考内容。
    2. 缩短 Prompt，减少 prompt_eval。
    3. 限制输出长度，避免模型生成过多无关内容。
    4. keep_alive 保持模型在内存中。
    """

    prompt = f"""判断 Telegram 群消息是否值得自然接话。

最近聊天：
{context_text or "(无)"}

当前消息：
{current_text}

规则：

1. 以下情况只输出 NO：
- 纯乱码
- 明显无意义的内容
- 广告、推广、群发内容
- 单纯的网址或链接
- 明显只是命令、通知或系统消息
- 程序日志、报错信息、调试信息、JSON、代码片段
- 包含 message_id、sent_message_id、耗时、status、HTTP、Telegram 发送成功等明显技术日志内容
- 明显是机器人运行状态、系统提示或后台记录
- 完全没有自然聊天意义的内容

2. 普通聊天、闲聊、八卦、吐槽、抱怨、分享观点、讲经历，即使不是提问，也可以自然接一句。

3. 可以主动接话，不需要用户明确提问。

4. 回复必须有新的内容。
- 禁止重复用户刚刚说的话。
- 禁止把用户的话换几个字重新说一遍。
- 禁止总结用户刚才的话来充当回复。
- 不能只重复用户句子中的关键词。
- 不要用“你说的是……”“你刚才说……”这类方式机械回应。

5. 不要为了回复而强行回复。

6. 如果前面的聊天中已经回答过相同问题：
- 不要重复之前相同的答案。
- 可以换一个角度回答。
- 如果没有新的内容可说，输出 NO。

7. 回复最多40个字。

8. 回复必须简短、自然、口语化，像真实群聊中的一句接话。

9. 不解释，不输出分析过程，不说“作为AI”。

如果没有自然且有新内容的回复方式，就输出 NO。

只输出最终结果：
- 一句直接发送给用户的中文回复

不要输出其他任何内容。"""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是一个简短、自然的中文群聊助手。",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "stream": False,

        # 关闭 Qwen3 的思考输出；群接话只读取 message.content。
        # 启用 thinking 时，有限的 num_predict 可能全被 thinking 占用，导致 content 为空。
        "think": False,

        # 尽量保持模型在内存中
        "keep_alive": OLLAMA_KEEP_ALIVE,

        "options": {
            "temperature": 0.7,
            # 只需要 NO 或一条 <=40 字的回复，不需要很长输出
            "num_predict": 60,
        },
    }

    try:

        request_start = time.time()

        # timeout 不需要 120 秒；正常本地推理应该更快。
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.post(
                OLLAMA_URL,
                json=payload,
            )

        response.raise_for_status()
        data = response.json()
        result = (
            data
            .get("message", {})
            .get("content", "")
            .strip()
        )


        if not result:
            return None

        # NO / NO 前缀都视为不回复
        if result.upper() == "NO":
            return None

        if result.upper().startswith("NO"):
            return None

        # 清理格式
        result = result.strip("` \n\t\"'“”")

        if not result:
            return None

        # 限制长度
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
        print(f"[Ollama] HTTP 错误: {e}")
        return None

    except Exception as e:
        print(f"[Ollama] 调用失败: {e}")
        return None


# ============================================================
# AI 群聊 Handler
# ============================================================

async def ai_group_reply_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    AI 群聊自动接话 Handler。

    这个版本增加了大量调试日志，
    用来定位“为什么没有回复”。
    """


    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user


    if not message or not chat:
        print("[AI][SKIP] 没有 message 或 chat")
        return



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
    # 群配置开关与限额
    # ========================================================

    ai_settings = get_ai_reply_settings(context, chat_id)
    print(
        f"[AI][CONFIG] 群 {chat_id} "
        f"enabled={ai_settings['enabled']} "
        f"probability={ai_settings['probability_percent']}% "
        f"hourly_limit={ai_settings['max_replies_per_hour']} "
        f"cooldown={ai_settings['min_interval_sec']}s"
    )
    if not ai_settings["enabled"]:
        print(f"[AI][SKIP] 群 {chat_id} 没有开启 AI 接话")
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

    if not can_reply(chat_id, ai_settings):
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

    reply_probability = ai_settings["probability_percent"] / 100
    print(
        f"[AI][PROBABILITY] random={random_value:.4f} "
        f"threshold={reply_probability:.2f} ({ai_settings['probability_percent']}%)"
    )

    if random_value > reply_probability:
        print(
            f"[AI][SKIP] 概率未命中："
            f"{random_value:.4f} > {reply_probability:.2f}"
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
        ollama_start = time.time()

        reply_text = await ask_ollama(
            context_text=context_text,
            current_text=text,
        )

        ollama_cost = time.time() - ollama_start

       
        # AI 判断不回复
        # ----------------------------------------------------

        if not reply_text:
            return

      

        # ----------------------------------------------------
        # 随机等待
        # ----------------------------------------------------

        delay = random.uniform(
            MIN_DELAY,
            MAX_DELAY,
        )

        await asyncio.sleep(delay)

        # ----------------------------------------------------
        # 发送前再次检查冷却
        # ----------------------------------------------------

        print(
            "[AI][CHECK] 等待结束，发送前再次检查冷却"
        )

        # AI 请求期间管理员可能关闭了功能或修改了限额，因此发送前重新读取群配置。
        send_settings = get_ai_reply_settings(context, chat_id)
        if not send_settings["enabled"]:
            print("[AI][SKIP] AI 接话已被关闭，取消发送")
            return
        if not can_reply(chat_id, send_settings):
            print(
                "[AI][SKIP] 发送前冷却检查未通过"
            )
            return

        # ----------------------------------------------------
        # 回复原消息
        # ----------------------------------------------------
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

