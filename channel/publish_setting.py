# -*- coding: utf-8 -*-
from datetime import datetime
import os
import random
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from channel.channel_config import USER_MESSAGE_FILE
from utils import BOT_USER_FILE, load_json, save_json

PUBLISH_CONFIG_FILE = "config_data/publish_config.json"
ANON_CHAT_FILE = "data/anon_chat.json"
USER_MESSAGE_FILE = "data/user_message_file.json"
BOTTLE_HISTORY_FILE = "data/bottle_history.json"

CALLBACK_PREFIX = "publish"

# =========================
# 配置读写
# =========================

def load_publish_config():
    default = {
        "channel_id": None,
        "review_enabled": False,
        "daily_limit": 0,
        "ads_enabled": False,
        "ads": []
    }


    data = load_json(PUBLISH_CONFIG_FILE)
    
    if not data:
        data = default; 
        save_json(PUBLISH_CONFIG_FILE, data) 
        
    return data


def save_publish_config(data):
    save_json(PUBLISH_CONFIG_FILE, data)


# =========================
# 键盘
# =========================
def publish_setting_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 发布频道", callback_data="publish:channel")],
        [InlineKeyboardButton("📝 审核设置", callback_data="publish:review")],
        [InlineKeyboardButton("📊 每日发布上限", callback_data="publish:limit")],
        [InlineKeyboardButton("📣 广告管理", callback_data="publish:ads")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="start:back")]
    ])


def publish_channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 修改频道", callback_data="publish:set_channel")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_review_keyboard(enabled):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ 已开启' if enabled else '❌ 已关闭'}",
                callback_data="publish:toggle_review"
            )
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_limit_keyboard(limit):
    text = "♾ 不限制" if limit <= 0 else str(limit)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"当前：{text}", callback_data="noop")],
        [InlineKeyboardButton("✏️ 修改上限", callback_data="publish:set_limit")],
        [InlineKeyboardButton("🚫 不限制", callback_data="publish:unlimit")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


def publish_ads_keyboard(enabled):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ 已启用' if enabled else '❌ 已禁用'}",
                callback_data="publish:toggle_ads"
            )
        ],
        [InlineKeyboardButton("➕ 添加广告", callback_data="publish:add_ad")],
        [InlineKeyboardButton("📝 编辑广告", callback_data="publish:edit_ad")],
        [InlineKeyboardButton("🗑 删除广告", callback_data="publish:delete_ad")],
        [InlineKeyboardButton("📋 广告列表", callback_data="publish:list_ad")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")]
    ])


