from command_router import FEATURE_MANOR, feature_required, register_command
from farm.animals_config import ANIMAL_PRODUCT_CONFIG
from farm.crafting_config import CRAFT_RECIPES
from farm.crop_config import CROP_CONFIG
from farm.garden_config import GARDEN_CONFIG
from utils import SYSTEM_SHOP_FILE, load_json, save_json, sort_shop
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import register_command
from utils import safe_reply, load_json, save_json
from info.economy import get_balance, change_balance  # 操作金币
from farm.inventory import get_user_inventory, save_user_inventory
from pypinyin import lazy_pinyin

def load_system_shop():
    return load_json(SYSTEM_SHOP_FILE)


def save_system_shop(data):
    save_json(SYSTEM_SHOP_FILE, data)


def get_group_shop(chat_id: str):
    all_data = load_system_shop()
    return all_data.get(chat_id, {})


def save_group_shop(chat_id: str, group_shop: dict):
    all_data = load_system_shop()
    all_data[chat_id] = group_shop
    save_system_shop(all_data)


def add_to_system_shop(chat_id: str, crop_name: str, amount: int, price: int):
    shop = get_group_shop(chat_id)
    if crop_name in shop:
        shop[crop_name]["stock"] += amount
    else:
        shop[crop_name] = {"stock": amount, "price": price}
        # 根据 crop_name 排序（字典的 key）
        shop = sort_shop(shop)
    
    save_group_shop(chat_id, shop)


@register_command("商店")
@feature_required(FEATURE_MANOR)
async def view_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    shop = get_group_shop(chat_id)
    
    if not shop:
        return await safe_reply(update, context, "商店暂时没有任何商品。")
    
      # 获取命令参数
    args = context.args  # Telegram bot 框架提供的参数列表
    category = args[0] if args else "全部"  # 默认显示全部
    
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
    

    lines = []
    for name, info in shop.items():
        if info['stock'] <= 0:
            continue
        if config_keys and name not in config_keys:
            continue  # 不在对应配置里就跳过
        lines.append(f"{name}：库存 {info['stock']}，单价 {info['price']} 金币")

    if not lines:
        return await safe_reply(update, context, "📦 你的库存为空。")
    
    text = f"🛒 本群系统商店 - {category}：\n" + "\n".join(lines)
    await safe_reply(update, context, text)
    
    
@register_command("采购")
@feature_required(FEATURE_MANOR)
async def buy_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    if len(context.args) < 1:
        return await safe_reply(
            update, context, "请输入要购买的作物名称，例如：购买 玉米 [数量]"
        )

    crop_name = context.args[0]
    try:
        quantity = int(context.args[1]) if len(context.args) > 1 else 1
        if quantity <= 0:
            raise ValueError
    except ValueError:
        return await safe_reply(update, context, "❌ 购买数量必须是正整数。")

    shop = get_group_shop(chat_id)
    if crop_name not in shop:
        return await safe_reply(update, context, f"❌ 商店中没有该作物：{crop_name}")

    stock = shop[crop_name]["stock"]
    price = shop[crop_name]["price"]

    if stock < quantity:
        return await safe_reply(
            update, context, f"❌ 商店中 {crop_name} 库存不足（剩余 {stock}）"
        )

    total_cost = int(price * quantity)
    user_balance = get_balance(chat_id, user_id)

    if user_balance < total_cost:
        return await safe_reply(
            update,
            context,
            f"❌ 你的金币不足，需要 {total_cost} 金币，你当前只有 {user_balance} 金币。",
        )

    # 扣金币
    change_balance(chat_id, user_id, -total_cost)

    # 减少商店库存
    shop[crop_name]["stock"] -= quantity
    if shop[crop_name]["stock"] == 0:
        del shop[crop_name]
    save_group_shop(chat_id, shop)

    # 增加玩家库存
    inventory = get_user_inventory(chat_id, user_id)
    inventory[crop_name] = inventory.get(crop_name, 0) + quantity
    save_user_inventory(chat_id, user_id, inventory)

    return await safe_reply(
        update,
        context,
        f"✅ 成功购买 {crop_name} ×{quantity}，花费 {total_cost} 金币。",
    )


def register_shop_game_handlers(app):
    app.add_handler(CommandHandler("shop", view_shop))
    app.add_handler(CommandHandler("buy_item", buy_item))
