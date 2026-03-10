import asyncio
import time
from html import escape
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from info.economy import change_balance
from farm.animals_config import ANIMAL_CONFIG
from farm.animals_game import ANIMALS_DATA_FILE
from command_router import FEATURE_MANOR, feature_required, register_command
from farm.garden_config import GARDEN_CONFIG
from farm.garden_game import GARDEN_DATA_FILE, create_empty_plot
from utils import (
    GROUP_LIST_FILE,
    INFO_FILE,
    MANAGER_FILE,
    delete_later,
    load_json,
    save_json,
    safe_reply,
)

from farm.crop_config import CROP_CONFIG  # 作物数据
from farm.farm_game import FARM_DATA_FILE, create_empty_land, get_growth_stage
from farm.inventory import get_user_inventory, change_item, save_user_inventory


INTERVAL_DAY = 86400  # 一天秒数
MANAGER_PRICE_PER_DAY = 100  # 每天管家费用（金币）

ITEM_CONFIG = {
    "管家体验卡": {"name": "农场管家体验卡(1天)", "days": 1},
}


def use_item_manager_card(chat_id, user_id, item_name, count):
    """使用管家体验卡续费"""
    manager_data = load_json(MANAGER_FILE)

    # 道具天数
    days = ITEM_CONFIG[item_name]["days"]
    days *= count

    # 初始化结构
    if chat_id not in manager_data:
        manager_data[chat_id] = {}
    if user_id not in manager_data[chat_id]:
        manager_data[chat_id][user_id] = {}

    now = int(time.time())
    current_expire = manager_data[chat_id][user_id].get("expire_time", 0)

    # 如果管家没过期，直接加天数；过期则从现在算起
    if current_expire > now:
        new_expire = current_expire + days * INTERVAL_DAY
    else:
        new_expire = now + days * INTERVAL_DAY

    manager_data[chat_id][user_id]["expire_time"] = new_expire
    save_json(MANAGER_FILE, manager_data)

    expire_str = datetime.fromtimestamp(new_expire).strftime("%Y-%m-%d %H:%M:%S")
    return f"管家有效期延长 {days} 天，至 {expire_str}"


@register_command("管家到期", "我的管家")
@feature_required(FEATURE_MANOR)
async def get_butler_expire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    manager_data = load_json(MANAGER_FILE)
    now_ts = int(time.time())

    # 检查是否有管家数据
    if chat_id not in manager_data or user_id not in manager_data[chat_id]:
        return await safe_reply(update, context, "❌ 你还没有农场管家。")

    user_data = manager_data[chat_id][user_id]
    expire_ts = user_data.get("expire_time", 0)

    if expire_ts <= now_ts:
        return await safe_reply(update, context, "⌛ 你的农场管家已经到期了。")

    # 转为可读时间
    expire_time = datetime.fromtimestamp(expire_ts)
    remaining_seconds = expire_ts - now_ts
    days = remaining_seconds // 86400
    hours = (remaining_seconds % 86400) // 3600

    msg = (
        f"🧑‍🌾 农场管家到期时间：{expire_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"剩余：{days} 天 {hours} 小时"
    )

    await safe_reply(update, context, msg)


@register_command("设置自动种植", "auto_plant")
@feature_required(FEATURE_MANOR)
async def set_auto_plant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    if not context.args:
        return await safe_reply(update, context, "❗用法：/设置自动种植 <作物名称>")

    crop_name = " ".join(context.args).strip()
    if not crop_name:
        return await safe_reply(update, context, "❗作物名称不能为空")

    # ✅ 验证作物是否存在
    if crop_name not in CROP_CONFIG:
        return await safe_reply(
            update,
            context,
            f"❌ 作物『{crop_name}』不存在，请输入正确的作物名称",
        )

    # 加载管家数据
    manager_data = load_json(MANAGER_FILE)
    if chat_id not in manager_data or user_id not in manager_data[chat_id]:
        return await safe_reply(
            update, context, "❌ 你还没有农场管家，无法设置自动种植。"
        )

    # 保存设置
    user_data = manager_data[chat_id][user_id]
    user_data["auto_plant_crop"] = crop_name

    save_json(MANAGER_FILE, manager_data)

    await safe_reply(update, context, f"✅ 已设置收获后自动种植作物为：{crop_name}")


