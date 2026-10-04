
# invite_approval.py

from html import escape
import logging
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)

# 替换为你项目中实际存放 load_json / save_json 的模块

from utils import get_group_whitelist, load_json, save_json
from telegram import MessageEntity
logger = logging.getLogger(__name__)


# ============================================================
# 配置
# ============================================================

INVITE_APPROVAL_FILE = "data/invite_approval.json"


# ============================================================
# JSON 数据读写
# ============================================================

def _load_approval_data() -> dict:
    data = load_json(INVITE_APPROVAL_FILE)
    return data if isinstance(data, dict) else {}


def _save_approval_data(data: dict):
    save_json(INVITE_APPROVAL_FILE, data)

# 同步消息
async def _sync_review_messages(
    context: ContextTypes.DEFAULT_TYPE,
    request: dict,
    text: str,
):
    """同步更新所有管理员收到的审核消息"""

    review_messages = request.get("review_messages", [])

    for item in review_messages:
        admin_id = item.get("admin_id")
        message_id = item.get("message_id")

        if not admin_id or not message_id:
            continue

        try:
            await context.bot.edit_message_text(
                chat_id=admin_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )

        except TelegramError as e:
            logger.warning(
                "同步管理员审核消息失败 admin=%s message=%s: %s",
                admin_id,
                message_id,
                e,
            )

def _request_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def get_approval_request(chat_id: int, user_id: int):
    data = _load_approval_data()
    return data.get(_request_key(chat_id, user_id))


def update_approval_request(
    chat_id: int,
    user_id: int,
    values: dict,
):
    data = _load_approval_data()
    key = _request_key(chat_id, user_id)

    request = data.get(key, {})
    request.update(values)

    data[key] = request
    _save_approval_data(data)


# ============================================================
# 查找邀请链接信息
# ============================================================

def _find_invite_info(invite_code: str):
    """
    根据短码查找邀请链接。

    返回：
        chat_id
        invite_link
        invite_info
    """


    from group.invite_stats import load_invite_link_map
    link_map_data = load_invite_link_map()

    for chat_key, group_links in link_map_data.items():
        if not isinstance(group_links, dict):
            continue

        for invite_link, info in group_links.items():
            if not isinstance(info, dict):
                continue

            if str(info.get("invite_code", "")) == invite_code:
                try:
                    chat_id = int(chat_key)
                except (TypeError, ValueError):
                    continue

                return chat_id, invite_link, info

    return None, None, None


# ============================================================
# 申请入群
# ============================================================

