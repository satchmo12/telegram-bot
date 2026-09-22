from telegram import Update
from telegram.ext import ChatMemberHandler, CommandHandler, MessageHandler, ContextTypes, filters
from html import escape
from html import escape as html_escape
import secrets
import string
from command_router import register_command
from group.grouplist import load_users
from group.points_rules import award_invite_points
from utils import get_group_whitelist, load_json, save_json, safe_reply

INVITE_STATS_FILE = "data/invite_stats.json"
INVITE_LINK_MAP_FILE = "data/invite_link_map.json"


def load_invite_stats() -> dict:
    data = load_json(INVITE_STATS_FILE)
    if not isinstance(data, dict):
        return {}
    for group_data in data.values():
        for user_data in group_data.values():
            user_data["invitees"] = set(user_data.get("invitees", []))
    return data


def save_invite_stats(data: dict):
    data_copy = {
        gid: {
            uid: {
                "username": info.get("username", "未知用户"),
                "count": int(info.get("count", 0)),
                "invitees": list(info.get("invitees", set())),
            }
            for uid, info in users.items()
        }
        for gid, users in data.items()
    }
    save_json(INVITE_STATS_FILE, data_copy)


def load_invite_link_map() -> dict:
    data = load_json(INVITE_LINK_MAP_FILE)
    return data if isinstance(data, dict) else {}


def save_invite_link_map(data: dict):
    save_json(INVITE_LINK_MAP_FILE, data)


def get_user_invite_count(stats_data: dict, chat_id: int, user_id: int) -> int:
    group_stats = stats_data.get(str(chat_id), {})
    user_stats = group_stats.get(str(user_id), {})
    try:
        return int(user_stats.get("count", 0))
    except Exception:
        return 0


def format_personal_link_text(display_name: str, link: str, total_count: int) -> str:
    safe_name = escape(display_name or "用户")
    safe_link = escape(link or "")
    return (
        f"🔗 {safe_name} 您的专属链接:\n"
        f"<code>{safe_link}</code>\n"
        "(点击复制)\n\n"
        f"👉 当前总共邀请 {int(total_count)} 人"
    )


def update_invite_stats_by_user(
    stats_data: dict,
    chat_id: int,
    inviter_id: int,
    inviter_name: str,
    new_member_ids: list[int],
)-> list[int]:
    group_id = str(chat_id)
    inviter_id_str = str(inviter_id)

    group_stats = stats_data.setdefault(group_id, {})
    stat = group_stats.setdefault(
        inviter_id_str,
        {"username": inviter_name or "未知用户", "count": 0, "invitees": set()},
    )

    stat["username"] = inviter_name or stat.get("username", "未知用户")
    if not isinstance(stat.get("invitees"), set):
        stat["invitees"] = set(stat.get("invitees", []) or [])

    # A person can generate an invite reward only once per group, even if they
    # leave and later rejoin through a different member's link.  Historical
    # invitees are retained in the group-wide stats specifically for this
    # anti-reward-farming check.
    previously_invited = set()
    for recorded_stat in group_stats.values():
        if not isinstance(recorded_stat, dict):
            continue
        invitees = recorded_stat.get("invitees", set())
        if not isinstance(invitees, (set, list, tuple)):
            continue
        for recorded_uid in invitees:
            try:
                previously_invited.add(str(int(recorded_uid)))
            except (TypeError, ValueError):
                continue

    added_invitees = []
    for uid in new_member_ids:
        try:
            invitee_key = str(int(uid))
        except (TypeError, ValueError):
            continue
        if invitee_key in previously_invited:
            print(
                f"[邀请积分] 跳过重复入群用户 chat={chat_id} "
                f"inviter={inviter_id} invitee={invitee_key}"
            )
            continue
        stat["invitees"].add(int(uid))
        stat["count"] += 1
        added_invitees.append(int(uid))
        previously_invited.add(invitee_key)

    save_invite_stats(stats_data)
    return added_invitees


@register_command("邀请链接")
# async def create_personal_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     if not update.message or not update.effective_chat or not update.effective_user:
#         return

#     chat = update.effective_chat
#     if chat.type not in ("group", "supergroup"):
#         return await safe_reply(update, context, "⚠️ 该命令只能在群里使用。")

#     user = update.effective_user
#     chat_key = str(chat.id)
#     stats_data = load_invite_stats()
#     invited_count = get_user_invite_count(stats_data, chat.id, user.id)
#     link_map_data = load_invite_link_map()
#     group_link_map = link_map_data.setdefault(chat_key, {})

#     # 同群同用户复用已有链接，避免每次生成新链接
#     existing_link = None
#     existing_created_at = -1
#     for link, info in group_link_map.items():
#         if int(info.get("inviter_id", 0)) != int(user.id):
#             continue
#         ts = int(info.get("created_at", 0))
#         if ts >= existing_created_at:
#             existing_created_at = ts
#             existing_link = link

