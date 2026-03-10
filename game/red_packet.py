import json
import time
import random
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from command_router import register_command
from utils import GROUP_LIST_FILE, RED_PACKET_FILE, load_json, save_json, group_allowed, safe_reply
from info.economy import change_balance, get_balance

SYSTEM_EXPIRE = 300  # 系统红包过期时间：5 分钟
USER_EXPIRE = 3600  # 用户红包过期时间：1 小时

# ====== 工具函数 ======
def get_all_packets():
    return load_json(RED_PACKET_FILE)


def save_all_packets(data):
    save_json(RED_PACKET_FILE, data)


def get_group_list():
    data = load_json(GROUP_LIST_FILE)
    return data if isinstance(data, dict) else {}


def cleanup_expired_packets(chat_id):
    data = get_all_packets()
    now = time.time()
    chat_packets = data.get(str(chat_id), [])

    new_packets = []
    refunds = []
    for pkt in chat_packets:
        expired = now - pkt["time"] > (
            SYSTEM_EXPIRE if pkt["type"] == "system" else USER_EXPIRE
        )
        if expired and pkt["remain"] > 0:
            refunds.append(pkt)
        elif pkt["remain"] == 0:
            pass
        else:
            new_packets.append(pkt)

    if refunds:
        for pkt in refunds:
            change_balance(chat_id, pkt["sender"], pkt["remain_amount"])
            pkt["remain"] = 0  # 标记为0，防止再抢

    data[str(chat_id)] = new_packets
    save_all_packets(data)


# ====== 抢红包 ======
@group_allowed
@register_command("抢红包")
async def grab_packet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name

    cleanup_expired_packets(chat_id)
    data = get_all_packets()
    packets = data.get(chat_id, [])

    # 查找用户可抢的红包
    packet = None
    for p in packets:
        if p["remain"] <= 0 or p["remain_amount"] <= 0:
            continue
        if user_id in p["grabs"]:
            continue
        if p.get("type") == "private" and user_id not in p.get("targets", []):
            continue
        if p.get("type") == "one_to_one" and user_id != p.get("target"):
            continue
        packet = p
        break

    if not packet:
        return await safe_reply(
            update, context, "🎁 当前没有适合你领取的红包 系统红包5分钟超时"
        )

    # === 精准拆包逻辑 ===
    remain = packet["remain"]
    remain_amount = packet["remain_amount"]

    if remain == 1:
        grab_amount = remain_amount
    else:
        max_grab = remain_amount - (remain - 1)
        grab_amount = random.randint(1, max_grab)

    # 更新红包
    packet["remain"] -= 1
    packet["remain_amount"] -= grab_amount
    packet["grabs"][user_id] = grab_amount

    change_balance(chat_id, user_id, grab_amount)
    save_all_packets(data)

    await safe_reply(update, context, f"🎉 {user_name} 抢到 {grab_amount} 金币红包！")


# ====== 用户发红包 ======
@group_allowed
@register_command("发红包")
async def send_packet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    args = context.args

    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        return await safe_reply(update, context, "用法：发红包 金额 个数")

    amount = int(args[0])
    count = int(args[1])

    if amount < count or count <= 0:
        return await safe_reply(update, context, "❌ 金额必须大于份数，且份数 > 0")

    balance = get_balance(chat_id, user_id)
    if balance < amount:
        return await safe_reply(update, context, "❌ 你的金币不足以发这个红包。")

    change_balance(chat_id, user_id, -amount)

    new_packet = {
        "type": "user",
        "sender": user_id,
        "total": amount,
        "remain_amount": amount,
        "remain": count,
        "time": time.time(),
        "grabs": {},
    }

    data = get_all_packets()
    data.setdefault(chat_id, []).append(new_packet)
    save_all_packets(data)

    await safe_reply(
        update,
        context,
        f"🧧 你发了一个 {amount} 金币，共 {count} 份的红包，大家快来抢！",
    )


@group_allowed
@register_command("专属红包")
async def send_one_packet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    sender_id = str(update.effective_user.id)
    args = context.args

    if len(args) < 1 or not args[0].isdigit():
        return await safe_reply(update, context, "用法：专属红包 金额 （回复某人）")

    amount = int(args[0])
    if amount <= 0:
        return await safe_reply(update, context, "❌ 金额必须大于 0")

    # 优先尝试从回复中提取用户
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        # 尝试从实体中提取
        for entity in update.message.entities:
            if entity.type == "text_mention":
                target_user = entity.user
                break

    if not target_user:
        return await safe_reply(update, context, "❌ 请通过回复某人发送红包")

    target_user_id = str(target_user.id)
    if sender_id == target_user_id:
        return await safe_reply(update, context, "❌ 不能给自己发一对一红包")

    balance = get_balance(chat_id, sender_id)
    if balance < amount:
        return await safe_reply(update, context, "❌ 你的金币不足")

    change_balance(chat_id, sender_id, -amount)

    new_packet = {
        "type": "one_to_one",
        "sender": sender_id,
        "target": target_user_id,
        "total": amount,
        "remain_amount": amount,
        "remain": 1,
        "time": time.time(),
        "grabs": {},
    }

    data = get_all_packets()
    data.setdefault(chat_id, []).append(new_packet)
    save_all_packets(data)

    await safe_reply(
        update,
        context,
        f"🎁 你向 {target_user.first_name} 发送了一个专属红包（{amount} 金币）",
    )


# ====== 系统定时红包发放 ======
async def send_system_packet(context: ContextTypes.DEFAULT_TYPE):
    chat_id = "你的群聊ID"  # 可用配置或参数动态注入

    for chat_id in list(get_group_list().keys()):
        try:
            system_packet = {
                "type": "system",
                "sender": "0",
                "total": 300,
                "remain_amount": 300,
                "remain": 5,
                "time": time.time(),
                "grabs": {},
            }

            data = get_all_packets()
            data.setdefault(chat_id, []).append(system_packet)
            save_all_packets(data)
            # 清理
            cleanup_expired_packets(chat_id)

            await context.bot.send_message(
                chat_id=int(chat_id),
                text="🎁 系统发放了一个 300 金币红包（5份），大家快来抢！发送 抢红包 领取红包！",
            )
            print(f"✅ 已发红包到群 {chat_id}")
        except Exception as e:
            print(f"❌ 发红包到群 {chat_id} 失败: {e}")


# ====== 注册 ======
def register_red_packet_handlers(app):
    app.add_handler(CommandHandler("grab", grab_packet))
    app.add_handler(CommandHandler("sendpacket", send_packet))
    app.add_handler(CommandHandler("send1packet", send_one_packet))