# =========================
# 回调处理
# =========================

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith("publish:"):
        return

    await query.answer()

    config = load_json(PUBLISH_CONFIG_FILE)
    channel_id = config.get("channel_id")
    
    action = query.data.split(":")[1]
    
    if action == "publishset":
        help_text = "📣 请设置发布的频道"       
        await query.edit_message_text(
            help_text,
            reply_markup=publish_setting_keyboard()
        )

    if action == "channel":
        
        text = (
            f"📢 当前频道：\n{channel_id}"
            if channel_id
            else "📢 当前未设置发布频道"
        )

        return await query.edit_message_text(
            text,
            reply_markup=publish_channel_keyboard()
        )

    if action == "set_channel":
        context.user_data["waiting_channel_id"] = True

        return await query.edit_message_text(
            "请输入频道ID\n\n例如：\n-1001234567890"
        )

    if action == "review":
        return await query.edit_message_text(
            f"📝 审核状态：{'开启' if config['review_enabled'] else '关闭'}",
            reply_markup=publish_review_keyboard(
                config["review_enabled"]
            )
        )

    if action == "toggle_review":
        config["review_enabled"] = not config["review_enabled"]
        save_publish_config(config)

        return await query.edit_message_text(
            f"📝 审核状态：{'开启' if config['review_enabled'] else '关闭'}",
            reply_markup=publish_review_keyboard(
                config["review_enabled"]
            )
        )

    if action == "limit":
        limit = config["daily_limit"]

        return await query.edit_message_text(
            f"📊 当前每日发布上限：{'♾ 不限制' if limit <= 0 else limit}",
            reply_markup=publish_limit_keyboard(limit)
        )

    if action == "set_limit":
        context.user_data["waiting_limit"] = True

        return await query.edit_message_text(
            "请输入每日发布上限数字"
        )

    if action == "unlimit":
        config["daily_limit"] = 0
        save_publish_config(config)

        return await query.edit_message_text(
            "✅ 已设置为不限制",
            reply_markup=publish_limit_keyboard(0)
        )

    if action == "ads":
        return await query.edit_message_text(
            f"📣 广告状态：{'开启' if config['ads_enabled'] else '关闭'}",
            reply_markup=publish_ads_keyboard(
                config["ads_enabled"]
            )
        )

    if action == "toggle_ads":
        config["ads_enabled"] = not config["ads_enabled"]
        save_publish_config(config)

        return await query.edit_message_text(
            f"📣 广告状态：{'开启' if config['ads_enabled'] else '关闭'}",
            reply_markup=publish_ads_keyboard(
                config["ads_enabled"]
            )
        )

    if action == "add_ad":

        context.user_data["waiting_add_ad"] = True

        return await query.edit_message_text(
            "请输入广告内容：\n\n例如：\n欢迎加入交流群 https://t.me/xxx"
        )
    if action == "list_ad":

        ads = config.get("ads", [])

        if not ads:
            return await query.edit_message_text(
                "暂无广告"
            )

        lines = ["📋 广告列表", ""]
        rows = []
        
        for ad in ads:
            lines.append(
                f"#{ad['id']} {'✅' if ad['enabled'] else '❌'}"
            )

            rows.append([
                InlineKeyboardButton(
                    f"{'✅' if ad['enabled'] else '❌'} #{ad['id']}",
                    callback_data=f"publish:toggle_ad_{ad['id']}"
                )
            ])

        return await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows)
        )

    if action == "delete_ad":

        ads = config.get("ads", [])

        if not ads:
            return await query.edit_message_text("暂无广告")

        rows = []

        for ad in ads:
            rows.append([
                InlineKeyboardButton(
                    f"🗑 删除 #{ad['id']}",
                    callback_data=f"publish:delete_ad_{ad['id']}"
                )
            ])

        rows.append([
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="publish:ads"
            )
        ])

        return await query.edit_message_text(
            "请选择要删除的广告",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    if action.startswith("delete_ad_"):

        ad_id = int(action.replace("delete_ad_", ""))

        ads = config.get("ads", [])

        ads = [x for x in ads if x["id"] != ad_id]

        config["ads"] = ads

        save_publish_config(config)

        return await query.edit_message_text(
            "✅ 删除成功"
        )
    if action == "edit_ad":

        ads = config.get("ads", [])

        rows = []

        for ad in ads:
            rows.append([
                InlineKeyboardButton(
                    f"📝 #{ad['id']}",
                    callback_data=f"publish:edit_ad_{ad['id']}"
                )
            ])

        return await query.edit_message_text(
            "选择广告",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    if action.startswith("edit_ad_"):

        ad_id = int(action.replace("edit_ad_", ""))

        context.user_data["edit_ad_id"] = ad_id
        context.user_data["waiting_edit_ad"] = True

        return await query.edit_message_text(
            f"请输入新的广告内容\n广告ID:{ad_id}"
        )
    
    if action.startswith("toggle_ad_"):

        ad_id = int(action.replace("toggle_ad_", ""))

        for ad in config["ads"]:
            if ad["id"] == ad_id:
                ad["enabled"] = not ad["enabled"]
                break
        
        save_publish_config(config)
        
        await query.edit_message_text(
        "📋 广告列表（点击切换启用状态）",
        reply_markup=build_ads_list_keyboard(config["ads"])
    ) 
        return
        
    if action == "back":
        return await query.edit_message_text(
            "⚙️ 发布设置",
            reply_markup=publish_setting_keyboard()
        )
    
    if action == "publish":
        # 发布
        await publish_message(update, context)
        
    if action == "channel_message":
        
        context.user_data["reply_bottle"] = True
        
        user_id = query.from_user.id

        posts = _load_cannel_message()

        history_data = _load_bottle_history()

        user_key = str(user_id)

        if user_key not in history_data:
            history_data[user_key] = {
                "history": [],
                "index": -1
            }

        user_info = history_data[user_key]

        history = user_info["history"]

        available_posts = [
            p for p in posts
            if p["user_id"] != user_id
            and p["channel_message_id"] not in history
        ]

        if not available_posts:
            await query.message.reply_text("没有更多瓶子了")
            return

        post = random.choice(available_posts)

        history.append(post["channel_message_id"])

        user_info["index"] = len(history) - 1

        _save_bottle_history(history_data)

        await send_bottle(
            context,
            user_id,
            channel_id,
            post
        )

    if action == "global_ad_toggle":
       
        enabled = not context.user_data["post_no_name"]
        
        await query.answer("✅ 已更新", show_alert=False)
        
        
        help_text = "📣 请发送您要的内容。\n\n支持文字、图片、视频等消息。"
    
        context.user_data["post_no_name"] = enabled
        
        await query.edit_message_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )

    if action == "bottle_next":
        user_id = query.from_user.id

        posts = _load_cannel_message()

        history_data = _load_bottle_history()

        user_info = history_data.get(str(user_id))

        if not user_info:
            await query.message.reply_text("请先捞一个瓶子")
            return

        history = user_info["history"]
        index = user_info["index"]

        # 已浏览历史里还有下一条
        if index < len(history) - 1:

            index += 1

            user_info["index"] = index

            _save_bottle_history(history_data)

            message_id = history[index]

        else:

            available_posts = [
                p for p in posts
                if p["user_id"] != user_id
                and p["channel_message_id"] not in history
            ]

            if not available_posts:
                await query.message.reply_text("没有更多瓶子了")
                return

            post = random.choice(available_posts)

            history.append(post["channel_message_id"])

            user_info["index"] = len(history) - 1

            _save_bottle_history(history_data)

            message_id = post["channel_message_id"]

        await query.message.delete()

        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_id=message_id,
            reply_markup=query.message.reply_markup
        )

    if action == "bottle_prev":

        user_id = query.from_user.id

        history_data = _load_bottle_history()

        user_info = history_data.get(str(user_id))

        if not user_info:
            return

        index = user_info["index"]

        if index <= 0:
            await query.message.reply_text(
                "已经是第一条了"
            )
            return

        index -= 1

        user_info["index"] = index

        _save_bottle_history(history_data)

        message_id = user_info["history"][index]

        await query.message.delete()

        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_id=message_id,
            reply_markup=query.message.reply_markup
        )
    
    if action == "add_friend":

        target_user_id = int(query.data.split(":")[2])
        target_user_id = int(target_user_id)
        from_user_id = query.from_user.id
        
        history_data = _load_bottle_history()
        user_key = str(from_user_id)
        friend_applied = history_data[user_key].setdefault(
            "friend_applied",
            {}
        )
        
        if str(target_user_id) in friend_applied:
            await query.message.reply_text(
                "你已经发送过好友申请了"
            )
            return
        
        accepter = get_user(from_user_id)
        
        await context.bot.send_message(
            chat_id=target_user_id,
            text= (
                f"@{accepter['username']}  想认识你" 
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ 同意",
                        callback_data=f"publish:accept_friend:{from_user_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ 拒绝",
                        callback_data=f"publish:reject_friend:{from_user_id}"
                    )
                ]
            ])
        )
        
        await query.message.reply_text(
            "好友申请发送成功"
        )
        
        friend_applied[str(target_user_id)] = True
        _save_bottle_history(history_data)
    if action == "accept_friend":
        requester_id = int(query.data.split(":")[2])

        accepter_id = query.from_user.id

        print("申请人:", requester_id)
        print("同意人:", accepter_id)

        # 从数据库读取双方信息
        requester = get_user(requester_id)
        accepter = get_user(accepter_id)

        # 通知申请人
        await context.bot.send_message(
            chat_id=requester_id,
            text=(
                "🎉 对方已同意交换联系方式\n\n"
                f"用户名：@{accepter['username']}"
            )
        )

        await query.message.delete()
         
        # 通知同意人
        await query.message.reply_text(
            f"已向对方发送你的联系方式，对方联系方式：@{requester['username']}"
        )
        
       
    
    if action == "reject_friend":
        requester_id = int(query.data.split(":")[2])

        await context.bot.send_message(
            chat_id=requester_id,
            text="❌ 对方拒绝了你的好友申请"
        )

        await query.message.delete()
        
        await query.message.reply_text("已拒绝")
        
