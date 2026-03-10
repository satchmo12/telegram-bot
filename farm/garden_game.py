# garden.py
import time
import random
from copy import deepcopy
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import FEATURE_MANOR, feature_required, register_command
from farm.garden_config import GARDEN_CONFIG
from utils import GARDEN_DATA_FILE, format_duration, safe_reply, load_json, save_json
from info.economy import get_user_data, change_balance
from farm.inventory import change_item


BASE_EXPAND_COST = 500
MAX_GARDEN_SIZE = 12
EXPAND_COOLDOWN = 600  # 扩建冷却时间，单位秒

EMPTY_PLOT_TEMPLATE = {
    "type": None,
    "planted_time": None,
    "alive": True,
    "yield_left": 0,
    "stolen_by": [],  # 记录地块被谁偷过
}

PLANT_ICON = {
    "玫瑰": "🌹",
    "满天星": "✨",
    "郁金香": "🌷",
}


def create_empty_plot():
    return deepcopy(EMPTY_PLOT_TEMPLATE)


def create_garden(size: int):
    return [create_empty_plot() for _ in range(size)]


@register_command("我的花园")
@feature_required(FEATURE_MANOR)
async def start_garden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id, user_id = str(update.effective_chat.id), str(user.id)
    data = load_json(GARDEN_DATA_FILE)

    if chat_id not in data:
        data[chat_id] = {}
    if user_id in data[chat_id]:
        return await show_garden(update, context)
    garden = create_garden(2)
    data[chat_id][user_id] = {
        "plots": garden,
        "expansions": 1,
        "last_expand_time": int(time.time()),
    }
    save_json(GARDEN_DATA_FILE, data)

    await safe_reply(
        update, context, "✅ 花园创建成功！拥有2块土地，使用 种花【植物名称】 开始吧！"
    )


