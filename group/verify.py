import asyncio
from datetime import datetime, timedelta
import logging
import time

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from telegram.constants import ChatMemberStatus

from telegram.ext import (
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

from command_router import (
    FEATURE_WELCOME,
    is_feature_enabled,
    register_command,
)
from utils import (
    BOT_ID,
    BOT_USER_FILE,
    GROUP_LIST_FILE,
    get_group_whitelist,
    delete_later,
    is_bot_admin,
    load_json,
    save_json,
)

# 验证记录：chat_id -> {user_id: 到期时间}
pending_verification = {}
# 映射用户 ID 到验证群 ID
override_chat_map = {}

async def handle_new_member_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = str(update.effective_chat.id)

    group_config = get_group_whitelist(context).get(chat_id, {})
    welcome_msg_template = group_config.get("welcome_message", "欢迎 {name} 🎉")

    if not group_config.get("verify", False):

        # 静默模式不提醒
        if not is_feature_enabled(chat_id, FEATURE_WELCOME):
            return

        for member in update.message.new_chat_members:
            welcome_msg = welcome_msg_template.format(name=member.full_name)
            await update.message.reply_text(welcome_msg)
        return

    # 获取机器人自身权限
    bot_is_admin = await is_bot_admin(update, context)

    for user in update.message.new_chat_members:
        user_id = user.id

        # 私聊验证链接（无论是否能禁言，都生成）
        bot_username = (await context.bot.get_me()).username
        verify_link = f"https://t.me/{bot_username}?start=verify_{user_id}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ 点此私聊验证身份", url=verify_link)]]
        )

        if bot_is_admin:
            # 机器人有权限时，禁言+验证
            await context.bot.restrict_chat_member(
                chat_id, user_id, permissions=ChatPermissions(can_send_messages=False)
            )
            # 记录验证时间（2分钟）
            pending_verification.setdefault(chat_id, {})[
                user_id
            ] = datetime.utcnow() + timedelta(minutes=2)
            override_chat_map[user_id] = chat_id
            # 发送提示
            tip_msg = await update.message.reply_text(
                f"👋 欢迎 {user.full_name}！请在 2 分钟内私聊我进行验证，否则将被移出群组。",
                reply_markup=keyboard,
            )
            # 启动自动踢
            asyncio.create_task(auto_kick_if_not_verified(chat_id, user_id, context))

            asyncio.create_task(delete_later(tip_msg, delay=60 * 2))
        else:
            # 机器人不是管理员，只发送私聊验证提示
            await update.message.reply_text(
                f"👋 欢迎 {user.full_name}！请点击下面按钮私聊我进行验证。",
                reply_markup=keyboard,
            )


# /start 私聊入口
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
 
    args = context.args
    user_id = update.effective_user.id
    user = update.effective_user

    # ✅ 记录用户到固定列表（只记录第一次出现的用户）
    users = load_json(BOT_USER_FILE) or {}
    uid = str(user.id)

    users[uid] = {
        "name": user.first_name or "",
        "username": user.username or "",
        "join_time": users.get(uid, {}).get("join_time", int(time.time())),
        "last_active": int(time.time()),
        "blocked": False
    }

    save_json(BOT_USER_FILE, users)

    # 私聊验证逻辑
    if args and args[0].startswith("verify_"):
        verify_id = int(args[0].split("_")[1])

        if user_id != verify_id:
            await update.message.reply_text("⛔ 无效验证请求。")
            return

        chat_id = override_chat_map.get(user_id)
        if not chat_id:
            await update.message.reply_text("⚠️ 验证已过期或无效。")
            return

        # 解禁该用户
        await context.bot.restrict_chat_member(
            chat_id, user_id, permissions=ChatPermissions(can_send_messages=True)
        )

        # 清除记录
        pending_verification.get(chat_id, {}).pop(user_id, None)
        override_chat_map.pop(user_id, None)

        await update.message.reply_text("✅ 验证成功！你现在可以在群里发言了。")
        #  群提醒 关掉
        # await context.bot.send_message(
        #     chat_id,
        #     f"✅ 用户 [{update.effective_user.full_name}](tg://user?id={user_id}) 验证成功！",
        #     parse_mode="Markdown",
        # )
    else:

        # 普通 /start，显示机器人介绍
        await update.message.reply_text(
            """📖 机器人用法说明！
欢迎使用娱乐机器人！本机器人可以学习说话，不定时回复，帮助大家在群聊天中获得更多乐趣！
奴隶买卖，结婚系统，我的农场，我的牧场，我的花园，完成订单
成语接龙，五子棋，谁是卧底，牛牛排行榜
群管功能，广告拦截，频道转发，更多功能尽情期待
交流建议群 @dubai_mm 或私发机器人，机器人会联系开发者 @nuan12。"""
        )


# 自动踢出未验证用户
async def auto_kick_if_not_verified(chat_id, user_id, context):
    await asyncio.sleep(120)  # 等 2 分钟
    expire = pending_verification.get(chat_id, {}).get(user_id)
    if expire and datetime.utcnow() > expire:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)

            # await context.bot.send_message(
            #     chat_id, f"🚫 用户 {user_id} 未及时验证，已被移出群组。"
            # )
        except:
            pass
        finally:
            pending_verification.get(chat_id, {}).pop(user_id, None)
            override_chat_map.pop(user_id, None)


async def handle_user_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return
    left_user = update.message.left_chat_member
    from_user = update.message.from_user

    chat_id = str(update.effective_chat.id)
    user_id = left_user.id

    # 如果被踢的是机器人自己，直接跳过
    if left_user.id == BOT_ID:
        return

    if user_id == from_user.id:
        # 用户主动退出
        await update.message.reply_text(f"👋 用户 {left_user.full_name} 自行退出群组。")
    else:
        #
        await update.message.reply_text(
            f"⚠️ 用户 {left_user.full_name} 被 {from_user.full_name}移出群组！"
        )


# 开关验证功能
@register_command("设置欢迎词")
async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    # 检查用户是否为管理员
    member = await update.effective_chat.get_member(update.effective_user.id)
    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("❌ 只有管理员才能设置欢迎词。")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ 用法: /setwelcome 欢迎词内容，可以使用 {name} 代表新成员的名字"
        )
        return

    welcome_text = " ".join(context.args)

    # 按群 ID 保存欢迎词
    group_whitelist = get_group_whitelist(context)
    if chat_id not in group_whitelist:
        group_whitelist[chat_id] = {}
    group_whitelist[chat_id]["welcome_message"] = welcome_text
    save_json(GROUP_LIST_FILE, group_whitelist)

    await update.message.reply_text(
        f"✅ 群 {chat_id} 的欢迎词已更新为:\n{welcome_text}"
    )

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    user = request.from_user
    chat = request.chat
    # 按钮 callback_data 带 chat_id + user_id
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ 点击验证加入群", callback_data=f"verify_group|{chat.id}|{user.id}"
                )
            ]
        ]
    )

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"欢迎申请加入 {chat.title} 👋\n\n请点击下方按钮完成验证。",
            reply_markup=keyboard,
        )
  
    except Exception as e:
        pass


# ---------- 2️⃣ 用户点击按钮 ----------
async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, chat_id_str, user_id_str = query.data.split("|")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
      
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    

        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)

        await query.edit_message_text("✅ 已批准入群")

    except Exception as e:

        await query.edit_message_text("❌ 审核失败")


# 注册
def register_verification_handlers(app):

    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_group\|"))

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member_verify)
    )

    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_user_left)
    )
    app.add_handler(CommandHandler("start", start_command))

    # 命令处理器
    app.add_handler(CommandHandler("setwelcome", set_welcome))
