# marry_system.py
import json
import os
from datetime import datetime
from html import escape
import random
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import mention_html

from command_router import FEATURE_FRIENDS, feature_required, register_command
from info.economy import INFO_FILE, ensure_user_exists
from slave.status_warnings import (
    LOVER_MARRIED_WARNINGS,
    LOVER_SPONSORED_WARNINGS,
    OWNER_MARRIED_WARNINGS,
    OWNER_SPONSORED_WARNINGS,
)
from utils import (
    MARRY_FILE,
    get_bot_path,
    get_group_whitelist,
    group_allowed,
    load_json,
    safe_reply,
    save_json,
)

KISS_TEXTS = [
    "😘 轻轻地亲了亲",
    "💋 偷偷亲了一下",
    "😚 在脸颊上亲了一口",
]

HUG_TEXTS = [
    "🤗 紧紧抱住了",
    "🫂 给了一个温暖的拥抱",
    "🥰 把对方拥入怀中",
]

ACTION_TEXTS = {
    "亲亲": [
        "😘 轻轻地亲了亲",
        "💋 偷偷亲了一下",
        "😚 在脸颊上亲了一口",
    ],
    "抱抱": [
        "🤗 紧紧抱住了",
        "🫂 给了一个温暖的拥抱",
        "🥰 把对方拥入怀中",
    ],
    "举高高": [
        "🙌 把对方举高高转了一圈",
        "😄 笑着把对方举了起来",
        "🥰 轻轻地把对方举高高",
    ],
    "摸头": [
        "🫳 温柔地摸了摸头",
        "😊 轻轻揉了揉脑袋",
        "🐾 摸摸头以示安慰",
    ],
    "撒娇": [
        "🥺 对着对方撒娇",
        "😖 小声地撒起了娇",
        "💗 拉着对方衣角撒娇",
    ],
}

BABY_PROBABILITY = 0.15  # 15% 概率
DONGFANG_COOLDOWN = 3600  # 1 小时
INTIMACY_ACTION_POINTS = {
    "亲亲": 2,
    "抱抱": 2,
    "举高高": 3,
    "摸头": 2,
    "撒娇": 2,
}
INTIMACY_ACTION_COOLDOWN = 300  # 5 分钟
BABY_FEED_COOLDOWN = 3600  # 1 小时
BABY_STARVE_SECONDS = 86400  # 24 小时
DONGFANG_INTIMACY_POINTS = 5
BABY_BIRTH_INTIMACY_POINTS = 6
BABY_DEATH_INTIMACY_PENALTY = 50

dongfang_cooldown = {}  # (chat_id, uid) -> timestamp


