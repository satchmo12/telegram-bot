import os
import json
import random
import asyncio
from html import escape
from datetime import datetime
from telegram import Update
from telegram.helpers import mention_html
from telegram.ext import CommandHandler, ContextTypes

from command_router import FEATURE_FRIENDS, feature_required, register_command
from utils import PET_FILE, get_group_whitelist, group_allowed, load_json, save_json, safe_reply
from info.economy import change_balance, get_balance



# ===== 常量配置 =====
DEFAULT_PET = {
    "stamina": 100,
    "charm": 60,
    "intimacy": 30,
    "hunger": 100,
    "speed": 50,  # ✅ 新增速度属性
}

FEED_GAIN_RANGE = (10, 25)

# ===== 名字生成组件 =====
PREFIXES = ["小", "大"]
COLORS = ["红", "黄", "绿", "蓝", "紫", "白", "黑"]
ANIMALS = ["喵", "狗", "兔", "鼠", "猪"]

race_queue = {}  # {chat_id: {user_id: bet_amount}}
FIXED_BET = 200  # 所有用户固定下注金币

def random_pet_name():
    return f"{random.choice(PREFIXES)}{random.choice(COLORS)}{random.choice(ANIMALS)}"

# ===== 获取用户宠物（自动初始化） =====
def get_user_pet(chat_id, user_id):
    chat_id = str(chat_id)
    user_id = str(user_id)
    data = load_json(PET_FILE)
    pets = data.setdefault(chat_id, {}).setdefault("pet", {})
    pet = pets.setdefault(user_id, {})

    if "name" not in pet:
        pet["name"] = random_pet_name()
    for attr, default in DEFAULT_PET.items():
        pet.setdefault(attr, default)

    # ✅ 仅增加亲密度，不再减少饥饿值
    pet["intimacy"] = min(100, pet["intimacy"] + 1)

    save_json(PET_FILE, data)
    return pet

# ===== 我的宠物 =====
@register_command("我的宠物")
@feature_required(FEATURE_FRIENDS)
async def my_pet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    pet = get_user_pet(chat_id, user_id)

    hunger_status = "🟢 饱饱的" if pet["hunger"] > 80 else \
        "🟡 有点饿" if pet["hunger"] > 50 else \
        "🟠 很饿了" if pet["hunger"] > 20 else \
        "🔴 饿得要死了！"

    msg = (
        f"🐾 你的宠物信息：\n"
        f"📛 名字：{pet['name']}\n"
        f"🧡 亲密度：{pet['intimacy']}/100\n"
        f"🍖 饥饿值：{pet['hunger']}/100（{hunger_status}）\n"
        f"💪 体力值：{pet['stamina']}/100\n"
        f"🚀 速度值：{pet.get('speed', 50)}/100"
    )

    await update.message.reply_text(msg)

# ===== 喂食宠物 =====
@register_command("宠物喂养")
@feature_required(FEATURE_FRIENDS)
async def feed_pet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    data = load_json(PET_FILE)
    pets = data.setdefault(chat_id, {}).setdefault("pet", {})
    pet = pets.get(user_id)

    if not pet:
        return await update.message.reply_text("😅 您还没有宠物，请先使用 /pet 领取一只！")

    if pet["hunger"] >= 95:
        return await update.message.reply_text("😅 宠物已经很饱了，先别再喂了～")

    gain = random.randint(*FEED_GAIN_RANGE)
    pet["hunger"] = min(100, pet["hunger"] + gain)
    pet["intimacy"] = min(100, pet["intimacy"] + 3)
    pet["stamina"] = min(100, pet["stamina"] + 5)

    save_json(PET_FILE, data)

    return await update.message.reply_text(
        f"🍖 你喂了 {pet['name']} 一口好吃的！\n"
        f"🍗 饥饿值 +{gain} → {pet['hunger']}/100\n"
        f"🧡 亲密度上升，当前为 {pet['intimacy']}。\n"
        f"💪 体力略有恢复，现在是 {pet['stamina']}。"
    )

