# -*- coding: utf-8 -*-
from datetime import datetime
from urllib.parse import urlparse
import os
import random
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from channel.channel_config import USER_MESSAGE_FILE
from utils import BOT_USER_FILE, load_json, save_json

PUBLISH_CONFIG_FILE = "config_data/publish_config.json"
ANON_CHAT_FILE = "data/anon_chat.json"
USER_MESSAGE_FILE = "data/user_message_file.json"
BOTTLE_HISTORY_FILE = "data/bottle_history.json"
PENDING_SUBMISSIONS_FILE = "data/pending_submissions.json"

CALLBACK_PREFIX = "publish"
BUTTON_TEXT_MAX_LENGTH = 64
MAX_PUBLISH_BUTTONS = 20

# =========================
# 配置读写
# =========================

def load_publish_config():
    default = {
        "channel_id": None,
        "review_enabled": False,
        "daily_limit": 0,
        "ads_enabled": False,
        "ads": [],
        "buttons": [],
        # Keep existing bots' public buttons visible until an owner changes
        # this setting in 投稿配置.
        "bottom_buttons_enabled": True,
        "submission_enabled": False,
        "random_view_enabled": False,
    }

    data = load_json(PUBLISH_CONFIG_FILE)
    if not isinstance(data, dict) or not data:
        data = default.copy()
        save_json(PUBLISH_CONFIG_FILE, data)
        return data

    changed = False
    for key, value in default.items():
        if key not in data:
            data[key] = value.copy() if isinstance(value, list) else value
            changed = True
    if changed:
        save_json(PUBLISH_CONFIG_FILE, data)

    return data


def save_publish_config(data):
    save_json(PUBLISH_CONFIG_FILE, data)


def _load_pending_submissions() -> dict:
    """Load review queue. JSON storage keeps pending reviews after a restart."""
    data = load_json(PENDING_SUBMISSIONS_FILE)
    return data if isinstance(data, dict) else {}


def _save_pending_submissions(data: dict) -> None:
    save_json(PENDING_SUBMISSIONS_FILE, data)


def _owner_id(context: ContextTypes.DEFAULT_TYPE):
    """Read the owner from this app instead of a process-global multi-bot value."""
    try:
        return int(context.application.bot_data.get("owner_id"))
    except (TypeError, ValueError):
        return None


def _review_keyboard(submission_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 通过并发布",
                callback_data=f"publish:review_approve:{submission_id}",
            ),
            InlineKeyboardButton(
                "❌ 拒绝",
                callback_data=f"publish:review_reject:{submission_id}",
            ),
        ]
    ])


def _submission_author_text(msg) -> str:
    user = getattr(msg, "from_user", None)
    if not user:
        return "未知用户"
    name = " ".join(
        part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if part
    ).strip()
    username = getattr(user, "username", None)
    if username:
        return f"{name or '用户'} (@{username}, ID: {user.id})"
    return f"{name or '用户'} (ID: {user.id})"


def _append_published_submission(msg, published_message_id: int) -> None:
    """Store a published submission for the existing random-view feature."""
    user = getattr(msg, "from_user", None)
    data = _load_cannel_message()
    data.append({
        "user_id": getattr(user, "id", None),
        "user_chat_id": msg.chat_id,
        "username": getattr(user, "username", None),
        "user_message_id": msg.message_id,
        "channel_message_id": published_message_id,
        "publish_time": int(time.time()),
    })
    save_json(USER_MESSAGE_FILE, data)


