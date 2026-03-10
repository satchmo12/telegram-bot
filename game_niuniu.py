import random
from datetime import datetime, date
from html import escape
from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
)
from command_router import register_command
from database import *
import config
from utils import get_group_whitelist

LUAFA_COOLDOWN_SECONDS = 6000
last_lufa_ts = {}
PK_COOLDOWN_SECONDS = 3600
QJ_COOLDOWN_SECONDS = 18000
KJ_COOLDOWN_SECONDS = 12000
YP_COOLDOWN_SECONDS = 18000
AV_COOLDOWN_SECONDS = 6000
last_action_ts = {}
NAQIE_COST = 30
NAQIE_DAILY_PER_CONCUBINE = 1
SIGN_FLAVOR_LINES = [
    "🌤️ 今日状态拉满，开工就有好手感！",
    "🍀 运势不错，今天适合狠狠干票大的。",
    "🎯 节奏在线，这波成长来得刚刚好。",
]
LUAFA_FLAVOR_LINES = [
    "🔥 一顿操作猛如虎，战意瞬间点满。",
    "⚡ 手起刀落，临场状态直接起飞。",
    "😼 小试牛刀，感觉今天谁都能碰一碰。",
]
JY_FLAVOR_LINES = [
    "🤝 江湖有情有义，这波兄弟情拉满。",
    "💸 虽然肉疼，但面子和义气都在。",
    "🍻 一句都在酒里，长度也在礼里。",
]
QJ_SUCCESS_LINES = [
    "😈 出手果断，现场直接被你拿捏。",
    "🗡️ 节奏压制到位，对面毫无还手空间。",
    "🎯 这一击又准又狠，收益稳稳到手。",
]
QJ_FAIL_LINES = [
    "💥 反打来得太快，场面当场失控。",
    "🛡️ 对面防守拉满，你被反制了一手。",
    "😵 本想狠狠干票大的，结果翻车了。",
]
AV_FLAVOR_LINES = [
    "🎬 观摩结束，灵感和自信一起上涨。",
    "📈 学习资料到位，状态曲线持续上扬。",
    "🧠 技术复盘完成，下一把更有把握。",
]
KJ_SUCCESS_LINES = [
    "🕶️ 动作干净利落，现场无人察觉。",
    "🎒 捞完就跑，这波身法有点东西。",
    "💨 快进快出，收益直接装进口袋。",
]
KJ_FAIL_LINES = [
    "🚨 刚伸手就被逮住，场面极度尴尬。",
    "👮 对面警觉拉满，你当场翻车。",
    "🫠 想偷鸡结果蚀把米，这波亏了。",
]
YP_SUCCESS_LINES = [
    "🌹 氛围拿捏到位，今晚直接甜度超标。",
    "✨ 双方状态在线，体验值一路飙升。",
    "🥂 进展顺利，心情和状态双丰收。",
]
YP_FAIL_LINES = [
    "🕰️ 等到夜深人静，还是没等来消息。",
    "🥶 气氛刚热起来，对面突然失联。",
    "📵 剧本走偏，最后只剩自己emo。",
]

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS niuniu_concubines (
    user_id INTEGER PRIMARY KEY,
    count INTEGER DEFAULT 0,
    last_income_date TEXT
)
"""
)
conn.commit()

# ===== 工具函数 =====


def today():
    return str(date.today())


def get_length(user_id):
    cursor.execute("SELECT length FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()[0]


def get_profile_row(user_id):
    cursor.execute(
        "SELECT user_id, username, length, last_sign, anonymous FROM users WHERE user_id=?",
        (user_id,),
    )
    return cursor.fetchone()


def ensure_concubine_row(user_id: int):
    cursor.execute(
        "SELECT user_id, count, last_income_date FROM niuniu_concubines WHERE user_id=?",
        (user_id,),
    )
    row = cursor.fetchone()
    if row:
        return row
    cursor.execute(
        "INSERT INTO niuniu_concubines (user_id, count, last_income_date) VALUES (?, ?, ?)",
        (user_id, 0, None),
    )
    conn.commit()
    cursor.execute(
        "SELECT user_id, count, last_income_date FROM niuniu_concubines WHERE user_id=?",
        (user_id,),
    )
    return cursor.fetchone()


def get_concubine_count(user_id: int) -> int:
    row = ensure_concubine_row(user_id)
    return int(row[1] or 0)


def add_concubine(user_id: int, inc: int = 1):
    row = ensure_concubine_row(user_id)
    new_count = max(0, int(row[1] or 0)) + max(0, int(inc))
    cursor.execute(
        "UPDATE niuniu_concubines SET count=? WHERE user_id=?",
        (new_count, user_id),
    )
    conn.commit()


def apply_daily_concubine_income(user_id: int):
    row = ensure_concubine_row(user_id)
    count = int(row[1] or 0)
    last_income_date = row[2]
    td = today()
    if count <= 0 or last_income_date == td:
        return 0, count

    per = int(getattr(config, "NAQIE_DAILY_PER_CONCUBINE", NAQIE_DAILY_PER_CONCUBINE))
    gain = max(0, count * per)
    if gain > 0:
        update_length(user_id, gain)
    cursor.execute(
        "UPDATE niuniu_concubines SET last_income_date=? WHERE user_id=?",
        (td, user_id),
    )
    conn.commit()
    return gain, count


def set_anonymous(user_id: int, value: int):
    cursor.execute("UPDATE users SET anonymous=? WHERE user_id=?", (value, user_id))
    conn.commit()


def display_name(user, db_user) -> str:
    if db_user and int(db_user[4] or 0) == 1:
        return "匿名用户"
    if user and user.username:
        return f"@{user.username}"
    if user and user.first_name:
        return user.first_name
    return "未知用户"


def safe_randint(rng, fallback):
    if isinstance(rng, (tuple, list)) and len(rng) == 2:
        lo, hi = int(rng[0]), int(rng[1])
        if lo > hi:
            lo, hi = hi, lo
        return random.randint(lo, hi)
    return random.randint(fallback[0], fallback[1])


def check_action_cooldown(chat_id: int, user_id: int, action_key: str, cooldown: int):
    cooldown = max(1, int(cooldown))
    key = f"{chat_id}:{user_id}:{action_key}"
    now_ts = int(datetime.now().timestamp())
    last_ts = int(last_action_ts.get(key, 0))
    remain = cooldown - (now_ts - last_ts)
    if remain > 0:
        return remain
    last_action_ts[key] = now_ts
    return 0


# ===== 指令 =====
@register_command("牛牛指令")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🍆 你好！我是鸡巴机器人 🤖

💯 /sign - 每日签到
⚔️ /pk - 回复他人发起PK
🎁 /jy - 回复他人赠送长度
😈 /qj - 回复他人强奸
🔍 /dick - 查看长度
📊 /info - 个人信息
🔞 /av - 看AV
🐔 /kj - 偷取
❤️ /yp - 约炮
📌 /aim - 置顶10分钟
📍 /unaim - 取消置顶
🏆 /leaderboard - 排行榜
⚙️ /setting - 匿名模式
"""
    await update.message.reply_text(text)