# ===== 训练宠物（提升速度） =====
@register_command("宠物训练")
@feature_required(FEATURE_FRIENDS)
async def train_pet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    pet = get_user_pet(chat_id, user_id)

    if pet["stamina"] < 10:
        return await update.message.reply_text("😵 宠物太累了，需要休息后再训练。")

    pet["stamina"] -= 10
    pet["speed"] = min(100, pet.get("speed", 50) + random.randint(3, 6))

    data = load_json(PET_FILE)
    data[str(chat_id)]["pet"][str(user_id)] = pet
    save_json(PET_FILE, data)

    return await update.message.reply_text(
        f"🏋️ 你训练了 {pet['name']}，速度提升啦！\n"
        f"🚀 当前速度：{pet['speed']} / 100\n"
        f"💪 体力剩余：{pet['stamina']}"
    )
    

@register_command("宠物赛跑")
async def join_race(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    user_id = str(user.id)

    balance = get_balance(chat_id, user_id)
    if balance < FIXED_BET:
        return await update.message.reply_text(f"❌ 你需要至少 {FIXED_BET} 金币才能参赛。")
    

    pet = get_user_pet(chat_id, user_id)

    # 🐾 检查是否拥有宠物
    if not pet:
        return await update.message.reply_text("🐾 你还没有宠物，无法参赛！请先领养一只吧～")

    # 💪 检查宠物状态
    if pet.get("stamina", 0) < 20 or pet.get("hunger", 0) < 10:
        return await update.message.reply_text(
            "⚠️ 宠物状态不足，无法参赛。\n需要体力≥20，饥饿度≥10，请先喂食或休息！"
        )

    if chat_id not in race_queue:
        race_queue[chat_id] = {}

    if user_id in race_queue[chat_id]:
        return await update.message.reply_text("⚠️ 你已报名本场比赛，请等待开始。")

    race_queue[chat_id][user_id] = FIXED_BET
    await update.message.reply_text(f"✅ 报名成功！已自动下注 {FIXED_BET} 金币。")
    

@register_command("开始赛跑")
@feature_required(FEATURE_FRIENDS)
async def start_race(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    group_cfg = get_group_whitelist(context).get(chat_id, {})
    is_silent = bool(group_cfg.get("silent", False))
    if chat_id not in race_queue or not race_queue[chat_id]:
        return await update.message.reply_text("❌ 当前没有玩家报名比赛。")

    participants = race_queue[chat_id]
    data = load_json(PET_FILE)
    pets = data.setdefault(chat_id, {}).setdefault("pet", {})

    results = []
    removed = []

    for user_id, bet in participants.items():
        pet = pets.get(user_id)
        if not pet:
            removed.append(f"❌ 用户 {user_id} 没有宠物，取消参赛资格")
            continue
        if pet["stamina"] < 20 or pet["hunger"] < 10:
            removed.append(f"⚠️ {pet['name']} 体力或饥饿不足，取消参赛资格")
            continue

        score = (
            pet.get("speed", 50)
            + pet.get("intimacy", 30) * 0.3
            + random.uniform(0, 10)
        )
        results.append((user_id, score, bet, pet["name"]))

    if not results:
        return await update.message.reply_text("❌ 没有有效参赛者，比赛取消。")

    for user_id, _, _, _ in results:
        pet = pets[user_id]
        pet["stamina"] = max(0, pet["stamina"] - 20)
        pet["hunger"] = max(0, pet["hunger"] - 10)

    results.sort(key=lambda x: x[1], reverse=True)
    winner = results[0]
    winner_user_id, winner_score, _, _ = winner

    total_pot = sum(bet for _, _, bet, _ in results)

    # 扣金币 & 发奖励
    for user_id, _, bet, _ in results:
        change_balance(chat_id, user_id, -bet)
    change_balance(chat_id, winner_user_id, total_pot)

    save_json(PET_FILE, data)

    # 比赛过程模拟动画
    race_msg = await update.message.reply_text("🏁 比赛即将开始...")
    await asyncio.sleep(1)
    for i in range(3):
        await race_msg.edit_text("🏇" * (i + 1) + " ...冲刺中...")
        await asyncio.sleep(0.7)

    await asyncio.sleep(0.3)
    await race_msg.edit_text("🏁 比赛结束，正在计算结果...")

    # 最终排名结果展示
    msg_lines = [f"🏆 宠物赛跑结果如下：\n"]
    for i, (uid, score, bet, pet_name) in enumerate(results, 1):
        if is_silent:
            user_text = escape(pet_name)
        else:
            user_text = mention_html(uid, pet_name)
        line = f"{i}. {user_text} - 分数：{score:.1f} - 下注：{bet} 金币"
        if uid == winner_user_id:
            line += f" 🎉【冠军】+ {total_pot}"
        msg_lines.append(line)

    # 报错/失格信息附后
    if removed:
        msg_lines.append("\n🚫 未参赛原因：")
        msg_lines.extend(removed)

    race_queue[chat_id] = {}
    await race_msg.edit_text(
        "\n".join(msg_lines),
        parse_mode=("HTML" if not is_silent else None),
    )

@register_command("宠物改名")
@feature_required(FEATURE_FRIENDS)
async def set_pet_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    # 加载数据
    data = load_json(PET_FILE)
    pets = data.setdefault(chat_id, {}).setdefault("pet", {})
    pet = pets.get(user_id)

    # ❗ 检查是否拥有宠物
    if not pet:
        return await update.message.reply_text("🐾 你还没有宠物，无法改名！请先使用 /pet 领取一只。")

    # ❗ 检查是否输入名称
    if not context.args:
        return await update.message.reply_text(
            "请输入你要给宠物取的名字，例如：`/petname 小黄喵`",
            parse_mode="Markdown"
        )

    name = " ".join(context.args).strip()

    # ✅ 可选：限制名称长度
    if len(name) > 10:
        return await update.message.reply_text("⚠️ 名字太长啦，请控制在10个字以内！")

    # ✅ 设置名称并保存
    pet["name"] = name
    save_json(PET_FILE, data)

    await update.message.reply_text(f"✅ 你的宠物现在叫做：{name} 啦！")

@register_command("下注金额")
@feature_required(FEATURE_FRIENDS)
async def set_bet_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    global FIXED_BET  # ✅ 正确声明使用全局变量
    
     # 🛑 判断比赛队列是否存在并非空
    if chat_id in race_queue and race_queue[chat_id]:
        return await safe_reply(update, context,  "⚠️ 当前没有正在进行的比赛，无法设置下注金额。")

    # 检查权限（可选）
    # member = await context.bot.get_chat_member(chat_id, user.id)
    # if not member.status in ["creator", "administrator"]:
    #     return await safe_reply(update, context,  "⚠️ 只有管理员才能设置下注金额。")
    
    if not context.args or not context.args[0].isdigit():
        return await safe_reply(update, context,  "用法：/setbet <金额>\n例如：/setbet 300")
    
    amount = int(context.args[0])
    if amount < 10:
        return await safe_reply(update, context,  "❌ 下注金额必须大于 10。")

    FIXED_BET = amount
    await safe_reply(update, context,  f"✅ 下注金额已设置为：{amount} 金币")
    
    
def give_daily_stamina_to_all_pets():
    data = load_json(PET_FILE)
    total = 0

    for chat_id, chat_data in data.items():
        pets = chat_data.get("pet", {})
        for user_id, pet in pets.items():
            old_stamina = pet.get("stamina", 100)
            if old_stamina < 100:
                pet["stamina"] = min(100, old_stamina + 80)
                total += 1

    save_json(PET_FILE, data)
    print(f"✅ [{datetime.now():%Y-%m-%d %H:%M:%S}] 已为 {total} 只宠物恢复体力 +80")


# ===== 注册命令 =====
def register_pet_handlers(app):
    app.add_handler(CommandHandler("pet", my_pet))
    app.add_handler(CommandHandler("feed", feed_pet))
    app.add_handler(CommandHandler("train", train_pet))  # ✅ 新增训练命令
    # 加入比赛
    app.add_handler(CommandHandler("joinrace", join_race))  
    # 开始比赛
    app.add_handler(CommandHandler("startrace", start_race))
    
    app.add_handler(CommandHandler("petname", set_pet_name))
    app.add_handler(CommandHandler("setbet", set_bet_amount))
    
    