def _is_chat_silent(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> bool:
    group_cfg = get_group_whitelist(context).get(str(chat_id), {})
    return bool(group_cfg.get("silent", False))


def _mention_or_name(user_id: str, name: str, is_silent: bool) -> str:
    if is_silent:
        return escape(name or "用户")
    return mention_html(user_id, name or "用户")


def _user_ref(user, is_silent: bool) -> str:
    name = user.full_name or user.first_name or "用户"
    if is_silent:
        return escape(name)
    return user.mention_html()


def _load_marry_data(context: ContextTypes.DEFAULT_TYPE):
    return load_json(get_bot_path(context, MARRY_FILE))


def _save_marry_data(context: ContextTypes.DEFAULT_TYPE, data: dict):
    save_json(get_bot_path(context, MARRY_FILE), data)


def is_lover(chat_id: str, uid: str, target_id: str) -> bool:
    data = load_json(MARRY_FILE)
    return data.get(chat_id, {}).get(uid, {}).get("lover_id") == target_id


def _new_baby_id() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"bb{ts}{random.randint(1000, 9999)}"


def _ensure_child_id(child: dict) -> str:
    cid = str(child.get("id", "")).strip() if isinstance(child, dict) else ""
    if cid:
        return cid
    cid = _new_baby_id()
    if isinstance(child, dict):
        child["id"] = cid
    return cid


def _get_child_by_selector(children: list, selector: str):
    if selector.isdigit():
        idx = int(selector)
        if idx < 1 or idx > len(children):
            return None, f"编号超出范围：1~{len(children)}"
        return children[idx - 1], None
    matched = [c for c in children if str(c.get("name", "")) == selector]
    if not matched:
        return None, f"没找到名字为「{selector}」的宝宝。"
    if len(matched) > 1:
        return None, "有多个同名宝宝，请用编号操作（先发送“宝宝”查看编号）。"
    return matched[0], None


def _format_since(ts: int) -> str:
    if not ts:
        return "未喂养"
    delta = int(datetime.now().timestamp() - ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


def _baby_growth_stage(feed_count: int) -> str:
    if feed_count >= 10:
        return "少年"
    if feed_count >= 6:
        return "幼童"
    if feed_count >= 3:
        return "学步"
    if feed_count >= 1:
        return "新生"
    return "初生"


def _parse_birthday_ts(birthday: str) -> int:
    if not birthday:
        return 0
    try:
        dt = datetime.strptime(birthday, "%Y-%m-%d")
        return int(dt.timestamp())
    except Exception:
        return 0


def _get_born_ts(child: dict) -> int:
    born_ts = int(child.get("born_ts", 0) or 0)
    if born_ts:
        return born_ts
    return _parse_birthday_ts(str(child.get("birthday", "")))


def _find_child_by_id(children: list, child_id: str):
    for c in children:
        if _ensure_child_id(c) == child_id:
            return c
    return None


def _sync_child_fields(group: dict, lover_id: str, child_id: str, fields: dict):
    if not lover_id:
        return
    partner_info = group.setdefault(str(lover_id), {})
    partner_children = partner_info.setdefault("children", [])
    partner_child = _find_child_by_id(partner_children, child_id)
    if partner_child:
        partner_child.update(fields)


def _apply_intimacy(group: dict, uid: str, lover_id: str, delta: int):
    for pid in (uid, lover_id):
        if not pid:
            continue
        pdata = group.setdefault(str(pid), {})
        pdata["intimacy"] = int(pdata.get("intimacy", 0) or 0) + delta


def _maybe_handle_starve(
    group: dict, uid: str, lover_id: str, child: dict, now_ts: int
):
    if child.get("dead"):
        return False
    last_fed = int(child.get("last_fed", 0) or 0)
    born_ts = _get_born_ts(child)
    base_ts = last_fed or born_ts
    if not base_ts:
        return False
    if now_ts - base_ts < BABY_STARVE_SECONDS:
        return False

    child_id = _ensure_child_id(child)
    child["dead"] = True
    child["death_ts"] = now_ts
    if not child.get("death_handled"):
        child["death_handled"] = True
        _apply_intimacy(group, uid, lover_id, -BABY_DEATH_INTIMACY_PENALTY)

    _sync_child_fields(
        group,
        lover_id,
        child_id,
        {"dead": True, "death_ts": now_ts, "death_handled": True},
    )
    return True


@register_command("求婚")
@feature_required(FEATURE_FRIENDS)
async def marry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(
            update, context, "请使用【回复你要表白的人】的方式使用 求婚"
        )

    lover = update.message.reply_to_message.from_user
    lover_id = str(lover.id)
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)

    if user_id == lover_id:
        return await safe_reply(update, context, "你不能和自己结婚...")

    # 初始化双方用户数据
    ensure_user_exists(chat_id, user_id, user.full_name)
    ensure_user_exists(chat_id, lover_id, lover.full_name)

    userData = load_json(INFO_FILE)
    user_info = userData.get(chat_id, {}).get("users", {}).get(user_id)
    lover_data = userData.get(chat_id, {}).get("users", {}).get(lover_id)

    if user_info is None or lover_data is None:
        return await safe_reply(update, context, "未找到用户数据，请稍后重试。")

    if user_info.get("relationship_status") == "包养中":
        return await safe_reply(
            update, context, random.choice(OWNER_SPONSORED_WARNINGS)
        )
    elif user_info.get("relationship_status") == "已婚":
        return await safe_reply(update, context, random.choice(OWNER_MARRIED_WARNINGS))

    if lover_data.get("relationship_status") == "包养中":
        return await safe_reply(
            update, context, random.choice(LOVER_SPONSORED_WARNINGS)
        )
    elif lover_data.get("relationship_status") == "已婚":
        return await safe_reply(update, context, random.choice(LOVER_MARRIED_WARNINGS))

    data = _load_marry_data(context)
    group = data.setdefault(chat_id, {})
    user_info_marry = group.setdefault(user_id, {})

    user_info_marry["pending"] = lover_id
    _save_marry_data(context, data)

    await safe_reply(
        update,
        context,
        f"💌 你向 {_mention_or_name(lover_id, lover.full_name, is_silent)} 表白了，等待对方使用 同意 接受！",
        html=True,
    )


@register_command("同意")
@feature_required(FEATURE_FRIENDS)
async def accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)

    data = _load_marry_data(context)
    group = data.get(chat_id, {})

    for uid, info in group.items():
        if info.get("pending") == user_id:
            if uid == user_id:
                return await safe_reply(update, context, "你不能和自己结婚...")
            # 确认双方数据初始化
            ensure_user_exists(chat_id, user_id, user.full_name)
            lover_user = await context.bot.get_chat_member(
                chat_id=int(chat_id), user_id=int(uid)
            )
            lover_full_name = lover_user.user.full_name if lover_user else "对象"
            ensure_user_exists(chat_id, uid, lover_full_name)

            # 更新结婚数据
            now_str = datetime.now().strftime("%Y-%m-%d")
            group.setdefault(uid, {})["lover_id"] = user_id
            group[uid]["since"] = now_str
            group[uid]["pending"] = None

            group.setdefault(user_id, {})["lover_id"] = uid
            group[user_id]["since"] = now_str
            group[user_id]["pending"] = None

            _save_marry_data(context, data)

            # 更新金币系统的状态
            userData = load_json(INFO_FILE)
            users = userData.setdefault(chat_id, {}).setdefault("users", {})
            users[user_id]["relationship_status"] = "已婚"
            users[uid]["relationship_status"] = "已婚"
            save_json(INFO_FILE, userData)

            mention1 = _mention_or_name(uid, "你", is_silent)
            mention2 = _user_ref(user, is_silent)
            return await safe_reply(
                update,
                context,
                f"🎉 恭喜！{mention1} 和 {mention2} 正式成为情侣了！💞",
                html=True,
            )

    await safe_reply(update, context, "没有人向你表白，别自作多情~")


