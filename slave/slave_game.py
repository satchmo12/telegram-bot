# slave_game.py
import json
import os
import random
from html import escape
from telegram import Update, User
from telegram.ext import CommandHandler, ContextTypes
from command_router import FEATURE_FRIENDS, feature_required, register_command
from config import ESCAPE_LIMIT
from info.economy import (
    get_balance,
    change_balance,
    get_user_data,
    save_user_data,
)
from telegram.helpers import mention_html
from datetime import datetime
from slave.cooldown import is_on_cooldown
from slave.luck_helper import calculate_success
from utils import SLAVE_FILE, get_group_whitelist, load_json, safe_reply, save_json


# 价格控制参数
MAX_PRICE = 1000000  # 最高身价限制
MIN_PRICE = 100  # 最低身价限制
MAX_INCREASE = 1000  # 单次最高涨价幅度
MAX_WORK_HOURS = 4
WORK_REWARD_RATE = 0.10


# 计算奴隶身价
def calculate_new_price(old_price):
    chance_up = 0.6  # 60%涨价
    chance_down = 0.2  # 20%跌价
    chance_same = 0.2  # 20%不变

    rand_val = random.random()

    if rand_val < chance_up:
        # 涨价，涨幅递减
        if old_price < 1000:
            growth_rate = 1.5
        elif old_price < 5000:
            growth_rate = 1.3
        elif old_price < 20000:
            growth_rate = 1.15
        else:
            growth_rate = 1.05
        new_price = int(old_price * growth_rate)
    elif rand_val < chance_up + chance_down:
        # 跌价，跌幅随机
        decline_rate = random.uniform(0.85, 0.95)
        new_price = int(old_price * decline_rate)
    else:
        # 价格不变
        new_price = old_price

    # 限制最大涨幅
    if new_price > old_price + MAX_INCREASE:
        new_price = old_price + MAX_INCREASE

    # 价格上下限控制
    if new_price > MAX_PRICE:
        new_price = MAX_PRICE
    if new_price < MIN_PRICE:
        new_price = MIN_PRICE

    return new_price


def _get_slave_work_info(info: dict) -> dict:
    work = info.get("work")
    return work if isinstance(work, dict) else {}


def _is_slave_working(info: dict) -> bool:
    work = _get_slave_work_info(info)
    if not work:
        return False
    start_ts = int(work.get("start_ts", 0) or 0)
    hours = float(work.get("hours", 0) or 0)
    if not start_ts or hours <= 0:
        return False
    end_ts = start_ts + int(hours * 3600)
    return datetime.now().timestamp() < end_ts


def _calc_work_reward(hours: float, price: int) -> int:
    return int(hours * price * WORK_REWARD_RATE)


def _get_owned_slaves(group: dict, owner_id: str) -> list[tuple[str, dict]]:
    return [(sid, info) for sid, info in group.items() if info.get("owner") == owner_id]