async def _handle_review_callback(query, context: ContextTypes.DEFAULT_TYPE, config: dict):
    """Approve or reject a submission from the owner review message."""
    parts = query.data.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        return await query.answer("审核数据无效。", show_alert=True)

    owner_id = _owner_id(context)
    if owner_id is None or query.from_user.id != owner_id:
        return await query.answer("只有机器人所有者可以审核投稿。", show_alert=True)

    action, submission_id = parts[1], parts[2]
    pending = _load_pending_submissions()
    submission = pending.get(submission_id)
    if not isinstance(submission, dict):
        return await query.answer("该投稿不存在或已清理。", show_alert=True)

    status = submission.get("status", "pending")
    if status != "pending":
        status_text = {
            "approved": "已通过",
            "rejected": "已拒绝",
            "publishing": "正在发布中",
        }.get(status, "已处理")
        return await query.answer(f"该投稿{status_text}，请勿重复处理。", show_alert=True)

    if action == "review_approve":
        channel_id = config.get("channel_id")
        if not channel_id:
            return await query.answer("未配置发布频道，无法发布。", show_alert=True)

        # Persist a transient state first to prevent two owner clicks from publishing twice.
        submission["status"] = "publishing"
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        try:
            published = await context.bot.copy_message(
                chat_id=channel_id,
                from_chat_id=submission["user_chat_id"],
                message_id=submission["user_message_id"],
                reply_markup=publish_buttons_keyboard(config),
            )
        except Exception as exc:
            submission["status"] = "pending"
            pending[submission_id] = submission
            _save_pending_submissions(pending)
            print("审核投稿发布失败:", exc)
            return await query.answer(
                "发布失败，请检查频道配置和机器人权限后重试。",
                show_alert=True,
            )

        submission["status"] = "approved"
        submission["reviewed_at"] = int(time.time())
        submission["channel_message_id"] = published.message_id
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        # Keep this compatible with the existing random-view function.
        data = _load_cannel_message()
        data.append({
            "user_id": submission.get("user_id"),
            "user_chat_id": submission["user_chat_id"],
            "username": submission.get("username"),
            "user_message_id": submission["user_message_id"],
            "channel_message_id": published.message_id,
            "publish_time": int(time.time()),
        })
        save_json(USER_MESSAGE_FILE, data)

        try:
            await context.bot.send_message(
                chat_id=submission["user_chat_id"],
                text="✅ 您的投稿已审核通过，并已发布到频道。",
            )
        except Exception as exc:
            print("投稿通过通知失败:", exc)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            print("更新审核消息失败:", exc)
        await query.answer("投稿已通过并发布。")
        return await query.message.reply_text("✅ 已通过投稿并发布到频道。")

    if action == "review_reject":
        submission["status"] = "rejected"
        submission["reviewed_at"] = int(time.time())
        pending[submission_id] = submission
        _save_pending_submissions(pending)

        try:
            await context.bot.send_message(
                chat_id=submission["user_chat_id"],
                text="❌ 很抱歉，您的投稿未通过审核。",
            )
        except Exception as exc:
            print("投稿拒绝通知失败:", exc)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            print("更新审核消息失败:", exc)
        await query.answer("投稿已拒绝。")
        return await query.message.reply_text("❌ 已拒绝投稿，已通知投稿人。")

    return await query.answer("未知审核操作。", show_alert=True)


def _publish_buttons(config: dict) -> list[dict]:
    """Return valid custom buttons from the current publish configuration."""
    buttons = config.get("buttons", []) if isinstance(config, dict) else []
    if not isinstance(buttons, list):
        return []
    return [button for button in buttons if isinstance(button, dict)]


def _button_settings_text(config: dict) -> str:
    buttons = _publish_buttons(config)
    lines = [
        "🔘 投稿按钮设置",
        "投稿发布到频道时，会在消息下方附加这些链接按钮。",
        "",
    ]
    if not buttons:
        lines.append("当前未设置按钮。")
    else:
        lines.append(f"当前已设置 {len(buttons)} 个按钮：")
        for button in buttons:
            lines.append(
                f"#{button.get('id')} {button.get('text', '未命名')}\n{button.get('url', '')}"
            )
    return "\n".join(lines)


def publish_buttons_keyboard(config: dict):
    """Build the URL keyboard appended to a published submission."""
    if not bool(config.get("bottom_buttons_enabled", True)):
        return None

    rows = []
    for button in _publish_buttons(config):
        button_text = str(button.get("text", "")).strip()
        button_url = str(button.get("url", "")).strip()
        if button_text and button_url:
            rows.append([InlineKeyboardButton(button_text, url=button_url)])
    return InlineKeyboardMarkup(rows) if rows else None


