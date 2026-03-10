import json
import os
import random
import time
from datetime import datetime
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import FEATURE_MANOR, feature_required, register_command
from utils import ORDERS_FILE, safe_reply, load_json, save_json
from farm.crop_price import CROP_PEICE  # 假设你的作物定义在这里
from farm.inventory import get_user_inventory, change_item  # 需要你的现有库存方法


DAILY_ORDER_COUNT = 10
USER_DAILY_LIMIT = 10


def load_orders():
    return load_json(ORDERS_FILE)


def save_orders(data):
    save_json(ORDERS_FILE, data)


def get_today_str():
    return datetime.now().strftime("%Y-%m-%d")


def generate_daily_orders(chat_id):
    orders_data = load_orders()
    today = get_today_str()

    if chat_id in orders_data and orders_data[chat_id].get("date") == today:
        return orders_data[chat_id]["orders"]

    crops = list(CROP_PEICE.keys())
    orders = []
    for i in range(DAILY_ORDER_COUNT):
        crop = random.choice(crops)
        amount = random.choice([5 * i for i in range(2, 7)])
        price = CROP_PEICE[crop]["sell_price"]
        reward = amount * price * 2  # 奖励公式
        orders.append(
            {
                "id": i + 1,
                "crop": crop,
                "amount": amount,
                "reward": reward,
                "completed_users": [],
            }
        )

    orders_data[chat_id] = {"date": today, "orders": orders}
    save_orders(orders_data)
    return orders


@register_command("今日订单")
@feature_required(FEATURE_MANOR)
async def view_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    orders = generate_daily_orders(chat_id)

    msg = f"📋 今日订单（{get_today_str()}）：\n"
    for o in orders:
        status = "✅ 已完成" if user_id in o["completed_users"] else "❌ 未完成"
        msg += f"ID {o['id']} - 交付 {o['amount']} 个{o['crop']} 💰奖励 {o['reward']} 金币 [{status}]\n"

    await safe_reply(update, context, msg)

@register_command("完成订单")
@feature_required(FEATURE_MANOR)
async def deliver_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    args = context.args

    orders_data = load_orders()
    if chat_id not in orders_data:
        await safe_reply(update, context, "今天还没有订单，请先生成。")
        return

    today_orders = orders_data[chat_id]["orders"]

    # -------------------
    # 情况1：带参数 -> 按编号完成
    # -------------------
    if args:
        try:
            order_id = int(args[0])
        except ValueError:
            await safe_reply(update, context, "订单ID必须是数字")
            return

        order = next((o for o in today_orders if o["id"] == order_id), None)
        if not order:
            await safe_reply(update, context, "订单不存在")
            return

        if user_id in order.get("completed_users", []):
            await safe_reply(update, context, "你已经完成过这个订单了。")
            return

        completed_count = sum(1 for o in today_orders if user_id in o.get("completed_users", []))
        if completed_count >= USER_DAILY_LIMIT:
            await safe_reply(update, context, f"你今天已经完成 {USER_DAILY_LIMIT} 个订单了。")
            return

        inventory = get_user_inventory(chat_id, user_id)
        if inventory.get(order["crop"], 0) < order["amount"]:
            await safe_reply(update, context, f"你的 {order['crop']} 不足，无法交付")
            return

        # 扣除作物并发奖励
        change_item(chat_id, user_id, order["crop"], -order["amount"])
        from info.economy import change_balance
        change_balance(chat_id, user_id, order["reward"])

        order.setdefault("completed_users", []).append(user_id)
        save_orders(orders_data)

        await safe_reply(
            update,
            context,
            f"✅ 成功交付 {order['amount']} 个 {order['crop']}，获得 {order['reward']} 金币！",
        )
        return

    # -------------------
    # 情况2：不带参数 -> 自动完成所有能完成的订单
    # -------------------
    inventory = get_user_inventory(chat_id, user_id)
    completed_orders = []
    failed_orders = []

    # 已完成数量
    completed_count = sum(1 for o in today_orders if user_id in o.get("completed_users", []))

    for order in today_orders:
        if user_id in order.get("completed_users", []):
            continue  # 已完成过
        if completed_count >= USER_DAILY_LIMIT:
            break  # 达到每日上限
        if inventory.get(order["crop"], 0) < order["amount"]:
            failed_orders.append(f"❌ {order['crop']} 不足（需要 {order['amount']}）")
            continue

        # ✅ 可以完成
        change_item(chat_id, user_id, order["crop"], -order["amount"])
        from info.economy import change_balance
        change_balance(chat_id, user_id, order["reward"])

        order.setdefault("completed_users", []).append(user_id)
        completed_orders.append(f"✅ 交付 {order['amount']} 个 {order['crop']}，奖励 {order['reward']} 金币")
        completed_count += 1

    save_orders(orders_data)

    if not completed_orders and not failed_orders:
        msg = "没有可以完成的订单。"
    else:
        msg = "📋 完成情况：\n"
        if completed_orders:
            msg += "\n".join(completed_orders) + "\n"
        if failed_orders:
            msg += "\n未完成：\n" + "\n".join(failed_orders)

    await safe_reply(update, context, msg)



def register_farm_order_handlers(app):
    app.add_handler(CommandHandler("view_orders", view_orders))
    app.add_handler(CommandHandler("deliver_order", deliver_order))