@register_command("伴侣")
@feature_required(FEATURE_FRIENDS)
async def lover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)

    data = _load_marry_data(context)
    lover_id = data.get(chat_id, {}).get(user_id, {}).get("lover_id")
    since = data.get(chat_id, {}).get(user_id, {}).get("since")

    if lover_id:
        mention = _mention_or_name(lover_id, "你的对象", is_silent)
        await safe_reply(
            update, context, f"💕 {mention}，在一起时间：{since}", html=True
        )
    else:
        await safe_reply(update, context, "你目前是单身。")


@register_command("离婚")
@feature_required(FEATURE_FRIENDS)
async def divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)

    data = _load_marry_data(context)
    group = data.get(chat_id, {})
    lover_id = group.get(user_id, {}).get("lover_id")

    if lover_id:
        now = datetime.now().strftime("%Y-%m-%d")

        # 添加前任记录
        for uid, other_uid in [(user_id, lover_id), (lover_id, user_id)]:
            ex_list = group[uid].setdefault("exes", [])
            ex_list.append(
                {
                    "id": other_uid,
                    "since": group[uid].get("since", "未知"),
                    "until": now,
                }
            )
            group[uid]["lover_id"] = None
            group[uid]["since"] = None

        _save_marry_data(context, data)

        userData = load_json(INFO_FILE)
        users = userData.setdefault(chat_id, {}).setdefault("users", {})
        users[user_id]["relationship_status"] = "单身"
        users[uid]["relationship_status"] = "单身"
        save_json(INFO_FILE, userData)

        await safe_reply(
            update,
            context,
            f"💔 你和 {_mention_or_name(lover_id, '你的对象', is_silent)} 已解除关系。",
            html=True,
        )
    else:
        await safe_reply(update, context, "你并没有情侣。")


@register_command("前任")
@feature_required(FEATURE_FRIENDS)
async def exes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)

    data = _load_marry_data(context)
    group = data.get(chat_id, {})
    ex_list = group.get(user_id, {}).get("exes", [])
    userData = load_json(INFO_FILE)
    users = userData.setdefault(chat_id, {}).setdefault("users", {})

    if not ex_list:
        return await safe_reply(update, context, "你目前还没有前任～")

    lines = ["📜 前任列表："]
    for ex in ex_list:
        ex_id = str(ex.get("id", ""))
        ex_info = users.get(ex_id, {})
        ex_name = ex_info.get("name") or f"用户{ex_id or '未知'}"
        lines.append(
            f"- {ex_name}（{ex.get('since', '未知')} ~ {ex.get('until', '未知')}）"
        )

    await safe_reply(update, context, "\n".join(lines))


