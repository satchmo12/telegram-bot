import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, ReplyParameters, Update
from telegram.constants import MessageEntityType
from telegram.ext import Application, InlineQueryHandler, MessageHandler, filters, ContextTypes
from telegram.ext import TypeHandler

# 启用详细日志
from group.ai_group_reply import register_ai_group_reply_handlers
import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from telethon import TelegramClient, functions, types


BOT_TOKEN="8815482999:AAEYSrIB9bv8v2SolDHL5oQktg9_oIlIBgM"
API_ID=38759669
API_HASH="da5506797f6c82027d20712a9ef180fa"

guest_client = TelegramClient(
    "chatbot2030_guest_bot",
    API_ID,
    API_HASH,
)

async def send_guest_reply(
    query_id: str,
    text: str,
):
    print("\n📤 准备发送 Guest Bot 回复", flush=True)
    print(f"   query_id = {query_id}", flush=True)
    print(f"   text     = {text}", flush=True)
    
    
    reply_markup = types.ReplyInlineMarkup(
            rows=[
                types.KeyboardButtonRow(
                    buttons=[
                        types.KeyboardButtonUrl(
                            text="点击联系",
                            url="https://t.me/iwoai",
                        )
                    ]
                )
            ]
        )

    result = types.InputBotInlineResult(
        id="guest_reply_1",
        type="article",
        title="机器人回复",
        send_message=types.InputBotInlineMessageText(
            message=text,
            reply_markup=reply_markup,
        ),
    )
    
   

    response = await guest_client(
        functions.messages.SetBotGuestChatResultRequest(
            query_id=int(query_id),
            result=result,
        )
    )

    print("✅ Guest Bot 回复成功", flush=True)
    print(f"   result = {response}", flush=True)

    return response


# ============================================================
# Guest Message Handler
# ============================================================

async def guest_bot_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
   
    guest_message = (
        update.api_kwargs or {}
    ).get("guest_message")

    if not guest_message:
        print("ℹ️ 不是 Guest Message，忽略", flush=True)
        return

    # --------------------------------------------------------
    # 获取 query_id
    # --------------------------------------------------------

    query_id = guest_message.get(
        "guest_query_id"
    )

    if not query_id:
        print("❌ Guest Message 没有 guest_query_id", flush=True)
        return

    # --------------------------------------------------------
    # 获取用户信息
    # --------------------------------------------------------

    user = guest_message.get("from", {})

    user_id = user.get("id")
    username = user.get("username")
    first_name = user.get("first_name") or "用户"

    text = guest_message.get("text") or ""

    chat = guest_message.get("chat", {})

    # --------------------------------------------------------
    # 回复内容
    # --------------------------------------------------------

    reply_text = (
        f"🤖 你好，{first_name}！\n\n"
         f"我是 {text}。\n"
        "这里是测试数据。"
    )


    # --------------------------------------------------------
    # Guest Bot 回复
    # --------------------------------------------------------

    try:
        
        await send_guest_reply(
            query_id=query_id,
            text=reply_text,
        )

    except Exception as e:

        print(
            "\n❌ Guest Bot 回复失败",
            flush=True,
        )

        print(
            f"   类型: {type(e).__name__}",
            flush=True,
        )

        print(
            f"   错误: {e}",
            flush=True,
        )


# ============================================================
# Telethon 启动
# ============================================================

async def start_guest_client():
    print("🔌 正在启动 Guest Bot MTProto...", flush=True)

    await guest_client.start(
        bot_token=BOT_TOKEN,
    )

    me = await guest_client.get_me()

    print(
        f"✅ Guest Bot MTProto 已登录: "
        f"@{me.username}",
        flush=True,
    )


# ============================================================
# PTB 启动
# ============================================================
async def post_init(application):
    await start_guest_client()
    
async def post_shutdown(application):
    print("🔌 正在关闭 Guest Bot MTProto...", flush=True)
    await guest_client.disconnect()
    print("✅ Guest Bot MTProto 已关闭", flush=True)
 
def main():
    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    # 只要包含文本就放行，我们在函数内部去拆解 api_kwargs
    app.add_handler( TypeHandler(Update, guest_bot_handler))
    # register_ai_group_reply_handlers(app)
    app.run_polling()
        
if __name__ == "__main__":
    main()