# ===== 签到 =====
@register_command("撸一发", "签到")
async def sign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    is_lufa = text.startswith("撸一发")

    get_user(user.id, user.username)
    daily_gain, concubines = apply_daily_concubine_income(user.id)
    if daily_gain > 0:
        await update.message.reply_text(f"🏮 妾室供养到账：+{daily_gain}cm（{concubines}位）")
    gain = safe_randint(getattr(config, "SIGN_GAIN", (1, 5)), (1, 5))

    if is_lufa:
        cooldown = int(getattr(config, "LUAFA_COOLDOWN_SECONDS", LUAFA_COOLDOWN_SECONDS))
        cooldown = max(1, cooldown)
        key = f"{chat_id}:{user.id}"
        now_ts = int(datetime.now().timestamp())
        last_ts = int(last_lufa_ts.get(key, 0))
        remain = cooldown - (now_ts - last_ts)
        if remain > 0:
            return await update.message.reply_text(
                f"⏳ 撸一发冷却中，还需 {remain} 秒"
            )
        update_length(user.id, gain)
        last_lufa_ts[key] = now_ts
        lines = [
            f"🔥 撸一发成功 +{gain}cm",
            random.choice(LUAFA_FLAVOR_LINES),
        ]
        return await update.message.reply_text("\n".join(lines))

    db_user = get_user(user.id, user.username)
    if db_user[3] == today():
        await update.message.reply_text("❌ 今天已签到")
        return
    update_length(user.id, gain)
    set_sign(user.id)
    lines = [
        f"✅ 签到成功 +{gain}cm",
        random.choice(SIGN_FLAVOR_LINES),
    ]
    await update.message.reply_text("\n".join(lines))


