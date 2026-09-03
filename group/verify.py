import asyncio
import html
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
from telegram.constants import ParseMode

from telegram.ext import (
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import mention_html
from group.grouplist import load_users, save_users

from command_router import register_command
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

JOIN_EVENT_DEDUP_SECONDS = 15
_recent_join_events = {}


def _is_new_member_join(old_status, new_status) -> bool:
    old_status = str(old_status or "").lower()
    new_status = str(new_status or "").lower()
    return old_status in {"left", "kicked"} and new_status in {
        "member",
        "restricted",
        "administrator",
        "creator",
        "owner",
    }


def _claim_join_event(chat_id: str, user_id: int) -> bool:
    """Avoid duplicate handling when both service and chat_member updates arrive."""
    now = time.monotonic()
    key = (str(chat_id), int(user_id))
    previous = _recent_join_events.get(key, 0.0)
    if previous and now - previous < JOIN_EVENT_DEDUP_SECONDS:
        return False
    _recent_join_events[key] = now
    if len(_recent_join_events) > 2000:
        cutoff = now - JOIN_EVENT_DEDUP_SECONDS
        for stale_key, timestamp in list(_recent_join_events.items()):
            if timestamp < cutoff:
                _recent_join_events.pop(stale_key, None)
    return True


async def _send_join_message(
    chat_id: str, text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None
):
    return await context.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def _process_new_members(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
    members: list,
    *,
    source: str,
):
    members = [member for member in members if member and _claim_join_event(chat_id, member.id)]
    if not members:
        return

    print(
        f"[入群事件] source={source} chat={chat_id} "
        f"members={','.join(str(member.id) for member in members)}"
    )
    group_config = get_group_whitelist(context).get(chat_id, {})
    welcome_msg_template = str(
        group_config.get("welcome_message") or "欢迎 {name} 🎉"
    )

    if not group_config.get("verify", False):
        if not bool(group_config.get("welcome", False)):
            return
        for member in members:
            try:
                welcome_msg = welcome_msg_template.format(
                    name=mention_html(member.id, member.full_name or "新成员"),
                    username=html.escape(f"@{member.username}") if member.username else "",
                    user_id=member.id,
                )
            except (KeyError, ValueError, IndexError):
                welcome_msg = (
                    f"欢迎 {mention_html(member.id, member.full_name or '新成员')} 🎉"
                )
            try:
                await _send_join_message(chat_id, welcome_msg, context)
                print(f"[入群欢迎] 已发送 chat={chat_id} user={member.id}")
            except Exception as exc:
                print(f"[入群欢迎] 发送失败 chat={chat_id} user={member.id}: {exc}")
        return

    bot_is_admin = await is_bot_admin(update, context)
    bot_username = (getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not bot_username:
        try:
            bot_username = (await context.bot.get_me()).username or ""
        except Exception as exc:
            print(f"[入群验证] 获取机器人用户名失败 chat={chat_id}: {exc}")

    for user in members:
        user_id = user.id
        keyboard = None
        if bot_username:
            verify_link = f"https://t.me/{bot_username}?start=verify_{user_id}"
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ 点此私聊验证身份", url=verify_link)]]
            )

        if bot_is_admin:
            try:
                await context.bot.restrict_chat_member(
                    int(chat_id), user_id, permissions=ChatPermissions(can_send_messages=False)
                )
                pending_verification.setdefault(chat_id, {})[
                    user_id
                ] = datetime.utcnow() + timedelta(minutes=2)
                override_chat_map[user_id] = chat_id
                tip_msg = await _send_join_message(
                    chat_id,
                    f"👋 欢迎 {mention_html(user.id, user.full_name or '新成员')}！"
                    "请在 2 分钟内私聊我进行验证，否则将被移出群组。",
                    context,
                    reply_markup=keyboard,
                )
                asyncio.create_task(auto_kick_if_not_verified(chat_id, user_id, context))
                asyncio.create_task(delete_later(tip_msg, delay=60 * 2))
                continue
            except Exception as exc:
                print(f"[入群验证] 禁言失败 chat={chat_id} user={user_id}: {exc}")

        try:
            await _send_join_message(
                chat_id,
                f"👋 欢迎 {mention_html(user.id, user.full_name or '新成员')}！"
                "请点击下面按钮私聊我进行验证。",
                context,
                reply_markup=keyboard,
            )
        except Exception as exc:
            print(f"[入群验证] 欢迎提示发送失败 chat={chat_id} user={user_id}: {exc}")


async def handle_new_member_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members or not update.effective_chat:
        return
    await _process_new_members(
        update,
        context,
        str(update.effective_chat.id),
        list(update.message.new_chat_members),
        source="new_chat_members",
    )


async def handle_chat_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for groups where Telegram does not deliver new_chat_members messages."""
    change = update.chat_member
    if not change or not change.chat or not _is_new_member_join(
        change.old_chat_member.status, change.new_chat_member.status
    ):
        return
    member = getattr(change.new_chat_member, "user", None)
    if not member or getattr(member, "is_bot", False):
        return
    await _process_new_members(
        update,
        context,
        str(change.chat.id),
        [member],
        source="chat_member",
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
        pass
    
        # 普通 /start，显示机器人介绍
#         await update.message.reply_text(
#             """📖 机器人用法说明！
# 欢迎使用娱乐机器人！本机器人可以学习说话，不定时回复，帮助大家在群聊天中获得更多乐趣！
# 奴隶买卖，结婚系统，我的农场，我的牧场，我的花园，完成订单
# 成语接龙，五子棋，谁是卧底，牛牛排行榜
# 群管功能，广告拦截，频道转发，更多功能尽情期待
# 频道转发有两种模式: 1.添加一个小号，小号为自己频道的管理员，小号关注要搬运的频道。配置好搬运规则便可搬运
# 交流建议群 @iabc6 或私发机器人，机器人会联系开发者 @nuan12。"""
#         )


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

        users = load_users(chat_id)
        uid = str(user_id)
        existing = users.get(uid, {}) if isinstance(users.get(uid, {}), dict) else {}
        users[uid] = {
            "full_name": query.from_user.full_name,
            "username": query.from_user.username,
            "username_history": existing.get("username_history", []),
            "join_time": int(time.time()),
            "last_seen": int(time.time()),
        }
        save_users(chat_id, users)

        await query.edit_message_text("✅ 已批准入群")

    except Exception as e:

        await query.edit_message_text("❌ 审核失败")


# 注册
def register_verification_handlers(app):

    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_group\|"))

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member_verify),
        # 与邀请统计（group=10）和用户记录（group=12）分开，避免同组内
        # 第一个匹配的 MessageHandler 截获入群欢迎事件。
        group=11,
    )
    app.add_handler(
        ChatMemberHandler(
            handle_chat_member_join,
            chat_member_types=ChatMemberHandler.CHAT_MEMBER,
        ),
        group=12,
    )

    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_user_left)
    )
    app.add_handler(CommandHandler("start", start_command))

    # 命令处理器
    app.add_handler(CommandHandler("setwelcome", set_welcome))