# /buy 回复某人购买奴隶
@register_command("购买")
@feature_required(FEATURE_FRIENDS)
async def buy_slave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请回复你要购买的用户。")

    buyer = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    buyer_id = str(buyer.id)
    slave_id = str(target.id)
    user_id = buyer_id

    if buyer_id == slave_id:
        return await safe_reply(update, context, "你不能购买自己！")

    data = load_json(SLAVE_FILE)
    group = data.setdefault(chat_id, {})
    owned = [sid for sid, info in group.items() if info["owner"] == buyer_id]

    if len(owned) >= 100:
        return await safe_reply(
            update, context, "🚫 你最多只能拥有 100 个奴隶，无法再购买。"
        )

    user_info = get_user_data(chat_id, user_id)

    if user_info.get("stamina", 0) < 1:
        return await safe_reply(update, context, f"💤 你当前体力不足（需要 1 点）")

    old = group.get(slave_id)
    if old:
        price = old["price"]
        owner_id = old["owner"]
        owner_name = old.get("ownername", "原主人")

        if owner_id == buyer_id:
            return await safe_reply(
                update, context, "你已经是此人的主人，无需再次购买。"
            )

        buyer_data = group.get(buyer_id)
        if buyer_data and buyer_data.get("owner"):
            if buyer_data["owner"] == slave_id:
                return await safe_reply(update, context, "🚫 你不能购买你的主人！")

    else:
        price = 100
        owner_id = None
        owner_name = "系统"

    on_cd, remain = is_on_cooldown(chat_id, buyer_id, "购买", cooldown_seconds=120)
    if on_cd:
        return await safe_reply(
            update, context, f"⌛ 购买行为冷却中，请 {remain} 秒后再试。"
        )

    balance = user_info.get("balance", 100)

    if balance < price:
        return await safe_reply(
            update, context, f"💸 你的金币不足，还需 {price - balance} 枚。"
        )

    if owner_id:
        owner_info = get_user_data(chat_id, owner_id)
        owner_info["balance"] = owner_info["balance"] + price
        save_user_data(chat_id, owner_id, owner_info)

    # 扣除体力
    user_info["stamina"] = user_info["stamina"] - 1
    user_info["balance"] = user_info["balance"] - price

    save_user_data(chat_id, user_id, user_info)

    new_price = int(price * 1.25)
    # 计算新身价
    # new_price = calculate_new_price(price)

    # 初始化或追加交易历史
    history = old.get("history", []) if old else []
    history.append(
        {
            "from": owner_name,
            "to": buyer.full_name,
            "to_id": buyer_id,
            "price": price,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    group[slave_id] = {
        "owner": buyer_id,
        "ownername": buyer.full_name,
        "price": new_price,
        "nickname": target.full_name,
        "history": history,
    }

    # 保存数据
    save_json(SLAVE_FILE, data)

    await safe_reply(
        update,
        context,
        f"🔗 购买成功！{target.full_name} 现归你所有，身价变动：原价 {price} → 新价 {new_price} 金币（原主人：{owner_name}）。 可以发送赎复自由身。金币可以通过发送打工获取",
    )


@register_command("逃跑")
@feature_required(FEATURE_FRIENDS)
async def escape_slave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})
    info = group.get(user_id)
    if not info or not info.get("owner"):  # 判断奴隶数据或主人字段是否缺失
        return await safe_reply(update, context, "你目前不是任何人的奴隶，无法逃跑。")

    owner_id = info["owner"]

    # 如果 owner_id 是 None 或等于自己，则为异常情况
    if not isinstance(owner_id, str) or owner_id == user_id:
        return await safe_reply(update, context, "⚠️ 主人数据异常，无法逃跑。")

    owner_info = get_user_data(chat_id, owner_id)
    user_info = get_user_data(chat_id, user_id)

    # 体力判断
    if user_info.get("stamina", 0) < 1:
        return await safe_reply(update, context, "💤 你当前体力不足（需要 1 点）")

    # 冷却判断
    on_cd, remain = is_on_cooldown(chat_id, user_id, "逃跑", cooldown_seconds=120)
    if on_cd:
        return await safe_reply(
            update, context, f"⌛ 逃跑行为冷却中，请 {remain} 秒后再试。"
        )

    # 消耗体力
    user_info["stamina"] -= 1

    # 逃跑成功
    if calculate_success(user_info["luck"], ESCAPE_LIMIT):
        group[user_id]["owner"] = None
        group[user_id]["ownername"] = "无主"
        user_info["luck"] = max(0, user_info.get("luck", 100) - 5)

        save_user_data(chat_id, user_id, user_info)
        save_json(SLAVE_FILE, data)

        return await safe_reply(update, context, "🎉 逃跑成功！你已恢复自由。")

    # 逃跑失败：赔偿金币
    penalty = 20
    price = info.get("price", 100)
    deduct = max(int(price * 0.2), penalty)

    user_info["balance"] = user_info.get("balance", 100) - deduct
    owner_info["balance"] = owner_info.get("balance", 100) + deduct

    save_user_data(chat_id, user_id, user_info)
    save_user_data(chat_id, owner_id, owner_info)

    return await safe_reply(
        update, context, f"❌ 逃跑失败，你被抓回并赔偿了 {deduct} 枚金币给主人。"
    )


@register_command("查看主人", "我的主人")
@feature_required(FEATURE_FRIENDS)
# @register_cmd("owner")  # 或者你在主处理逻辑中添加关键词匹配
async def view_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})
    info = group.get(user_id)

    if (
        not info
        or not info.get("owner")
        or info.get("owner") in [None, "None", "", "无主"]
    ):
        return await safe_reply(update, context, "🙅‍♂️ 你当前没有主人，自由之身。")

    owner_id = info["owner"]
    owner_name = info.get("ownername", "未知主人")
    price = info.get("price", 0)

    reply = f"🤝 你当前的主人是：{owner_name}\n"
    reply += f"💰 当前奴隶价格：{price} 金币"

    return await safe_reply(update, context, reply)