# ===== 查看长度 =====
@register_command("我要验牌", "我要演牌")
async def dick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    target_user = update.effective_user
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user

    get_user(target_user.id, target_user.username)
    length = get_length(target_user.id)
    name = target_user.full_name or target_user.first_name or "该用户"
    await update.message.reply_text(f"📏 {name} 当前长度：{length} cm")


# ===== PK =====
@register_command("对狙", "操", "日")
async def pk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ 请回复对方")
        return

    attacker = update.effective_user
    defender = update.message.reply_to_message.from_user
    if not defender or defender.is_bot:
        await update.message.reply_text("⚠️ 不能对机器人发起PK")
        return
    if attacker.id == defender.id:
        await update.message.reply_text("⚠️ 不能和自己PK")
        return

    remain = check_action_cooldown(
        update.effective_chat.id,
        attacker.id,
        "pk",
        getattr(config, "PK_COOLDOWN_SECONDS", PK_COOLDOWN_SECONDS),
    )
    if remain > 0:
        await update.message.reply_text(f"⏳ PK冷却中，还需 {remain} 秒")
        return

    get_user(attacker.id, attacker.username)
    get_user(defender.id, defender.username)

    winner = random.choice([attacker, defender])
    loser = defender if winner == attacker else attacker
    gain = safe_randint(getattr(config, "PK_GAIN", (1, 3)), (1, 3))

    update_length(winner.id, gain)
    update_length(loser.id, -gain)
    lines = [
        f"⚔️ {attacker.first_name} VS {defender.first_name}，大战一触即发！",
        f"💥 终极一击命中，{winner.first_name} 拿下胜利！",
        f"🏆 本局战果：{winner.first_name} +{gain}cm",
        f"😵 败者 {loser.first_name} -{gain}cm",
    ]
    await update.message.reply_text("\n".join(lines))


# ===== 赠送 =====
@register_command("激情", "割吊")
async def jy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ 请回复对方")
        return

    sender = update.effective_user
    receiver = update.message.reply_to_message.from_user
    if not receiver or receiver.is_bot:
        await update.message.reply_text("⚠️ 不能赠送给机器人")
        return
    if sender.id == receiver.id:
        await update.message.reply_text("⚠️ 不能给自己赠送")
        return

    get_user(sender.id, sender.username)
    get_user(receiver.id, receiver.username)
    if get_length(sender.id) < 5:
        await update.message.reply_text("❌ 你的长度不足 5cm")
        return

    update_length(sender.id, -5)
    update_length(receiver.id, 5)

    lines = [
        f"🎁 {sender.first_name} 向 {receiver.first_name} 赠送成功 5cm",
        random.choice(JY_FLAVOR_LINES),
    ]
    await update.message.reply_text("\n".join(lines))