async def publish_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["waiting_post"] = True
        
    enabled = context.user_data.get("post_no_name", True)
    
    help_text = "📣 请发送您要投稿的内容。\n\n支持文字、图片、视频等消息。"
    
    context.user_data["post_no_name"] = enabled
    
    if query:
        
        await query.edit_message_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )
        
    else:
        await update.message.reply_text(
            help_text,
            reply_markup=create_post_keyboard(enabled)
        )
    
def create_post_keyboard(enabled: bool):
    rows = [
        [
            # InlineKeyboardButton(
            #     f"{'✅' if enabled else '🚫'} 匿名模式",
            #     callback_data=f"{CALLBACK_PREFIX}:global_ad_toggle",
            # ),
            InlineKeyboardButton(
                "✅ 继续扔",
                callback_data="publish:publish",
            ),
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="start:back",
            ),
        ]
    ]
    return InlineKeyboardMarkup(rows)


async def handle_wall_publish(update, context):
    if not context.user_data.get("waiting_post"):
        return
    
    msg = update.message
    config = load_json(PUBLISH_CONFIG_FILE)
    channel_id = config.get("channel_id")
    
    if not channel_id:
        await msg.reply_text("✅ 暂未配置大海地址，感谢参与！")
        return
    
    try:
        published = await context.bot.copy_message(
            chat_id=channel_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id
        )
        
        data =  _load_cannel_message()
        
        data.append({
            "user_id": msg.from_user.id,
            "user_chat_id": msg.chat_id,
            "username": msg.from_user.username,
            "user_message_id": msg.message_id,       # 用户原消息ID
            "channel_message_id": published.message_id,  # 频道消息ID
            "publish_time": int(time.time())
        })
        
        save_json(USER_MESSAGE_FILE, data)
        await msg.reply_text("✅ 发送成功")
        
    except Exception as e:
        print("投稿失败:", e)
        await msg.reply_text(f"❌ 发送失败：{e}")
    
    context.user_data["waiting_post"] = False
        
    # await publish_message(update, context)
   