@register_command("种花")
@feature_required(FEATURE_MANOR)
async def plant_flower(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id, user_id = str(update.effective_chat.id), str(user.id)
    args = context.args

    if not args:
        return await safe_reply(update, context, "请输入植物名称，例如：种花 玫瑰")

    plant_name = args[0]
    if plant_name not in GARDEN_CONFIG:
        return await safe_reply(
            update, context, "不支持的植物类型，请选择 玫瑰/向日葵/郁金香"
        )

    plant_cfg = GARDEN_CONFIG[plant_name]
    data = load_json(GARDEN_DATA_FILE)
    user_garden = data.get(chat_id, {}).get(user_id)
    if not user_garden:
        return await safe_reply(update, context, "你还没有花园，请先用 我的花园 创建")

    user_data = get_user_data(chat_id, user_id)
    now = int(time.time())

    # 找空地块，field "plots" 或 "land"，你要确认统一用哪个字段
    plots = user_garden.get("plots", [])

    empty_lands = [i for i, land in enumerate(plots) if land.get("type") is None]

    # 支持全部种植
    if len(args) == 2 and args[1] in ["全部", "all"]:
        max_can_plant = user_data["balance"] // plant_cfg["cost"]
        to_plant_count = min(len(empty_lands), max_can_plant)

        if to_plant_count == 0:
            return await safe_reply(update, context, "❌ 没有足够空地或金币来种植。")

        for i in empty_lands[:to_plant_count]:
            plots[i] = {
                "type": plant_name,
                "planted_time": now,
                "alive": True,
                "yield_left": plant_cfg["max_yield"],
            }
            change_balance(chat_id, user_id, -plant_cfg["cost"])

        save_json(GARDEN_DATA_FILE, data)
        return await safe_reply(
            update,
            context,
            f"✅ 成功在 {to_plant_count} 块地种植 {plant_name}！将在 {plant_cfg['grow_time']} 秒后成熟。",
        )

    # 单块种植
    if not empty_lands:
        return await safe_reply(update, context, "土地已满，请花园扩建或收获")

    if user_data["balance"] < plant_cfg["cost"]:
        return await safe_reply(
            update,
            context,
            f"金币不足，种植 {plant_name} 需要 {plant_cfg['cost']} 金币",
        )

    i = empty_lands[0]
    change_balance(chat_id, user_id, -plant_cfg["cost"])
    plots[i] = {
        "type": plant_name,
        "planted_time": now,
        "alive": True,
        "yield_left": plant_cfg["max_yield"],
    }

    save_json(GARDEN_DATA_FILE, data)
    await safe_reply(
        update,
        context,
        f"成功种植 {plant_name}，位于第 {i+1} 块土地 {PLANT_ICON.get(plant_name, '')}",
    )


@register_command("好友花园", "查看花园", "花园状态")
@feature_required(FEATURE_MANOR)
async def show_garden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_json(GARDEN_DATA_FILE)

    if update.message.reply_to_message:
        friend = update.message.reply_to_message.from_user
        friend_id = str(friend.id)
        name = friend.full_name
        user_garden = data.get(chat_id, {}).get(friend_id)
    else:
        user = update.effective_user
        user_id = str(user.id)
        name = user.full_name
        user_garden = data.get(chat_id, {}).get(user_id)

    # user = update.effective_user
    # user_id, name = str(user.id), user.full_name
    # user_garden = data.get(chat_id, {}).get(user_id)
    if not user_garden:
        return await safe_reply(update, context, f"{name} 还没有花园")
    plots = user_garden["plots"]
    now = int(time.time())
    text = f"{name} 的花园有 {len(plots)} 块土地：\n\n"
    for idx, slot in enumerate(plots):
        plot_num = idx + 1
        if slot.get("type") is None:
            text += f"第 {plot_num} 块：🌱 空地\n"
            continue
        p_type = slot["type"]
        p_cfg = GARDEN_CONFIG.get(p_type)
        if not p_cfg:
            text += f"第 {plot_num} 块：❓未知植物\n"
            continue
        icon = PLANT_ICON.get(p_type, "🌿")
        elapsed = now - slot.get("planted_time", 0)
        remaining = p_cfg["grow_time"] - elapsed
        hours, minutes = int(remaining // 3600), int((remaining % 3600) // 60)
        time_str = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"
        if remaining <= 0:
            text += f"第 {plot_num} 块：{icon} {p_type}（{p_cfg['product_name']} ✅ 可收获）\n"
        else:
            text += f"第 {plot_num} 块：{icon} {p_type} ⏳{time_str} 后可收获\n"
    await safe_reply(update, context, text)


@register_command("扩建花园", "花园扩建")
@feature_required(FEATURE_MANOR)
async def expand_garden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id, user_id = str(update.effective_chat.id), str(user.id)
    data = load_json(GARDEN_DATA_FILE)
    user_garden = data.get(chat_id, {}).get(user_id)
    if not user_garden:
        return await safe_reply(update, context, "还没有花园，请先创建")
    expansions = user_garden.get("expansions", 1)
    cost = BASE_EXPAND_COST * expansions
    if len(user_garden["plots"]) >= MAX_GARDEN_SIZE:
        return await safe_reply(
            update, context, f"已达最大上限（{MAX_GARDEN_SIZE}块土地）"
        )

    # 扩建冷却时间限制
    last_expand_time = user_garden.get("last_expand_time", 0)
    cost_time = EXPAND_COOLDOWN * expansions
    now = int(time.time())
    if now - last_expand_time < cost_time:
        remain = cost_time - (now - last_expand_time)
        return await safe_reply(update, context, f"⏳ 请等待 {remain} 秒后再扩建。")

    user_data = get_user_data(chat_id, user_id)
    if user_data["balance"] < cost:
        return await safe_reply(update, context, f"扩建需 {cost} 金币，余额不足")
    change_balance(chat_id, user_id, -cost)
    user_garden["plots"].append(create_empty_plot())
    user_garden["expansions"] += 1
    user_garden["last_expand_time"] = now

    save_json(GARDEN_DATA_FILE, data)

    await safe_reply(
        update, context, f"扩建成功！现有土地 {len(user_garden['plots'])} 块"
    )


@register_command("收获花园", "采花", "偷花", "花园收获")
@feature_required(FEATURE_MANOR)
async def harvest_garden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_json(GARDEN_DATA_FILE)

    is_stealing = False
    if update.message.reply_to_message:
        victim = update.message.reply_to_message.from_user
        victim_id = str(victim.id)
        name = victim.full_name
        user_garden = data.get(chat_id, {}).get(victim_id)
        user_id = str(update.effective_user.id)
        is_stealing = True
    else:
        user = update.effective_user
        user_id = str(user.id)
        name = user.full_name
        user_garden = data.get(chat_id, {}).get(user_id)

    if not user_garden:
        await safe_reply(update, context, f"{name} 还没有花园，请先创建")
        return

    now = int(time.time())
    plots = user_garden.get("plots", [])

    collected = []

    for i, slot in enumerate(plots):
        if slot["type"] is None:
            continue
        p_type = slot["type"]
        p_cfg = GARDEN_CONFIG.get(p_type)
        if not p_cfg:
            continue

        elapsed = now - slot.get("planted_time", 0)
        if elapsed < p_cfg["grow_time"]:
            continue
        # if not slot.get("alive", True):
        #     continue

        amount = slot.get("yield_left", 0)
        if amount <= 0:
            continue

        if is_stealing:
            stolen_by = slot.setdefault("stolen_by", [])
            # last_steal_time_by = slot.setdefault("last_steal_time_by", {})
            # 重复偷同一块地跳过
            if user_id in stolen_by:
                continue

            # # 偷窃冷却10分钟
            # last_steal = last_steal_time_by.get(user_id, 0)
            # if now - last_steal < 600:
            #     continue
            # last_steal_time_by[user_id] = now

            # # 偷窃成功概率70%
            # if random.random() > 0.7:
            #     continue

            steal_amount = min(1, amount)  # 每次偷一份
            slot["yield_left"] = max(amount - steal_amount, 0)
            stolen_by.append(user_id)
            success = change_item(chat_id, user_id, p_cfg["product_name"], steal_amount)
            if success:
                collected.append(
                    f"🕵️ 偷了第 {i+1} 块：{p_cfg['product_name']} +{steal_amount}"
                )
        else:
            # 正常收获全部产物
            success = change_item(chat_id, user_id, p_cfg["product_name"], amount)
            if success:
                # slot["yield_left"] = 0
                # slot["alive"] = False
                # slot["stolen_by"] = []
                # slot["planted_time"] = now
                plots[i] = create_empty_plot()
                collected.append(f"✅ 第 {i+1} 块：{p_cfg['product_name']} +{amount}")

    save_json(GARDEN_DATA_FILE, data)

    if not collected:
        if is_stealing:
            await safe_reply(
                update, context, f"你试图偷 {name} 的花园产物，但什么也没偷到。"
            )
        else:
            await safe_reply(update, context, "当前没有可收获的植物产物～")
        return

    result = "\n".join(collected)
    if is_stealing:
        await safe_reply(update, context, f"🤫 偷窃成功：\n{result}")
    else:
        await safe_reply(update, context, f"🎉 收获完成：\n{result}")


@register_command("花名列表")
@feature_required(FEATURE_MANOR)
async def show_garden_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    crops_list = "\n".join(
        [
            f"{name}："
            f"价格：{info['cost']}  "
            f"生长时间：{format_duration(info['grow_time'])} \n"
            f"售价：{info['sell_price']}  "
            f"产量：{info['max_yield']}  "
            for name, info in GARDEN_CONFIG.items()
        ]
    )

    text = "花名列表：\n" f"{crops_list}\n\n"
    await safe_reply(update, context, text)


# 我的花园
@register_command("花园命令")
# @feature_required(FEATURE_MANOR)
async def garden_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 我的花园命令：\n"
        "我的花园｜种花 <作物>｜收获花园/花园收获/采花｜花名列表｜扩建花园/花园扩建｜好友花园/查看花园/花园状态"
    )
    await safe_reply(update, context, text)


def register_garden_game_handlers(app):
    app.add_handler(CommandHandler("start_garden", start_garden))
    app.add_handler(CommandHandler("plant_flower", plant_flower))
    app.add_handler(CommandHandler("harvest_garden", harvest_garden))
    app.add_handler(CommandHandler("show_garden", show_garden))
    app.add_handler(CommandHandler("show_garden_shop", show_garden_shop))

    app.add_handler(CommandHandler("garden_help", garden_help))