#     if existing_link:
#         msg = format_personal_link_text(user.full_name, existing_link, invited_count)
#         return await safe_reply(update, context, msg, html=True)

#     # 机器人无创建邀请链接权限时，静默跳过（多机器人同群场景）
#     try:
#         bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
#         can_invite = bool(getattr(bot_member, "can_invite_users", False))
#         if not can_invite:
#             return
#     except Exception:
#         return

#     try:
#         link_obj = await context.bot.create_chat_invite_link(
#             chat_id=chat.id,
#             name=f"inviter:{user.id}",
#         )
#     except Exception as e:
#         return await safe_reply(update, context, f"❌ 生成链接失败：{e}")

#     group_link_map[link_obj.invite_link] = {
#         "inviter_id": user.id,
#         "inviter_name": user.full_name,
#         "created_at": int(update.message.date.timestamp()) if update.message.date else 0,
#     }
#     save_invite_link_map(link_map_data)

#     msg = format_personal_link_text(user.full_name, link_obj.invite_link, invited_count)
#     await safe_reply(update, context, msg, html=True)



@register_command("邀请链接", "邀请")
async def create_personal_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return await safe_reply(update, context, "⚠️ 该命令只能在群里使用。")

    user = update.effective_user
    chat_key = str(chat.id)

    stats_data = load_invite_stats()
    invited_count = get_user_invite_count(
        stats_data,
        chat.id,
        user.id,
    )

    link_map_data = load_invite_link_map()
    group_link_map = link_map_data.setdefault(chat_key, {})

    # 同群同用户复用已有链接
    existing_link = None
    existing_created_at = -1
    invite_code = None

    for link, info in group_link_map.items():
        if int(info.get("inviter_id", 0)) != int(user.id):
            continue

        ts = int(info.get("created_at", 0))

        if ts >= existing_created_at:
            existing_created_at = ts
            existing_link = link
            invite_code = info.get("invite_code")

    # 已经存在群邀请链接
    if existing_link:
        # 老数据没有短码，则补一个
        if not invite_code:
            invite_code = generate_invite_code()

            group_link_map[existing_link]["invite_code"] = invite_code
            save_invite_link_map(link_map_data)

    else:
        # 机器人无创建邀请链接权限时，静默跳过
        try:
            bot_member = await context.bot.get_chat_member(
                chat.id,
                context.bot.id,
            )

            can_invite = bool(
                getattr(bot_member, "can_invite_users", False)
            )

            if not can_invite:
                return

        except Exception:
            return

        # 创建真实群邀请链接
        try:
            link_obj = await context.bot.create_chat_invite_link(
                chat_id=chat.id,
                name=f"inviter:{user.id}",
            )
        except Exception as e:
            return await safe_reply(
                update,
                context,
                f"❌ 生成链接失败：{e}",
            )

        existing_link = link_obj.invite_link
        invite_code = generate_invite_code()

        group_link_map[existing_link] = {
            "inviter_id": user.id,
            "inviter_name": user.full_name,
            "inviter_username": user.username,
            "created_at": (
                int(update.message.date.timestamp())
                if update.message.date else 0
            ),
            "invite_code": invite_code,
        }

        save_invite_link_map(link_map_data)

    # 获取当前机器人用户名
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
    except Exception:
        return

    # 最终给用户的是机器人短链接
    bot_link = f"https://t.me/{bot_username}?start=invite_{invite_code}"

    msg = format_personal_bot_link_text(
        user.full_name,
        bot_link,
        invited_count,
    )

    await safe_reply(
        update,
        context,
        msg,
        html=True,
    )

def format_personal_bot_link_text(
    display_name: str,
    link: str,
    total_count: int,
) -> str:
    safe_name = escape(display_name or "用户")
    safe_link = escape(link or "")
    return (
        f"🔗 {safe_name} 您的专属邀请链接:\n"
        f"<code>{safe_link}</code>\n"
        "(点击复制)\n\n"
        f"👉 当前总共邀请 {int(total_count)} 人"
    )
    
# def format_personal_bot_link_text(
#     user_name: str,
#     bot_link: str,
#     invited_count: int,
# ) -> str:
#     return (
#         f"👤 <b>{html.escape(user_name)}</b>\n\n"
#         f"🔗 你的专属邀请链接：\n"
#         f'<a href="{html.escape(bot_link, quote=True)}">{html.escape(bot_link)}</a>\n\n'
#         f"👥 已邀请：{invited_count} 人"
    # )

