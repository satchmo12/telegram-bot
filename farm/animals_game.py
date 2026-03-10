# farm_animals.py
import time

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from farm.animals_config import ANIMAL_CONFIG
from command_router import  FEATURE_MANOR, feature_required, register_command
from utils import ANIMALS_DATA_FILE, safe_reply, load_json, save_json
from info.economy import get_user_data, change_balance
from copy import deepcopy
from farm.inventory import change_item


BASE_EXPAND_COST = 1000  # 扩建费用基数
MAX_ANIMALS_COUNT = 12  # 最大土地数
EXPAND_COOLDOWN = 600  # 扩建冷却时间，单位秒

EMPTY_LAND_TEMPLATE = {
    "type": None,
    "start_time": None,
    "produce_time": None,  # 记录生产时间
    "alive": True,
    "yield_left": 0,
    "stolen_by": [],  # 记录地块被谁偷过
}

ANIMALS_ICON = {
    "牛": "🐮",
    "鸡": "🐔",
    "羊": "🐑",  # 产奶一次最多 3 份
    "猪": "🐷",
}


def create_empty_land():
    return deepcopy(EMPTY_LAND_TEMPLATE)


def create_farmland(size: int):
    return [create_empty_land() for _ in range(size)]


@register_command("我的牧场")
@feature_required(FEATURE_MANOR)
async def start_animals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(ANIMALS_DATA_FILE)
    if chat_id not in data:
        data[chat_id] = {}

    if user_id in data[chat_id]:
        return await show_animals(update, context)

    land = create_farmland(2)

    data[chat_id][user_id] = {
        "land": land,
        "expansions": 1,
        "last_expand_time": int(time.time()),
    }

    save_json(ANIMALS_DATA_FILE, data)
    await safe_reply(
        update,
        context,
        "✅ 牧场创建成功！你拥有了 2 块土地。\n使用 饲养 【动物名称】 饲养第一只动物吧！",
    )


@register_command("饲养")
@feature_required(FEATURE_MANOR)
async def feed_animal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    args = context.args
    if not args:
        return await safe_reply(
            update, context, "请输入要饲养的动物，例如：饲养 牛 或 饲养 牛 全部"
        )

    animal_zh = args[0]
    if animal_zh not in ANIMAL_CONFIG:
        return await safe_reply(update, context, "不支持的动物类型，请选择 牛/鸡/羊/猪")
    animal_cfg = ANIMAL_CONFIG[animal_zh]

    # 第二个参数解析
    amount = 1
    if len(args) > 1:
        if args[1] == "全部":
            amount = None  # 表示所有空位
        else:
            try:
                amount = max(1, int(args[1]))
            except ValueError:
                return await safe_reply(update, context, "数量必须是数字或 '全部'")

    data = load_json(ANIMALS_DATA_FILE)
    user_farm = data.get(chat_id, {}).get(user_id)
    if not user_farm:
        return await safe_reply(
            update, context, "你还没有牧场，请先使用 我的牧场 创建一个"
        )

    land = user_farm["land"]
    cost = animal_cfg.get("cost", 9999)
    user_data = get_user_data(chat_id, user_id)

    # 找出空地
    empty_slots = [i for i, slot in enumerate(land) if slot["type"] is None]
    if not empty_slots:
        return await safe_reply(
            update, context, "你所有的土地都被动物占用了，先扩建或释放动物吧～"
        )

    # 确定目标数量
    max_by_space = len(empty_slots)
    max_by_money = user_data["balance"] // cost
    if max_by_money <= 0:
        return await safe_reply(
            update, context, f"金币不足，饲养 {animal_zh} 需要 {cost} 金币"
        )

    if amount is None:  # 全部
        final_amount = min(max_by_space, max_by_money)
    else:
        final_amount = min(amount, max_by_space, max_by_money)

    slots_to_fill = empty_slots[:final_amount]

    # 扣钱并饲养
    total_cost = cost * final_amount
    change_balance(chat_id, user_id, -total_cost)
    for i in slots_to_fill:
        land[i] = {
            "type": animal_zh,
            "start_time": int(time.time()),
            "produce_time": int(time.time()),
            "alive": True,
            "yield_left": animal_cfg["max_yield"],
        }

    save_json(ANIMALS_DATA_FILE, data)
    await safe_reply(
        update,
        context,
        f"成功饲养 {final_amount} 只 {animal_zh}！🐾（花费 {total_cost} 金币）",
    )