@register_command("赎身")
@feature_required(FEATURE_FRIENDS)
async def free_slave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})
    info = group.get(user_id)
    if not info:
        return await safe_reply(update, context, "你目前不是任何人的奴隶，无需赎身。")

    owner_id = info["owner"]
    price = info["price"]
    balance = get_balance(chat_id, user_id)
    deduct = int(price * 2)

    user_info = get_user_data(chat_id, user_id)

    if user_info.get("stamina", 0) < 1:
        return await safe_reply(update, context, f"💤 你当前体力不足（需要 1 点）")

    # 冷却判断 6小时
    on_cd, remain = is_on_cooldown(chat_id, user_id, "赎身", cooldown_seconds=21600)
    if on_cd:
        return await safe_reply(
            update, context, f"⌛ 赎身行为冷却中，请 {remain} 秒后再试。"
        )

    user_info["stamina"] = user_info["stamina"] - 1
    save_user_data(chat_id, user_id, user_info)

    if balance < deduct:
        return await safe_reply(
            update, context, f"❌ 赎身失败，你还差 {price * 2 - balance} 枚金币。"
        )
    else:
        change_balance(chat_id, user_id, -deduct)
        change_balance(chat_id, owner_id, deduct)

        group[user_id]["owner"] = None
        group[user_id]["ownername"] = "无主"

        return await safe_reply(
            update, context, f"🎉 花费 {deduct} 枚金币赎身成功！你已恢复自由。"
        )


@register_command("我的奴隶")
@feature_required(FEATURE_FRIENDS)
async def my_slave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    buyer_id = str(user.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))

    slaves = [(sid, info) for sid, info in group.items() if info["owner"] == buyer_id]
    if not slaves:
        return await safe_reply(update, context, "你目前没有任何奴隶。")

    # 根据价格降序排序
    slaves.sort(key=lambda x: x[1].get("price", 0), reverse=True)

    lines = ["🧍 你的奴隶列表："]
    for sid, info in slaves:
        name = info.get("nickname", f"ID:{sid}")
        price = info.get("price", 0)
        if is_silent:
            lines.append(f"🔹 {escape(name or '用户')} - 💰 {price} 金币")
        else:
            mention = mention_html(sid, name or "用户")
            lines.append(f"🔹 {mention} - 💰 {price} 金币")

    await safe_reply(update, context, "\n".join(lines), html=(not is_silent))


@register_command("奴隶干活", "派遣干活")
@feature_required(FEATURE_FRIENDS)
async def assign_slave_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = update.effective_user
    chat_id = str(update.effective_chat.id)
    owner_id = str(owner.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})

    hours = 1.0
    if context.args:
        try:
            hours = float(context.args[0])
        except ValueError:
            return await safe_reply(update, context, "用法：奴隶干活 [小时数(1-4)]")
    hours = max(0.5, min(MAX_WORK_HOURS, hours))

    targets = []
    if update.message and update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        slave_id = str(target.id)
        info = group.get(slave_id)
        if not info or info.get("owner") != owner_id:
            return await safe_reply(update, context, "你不是此人的主人，无法派遣。")
        targets = [(slave_id, info, target.full_name)]
    else:
        targets = [(sid, info, info.get("nickname", sid)) for sid, info in _get_owned_slaves(group, owner_id)]
        if not targets:
            return await safe_reply(update, context, "你目前没有任何奴隶。")

    started = []
    skipped = []
    now_ts = int(datetime.now().timestamp())
    for sid, info, name in targets:
        if _is_slave_working(info):
            skipped.append(name)
            continue
        info["work"] = {
            "start_ts": now_ts,
            "hours": hours,
        }
        started.append(name)

    save_json(SLAVE_FILE, data)
    lines = []
    if started:
        lines.append(f"✅ 已派遣 {len(started)} 名奴隶去干活 {hours} 小时。")
    if skipped:
        lines.append("⏳ 已在工作中：" + "，".join(skipped))
    await safe_reply(update, context, "\n".join(lines) if lines else "没有可派遣的奴隶。")