@register_command("设置自动种花", "auto_plant_flower")
@feature_required(FEATURE_MANOR)
async def set_auto_plant_flower(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    if not context.args:
        return await safe_reply(update, context, "❗用法：设置自动种花 <花名称>")

    flower_name = " ".join(context.args).strip()
    if not flower_name:
        return await safe_reply(update, context, "❗花名称不能为空")

    # ✅ 验证作物是否存在，并且属于花园可种作物
    if flower_name not in GARDEN_CONFIG:
        return await safe_reply(
            update,
            context,
            f"❌ 花『{flower_name}』不存在或不可种植，请输入正确的花名称",
        )

    # 加载管家数据
    manager_data = load_json(MANAGER_FILE)
    if chat_id not in manager_data or user_id not in manager_data[chat_id]:
        return await safe_reply(
            update, context, "❌ 你还没有农场管家，无法设置自动种花。"
        )

    # 保存设置
    user_data = manager_data[chat_id][user_id]
    user_data["auto_plant_flower"] = flower_name  # 仍用 auto_plant_crop 字段

    save_json(MANAGER_FILE, manager_data)

    await safe_reply(update, context, f"✅ 已设置收获后自动种花为：{flower_name}")


@register_command("设置自动饲养", "auto_feed")
@feature_required(FEATURE_MANOR)
async def set_auto_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    if not context.args:
        return await safe_reply(update, context, "❗用法：/设置自动饲养 <动物名称>")

    animal_name = " ".join(context.args).strip()
    if not animal_name:
        return await safe_reply(update, context, "❗动物名称不能为空")

    # 验证动物是否存在配置里
    if animal_name not in ANIMAL_CONFIG:
        return await safe_reply(
            update,
            context,
            f"❌ 动物『{animal_name}』不存在，请输入正确的动物名称",
        )

    # 加载管家数据
    manager_data = load_json(MANAGER_FILE)
    if chat_id not in manager_data or user_id not in manager_data[chat_id]:
        return await safe_reply(
            update, context, "❌ 你还没有农场管家，无法设置自动饲养。"
        )

    # 保存设置
    user_data = manager_data[chat_id][user_id]
    user_data["auto_feed_animal"] = animal_name

    save_json(MANAGER_FILE, manager_data)

    await safe_reply(update, context, f"✅ 已设置自动饲养动物为：{animal_name}")


async def auto_farm_tasks(bot):
    """管家自动执行农场任务（定时执行）"""
    manager_data = load_json(MANAGER_FILE)
    group_cfg_all = load_json(GROUP_LIST_FILE)
    info_all = load_json(INFO_FILE)
    farm_data = load_json(FARM_DATA_FILE)
    animals_data = load_json(ANIMALS_DATA_FILE)
    garden_data = load_json(GARDEN_DATA_FILE)

    now = int(time.time())
    updated_count = 0

    for chat_id, users in manager_data.items():
        for user_id, user in users.items():
            expire_time = user.get("expire_time", 0)
            if expire_time <= now:
                continue  # 跳过已过期管家

            action_log = []  # 记录管家干的活
            harvested_total = {}

            # ---- 农场 ----
            farm = farm_data.get(chat_id, {}).get(user_id)
            if farm:
                for i, land in enumerate(farm["land"]):
                    crop = land.get("crop")
                    planted_time = land.get("planted_time")
                    if not crop or not planted_time:
                        # 若为空地，检查是否设置了自动种植
                        auto_crop = user.get("auto_plant_crop")
                        if auto_crop:
                            farm["land"][i] = {
                                "crop": auto_crop,
                                "planted_time": now,
                                "watered": False,
                                "fertilized": False,
                                "sprayed": False,
                                "yield_left": CROP_CONFIG.get(auto_crop, {}).get(
                                    "max_yield", 10
                                ),
                                "stolen_by": [],  # 记录地块被谁偷过
                            }
                            action_log.append(f"🌱 自动种植 -> {auto_crop}")
                        continue

                    crop_info = CROP_CONFIG.get(crop)
                    if not crop_info:
                        continue

                    grow_time = crop_info["grow_time"]
                    stage = get_growth_stage(land, crop)

                    # 浇水、施肥、杀虫
                    if not land.get("watered") and stage == "苗期":
                        land["watered"] = True
                        land["planted_time"] -= grow_time * 0.1
                        action_log.append(f"💧浇水 -> {crop}")
                    if not land.get("fertilized") and stage == "花期":
                        land["fertilized"] = True
                        land["planted_time"] -= grow_time * 0.1
                        action_log.append(f"🌿施肥 -> {crop}")
                    if not land.get("sprayed") and stage == "果期":
                        land["sprayed"] = True
                        action_log.append(f"🐛杀虫 -> {crop}（增产）")

                    # 收获
                    num = land.get("yield_left", 10)
                    if not land.get("sprayed"):
                        num -= 2
                    if now - planted_time >= grow_time:
                        harvested_total[crop] = harvested_total.get(crop, 0) + num
                        farm["land"][i] = create_empty_land()
                        updated_count += 1

                        # 自动种植管家设置作物
                        auto_crop = user.get("auto_plant_crop")
                        if auto_crop:
                            total_cost = CROP_CONFIG.get(auto_crop, {}).get(
                                "seed_cost", 10
                            )
                            change_balance(chat_id, user_id, -total_cost)
                            farm["land"][i] = {
                                "crop": auto_crop,
                                "planted_time": now,
                                "watered": False,
                                "fertilized": False,
                                "sprayed": False,
                                "yield_left": CROP_CONFIG.get(auto_crop, {}).get(
                                    "max_yield", 10
                                ),
                                "stolen_by": [],  # 记录地块被谁偷过
                            }
                            action_log.append(f"🌱 自动种植 -> {auto_crop}")

                # 保存农场数据
                farm_data.setdefault(chat_id, {})[user_id] = farm

            # 把农场收成加到背包
            for crop, qty in harvested_total.items():
                change_item(chat_id, user_id, crop, qty)
            if harvested_total:
                action_log.append(
                    "📦 收获："
                    + ", ".join([f"{c}x{q}" for c, q in harvested_total.items()])
                )

            # ---- 牧场 ----
            animals = animals_data.get(chat_id, {}).get(user_id)
            if animals:
                ranch_harvested = {}
                for animal in animals.get("land", []):
                    a_type = animal.get("type")
                    a_cfg = ANIMAL_CONFIG.get(a_type)
                    if not a_cfg:
                        continue
                    interval = a_cfg.get("interval")
                    last_collect = animal.get("produce_time", 0)
                    if interval and now - last_collect >= interval:
                        product_name = a_cfg.get("product_name")
                        amount = animal["yield_left"]
                        animal["produce_time"] = now
                        animal["yield_left"] = a_cfg.get("max_yield")
                        ranch_harvested[product_name] = (
                            ranch_harvested.get(product_name, 0) + amount
                        )
                        action_log.append(
                            f"📦 收集 {a_type} 产物 -> {product_name} x{amount}"
                        )

                for item, qty in ranch_harvested.items():
                    change_item(chat_id, user_id, item, qty)
                if animals:
                    animals_data.setdefault(chat_id, {})[user_id] = animals

            # ---- 花园 ----
            garden = garden_data.get(chat_id, {}).get(user_id)
            if garden:
                garden_harvested = {}
                for i, plot in enumerate(garden.get("plots", [])):
                    g_type = plot.get("type")
                    a_cfg = GARDEN_CONFIG.get(g_type)

                    # 自动收获
                    if g_type and a_cfg:
                        interval = a_cfg.get("grow_time")
                        last_planted = plot.get("planted_time", 0)
                        if interval and now - last_planted >= interval:
                            product_name = a_cfg.get("product_name")
                            amount = plot.get("yield_left", 0)

                            # 清空地块
                            garden["plots"][i] = create_empty_plot()
                            action_log.append(
                                f"📦 收集 {g_type} 产物 -> {product_name} x{amount}"
                            )

                            # 加入收获
                            garden_harvested[product_name] = (
                                garden_harvested.get(product_name, 0) + amount
                            )

                    # 空地自动种花
                    if garden["plots"][i].get("type") is None:
                        auto_flower = user.get("auto_plant_flower")
                        if auto_flower in GARDEN_CONFIG:

                            total_cost = GARDEN_CONFIG.get(auto_flower, {}).get(
                                "cost", 10
                            )
                            change_balance(chat_id, user_id, -total_cost)

                            garden["plots"][i] = {
                                "type": auto_flower,
                                "planted_time": now,
                                "yield_left": GARDEN_CONFIG[auto_flower].get(
                                    "max_yield", 10
                                ),
                            }
                            action_log.append(f"🌱 自动种花 -> {auto_flower}")

                    # 把收获加入背包
                    for item, qty in garden_harvested.items():
                        change_item(chat_id, user_id, item, qty)

                    # 保存数据
                    garden_data.setdefault(chat_id, {})[user_id] = garden

            # ---- 发送通知 ----
            if action_log:
                try:
                    user_profile = (
                        info_all.get(str(chat_id), {})
                        .get("users", {})
                        .get(str(user_id), {})
                        if isinstance(info_all, dict)
                        else {}
                    )
                    display_name = user_profile.get("name") or f"用户{user_id}"
                    safe_name = escape(str(display_name))

                    group_cfg = (
                        group_cfg_all.get(str(chat_id), {})
                        if isinstance(group_cfg_all, dict)
                        else {}
                    )
                    is_silent = bool(group_cfg.get("silent", False))
                    if is_silent:
                        notify_text = (
                            f"🤖 管家为 {safe_name} 完成了以下任务：\n"
                            + "\n".join(action_log)
                        )
                        parse_mode = None
                    else:
                        notify_text = (
                            f"🤖 管家为 <a href='tg://user?id={user_id}'>{safe_name}</a> 完成了以下任务：\n"
                            + "\n".join(action_log)
                        )
                        parse_mode = "HTML"
                    msg = await bot.send_message(
                        chat_id,
                        notify_text,
                        parse_mode=parse_mode,
                    )
                    # 自动删除消息
                    if msg:
                        asyncio.create_task(delete_later(msg, delay=60))
                        
                except Exception as e:
                    print(f"发送消息失败: {e}")

    # 保存所有数据
    save_json(ANIMALS_DATA_FILE, animals_data)
    save_json(FARM_DATA_FILE, farm_data)
    save_json(GARDEN_DATA_FILE, garden_data)
    save_json(MANAGER_FILE, manager_data)

    # print(f"管家自动任务完成，共处理 {updated_count} 块土地。")


def register_farm_manager_handlers(app):
    app.add_handler(CommandHandler("get_butler_expire", get_butler_expire))