@register_command("好友牧场", "查看牧场", "牧场状态")
@feature_required(FEATURE_MANOR)
async def show_animals(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = str(update.effective_chat.id)
    data = load_json(ANIMALS_DATA_FILE)

    if update.message.reply_to_message:
        friend = update.message.reply_to_message.from_user
        friend_id = str(friend.id)
        name = friend.full_name
        user_farm = data.get(chat_id, {}).get(friend_id)
    else:
        user = update.effective_user
        user_id = str(user.id)
        name = user.full_name
        user_farm = data.get(chat_id, {}).get(user_id)

    if not user_farm:
        # await start_animals(update, context)
        return await safe_reply(update, context, f"{name}还没有牧场")

    land = user_farm["land"]
    now = int(time.time())
    text = f"{name} 的牧场共有 {len(land)} 块土地：\n\n"

    for idx, slot in enumerate(land):
        land_num = idx + 1

        if slot.get("type") is None:
            text += f"第 {land_num} 块土地：🌱 空地\n"
            continue

        a_type = slot["type"]
        a_cfg = ANIMAL_CONFIG.get(a_type)
        start_time = slot.get("produce_time", 0)

        if not a_cfg:
            text += f"第 {land_num} 块土地：❓未知动物类型\n"
            continue

        name = a_cfg.get("name", "未知")
        product_name = a_cfg.get("product_name", "产物")
        icon = ANIMALS_ICON.get(name, "🐾")

        interval = a_cfg.get("interval")

        if not interval:
            interval = a_cfg.get("grow_time")

        elapsed = now - start_time
        remaining = interval - elapsed
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        time_str = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"

        if remaining <= 0:
            text += f"第 {land_num} 块：{icon} {name}（{product_name} ✅ 可收获）\n"
        else:
            # minutes = int(remaining // 60)
            text += (
                f"第 {land_num} 块：{icon} {name} {product_name} ⏳{time_str} 后可收\n"
            )

    await safe_reply(update, context, text)


@register_command("扩建牧场", "牧场扩建")
@feature_required(FEATURE_MANOR)
async def expand_animals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data = load_json(ANIMALS_DATA_FILE)
    user_farm = data.get(chat_id, {}).get(user_id)
    if not user_farm:
        return await safe_reply(
            update, context, "你还没有牧场，请先使用 我的牧场 创建一个"
        )

    expansions = user_farm.get("expansions", 1)
    cost = BASE_EXPAND_COST * expansions

    if len(user_farm["land"]) >= MAX_ANIMALS_COUNT:
        return await safe_reply(
            update,
            context,
            f"🚫 已达最大牧场上限（{MAX_ANIMALS_COUNT} 块土地），无法再扩建",
        )

    # 扩建冷却时间限制
    last_expand_time = user_farm.get("last_expand_time", 0)
    cost_time = EXPAND_COOLDOWN * expansions
    now = int(time.time())
    if now - last_expand_time < cost_time:
        remain = cost_time - (now - last_expand_time)
        return await safe_reply(update, context, f"⏳ 请等待 {remain} 秒后再扩建。")

    user_data = get_user_data(chat_id, user_id)
    if user_data["balance"] < cost:
        return await safe_reply(
            update, context, f"💰 扩建需要 {cost} 金币，你的余额不足"
        )

    change_balance(chat_id, user_id, -cost)
    user_farm["land"].append(create_empty_land())
    user_farm["expansions"] += 1
    user_farm["last_expand_time"] = now

    save_json(ANIMALS_DATA_FILE, data)
    await safe_reply(
        update,
        context,
        f"✅ 成功扩建牧场！现在你拥有 {len(user_farm['land'])} 块土地。",
    )


@register_command("洗劫牧场", "牧场收获", "收获牧场")
@feature_required(FEATURE_MANOR)
async def collect_animals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_json(ANIMALS_DATA_FILE)

    is_stealing = False  # 是否为偷取行为

    if update.message.reply_to_message:
        friend = update.message.reply_to_message.from_user
        friend_id = str(friend.id)
        name = friend.full_name
        user_farm = data.get(chat_id, {}).get(friend_id)
        user_id = str(update.effective_user.id)
        is_stealing = True
    else:
        user = update.effective_user
        user_id = str(user.id)
        name = user.full_name
        user_farm = data.get(chat_id, {}).get(user_id)

    if not user_farm:
        await start_animals(update, context)
        return await safe_reply(update, context, f"{name} 还没有牧场～")

    now = int(time.time())
    land = user_farm["land"]

    collected = []

    for i, slot in enumerate(land):
        if slot["type"] is None:
            continue
        a_type = slot["type"]
        a_cfg = ANIMAL_CONFIG.get(a_type)
        if not a_cfg:
            continue
        interval = a_cfg.get("interval")
        product_name = a_cfg.get("product_name")

        if interval and slot.get("produce_time") and slot.get("alive", True):
            elapsed = now - slot["produce_time"]
            if elapsed >= interval:
                # 偷取的收成减少，或有概率失败也可以在这里做
                amount = slot["yield_left"]

                if is_stealing:
                    # 判断是否已经被这个小偷偷过
                    stolen_by = slot.setdefault("stolen_by", [])
                    if user_id in stolen_by:
                        continue  # 这个地块已经被偷过了

                    amount = 1  # 偷的收成减少
                    stolen_by.append(user_id)
                    slot["yield_left"] = slot["yield_left"] - amount

                success = change_item(chat_id, user_id, product_name, amount)

                if success:
                    if is_stealing:
                        collected.append(
                            f"🕵️ 偷到第 {i+1} 块的 {product_name} +{amount}"
                        )
                    else:
                        slot["produce_time"] = now
                        slot["stolen_by"] = []
                        slot["yield_left"] = a_cfg.get("max_yield")
                        collected.append(f"✅ 第 {i+1} 块：{product_name} +{amount}")

    save_json(ANIMALS_DATA_FILE, data)

    if not collected:
        if is_stealing:
            return await safe_reply(
                update, context, f"你试图偷 {name} 的动物产出物，但什么也没偷到。"
            )
        else:
            return await safe_reply(update, context, "当前没有任何动物产出物可以收获～")

    result = "\n".join(collected)
    if is_stealing:
        await safe_reply(update, context, f"🤫 偷菜成功：\n{result}")
    else:
        await safe_reply(update, context, f"🎉 收获完成：\n{result}")


@register_command("出栏", "宰", "屠宰", "杀")
@feature_required(FEATURE_MANOR)
async def butcher_animals(update, context):
    user = update.effective_user
    user_id = str(user.id)
    chat_id = str(update.effective_chat.id)
    now = int(time.time())
    args = context.args

    data = load_json(ANIMALS_DATA_FILE)
    user_farm = data.get(chat_id, {}).get(user_id)
    if not user_farm:
        return await safe_reply(
            update, context, "你还没有牧场，请先使用 我的牧场 创建一个"
        )

    land = user_farm["land"]
    slaughtered = []

    # 获取参数
    animal_type = args[0] if len(args) > 0 and args[0] in ANIMAL_CONFIG else None
    try:
        count = int(args[1]) if len(args) > 1 else None
    except:
        return await safe_reply(update, context, "❌ 数量格式不正确。")

    # 过滤可出栏动物
    candidates = []
    for idx, a in enumerate(land):
        if not a or (animal_type and a["type"] != animal_type):
            continue
        a_type = a["type"]
        animal_cfg = ANIMAL_CONFIG[a_type]
        last_time = a.get("last_produce_time") or a.get("start_time")
        if not last_time:
            continue

        grow_time = animal_cfg.get("grow_time", 3600)
        duration = now - last_time

        if duration >= grow_time:
            candidates.append((idx, duration))

    if not candidates:
        return await safe_reply(update, context, "❌ 没有满足出栏条件的动物。")

    # 按时间倒序，优先出栏生产最久的
    candidates.sort(key=lambda x: -x[1])
    if count:
        candidates = candidates[:count]

    for idx, _ in candidates:
        animal = land[idx]
        a_type = animal["type"]
        meat = ANIMAL_CONFIG[a_type]["butcher_product"]
        # land[idx] = None  # 清空土地
        land[idx] = create_empty_land()
        if change_item(chat_id, user_id, meat, 10):
            slaughtered.append(a_type)

    # 清除 None
    user_farm["land"] = [slot for slot in land if slot is not None]
    save_json(ANIMALS_DATA_FILE, data)

    summary = {}
    for a in slaughtered:
        summary[a] = summary.get(a, 0) + 1

    msg = "✅ 出栏完成：\n"
    for k, v in summary.items():
        msg += f"{k} × {v} => {ANIMAL_CONFIG[k]['butcher_product']}\n"

    await safe_reply(update, context, msg)


# 我的牧场
@register_command("牧场命令")
# @feature_required(FEATURE_MANOR)
async def animals_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 我的牧场命令：\n"
        "我的牧场｜饲养 <动物>｜牧场收获｜牧场扩建/扩建牧场｜好友牧场/查看牧场/牧场状态｜洗劫牧场 | 出栏/屠宰/宰 <动物> <数量>（不填参数=全部）"
    )
    await safe_reply(update, context, text)


def register_animals_game_handlers(app):
    app.add_handler(CommandHandler("start_animals", start_animals))
    app.add_handler(CommandHandler("feed", feed_animal))
    app.add_handler(CommandHandler("animals", show_animals))
    app.add_handler(CommandHandler("collect", collect_animals))
    app.add_handler(CommandHandler("expand", expand_animals))
    app.add_handler(CommandHandler("butcher", butcher_animals))

    app.add_handler(CommandHandler("animals_help", animals_help))