async def lover_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action_name: str
):
    if not update.message or not update.message.reply_to_message:
        return
        return await safe_reply(
            update, context, f"请回复你对象的消息使用 {action_name}"
        )

    user = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)

    uid = str(user.id)
    target_id = str(target.id)

    if uid == target_id:
        return await safe_reply(update, context, "你这是在对自己做什么啦😳")

    data = _load_marry_data(context)
    lover_id = data.get(chat_id, {}).get(uid, {}).get("lover_id")

    if lover_id != target_id:
        return 
        return await safe_reply(update, context, "❌ 只能对你的对象使用这个行为。")

    group = data.setdefault(chat_id, {})
    me = group.setdefault(uid, {})
    other = group.setdefault(str(lover_id), {})

    now_ts = int(datetime.now().timestamp())
    cooldowns = me.setdefault("action_cd", {})
    last_ts = int(cooldowns.get(action_name, 0) or 0)
    if now_ts - last_ts < INTIMACY_ACTION_COOLDOWN:
        remain = INTIMACY_ACTION_COOLDOWN - (now_ts - last_ts)
        return await safe_reply(update, context, f"动作冷却中，请 {remain} 秒后再试。")

    text = random.choice(ACTION_TEXTS[action_name])
    points = INTIMACY_ACTION_POINTS.get(action_name, 0)
    if points > 0:
        me["intimacy"] = int(me.get("intimacy", 0) or 0) + points
        other["intimacy"] = int(other.get("intimacy", 0) or 0) + points
        cooldowns[action_name] = now_ts
        _save_marry_data(context, data)

    await safe_reply(
        update,
        context,
        f"{_user_ref(user, is_silent)} {text} {_user_ref(target, is_silent)} 💕（亲密度 +{points}）",
        html=True,
    )


@register_command("抱抱")
@feature_required(FEATURE_FRIENDS)
async def hug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await lover_action(update, context, "抱抱")


@register_command("亲亲")
@feature_required(FEATURE_FRIENDS)
async def kiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await lover_action(update, context, "亲亲")


@register_command("举高高")
@feature_required(FEATURE_FRIENDS)
async def tie_tie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await lover_action(update, context, "举高高")