@register_command("结束干活", "提前结束干活")
@feature_required(FEATURE_FRIENDS)
async def finish_slave_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = update.effective_user
    chat_id = str(update.effective_chat.id)
    owner_id = str(owner.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})
    targets = []
    if update.message and update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        slave_id = str(target.id)
        info = group.get(slave_id)
        if not info or info.get("owner") != owner_id:
            return await safe_reply(update, context, "你不是此人的主人，无法结束。")
        targets = [(slave_id, info, target.full_name)]
    else:
        targets = [(sid, info, info.get("nickname", sid)) for sid, info in _get_owned_slaves(group, owner_id)]
        if not targets:
            return await safe_reply(update, context, "你目前没有任何奴隶。")

    total_reward = 0
    finished = []
    skipped = []
    now_ts = datetime.now().timestamp()
    for sid, info, name in targets:
        work = _get_slave_work_info(info)
        if not work:
            skipped.append(name)
            continue
        start_ts = int(work.get("start_ts", 0) or 0)
        hours_plan = float(work.get("hours", 0) or 0)
        if not start_ts or hours_plan <= 0:
            info.pop("work", None)
            skipped.append(name)
            continue
        elapsed_hours = max(0.0, min(hours_plan, (now_ts - start_ts) / 3600.0))
        reward = _calc_work_reward(elapsed_hours, int(info.get("price", 0) or 0))
        info.pop("work", None)
        total_reward += reward
        finished.append((name, elapsed_hours, reward))

    save_json(SLAVE_FILE, data)
    if total_reward > 0:
        change_balance(chat_id, owner_id, total_reward)

    lines = []
    if finished:
        lines.append(f"✅ 已结束 {len(finished)} 名奴隶的工作，总获得 {total_reward} 金币。")
    if skipped:
        lines.append("⏳ 无工作记录：" + "，".join(skipped))
    await safe_reply(update, context, "\n".join(lines) if lines else "没有可结束的工作。")


@register_command(
    "折磨奴隶",
    "唱歌",
    "跳舞",
    "按摩",
    "睡觉",
    "洗澡",
    "捶背",
    "讲故事",
    "去裸奔",
    "挑大粪",
    "跪榴莲",
    "电击",
    # 新增（聊天机器人安全向）
    "喂饭",
    "搓脚",
    "暖床",
    "哄睡",
    "罚站",
    "扇风",
    "端茶",
    "挠痒",
    "写检讨",
    "陪喝酒",
)
@feature_required(FEATURE_FRIENDS)
async def slave_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(
            update, context, "请通过【回复你的奴隶消息】的方式使用此命令。"
        )

    buyer = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    buyer_id = str(buyer.id)
    slave_id = str(target.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})

    user_id = buyer_id
    info = group.get(slave_id)
    if not info or info["owner"] != buyer_id:
        return await safe_reply(update, context, "你不是此人的主人，不能让他干活。")
    if _is_slave_working(info):
        return await safe_reply(update, context, "该奴隶正在工作中，不能折磨。")

    user_info = get_user_data(chat_id, user_id)

    if user_info.get("stamina", 0) < 1:
        return await safe_reply(update, context, f"💤 你当前体力不足（需要 1 点）")

    on_cd, remain = is_on_cooldown(chat_id, buyer_id, f"折磨奴隶:{slave_id}", 300)
    if on_cd:
        return await safe_reply(
            update, context, f"⏳ 你刚折磨过他，请 {remain} 秒后再命令。"
        )

    user_info["mood"] = min(user_info["mood"] + 2, 100)
    user_info["stamina"] = user_info["stamina"] - 1

    save_user_data(chat_id, user_id, user_info)

    actions = {
        "跳舞": f"💃 {target.full_name} 给你跳了一支钢管舞，你很开心。",
        "唱歌": f"🎤 {target.full_name} 唱了一首十八摸，你笑出了声。",
        "按摩": f"💆 {target.full_name} 给你按摩了一小时，你快活似神仙。",
        "睡觉": f"🛏️ {target.full_name} 陪你睡了一觉，做了七次，你很满足。",
        "洗澡": f"🛁 {target.full_name} 亲自给你搓背洗澡，还撒了花瓣和香水泡泡。",
        "捶背": f"👊 {target.full_name} 用小拳拳锤你背，直锤到你骨头发麻。",
        "讲故事": f"📖 {target.full_name} 给你讲了个18禁的睡前故事，你彻夜难眠。",
        "去裸奔": f" {target.full_name} 去裸奔，被警察抓到揍了一顿。",
        "挑大粪": f" {target.full_name} 去挑大粪，不小心弄了一身。正好被你看到",
        "跪榴莲": f" {target.full_name} 被罚跪在榴莲上，痛得直冒汗，但嘴里还忍不住流口水。",
        "电击": f"⚡ {target.full_name} 被你轻轻电了一下，尖叫着抱头滚了三圈。",
        "喂饭": f"🥄 {target.full_name} 一口一口喂你吃饭，生怕你噎着。",
        "搓脚": f"🦶 {target.full_name} 给你搓脚搓到起火，脚底板红得发亮。",
        "暖床": f"🔥 {target.full_name} 先帮你把床暖好，自己却累得满头大汗。",
        "哄睡": f"😴 {target.full_name} 轻声哼歌哄你睡觉，结果自己先睡着了。",
        "罚站": f"🧍 {target.full_name} 被你罚站一小时，腿抖得像筛糠。",
        "扇风": f"🪭 {target.full_name} 给你扇了一晚上的风，胳膊都快废了。",
        "端茶": f"🍵 {target.full_name} 毕恭毕敬地给你端茶倒水，姿态十分熟练。",
        "挠痒": f"🤣 {target.full_name} 被你挠得满地打滚，连连求饶。",
        "写检讨": f"✍️ {target.full_name} 写了一万字检讨书，手都写抽筋了。",
        "陪喝酒": f"🍺 {target.full_name} 陪你喝到天亮，最后抱着马桶痛哭流涕。",
    }

    action = update.message.text.strip()
    text = actions.get(action)
    if text:
        text += f"心情+2"
        return await safe_reply(update, context, text)
    else:
        random_text = random.choice(list(actions.values())) + f"心情+2"
        return await safe_reply(update, context, f"{random_text}")