def generate_invite_code(length: int = 6) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _user_mention_html(user_id: int, display_name: str) -> str:
    """Build a Telegram HTML mention that displays a safe nickname."""
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return html_escape(str(display_name or "用户"))
    name = html_escape(str(display_name or "用户"))
    return f'<a href="tg://user?id={user_id}">{name}</a>'


async def _credit_invite_join(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    new_member_ids: list[int],
    used_invite_link: str,
    member_usernames=None,
    member_names=None,
) -> list[int]:
    """Record and award one or more joins attributable to a tracked link.

    Both service messages and chat-member updates can report the same join.
    ``update_invite_stats_by_user`` de-duplicates invitees, so only the first
    event can generate points.
    """
    if not used_invite_link or not new_member_ids:
        return []
    link_map_data = load_invite_link_map()
    owner_info = (link_map_data.get(str(chat_id), {}) or {}).get(used_invite_link)
    if not isinstance(owner_info, dict):
        print(f"[邀请积分] 未找到邀请链接映射 chat={chat_id} link={used_invite_link[:48]}")
        return []
    inviter_id = int(owner_info.get("inviter_id", 0) or 0)
    inviter_name = str(owner_info.get("inviter_name") or "未知用户")
    if inviter_id <= 0:
        print(f"[邀请积分] 邀请链接缺少邀请人 chat={chat_id} link={used_invite_link[:48]}")
        return []

    stats_data = load_invite_stats()
    added_invitees = update_invite_stats_by_user(
        stats_data, chat_id, inviter_id, inviter_name, new_member_ids
    )
    if not added_invitees:
        return []
    total_invite_count = get_user_invite_count(stats_data, chat_id, inviter_id)

    # The invite handler (group=10) runs before the group-user tracker
    # (group=12).  For a just-joined member, load_users() can therefore still
    # be empty even though Telegram supplied a username in this update. Prefer
    # the username carried by the join update and use persisted data only as a
    # fallback for older callers.
    current_usernames = member_usernames if isinstance(member_usernames, dict) else {}
    current_names = member_names if isinstance(member_names, dict) else {}
    users = load_users(chat_id)
    valid_invitees = []
    invitee_usernames = {}
    invitee_names = {}

    for user_id in added_invitees:
        info = users.get(str(user_id), {})
        if not isinstance(info, dict):
            info = {}
        username = current_usernames.get(user_id) or info.get("username")
        username = str(username or "").strip().lstrip("@")
        display_name = current_names.get(user_id) or info.get("full_name") or f"用户{user_id}"

        # 没有 username，不算有效邀请
        if username:
            valid_invitees.append(user_id)
            invitee_usernames[user_id] = username
            invitee_names[user_id] = str(display_name).strip() or f"用户{user_id}"
            
    try:
        group_cfg = get_group_whitelist(context).get(str(chat_id), {})
        points_enabled = bool((group_cfg or {}).get("invite_points_enabled", False))

        # Only invitees with a recorded username are eligible for invitation
        # points.  ``added_invitees`` is a list, so it must never be used as a
        # dictionary key (the previous expression caused "unhashable type:
        # 'list'").
        if not points_enabled:
            awarded = 0
            no_award_reason = "本群未开启邀请积分"
        elif not valid_invitees:
            awarded = 0
            no_award_reason = "被邀请人没有可用用户名，不参与邀请积分"
        else:
            awarded = award_invite_points(
                str(chat_id), inviter_id, valid_invitees, group_cfg
            )
            no_award_reason = "已达到每日上限或本次可发积分为 0"

        if not points_enabled:
            print(
                f"[邀请积分] 已记录邀请但本群未开启邀请积分 "
                f"chat={chat_id} inviter={inviter_id}"
            )
        elif awarded <= 0:
            print(
                f"[邀请积分] 已记录邀请但未发分（{no_award_reason}） "
                f"chat={chat_id} inviter={inviter_id} "
                f"amount={group_cfg.get('invite_points_amount')} "
                f"daily_limit={group_cfg.get('invite_points_daily_limit')}"
            )

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎉 邀请成功！\n"
                        f"👤 邀请人：{_user_mention_html(inviter_id, inviter_name)}\n"
                        f"👥 成功邀请：{len(added_invitees)} 人\n"
                        f"📊 总邀请人数：{total_invite_count} 人\n"
                        f"🎁 获得积分：+{awarded}"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as notify_exc:
                print(
                    f"⚠️ 邀请积分群内提醒发送失败: "
                    f"chat={chat_id}, inviter={inviter_id}, {notify_exc}"
                )

        else:
            print(
                f"[邀请积分] 已发放 chat={chat_id} inviter={inviter_id} "
                f"invitees={len(added_invitees)} points={awarded}"
            )
            
             # 在群里提醒邀请成功
            try:
                
                invitee_text = "、".join(
                    _user_mention_html(
                        user_id,
                        invitee_names.get(user_id) or invitee_usernames.get(user_id),
                    )
                    for user_id in valid_invitees
                )

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎉 邀请成功！\n"
                        f"👤 邀请人：{_user_mention_html(inviter_id, inviter_name)}\n"
                        f"👥 成功邀请：{len(added_invitees)} 人\n"
                        f"🙋 被邀请人：{invitee_text}\n"
                        f"📊 总邀请人数：{total_invite_count} 人\n"
                        f"🎁 获得积分：+{awarded}"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as notify_exc:
                print(
                    f"⚠️ 邀请积分群内提醒发送失败: "
                    f"chat={chat_id}, inviter={inviter_id}, {notify_exc}"
                )
        
    except Exception as exc:
        print(f"⚠️ 邀请积分发放失败: chat={chat_id} inviter={inviter_id}, {exc}")
    return added_invitees


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    new_members = [member for member in update.message.new_chat_members if member]
    new_member_ids = [member.id for member in new_members]
    if not new_member_ids:
        return
    member_usernames = {
        int(member.id): member.username
        for member in new_members
        if getattr(member, "username", None)
    }
    member_names = {
        int(member.id): member.full_name
        for member in new_members
    }

    invite_link_obj = getattr(update.message, "invite_link", None)
    used_invite_link = getattr(invite_link_obj, "invite_link", None) if invite_link_obj else None
    if used_invite_link:
        await _credit_invite_join(
            context,
            chat_id,
            new_member_ids,
            used_invite_link,
            member_usernames,
            member_names,
        )
        return

    # Retain the legacy fallback for ordinary manual additions. It has no
    # invite-link attribution, but preserves existing invitation statistics.
    inviter = update.message.from_user
    if inviter and any(uid != inviter.id for uid in new_member_ids):
        added_invitees = update_invite_stats_by_user(
            load_invite_stats(), chat_id, inviter.id, inviter.full_name, new_member_ids
        )
        try:
            group_cfg = get_group_whitelist(context).get(str(chat_id), {})
            award_invite_points(str(chat_id), inviter.id, added_invitees, group_cfg)
        except Exception as exc:
            print(f"⚠️ 邀请积分发放失败: chat={chat_id} inviter={inviter.id}, {exc}")