@register_command("摸头")
@feature_required(FEATURE_FRIENDS)
async def pat_head(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await lover_action(update, context, "摸头")


@register_command("撒娇")
@feature_required(FEATURE_FRIENDS)
async def act_cute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await lover_action(update, context, "撒娇")


@register_command("洞房")
@feature_required(FEATURE_FRIENDS)
async def dongfang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请回复你对象的消息使用 洞房")

    user = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    is_silent = _is_chat_silent(context, chat_id)

    uid = str(user.id)
    tid = str(target.id)

    if uid == tid:
        return await safe_reply(update, context, "……你冷静一点 😳")

    data = _load_marry_data(context)
    group = data.get(chat_id, {})
    user_info = group.get(uid, {})

    # 必须是已婚
    if user_info.get("lover_id") != tid:
        return await safe_reply(update, context, "❌ 只能对你的伴侣使用 洞房")

    # 冷却判断
    now = datetime.now().timestamp()
    cd_key = (chat_id, uid)
    if now - dongfang_cooldown.get(cd_key, 0) < DONGFANG_COOLDOWN:
        return await safe_reply(update, context, "🕰️ 洞房刚结束，休息一下吧~")

    dongfang_cooldown[cd_key] = now
    _apply_intimacy(group, uid, tid, DONGFANG_INTIMACY_POINTS)
    _save_marry_data(context, data)

    # 基础描述（不露骨）
    await safe_reply(
        update,
        context,
        f"💞 {_user_ref(user, is_silent)} 与 {_user_ref(target, is_silent)} 共度了一个浪漫的夜晚……（亲密度 +{DONGFANG_INTIMACY_POINTS}）",
        html=True,
    )

    # 概率生宝宝
    if random.random() > BABY_PROBABILITY:
        return  # 没怀上，结束

    # 👶 生宝宝
    baby_name = random.choice(["小团子", "小星星", "小奶糖", "小糯米", "小月亮"])
    today = datetime.now().strftime("%Y-%m-%d")
    now_ts = int(datetime.now().timestamp())
    baby_id = _new_baby_id()
    baby_gender = random.choice(["男宝宝", "女宝宝"])

    for pid in (uid, tid):
        pdata = group.setdefault(pid, {})
        children = pdata.setdefault("children", [])
        children.append(
            {
                "id": baby_id,
                "name": baby_name,
                "gender": baby_gender,
                "birthday": today,
                "born_ts": now_ts,
                "parents": [uid, tid],
            }
        )

    _apply_intimacy(group, uid, tid, BABY_BIRTH_INTIMACY_POINTS)
    _save_marry_data(context, data)

    await safe_reply(
        update,
        context,
        f"👶✨ 喜讯！{_user_ref(user, is_silent)} 和 {_user_ref(target, is_silent)} 迎来了{baby_gender} **{baby_name}**！"
        f"（亲密度 +{BABY_BIRTH_INTIMACY_POINTS}）",
        html=True,
    )


@register_command("宝宝")
@feature_required(FEATURE_FRIENDS)
async def children(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    chat_id = str(update.effective_chat.id)

    data = _load_marry_data(context)
    children = data.get(chat_id, {}).get(uid, {}).get("children", [])

    if not children:
        return
        # return await safe_reply(update, context, "👶 你目前还没有宝宝。")

    msg = "👶 你的宝宝们：\n"
    changed = False
    now_ts = int(datetime.now().timestamp())
    lover_id = data.get(chat_id, {}).get(uid, {}).get("lover_id")
    for i, c in enumerate(children, 1):
        if not str(c.get("id", "")).strip():
            c["id"] = _new_baby_id()
            changed = True
        cid = str(c.get("id", ""))
        short_id = cid[-6:] if cid else "------"
        last_fed = int(c.get("last_fed", 0) or 0)
        feed_count = int(c.get("feed_count", 0) or 0)
        if _maybe_handle_starve(data.get(chat_id, {}), uid, lover_id, c, now_ts):
            changed = True
        growth = c.get("growth_stage") or _baby_growth_stage(feed_count)
        if c.get("growth_stage") != growth:
            c["growth_stage"] = growth
            _sync_child_fields(data.get(chat_id, {}), lover_id, cid, {"growth_stage": growth})
            changed = True
        gender = c.get("gender", "未知")
        status = "夭折" if c.get("dead") else "健康"
        msg += (
            f"{i}. {c.get('name', '未命名')}（{gender}，{status}，成长：{growth}，"
            f"出生：{c.get('birthday', '未知')}，ID:{short_id}，喂养：{_format_since(last_fed)}）\n"
        )

    if changed:
        _save_marry_data(context, data)

    await safe_reply(update, context, msg)


@register_command("宝宝改名")
@feature_required(FEATURE_FRIENDS)
async def rename_child(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    uid = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)

    if len(context.args) < 2:
        return await safe_reply(
            update,
            context,
            "用法：宝宝改名 编号 新名字\n例如：宝宝改名 2 小糯米",
        )

    selector = str(context.args[0]).strip()
    new_name = " ".join(context.args[1:]).strip()
    if not new_name:
        return await safe_reply(update, context, "❗ 新名字不能为空。")
    if len(new_name) > 20:
        return await safe_reply(update, context, "❗ 宝宝名字太长了（最多20字）。")

    data = _load_marry_data(context)
    group = data.setdefault(chat_id, {})
    my_info = group.setdefault(uid, {})
    my_children = my_info.setdefault("children", [])
    lover_id = my_info.get("lover_id")

    if not my_children:
        return await safe_reply(update, context, "👶 你目前还没有宝宝。")

    target, err = _get_child_by_selector(my_children, selector)
    if err:
        return await safe_reply(update, context, f"❗ {err}")
    target_idx = my_children.index(target)
    old_name = str(target.get("name", "未命名"))
    child_id = _ensure_child_id(target)
    target["name"] = new_name

    if lover_id:
        partner_info = group.setdefault(str(lover_id), {})
        partner_children = partner_info.setdefault("children", [])
        synced = False
        for c in partner_children:
            if _ensure_child_id(c) == child_id:
                c["name"] = new_name
                synced = True
                break
        if not synced and 0 <= target_idx < len(partner_children):
            partner_children[target_idx]["name"] = new_name

    _save_marry_data(context, data)
    await safe_reply(
        update,
        context,
        f"✅ 宝宝改名成功：{target_idx + 1}. {old_name} → {new_name}",
    )


@register_command("宝宝喂养", "喂养宝宝")
@feature_required(FEATURE_FRIENDS)
async def feed_child(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    uid = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)

    if not context.args:
        return await safe_reply(update, context, "用法：宝宝喂养 编号\n例如：宝宝喂养 1")

    selector = str(context.args[0]).strip()
    data = _load_marry_data(context)
    group = data.setdefault(chat_id, {})
    my_info = group.setdefault(uid, {})
    my_children = my_info.setdefault("children", [])
    lover_id = my_info.get("lover_id")

    if not my_children:
        return await safe_reply(update, context, "👶 你目前还没有宝宝。")

    target, err = _get_child_by_selector(my_children, selector)
    if err:
        return await safe_reply(update, context, f"❗ {err}")

    now_ts = int(datetime.now().timestamp())
    if _maybe_handle_starve(group, uid, lover_id, target, now_ts):
        _save_marry_data(context, data)
        return await safe_reply(update, context, "💔 宝宝已经夭折，无法喂养。")
    if target.get("dead"):
        return await safe_reply(update, context, "💔 宝宝已经夭折，无法喂养。")

    last_fed = int(target.get("last_fed", 0) or 0)
    if last_fed and now_ts - last_fed < BABY_FEED_COOLDOWN:
        remain = BABY_FEED_COOLDOWN - (now_ts - last_fed)
        return await safe_reply(update, context, f"🍼 宝宝刚喂过，{remain} 秒后再试。")

    target["last_fed"] = now_ts
    target["feed_count"] = int(target.get("feed_count", 0) or 0) + 1
    target["growth_stage"] = _baby_growth_stage(int(target["feed_count"]))
    child_id = _ensure_child_id(target)

    if lover_id:
        partner_info = group.setdefault(str(lover_id), {})
        partner_children = partner_info.setdefault("children", [])
        for c in partner_children:
            if _ensure_child_id(c) == child_id:
                c["last_fed"] = now_ts
                c["feed_count"] = int(c.get("feed_count", 0) or 0) + 1
                c["growth_stage"] = _baby_growth_stage(int(c["feed_count"]))
                break

    _save_marry_data(context, data)
    await safe_reply(
        update,
        context,
        f"🍼 已喂养宝宝：{target.get('name', '未命名')}，成长为 {target.get('growth_stage')}",
    )


@register_command("情侣亲密榜", "亲密榜")
@feature_required(FEATURE_FRIENDS)
async def intimacy_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = _load_marry_data(context)
    group = data.get(chat_id, {})
    if not group:
        
        return await safe_reply(update, context, "当前没有情侣数据。")

    pairs = []
    seen = set()
    for uid, info in group.items():
        lover_id = info.get("lover_id")
        if not lover_id:
            continue
        key = tuple(sorted([str(uid), str(lover_id)]))
        if key in seen:
            continue
        seen.add(key)
        a = group.get(key[0], {})
        b = group.get(key[1], {})
        intimacy = max(int(a.get("intimacy", 0) or 0), int(b.get("intimacy", 0) or 0))
        pairs.append((key[0], key[1], intimacy))

    if not pairs:
        return await safe_reply(update, context, "当前没有情侣数据。")

    pairs.sort(key=lambda x: x[2], reverse=True)
    top = pairs[:10]
    lines = ["情侣亲密值榜单 Top10："]
    for i, (a_id, b_id, val) in enumerate(top, 1):
        a_name = _mention_or_name(a_id, "用户", _is_chat_silent(context, chat_id))
        b_name = _mention_or_name(b_id, "用户", _is_chat_silent(context, chat_id))
        lines.append(f"{i}. {a_name} ❤ {b_name}：{val}")

    await safe_reply(update, context, "\n".join(lines), html=True)


def register_marry_handlers(app):
    app.add_handler(CommandHandler("marry", marry))
    app.add_handler(CommandHandler("accept", accept))
    app.add_handler(CommandHandler("lover", lover))
    app.add_handler(CommandHandler("divorce", divorce))
    app.add_handler(CommandHandler("exes", exes))
    app.add_handler(CommandHandler("kiss", kiss))
    app.add_handler(CommandHandler("hug", hug))
    app.add_handler(CommandHandler("tie_tie", tie_tie))
    app.add_handler(CommandHandler("pat_head", pat_head))
    app.add_handler(CommandHandler("act_cute", act_cute))
    app.add_handler(CommandHandler("dongfang", dongfang))
    app.add_handler(CommandHandler("children", children))
    app.add_handler(CommandHandler("rename_child", rename_child))
    app.add_handler(CommandHandler("feed_child", feed_child))
    app.add_handler(CommandHandler("intimacy_rank", intimacy_rank))
