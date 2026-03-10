import math
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import FEATURE_MANOR, feature_required, register_command
from info.economy import change_balance, get_user_data, save_user_data
from farm.animals_config import ANIMAL_PRODUCT_CONFIG
from farm.animals_game import collect_animals
from farm.crafting_config import CRAFT_RECIPES
from farm.crop_config import CROP_CONFIG
from farm.crop_price import CROP_PEICE
from farm.farm_game import farm_harvest
from farm.garden_config import GARDEN_CONFIG
from farm.garden_game import harvest_garden
from farm.shop_game import add_to_system_shop
from utils import apply_reward, format_reward_text, load_json, safe_reply, save_json

from farm.inventory import (
    get_user_inventory,
    change_item,
    save_user_inventory,
)


@register_command("合成")
@feature_required(FEATURE_MANOR)
async def craft_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    inventory = get_user_inventory(chat_id, user_id)

    if len(context.args) < 1:
        return await safe_reply(
            update, context, "请输入要合成的物品名称，例如：合成 面包 [数量]"
        )

    item_name = context.args[0]
    try:
        quantity = int(context.args[1]) if len(context.args) > 1 else 1
        if quantity <= 0:
            raise ValueError
    except ValueError:
        return await safe_reply(
            update, context, "合成数量必须是正整数，例如：合成 面包 2"
        )

    recipe = next(
        (
            v
            for k, v in CRAFT_RECIPES.items()
            if v["name"] == item_name or k == item_name
        ),
        None,
    )

    if not recipe:
        return await safe_reply(update, context, f"没有找到配方：{item_name}")

    # 检查材料是否充足（收集所有不足的材料）
    missing_materials = []
    for material, count in recipe["ingredients"].items():
        required_amount = count * quantity
        owned_amount = inventory.get(material, 0)
        if owned_amount < required_amount:
            shortage = required_amount - owned_amount
            missing_materials.append(
                f"{material} ×{required_amount} (已有 {owned_amount}，缺少 {shortage})"
            )

    if missing_materials:
        missing_text = "\n".join(missing_materials)
        return await safe_reply(
            update, context, f"❌ 材料不足，无法合成：\n{missing_text}"
        )

    # 材料足够 → 扣除
    for material, count in recipe["ingredients"].items():
        inventory[material] -= count * quantity

    # 添加产出
    result_msg = "✅ 合成成功："
    for product, count in recipe["product"].items():
        total_product = count * quantity
        inventory[product] = inventory.get(product, 0) + total_product
        result_msg += f"{product} ×{total_product} "

        # 特殊产物：百花丸 → 加积分
        if product == "百花丸":
            rewards = [
                {"text": "✨ 积分增加 {points} 点", "points": total_product},
            ]
            user_data = get_user_data(chat_id, user_id)
            user_data = apply_reward(user_data, rewards[0])
            save_user_data(chat_id, user_id, user_data)

            result_msg += format_reward_text(rewards[0])

    # 保存数据
    save_user_inventory(chat_id, user_id, inventory)

    return await safe_reply(update, context, result_msg.strip())


@register_command("查看配方")
@feature_required(FEATURE_MANOR)
async def show_recipes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "📦 当前可合成配方：\n"
    for key, recipe in CRAFT_RECIPES.items():
        materials = "、".join(
            [f"{mat}×{cnt}" for mat, cnt in recipe["ingredients"].items()]
        )
        products = "、".join(
            [f"{prod}×{cnt}" for prod, cnt in recipe["product"].items()]
        )

        msg += f"🔸 {recipe['name']}：\n   材料：{materials}\n   产出：{products}\n {recipe['description']} \n\n"
    await safe_reply(update, context, msg.strip())


@register_command("我的库存", "我的仓库")
@feature_required(FEATURE_MANOR)
async def show_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    # 获取命令参数
    args = context.args  # Telegram bot 框架提供的参数列表
    category = args[0] if args else "全部"  # 默认显示全部

    inventory = get_user_inventory(chat_id, user_id)

    # 根据分类过滤库存
    if category == "农场":
        config_keys = CROP_CONFIG.keys()
    elif category == "花园":
        config_keys = GARDEN_CONFIG.keys()
    elif category == "牧场":
        config_keys = ANIMAL_PRODUCT_CONFIG.keys()
    elif category == "合成":
        config_keys = CRAFT_RECIPES.keys()
    else:  # 全部
        category = "全部"
        config_keys = None

    if not inventory:
        return await safe_reply(update, context, "📦 你的库存为空。")

    lines = []
    for crop_name, amount in inventory.items():
        if amount <= 0:
            continue
        if config_keys and crop_name not in config_keys:
            continue  # 不在对应配置里就跳过
        lines.append(f"{crop_name}: {amount}")

    if not lines:
        return await safe_reply(update, context, "📦 你的库存为空。")

    text = f"📦 你的库存 - {category}：\n" + "\n".join(lines)
    await safe_reply(update, context, text)