# ===== 排行榜 =====
@register_command("排行榜", "牛牛", "给我擦皮鞋")
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    cursor.execute(
        "SELECT user_id, username, length FROM users ORDER BY length DESC LIMIT 10"
    )
    rows = cursor.fetchall()
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))

    text = "🏆牛牛长度全球排行榜\n\n"
    for i, row in enumerate(rows, 1):
        user_id = int(row[0])
        db_username = row[1]
        raw_name = None
        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id, user_id
            )
            if member and member.user:
                raw_name = member.user.full_name
        except Exception:
            raw_name = None

        if not raw_name:
            raw_name = db_username if db_username else f"用户{user_id}"

        name = escape(str(raw_name))
        length = int(row[2])
        if is_silent:
            text += f"{i}. {name} - {length}cm\n"
        else:
            text += f'{i}. <a href="tg://user?id={user_id}">{name}</a> - {length}cm\n'

    await update.message.reply_text(text, parse_mode=("HTML" if not is_silent else None))


# ===== 置顶 =====
@register_command("置顶")
async def aim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    get_user(user.id, user.username)
    if get_length(user.id) < config.AIM_COST:
        await update.message.reply_text("❌ 长度不足")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ 请回复一条消息后再置顶")
        return

    update_length(user.id, -config.AIM_COST)
    await context.bot.pin_chat_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.reply_to_message.message_id,
        disable_notification=True,
    )
    await update.message.reply_text(f"📌 置顶成功，消耗 {config.AIM_COST}cm")


# ===== 取消置顶 =====
@register_command("取消置顶")
async def unaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    get_user(user.id, user.username)
    if get_length(user.id) < config.UNAIM_COST:
        await update.message.reply_text("❌ 长度不足")
        return

    update_length(user.id, -config.UNAIM_COST)
    await context.bot.unpin_chat_message(chat_id=update.effective_chat.id)
    await update.message.reply_text(f"📍 取消置顶成功，消耗 {config.UNAIM_COST}cm")