@register_command("奴隶战斗")
@feature_required(FEATURE_FRIENDS)
async def slave_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请回复你要挑战的用户。")

    attacker = update.effective_user
    defender = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    attacker_id = str(attacker.id)
    defender_id = str(defender.id)

    if attacker_id == defender_id:
        return await safe_reply(update, context, "你不能和自己战斗。")

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})

    attacker_info = group.get(attacker_id, {})
    defender_info = group.get(defender_id, {})

    if attacker_info.get("owner") == defender_id:
        return await safe_reply(update, context, "你不能和你的主人战斗。")
    if defender_info.get("owner") == attacker_id:
        return await safe_reply(update, context, "你不能和你的奴隶战斗。")

    attacker_slaves = _get_owned_slaves(group, attacker_id)
    defender_slaves = _get_owned_slaves(group, defender_id)

    if not attacker_slaves or not defender_slaves:
        return await safe_reply(update, context, "双方必须都有奴隶才能战斗。")

    atk_total = sum(int(info.get("price", 0) or 0) for _, info in attacker_slaves)
    def_total = sum(int(info.get("price", 0) or 0) for _, info in defender_slaves)
    atk_power = atk_total + len(attacker_slaves) * 50
    def_power = def_total + len(defender_slaves) * 50
    if atk_power <= 0 or def_power <= 0:
        return await safe_reply(update, context, "战力不足，无法开战。")

    win_rate = atk_power / (atk_power + def_power)
    attacker_win = random.random() < win_rate

    winner = attacker if attacker_win else defender
    loser = defender if attacker_win else attacker
    winner_id = attacker_id if attacker_win else defender_id
    loser_id = defender_id if attacker_win else attacker_id

    loser_slaves = defender_slaves if attacker_win else attacker_slaves
    loser_total = def_total if attacker_win else atk_total

    reward_text = ""
    if loser_slaves and random.random() < 0.3:
        stolen_id, stolen_info = random.choice(loser_slaves)
        stolen_info["owner"] = winner_id
        stolen_info["ownername"] = winner.full_name
        history = stolen_info.get("history", [])
        history.append(
            {
                "from": loser.full_name,
                "to": winner.full_name,
                "to_id": winner_id,
                "price": stolen_info.get("price", 0),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        stolen_info["history"] = history
        reward_text = f"掠夺了对方奴隶 {stolen_info.get('nickname', stolen_id)}"
    else:
        coins = max(1, int(loser_total * 0.05))
        change_balance(chat_id, winner_id, coins)
        reward_text = f"获得金币 {coins}"

    save_json(SLAVE_FILE, data)

    await safe_reply(
        update,
        context,
        f"⚔️ 战斗结束！胜利者：{winner.full_name}。{reward_text}。",
    )


@register_command("身价排行")
@feature_required(FEATURE_FRIENDS)
async def top_slaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)  # 注意转成字符串以匹配 JSON key
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    all_slaves = load_json(SLAVE_FILE)
    group_slaves = all_slaves.get(chat_id, {})

    if not group_slaves:
        return await update.message.reply_text("目前这个群还没有任何奴隶。")

    # 排序：按 price 倒序排列
    sorted_slaves = sorted(
        group_slaves.items(), key=lambda x: x[1].get("price", 0), reverse=True
    )

    top_n = 10
    lines = ["🏆 奴隶身价排行榜："]
    for i, (user_id, info) in enumerate(sorted_slaves[:top_n], start=1):
        price = info.get("price", 0)
        nickname = info.get("nickname", "未命名")
        if is_silent:
            lines.append(f"{i}. {escape(nickname)} - 💰{price} 金币")
        else:
            mention = mention_html(user_id, nickname)
            lines.append(f"{i}. {mention} - 💰{price} 金币")

    if is_silent:
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
    else:
        await update.message.reply_html("\n".join(lines), disable_web_page_preview=True)


# /slaveHelp 简易帮助
@register_command("奴隶系统")
@feature_required(FEATURE_FRIENDS)
async def slave_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""💡 奴隶系统指令菜单（快收藏，不迷路！）
📥【购买】↪️ 回复某人使用，可将对方收入为你的奴隶。
🆓【赎身】  奴隶主自费放自己一马，重获自由。
💰【打劫】↪️ 回复某人，抢他的钱，可能成功也可能失败！
🛠️【打工】  靠自己劳力挣金币，消耗体力。
🥺【求包养】💸【求打赏】↪️ 低声下气求金币，也许有人心软给你一笔！
💪【自力更生】摆脱包养，回归自由人生。
📒【交易记录】↪️ 回复某人，查看对方的交易明细。
🏃【逃跑】 你是奴隶？试试看能否逃脱命运吧。
🔗【我的奴隶】 查看目前属于你的奴隶们。
🔪【折磨奴隶】↪️ 随机一种方式（如：唱歌、跳舞、吃饭、按摩等）好好“调教”一下你的奴隶。
💎【身价排行】  谁的奴隶最值钱？看看排行！
📖【奴隶系统】  查看本菜单。
🎯【绑架】↪️ 回复某人，有机会将其强行收入旗下（需看保镖等级）。
🛡️【雇佣保镖】  给自己请个保镖，保护不被绑架。
⬆️【保镖升级】  升级保镖，提高防绑能力。
🧍【我的保镖】  查看你当前雇佣的保镖信息。
⚙️ 使用方式：大多数指令通过“回复某人+命令”触发
🔒 有冷却时间、体力或金币消耗，请合理安排！
📬 输入 奴隶系统  随时查看菜单"""
    await safe_reply(update, context, text)


# 奴隶交易历史
@register_command("交易记录")
@feature_required(FEATURE_FRIENDS)
async def slave_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请回复一个奴隶用户，查看其交易历史。")

    target = update.message.reply_to_message.from_user
    slave_id = str(target.id)
    chat_id = str(update.effective_chat.id)

    data = load_json(SLAVE_FILE)
    group = data.get(chat_id, {})
    slave = group.get(slave_id)

    if not slave:
        return await safe_reply(update, context, "此用户不是奴隶。")

    history = slave.get("history", [])
    if not history:
        return await safe_reply(update, context, "暂无交易记录。")

    lines = [f"📜 {target.full_name} 的奴隶交易历史："]
    for h in history:
        lines.append(f"- {h['time']}: {h['from']} ➡ {h['to']}（💰{h['price']}）")

    await safe_reply(update, context, "\n".join(lines))


# 注册命令
def register_slave_handlers(app):
    app.add_handler(CommandHandler("slaveHelp", slave_help))
    app.add_handler(CommandHandler("buy", buy_slave))  # 购买
    app.add_handler(CommandHandler("escape", escape_slave))  # 逃跑
    app.add_handler(CommandHandler("free", free_slave))  # 赎身
    app.add_handler(CommandHandler("mySlave", my_slave))  # 我的奴隶
    app.add_handler(CommandHandler("slavehistory", slave_history))  # 交易记录
    app.add_handler(CommandHandler("slave_work", slave_work))  # 折磨的奴隶
    app.add_handler(CommandHandler("topslaves", top_slaves))  # 身价排行
    app.add_handler(CommandHandler("viewowner", view_owner))  # 查看主人