async def handle_chat_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture joins where Telegram omits the new_chat_members service message.

    Telegram attaches the exact invite link to ChatMemberUpdated.invite_link.
    This makes invitation attribution reliable for users who enter via a link.
    """
    change = getattr(update, "chat_member", None)
    if not change or not getattr(change, "chat", None) or not getattr(change, "new_chat_member", None):
        return
    old_status = str(getattr(getattr(change, "old_chat_member", None), "status", ""))
    new_status = str(getattr(change.new_chat_member, "status", ""))
    if old_status not in {"left", "kicked"} or new_status not in {"member", "administrator", "restricted"}:
        return
    invite_link_obj = getattr(change, "invite_link", None)
    used_invite_link = getattr(invite_link_obj, "invite_link", None) if invite_link_obj else None
    user = getattr(change.new_chat_member, "user", None)
    if not used_invite_link or not user or getattr(user, "is_bot", False):
        return
    await _credit_invite_join(
        context,
        int(change.chat.id),
        [int(user.id)],
        used_invite_link,
        {int(user.id): user.username} if getattr(user, "username", None) else {},
        {int(user.id): user.full_name},
    )


@register_command("邀请统计")
async def show_invites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in ("administrator", "creator"):
        return await safe_reply(update, context, "🚫 你没有权限查看邀请统计。")

    stats_data = load_invite_stats()
    group_stats = stats_data.get(chat_id)
    if not group_stats:
        return await safe_reply(update, context, "暂无该群的邀请数据。")

    sorted_stats = sorted(
        group_stats.items(), key=lambda x: x[1].get("count", 0), reverse=True
    )

    msg_lines = ["📊 本群邀请排行榜："]
    for i, (_, info) in enumerate(sorted_stats[:10], start=1):
        msg_lines.append(f"{i}. {info['username']} — 邀请 {info['count']} 人")

    await safe_reply(update, context, "\n".join(msg_lines))


def register_invite_handlers(app):
    # PTB 在同一个 handler group 内只会执行第一个匹配的 handler。
    # 新成员事件还要交给欢迎词和用户记录处理，因此使用独立 group。
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member),
        group=10,
    )
    # Some joins arrive only as a chat_member update; that update contains the
    # invite link used by the joining user.
    app.add_handler(ChatMemberHandler(handle_chat_member_join, ChatMemberHandler.CHAT_MEMBER), group=10)
    app.add_handler(CommandHandler("invites", show_invites))
    app.add_handler(CommandHandler("link", create_personal_invite_link))
