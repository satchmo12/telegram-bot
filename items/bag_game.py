from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from items.bag import get_user_bag, change_bag_item, use_item
from command_router import register_command
from info.economy import change_balance, change_points, get_points
from items.items_config import EXCHANGE_ITEMS
from utils import safe_reply

@register_command("我的背包")
async def show_bag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    inventory = get_user_bag(chat_id, user_id)

    if not inventory:
        return await safe_reply(update, context, "📦 你的背包为空。")

    lines = []
    for crop_name, amount in inventory.items():
        if amount > 0:
            lines.append(f"{crop_name}: {amount}")

    if not lines:
        return await safe_reply(update, context, "📦 你的背包为空。")

    text = "📦 你的背包：\n" + "\n".join(lines)
    await safe_reply(update, context, text)


@register_command("使用")
async def use_bag_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    args = context.args
    if len(args) < 1:
        await safe_reply(update, context, "用法：使用道具 <道具名称> [数量]")
        return

    item_name = args[0]
    count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    if count <= 0:
        await safe_reply(update, context, "❌ 数量必须是正整数")
        return

    inventory = get_user_bag(chat_id, user_id)
    if not inventory or inventory.get(item_name, 0) < count:
        await safe_reply(update, context, f"❌ 你的背包中没有足够的 {item_name}")
        return

    # 扣除背包道具
    change_bag_item(chat_id, user_id, item_name, -count)
    
    msg = use_item(chat_id, user_id, item_name, count)

    # 其他道具使用逻辑
    await safe_reply(update, context, f"✅ 成功使用 {item_name} x{count}" + msg)


def can_exchange_item(chat_id, user_id, item_name, count=1):
    if item_name not in EXCHANGE_ITEMS:
        return False, "该道具不可兑换"
    points = get_points(chat_id, user_id)
    cost = EXCHANGE_ITEMS[item_name]["points_cost"] * count
    if points < cost:
        return False, f"积分不足，需要 {cost} 积分，当前只有 {points} 积分"
    return True, None


def exchange_item(chat_id, user_id, item_name, count=1):
    # 扣积分
    cost = EXCHANGE_ITEMS[item_name]["points_cost"] * count
    if not change_points(chat_id, user_id, -cost):
        return False, "扣除积分失败"
    # 添加道具到背包
    change_bag_item(chat_id, user_id, item_name, count)
    return True, f"成功兑换 {item_name} x{count}"


@register_command("积分兑换")
async def exchange_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    args = context.args
    if len(args) < 1:
        await safe_reply(update, context, "积分兑换 <道具名称> [数量]")
        return

    item_name = args[0]
    count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    if count <= 0:
        await safe_reply(update, context, "❌ 数量必须是正整数")
        return

    can_exchange, msg = can_exchange_item(chat_id, user_id, item_name, count)
    if not can_exchange:
        await safe_reply(update, context, f"❌ {msg}")
        return

    success, msg = exchange_item(chat_id, user_id, item_name, count)
    if success:
        await safe_reply(update, context, f"✅ {msg}")
    else:
        await safe_reply(update, context, f"❌ {msg}")


def register_bag_game_handlers(app):

    app.add_handler(CommandHandler("bag", show_bag))
    app.add_handler(CommandHandler("use_bagitem", use_bag_item))
    app.add_handler(CommandHandler("exchange_command", exchange_command))