# =========================
# 文本输入处理
# =========================

async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await handle_wall_publish(update, context)

    if not update.message.text:
        return
    
    config = load_json(PUBLISH_CONFIG_FILE)
    
    if context.user_data.get("waiting_channel_id"):
        context.user_data["waiting_channel_id"] = False

        channel_id = update.message.text.strip()
        config["channel_id"] = int(channel_id)

        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 已保存频道：{channel_id}"
        )

    if context.user_data.get("waiting_limit"):
        context.user_data["waiting_limit"] = False

        limit = int(update.message.text.strip())

 
        config["daily_limit"] = limit

        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 已设置每日上限：{limit}"
        )
    if context.user_data.get("waiting_add_ad"):

        context.user_data["waiting_add_ad"] = False


        ads = config.get("ads", [])

        ad_id = max([x["id"] for x in ads], default=0) + 1

        ads.append({
            "id": ad_id,
            "title": f"广告{ad_id}",
            "content": update.message.text,
            "enabled": True
        })

        config["ads"] = ads
        save_publish_config(config)

        return await update.message.reply_text(
            f"✅ 广告添加成功\nID: {ad_id}"
        )
    if context.user_data.get("waiting_edit_ad"):

        context.user_data["waiting_edit_ad"] = False

        ad_id = context.user_data.pop("edit_ad_id")



        for ad in config["ads"]:
            if ad["id"] == ad_id:
                ad["content"] = update.message.text
                break

        save_publish_config(config)

        return await update.message.reply_text(
            "✅ 广告修改成功"
        )
        
def build_ads_list_keyboard(ads):
    rows = []

    for ad in ads:
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if ad['enabled'] else '❌'} #{ad['id']} {ad['content'][:20]}",
                callback_data=f"publish:toggle_ad_{ad['id']}"
            )
        ])

    rows.append([
        InlineKeyboardButton("⬅️ 返回", callback_data="publish:ads")
    ])

    return InlineKeyboardMarkup(rows)   
    
def get_user(user_id):
    users = load_json(BOT_USER_FILE)
    return users.get(str(user_id))       

def _load_bottle_history():
    data = load_json(BOTTLE_HISTORY_FILE)
    return data if isinstance(data, dict) else {}

def _save_bottle_history(data):
    save_json(BOTTLE_HISTORY_FILE, data)

def get_user_bottle_data(user_id):
    data = _load_bottle_history()

    user_key = str(user_id)

    if user_key not in data:
        data[user_key] = {
            "history": [],
            "index": -1
        }

    return data

async def send_bottle(
    context,
    chat_id,
    channel_id,
    post
):

    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ 上一条",
                callback_data="publish:bottle_prev"
            ),
            InlineKeyboardButton(
                "👤 添加好友",
                callback_data=f"publish:add_friend:{post['user_id']}"
            ),
            InlineKeyboardButton(
                "➡️ 下一条",
                callback_data="publish:bottle_next"
            ),
        ]
    ]

    await context.bot.copy_message(
        chat_id=chat_id,
        from_chat_id=channel_id,
        message_id=post["channel_message_id"],
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def _load_cannel_message() -> list:
    data = load_json(USER_MESSAGE_FILE)
    return data if isinstance(data, list) else []

    
# =========================
# 注册
# =========================

def register_publish_setting_handlers(app):
    app.add_handler( CallbackQueryHandler( _handle_callback, pattern=r"^publish:.+"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND),_handle_text_input), group=10)