def publish_button_settings_keyboard(config: dict):
    buttons = _publish_buttons(config)
    rows = [[InlineKeyboardButton("➕ 添加按钮", callback_data="publish:button_add")]]
    if buttons:
        rows.extend(
            [
                [InlineKeyboardButton("📝 修改按钮", callback_data="publish:button_edit")],
                [InlineKeyboardButton("🗑 删除按钮", callback_data="publish:button_delete")],
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:back")])
    return InlineKeyboardMarkup(rows)


def _button_selection_keyboard(config: dict, action: str):
    rows = []
    for button in _publish_buttons(config):
        button_id = button.get("id")
        button_text = str(button.get("text", "未命名"))[:40]
        prefix = "📝" if action == "edit" else "🗑"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{prefix} #{button_id} {button_text}",
                    callback_data=f"publish:button_{action}_{button_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="publish:buttons")])
    return InlineKeyboardMarkup(rows)


def _normalize_button_url(value: str):
    url = (value or "").strip()
    if url.startswith("t.me/"):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "tg"} or not parsed.netloc and parsed.scheme != "tg":
        return None
    return url


def _clear_button_input(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("publish_button_input", None)


# =========================
# 键盘
# =========================
def publish_setting_keyboard(config: dict):
    submission_enabled = bool(config.get("submission_enabled", True))
    random_view_enabled = bool(config.get("random_view_enabled", True))
    bottom_buttons_enabled = bool(config.get("bottom_buttons_enabled", True))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 发布频道", callback_data="publish:channel")],
        [InlineKeyboardButton("📝 审核设置", callback_data="publish:review")],
        # [InlineKeyboardButton("📊 每日发布上限", callback_data="publish:limit")],
        # [InlineKeyboardButton("📣 广告管理", callback_data="publish:ads")],
        [InlineKeyboardButton("🔘 底部按钮设置", callback_data="publish:buttons")],
        [
            InlineKeyboardButton(
                f"{'✅' if bottom_buttons_enabled else '🚫'} 显示底部按钮",
                callback_data="publish:toggle_bottom_buttons",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if submission_enabled else '🚫'} 投稿开关",
                callback_data="publish:toggle_submission",
            ),
            InlineKeyboardButton(
                f"{'✅' if random_view_enabled else '🚫'} 随机查看开关",
                callback_data="publish:toggle_random_view",
            ),
        ],
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

    config = load_publish_config()
    channel_id = config.get("channel_id")
    action = query.data.split(":")[1]

    if action in {"review_approve", "review_reject"}:
        return await _handle_review_callback(query, context, config)

    await query.answer()
    
    if action == "publishset":
        help_text = "📣 请设置发布的频道"       
        await query.edit_message_text(
            help_text,
            reply_markup=publish_setting_keyboard(config)
        )

    if action == "toggle_submission":
        config["submission_enabled"] = not bool(config.get("submission_enabled", True))
        save_publish_config(config)
        return await query.edit_message_text(
            f"投稿开关已{'开启' if config['submission_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_random_view":
        config["random_view_enabled"] = not bool(config.get("random_view_enabled", True))
        save_publish_config(config)
        return await query.edit_message_text(
            f"随机查看开关已{'开启' if config['random_view_enabled'] else '关闭'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "toggle_bottom_buttons":
        config["bottom_buttons_enabled"] = not bool(
            config.get("bottom_buttons_enabled", True)
        )
        save_publish_config(config)
        return await query.edit_message_text(
            f"发布底部按钮已{'显示' if config['bottom_buttons_enabled'] else '隐藏'}。",
            reply_markup=publish_setting_keyboard(config),
        )

    if action == "channel":
        
        text = (
            f"📢 当前频道：\n{channel_id}"
            if channel_id
            else "📢 当前未设置发布频道。请将频道任意一条消息转发给机器人，获取频道 ID 后输入即可设置。"
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
        
    if action == "buttons":
        _clear_button_input(context)
        return await query.edit_message_text(
            _button_settings_text(config),
            reply_markup=publish_button_settings_keyboard(config),
        )

    if action == "button_add":
        buttons = _publish_buttons(config)
        if len(buttons) >= MAX_PUBLISH_BUTTONS:
            return await query.edit_message_text(
                f"最多只能设置 {MAX_PUBLISH_BUTTONS} 个投稿按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        context.user_data["waiting_post"] = False
        context.user_data["publish_button_input"] = {"mode": "add", "step": "text"}
        return await query.edit_message_text(
            "请输入按钮文字（最多 64 个字符）。\n例如：加入交流群"
        )

    if action == "button_edit":
        buttons = _publish_buttons(config)
        if not buttons:
            return await query.edit_message_text(
                "当前未设置按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        return await query.edit_message_text(
            "请选择要修改的按钮：",
            reply_markup=_button_selection_keyboard(config, "edit"),
        )

    if action.startswith("button_edit_"):
        try:
            button_id = int(action.removeprefix("button_edit_"))
        except ValueError:
            return await query.answer("按钮数据无效", show_alert=True)
        if not any(button.get("id") == button_id for button in _publish_buttons(config)):
            return await query.answer("按钮不存在或已删除", show_alert=True)
        context.user_data["waiting_post"] = False
        context.user_data["publish_button_input"] = {
            "mode": "edit",
            "step": "text",
            "button_id": button_id,
        }
        return await query.edit_message_text(
            "请输入新的按钮文字（最多 64 个字符）。"
        )

    if action == "button_delete":
        buttons = _publish_buttons(config)
        if not buttons:
            return await query.edit_message_text(
                "当前未设置按钮。",
                reply_markup=publish_button_settings_keyboard(config),
            )
        return await query.edit_message_text(
            "请选择要删除的按钮：",
            reply_markup=_button_selection_keyboard(config, "delete"),
        )

    if action.startswith("button_delete_"):
        try:
            button_id = int(action.removeprefix("button_delete_"))
        except ValueError:
            return await query.answer("按钮数据无效", show_alert=True)
        buttons = _publish_buttons(config)
        updated_buttons = [button for button in buttons if button.get("id") != button_id]
        if len(updated_buttons) == len(buttons):
            return await query.answer("按钮不存在或已删除", show_alert=True)
        config["buttons"] = updated_buttons
        save_publish_config(config)
        return await query.edit_message_text(
            "✅ 按钮已删除。\n\n" + _button_settings_text(config),
            reply_markup=publish_button_settings_keyboard(config),
        )

    if action == "back":
        return await query.edit_message_text(
            "⚙️ 发布设置",
            reply_markup=publish_setting_keyboard(config)
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
            await query.message.reply_text("没有更多资源了")
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
        
        
        help_text = "📣 请发送您要的内容。\n\n支持文字、图片、视频等消息。 点击返回停止发送"
    
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
            await query.message.reply_text("请先请求一个资源")
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
                await query.message.reply_text("没有更多资源了")
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
                "✅ 继续发",
                callback_data="publish:publish",
            ),
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="start:back",
            ),
        ]
    ]
    return InlineKeyboardMarkup(rows)


async def handle_wall_publish(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_post"):
        return

    msg = update.message
    if not msg or not msg.from_user:
        return

    config = load_publish_config()
    channel_id = config.get("channel_id")
    if not channel_id:
        await msg.reply_text("✅ 暂未配置发布频道，感谢参与！")
        return

    owner_id = _owner_id(context)
    # 机器人所有者自己投稿时直接发布，无需再转发给自己审核。
    if bool(config.get("review_enabled", False)) and msg.from_user.id != owner_id:
        if owner_id is None:
            await msg.reply_text("❌ 未配置机器人所有者，暂时无法提交审核。")
            return

        submission_id = uuid.uuid4().hex[:16]
        pending = _load_pending_submissions()
        pending[submission_id] = {
            "status": "pending",
            "user_id": msg.from_user.id,
            "user_chat_id": msg.chat_id,
            "username": msg.from_user.username,
            "user_message_id": msg.message_id,
            "submitted_at": int(time.time()),
        }
        _save_pending_submissions(pending)

        try:
            # Use forward_message as requested so the owner receives the original submission.
            await context.bot.forward_message(
                chat_id=owner_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
            await context.bot.send_message(
                chat_id=owner_id,
                text=(
                    "📝 收到新的投稿，请审核。\n"
                    f"投稿人：{_submission_author_text(msg)}\n"
                    f"投稿编号：{submission_id}"
                ),
                reply_markup=_review_keyboard(submission_id),
            )
        except Exception as exc:
            pending.pop(submission_id, None)
            _save_pending_submissions(pending)
            print("转发投稿审核失败:", exc)
            await msg.reply_text(f"❌ 提交审核失败：{exc}")
            return

        await msg.reply_text("🕒 投稿已提交，等待管理员审核。")
        return

    try:
        published = await context.bot.copy_message(
            chat_id=channel_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=publish_buttons_keyboard(config),
        )
        _append_published_submission(msg, published.message_id)
        await msg.reply_text("✅ 发送成功")
    except Exception as exc:
        print("投稿失败:", exc)
        await msg.reply_text(f"❌ 发送失败：{exc}")
   
# =========================
# 文本输入处理
# =========================

async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await handle_wall_publish(update, context)

    if not update.message.text:
        return
    
    config = load_publish_config()

    button_input = context.user_data.get("publish_button_input")
    if isinstance(button_input, dict):
        value = update.message.text.strip()
        if button_input.get("step") == "text":
            if not value:
                return await update.message.reply_text("❗ 按钮文字不能为空，请重新输入。")
            if len(value) > BUTTON_TEXT_MAX_LENGTH:
                return await update.message.reply_text(
                    f"❗ 按钮文字不能超过 {BUTTON_TEXT_MAX_LENGTH} 个字符，请重新输入。"
                )
            button_input["text"] = value
            button_input["step"] = "url"
            return await update.message.reply_text(
                "请输入按钮链接。\n支持 https://、http://、tg:// 或 t.me/ 开头的链接。"
            )

        if button_input.get("step") == "url":
            url = _normalize_button_url(value)
            if not url:
                return await update.message.reply_text(
                    "❗ 链接格式不正确，请输入完整链接，例如：https://t.me/example"
                )

            buttons = _publish_buttons(config)
            if button_input.get("mode") == "edit":
                button_id = button_input.get("button_id")
                for button in buttons:
                    if button.get("id") == button_id:
                        button["text"] = button_input["text"]
                        button["url"] = url
                        break
                else:
                    _clear_button_input(context)
                    return await update.message.reply_text("❗ 按钮不存在或已删除。")
                result_text = "✅ 按钮修改成功。"
            else:
                if len(buttons) >= MAX_PUBLISH_BUTTONS:
                    _clear_button_input(context)
                    return await update.message.reply_text(
                        f"❗ 最多只能设置 {MAX_PUBLISH_BUTTONS} 个投稿按钮。"
                    )
                button_id = max(
                    (int(button.get("id", 0) or 0) for button in buttons), default=0
                ) + 1
                buttons.append({"id": button_id, "text": button_input["text"], "url": url})
                config["buttons"] = buttons
                result_text = "✅ 按钮添加成功。"

            save_publish_config(config)
            _clear_button_input(context)
            return await update.message.reply_text(
                result_text + "\n\n" + _button_settings_text(config),
                reply_markup=publish_button_settings_keyboard(config),
            )

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
            # InlineKeyboardButton(
            #     "👤 添加好友",
            #     callback_data=f"publish:add_friend:{post['user_id']}"
            # ),
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
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND) & ~filters.UpdateType.BUSINESS_MESSAGE ,_handle_text_input), group=10)