# ===== 强奸 =====
@register_command("强奸")
async def qj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("⚠️ 请回复对方")

    attacker = update.effective_user
    target = update.message.reply_to_message.from_user
    if not target or target.is_bot:
        return await update.message.reply_text("⚠️ 不能对机器人使用")
    if attacker.id == target.id:
        return await update.message.reply_text("⚠️ 不能对自己使用")

    remain = check_action_cooldown(
        update.effective_chat.id,
        attacker.id,
        "qj",
        getattr(config, "QJ_COOLDOWN_SECONDS", QJ_COOLDOWN_SECONDS),
    )
    if remain > 0:
        return await update.message.reply_text(f"⏳ 强奸冷却中，还需 {remain} 秒")

    get_user(attacker.id, attacker.username)
    get_user(target.id, target.username)

    steal = random.randint(2, 8)
    success_rate = 0.6
    if random.random() <= success_rate:
        update_length(attacker.id, steal)
        update_length(target.id, -steal)
        lines = [
            f"😈 行动成功，{attacker.first_name} 从 {target.first_name} 身上掠夺 {steal}cm",
            random.choice(QJ_SUCCESS_LINES),
        ]
        return await update.message.reply_text("\n".join(lines))

    loss = max(1, steal // 2)
    update_length(attacker.id, -loss)
    update_length(target.id, loss)
    lines = [
        f"💥 行动失败，{attacker.first_name} 反被 {target.first_name} 反杀 {loss}cm",
        random.choice(QJ_FAIL_LINES),
    ]
    await update.message.reply_text("\n".join(lines))


# ===== 个人信息 =====
@register_command("牛牛信息", "个人信息")
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    get_user(user.id, user.username)
    daily_gain, concubines = apply_daily_concubine_income(user.id)
    row = get_profile_row(user.id)
    if not row:
        return

    cursor.execute("SELECT COUNT(*) FROM users WHERE length > ?", (int(row[2]),))
    better_count = int(cursor.fetchone()[0])
    rank = better_count + 1
    anon_text = "开启" if int(row[4] or 0) == 1 else "关闭"
    name_text = display_name(user, row)
    sign_text = row[3] or "从未签到"

    msg = (
        "📊 牛牛个人信息\n\n"
        f"👤 昵称：{name_text}\n"
        f"📏 长度：{int(row[2])} cm\n"
        f"🏆 排名：#{rank}\n"
        f"🕒 上次签到：{sign_text}\n"
        f"🕶 匿名模式：{anon_text}\n"
        f"🏮 妾室数量：{concubines}（每日增长 {concubines}cm）"
    )
    if daily_gain > 0:
        msg = f"🏮 妾室供养到账：+{daily_gain}cm（{concubines}位）\n\n" + msg
    await update.message.reply_text(msg)


# ===== 看AV =====
@register_command("看AV", "看片")
async def av(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    remain = check_action_cooldown(
        update.effective_chat.id,
        user.id,
        "av",
        getattr(config, "AV_COOLDOWN_SECONDS", AV_COOLDOWN_SECONDS),
    )
    if remain > 0:
        return await update.message.reply_text(f"⏳ 看片冷却中，还需 {remain} 秒")

    get_user(user.id, user.username)

    gain = safe_randint(getattr(config, "AV_GAIN", (1, 4)), (1, 4))
    update_length(user.id, gain)
    lines = [
        f"🔞 你看了一会儿AV，状态提升 +{gain}cm",
        random.choice(AV_FLAVOR_LINES),
    ]
    await update.message.reply_text("\n".join(lines))


# ===== 偷取 =====
@register_command("偷取", "口交")
async def kj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("⚠️ 请回复对方")

    thief = update.effective_user
    victim = update.message.reply_to_message.from_user
    if not victim or victim.is_bot:
        return await update.message.reply_text("⚠️ 不能偷机器人")
    if thief.id == victim.id:
        return await update.message.reply_text("⚠️ 不能偷自己")

    remain = check_action_cooldown(
        update.effective_chat.id,
        thief.id,
        "kj",
        getattr(config, "KJ_COOLDOWN_SECONDS", KJ_COOLDOWN_SECONDS),
    )
    if remain > 0:
        return await update.message.reply_text(f"⏳ 偷取冷却中，还需 {remain} 秒")

    get_user(thief.id, thief.username)
    get_user(victim.id, victim.username)

    steal = random.randint(1, 5)
    if random.random() <= 0.5:
        update_length(thief.id, steal)
        update_length(victim.id, -steal)
        lines = [
            f"🐔 偷取成功，{thief.first_name} 从 {victim.first_name} 身上顺走 {steal}cm",
            random.choice(KJ_SUCCESS_LINES),
        ]
        await update.message.reply_text("\n".join(lines))
    else:
        penalty = max(1, steal // 2)
        update_length(thief.id, -penalty)
        lines = [
            f"🚨 偷取失败，{thief.first_name} 被抓扣了 {penalty}cm",
            random.choice(KJ_FAIL_LINES),
        ]
        await update.message.reply_text("\n".join(lines))


# ===== 约炮 =====
@register_command("约炮")
async def yp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user

    remain = check_action_cooldown(
        update.effective_chat.id,
        user.id,
        "yp",
        getattr(config, "YP_COOLDOWN_SECONDS", YP_COOLDOWN_SECONDS),
    )
    if remain > 0:
        return await update.message.reply_text(f"⏳ 约炮冷却中，还需 {remain} 秒")

    get_user(user.id, user.username)

    gain = safe_randint(getattr(config, "YP_GAIN", (5, 10)), (5, 10))
    if random.random() <= 0.7:
        update_length(user.id, gain)
        lines = [
            f"❤️ 约会成功，状态爆棚 +{gain}cm",
            random.choice(YP_SUCCESS_LINES),
        ]
        await update.message.reply_text("\n".join(lines))
    else:
        loss = max(1, gain // 2)
        update_length(user.id, -loss)
        lines = [
            f"💔 被放鸽子，心情受挫 -{loss}cm",
            random.choice(YP_FAIL_LINES),
        ]
        await update.message.reply_text("\n".join(lines))


# ===== 匿名模式 =====
@register_command("匿名模式")
async def setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    get_user(user.id, user.username)
    row = get_profile_row(user.id)
    if not row:
        return

    current = int(row[4] or 0)
    new_value = 0 if current == 1 else 1
    set_anonymous(user.id, new_value)
    await update.message.reply_text(
        f"⚙️ 匿名模式已{'开启' if new_value == 1 else '关闭'}"
    )


# ===== 纳妾 =====
@register_command("纳妾")
async def naqie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    get_user(user.id, user.username)
    daily_gain, concubines = apply_daily_concubine_income(user.id)
    if daily_gain > 0:
        await update.message.reply_text(f"🏮 妾室供养到账：+{daily_gain}cm（{concubines}位）")

    cost = int(getattr(config, "NAQIE_COST", NAQIE_COST))
    if get_length(user.id) < cost:
        return await update.message.reply_text(f"❌ 长度不足，纳妾需要 {cost}cm")

    update_length(user.id, -cost)
    add_concubine(user.id, 1)
    total = get_concubine_count(user.id)
    per = int(getattr(config, "NAQIE_DAILY_PER_CONCUBINE", NAQIE_DAILY_PER_CONCUBINE))
    await update.message.reply_text(
        f"🏮 纳妾成功，消耗 {cost}cm。\n当前妾室：{total} 位（每日增长 {total * per}cm）"
    )


@register_command("妻妾排行榜", "妻妾榜", "妾榜", "纳妾榜")
async def concubine_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))

    cursor.execute(
        """
        SELECT user_id, count
        FROM niuniu_concubines
        WHERE count > 0
        ORDER BY count DESC, user_id ASC
        LIMIT 10
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("🏮 当前还没有人纳妾。")

    per = int(getattr(config, "NAQIE_DAILY_PER_CONCUBINE", NAQIE_DAILY_PER_CONCUBINE))
    text = "🏮 妻妾排行榜\n\n"
    for i, row in enumerate(rows, 1):
        user_id = int(row[0])
        count = int(row[1] or 0)

        raw_name = None
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            if member and member.user:
                raw_name = member.user.full_name
        except Exception:
            raw_name = None

        if not raw_name:
            cursor.execute("SELECT username FROM users WHERE user_id=?", (user_id,))
            r = cursor.fetchone()
            db_username = r[0] if r else None
            raw_name = db_username if db_username else f"用户{user_id}"

        name = escape(str(raw_name))
        if is_silent:
            text += f"{i}. {name} - 妾室 {count} 位（每日 +{count * per}cm）\n"
        else:
            text += (
                f'{i}. <a href="tg://user?id={user_id}">{name}</a>'
                f" - 妾室 {count} 位（每日 +{count * per}cm）\n"
            )

    await update.message.reply_text(text, parse_mode=("HTML" if not is_silent else None))


def register_niuniu_handlers(app):

    app.add_handler(CommandHandler("start_niuniu", start))
    app.add_handler(CommandHandler("sign", sign))
    app.add_handler(CommandHandler("dick", dick))
    app.add_handler(CommandHandler("pk", pk))
    app.add_handler(CommandHandler("jy", jy))
    app.add_handler(CommandHandler("qj", qj))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("av", av))
    app.add_handler(CommandHandler("kj", kj))
    app.add_handler(CommandHandler("yp", yp))
    app.add_handler(CommandHandler("setting", setting))
    app.add_handler(CommandHandler("naqie", naqie))
    app.add_handler(CommandHandler("naqie_top", concubine_leaderboard))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("aim", aim))
    app.add_handler(CommandHandler("unaim", unaim))