async def handle_invite_apply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not query.data or not query.from_user:
        return

    if not query.data.startswith("invite_apply:"):
        return

    invite_code = query.data.split(":", 1)[1].strip()

    if not invite_code:
        await query.answer("申请信息无效", show_alert=True,)
        return

    user = query.from_user

    # 查找邀请链接
    chat_id, invite_link, invite_info = _find_invite_info(invite_code)

    if not invite_info or chat_id is None:
        await query.answer("邀请链接已失效", show_alert=True,)
        return

    inviter_id = int(invite_info.get("inviter_id", 0))

    # 不允许邀请人自己申请
    if inviter_id == user.id:
        await query.answer(
            "不能通过自己的邀请链接申请入群",
            show_alert=True,
        )
        return

    # 检查群审核开关
    

    group_config = get_group_whitelist(context).get(str(chat_id), {})

    if not group_config.get("invite_approval_enabled", False):
        await query.answer("该群未开启入群审核", show_alert=True,)
        return

    # 检查是否已经在群里
    try:
        member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=user.id,
        )

        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            await query.answer("你已经在群里了", show_alert=True,)
            return

    except TelegramError:
        logger.exception(
            "检查用户群成员状态失败 chat=%s user=%s",
            chat_id,
            user.id,
        )

    # 检查重复申请
    key = _request_key(chat_id, user.id)
    data = _load_approval_data()
    old_request = data.get(key)
    reject_count = int(old_request.get("reject_count", 0)) if old_request else 0
    if old_request:
        status = old_request.get("status")

        if status == "pending":
            await query.answer(
                "你的申请正在等待管理员审核",
                show_alert=True,
            )
            return

        if status == "approved":
            await query.answer(
                "你的申请已经通过，请查看机器人私聊",
                show_alert=True,
            )
            return

        # if status == "joined":
        #     await query.answer("你已经加入过该群", show_alert=True)
        #     return

        # if status == "rejected":
        #     await query.answer(
        #         "你的申请曾被拒绝，请联系群管理员",
        #         show_alert=True,
        #     )
        #     return
        # 拒绝达到 3 次，禁止继续申请
        if reject_count >= 3:
            await query.answer(
                "你的申请已被拒绝 3 次，请联系群管理员",
                show_alert=True,
            )
            return

    now = int(time.time())

    request = {
        "chat_id": chat_id,
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "inviter_id": inviter_id,
        "inviter_name": invite_info.get("inviter_name", ""),
        "inviter_username": invite_info.get("inviter_username", ""),
        "invite_code": invite_code,
        "invite_link": invite_link,
        "status": "pending",
        "created_at": now,
        "reviewed_at": 0,
        "reviewer_id": 0,
        "approved_invite_link": "",
        # 保留历史拒绝次数
        "reject_count": reject_count,
    }

    data[key] = request
    _save_approval_data(data)

    await query.answer("申请已提交" , show_alert=True,)

    # 获取管理员
    try:
        admins = await context.bot.get_chat_administrators(chat_id)

    except TelegramError:
        logger.exception("获取群管理员失败 chat=%s", chat_id)

        update_approval_request(
            chat_id,
            user.id,
            {"status": "notification_failed"},
        )

        await query.message.reply_text(
            "申请已记录，但暂时无法通知管理员，请稍后联系群管理员。"
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 通过",
                callback_data=(
                    f"invite_review:approve:{chat_id}:{user.id}"
                ),
            ),
            InlineKeyboardButton(
                "❌ 拒绝",
                callback_data=(
                    f"invite_review:reject:{chat_id}:{user.id}"
                ),
            ),
        ]
    ])

    if user.username:
        username_text = (
            f'<a href="https://t.me/{escape(user.username)}">'
            f'@{escape(user.username)}</a>'
        )
    else:
        username_text = (
            f'<a href="tg://user?id={user.id}">无用户名</a>'
        )
    
    # 获取群组名称
    chat_title = "未知群组"

    try:
        chat = await context.bot.get_chat(chat_id)
        chat_title = chat.title or "未知群组"
    except Exception as e:
        logger.warning("获取群组名称失败 chat_id=%s: %s", chat_id, e)
        
    # 申请人：蓝色可点击，点击后打开用户

    applicant_name = escape(user.full_name or str(user.id))
    applicant_link = (
        f'<a href="tg://user?id={user.id}">{applicant_name}</a>'
    )
    inviter_id = request["inviter_id"]
    inviter_name = escape(request.get("inviter_name") or str(inviter_id))
    inviter_link = (
        f'<a href="tg://user?id={inviter_id}">{inviter_name}</a>'
    )
    message = (
        "📥 <b>新的入群申请</b>\n\n"
        f"申请人：{applicant_link}\n"
        f"用户名：{username_text}\n"
        f"用户 ID：<code>{user.id}</code>\n"
        f"邀请人：{inviter_link}\n"
        f"邀请人 ID：<code>{inviter_id}</code>\n"
        f"群组名称：{escape(chat_title)}\n"
        f"群组 ID：<code>{chat_id}</code>\n\n"
        "请审核是否允许该用户加入群组。"
    )
    
    success_count = 0
    review_messages = []

    for admin in admins:
        if admin.status not in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            continue

        try:
            sent_message = await context.bot.send_message(
                chat_id=admin.user.id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            success_count += 1

            # 记录管理员收到的消息
            review_messages.append({
                "admin_id": admin.user.id,
                "message_id": sent_message.message_id,
            })

        except TelegramError:
            logger.info(
                "无法私聊管理员 admin=%s chat=%s",
                admin.user.id,
                chat_id,
            )

    # 保存所有管理员的审核消息 ID
    update_approval_request(
        chat_id,
        user.id,
        {
            "review_messages": review_messages,
        },
    )


# ============================================================
# 管理员审核
# ============================================================

async def handle_invite_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not query.data or not query.from_user:
        return

    parts = query.data.split(":")

    if len(parts) != 4 or parts[0] != "invite_review":
        return

    action = parts[1]

    try:
        chat_id = int(parts[2])
        user_id = int(parts[3])
    except ValueError:
        await query.answer("审核参数无效", show_alert=True)
        return

    reviewer = query.from_user

    # 验证管理员身份
    try:
        member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=reviewer.id,
        )

    except TelegramError:
        await query.answer(
            "无法验证管理员身份",
            show_alert=True,
        )
        return

    if member.status not in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        await query.answer(
            "只有群管理员才能审核",
            show_alert=True,
        )
        return

    request = get_approval_request(chat_id, user_id)

    if not request:
        await query.answer("申请记录不存在", show_alert=True)
        return

    if request.get("status") != "pending":
        await query.answer(
            "该申请已经处理过",
            show_alert=True,
        )
        return
    
    
    # 申请人
    applicant_name = escape(
        request.get("full_name")
        or str(user_id)
    )
    applicant_link = (
        f'<a href="tg://user?id={user_id}">{applicant_name}</a>'
    )

    # 审核人
    reviewer_name = escape(
        reviewer.full_name or str(reviewer.id)
    )
    reviewer_link = (
        f'<a href="tg://user?id={reviewer.id}">{reviewer_name}</a>'
    )

    # 邀请人
    inviter_id = request.get("inviter_id")
    inviter_name = escape(
        request.get("inviter_name") or str(inviter_id or "未知")
    )

    if inviter_id:
        inviter_link = (
            f'<a href="tg://user?id={inviter_id}">{inviter_name}</a>'
        )
    else:
        inviter_link = inviter_name

    # --------------------------------------------------------
    # 拒绝申请
    # --------------------------------------------------------

    if action == "reject":

    # 累加拒绝次数
        reject_count = int(request.get("reject_count", 0)) + 1

        update_approval_request(
            chat_id,
            user_id,
            {
                "status": "rejected",
                "reject_count": reject_count,
                "reviewed_at": int(time.time()),
                "reviewer_id": reviewer.id,
            },
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "很抱歉，你的入群申请未通过。\n"
                    f"累计拒绝次数：{reject_count} 次。\n"
                    + (
                        "你的申请已被拒绝 3 次，请联系群管理员。"
                        if reject_count >= 3
                        else "你可以重新提交申请。"
                    )
                ),
            )

        except TelegramError:
            logger.info(
                "无法通知申请人拒绝结果 user=%s",
                user_id,
            )

        await query.answer("已拒绝申请", show_alert=True )
        
        
        
         # 重新读取申请记录，获取所有管理员的审核消息
        request = get_approval_request(chat_id, user_id)
        
        # 审核结果
        result_text = (
            "❌ <b>入群申请已拒绝</b>\n\n"
            f"申请人：{applicant_link}\n"
            f"申请人 ID：<code>{user_id}</code>\n\n"
            f"累计拒绝次数：{reject_count} 次\n"
            f"审核人：{reviewer_link}\n"
            f"审核人 ID：<code>{reviewer.id}</code>\n\n"
            f"邀请人：{inviter_link}\n"
            f"邀请人 ID：<code>{inviter_id or '未知'}</code>"
        )

        if request:
            await _sync_review_messages(
                context,
                request,
                result_text,
            )

        return

    # --------------------------------------------------------
    # 通过申请
    # --------------------------------------------------------

    if action != "approve":
        await query.answer("未知审核操作", show_alert=True,)
        return

    # 检查用户是否已经加入
    try:
        current_member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        if current_member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            update_approval_request(
                chat_id,
                user_id,
                {
                    "status": "joined",
                    "reviewed_at": int(time.time()),
                    "reviewer_id": reviewer.id,
                },
            )

            await query.answer(
                "该用户已经在群里",
                show_alert=True,
            )

            await query.edit_message_text(
                f"该用户已经加入群组。\n用户 ID：{user_id}"
            )
            return

    except TelegramError:
        logger.exception(
            "检查申请人状态失败 chat=%s user=%s",
            chat_id,
            user_id,
        )

    # 创建仅限一人使用的入群链接
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            name=f"approved:{user_id}",
            member_limit=1,
        )

    except TelegramError:
        logger.exception(
            "创建审核通过链接失败 chat=%s user=%s",
            chat_id,
            user_id,
        )

        await query.answer(
            "创建入群链接失败，请检查机器人权限",
            show_alert=True,
        )
        return

    # 保存审核状态
    update_approval_request(
        chat_id,
        user_id,
        {
            "status": "approved",
            "reviewed_at": int(time.time()),
            "reviewer_id": reviewer.id,
            "approved_invite_link": invite.invite_link,
        },
    )

    # 私聊申请人
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 你的入群申请已通过！\n\n"
                "请点击下方链接加入群组：\n"
                f"{invite.invite_link}\n\n"
                "该链接仅限一人使用，请勿转发。"
            ),
        )

    except TelegramError:
        logger.exception(
            "审核通过后无法私聊申请人 user=%s",
            user_id,
        )

        await query.answer(
            "已通过，但无法私聊申请人",
            show_alert=True,
        )

        await query.edit_message_text(
            f"✅ 申请已通过，但无法发送入群链接。\n"
            f"申请人 ID：{user_id}\n\n"
            f"入群链接：{invite.invite_link}"
        )
        return

    await query.answer("审核通过")
    
    # 审核结果
    result_text = (
        "✅ <b>入群申请已通过</b>\n\n"
        f"申请人：{applicant_link}\n"
        f"申请人 ID：<code>{user_id}</code>\n\n"
        f"审核人：{reviewer_link}\n"
        f"审核人 ID：<code>{reviewer.id}</code>\n\n"
        f"邀请人：{inviter_link}\n"
        f"邀请人 ID：<code>{inviter_id or '未知'}</code>"
    )

    # 同步更新所有管理员收到的消息
    request = get_approval_request(chat_id, user_id)

    if request:
        await _sync_review_messages(
            context,
            request,
            result_text,
        )

    # 更新当前管理员的消息
    try:
        await query.edit_message_text(result_text, parse_mode="HTML",
                        reply_markup=None,)
    except TelegramError:
        pass


# ============================================================
# 注册处理器
# ============================================================

def register_invite_approval_handlers(app):

    app.add_handler(
        CallbackQueryHandler(
            handle_invite_apply,
            pattern=r"^invite_apply:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_invite_review,
            pattern=r"^invite_review:",
        )
    )