@register_command("出售")
@feature_required(FEATURE_MANOR)
async def sell_crops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    args = context.args

    inventory = get_user_inventory(chat_id, user_id)

    if not inventory:
        return await safe_reply(update, context, "❌ 你当前没有任何库存作物可出售。")

    # 不传参数，出售全部
    if not args:
        total_earnings = 0
        sell_details = []

        for crop_name, amount in list(inventory.items()):
            if crop_name in CROP_PEICE and amount > 0:
                price = CROP_PEICE[crop_name]["sell_price"]
                earnings = price * amount
                total_earnings += earnings
                sell_details.append(f"{crop_name} x{amount}，获得 {earnings} 金币")
                inventory[crop_name] = 0
                add_to_system_shop(chat_id, crop_name, amount, price * 1.5)

        if total_earnings == 0:
            return await safe_reply(
                update, context, "❌ 你当前没有任何有效库存作物可出售。"
            )

        change_balance(chat_id, user_id, total_earnings)
        save_user_inventory(chat_id, user_id, inventory)
        detail_text = "\n".join(sell_details)
        return await safe_reply(
            update,
            context,
            f"✅ 出售全部成功！共获得 {total_earnings} 金币。\n详细：\n{detail_text}",
        )

    # 指定作物
    crop_name = args[0]

    if crop_name not in CROP_PEICE:
        return await safe_reply(update, context, "❌ 不存在该作物，请检查名称后重试。")

    if crop_name not in inventory or inventory[crop_name] == 0:
        return await safe_reply(
            update, context, f"❌ 你的库存中没有 {crop_name} 可出售。"
        )

    # 指定数量出售
    sell_amount = None
    if len(args) >= 2:
        try:
            sell_amount = int(args[1])
            if sell_amount <= 0:
                return await safe_reply(update, context, "❌ 出售数量必须是正整数。")
        except ValueError:
            return await safe_reply(update, context, "❌ 数量必须是数字。")

    current_amount = inventory.get(crop_name, 0)
    if sell_amount is None or sell_amount > current_amount:
        sell_amount = current_amount  # 出售所有库存

    price = CROP_PEICE[crop_name]["sell_price"]
    earnings = price * sell_amount

    # 扣除库存
    inventory[crop_name] = current_amount - sell_amount
    add_to_system_shop(chat_id, crop_name, sell_amount, price * 1.5)
    # 加金币
    change_balance(chat_id, user_id, earnings)
    save_user_inventory(chat_id, user_id, inventory)
    await safe_reply(
        update,
        context,
        f"✅ 成功出售 {crop_name} x{sell_amount}，获得 {earnings} 金币。",
    )


@register_command("赠送")
@feature_required(FEATURE_MANOR)
async def gift_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(
            update, context, "请回复你要赠送的用户，并输入物品名称，例如：\n赠送 玉米 3"
        )

    user = update.effective_user
    target = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    target_id = str(target.id)

    if user_id == target_id:
        return await safe_reply(update, context, "你不能赠送给自己！")

    if len(context.args) < 1:
        return await safe_reply(
            update, context, "请输入要赠送的物品名称，例如：赠送 玉米 [数量]"
        )

    item_name = context.args[0]
    try:
        quantity = int(context.args[1]) if len(context.args) > 1 else 1
        if quantity <= 0:
            raise ValueError
    except ValueError:
        return await safe_reply(
            update, context, "赠送数量必须是正整数，例如：赠送 玉米 2"
        )

    # 获取库存
    inventory = get_user_inventory(chat_id, user_id)
    target_inventory = get_user_inventory(chat_id, target_id)

    current_count = inventory.get(item_name, 0)
    if current_count < quantity:
        return await safe_reply(
            update, context, f"你没有足够的 {item_name}（当前拥有：{current_count}）"
        )

    # 扣除赠送者物品
    inventory[item_name] -= quantity
    if inventory[item_name] <= 0:
        del inventory[item_name]

    num = min(quantity // 10, 100)
    rewards = [
        {"text": "✨ 魅力值增加 {charm} 点", "charm": num},
    ]

    if item_name.strip() == "玫瑰":
        user_data = get_user_data(chat_id, target_id)
        user_data = apply_reward(user_data, rewards[0])
        save_user_data(chat_id, target_id, user_data)
        # 防止互刷
        quantity = quantity / 2

    # 增加接收者物品
    target_inventory[item_name] = target_inventory.get(item_name, 0) + quantity

    # 保存数据
    save_user_inventory(chat_id, user_id, inventory)
    save_user_inventory(chat_id, target_id, target_inventory)

    return await safe_reply(
        update,
        context,
        f"🎁 你成功赠送 {item_name} ×{quantity} 给 {target.full_name}！",
    )


@register_command("一键收获", "一键采摘", "收获", "采摘")
@feature_required(FEATURE_MANOR)
async def onekey_harvest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await farm_harvest(update, context)
    await collect_animals(update, context)
    await harvest_garden(update, context)


def register_crafting_game_handlers(app):
    app.add_handler(CommandHandler("sell", sell_crops))
    app.add_handler(CommandHandler("inventory", show_inventory))
    app.add_handler(CommandHandler("recipes", show_recipes))
    app.add_handler(CommandHandler("craft", craft_item))

    app.add_handler(CommandHandler("gift", gift_item))
